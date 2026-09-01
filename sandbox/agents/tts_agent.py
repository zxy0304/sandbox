"""Text-to-speech generation for assistant replies."""

from pathlib import Path

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient


class TTSAgent(BaseAgent):
    """Generate one audio file through an OpenAI-compatible speech endpoint."""

    def __init__(self, name=None, config=None, client=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.settings = (config or {}).get("tts", {})
        self.client = client or LLMClient(config=config, role="tts")

    def generate(self, context):
        self.validate_context(context, ["text", "output_path"])
        audio_format = str(self.settings.get("format", "wav")).lower()
        payload = {
            "model": self.client.model_name,
            "voice": self.settings.get("voice", "alloy"),
            "input": str(context.get("text", "")),
            "response_format": audio_format,
        }
        if self.settings.get("instructions"):
            payload["instructions"] = self.settings["instructions"]
        if self.settings.get("speed") is not None:
            payload["speed"] = self.settings["speed"]
        if self.settings.get("protocol") == "dashscope_tts":
            data = self._dashscope_audio(payload)
        else:
            data = self.client.post_binary(self.settings.get("endpoint", "audio/speech"), payload)
        if data is None:
            raise ValueError("TTS request failed: %s" % self.client.last_error)
        path = Path(context["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {
            "path": str(path),
            "format": audio_format,
            "voice": payload["voice"],
            "model": self.client.model_name,
            "bytes": len(data),
        }

    def _dashscope_audio(self, payload):
        """Call DashScope SpeechSynthesizer and download its temporary audio URL."""
        dashscope_payload = {
            "model": payload["model"],
            "input": {
                "text": payload["input"],
                "voice": payload["voice"],
                "format": payload["response_format"],
                "sample_rate": int(self.settings.get("sample_rate", 24000)),
            },
        }
        if payload.get("instructions"):
            dashscope_payload["input"]["instruction"] = payload["instructions"]
        response = self.client.post_json(
            self.settings.get("endpoint", "services/audio/tts/SpeechSynthesizer"),
            dashscope_payload,
        )
        if not isinstance(response, dict):
            return None
        output = response.get("output", {})
        audio = output.get("audio", {}) if isinstance(output, dict) else {}
        url = audio.get("url") if isinstance(audio, dict) else ""
        if not url:
            self.client.last_error = "DashScope TTS response did not contain output.audio.url"
            return None
        return self.client.get_binary(url)
