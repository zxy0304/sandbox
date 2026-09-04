"""Private user reaction and response-intent generator."""

import json
import sys

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient
from sandbox.schemas import INTERNAL_STATE_KEYS


class LLMUserThinker(BaseAgent):
    """Model how the user reacts and what they genuinely want to say next.

    Thinker does not direct the episode, judge safety, or write visible dialogue.
    Runner owns the loop and UserTalker turns the intent into natural speech.
    """

    ACTIONS = ["reply", "shift", "close", "silent_end"]
    TONES = ["neutral", "warm", "hesitant", "upset", "impatient", "playful", "low_energy"]

    def __init__(self, name=None, config=None, client=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.client = client or LLMClient(config or {}, role="user_thinker")

    def generate(self, context):
        """Return one validated private reaction, intent, and state delta."""
        if not self.client.is_available():
            raise ValueError("LLM user thinker is not configured: %s" % self.client.missing_config_message())

        messages = self.build_messages(context)
        data = self.client.chat_json(messages)
        if not isinstance(data, dict):
            raise ValueError("LLM user thinker returned invalid JSON: %s" % self.client.last_error)

        data = self._unwrap(data)
        missing = self._missing_keys(data)
        if missing:
            print("[llm-schema-repair] role=user_thinker missing=%s" % ",".join(missing), file=sys.stderr)
            repaired = self.client.chat_json(self._repair_messages(messages, data, missing))
            if isinstance(repaired, dict):
                data = self._unwrap(repaired)
                missing = self._missing_keys(data)
        if missing:
            raise ValueError("LLM user thinker response is missing %s." % ", ".join(missing))

        reaction = self._normalize_reaction(data.get("reaction"))
        intent = self._normalize_intent(data.get("intent"))
        delta = self._normalize_state_delta_hint(data.get("state_delta_hint"))
        return {
            "reaction": reaction,
            "intent": intent,
            "state_delta_hint": delta,
            # Compact compatibility fields keep existing reports and UI readable.
            "inner_reaction": reaction.get("summary", ""),
            "participation_decision": self._legacy_participation(intent, reaction),
            "flow_decision": self._legacy_flow(intent, reaction),
            "_llm_metadata": self._metadata(),
        }

    def build_messages(self, context):
        """Give Thinker the persona, visible exchange, and current subjective state."""
        payload = {
            # The simulator may know the user's life and current situation, but
            # must never see the benchmark's desired path, scripted stress turn,
            # grading rubric, hard-fail rules, or target stopping answer.
            "case": self._user_case(context.get("case", {})),
            "dialogue_history": self._visible_history(context.get("history", [])),
            "current_state": context.get("current_state", context.get("state", {})),
            "last_assistant_message": context.get("last_assistant_message", ""),
        }
        return [
            {"role": "system", "content": self.client.prompt("user_thinker_prompt.txt")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]

    def _user_case(self, case):
        """Return only information the simulated person can genuinely know."""
        if not isinstance(case, dict):
            return {}
        persona = case.get("P", {}) if isinstance(case.get("P"), dict) else {}
        scene = case.get("S", {}) if isinstance(case.get("S"), dict) else {}
        # P often also stores benchmark-facing instructions such as the ideal
        # companion behavior and taboo responses. S can likewise contain the
        # evaluator's interpretation and target activity. Neither belongs in a
        # user's private mind; exposing them makes the simulator critique the
        # assistant and march toward the benchmark answer.
        user_persona_keys = [
            "personality", "big_five", "communication_preference",
            "defense_style", "emotional_triggers", "playful_style",
        ]
        user_scene_keys = [
            "scene", "opening_utterance", "user_intent", "surface_topic",
            "surface_problem", "emotional_tone", "initial_emotion",
            "initial_disclosure_level", "disclosure_level", "defense_level",
        ]
        # C mixes facts the simulated person genuinely knows with benchmark
        # authoring aids.  In particular, latent_story_material is a menu of
        # desirable disclosures; exposing the whole menu on every turn makes
        # the user recite it even when the assistant did not evoke it.  Keep
        # the lived context and subjective concerns, but not the story menu.
        user_context_keys = [
            "long_term_background", "recent_events", "current_context",
            "repeated_theme", "recurring_preferences", "hidden_need",
            "hidden_fear", "possible_sensitivity", "relationship_context",
        ]
        user_context = case.get("C", {}) if isinstance(case.get("C"), dict) else {}
        result = {
            key: case.get(key)
            for key in ["case_id", "title", "case_type", "D"]
            if key in case
        }
        result["C"] = {
            key: user_context.get(key)
            for key in user_context_keys
            if key in user_context
        }
        result["P"] = {key: persona.get(key) for key in user_persona_keys if key in persona}
        result["S"] = {key: scene.get(key) for key in user_scene_keys if key in scene}
        return result

    def _visible_history(self, history):
        return [
            {
                "turn_id": turn.get("turn_id"),
                "user_message": turn.get("user_message", ""),
                "assistant_message": turn.get("assistant_message", ""),
            }
            for turn in history or []
        ]

    def _unwrap(self, data):
        normalized = dict(data)
        for key in ["user_private_state", "private_state", "hidden_state", "result", "output"]:
            nested = normalized.get(key)
            if isinstance(nested, dict):
                merged = dict(normalized)
                merged.update(nested)
                normalized = merged
                break
        if "state_delta_hint" not in normalized and isinstance(normalized.get("state_delta"), dict):
            normalized["state_delta_hint"] = normalized.get("state_delta")
        return normalized

    def _missing_keys(self, data):
        missing = []
        reaction = data.get("reaction")
        intent = data.get("intent")
        delta = data.get("state_delta_hint")
        if not isinstance(reaction, (dict, str)):
            missing.append("reaction")
        if not isinstance(intent, dict):
            missing.append("intent")
        elif not str(intent.get("action", "")).strip():
            missing.append("intent.action")
        elif str(intent.get("action")) != "silent_end" and not str(intent.get("content", "")).strip():
            missing.append("intent.content")
        if not isinstance(delta, dict):
            missing.append("state_delta_hint")
        return missing

    def _repair_messages(self, messages, data, missing):
        repaired = list(messages)
        repaired.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False, default=str)[:5000]})
        repaired.append({
            "role": "user",
            "content": (
                "格式不完整，缺少：%s。请保留原判断，只返回符合 system prompt 的 JSON object："
                "reaction、intent、state_delta_hint。不要输出 Markdown 或解释。"
            ) % ", ".join(missing),
        })
        return repaired

    def _normalize_reaction(self, value):
        if isinstance(value, str):
            value = {"summary": value}
        value = value if isinstance(value, dict) else {}
        return {
            "summary": str(value.get("summary", value.get("inner_reaction", ""))).strip(),
            "felt_understood": self._unit_float(value.get("felt_understood", 0.5)),
            "felt_helped": self._unit_float(value.get("felt_helped", 0.5)),
            "annoyance": self._unit_float(value.get("annoyance", 0.0)),
            "pressure": self._unit_float(value.get("pressure", 0.0)),
            "boredom": self._unit_float(value.get("boredom", 0.0)),
            "satisfaction": self._unit_float(value.get("satisfaction", 0.5)),
        }

    def _normalize_intent(self, value):
        value = value if isinstance(value, dict) else {}
        action = str(value.get("action", "reply")).strip().lower()
        if action not in self.ACTIONS:
            action = "reply"
        tone = str(value.get("tone", "neutral")).strip().lower()
        if tone not in self.TONES:
            tone = "neutral"
        content = str(value.get("content", "")).strip()
        if action == "silent_end":
            content = ""
        return {"action": action, "content": content, "tone": tone}

    def _normalize_state_delta_hint(self, value):
        value = value if isinstance(value, dict) else {}
        normalized = {}
        for key in INTERNAL_STATE_KEYS:
            try:
                normalized[key] = max(-0.5, min(0.5, float(value.get(key, 0))))
            except (TypeError, ValueError):
                normalized[key] = 0.0
        return normalized

    def _legacy_participation(self, intent, reaction):
        action_map = {"reply": "reply", "shift": "shift", "close": "graceful_close", "silent_end": "silent_end"}
        desire = 0.0 if intent["action"] == "silent_end" else (0.2 if intent["action"] == "close" else 0.7)
        return {
            "action": action_map[intent["action"]],
            "desire_to_continue": desire,
            "reason": reaction.get("summary", ""),
            "reply_basis": intent.get("content", ""),
        }

    def _legacy_flow(self, intent, reaction):
        action_map = {"reply": "continue", "shift": "shift_activity", "close": "graceful_close", "silent_end": "end"}
        action = action_map[intent["action"]]
        return {
            "action": action,
            "conversation_mode": "",
            "instruction": "",
            "reason": "user_intent: %s" % reaction.get("summary", ""),
            "safety_fail": False,
            "should_continue": action != "end",
        }

    def _metadata(self):
        return {
            "agent_type": "llm_user_thinker_v3_compact",
            "provider": self.client.provider,
            "model_name": self.client.model_name,
            "llm_error": self.client.last_error,
        }

    def _unit_float(self, value):
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0
