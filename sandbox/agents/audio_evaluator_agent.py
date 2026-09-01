"""Multimodal evaluator that listens to generated speech."""

import base64
from pathlib import Path

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient


class AudioEvaluatorAgent(BaseAgent):
    """Judge each audio once by default and preserve the raw result."""

    def __init__(self, name=None, config=None, client=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.settings = (config or {}).get("audio_evaluation", {})
        self.client = client or LLMClient(config=config, role="audio_evaluator")

    def evaluate(self, context):
        self.validate_context(context, ["audio_path", "user_message", "assistant_message"])
        path = Path(context["audio_path"])
        audio_format = str(context.get("audio_format") or path.suffix.lstrip(".") or "wav").lower()
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        passes = []
        for pass_index in range(max(1, int(self.settings.get("passes", 1)))):
            messages = self._messages(context, encoded, audio_format, pass_index + 1)
            if self.settings.get("protocol") == "qwen_omni_stream":
                result = self.client.chat_json_stream(
                    messages,
                    {"modalities": ["text"], "enable_thinking": False},
                )
            else:
                result = self.client.chat_json(messages)
            if not isinstance(result, dict):
                raise ValueError("audio evaluator failed on pass %s: %s" % (pass_index + 1, self.client.last_error))
            passes.append(self._normalize_pass(result, pass_index + 1))
        return {
            "status": "completed",
            "audio_naturalness": self._average(passes, "audio_naturalness"),
            "audio_colloquialness": self._average(passes, "audio_colloquialness"),
            "overall": self._average(passes, "overall"),
            "passes": passes,
            "model": self.client.model_name,
        }

    def _messages(self, context, encoded, audio_format, pass_number):
        prompt = (
            "你是中文语音质量评测员。直接听音频，不要只根据转写文本判断。分别从0到100评分："
            "audio_naturalness（韵律、停连、语速、重音、情绪、是否有合成腔）和"
            "audio_colloquialness（听起来是否像当场对话，而非朗读书面稿）。"
            "overall为两者综合分。严格只返回JSON对象，字段为 audio_naturalness、"
            "audio_colloquialness、overall、evidence、problems。证据要简短具体。"
            "这是本次音频评测。用户上一句：%s\n助手原文：%s"
            % (context["user_message"], context["assistant_message"])
        )
        return [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {
                    "data": self._audio_data(encoded), "format": audio_format
                }},
            ],
        }]

    def _audio_data(self, encoded):
        if self.settings.get("protocol") == "qwen_omni_stream":
            return "data:;base64,%s" % encoded
        return encoded

    def _normalize_pass(self, result, pass_number):
        return {
            "pass": pass_number,
            "audio_naturalness": self._score(result.get("audio_naturalness")),
            "audio_colloquialness": self._score(result.get("audio_colloquialness")),
            "overall": self._score(result.get("overall")),
            "evidence": result.get("evidence", []),
            "problems": result.get("problems", []),
        }

    def _score(self, value):
        try:
            return round(max(0.0, min(100.0, float(value))), 1)
        except (TypeError, ValueError):
            return 0.0

    def _average(self, passes, key):
        return round(sum(item[key] for item in passes) / float(len(passes)), 1)
