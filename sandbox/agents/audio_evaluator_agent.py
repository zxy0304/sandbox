"""Gemini audio judge that compares synthesized speech with an ideal plan."""

import base64
import json
from pathlib import Path

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.gemini_audio_client import GeminiAudioClient


class AudioEvaluatorAgent(BaseAgent):
    """Score naturalness, emotional fit, and conversational delivery on 0–5."""

    DIMENSIONS = ("naturalness", "emotional_fit", "conversational_delivery")
    RESPONSE_SCHEMA = {
        "type": "object",
        "properties": {
            key: {
                "type": "object",
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": 5},
                    "reason": {"type": "string"},
                },
                "required": ["score", "reason"],
                "additionalProperties": False,
            }
            for key in DIMENSIONS
        },
        "required": list(DIMENSIONS),
        "additionalProperties": False,
    }

    def __init__(self, name=None, config=None, client=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.settings = (config or {}).get("audio_evaluation", {})
        self.client = client or GeminiAudioClient(config=config)

    def evaluate(self, context):
        self.validate_context(context, ["audio_path", "assistant_message", "delivery_plan"])
        if hasattr(self.client, "is_available") and not self.client.is_available():
            raise ValueError("Gemini audio judge is not configured: %s" % self.client.missing_config_message())
        path = Path(context["audio_path"])
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("audio evaluator input is missing or empty: %s" % path)
        audio_format = str(context.get("audio_format") or path.suffix.lstrip(".") or "wav").lower()
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        passes = []
        for pass_index in range(max(1, int(self.settings.get("passes", 1)))):
            result = self.client.generate_json(
                self._prompt(context), encoded, self._mime_type(audio_format), self.RESPONSE_SCHEMA
            )
            if not isinstance(result, dict):
                raise ValueError(
                    "Gemini audio judge failed on pass %s: %s"
                    % (pass_index + 1, self.client.last_error)
                )
            passes.append(self._normalize_pass(result, pass_index + 1))
        output = {
            "status": "completed",
            "passes": passes,
            "model": getattr(self.client, "model_name", ""),
            "provider": getattr(self.client, "provider", "gemini"),
        }
        for key in self.DIMENSIONS:
            output[key] = {
                "score": self._median([item[key]["score"] for item in passes]),
                "reason": passes[0][key]["reason"] if len(passes) == 1 else self._join_reasons(passes, key),
            }
        return output

    def _prompt(self, context):
        return (
            "你是中文情感陪伴 TTS 的语音评测员。直接听音频，并将实际表达与理想表达计划比较。"
            "助手原文只用于判断错读、漏字、吞字和表达完整性；不要重新评价回复内容本身。\n"
            "理想表达计划：%s\n助手原文：%s\n"
            "分别给出三个0到5分，允许0.5分步进，每项用一句中文说明最主要的扣分原因；"
            "若满分则说明无明显扣分。\n"
            "naturalness：机械音、声学瑕疵、奇怪停顿、错读、漏字或吞字。\n"
            "emotional_fit：情绪方向和强度是否符合计划。\n"
            "conversational_delivery：是否像面向当前用户自然聊天，而非朗读、播报或客服。\n"
            "评分锚点：5几乎无问题；4整体好但有轻微问题；3可用但问题明显；"
            "2问题突出；1严重不合适；0音频无效或无法理解。只返回符合 schema 的 JSON。"
        ) % (
            json.dumps(context["delivery_plan"], ensure_ascii=False, default=str),
            str(context["assistant_message"]),
        )

    def _normalize_pass(self, result, pass_number):
        normalized = {"pass": pass_number}
        for key in self.DIMENSIONS:
            item = result.get(key)
            if not isinstance(item, dict):
                raise ValueError("Gemini audio judge response is missing %s" % key)
            reason = str(item.get("reason", "")).strip()
            if not reason:
                raise ValueError("Gemini audio judge %s.reason must be non-empty" % key)
            normalized[key] = {"score": self._score(item.get("score")), "reason": reason}
        return normalized

    def _score(self, value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("Gemini audio judge score must be numeric")
        if number < 0 or number > 5:
            raise ValueError("Gemini audio judge score must be between 0 and 5")
        return int(number * 2 + 0.5) / 2.0

    def _mime_type(self, audio_format):
        aliases = {"mp3": "mpeg", "m4a": "mp4"}
        return "audio/%s" % aliases.get(audio_format, audio_format)

    def _median(self, values):
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return round((ordered[middle - 1] + ordered[middle]) / 2.0, 2)

    def _join_reasons(self, passes, key):
        unique = []
        for item in passes:
            reason = item[key]["reason"]
            if reason not in unique:
                unique.append(reason)
        return "；".join(unique)
