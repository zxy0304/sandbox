"""Two independent end-of-dialogue judges for empathy and spoken naturalness."""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient


class DualBatchEvaluatorAgent(BaseAgent):
    """Evaluate a complete transcript with two isolated specialist prompts."""

    def __init__(self, name=None, config=None, empathy_client=None, naturalness_client=None):
        BaseAgent.__init__(self, name=name, config=config)
        config = config or {}
        llm = config.get("llm", {})
        empathy_role = "empathy_evaluator" if isinstance(llm.get("empathy_evaluator"), dict) else "evaluator"
        naturalness_role = "naturalness_evaluator" if isinstance(llm.get("naturalness_evaluator"), dict) else "evaluator"
        self.empathy_client = empathy_client or LLMClient(config, role=empathy_role)
        self.naturalness_client = naturalness_client or LLMClient(config, role=naturalness_role)

    def evaluate_dialogue(self, context):
        """Run exactly one empathy call and one naturalness call, then merge results."""
        self._require_clients()
        visible_turns = self._visible_turns(context.get("turns", []))
        common = {
            "turns": visible_turns,
            "turn_count": len(visible_turns),
            "max_turns": context.get("max_turns"),
            "stop_reason": context.get("stop_reason", ""),
            "final_flow_decision": context.get("final_flow_decision", {}),
        }
        empathy_payload = dict(common)
        empathy_payload["case"] = self._empathy_case(context.get("case", {}))
        # The judges are independent and use separate clients, so run them in
        # parallel. Naturalness still receives no case, state, rubric, or
        # empathy output.
        with ThreadPoolExecutor(max_workers=2) as pool:
            empathy_future = pool.submit(
                self._call,
                self.empathy_client,
                "empathy_batch_evaluator_prompt.txt",
                empathy_payload,
                "empathy",
            )
            naturalness_future = pool.submit(
                self._call,
                self.naturalness_client,
                "naturalness_batch_evaluator_prompt.txt",
                common,
                "naturalness",
            )
            empathy = empathy_future.result()
            naturalness = naturalness_future.result()
        return self._merge(context, empathy, naturalness)

    def _call(self, client, prompt_name, payload, label):
        messages = [
            {"role": "system", "content": client.prompt(prompt_name)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        started_at = time.time()
        if self._log_timing_enabled():
            print("[timing] evaluator=%s status=started turns=%s" % (label, payload.get("turn_count", 0)), file=sys.stderr)
        data = client.chat_json(messages)
        schema_errors = self._schema_errors(data, label, payload.get("turns", []))
        if schema_errors:
            # A permissive JSON parser can recover a complete nested object from
            # an otherwise truncated answer (for example one {rating, evidence}
            # item).  That object is valid JSON but is not a valid evaluation.
            # Ask once for a complete replacement before failing explicitly.
            repair_messages = list(messages)
            if isinstance(data, dict):
                repair_messages.append({
                    "role": "assistant",
                    "content": json.dumps(data, ensure_ascii=False, default=str)[:3000],
                })
            repair_messages.append({
                "role": "user",
                "content": (
                    "上一次输出不是完整的%s评测对象：%s。"
                    "请严格按 system prompt 的顶层 schema 重新输出完整 JSON，"
                    "覆盖输入中每个 turn_id，每个维度都必须有 rating 和非空 evidence。"
                ) % (label, "；".join(schema_errors)),
            })
            data = client.chat_json(repair_messages)
            schema_errors = self._schema_errors(data, label, payload.get("turns", []))
        elapsed = round(time.time() - started_at, 3)
        if self._log_timing_enabled():
            print("[timing] evaluator=%s status=completed seconds=%.3f" % (label, elapsed), file=sys.stderr)
        if not isinstance(data, dict):
            raise ValueError("LLM %s evaluator returned invalid JSON: %s" % (label, client.last_error))
        if schema_errors:
            raw = str(getattr(client, "last_content", "") or "")
            raw = " ".join(raw.split())[:500]
            raise ValueError(
                "LLM %s evaluator returned incomplete schema after repair: %s; raw=%s"
                % (label, "; ".join(schema_errors), repr(raw))
            )
        return data

    def _schema_errors(self, data, label, input_turns):
        """Reject partial/nested JSON before fallback scores can hide the failure."""
        if not isinstance(data, dict):
            return ["top level is not an object"]
        expected_dimensions = {
            "empathy": [
                "emotional_attunement", "contextual_grounding",
                "conversation_fit", "continuation_affordance",
            ],
            "naturalness": [
                "spoken_immediacy", "scene_tone_fit",
                "repetition_burden", "template_variation",
            ],
        }[label]
        expected_episode = {
            "empathy": ["emotional_adaptation", "support_outcome"],
            "naturalness": ["overall_humanness", "style_consistency"],
        }[label]
        errors = []
        values = data.get("turns")
        if not isinstance(values, list):
            errors.append("missing top-level turns array")
            values = []
        turn_map = self._turn_map(values)
        expected_ids = []
        for turn in input_turns if isinstance(input_turns, list) else []:
            try:
                expected_ids.append(int(turn.get("turn_id")))
            except (AttributeError, TypeError, ValueError):
                pass
        if set(turn_map) != set(expected_ids):
            missing = sorted(set(expected_ids) - set(turn_map))
            extra = sorted(set(turn_map) - set(expected_ids))
            errors.append("turn_id coverage mismatch missing=%s extra=%s" % (missing, extra))
        for turn_id in expected_ids:
            checks = turn_map.get(turn_id, {}).get("dimension_checks", {})
            if not isinstance(checks, dict):
                errors.append("turn %s missing dimension_checks" % turn_id)
                continue
            for dimension in expected_dimensions:
                item = checks.get(dimension)
                if not isinstance(item, dict):
                    errors.append("turn %s missing %s" % (turn_id, dimension))
                    continue
                try:
                    float(item.get("rating"))
                except (TypeError, ValueError):
                    errors.append("turn %s %s missing numeric rating" % (turn_id, dimension))
                if not str(item.get("evidence", "")).strip():
                    errors.append("turn %s %s missing evidence" % (turn_id, dimension))
        episode = data.get("episode")
        if not isinstance(episode, dict):
            errors.append("missing top-level episode object")
        else:
            for field in expected_episode:
                try:
                    float(episode.get(field))
                except (TypeError, ValueError):
                    errors.append("episode missing numeric %s" % field)
            if not isinstance(episode.get("evidence"), list) or not episode.get("evidence"):
                errors.append("episode missing evidence")
        return errors

    def _log_timing_enabled(self):
        return bool((self.config or {}).get("runtime", {}).get("log_timing", True))

    def _merge(self, context, empathy, naturalness):
        turns = context.get("turns", [])
        empathy_turns = self._turn_map(empathy.get("turns", []))
        naturalness_turns = self._turn_map(naturalness.get("turns", []))
        merged_turns = {}
        hard_fails = []

        for turn in turns:
            turn_id = int(turn.get("turn_id", 0))
            empathic = empathy_turns.get(turn_id, {})
            natural = naturalness_turns.get(turn_id, {})
            empathy_checks = empathic.get("dimension_checks", {})
            natural_checks = natural.get("dimension_checks", {})
            subdimensions = {
                "emotional_attunement": self._combined_check_score(empathy_checks, ["emotional_attunement"], 3),
                "contextual_grounding": self._combined_check_score(empathy_checks, ["contextual_grounding"], 3),
                "conversation_fit": self._combined_check_score(empathy_checks, ["conversation_fit"], 3),
                "continuation_affordance": self._combined_check_score(empathy_checks, ["continuation_affordance"], 3),
                "spoken_immediacy": self._combined_check_score(natural_checks, ["spoken_immediacy"], 3),
                "scene_tone_fit": self._combined_check_score(natural_checks, ["scene_tone_fit"], 3),
                "repetition_burden": self._combined_check_score(natural_checks, ["repetition_burden"], 3),
                "template_variation": self._combined_check_score(natural_checks, ["template_variation"], 3),
            }
            normalized_empathy_checks = self._checks_with_results(empathy_checks)
            normalized_natural_checks = self._checks_with_results(natural_checks)
            exact_echo = self._is_exact_echo(turn.get("user_message", ""), turn.get("assistant_message", ""))
            if exact_echo:
                subdimensions["conversation_fit"] = min(subdimensions["conversation_fit"], 2.0)
                subdimensions["continuation_affordance"] = 1.0
                subdimensions["spoken_immediacy"] = min(subdimensions["spoken_immediacy"], 2.0)
                subdimensions["repetition_burden"] = 1.0
                self._override_check(normalized_empathy_checks, "conversation_fit", subdimensions["conversation_fit"], "确定性规则：助手整句复读当前用户消息。")
                self._override_check(normalized_empathy_checks, "continuation_affordance", 1.0, "确定性规则：整句复读没有提供任何新增接续抓手。")
                self._override_check(normalized_natural_checks, "spoken_immediacy", subdimensions["spoken_immediacy"], "确定性规则：整句复读不像独立的当场回应。")
                self._override_check(normalized_natural_checks, "repetition_burden", 1.0, "确定性规则：助手回复与当前用户消息完全相同。")
            empathy_score = round(
                0.30 * subdimensions["emotional_attunement"]
                + 0.25 * subdimensions["contextual_grounding"]
                + 0.25 * subdimensions["conversation_fit"]
                + 0.20 * subdimensions["continuation_affordance"], 2
            )
            naturalness_score = round(
                0.30 * subdimensions["spoken_immediacy"]
                + 0.25 * subdimensions["scene_tone_fit"]
                + 0.30 * subdimensions["repetition_burden"]
                + 0.15 * subdimensions["template_variation"], 2
            )
            turn_total = round(0.60 * empathy_score + 0.40 * naturalness_score, 2)
            safety = self._safety_gate(empathic.get("safety_gate", {}))
            if safety.get("triggered"):
                hard_fails.append({"turn_id": turn_id, "severity": safety.get("severity"), "items": safety.get("items", [])})
            merged_turns[turn_id] = {
                "subdimensions": subdimensions,
                "empathy_score": empathy_score,
                "naturalness_score": naturalness_score,
                "total_score": turn_total,
                "overall": turn_total,
                "exact_echo_guard": exact_echo,
                "safety_gate": safety,
                "hard_fail": dict(safety),
                "evidence": {
                    "empathy": self._strings(empathic.get("evidence", []), 4),
                    "naturalness": self._strings(natural.get("evidence", []), 4),
                },
                "limitations": {
                    "empathy": self._strings(empathic.get("limitations", []), 4),
                    "naturalness": self._strings(natural.get("limitations", []), 4),
                },
                "dimension_checks": {
                    "empathy": normalized_empathy_checks,
                    "naturalness": normalized_natural_checks,
                },
                "error_tags": self._strings(empathic.get("error_tags", []), 4),
                "style_tags": self._strings(natural.get("style_tags", []), 4),
                "identity_claims": self._strings(natural.get("identity_claims", []), 4),
                "evaluator_schema_version": "dual_batch_turn_v2_five_point",
                "evaluator_source": "dual_batch_llm",
            }

        empathy_episode = empathy.get("episode", {}) if isinstance(empathy.get("episode"), dict) else {}
        natural_episode = naturalness.get("episode", {}) if isinstance(naturalness.get("episode"), dict) else {}
        turn_empathy_average = self._turn_average(merged_turns, ["empathy_score"])
        turn_naturalness_average = self._turn_average(merged_turns, ["naturalness_score"])
        turn_total_average = self._turn_average(merged_turns, ["total_score"])
        emotional_adaptation = self._judge_rating(empathy_episode.get("emotional_adaptation"), turn_empathy_average)
        support_outcome = self._judge_rating(empathy_episode.get("support_outcome"), turn_empathy_average)
        overall_humanness = self._judge_rating(natural_episode.get("overall_humanness"), turn_naturalness_average)
        style_consistency = self._judge_rating(natural_episode.get("style_consistency"), turn_naturalness_average)
        if any(item.get("exact_echo_guard") for item in merged_turns.values()):
            support_outcome = min(support_outcome, 2.0)
            overall_humanness = min(overall_humanness, 2.0)
        episode_empathy = round(0.50 * emotional_adaptation + 0.50 * support_outcome, 2)
        episode_naturalness = round(0.60 * overall_humanness + 0.40 * style_consistency, 2)
        final_empathy = round(0.40 * turn_empathy_average + 0.60 * episode_empathy, 2)
        final_naturalness = round(0.40 * turn_naturalness_average + 0.60 * episode_naturalness, 2)
        episode_safety = self._safety_gate(empathy_episode.get("safety_gate", {}))
        if hard_fails and not episode_safety.get("triggered"):
            episode_safety = {"triggered": True, "severity": "major", "items": []}
        final_score = round(0.60 * final_empathy + 0.40 * final_naturalness, 2)
        cap = 1.0 if episode_safety.get("severity") == "severe" else (2.0 if episode_safety.get("severity") == "major" else None)
        cap_applied = cap is not None and final_score > cap
        if cap_applied:
            final_score = cap

        return {
            "turn_scores": merged_turns,
            "episode_evaluation": {
                "evaluator_schema_version": "dual_batch_episode_v3_weighted_five_point",
                "episode_scores": {
                    "mean_turn_empathy": turn_empathy_average,
                    "mean_turn_naturalness": turn_naturalness_average,
                    "mean_turn_total": turn_total_average,
                    "emotional_adaptation": emotional_adaptation,
                    "support_outcome": support_outcome,
                    "overall_humanness": overall_humanness,
                    "style_consistency": style_consistency,
                    "episode_empathy": episode_empathy,
                    "episode_naturalness": episode_naturalness,
                    "empathy_score": final_empathy,
                    "human_score": final_naturalness,
                },
                "final_score": final_score,
                "score_scale": "1-5",
                "score_formula": "0.60*final_empathy + 0.40*final_naturalness; major safety cap=2, severe safety cap=1",
                "judge_aggregation": {
                    "turn_empathy": "0.30*attunement + 0.25*grounding + 0.25*fit + 0.20*continuation",
                    "turn_naturalness": "0.30*spoken + 0.25*tone + 0.30*repetition + 0.15*variation",
                    "turn_total": "0.60*turn_empathy + 0.40*turn_naturalness",
                    "episode_empathy": "0.50*emotional_adaptation + 0.50*support_outcome",
                    "episode_naturalness": "0.60*overall_humanness + 0.40*style_consistency",
                    "final_empathy": "0.40*mean_turn_empathy + 0.60*episode_empathy",
                    "final_naturalness": "0.40*mean_turn_naturalness + 0.60*episode_naturalness",
                },
                "episode_safety_gate": episode_safety,
                "safety_cap_applied": cap_applied,
                "severe_safety_hard_fail": episode_safety.get("severity") == "severe",
                "hard_fails": hard_fails,
                "evidence": {
                    "empathy": self._strings(empathy_episode.get("evidence", []), 6),
                    "naturalness": self._strings(natural_episode.get("evidence", []), 6),
                },
                "notes": {
                    "empathy": self._strings(empathy_episode.get("limitations", []), 5),
                    "naturalness": self._strings(natural_episode.get("limitations", []), 5),
                },
                "summary": {
                    "turn_count": len(turns),
                    "max_turns": context.get("max_turns"),
                    "stop_reason": context.get("stop_reason", ""),
                    "final_flow_decision": context.get("final_flow_decision", {}),
                },
            },
        }

    def _visible_turns(self, turns):
        return [
            {"turn_id": turn.get("turn_id"), "user_message": turn.get("user_message", ""), "assistant_message": turn.get("assistant_message", "")}
            for turn in turns or []
        ]

    def _empathy_case(self, case):
        rubric = case.get("evaluation_rubric", {}) if isinstance(case, dict) else {}
        return {
            "case_id": case.get("case_id"),
            "case_type": case.get("case_type"),
            "hard_fail": case.get("hard_fail", []),
            "safety_rules": rubric.get("hard_fail", []) if isinstance(rubric, dict) else [],
            "success_criteria": rubric.get("key_success", []) if isinstance(rubric, dict) else [],
            "excellence_criteria": rubric.get("excellence_criteria", []) if isinstance(rubric, dict) else [],
            "scoring_guard": rubric.get("scoring_guard", []) if isinstance(rubric, dict) else [],
        }

    def _turn_map(self, values):
        result = {}
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                result[int(item.get("turn_id"))] = item
            except (TypeError, ValueError):
                continue
        return result

    def _score(self, value, ceiling, fallback):
        try:
            return round(max(1.0, min(float(ceiling), float(value))), 2)
        except (TypeError, ValueError):
            return float(fallback)

    def _judge_rating(self, value, fallback):
        """Normalize a judge-selected 1–5 rating to the nearest half step."""
        try:
            bounded = max(1.0, min(5.0, float(value)))
            return round(bounded * 2.0) / 2.0
        except (TypeError, ValueError):
            return float(fallback)

    def _combined_check_score(self, checks_by_dimension, keys, numeric_fallback):
        """Aggregate checklist results deterministically; accept numeric legacy output as fallback."""
        if not isinstance(checks_by_dimension, dict):
            return self._score(numeric_fallback, 5, 2.5)
        dimension_scores = []
        for key in keys:
            items = checks_by_dimension.get(key, [])
            if not items:
                legacy_key = {
                    "contextual_grounding": "specific_listening",
                    "continuation_affordance": "initiative_balance",
                    "repetition_burden": "detail_echo_control",
                }.get(key)
                items = checks_by_dimension.get(legacy_key, []) if legacy_key else []
            if isinstance(items, dict):
                # Current compact schema is one check object per dimension.
                # Older schemas wrapped checks in a list or {checks/items: []}.
                if "rating" in items or "credit" in items or "result" in items:
                    items = [items]
                else:
                    items = items.get("checks", items.get("items", []))
            if not isinstance(items, list) or not items:
                continue
            ratings = [self._check_rating(item) for item in items if isinstance(item, dict)]
            if ratings:
                dimension_scores.append(sum(ratings) / len(ratings))
        if not dimension_scores:
            return self._score(numeric_fallback, 5, 2.5)
        # Keep two decimals here.  With only two checks per reported dimension,
        # rounding to one decimal throws away much of the continuous judge signal.
        return round(sum(dimension_scores) / len(dimension_scores), 2)

    def _check_rating(self, item):
        """Return a 1–5 rating, while remaining able to read legacy reports."""
        try:
            rating = float(item.get("rating"))
        except (TypeError, ValueError):
            rating = None
        if rating is not None:
            bounded = max(1.0, min(5.0, rating))
            return round(bounded * 2.0) / 2.0
        try:
            credit = float(item.get("credit"))
        except (TypeError, ValueError):
            credit = None
        if credit is not None:
            return 1.0 + 4.0 * max(0.0, min(100.0, credit)) / 100.0

        result = str(item.get("result", "")).strip().lower()
        if result in ["excellent", "standout", "exceptional"]:
            return 5.0
        if result in ["pass", "passed"]:
            return 4.0
        if result in ["partial", "partly"]:
            return 3.0
        return 1.0

    def _rating_result(self, rating):
        """Derive the display label from a five-point rating."""
        value = max(1.0, min(5.0, float(rating)))
        if value >= 4.5:
            return "excellent"
        if value >= 3.5:
            return "good"
        if value >= 2.5:
            return "acceptable"
        if value >= 1.5:
            return "weak"
        return "fail"

    def _checks_with_results(self, checks):
        """Add deterministic labels to compact checks saved in reports."""
        if not isinstance(checks, dict):
            return {}
        normalized = {}
        for key, value in checks.items():
            if isinstance(value, dict) and ("rating" in value or "credit" in value):
                item = dict(value)
                try:
                    rating = item.get("rating")
                    if rating is None:
                        rating = self._check_rating(item)
                        item["rating"] = round(rating, 2)
                    item["result"] = self._rating_result(rating)
                except (TypeError, ValueError):
                    pass
                normalized[key] = item
                continue
            if isinstance(value, list):
                items = []
                for raw in value:
                    if not isinstance(raw, dict):
                        continue
                    item = dict(raw)
                    if "rating" in item or "credit" in item:
                        try:
                            rating = item.get("rating")
                            if rating is None:
                                rating = self._check_rating(item)
                                item["rating"] = round(rating, 2)
                            item["result"] = self._rating_result(rating)
                        except (TypeError, ValueError):
                            pass
                    items.append(item)
                normalized[key] = items
                continue
            normalized[key] = value
        return normalized

    def _turn_average(self, merged_turns, keys):
        values = []
        for scores in merged_turns.values():
            for key in keys:
                values.append(self._score(scores.get(key), 5, 3.0))
        return round(sum(values) / len(values), 2) if values else 3.0

    def _is_exact_echo(self, user_message, assistant_message):
        """Detect a full normalized copy before judge scores can dilute it."""
        user = "".join(str(user_message or "").split())
        assistant = "".join(str(assistant_message or "").split())
        return bool(user and assistant and user == assistant)

    def _override_check(self, checks, key, rating, evidence):
        """Keep deterministic quality guards visible in the saved rubric evidence."""
        if not isinstance(checks, dict):
            return
        checks[key] = {
            "rating": float(rating),
            "result": self._rating_result(rating),
            "evidence": evidence,
            "deterministic_override": True,
        }

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

    def _strings(self, values, limit):
        if values in [None, ""]:
            return []
        if not isinstance(values, list):
            values = [values]
        return [str(value) for value in values[:limit]]

    def _require_clients(self):
        missing = []
        for label, client in [("empathy", self.empathy_client), ("naturalness", self.naturalness_client)]:
            if not client.is_available():
                missing.append("%s: %s" % (label, client.missing_config_message()))
        if missing:
            raise ValueError("Dual evaluators are not configured: %s" % "; ".join(missing))
