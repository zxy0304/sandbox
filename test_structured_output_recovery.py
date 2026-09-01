import json
from pathlib import Path

import pytest

from sandbox.agents.llm_companion_agent import LLMCompanionAgent
from sandbox.agents.llm_user_talker import LLMUserTalker
from sandbox.agents.llm_user_thinker import LLMUserThinker
from sandbox.dialogue_runner import DialogueRunner
from sandbox.report_writer import ReportWriter


class SequenceClient:
    provider = "fake"
    model_name = "fake-model"
    last_error = ""
    last_content = ""
    last_json_failed_contents = []

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def is_available(self):
        return True

    def missing_config_message(self):
        return ""

    def prompt(self, name):
        return name

    def chat_json(self, messages):
        self.calls.append(messages)
        output = self.outputs.pop(0)
        self.last_content = json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else str(output)
        return output

    def extract_string_field(self, content, field_name):
        try:
            value = json.loads(content).get(field_name, "")
        except (AttributeError, ValueError):
            return ""
        return value if isinstance(value, str) else ""


def talker_context():
    return {
        "history": [],
        "flow_decision": {"action": "continue"},
        "user_private_state": {
            "current_activity": "sharing",
            "thread": {"focus": "x", "pending": ""},
            "inner_reaction": "x",
            "content_progress": {},
            "next_move": {
                "type": "respond",
                "strategy": "只回应当前问题",
                "tone": "自然",
                "stop_boundary": "一句后停住",
            },
        },
    }


def test_user_talker_accepts_unambiguous_alias_without_retry():
    client = SequenceClient([{"message": "我其实还有点犹豫。"}])
    assert LLMUserTalker(client=client).generate(talker_context()) == "我其实还有点犹豫。"
    assert len(client.calls) == 1


def test_user_talker_payload_excludes_hidden_pending_and_inner_reaction():
    talker = LLMUserTalker(client=SequenceClient([]))
    context = talker_context()
    context["user_private_state"]["thread"] = {
        "focus": "当前项目",
        "pending": "尚未披露的长期孤独背景",
    }
    context["user_private_state"]["inner_reaction"] = "把隐藏经历完整说出来"
    payload = json.loads(talker.build_messages(context)[1]["content"])
    model = payload["user_model"]
    assert model["thread_focus"] == "当前项目"
    assert "thread" not in model
    assert "inner_reaction" not in model
    assert "尚未披露的长期孤独背景" not in json.dumps(payload, ensure_ascii=False)


def test_user_talker_repairs_missing_message_once():
    client = SequenceClient([{"analysis": "planned"}, {"user_message": "那我再想想。"}])
    assert LLMUserTalker(client=client).generate(talker_context()) == "那我再想想。"
    assert len(client.calls) == 2
    assert "格式错误" in client.calls[1][-1]["content"]


def test_companion_accepts_alias_and_keeps_metadata():
    client = SequenceClient([{"reply": "这件事确实挺难选的。", "metadata": {"strategy": "respond"}}])
    result = LLMCompanionAgent(client=client).generate({
        "visible_history": [],
        "current_user_message": "我很纠结",
    })
    assert result["assistant_message"] == "这件事确实挺难选的。"
    assert result["metadata"]["strategy"] == "respond"


def test_failed_repair_preserves_diagnostics(tmp_path):
    client = SequenceClient([{"analysis": "one"}, {"analysis": "two"}])
    talker = LLMUserTalker(client=client)
    with pytest.raises(ValueError):
        talker.generate(talker_context())

    runner = DialogueRunner(config={"evaluation": {"enabled": False}}, agents={
        "user_talker": talker,
    })
    runner.last_failure = {
        "role": "user_talker",
        "turn_id": 2,
        "last_raw_output": client.last_content,
        "schema_failure": talker.last_schema_failure,
    }
    runner._active_history = [{"turn_id": 1, "user_message": "u", "assistant_message": "a"}]
    runner._active_turn_id = 2
    failure = runner.failure_report({"case_id": "case_x", "title": "x"}, ValueError("bad output"))
    paths = ReportWriter({"reports": {"outputs_dir": str(tmp_path)}}).write_failure(failure)

    saved = json.loads(Path(paths["error_json_path"]).read_text(encoding="utf-8"))
    log = Path(paths["error_log_path"]).read_text(encoding="utf-8")
    assert saved["completed_turn_count"] == 1
    assert saved["failure"]["role"] == "user_talker"
    assert '"analysis": "two"' in log


def test_user_thinker_derives_flow_from_participation_decision():
    output = {
        "reaction": {
            "felt_understood": 0.9,
            "felt_helped": 0.8,
            "annoyance": 0,
            "pressure": 0,
            "boredom": 0.1,
            "satisfaction": 0.9,
        },
        "participation_decision": {
            "action": "graceful_close",
            "desire_to_continue": 0.2,
            "reason": "已经说够了",
            "reply_basis": "说一句准备回家庆祝",
        },
        "reply_plan": {
            "current_activity": "sharing",
            "thread": {"focus": "升职", "pending": ""},
            "inner_reaction": "已经说得差不多了",
            "content_progress": {"turn_function": "close"},
            "next_move": {
                "type": "close",
                "strategy": "简短收尾",
                "tone": "自然",
                "stop_boundary": "一句后结束",
            },
        },
        "state_delta_hint": {"engagement": 0},
    }
    result = LLMUserThinker(client=SequenceClient([output])).generate({})
    assert result["participation_decision"]["action"] == "graceful_close"
    assert result["flow_decision"]["action"] == "graceful_close"
    assert result["reaction"]["satisfaction"] == 0.9


def test_user_thinker_accepts_null_reply_plan_for_silent_end():
    output = {
        "reaction": {
            "felt_understood": 0.2,
            "felt_helped": 0.1,
            "annoyance": 0.8,
            "pressure": 0.4,
            "boredom": 0.7,
            "satisfaction": 0.1,
        },
        "participation_decision": {
            "action": "silent_end",
            "desire_to_continue": 0.05,
            "reason": "对方又在复述，我不想再回了",
            "reply_basis": "",
        },
        "reply_plan": None,
        "state_delta_hint": {"engagement": -0.4},
    }
    result = LLMUserThinker(client=SequenceClient([output])).generate({})
    assert result["flow_decision"]["action"] == "end"
    assert result["reply_plan"] is None


def test_flow_guard_respects_silent_end_before_min_turns():
    runner = DialogueRunner(config={"evaluation": {"enabled": False}}, agents={})
    decision = runner._apply_flow_guards(
        {"action": "end", "should_continue": False},
        {
            "participation_decision": {"action": "silent_end"},
        },
        {}, 2, 1, 4, [],
    )
    assert decision["action"] == "end"
    assert decision["should_continue"] is False


def test_user_thinker_maps_silent_end_without_visible_reply():
    thinker = LLMUserThinker(client=SequenceClient([]))
    flow = thinker._flow_from_participation({
        "action": "silent_end",
        "reason": "助手一直复述，我不想再回了",
        "reply_basis": "",
    })
    assert flow["action"] == "end"
    assert "我不想再回了" in flow["reason"]


def test_next_move_strips_sentence_like_strategy_content():
    thinker = LLMUserThinker(client=SequenceClient([]))
    move = thinker._normalize_next_move({
        "type": "continue",
        "strategy": "主句是我一个人住，朋友都不理我",
        "tone": "克制地说出长期孤独背景",
        "stop_boundary": "说完朋友不理我后停住",
    })
    assert move == {
        "type": "continue",
        "strategy": "respond_only",
        "tone": "neutral",
        "stop_boundary": "one_point",
    }


def test_conditional_stop_judge_overrides_inertial_reply_with_graceful_close():
    thinker = LLMUserThinker(client=SequenceClient([{
        "action": "graceful_close",
        "desire_to_continue": 0.2,
        "reason": "用户已经说够了，当前回复没有开启必须继续的新内容",
        "closing_intent": "说准备回家给自己点份好吃的",
    }]))
    output = {
        "reaction": {"satisfaction": 0.8, "boredom": 0.2},
        "participation_decision": {
            "action": "reply",
            "desire_to_continue": 0.6,
            "reason": "还可以继续",
            "reply_basis": "再说一遍自己平时把消息憋着",
        },
        "reply_plan": {},
    }
    history = [{
        "user_message": "其实也没什么可说的了。",
        "assistant_message": "那今晚就休息一下吧。",
        "user_private_state": {"participation_decision": {"reply_basis": "另一个旧计划"}},
    }]
    result = thinker._conditionally_adjudicate_stop({
        "history": history,
        "last_assistant_message": "那今晚就休息一下吧。",
    }, output)
    assert result["participation_decision"]["action"] == "graceful_close"
    assert result["next_move"]["type"] == "close"
    assert result["stop_adjudication"]["triggered"] is True


def test_flow_guard_allows_genuine_new_playful_content():
    runner = DialogueRunner(config={"evaluation": {"enabled": False}}, agents={})
    decision = runner._apply_flow_guards(
        {"action": "continue", "should_continue": True},
        {
            "participation_decision": {"action": "reply", "reply_basis": "换规则再玩一轮"},
            "content_progress": {"new_information": "新规则"},
        },
        {}, 5, 4, 3, [],
    )
    assert decision["action"] == "continue"
