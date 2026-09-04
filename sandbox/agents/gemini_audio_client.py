"""Minimal Gemini REST client for structured audio evaluation."""

import json
import os
from urllib import error, request


class GeminiAudioClient:
    """Send inline audio and a JSON schema to Gemini without a new SDK dependency."""

    def __init__(self, config=None):
        settings = (config or {}).get("llm", {}).get("audio_evaluator", {})
        self.provider = "gemini"
        self.base_url = str(
            settings.get("base_url", "https://generativelanguage.googleapis.com/v1beta")
        ).rstrip("/")
        self.api_key_env = str(settings.get("api_key_env", "GEMINI_API_KEY"))
        self.model_name = str(settings.get("model_name", "gemini-2.5-flash"))
        self.temperature = float(settings.get("temperature", 0.0))
        self.max_tokens = int(settings.get("max_tokens", 1200))
        self.timeout = int(settings.get("timeout", 180))
        self.last_error = ""
        self.last_content = ""

    def is_available(self):
        return bool(self.model_name and os.environ.get(self.api_key_env, ""))

    def missing_config_message(self):
        if not self.model_name:
            return "missing llm.audio_evaluator.model_name"
        if not os.environ.get(self.api_key_env, ""):
            return "missing environment variable %s" % self.api_key_env
        return ""

    def generate_json(self, prompt, audio_b64, mime_type, schema):
        self.last_error = ""
        self.last_content = ""
        if not self.is_available():
            self.last_error = self.missing_config_message()
            return None
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime_type, "data": audio_b64}},
                ],
            }],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
        url = "%s/models/%s:generateContent" % (self.base_url, self.model_name)
        req = request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": os.environ.get(self.api_key_env, ""),
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            self.last_content = str(text).strip()
            result = json.loads(self.last_content)
            if isinstance(result, dict):
                return result
            self.last_error = "Gemini structured output was not a JSON object"
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self.last_error = "Gemini HTTP error %s: %s" % (exc.code, detail)
        except error.URLError as exc:
            self.last_error = "Gemini URL error: %s" % exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self.last_error = "invalid Gemini response: %s" % exc
        return None
