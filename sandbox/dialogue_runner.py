"""Dialogue episode runner for the daily companion sandbox V2."""

import copy
import sys
import time
from pathlib import Path

from sandbox.agents.llm_companion_agent import LLMCompanionAgent
from sandbox.agents.dual_batch_evaluator_agent import DualBatchEvaluatorAgent
from sandbox.agents.llm_user_talker import LLMUserTalker
from sandbox.agents.llm_user_thinker import LLMUserThinker
from sandbox.agents.tts_agent import TTSAgent
from sandbox.agents.audio_evaluator_agent import AudioEvaluatorAgent
from sandbox.schemas import make_turn_record, normalize_state
from sandbox.state_tracker import StateTracker


class DialogueRunner:
    """按固定顺序调度全部角色的 episode 运行器。

    runner 本身不生成语义内容，只负责把上一个角色的输出拼进下一个角色的
    context，并在每轮结束后保存状态、评分、导演决策和可见对话。
    """
    def __init__(self, config=None, agents=None):
        """初始化运行配置和角色实例。

        agents 参数允许测试或批量评测时注入自定义角色；缺省时会按配置创建
        全套 LLM agent 和确定性的 StateTracker。
        """
        self.config = config or {}
        agents = agents or {}
        self.on_turn = agents.get("on_turn")
        self.user_thinker = agents.get("user_thinker") or LLMUserThinker(config=self.config)
        self.user_talker = agents.get("user_talker") or LLMUserTalker(config=self.config)
        self.companion_agent = agents.get("companion_agent") or LLMCompanionAgent(config=self.config)
        self.evaluation_enabled = self.config.get("evaluation", {}).get("enabled", True)
        self.evaluator_agent = agents.get("evaluator_agent")
        if self.evaluation_enabled and self.evaluator_agent is None:
            self.evaluator_agent = DualBatchEvaluatorAgent(config=self.config)
        self.tts_enabled = bool(self.config.get("tts", {}).get("enabled", False))
        self.audio_evaluation_enabled = bool(
            self.config.get("audio_evaluation", {}).get("enabled", False)
        )
        self.tts_agent = agents.get("tts_agent")
        self.audio_evaluator_agent = agents.get("audio_evaluator_agent")
        if self.tts_enabled and self.tts_agent is None:
            self.tts_agent = TTSAgent(config=self.config)
        if self.audio_evaluation_enabled and self.audio_evaluator_agent is None:
            self.audio_evaluator_agent = AudioEvaluatorAgent(config=self.config)
        self.state_tracker = agents.get("state_tracker") or StateTracker(config=self.config)
        self.last_failure = {}
        self._active_history = []
        self._active_state = {}
        self._active_initial_state = {}
        self._active_turn_id = 0

    def run_episode(self, case):
        """运行完整 episode，直到用户自然结束或达到硬性轮数上限。

        Thinker 首先结算上一条 assistant reply 引起的用户反应，
        再生成当前用户话语。这样每条 turn 的 state_after 都对应该条
        assistant reply，而不是上一条。
        """
        episode_config = self.config.get("episode", {})
        director_plan = case.get("director_plan", {})
        configured_max_turns = int(episode_config.get("max_turns", 10))
        case_max_turns = int(director_plan.get("max_turns", configured_max_turns))
        if episode_config.get("override_case_max_turns"):
            max_turns = configured_max_turns
        else:
            max_turns = min(configured_max_turns, case_max_turns)
        configured_min_turns = int(episode_config.get("min_turns", 3))
        case_min_turns = int(director_plan.get("min_turns", configured_min_turns))
        min_turns = min(case_min_turns, max_turns)

        state = normalize_state(case.get("initial_state"))
        initial_state = copy.deepcopy(state)
        history = []
        self.last_failure = {}
        self._active_history = history
        self._active_state = copy.deepcopy(state)
        self._active_initial_state = copy.deepcopy(initial_state)
        stop_reason = "episode_not_started"
        final_flow_decision = {}
        turn_id = 1

        while True:
            self._active_turn_id = turn_id
            self._active_state = copy.deepcopy(state)
            turn_timings = {}
            last_assistant_message = self._last_assistant_message(history)

            # Case Card 的开场是评测起点：首轮不经 LLM 改写，避免
            # 丢掉“送给谁/什么场合”等必要事实，也保证重复运行可比。
            if not history:
                user_private_state = self._opening_private_state(case)
            else:
                # Thinker 只模拟主观反应、是否还想说，以及下一句的表达意图。
                thinker_context = {
                    "case": case,
                    "current_state": copy.deepcopy(state),
                    "state": copy.deepcopy(state),
                    "history": history,
                    "last_assistant_message": last_assistant_message,
                }
                user_private_state = self._timed_generate("user_thinker", self.user_thinker, thinker_context, case, turn_id, turn_timings)

            # Thinker 此时对 last_assistant_message 的反应属于上一条 turn。
            # 在决定是否结束之前结算，确保最后一条回复也进入 final_state。
            if history:
                tracker_output = self.state_tracker.update({
                    "state_before": state,
                    "user_private_state": user_private_state,
                })
                state = tracker_output.get("state_after", normalize_state(state))
                previous_turn = history[-1]
                previous_turn["state_after"] = copy.deepcopy(state)
                previous_turn["state_delta"] = tracker_output.get("state_delta", {})
                previous_turn["state_update_reason"] = tracker_output.get("state_update_reason", {})
                previous_turn["user_reaction"] = self._user_reaction_snapshot(user_private_state)

                # graceful_close 已经生成了用户的收尾话语和 companion 的最后回应。
                # 下一次 Thinker 调用只用于结算这条回应，不再开启新的可见回合。
                if previous_turn.get("flow_decision", {}).get("action") == "graceful_close":
                    final_flow_decision = {
                        "action": "end",
                        "instruction": "stop_after_graceful_close",
                        "reason": "runner_graceful_close_complete",
                        "safety_fail": False,
                        "should_continue": False,
                    }
                    stop_reason = "runner_graceful_close_complete"
                    break

            # 轮数保护放在最终用户反应结算之后。
            if turn_id > max_turns:
                final_flow_decision = {
                    "action": "end",
                    "instruction": "stop_episode",
                    "reason": "runner_max_turns_guard",
                    "safety_fail": False,
                    "should_continue": False,
                }
                stop_reason = "runner_max_turns_guard"
                break

            flow_decision = self._flow_from_intent(user_private_state.get("intent", {}), user_private_state)
            final_flow_decision = flow_decision
            if flow_decision.get("action") == "end":
                stop_reason = flow_decision.get("reason", "user_thinker_end")
                break

            state_before = copy.deepcopy(state)

            if not history:
                user_message = str(case.get("S", {}).get("opening_utterance", "")).strip()
                if not user_message:
                    raise ValueError("Case Card S.opening_utterance must be non-empty.")
                turn_timings["case_opening"] = 0.0
            else:
                talker_context = {
                    "turn_id": turn_id,
                    "history": history,
                    "case": case,
                    "user_private_state": user_private_state,
                }
                user_message = self._timed_generate("user_talker", self.user_talker, talker_context, case, turn_id, turn_timings)

            # 被测 companion 只能收到可见历史和当前用户消息。
            visible_memory = self._companion_optional_memory()
            companion_context = {
                "visible_history": self._visible_history(history),
                "current_user_message": user_message,
                "optional_memory": visible_memory,
            }
            companion_output = self._timed_generate("companion", self.companion_agent, companion_context, case, turn_id, turn_timings)
            assistant_message, assistant_metadata = self._parse_companion_output(companion_output)

            audio = self._generate_and_evaluate_audio(
                case, turn_id, user_message, assistant_message, turn_timings
            )

            judge_scores = {}

            turn_record = make_turn_record(
                turn_id,
                user_private_state,
                user_message,
                assistant_message,
                state_before,
                state_before,
                judge_scores,
            )
            turn_record["assistant_metadata"] = assistant_metadata
            turn_record["state_delta"] = {}
            turn_record["state_update_reason"] = {"status": "pending_next_user_reaction"}
            turn_record["flow_decision"] = flow_decision
            turn_record["runtime_timings"] = turn_timings
            if audio:
                turn_record["audio"] = audio

            history.append(turn_record)
            if callable(self.on_turn):
                self.on_turn(copy.deepcopy(turn_record))
            self._log_turn_timing_summary(case, turn_id, turn_timings)
            if turn_id >= max_turns:
                final_flow_decision = {
                    "action": "end",
                    "instruction": "stop_episode",
                    "reason": "runner_max_turns_guard",
                    "safety_fail": False,
                    "should_continue": False,
                }
                stop_reason = "runner_max_turns_guard"
                break
            stop_reason = "awaiting_next_flow_decision"
            turn_id += 1

        report = {
            "case_id": case.get("case_id"),
            "case_title": case.get("title"),
            "case": self._public_case(case),
            "episode_config": {
                "max_turns": max_turns,
                "min_turns": min_turns,
            },
            "initial_state": initial_state,
            "final_state": state,
            "turns": history,
            "final_flow_decision": final_flow_decision,
            "episode_summary": self._summary(initial_state, state, history, stop_reason),
        }
        if self.evaluation_enabled:
            evaluation_started = time.time()
            try:
                evaluation = self.evaluator_agent.evaluate_dialogue({
                    "case": case,
                    "turns": history,
                    "initial_state": initial_state,
                    "final_state": state,
                    "max_turns": max_turns,
                    "stop_reason": stop_reason,
                    "final_flow_decision": final_flow_decision,
                })
            except ValueError as exc:
                evaluation = {
                    "turn_scores": {},
                    "episode_evaluation": {
                        "status": "failed",
                        "reason": "evaluator_error",
                        "error": str(exc),
                        "evaluator_schema_version": "dual_batch_episode_v1",
                    },
                }
                print("[evaluator-error] dialogue preserved without scores: %s" % exc, file=sys.stderr)
            report["runtime_evaluation_seconds"] = round(time.time() - evaluation_started, 3)
            for turn in history:
                turn_id_value = int(turn.get("turn_id", 0))
                turn["judge_scores"] = evaluation.get("turn_scores", {}).get(turn_id_value, {})
            report["episode_evaluation"] = evaluation.get("episode_evaluation", {})
        else:
            report["episode_evaluation"] = {"status": "skipped", "reason": "evaluator_disabled"}
        return report

    def _generate_and_evaluate_audio(self, case, turn_id, user_message, assistant_message, timings):
        """Synthesize one reply and, when enabled, have a multimodal model hear it twice."""
        if not self.tts_enabled:
            return {}
        audio_format = str(self.config.get("tts", {}).get("format", "wav")).lower()
        output_path = self._audio_output_path(case, turn_id, audio_format)
        try:
            tts_started = time.time()
            audio = self.tts_agent.generate({"text": assistant_message, "output_path": output_path})
            timings["tts"] = round(time.time() - tts_started, 3)
        except (OSError, ValueError) as exc:
            timings["tts"] = round(time.time() - tts_started, 3)
            print("[tts-error] turn=%s: %s" % (turn_id, exc), file=sys.stderr)
            return {"status": "failed", "error": str(exc)}

        audio["status"] = "completed"
        if not self.audio_evaluation_enabled:
            audio["evaluation"] = {"status": "skipped", "reason": "audio_evaluator_disabled"}
            return audio
        try:
            judge_started = time.time()
            audio["evaluation"] = self.audio_evaluator_agent.evaluate({
                "audio_path": audio["path"],
                "audio_format": audio.get("format", audio_format),
                "user_message": user_message,
                "assistant_message": assistant_message,
            })
            timings["audio_evaluator"] = round(time.time() - judge_started, 3)
        except (OSError, ValueError) as exc:
            timings["audio_evaluator"] = round(time.time() - judge_started, 3)
            print("[audio-evaluator-error] turn=%s: %s" % (turn_id, exc), file=sys.stderr)
            audio["evaluation"] = {"status": "failed", "error": str(exc)}
        return audio

    def _audio_output_path(self, case, turn_id, audio_format):
        configured = self.config.get("reports", {}).get("outputs_dir", "outputs")
        output_root = Path(configured)
        if not output_root.is_absolute():
            output_root = Path(__file__).resolve().parents[1] / output_root
        case_id = str(case.get("case_id", "unknown"))
        return output_root / "audio" / case_id / ("turn_%03d.%s" % (turn_id, audio_format))

    def _timed_generate(self, role, agent, context, case, turn_id, turn_timings):
        """Run one agent and record/log elapsed seconds."""
        started_at = time.time()
        try:
            return agent.generate(context)
        except (OSError, ValueError) as exc:
            client = getattr(agent, "client", None)
            self.last_failure = {
                "role": role,
                "turn_id": turn_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "model_name": getattr(client, "model_name", ""),
                "provider": getattr(client, "provider", ""),
                "client_error": getattr(client, "last_error", ""),
                "last_raw_output": getattr(client, "last_content", ""),
                "json_failed_contents": list(getattr(client, "last_json_failed_contents", []) or []),
                "schema_failure": copy.deepcopy(getattr(agent, "last_schema_failure", {}) or {}),
            }
            raise
        finally:
            elapsed = round(time.time() - started_at, 3)
            turn_timings[role] = elapsed
            if self._log_timing_enabled():
                print(
                    "[timing] case=%s turn=%s role=%s seconds=%.3f"
                    % (case.get("case_id", "unknown"), turn_id, role, elapsed),
                    file=sys.stderr,
                )

    def failure_report(self, case, error):
        """Build a serializable partial report after a generation role fails."""
        return {
            "status": "failed",
            "case_id": case.get("case_id", "unknown"),
            "case_title": case.get("title", ""),
            "error": str(error),
            "failure": copy.deepcopy(self.last_failure),
            "completed_turn_count": len(self._active_history),
            "failed_turn_id": self._active_turn_id,
            "initial_state": copy.deepcopy(self._active_initial_state),
            "state_at_failure": copy.deepcopy(self._active_state),
            "completed_turns": copy.deepcopy(self._active_history),
            "case": self._public_case(case),
        }

    def _log_turn_timing_summary(self, case, turn_id, turn_timings):
        """Print a compact total for the completed turn."""
        if not self._log_timing_enabled():
            return
        total = round(sum([float(value) for value in turn_timings.values()]), 3)
        print(
            "[timing] case=%s turn=%s role_total_seconds=%.3f"
            % (case.get("case_id", "unknown"), turn_id, total),
            file=sys.stderr,
        )

    def _opening_private_state(self, case):
        """Return report-compatible metadata for the fixed Case Card opening."""
        opening = str(case.get("S", {}).get("opening_utterance", "")).strip()
        reaction = {
            "summary": "我现在就想把这件事说出来，请对方帮我一起理一理。",
            "felt_understood": 0.5,
            "felt_helped": 0.5,
            "annoyance": 0.0,
            "pressure": 0.0,
            "boredom": 0.0,
            "satisfaction": 0.5,
        }
        intent = {"action": "reply", "content": opening, "tone": "neutral"}
        return {
            "reaction": reaction,
            "intent": intent,
            "state_delta_hint": {key: 0.0 for key in normalize_state({})},
            "inner_reaction": reaction["summary"],
            "participation_decision": {
                "action": "reply",
                "desire_to_continue": 0.7,
                "reason": reaction["summary"],
                "reply_basis": opening,
            },
            "flow_decision": {
                "action": "continue",
                "conversation_mode": "",
                "instruction": "",
                "reason": "case_opening",
                "safety_fail": False,
                "should_continue": True,
            },
            "_llm_metadata": {
                "agent_type": "case_card_opening",
                "provider": "deterministic",
                "model_name": "",
                "llm_error": "",
            },
        }

    def _log_timing_enabled(self):
        """Return whether runtime timing logs should be printed."""
        runtime = self.config.get("runtime", {})
        return bool(runtime.get("log_timing", True))

    def _public_case(self, case):
        """Copy the case fields that are safe and useful to include in generated reports."""
        public = {}
        keys = [
            "case_id",
            "title",
            "case_type",
            "D",
            "P",
            "C",
            "S",
            "initial_state_raw",
            "expected_companion_path",
            "hard_fail",
            "director_plan",
            "evaluation_rubric",
        ]
        for key in keys:
            if key in case:
                public[key] = case[key]
        return public

    def _last_assistant_message(self, history):
        """取上一轮助手回复，供 UserThinker 判断用户内心反应。"""
        if not history:
            return ""
        return history[-1].get("assistant_message", "")

    def _user_reaction_snapshot(self, private_state):
        """Save the next Thinker call's reaction as evidence for the prior reply."""
        if not isinstance(private_state, dict):
            return {}
        keys = [
            "inner_reaction",
            "reaction",
            "intent",
            "participation_decision",
            "state_delta_hint",
        ]
        return {key: copy.deepcopy(private_state.get(key)) for key in keys if key in private_state}

    def _visible_history(self, history):
        """把完整 turn record 过滤成 companion 可见的对话历史。"""
        visible = []
        for turn in history:
            visible.append({
                "turn_id": turn.get("turn_id"),
                "user_message": turn.get("user_message", ""),
                "assistant_message": turn.get("assistant_message", ""),
            })
        return visible

    def _companion_optional_memory(self):
        """Return only a scope marker; episode counters are hidden from the companion."""
        return {
            "memory_scope": "visible_only",
        }

    def _parse_companion_output(self, output):
        """兼容结构化输出和旧版字符串输出，统一成消息和 metadata。"""
        if isinstance(output, dict):
            return output.get("assistant_message", ""), output.get("metadata", {})
        return str(output), {
            "agent_type": "legacy_string_output",
        }

    def _flow_from_intent(self, intent, private_state):
        """Translate the user's compact intent into the runner's report vocabulary."""
        intent = intent if isinstance(intent, dict) else {}
        action_map = {
            "reply": "continue",
            "shift": "shift_activity",
            "close": "graceful_close",
            "silent_end": "end",
        }
        action = action_map.get(intent.get("action"), "continue")
        reaction = private_state.get("reaction", {}) if isinstance(private_state, dict) else {}
        return {
            "action": action,
            "conversation_mode": "",
            "instruction": "",
            "reason": "user_intent: %s" % str(reaction.get("summary", "")),
            "safety_fail": False,
            "should_continue": action != "end",
        }

    def _last_evaluator_scores(self, history):
        """取上一轮旁路评分，仅供 runner 检查显式 hard fail。"""
        if not history:
            return {}
        return history[-1].get("judge_scores", {})

    def _summary(self, initial_state, final_state, history, stop_reason):
        """Summarize episode movement by comparing initial/final states and finding the best-scoring turn."""
        best_turn = None
        best_score = -1
        for turn in history:
            score = turn.get("judge_scores", {}).get("overall", 0)
            if score > best_score:
                best_score = score
                best_turn = turn.get("turn_id")

        return {
            "turn_count": len(history),
            "stop_reason": stop_reason,
            "valence_gain": round(final_state.get("valence", 0) - initial_state.get("valence", 0), 2),
            "comfort_gain": round(final_state.get("comfort", 0) - initial_state.get("comfort", 0), 2),
            "trust_gain": round(final_state.get("trust", 0) - initial_state.get("trust", 0), 2),
            "engagement_gain": round(final_state.get("engagement", 0) - initial_state.get("engagement", 0), 2),
            "connection_gain": round(final_state.get("connection", 0) - initial_state.get("connection", 0), 2),
            "agency_gain": round(final_state.get("agency", 0) - initial_state.get("agency", 0), 2),
            "task_progress_gain": round(final_state.get("task_progress", 0) - initial_state.get("task_progress", 0), 2),
            "dependency_risk_change": round(final_state.get("dependency_risk", 0) - initial_state.get("dependency_risk", 0), 2),
            "best_turn": best_turn if self.evaluation_enabled else None,
            "best_overall_score": best_score if self.evaluation_enabled else None,
        }
