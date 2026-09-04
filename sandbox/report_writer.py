"""Report writer for the daily companion sandbox V2."""

from datetime import datetime
from pathlib import Path

from sandbox.schemas import INTERNAL_STATE_KEYS
from sandbox.utils.json_utils import save_json


class ReportWriter:
    """Persist JSON reports and render a compact Markdown review."""

    STATE_ROWS = INTERNAL_STATE_KEYS

    TURN_SCORE_KEYS = [
        "empathy_score",
        "naturalness_score",
        "total_score",
    ]

    EPISODE_SCORE_KEYS = [
        "empathy_score", "human_score", "mean_turn_empathy", "mean_turn_naturalness",
        "mean_turn_total", "emotional_adaptation", "support_outcome", "overall_humanness",
        "style_consistency", "episode_empathy", "episode_naturalness",
    ]

    DIMENSION_GROUPS = [
        ("empathy", [
            ("emotional_attunement", "attune"),
            ("contextual_grounding", "ground"),
            ("conversation_fit", "fit"),
            ("continuation_affordance", "continue"),
        ]),
        ("naturalness", [
            ("spoken_immediacy", "spoken"),
            ("scene_tone_fit", "tone"),
            ("repetition_burden", "repeat"),
            ("template_variation", "variation"),
        ]),
    ]

    def __init__(self, config=None):
        self.config = config or {}

    def write(self, report):
        outputs_dir = self._outputs_dir()
        case_id = report.get("case_id", "unknown")
        json_path = outputs_dir / ("report_%s.json" % case_id)
        md_path = outputs_dir / ("report_%s.md" % case_id)

        save_json(json_path, report)
        self._write_markdown(md_path, report)

        return {
            "json_path": str(json_path),
            "markdown_path": str(md_path),
        }

    def write_failure(self, failure_report):
        """Persist machine-readable diagnostics and a short plain-text error log."""
        outputs_dir = self._outputs_dir()
        case_id = failure_report.get("case_id", "unknown")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        error_dir = outputs_dir / "errors" / str(case_id)
        json_path = error_dir / ("error_%s.json" % timestamp)
        log_path = error_dir / ("error_%s.log" % timestamp)
        save_json(json_path, failure_report)

        failure = failure_report.get("failure", {})
        log_lines = [
            "status=failed",
            "case_id=%s" % self._text(case_id),
            "role=%s" % self._text(failure.get("role")),
            "turn_id=%s" % self._text(failure.get("turn_id")),
            "error_type=%s" % self._text(failure.get("error_type")),
            "error=%s" % self._text(failure_report.get("error")),
            "model_name=%s" % self._text(failure.get("model_name")),
            "client_error=%s" % self._text(failure.get("client_error")),
            "completed_turn_count=%s" % self._text(failure_report.get("completed_turn_count")),
            "last_raw_output=%s" % self._text(failure.get("last_raw_output")),
            "schema_failure=%s" % self._text(failure.get("schema_failure")),
        ]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(log_lines))
            handle.write("\n")
        return {"error_json_path": str(json_path), "error_log_path": str(log_path)}

    def _outputs_dir(self):
        reports_config = self.config.get("reports", {})
        configured = reports_config.get("outputs_dir", "outputs")
        path = Path(configured)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_markdown(self, path, report):
        lines = []
        lines.append("# Conversation Evaluation Report")
        lines.append("")

        self._append_basic_info(lines, report)
        self._append_review_dashboard(lines, report)
        self._append_clean_dialogue(lines, report)
        self._append_turn_score_matrix(lines, report)
        self._append_turn_evaluator_review(lines, report)
        self._append_evaluation_summary(lines, report)
        self._append_safety_analysis(lines, report)
        self._append_debug_appendix(lines, report)

        path.parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open("w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
            handle.write("\n")

    def _append_review_dashboard(self, lines, report):
        """Put the human-review essentials before transcript and implementation details."""
        evaluation = report.get("episode_evaluation", {})
        scores = evaluation.get("episode_scores", {})
        summary = report.get("episode_summary", {})
        turns = report.get("turns", [])

        lines.append("## Review Dashboard")
        lines.append("")
        lines.append("| final | empathy | naturalness | turns | stop |")
        lines.append("| ---: | ---: | ---: | ---: | --- |")
        lines.append("| %s | %s | %s | %s | %s |" % (
            self._score_badge(evaluation.get("final_score")),
            self._score_badge(scores.get("empathy_score")),
            self._score_badge(scores.get("human_score")),
            self._text(len(turns)),
            self._cell(summary.get("stop_reason")),
        ))
        lines.append("")

        warnings = self._review_warnings(report)
        if warnings:
            lines.append("**Review flags**")
            lines.append("")
            for warning in warnings:
                lines.append("- %s" % warning)
            lines.append("")

        averages = self._dimension_averages(report)
        if averages:
            lines.append("**Eight-dimension averages**")
            lines.append("")
            lines.append("| evaluator | dimension | average |")
            lines.append("| --- | --- | ---: |")
            for judge, dimensions in self.DIMENSION_GROUPS:
                for key, _label in dimensions:
                    if key in averages:
                        lines.append("| %s | %s | %s |" % (
                            self._cell(judge),
                            self._cell(key),
                            self._score_badge(averages[key]),
                        ))
            lines.append("")

    def _append_clean_dialogue(self, lines, report):
        """Render only user-visible messages so reviewers can judge the conversation first."""
        lines.append("## Raw Dialogue")
        lines.append("")
        lines.append("> 建议先只读本节并独立判断对话质量，再查看后面的 evaluator 分数。")
        lines.append("")
        for turn in report.get("turns", []):
            lines.append("### Turn %s" % self._text(turn.get("turn_id")))
            lines.append("")
            lines.append("**User**")
            lines.append("")
            self._append_quote(lines, turn.get("user_message", ""))
            lines.append("")
            lines.append("**Assistant**")
            lines.append("")
            self._append_quote(lines, turn.get("assistant_message", ""))
            lines.append("")

    def _append_turn_score_matrix(self, lines, report):
        """Show all eight five-point ratings side by side."""
        turns = report.get("turns", [])
        if not any(turn.get("judge_scores") for turn in turns):
            return

        columns = [item for _judge, dimensions in self.DIMENSION_GROUPS for item in dimensions]
        lines.append("## Turn Score Matrix")
        lines.append("")
        lines.append("Legend: 🟢 4.5–5 · 🟡 3.5–4.49 · 🟠 2.5–3.49 · 🔴 1–2.49 · — not scored")
        lines.append("")
        lines.append("| turn | overall/5 | " + " | ".join(label for _key, label in columns) + " |")
        lines.append("| ---: | ---: | " + " | ".join("---:" for _ in columns) + " |")
        for turn in turns:
            judge_scores = turn.get("judge_scores", {})
            checks = judge_scores.get("dimension_checks", {})
            values = []
            for judge, dimensions in self.DIMENSION_GROUPS:
                judge_checks = checks.get(judge, {}) if isinstance(checks, dict) else {}
                for key, _label in dimensions:
                    item = self._dimension_check_item(judge_checks.get(key, {})) if isinstance(judge_checks, dict) else {}
                    values.append(self._score_badge(item.get("rating")))
            lines.append("| %s | %s | %s |" % (
                self._text(turn.get("turn_id")),
                self._format_number(judge_scores.get("overall")),
                " | ".join(values),
            ))
        lines.append("")
        lines.append("Abbreviations: attune=emotional_attunement, ground=contextual_grounding, fit=conversation_fit, continue=continuation_affordance, spoken=spoken_immediacy, tone=scene_tone_fit, repeat=repetition_burden, variation=template_variation.")
        lines.append("")

    def _append_turn_evaluator_review(self, lines, report):
        """Keep evaluator evidence separate from the raw transcript."""
        lines.append("## Turn-by-turn Evaluator Review")
        lines.append("")
        for turn in report.get("turns", []):
            scores = turn.get("judge_scores", {})
            lines.append("### Turn %s" % self._text(turn.get("turn_id")))
            lines.append("")
            lines.append("**Assistant excerpt:** %s" % self._short(turn.get("assistant_message"), 180))
            lines.append("")
            lines.append(self._turn_score_line(scores) if scores else "- evaluator skipped")
            if scores:
                self._append_dimension_checks(lines, scores.get("dimension_checks", {}))
            if scores.get("error_tags"):
                lines.append("- error_tags: %s" % self._brief_list(scores.get("error_tags")))
            if scores.get("style_tags"):
                lines.append("- style_tags: %s" % self._brief_list(scores.get("style_tags")))
            if scores.get("identity_claims"):
                lines.append("- identity_claims (diagnostic only, not scored): %s" % self._brief_list(scores.get("identity_claims")))
            lines.append("")

    def _append_debug_appendix(self, lines, report):
        """Move hidden simulator state and trajectory out of the main human-review flow."""
        lines.append("## Debug Appendix")
        lines.append("")
        lines.append("本节包含 case 隐藏设定、UserThinker 私有状态和状态轨迹，不属于用户实际看到的对话。")
        lines.append("")
        self._append_case_summary(lines, report)
        self._append_state_table(lines, report)
        self._append_private_turn_debug(lines, report)
        self._append_trajectory(lines, report)

    def _append_private_turn_debug(self, lines, report):
        lines.append("### Private User State by Turn")
        lines.append("")
        for turn in report.get("turns", []):
            private_state = turn.get("user_private_state", {})
            decision = turn.get("flow_decision", {})
            lines.append("**Turn %s** — flow=`%s`; reason=%s" % (
                self._text(turn.get("turn_id")),
                self._text(decision.get("action")),
                self._short(decision.get("reason"), 120),
            ))
            lines.append("")
            lines.append("- inner_reaction: %s" % self._short(private_state.get("inner_reaction"), 220))
            lines.append("- reaction: %s" % self._short(private_state.get("reaction"), 260))
            lines.append("- intent: %s" % self._short(private_state.get("intent"), 300))
            lines.append("- state_delta: %s" % self._format_delta(turn.get("state_delta", {}), compact=True))
            lines.append("")

    def _dimension_averages(self, report):
        buckets = {}
        for turn in report.get("turns", []):
            checks = turn.get("judge_scores", {}).get("dimension_checks", {})
            if not isinstance(checks, dict):
                continue
            for judge, dimensions in self.DIMENSION_GROUPS:
                judge_checks = checks.get(judge, {})
                if not isinstance(judge_checks, dict):
                    continue
                for key, _label in dimensions:
                    item = self._dimension_check_item(judge_checks.get(key, {}))
                    value = item.get("rating") if item else None
                    if value is not None and value != "":
                        buckets.setdefault(key, []).append(self._number(value))
        return {key: sum(values) / len(values) for key, values in buckets.items() if values}

    def _review_warnings(self, report):
        warnings = []
        summary = report.get("episode_summary", {})
        if summary.get("stop_reason") == "runner_max_turns_guard":
            warnings.append("⚠️ Episode reached `runner_max_turns_guard`; check whether the conversation failed to form a natural landing.")

        averages = self._dimension_averages(report)
        weak = [(key, value) for key, value in averages.items() if value < 3.0]
        if weak:
            weak.sort(key=lambda pair: pair[1])
            warnings.append("Weak average dimensions: %s." % ", ".join(
                "%s=%0.1f" % (key, value) for key, value in weak
            ))

        low_turns = []
        for turn in report.get("turns", []):
            overall = turn.get("judge_scores", {}).get("overall")
            if overall is not None and self._number(overall) < 3.5:
                low_turns.append(str(turn.get("turn_id")))
        if low_turns:
            warnings.append("Low overall turns (<3.5/5): %s." % ", ".join(low_turns))
        return warnings

    def _score_badge(self, value):
        if value is None or value == "":
            return "—"
        number = self._number(value)
        if number >= 4.5:
            symbol = "🟢"
        elif number >= 3.5:
            symbol = "🟡"
        elif number >= 2.5:
            symbol = "🟠"
        else:
            symbol = "🔴"
        return "%s %0.1f" % (symbol, number)

    def _append_basic_info(self, lines, report):
        case = report.get("case", {})
        episode_config = report.get("episode_config", {})
        episode_evaluation = report.get("episode_evaluation", {})

        lines.append("## Basic Info")
        lines.append("")
        lines.append("- case_id: `%s`" % self._text(report.get("case_id")))
        lines.append("- title: %s" % self._text(report.get("case_title") or case.get("title", "")))
        lines.append("- case_type: `%s`" % self._text(case.get("case_type")))
        lines.append("- max_turns: `%s`" % self._text(episode_config.get("max_turns")))
        lines.append("- actual_turns: `%s`" % len(report.get("turns", [])))
        lines.append("- evaluation: `%s`" % self._text(episode_evaluation.get("status", "completed")))
        lines.append("- final_score: `%s`" % self._text(episode_evaluation.get("final_score")))
        lines.append("")

    def _append_case_summary(self, lines, report):
        case = report.get("case", {})
        d = case.get("D", {})
        p = case.get("P", {})
        c = case.get("C", {})
        s = case.get("S", {})

        lines.append("### Case Summary")
        lines.append("")
        lines.append("- D: %s" % self._join_parts([
            "age_group=%s" % self._short(d.get("age_group")),
            "gender=%s" % self._short(d.get("gender")),
            "occupation=%s" % self._short(d.get("occupation")),
            "roles=%s" % self._brief_list(d.get("social_roles")),
        ]))
        lines.append("- P: %s" % self._join_parts([
            "personality=%s" % self._brief_list(p.get("personality")),
            "communication=%s" % self._brief_list(p.get("communication_preference")),
            "companion=%s" % self._brief_list(p.get("companion_preference")),
            "playful_style=%s" % self._short(p.get("playful_style")),
        ]))
        lines.append("- C: %s" % self._join_parts([
            "current_context=%s" % self._short(c.get("current_context"), 140),
            "preferences=%s" % self._short(c.get("recurring_preferences"), 120),
            "hidden_need=%s" % self._short(c.get("hidden_need"), 120),
            "sensitivity=%s" % self._short(c.get("possible_sensitivity"), 120),
        ]))
        lines.append("- S: %s" % self._join_parts([
            "intent=%s" % self._short(s.get("user_intent")),
            "topic=%s" % self._short(s.get("surface_topic")),
            "tone=%s" % self._short(s.get("emotional_tone")),
            "activity=%s" % self._short(s.get("activity_or_task")),
            "opening=%s" % self._short(s.get("opening_utterance"), 120),
        ]))
        lines.append("")

    def _append_state_table(self, lines, report):
        initial = report.get("initial_state", {})
        final = report.get("final_state", {})

        lines.append("### Initial State and Final State")
        lines.append("")
        lines.append("| dimension | initial | final | delta |")
        lines.append("| --- | ---: | ---: | ---: |")
        for key in self.STATE_ROWS:
            start = self._number(initial.get(key))
            end = self._number(final.get(key))
            lines.append("| %s | %s | %s | %s |" % (
                self._cell(key),
                self._format_number(start),
                self._format_number(end),
                self._format_signed(end - start),
            ))
        lines.append("")

    def _append_dialogue_transcript(self, lines, report):
        lines.append("## Dialogue Transcript")
        lines.append("")
        for turn in report.get("turns", []):
            self._append_turn(lines, turn)
        lines.append("")

    def _append_turn(self, lines, turn):
        private_state = turn.get("user_private_state", {})
        judge_scores = turn.get("judge_scores", {})
        decision = turn.get("flow_decision", {})

        lines.append("### Turn %s" % self._text(turn.get("turn_id")))
        lines.append("")
        lines.append("Flow: `%s`, mode: `%s`, reason: %s" % (
            self._text(decision.get("action")),
            self._text(decision.get("conversation_mode")),
            self._short(decision.get("reason"), 120),
        ))
        lines.append("")
        lines.append("**User:**")
        lines.append("")
        self._append_quote(lines, turn.get("user_message", ""))
        lines.append("")
        lines.append("**Assistant:**")
        lines.append("")
        self._append_quote(lines, turn.get("assistant_message", ""))
        lines.append("")
        audio = turn.get("audio", {})
        if audio:
            lines.append("**Audio:**")
            lines.append("")
            lines.append("- file: `%s`" % self._text(audio.get("path")))
            audio_eval = audio.get("evaluation", {})
            lines.append("- status: `%s`; naturalness=%s, colloquialness=%s, overall=%s" % (
                self._text(audio_eval.get("status")),
                self._format_number(audio_eval.get("audio_naturalness")),
                self._format_number(audio_eval.get("audio_colloquialness")),
                self._format_number(audio_eval.get("overall")),
            ))
            lines.append("")
        lines.append("**Private user state summary:**")
        lines.append("")
        lines.append("- inner_reaction: %s" % self._short(private_state.get("inner_reaction"), 180))
        lines.append("- reaction: %s" % self._short(private_state.get("reaction"), 220))
        lines.append("- intent: %s" % self._short(private_state.get("intent"), 260))
        lines.append("")
        lines.append("**Scores:**")
        lines.append("")
        lines.append(self._turn_score_line(judge_scores) if judge_scores else "- evaluator skipped")
        if judge_scores:
            self._append_dimension_checks(lines, judge_scores.get("dimension_checks", {}))
        if judge_scores.get("error_tags"):
            lines.append("- error_tags: %s" % self._brief_list(judge_scores.get("error_tags")))
        if judge_scores.get("style_tags"):
            lines.append("- style_tags: %s" % self._brief_list(judge_scores.get("style_tags")))
        if judge_scores.get("identity_claims"):
            lines.append("- identity_claims (diagnostic only, not scored): %s" % self._brief_list(judge_scores.get("identity_claims")))
        if judge_scores.get("evidence"):
            evidence = judge_scores.get("evidence")
            if isinstance(evidence, dict):
                for key, values in evidence.items():
                    if values:
                        lines.append("- evidence.%s: %s" % (key, self._brief_list(values, limit=140)))
            else:
                lines.append("- evidence: %s" % self._brief_list(evidence, limit=140))
        if judge_scores.get("limitations"):
            limitations = judge_scores.get("limitations")
            if isinstance(limitations, dict):
                for key, values in limitations.items():
                    if values:
                        lines.append("- limitations.%s: %s" % (key, self._brief_list(values, limit=140)))
        lines.append("")
        lines.append("**State delta:**")
        lines.append("")
        lines.append("- %s" % self._format_delta(turn.get("state_delta", {})))
        reason = turn.get("state_update_reason", {})
        if reason:
            lines.append("- update_reason: %s" % self._short(reason.get("delta_summary"), 220))
        lines.append("")

    def _append_trajectory(self, lines, report):
        lines.append("### Trajectory")
        lines.append("")
        header = "| turn | flow_action | " + " | ".join(self.STATE_ROWS) + " | state_delta |"
        align = "| ---: | --- | " + " | ".join(["---:" for _ in self.STATE_ROWS]) + " | --- |"
        lines.append(header)
        lines.append(align)
        for turn in report.get("turns", []):
            state = turn.get("state_after", {})
            decision = turn.get("flow_decision", {})
            values = [self._format_number(state.get(key)) for key in self.STATE_ROWS]
            lines.append("| %s | %s | %s | %s |" % (
                self._text(turn.get("turn_id")),
                self._cell(decision.get("action")),
                " | ".join(values),
                self._cell(self._format_delta(turn.get("state_delta", {}), compact=True)),
            ))
        lines.append("")

    def _append_evaluation_summary(self, lines, report):
        episode_evaluation = report.get("episode_evaluation", {})
        episode_scores = episode_evaluation.get("episode_scores", {})
        summary = report.get("episode_summary", {})

        lines.append("## Evaluation Summary")
        lines.append("")
        if episode_evaluation.get("status") == "skipped":
            lines.append("- evaluator skipped; this report contains dialogue and user-state trajectory only.")
            lines.append("- stop_reason: %s" % self._short(summary.get("stop_reason"), 120))
            lines.append("")
            return
        lines.append("- final_score: `%s`" % self._text(episode_evaluation.get("final_score")))
        lines.append("- score_formula: `%s`" % self._text(episode_evaluation.get("score_formula")))
        lines.append("- safety_cap_applied: `%s`" % self._text(episode_evaluation.get("safety_cap_applied")))
        lines.append("- severe_safety_hard_fail: `%s`" % self._text(episode_evaluation.get("severe_safety_hard_fail")))
        lines.append("- stop_reason: %s" % self._short(summary.get("stop_reason"), 120))
        lines.append("- valence_gain: `%s`" % self._text(summary.get("valence_gain")))
        lines.append("- comfort_gain: `%s`" % self._text(summary.get("comfort_gain")))
        lines.append("- connection_gain: `%s`" % self._text(summary.get("connection_gain")))
        lines.append("- agency_gain: `%s`" % self._text(summary.get("agency_gain")))
        lines.append("- task_progress_gain: `%s`" % self._text(summary.get("task_progress_gain")))
        lines.append("- dependency_risk_change: `%s`" % self._text(summary.get("dependency_risk_change")))
        lines.append("")
        lines.append("| episode_metric | score |")
        lines.append("| --- | ---: |")
        for key in self.EPISODE_SCORE_KEYS:
            lines.append("| %s | %s |" % (
                self._cell(key),
                self._format_number(episode_scores.get(key)),
            ))
        lines.append("")
        for section_name in ["evidence", "notes"]:
            section = episode_evaluation.get(section_name, {})
            if isinstance(section, dict):
                for judge_name, values in section.items():
                    if values:
                        lines.append("- %s.%s: %s" % (
                            section_name,
                            judge_name,
                            self._brief_list(values, limit=220),
                        ))
        lines.append("")

    def _append_safety_analysis(self, lines, report):
        hard_fail_items = self._hard_fail_items(report)

        lines.append("## Safety Analysis")
        lines.append("")
        lines.append("| safety_risk | status | evidence |")
        lines.append("| --- | --- | --- |")
        self._append_safety_row(lines, "diagnosis", ["diagnosis"], hard_fail_items)
        self._append_safety_row(lines, "manipulation", ["manipulation"], hard_fail_items)
        self._append_safety_row(lines, "dependency_induction", ["dependency_induction"], hard_fail_items)
        self._append_safety_row(lines, "privacy_or_memory_overclaim", ["privacy", "memory_overclaim"], hard_fail_items)
        self._append_safety_row(lines, "unsafe_crisis_response", ["unsafe_crisis"], hard_fail_items)
        lines.append("")

    def _append_safety_row(self, lines, label, categories, hard_fail_items):
        items = []
        for item in hard_fail_items:
            if item.get("category") in categories:
                items.append(item)

        if items:
            status = "triggered"
            evidence = self._brief_hard_fail_evidence(items)
        else:
            status = "not observed"
            evidence = "no triggered evidence"

        lines.append("| %s | %s | %s |" % (
            self._cell(label),
            self._cell(status),
            self._cell(evidence),
        ))

    def _append_quote(self, lines, text):
        text = self._text(text)
        if not text:
            lines.append("> ")
            return
        for line in text.splitlines():
            lines.append("> %s" % line)

    def _turn_score_line(self, scores):
        pieces = []
        for key in self.TURN_SCORE_KEYS:
            value = scores.get(key)
            pieces.append("%s=%s" % (key, self._format_number(value)))
        return "- " + ", ".join(pieces)

    def _append_dimension_checks(self, lines, dimension_checks):
        """Render the eight underlying evaluator ratings and their evidence."""
        if not isinstance(dimension_checks, dict):
            return
        rows = []
        for judge in ["empathy", "naturalness"]:
            checks = dimension_checks.get(judge, {})
            if not isinstance(checks, dict):
                continue
            for dimension, raw in checks.items():
                item = self._dimension_check_item(raw)
                if not item:
                    continue
                rows.append((judge, dimension, item))
        if not rows:
            return
        lines.append("")
        lines.append("**Evaluator dimension ratings:**")
        lines.append("")
        lines.append("| evaluator | dimension | rating/5 | result | evidence |")
        lines.append("| --- | --- | ---: | --- | --- |")
        for judge, dimension, item in rows:
            lines.append("| %s | %s | %s | %s | %s |" % (
                self._cell(judge),
                self._cell(dimension),
                self._format_number(item.get("rating")),
                self._cell(item.get("result")),
                self._cell(self._short(item.get("evidence"), 180)),
            ))

    def _dimension_check_item(self, raw):
        """Read current compact checks and legacy one-item check lists."""
        if isinstance(raw, dict):
            if "rating" in raw or "credit" in raw or "evidence" in raw or "result" in raw:
                item = dict(raw)
                if "rating" not in item and "credit" in item:
                    try:
                        item["rating"] = round(1.0 + 4.0 * max(0.0, min(100.0, float(item["credit"]))) / 100.0, 2)
                    except (TypeError, ValueError):
                        pass
                return item
            values = raw.get("checks", raw.get("items", []))
            return self._dimension_check_item(values)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    return item
        return {}

    def _hard_fail_items(self, report):
        items = []
        episode_evaluation = report.get("episode_evaluation", {})
        for entry in episode_evaluation.get("hard_fails", []):
            turn_id = entry.get("turn_id")
            for item in entry.get("items", []):
                copied = dict(item)
                copied["turn_id"] = turn_id
                items.append(copied)
        return items

    def _brief_hard_fail_evidence(self, items):
        evidence = []
        for item in items[:3]:
            evidence.append("turn %s: %s / %s" % (
                self._text(item.get("turn_id")),
                self._text(item.get("category")),
                self._short(item.get("pattern"), 80),
            ))
        if len(items) > 3:
            evidence.append("and %s more" % (len(items) - 3))
        return "; ".join(evidence)

    def _low_score_turns(self, report, key, threshold):
        turns = []
        for turn in report.get("turns", []):
            value = self._number(turn.get("judge_scores", {}).get(key))
            if value < threshold:
                turns.append(str(turn.get("turn_id")))
        return turns

    def _format_delta(self, delta, compact=False):
        if not delta:
            return "no_change"
        pieces = []
        for key in self.STATE_ROWS:
            value = self._number(delta.get(key))
            if value != 0 or not compact:
                pieces.append("%s %s" % (key, self._format_signed(value)))
        if not pieces:
            return "no_change"
        return ", ".join(pieces)

    def _format_signed(self, value):
        value = self._number(value)
        if value >= 0:
            return "+%s" % self._format_number(value)
        return self._format_number(value)

    def _format_number(self, value):
        if value is None or value == "":
            return ""
        return "%0.2f" % self._number(value)

    def _number(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _join_parts(self, parts):
        clean = []
        for part in parts:
            if part and not str(part).endswith("="):
                clean.append(str(part))
        return "; ".join(clean)

    def _brief_list(self, values, max_items=3, limit=80):
        if not values:
            return ""
        if not isinstance(values, list):
            return self._short(values, limit)
        pieces = []
        for value in values[:max_items]:
            pieces.append(self._short(value, limit))
        if len(values) > max_items:
            pieces.append("and %s more" % (len(values) - max_items))
        return " / ".join(pieces)

    def _short(self, value, limit=100):
        text = self._text(value).replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        if limit <= 3:
            return text[:limit]
        return text[:limit - 3] + "..."

    def _cell(self, value):
        text = self._text(value)
        text = text.replace("\n", "<br>")
        text = text.replace("|", "\\|")
        return text

    def _text(self, value):
        if value is None:
            return ""
        return str(value)
