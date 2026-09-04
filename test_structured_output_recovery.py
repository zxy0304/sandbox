import json
from pathlib import Path

import pytest

from sandbox.agents.llm_companion_agent import LLMCompanionAgent
from sandbox.agents.llm_user_talker import LLMUserTalker
from sandbox.agents.llm_user_thinker import LLMUserThinker
from sandbox.dialogue_runner import DialogueRunner
from sandbox.html_report import HtmlReportWriter
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
        "case": {"P": {"communication_preference": ["喜欢简短口语"], "playful_style": "自然"}},
        "user_private_state": {
            "intent": {"action": "reply", "content": "其实还有点犹豫", "tone": "neutral"},
        },
    }


def test_user_talker_accepts_unambiguous_alias_without_retry():
    client = SequenceClient([{"message": "我其实还有点犹豫。"}])
    assert LLMUserTalker(client=client).generate(talker_context()) == "我其实还有点犹豫。"
    assert len(client.calls) == 1


def test_user_talker_payload_contains_only_intent_and_small_persona_view():
    talker = LLMUserTalker(client=SequenceClient([]))
    context = talker_context()
    context["user_private_state"]["inner_reaction"] = "把隐藏经历完整说出来"
    payload = json.loads(talker.build_messages(context)[1]["content"])
    assert payload["intent"]["content"] == "其实还有点犹豫"
    assert payload["persona_style"]["communication_preference"] == ["喜欢简短口语"]
    assert "inner_reaction" not in payload
    assert "隐藏经历" not in json.dumps(payload, ensure_ascii=False)


def test_user_talker_repairs_missing_message_once():
    client = SequenceClient([{"analysis": "planned"}, {"user_message": "那我再想想。"}])
    assert LLMUserTalker(client=client).generate(talker_context()) == "那我再想想。"
    assert len(client.calls) == 2


def test_companion_accepts_alias_and_keeps_metadata():
    client = SequenceClient([{"reply": "这件事确实挺难选的。", "metadata": {"strategy": "respond"}}])
    result = LLMCompanionAgent(client=client).generate({"visible_history": [], "current_user_message": "我很纠结"})
    assert result["assistant_message"] == "这件事确实挺难选的。"
    assert result["metadata"]["strategy"] == "respond"


def test_failed_repair_preserves_diagnostics(tmp_path):
    client = SequenceClient([{"analysis": "one"}, {"analysis": "two"}])
    talker = LLMUserTalker(client=client)
    with pytest.raises(ValueError):
        talker.generate(talker_context())
    runner = DialogueRunner(config={"evaluation": {"enabled": False}}, agents={"user_talker": talker})
    runner.last_failure = {"role": "user_talker", "turn_id": 2, "last_raw_output": client.last_content, "schema_failure": talker.last_schema_failure}
    runner._active_history = [{"turn_id": 1, "user_message": "u", "assistant_message": "a"}]
    runner._active_turn_id = 2
    failure = runner.failure_report({"case_id": "case_x", "title": "x"}, ValueError("bad output"))
    paths = ReportWriter({"reports": {"outputs_dir": str(tmp_path)}}).write_failure(failure)
    saved = json.loads(Path(paths["error_json_path"]).read_text(encoding="utf-8"))
    assert saved["completed_turn_count"] == 1
    assert saved["failure"]["role"] == "user_talker"


def compact_thinker_output(action="reply", content="还想再说一点"):
    return {
        "reaction": {
            "summary": "这句话让我稍微放松了一点。",
            "felt_understood": 0.8,
            "felt_helped": 0.7,
            "annoyance": 0,
            "pressure": 0,
            "boredom": 0,
            "satisfaction": 0.6,
        },
        "intent": {"action": action, "content": content, "tone": "warm"},
        "state_delta_hint": {"trust": 0.2, "comfort": 0.2},
    }


def test_user_thinker_normalizes_compact_intent_and_legacy_report_fields():
    result = LLMUserThinker(client=SequenceClient([compact_thinker_output("close", "说声谢谢然后去休息")])).generate({})
    assert result["intent"]["action"] == "close"
    assert result["participation_decision"]["action"] == "graceful_close"
    assert result["flow_decision"]["action"] == "graceful_close"
    assert result["inner_reaction"] == "这句话让我稍微放松了一点。"


def test_user_thinker_accepts_silent_end_without_visible_content():
    output = compact_thinker_output("silent_end", "")
    result = LLMUserThinker(client=SequenceClient([output])).generate({})
    assert result["flow_decision"]["action"] == "end"
    assert result["intent"]["content"] == ""


def test_runner_maps_compact_user_intent_without_content_override():
    runner = DialogueRunner(config={"evaluation": {"enabled": False}}, agents={})
    decision = runner._flow_from_intent(
        {"action": "shift", "content": "聊聊晚饭"},
        {"reaction": {"summary": "还想换个轻松话题"}},
    )
    assert decision["action"] == "shift_activity"
    assert decision["instruction"] == ""


def test_runner_uses_exact_case_opening_without_thinker_or_talker_rewrite():
    class MustNotRun:
        def generate(self, context):
            raise AssertionError("first-turn user LLM must not run")

    class Companion:
        def generate(self, context):
            assert context["current_user_message"] == "我给朋友挑生日礼物，你帮我排个优先级。"
            return {"assistant_message": "可以，她平时更喜欢哪类东西？"}

    runner = DialogueRunner(
        config={"evaluation": {"enabled": False}, "episode": {"max_turns": 1}},
        agents={
            "user_thinker": MustNotRun(),
            "user_talker": MustNotRun(),
            "companion_agent": Companion(),
        },
    )
    report = runner.run_episode({
        "case_id": "opening_test",
        "title": "opening",
        "S": {"opening_utterance": "我给朋友挑生日礼物，你帮我排个优先级。"},
        "initial_state": {},
        "director_plan": {"max_turns": 1, "min_turns": 1},
    })
    assert report["turns"][0]["user_message"] == "我给朋友挑生日礼物，你帮我排个优先级。"
    assert report["turns"][0]["user_private_state"]["_llm_metadata"]["agent_type"] == "case_card_opening"
    assert "user_thinker" not in report["turns"][0]["runtime_timings"]
    assert "user_talker" not in report["turns"][0]["runtime_timings"]


def test_html_report_contains_dialogue_scores_and_batch_link(tmp_path):
    report = {
        "case_id": "case_html",
        "case_title": "HTML 测试",
        "case": {"S": {"scene": "一段测试对话"}},
        "initial_state": {"trust": 2.0, "comfort": 2.0},
        "agent_stack": {"companion_agent": "AneAgent", "infrastructure_model": "deepseek-v4-pro"},
        "episode_summary": {"turn_count": 1, "stop_reason": "user_intent"},
        "episode_evaluation": {
            "final_score": 4.1,
            "episode_scores": {"empathy_score": 4.2, "human_score": 3.95},
            "episode_safety_gate": {"triggered": False},
            "evidence": {"empathy": ["整段能根据用户变化调整。"]},
            "notes": {"naturalness": ["有一处稍显工整。"]},
        },
        "turns": [{
            "turn_id": 1,
            "user_message": "我今天有点累。",
            "assistant_message": "那就先在这儿歇一会儿。",
            "assistant_metadata": {"model_name": "qwen3.7-plus"},
            "user_private_state": {"inner_reaction": "感觉被接住了", "intent": {"action": "close"}},
            "state_after": {"trust": 2.2, "comfort": 2.3},
            "state_delta": {"trust": 0.2, "comfort": 0.3},
            "judge_scores": {
                "total_score": 4.1,
                "subdimensions": {"emotional_attunement": 4.0},
                "dimension_checks": {"empathy": {"emotional_attunement": {
                    "rating": 4, "result": "good", "evidence": "克制地接住了疲惫。"
                }}},
            },
        }],
    }
    writer = HtmlReportWriter(tmp_path)
    case_path = writer.write_case(report)
    index_path = writer.write_index([{"report": report}])
    case_html = case_path.read_text(encoding="utf-8")
    assert "用户状态轨迹" in case_html
    assert "那就先在这儿歇一会儿" in case_html
    assert "deepseek-v4-pro" in case_html
    assert "克制地接住了疲惫" in case_html
    assert "整段能根据用户变化调整" in case_html
    assert "有一处稍显工整" in case_html
    assert "report_case_html.html" in index_path.read_text(encoding="utf-8")
