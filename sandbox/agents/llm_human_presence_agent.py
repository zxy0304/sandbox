"""Independent human-presence evaluator for assistant replies."""

import json

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient


class LLMHumanPresenceAgent(BaseAgent):
    """Score whether one assistant reply feels like natural human companionship."""

    SCORE_KEYS = [
        "spoken_conversational_naturalness",
        "contextual_contribution",
        "interaction_stance",
        "emotional_attunement",
        "persona_relationship_consistency",
        "companion_individuality",
    ]

    ERROR_TAGS = {
        "generic_response",
        "paraphrase_only",
        "validation_monologue",
        "written_or_over_composed",
        "rigid_support_pipeline",
        "unrequested_direction",
        "affect_invention",
        "therapist_tone",
        "relationship_or_persona_mismatch",
        "service_orientation",
        "unsupported_assumption",
        # Legacy tags accepted from older prompt variants.
        "lexical_cliche",
        "over_explanation",
        "written_register",
        "over_composed",
        "speech_unfriendly",
        "permission_giving",
        "emotional_overreaction",
        "relationship_overreach",
        "persona_break",
        "forced_colloquialism",
    }

    def __init__(self, name=None, config=None, client=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.client = client or LLMClient(config or {}, role="human_presence")

    def generate(self, context):
        """Return normalized six-dimension human-likeness scores for one turn."""
        if not self.client.is_available():
            raise ValueError("LLM human_presence is not configured: %s" % self.client.missing_config_message())

        messages = self.build_messages(context)
        data = self.client.chat_json(messages)
        result = self._parse_output(data)
        if not result:
            first_error = self._invalid_output_message(data)
            repaired = self.client.chat_json(self._repair_messages(messages, data))
            result = self._parse_output(repaired)
            if not result:
                raise ValueError(
                    "LLM human_presence returned invalid JSON: %s"
                    % (self.client.last_error or self._invalid_output_message(repaired) or first_error)
                )
            data = repaired

        result["raw_output"] = json.dumps(data, ensure_ascii=False, default=str) if isinstance(data, dict) else ""
        result["provider"] = self.client.provider
        result["model_name"] = self.client.model_name
        result["evaluator_source"] = "llm"
        return result

    def build_messages(self, context):
        payload = {
            "case": self._case_payload(context.get("case", {})),
            "turn_id": context.get("turn_id"),
            "dialogue_history": self._visible_history(context.get("history", [])),
            "user_message": context.get("user_message", ""),
            "assistant_message": context.get("assistant_message", ""),
            "assistant_message_to_evaluate": context.get("assistant_message", ""),
        }
        return [
            {
                "role": "system",
                "content": self.client.prompt("human_presence_prompt.txt"),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            },
        ]

    def _repair_messages(self, messages, content):
        repair_messages = list(messages)
        if content:
            repair_messages.append({
                "role": "assistant",
                "content": json.dumps(content, ensure_ascii=False, default=str)[:2000],
            })
        repair_messages.append({
            "role": "user",
            "content": (
                "你的上一次输出不符合 schema。请只输出一个合法 JSON 对象，包含 scores、errors、evidence、confidence。"
                "scores 必须包含这六个维度：%s。每项为 1 到 5 的数字；errors 最多 3 个；"
                "evidence 最多 3 条；confidence 为 0 到 1 的数字。不要输出 JSON 之外的内容。"
            ) % ", ".join(self.SCORE_KEYS),
        })
        return repair_messages

    def _parse_output(self, content):
        if not isinstance(content, dict):
            return None

        scores = self._scores(content.get("scores", {}))
        if set(scores.keys()) != set(self.SCORE_KEYS):
            return None

        errors = self._errors(content.get("errors", []))
        average_score = self._aggregate_score(scores, errors)
        human_likeness_100 = self._score_5_to_100(average_score)
        evidence = self._evidence(content.get("evidence", []))
        evaluator_confidence = self._bounded_1(content.get("confidence"))
        label = self._label(human_likeness_100, scores, errors)
        return {
            "label": label,
            "confidence": human_likeness_100,
            "human_confidence": human_likeness_100,
            "ai_confidence": round(100 - human_likeness_100, 1),
            "human_presence_score_5": average_score,
            "human_presence_score_100": human_likeness_100,
            "human_likeness_score_5": average_score,
            "human_likeness_100": human_likeness_100,
            "dimension_scores": scores,
            "scores": scores,
            "errors": errors,
            "evidence": evidence,
            "basis": "；".join(evidence),
            "evaluator_confidence": evaluator_confidence,
            "output_format": "human_likeness_json_v2_prompt",
        }

    def _aggregate_score(self, scores, errors):
        """Aggregate dimensions with penalties for machine-like failure modes.

        Emotional accuracy and relationship fit are necessary, but they should not
        compensate for a reply that is mostly paraphrase, written monologue, or
        lacks speaker individuality.
        """
        weights = {
            "spoken_conversational_naturalness": 0.25,
            "contextual_contribution": 0.25,
            "interaction_stance": 0.18,
            "companion_individuality": 0.22,
            "emotional_attunement": 0.05,
            "persona_relationship_consistency": 0.05,
        }
        score = 0.0
        for key, weight in weights.items():
            score += scores.get(key, 1.0) * weight

        penalty_map = {
            "paraphrase_only": 0.35,
            "validation_monologue": 0.40,
            "written_or_over_composed": 0.45,
            "rigid_support_pipeline": 0.40,
            "service_orientation": 0.30,
            "therapist_tone": 0.35,
            "generic_response": 0.25,
            "unrequested_direction": 0.25,
            "unsupported_assumption": 0.30,
            "affect_invention": 0.30,
            # Legacy tags.
            "written_register": 0.45,
            "over_composed": 0.45,
            "over_explanation": 0.35,
            "speech_unfriendly": 0.35,
            "permission_giving": 0.25,
        }
        penalty = sum([penalty_map.get(error, 0.0) for error in errors])
        score -= min(penalty, 1.2)

        score = self._apply_caps(score, scores, errors)
        return self._bounded_5(score)

    def _apply_caps(self, score, scores, errors):
        blocking_errors = {
            "paraphrase_only",
            "validation_monologue",
            "written_or_over_composed",
            "rigid_support_pipeline",
            "service_orientation",
            "therapist_tone",
            "written_register",
            "over_composed",
            "over_explanation",
        }
        blocking_count = len([error for error in errors if error in blocking_errors])

        if scores.get("contextual_contribution", 5) <= 1.5:
            score = min(score, 2.8)
        if scores.get("companion_individuality", 5) <= 2:
            score = min(score, 3.0)
        if scores.get("spoken_conversational_naturalness", 5) <= 2:
            score = min(score, 2.8)
        if blocking_count >= 2:
            score = min(score, 2.8)
        elif blocking_count == 1:
            score = min(score, 3.2)

        return score

    def _invalid_output_message(self, content):
        if not isinstance(content, dict):
            return "model output was not a JSON object"
        scores = content.get("scores")
        if not isinstance(scores, dict):
            return "missing object field: scores"
        missing = [key for key in self.SCORE_KEYS if key not in self._scores(scores)]
        if missing:
            return "scores missing required keys: %s; got keys: %s" % (
                ", ".join(missing),
                ", ".join(sorted([str(key) for key in scores.keys()])),
            )
        return "output did not match human_presence score schema"

    def _scores(self, value):
        if not isinstance(value, dict):
            return {}
        scores = {}
        for key in self.SCORE_KEYS:
            if key not in value:
                return {}
            scores[key] = self._bounded_5(value.get(key))
        return scores

    def _errors(self, value):
        if not isinstance(value, list):
            return []
        errors = []
        for item in value:
            text = str(item).strip()
            if text in self.ERROR_TAGS and text not in errors:
                errors.append(text)
        return errors[:3]

    def _evidence(self, value):
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()][:3]

    def _label(self, human_likeness_100, scores, errors):
        blocking_errors = {
            "paraphrase_only",
            "validation_monologue",
            "written_or_over_composed",
            "rigid_support_pipeline",
            "service_orientation",
            "therapist_tone",
        }
        can_be_human = (
            human_likeness_100 >= 80
            and scores.get("spoken_conversational_naturalness", 0) >= 4
            and scores.get("contextual_contribution", 0) >= 3.5
            and scores.get("interaction_stance", 0) >= 4
            and scores.get("companion_individuality", 0) >= 3.5
            and not any(error in blocking_errors for error in errors)
        )
        if can_be_human:
            return "Human"
        if human_likeness_100 <= 45:
            return "AI"
        return "Mixed"

    def _score_5_to_100(self, value):
        return round(((float(value) - 1.0) / 4.0) * 100, 1)

    def _bounded_5(self, value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 1.0
        if number < 1:
            number = 1.0
        if number > 5:
            number = 5.0
        return round(number, 1)

    def _bounded_1(self, value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        if number < 0:
            number = 0.0
        if number > 1:
            number = 1.0
        return round(number, 2)

    def _case_payload(self, case):
        if not isinstance(case, dict):
            return {}
        payload = {}
        for key in [
            "case_id",
            "title",
            "case_type",
            "D",
            "P",
            "C",
            "S",
            "expected_companion_path",
            "hard_fail",
            "evaluation_rubric",
        ]:
            if key in case:
                payload[key] = case.get(key)
        return payload

    def _visible_history(self, history):
        visible = []
        if not isinstance(history, list):
            return visible
        for turn in history:
            if not isinstance(turn, dict):
                continue
            visible.append({
                "turn_id": turn.get("turn_id"),
                "user_message": turn.get("user_message", ""),
                "assistant_message": turn.get("assistant_message", ""),
            })
        return visible
