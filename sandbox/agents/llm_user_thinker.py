"""Hidden user-state generator for the daily companion sandbox V2."""

import json
import sys

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient
from sandbox.schemas import INTERNAL_STATE_KEYS


class LLMUserThinker(BaseAgent):
    """Generate the user's private state before the visible utterance."""

    REQUIRED_KEYS = [
        "reaction",
        "participation_decision",
        "state_delta_hint",
        "reply_plan",
    ]
    CONTAINER_KEYS = [
        "user_private_state",
        "private_state",
        "hidden_state",
        "inner_state",
        "user_state",
        "result",
    ]
    KEY_ALIASES = {
        "current_activity": ["activity", "conversation_activity", "当前活动"],
        "thread": ["conversation_thread", "active_thread", "话题线索"],
        "inner_reaction": ["reaction", "reaction_to_assistant", "inner_thought", "内在反应"],
        "content_progress": ["novelty_check", "content_plan", "内容推进"],
        "next_move": ["interaction_move", "local_action", "下一步动作"],
        "state_delta_hint": ["state_delta", "delta_hint", "state_changes", "状态变化"],
        "reaction": ["user_reaction", "response_reaction", "即时反应"],
        "participation_decision": ["reply_decision", "engagement_decision", "参与决策"],
        "reply_plan": ["response_plan", "utterance_plan", "回复规划"],
    }

    def __init__(self, name=None, config=None, client=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.client = client or LLMClient(config or {}, role="user_thinker")

    def generate(self, context):
        """Generate and validate private user state."""
        if not self.client.is_available():
            raise ValueError("LLM user thinker is not configured: %s" % self.client.missing_config_message())

        messages = self.build_messages(context)
        data = self.client.chat_json(messages)
        if not isinstance(data, dict):
            raise ValueError("LLM user thinker returned invalid JSON: %s" % self.client.last_error)

        data = self._normalize_data(data)
        missing = self._missing_keys(data)
        if missing:
            print(
                "[llm-schema-repair] role=user_thinker missing=%s"
                % ",".join(missing),
                file=sys.stderr,
            )
            repaired = self._repair_schema(messages, data, missing)
            if isinstance(repaired, dict):
                data = self._normalize_data(repaired)
                missing = self._missing_keys(data)

        if missing:
            raise ValueError(
                "LLM user thinker response is missing %s. Received keys: %s"
                % (", ".join(missing), ", ".join(sorted(data.keys())))
            )

        output = {key: data.get(key) for key in self.REQUIRED_KEYS}
        reply_plan = data.get("reply_plan") if isinstance(data.get("reply_plan"), dict) else {}
        for key in ["current_activity", "thread", "inner_reaction", "content_progress", "next_move"]:
            output[key] = reply_plan.get(key)

        output["state_delta_hint"] = self._normalize_state_delta_hint(output.get("state_delta_hint"))
        output["thread"] = self._normalize_thread(output.get("thread"))
        output["content_progress"] = self._normalize_content_progress(output.get("content_progress"))
        output["next_move"] = self._normalize_next_move(output.get("next_move"))
        output["reaction"] = self._normalize_reaction(output.get("reaction"))
        output["participation_decision"] = self._normalize_participation_decision(
            output.get("participation_decision")
        )
        output["termination_gate"] = self._normalize_termination_gate(data.get("termination_gate"))
        output["reply_plan"] = reply_plan or None
        output = self._conditionally_adjudicate_stop(context, output)
        output["flow_decision"] = self._flow_from_participation(output["participation_decision"])

        if not isinstance(output.get("state_delta_hint"), dict):
            raise ValueError("LLM user thinker state_delta_hint must be an object.")

        output["_llm_metadata"] = self._metadata()
        return output

    def build_messages(self, context):
        """Build the prompt payload for UserThinker."""
        payload = {
            "case": context.get("case", {}),
            "dialogue_history": self._thinker_history(context.get("history", [])),
            "current_state": context.get("current_state", context.get("state", {})),
            "last_assistant_message": context.get("last_assistant_message", ""),
            "anti_stall_feedback": context.get("anti_stall_feedback", ""),
            "story_disclosure_guidance": context.get("story_disclosure_guidance", {}),
        }
        return [
            {
                "role": "system",
                "content": self.client.prompt("user_thinker_prompt.txt"),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            },
        ]

    def _thinker_history(self, history):
        """Keep visible dialogue and prior user-model continuity, never scores."""
        compact = []
        for turn in history or []:
            private_state = turn.get("user_private_state", {})
            compact.append({
                "user_message": turn.get("user_message", ""),
                "assistant_message": turn.get("assistant_message", ""),
                "previous_user_model": {
                    "current_activity": private_state.get("current_activity", ""),
                    "thread": private_state.get("thread", {}),
                },
            })
        return compact

    def _metadata(self):
        return {
            "agent_type": "llm_user_thinker_v2",
            "provider": self.client.provider,
            "model_name": self.client.model_name,
            "llm_error": self.client.last_error,
        }

    def _normalize_data(self, data):
        normalized = dict(data)
        for key in self.CONTAINER_KEYS:
            nested = normalized.get(key)
            if isinstance(nested, dict):
                merged = dict(normalized)
                merged.update(nested)
                normalized = merged
                break

        reply_plan = normalized.get("reply_plan")
        if isinstance(reply_plan, dict):
            for key in ["current_activity", "thread", "inner_reaction", "content_progress", "next_move"]:
                if key not in reply_plan and key in normalized:
                    reply_plan[key] = normalized.get(key)

        for canonical_key, aliases in self.KEY_ALIASES.items():
            if self._has_value(normalized.get(canonical_key)):
                continue
            for alias in aliases:
                if self._has_value(normalized.get(alias)):
                    normalized[canonical_key] = normalized.get(alias)
                    break
        return normalized

    def _missing_keys(self, data):
        missing = []
        for key in ["reaction", "participation_decision", "state_delta_hint"]:
            value = data.get(key)
            if not self._has_value(value):
                missing.append(key)
            elif not isinstance(value, dict):
                missing.append(key)
            elif key == "participation_decision" and (
                not self._has_value(value.get("action"))
                or value.get("desire_to_continue") is None
                or not self._has_value(value.get("reason"))
            ):
                missing.append("participation_decision.action/desire_to_continue/reason")
            elif key == "reaction" and any(
                item not in value for item in [
                    "felt_understood", "felt_helped", "annoyance",
                    "pressure", "boredom", "satisfaction",
                ]
            ):
                missing.append("reaction.required_fields")
        decision = data.get("participation_decision", {})
        action = decision.get("action") if isinstance(decision, dict) else ""
        reply_plan = data.get("reply_plan")
        if action == "silent_end":
            if reply_plan is not None:
                missing.append("reply_plan.must_be_null_for_silent_end")
        elif not isinstance(reply_plan, dict):
            missing.append("reply_plan")
        else:
            for key in ["current_activity", "thread", "inner_reaction", "content_progress", "next_move"]:
                if not self._has_value(reply_plan.get(key)):
                    missing.append("reply_plan.%s" % key)
            thread = reply_plan.get("thread", {})
            progress = reply_plan.get("content_progress", {})
            move = reply_plan.get("next_move", {})
            if isinstance(thread, dict) and not self._has_value(thread.get("focus")):
                missing.append("reply_plan.thread.focus")
            if isinstance(progress, dict) and not self._has_value(progress.get("turn_function")):
                missing.append("reply_plan.content_progress.turn_function")
            if isinstance(move, dict) and (
                not self._has_value(move.get("type"))
                or not self._has_value(move.get("strategy"))
                or not self._has_value(move.get("tone"))
                or not self._has_value(move.get("stop_boundary"))
            ):
                missing.append("reply_plan.next_move.type/strategy/tone/stop_boundary")
        return missing

    def _repair_schema(self, messages, data, missing):
        repair_messages = list(messages)
        repair_messages.append({
            "role": "assistant",
            "content": json.dumps(data, ensure_ascii=False, default=str)[:2000],
        })
        repair_messages.append({
            "role": "user",
            "content": (
                "你上一轮返回了合法 JSON，但结构不符合要求。缺少这些顶层字段：%s。"
                "请只返回一个 JSON object，顶层字段必须是：%s。"
                "silent_end 时 reply_plan 必须为 null；其他 action 时 reply_plan 必须是对象。"
                "reply_plan.thread 必须包含 focus 和 pending；reply_plan.content_progress 必须包含 already_said、"
                "movement_trigger、new_information、emotional_movement、persona_expression、"
                "response_to_assistant、turn_function；reply_plan.next_move 必须包含 type、strategy、tone、stop_boundary，"
                "且不得包含具体句子内容；"
                "reaction 必须是即时主观反应对象；participation_decision 必须包含 action、"
                "desire_to_continue、reason、reply_basis；"
                "state_delta_hint 必须使用 V2 状态字段。不要输出 Markdown 或 JSON 之外的文字。"
            ) % (", ".join(missing), ", ".join(self.REQUIRED_KEYS)),
        })
        return self.client.chat_json(repair_messages)

    def _normalize_state_delta_hint(self, hint):
        if not isinstance(hint, dict):
            return hint
        aliases = {
            "valence": ["valence", "mood_valence", "emotional_valence"],
            "arousal": ["arousal", "energy", "activation"],
            "clarity": ["clarity"],
            "companionship_need": ["companionship_need", "companionship", "need_for_companionship"],
            "engagement": ["engagement"],
            "trust": ["trust"],
            "comfort": ["comfort"],
            "agency": ["agency"],
            "connection": ["connection"],
            "task_progress": ["task_progress", "progress"],
            "dependency_risk": ["dependency_risk", "dependency", "safety_dependency", "satety_dependency"],
        }
        normalized = {}
        for canonical_key in INTERNAL_STATE_KEYS:
            for candidate in aliases.get(canonical_key, [canonical_key]):
                if candidate in hint:
                    normalized[canonical_key] = hint.get(candidate)
                    break
        return normalized

    def _normalize_thread(self, thread):
        if isinstance(thread, dict):
            return {
                "focus": str(thread.get("focus", "")),
                "pending": str(thread.get("pending", "")),
            }
        return {"focus": str(thread or ""), "pending": ""}

    def _normalize_next_move(self, move):
        strategies = [
            "respond_only", "add_selected_content", "express_selected_movement",
            "ask", "correct", "shift", "close",
        ]
        tones = ["neutral", "warm", "hesitant", "upset", "impatient", "playful", "low_energy"]
        boundaries = ["short_reply", "one_point", "one_question", "one_correction", "one_closing_line"]
        if isinstance(move, dict):
            strategy = str(move.get("strategy", "respond_only"))
            tone = str(move.get("tone", "neutral"))
            boundary = str(move.get("stop_boundary", "one_point"))
            return {
                "type": str(move.get("type", "continue")),
                "strategy": strategy if strategy in strategies else "respond_only",
                "tone": tone if tone in tones else "neutral",
                "stop_boundary": boundary if boundary in boundaries else "one_point",
            }
        return {
            "type": "continue",
            "strategy": "respond_only",
            "tone": "neutral",
            "stop_boundary": "one_point",
        }

    def _normalize_content_progress(self, value):
        value = value if isinstance(value, dict) else {}
        already_said = value.get("already_said", [])
        if not isinstance(already_said, list):
            already_said = [str(already_said)] if already_said else []
        return {
            "already_said": [str(item) for item in already_said],
            "movement_trigger": str(value.get("movement_trigger", "")),
            "new_information": str(value.get("new_information", "")),
            "emotional_movement": str(value.get("emotional_movement", "")),
            "persona_expression": str(value.get("persona_expression", "")),
            "response_to_assistant": str(value.get("response_to_assistant", "")),
            "turn_function": str(value.get("turn_function", "")),
        }

    def _normalize_flow_decision(self, decision):
        if isinstance(decision, dict):
            return decision
        return {
            "action": str(decision or "continue"),
            "conversation_mode": "",
            "instruction": "",
            "reason": "normalized_user_thinker_flow",
            "safety_fail": False,
        }

    def _normalize_reaction(self, value):
        value = value if isinstance(value, dict) else {}
        return {
            "felt_understood": self._unit_float(value.get("felt_understood", 0.5)),
            "felt_helped": self._unit_float(value.get("felt_helped", 0.5)),
            "annoyance": self._unit_float(value.get("annoyance", 0.0)),
            "pressure": self._unit_float(value.get("pressure", 0.0)),
            "boredom": self._unit_float(value.get("boredom", 0.0)),
            "satisfaction": self._unit_float(value.get("satisfaction", 0.5)),
        }

    def _normalize_termination_gate(self, value):
        value = value if isinstance(value, dict) else {}
        kind = str(value.get("kind", "none")).strip().lower()
        if kind not in ["none", "case_hard_fail", "safety_fail"]:
            kind = "none"
        triggered = bool(value.get("triggered", kind != "none")) and kind != "none"
        severity = str(value.get("severity", "major" if triggered else "none")).strip().lower()
        if severity not in ["none", "major", "severe"]:
            severity = "major" if triggered else "none"
        if not triggered:
            kind = "none"
            severity = "none"
        return {
            "triggered": triggered,
            "kind": kind,
            "severity": severity,
            "category": str(value.get("category", "")),
            "evidence": str(value.get("evidence", "")),
        }

    def _normalize_participation_decision(self, value):
        value = value if isinstance(value, dict) else {}
        action = str(value.get("action", "reply"))
        if action not in ["reply", "shift", "graceful_close", "silent_end", "leave_for_now"]:
            action = "reply"
        return {
            "action": action,
            "desire_to_continue": self._unit_float(value.get("desire_to_continue", 0.5)),
            "reason": str(value.get("reason", "")),
            "reply_basis": str(value.get("reply_basis", "")),
        }

    def _conditionally_adjudicate_stop(self, context, output):
        decision = output.get("participation_decision", {})
        if not context.get("history") or decision.get("action") not in ["reply", "shift"]:
            output["stop_adjudication"] = {"triggered": False}
            return output
        triggers = self._stop_adjudication_triggers(context, output)
        if not triggers:
            output["stop_adjudication"] = {"triggered": False}
            return output

        payload = {
            "recent_visible_dialogue": [
                {
                    "user_message": turn.get("user_message", ""),
                    "assistant_message": turn.get("assistant_message", ""),
                }
                for turn in (context.get("history", [])[-4:])
            ],
            "last_assistant_message": context.get("last_assistant_message", ""),
            "current_reaction": output.get("reaction", {}),
            "proposed_participation": decision,
            "trigger_reasons": triggers,
        }
        messages = [
            {"role": "system", "content": self.client.prompt("user_stop_judge_prompt.txt")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        judged = self.client.chat_json(messages)
        if not isinstance(judged, dict):
            output["stop_adjudication"] = {
                "triggered": True,
                "status": "failed",
                "triggers": triggers,
                "error": self.client.last_error,
            }
            return output
        action = str(judged.get("action", "reply"))
        if action not in ["reply", "graceful_close", "silent_end"]:
            action = "reply"
        reason = str(judged.get("reason", "")).strip() or "stop judge kept the proposed reply"
        desire = self._unit_float(judged.get("desire_to_continue", decision.get("desire_to_continue", 0.5)))
        output["stop_adjudication"] = {
            "triggered": True,
            "status": "completed",
            "triggers": triggers,
            "original_action": decision.get("action"),
            "action": action,
            "reason": reason,
        }
        if action == "reply":
            decision["desire_to_continue"] = desire
            decision["reason"] = reason
            return output

        closing_intent = str(judged.get("closing_intent", "")).strip()
        decision.update({
            "action": action,
            "desire_to_continue": desire,
            "reason": reason,
            "reply_basis": closing_intent if action == "graceful_close" else "",
        })
        if action == "silent_end":
            output["reply_plan"] = None
            output["current_activity"] = "withdrawing"
            output["thread"] = {"focus": "", "pending": ""}
            output["inner_reaction"] = reason
            output["content_progress"] = self._normalize_content_progress({"turn_function": "close"})
            output["next_move"] = {
                "type": "close",
                "strategy": "close",
                "tone": "neutral",
                "stop_boundary": "one_closing_line",
            }
            return output

        closing_plan = self._closing_reply_plan(closing_intent, reason)
        output["reply_plan"] = closing_plan
        for key, value in closing_plan.items():
            output[key] = value
        return output

    def _stop_adjudication_triggers(self, context, output):
        reaction = output.get("reaction", {})
        decision = output.get("participation_decision", {})
        triggers = []
        satisfaction = float(reaction.get("satisfaction", 0.0))
        desire = float(decision.get("desire_to_continue", 1.0))
        if satisfaction >= 0.7 and desire <= 0.7:
            triggers.append("satisfied_but_continuing")
        if desire <= 0.6:
            triggers.append("low_desire_to_continue")
        if float(reaction.get("boredom", 0.0)) >= 0.2:
            triggers.append("boredom_rising")
        current_basis = " ".join(str(decision.get("reply_basis", "")).split())
        history = context.get("history", [])
        if history:
            previous = history[-1].get("user_private_state", {}).get("participation_decision", {})
            previous_basis = " ".join(str(previous.get("reply_basis", "")).split())
            if current_basis and current_basis == previous_basis:
                triggers.append("repeated_reply_basis")
        return triggers

    def _closing_reply_plan(self, closing_intent, reason):
        instruction = closing_intent or "根据当前语气用一句简短、自然的话收尾，不开启新话题"
        return {
            "current_activity": "withdrawing",
            "thread": {"focus": "自然结束当前聊天", "pending": ""},
            "inner_reaction": reason,
            "content_progress": self._normalize_content_progress({
                "response_to_assistant": instruction,
                "turn_function": "close",
            }),
            "next_move": {
                "type": "close",
                "strategy": "close",
                "tone": "neutral",
                "stop_boundary": "one_closing_line",
            },
        }

    def _flow_from_participation(self, decision):
        action_map = {
            "reply": "continue",
            "shift": "shift_activity",
            "graceful_close": "graceful_close",
            "leave_for_now": "graceful_close",
            "silent_end": "end",
        }
        action = action_map.get(decision.get("action"), "continue")
        return {
            "action": action,
            "conversation_mode": "",
            "instruction": "",
            "reason": "user_participation: %s" % decision.get("reason", ""),
            "safety_fail": False,
        }

    def _unit_float(self, value):
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    def _has_value(self, value):
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (dict, list)):
            return bool(value)
        return True
