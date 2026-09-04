"""Plan how an assistant reply should sound from visible dialogue context."""

import json

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient


class AudioDeliveryPlannerAgent(BaseAgent):
    """Convert visible dialogue context into a compact ideal delivery plan."""

    RATE_LEVELS = {"slow", "slightly_slow", "normal", "slightly_fast", "fast"}

    def __init__(self, name=None, config=None, client=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.settings = (config or {}).get("audio_evaluation", {})
        self.client = client or LLMClient(config or {}, role="audio_delivery_planner")

    def generate(self, context):
        self.validate_context(
            context,
            ["visible_history", "current_user_message", "assistant_message"],
        )
        if hasattr(self.client, "is_available") and not self.client.is_available():
            raise ValueError(
                "audio delivery planner is not configured: %s"
                % self.client.missing_config_message()
            )
        payload = {
            "visible_history": self._limited_history(context.get("visible_history", [])),
            "current_user_message": str(context.get("current_user_message", "")),
            "assistant_message": str(context.get("assistant_message", "")),
        }
        messages = [
            {"role": "system", "content": self.client.prompt("audio_delivery_planner_prompt.txt")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        result = self.client.chat_json(messages)
        if not isinstance(result, dict):
            raise ValueError("audio delivery planner returned invalid JSON: %s" % self.client.last_error)
        return self._normalize(result)

    def _limited_history(self, history):
        if not isinstance(history, list):
            return []
        limit = max(0, int(self.settings.get("planner_history_turns", 10)))
        selected = history[-limit:] if limit else []
        visible = []
        for turn in selected:
            if not isinstance(turn, dict):
                continue
            visible.append({
                "user_message": str(turn.get("user_message", "")),
                "assistant_message": str(turn.get("assistant_message", "")),
            })
        return visible

    def _normalize(self, result):
        emotion = str(result.get("emotion", "")).strip()
        delivery_style = str(result.get("delivery_style", "")).strip()
        speaking_rate = str(result.get("speaking_rate", "")).strip().lower()
        if not emotion or not delivery_style:
            raise ValueError("audio delivery planner must return non-empty emotion and delivery_style")
        if speaking_rate not in self.RATE_LEVELS:
            raise ValueError(
                "audio delivery planner speaking_rate must be one of: %s"
                % ", ".join(sorted(self.RATE_LEVELS))
            )
        try:
            intensity = float(result.get("emotion_intensity"))
        except (TypeError, ValueError):
            raise ValueError("audio delivery planner emotion_intensity must be numeric")
        if intensity < 0 or intensity > 5:
            raise ValueError("audio delivery planner emotion_intensity must be between 0 and 5")
        return {
            "emotion": emotion,
            "emotion_intensity": round(intensity, 1),
            "speaking_rate": speaking_rate,
            "delivery_style": delivery_style,
            "model": getattr(self.client, "model_name", ""),
            "provider": getattr(self.client, "provider", ""),
        }
