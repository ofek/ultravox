import base64
import json
import os
import tempfile
from typing import Any, List, Optional

import gradio_client
import numpy as np
import requests

from ultravox.data import datasets
from ultravox.inference import base


class OpenAIInference(base.VoiceInference):
    def __init__(self, url: str, model: str, api_key: Optional[str] = None):
        self._base_url = url
        self._model = model
        self._api_key = api_key

    def infer(
        self,
        sample: datasets.VoiceSample,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> base.VoiceOutput:
        text = ""
        stats = None
        gen = self.infer_stream(sample, max_tokens, temperature)
        for msg in gen:
            if isinstance(msg, base.InferenceChunk):
                text += msg.text
            elif isinstance(msg, base.InferenceStats):
                stats = msg
        if stats is None:
            raise ValueError("No stats received")
        return base.VoiceOutput(text, stats.input_tokens, stats.output_tokens)

    def infer_stream(
        self,
        sample: datasets.VoiceSample,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> base.InferenceGenerator:
        url = f"{self._base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = {
            "model": self._model,
            "messages": [self._build_message(sample)],
            "stream": True,
        }
        if max_tokens is not None:
            data["max_tokens"] = max_tokens
        if temperature is not None:
            data["temperature"] = temperature
        response = requests.post(url, headers=headers, json=data, stream=True)
        response.raise_for_status()
        for line in response.iter_lines():
            event = line[6:].decode("utf-8")
            if event and event[0] == "{":
                obj = json.loads(event)
                if obj.get("choices") and obj["choices"][0]["delta"].get("content"):
                    yield base.InferenceChunk(obj["choices"][0]["delta"]["content"])
                if obj.get("usage"):
                    yield base.InferenceStats(
                        obj["usage"]["prompt_tokens"], obj["usage"]["completion_tokens"]
                    )

    def _build_message(self, sample: datasets.VoiceSample):
        if sample.audio is None:
            return {"role": "user", "content": sample.messages[0]["content"]}

        fragments = sample.messages[0]["content"].split("<|audio|>")
        assert len(fragments) == 2, "Expected one <|audio|> placeholder"
        url = datasets.audio_to_data_uri(sample.audio, sample.sample_rate)
        parts = [
            {"type": "text", "text": fragments[0]},
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": fragments[1]},
        ]
        return {"role": "user", "content": parts}


class RestInference(base.VoiceInference):
    def __init__(
        self,
        key_env_var: str,
        url: str,
        token: Optional[str] = None,
        token_header: str = "Authorization",
        token_type="Bearer",
    ):
        super().__init__()
        self._url = url
        self._token_header = token_header
        self._token_type = token_type
        self._token = token or os.environ[key_env_var]

    def infer(
        self,
        sample: datasets.VoiceSample,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> base.VoiceOutput:
        headers = {
            "Content-Type": "application/json",
            self._token_header: f"{self._token_type} {self._token}",
        }
        print(headers)
        response = requests.post(self._url, headers=headers, data=sample.to_json())
        response.raise_for_status()
        return response.json()


class BaseTenInference(RestInference):
    def __init__(self, url: str, api_key: Optional[str] = None):
        super().__init__("BASETEN_API_KEY", url, api_key, token_type="Api-Key")


class GradioInference(base.VoiceInference):
    def __init__(self, url: str):
        self._url = url
        self._client = gradio_client.Client(url)
        self._client.upload_files = False

    def infer(
        self,
        sample: datasets.VoiceSample,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> base.VoiceOutput:
        # For some reason the most recent Gradio endpoint only accepts
        # audio as a file, not as a base64-encoded string. There's probably
        # a better way to do this, but I spent too much time on this already.
        # api = self._client.view_api(print_info=False, return_format="dict")
        text = sample.messages[0]["content"]
        if self._url.startswith("https://demo.tincans.ai"):
            args: List[Any] = [text]
            if sample.audio is not None:
                args += [self._encode_audio(sample.audio, sample.sample_rate), None]
            else:
                args += [None, None]
            result = self._client.predict(*args, api_name="/predict")
        else:
            args = [text]
            if sample.audio is not None:
                with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                    f.write(datasets.audio_to_wav(sample.audio, sample.sample_rate))
                    f.flush()
                    args.append(gradio_client.file(f.name))
            else:
                args.append(None)
            result = self._client.predict(*args)
        return base.VoiceOutput(result, 0, 0)

    def _encode_audio(self, pcm: np.ndarray, sample_rate: int, filename: str = "x.wav"):
        wav = datasets.audio_to_wav(pcm, sample_rate)
        uri = f"data:audio/wav;base64,{base64.b64encode(wav).decode('utf-8')}"
        return {
            "name": filename,
            "data": uri,
            "orig_name": filename,
            "size": len(wav),
        }


def create_inference(
    url: str, model: Optional[str], api_key: Optional[str]
) -> base.VoiceInference:
    if (
        url.startswith("https://demo.tincans.ai")
        or url.endswith("gradio.live")
        or url.endswith(":7860")
    ):
        return GradioInference(url)
    elif url.endswith("api.baseten.co/production/predict"):
        return BaseTenInference(url, api_key)
    elif url.endswith("/v1"):
        assert model, "Model must be specified for OpenAI inference"
        return OpenAIInference(url, model, api_key)
    else:
        raise ValueError(f"Unknown inference URL: {url}")
