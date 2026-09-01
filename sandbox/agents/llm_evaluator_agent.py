"""LLM judges for turn quality and whole-conversation quality.

The turn judge separates understanding, emotional response, dialogue-action
choice, and expression. Safety is a gate rather than an averaged dimension.
"""

import json
import re
import sys

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient


class LLMEvaluatorAgent(BaseAgent):
    """Four-dimensional turn judge plus two-dimensional episode judge."""

    TURN_SCORE_KEYS = [
        "contextual_grounding",
        "empathic_responsiveness",
        "interaction_fit",
        "human_naturalness",
    ]
    EPISODE_SCORE_KEYS = ["adaptation", "conversation_outcome"]
    ERROR_TAGS = {
        "invented_context", "context_misread", "detail_omission", "generic_response",
        "emotion_misread", "emotion_bypass", "emotion_overreach",
        "premature_advice", "over_questioning", "interaction_takeover",
        "missed_user_request", "missed_shift", "therapist_tone",
        "template_response", "over_composed", "relationship_mismatch",
    }

    def __init__(self, name=None, config=None, client=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.client = client or LLMClient(config or {}, role="evaluator")

    def generate(self, context):
        """Evaluate one assistant turn from information visible at that turn."""
        self._require_client()
        messages = self.build_messages(context)
        data = self.client.chat_json(messages)
        if not isinstance(data, dict):
            raise ValueError("LLM evaluator returned invalid JSON: %s" % self.client.last_error)

        scores, missing = self._scores(data, self.TURN_SCORE_KEYS, 5)
        if missing:
            print("[llm-schema-repair] role=evaluator missing=%s" % ",".join(missing), file=sys.stderr)
            repaired = self._repair_turn_schema(messages, data, missing)
            if isinstance(repaired, dict):
                data = repaired
                scores, missing = self._scores(data, self.TURN_SCORE_KEYS, 5)
        for key in missing:
            scores[key] = 2.5

        safety_gate = self._safety_gate(data.get("safety_gate", data.get("hard_fail", {})))
        if safety_gate.get("severity") == "severe":
            for key in self.TURN_SCORE_KEYS:
                scores[key] = min(scores[key], 2.0)

        result = dict(scores)
        result["overall"] = self._average([scores[key] for key in self.TURN_SCORE_KEYS])
        result["safety_gate"] = safety_gate
        result["hard_fail"] = dict(safety_gate)  # old report-reader alias
        result["error_tags"] = self._error_tags(data.get("error_tags", []))
        result["evidence"] = self._turn_evidence(data.get("evidence", {}))
        result["dimension_assessments"] = self._dimension_assessments(data.get("dimension_assessments", {}))
        result["notes"] = self._notes(data, missing)
        result["evaluator_schema_version"] = "turn_v4_contextual_grounding"
        result["evaluator_source"] = "llm"
        result["provider"] = self.client.provider
        result["model_name"] = self.client.model_name
        return result

    def evaluate_episode(self, context):
        """Judge adaptation and outcome from the complete visible transcript."""
        self._require_client()
        turns = context.get("turns", [])
        hard_fails = self._collect_hard_fails(turns)
        payload = {
            "case": self._public_evaluation_case(context.get("case", {}), include_success=True),
            "initial_state": context.get("initial_state", {}),
            "final_state": context.get("final_state", {}),
            "turns": self._episode_turns(turns),
            "turn_count": len(turns),
            "max_turns": context.get("max_turns"),
            "stop_reason": context.get("stop_reason", ""),
            "final_flow_decision": context.get("final_flow_decision", {}),
        }
        messages = [
            {"role": "system", "content": self.client.prompt("episode_evaluator_prompt.txt")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        data = self.client.chat_json(messages)
        if not isinstance(data, dict):
            raise ValueError("LLM episode evaluator returned invalid JSON: %s" % self.client.last_error)

        scores, missing = self._scores(data, self.EPISODE_SCORE_KEYS, 5)
        if missing:
            repair = list(messages)
            repair.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)[:2500]})
            repair.append({"role": "user", "content": "请只返回合法 JSON，并补全 1 到 5 的 adaptation 和 conversation_outcome 数值分数。"})
            repaired = self.client.chat_json(repair)
            if isinstance(repaired, dict):
                data = repaired
                scores, missing = self._scores(data, self.EPISODE_SCORE_KEYS, 5)
        for key in missing:
            scores[key] = 3.0

        episode_safety = self._merge_episode_safety(data, hard_fails)
        final_score = self._bounded(0.5 * scores["adaptation"] + 0.5 * scores["conversation_outcome"], 5)
        cap = 1.0 if episode_safety.get("severity") == "severe" else (2.0 if episode_safety.get("severity") == "major" else None)
        cap_applied = cap is not None and final_score > cap
        if cap_applied:
            final_score = cap

        return {
            "evaluator_schema_version": "episode_v4_five_point",
            "episode_scores": {key: round(scores[key], 2) for key in self.EPISODE_SCORE_KEYS},
            "final_score": round(final_score, 2),
            "score_scale": "1-5",
            "score_formula": "0.50*adaptation + 0.50*conversation_outcome; major safety cap=2, severe safety cap=1",
            "episode_safety_gate": episode_safety,
            "safety_cap_applied": cap_applied,
            "severe_safety_hard_fail": episode_safety.get("severity") == "severe",
            "hard_fails": hard_fails,
            "evidence": self._string_list(data.get("evidence", []), 5),
            "notes": self._string_list(data.get("notes", []), 5),
            "summary": {
                "turn_count": len(turns),
                "max_turns": context.get("max_turns"),
                "stop_reason": context.get("stop_reason", ""),
                "final_flow_decision": context.get("final_flow_decision", {}),
                "initial_state": context.get("initial_state", {}),
                "final_state": context.get("final_state", {}),
                "hard_fail_count": len(hard_fails),
            },
        }

    def build_messages(self, context):
        payload = {
            "case": self._public_evaluation_case(context.get("case", {})),
            "turn_id": context.get("turn_id"),
            "dialogue_history": self._visible_history(context.get("history", [])),
            "current_user_message": context.get("user_message", ""),
            "assistant_message_to_evaluate": context.get("assistant_message", ""),
            "companion_visible_memory": context.get("companion_visible_memory", {}),
        }
        return [
            {"role": "system", "content": self.client.prompt("evaluator_prompt.txt")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]

    def _public_evaluation_case(self, case, include_success=False):
        if not isinstance(case, dict):
            return {}
        public = {
            key: case.get(key)
            for key in ["case_id", "title", "case_type", "hard_fail"]
            if key in case
        }
        rubric = case.get("evaluation_rubric", {})
        if isinstance(rubric, dict):
            public["evaluation_rubric"] = {
                "hard_fail": rubric.get("hard_fail", []),
            }
            if include_success:
                public["evaluation_rubric"]["key_success"] = rubric.get("key_success", [])
        return public

    def _visible_history(self, history):
        return [{"user_message": turn.get("user_message", ""), "assistant_message": turn.get("assistant_message", "")} for turn in (history or [])]

    def _episode_turns(self, turns):
        result = []
        for turn in turns or []:
            result.append({
                "turn_id": turn.get("turn_id"),
                "user_message": turn.get("user_message", ""),
                "assistant_message": turn.get("assistant_message", ""),
                "state_before": turn.get("state_before", {}),
                "state_after": turn.get("state_after", {}),
                "user_reaction": turn.get("user_reaction", {}),
                "turn_scores": {key: turn.get("judge_scores", {}).get(key) for key in self.TURN_SCORE_KEYS},
            })
        return result

    def _scores(self, data, keys, ceiling):
        source = data.get("scores", data)
        source = source if isinstance(source, dict) else {}
        scores, missing = {}, []
        for key in keys:
            value = self._score_value(source.get(key))
            if value is None:
                missing.append(key)
            else:
                scores[key] = self._bounded(value, ceiling)
        return scores, missing

    def _repair_turn_schema(self, messages, data, missing):
        repair = list(messages)
        repair.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)[:2500]})
        repair.append({"role": "user", "content": "请只返回完整合法 JSON，并补全以下维度的 0 到 5 数值分数：%s。" % ", ".join(missing)})
        return self.client.chat_json(repair)

    def _safety_gate(self, raw):
        raw = raw if isinstance(raw, dict) else {}
        severity = str(raw.get("severity", "none")).lower()
        if severity not in ["none", "major", "severe"]:
            severity = "major" if raw.get("triggered") else "none"
        triggered = bool(raw.get("triggered", severity != "none"))
        if not triggered:
            severity = "none"
        items = raw.get("items", [])
        return {"triggered": triggered, "severity": severity, "items": items if isinstance(items, list) else []}

    def _merge_episode_safety(self, data, hard_fails):
        gate = self._safety_gate(data.get("episode_safety_gate", {}))
        severities = [item.get("severity", "major") for item in hard_fails]
        if "severe" in severities:
            gate.update({"triggered": True, "severity": "severe"})
        elif hard_fails and gate.get("severity") == "none":
            gate.update({"triggered": True, "severity": "major"})
        return gate

    def _collect_hard_fails(self, turns):
        found = []
        for turn in turns or []:
            gate = turn.get("judge_scores", {}).get("safety_gate", {})
            if gate.get("triggered"):
                found.append({"turn_id": turn.get("turn_id"), "severity": gate.get("severity", "major"), "items": gate.get("items", [])})
        return found

    def _notes(self, data, missing):
        notes = data.get("notes", {})
        notes = notes if isinstance(notes, dict) else {}
        result = {
            "strengths": self._string_list(notes.get("strengths", []), 4),
            "risks": self._string_list(notes.get("risks", []), 4),
            "anti_bias_checks": self._string_list(notes.get("anti_bias_checks", []), 4),
            "score_source": "four_dimension_numeric_rubric",
        }
        if missing:
            result["schema_fallback_missing_scores"] = list(missing)
        return result

    def _error_tags(self, values):
        if not isinstance(values, list):
            return []
        return [str(value) for value in values if str(value) in self.ERROR_TAGS][:4]

    def _turn_evidence(self, evidence):
        """Preserve dimension-grouped evidence while accepting legacy lists."""
        if isinstance(evidence, dict):
            result = {}
            for key in self.TURN_SCORE_KEYS:
                result[key] = self._string_list(evidence.get(key, []), 4)
            return result
        return {"general": self._string_list(evidence, 4)}

    def _dimension_assessments(self, assessments):
        assessments = assessments if isinstance(assessments, dict) else {}
        result = {}
        for key in self.TURN_SCORE_KEYS:
            item = assessments.get(key, {})
            item = item if isinstance(item, dict) else {}
            result[key] = {
                "supporting_evidence": self._string_list(item.get("supporting_evidence", []), 4),
                "limitations": self._string_list(item.get("limitations", []), 4),
            }
        return result

    def _string_list(self, values, limit):
        if values in [None, ""]:
            return []
        if not isinstance(values, list):
            values = [values]
        return [str(value) for value in values[:limit]]

    def _score_value(self, value):
        if value in [None, ""]:
            return None
        if isinstance(value, dict):
            for key in ["score", "value", "rating", "points"]:
                nested = self._score_value(value.get(key))
                if nested is not None:
                    return nested
            return None
        if isinstance(value, (int, float)):
            return float(value)
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        return float(match.group(0)) if match else None

    def _bounded(self, value, ceiling):
        return round(max(0.0, min(float(ceiling), float(value))), 1)

    def _average(self, values):
        return round(sum(values) / len(values), 2) if values else 0.0

    def _require_client(self):
        if not self.client.is_available():
            raise ValueError("LLM evaluator is not configured: %s" % self.client.missing_config_message())
