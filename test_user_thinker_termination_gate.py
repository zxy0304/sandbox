import json
from pathlib import Path

from sandbox.agents.llm_user_thinker import LLMUserThinker


def thinker_without_client():
    return object.__new__(LLMUserThinker)


def test_intent_rejects_unknown_action_and_tone():
    intent = thinker_without_client()._normalize_intent({
        "action": "direct_the_plot",
        "content": "继续说当前的事",
        "tone": "perform_a_stress_test",
    })
    assert intent == {
        "action": "reply",
        "content": "继续说当前的事",
        "tone": "neutral",
    }


def test_silent_end_cannot_carry_visible_content():
    intent = thinker_without_client()._normalize_intent({
        "action": "silent_end",
        "content": "这句不应该被说出来",
        "tone": "low_energy",
    })
    assert intent["content"] == ""


def test_thinker_prompt_requires_semantic_progress_without_a_director_schema():
    prompt = (Path(__file__).parent / "sandbox" / "prompts" / "user_thinker_prompt.txt").read_text(encoding="utf-8")
    assert "相同语义不能原地循环" in prompt
    assert "用户自己的全部历史发言" in prompt
    assert "中间隔了几轮" in prompt
    assert "如果既没有新内容，也没有新的互动功能" in prompt
    assert "Case 是记忆，不是剧本" in prompt
    assert "真人叙事的话拍" in prompt
    assert "content_progress" not in prompt
    assert "director_guidance" not in prompt


def test_thinker_receives_user_memory_but_not_benchmark_orchestration():
    thinker = thinker_without_client()
    thinker.client = type("PromptClient", (), {"prompt": lambda self, _: "prompt"})()
    messages = thinker.build_messages({
        "case": {
            "case_id": "case_x",
            "D": {"occupation": "图书馆员"},
            "C": {"latent_story_material": ["关东煮"]},
            "S": {
                "scene": "深夜",
                "true_problem": "评分者的深层解释",
                "activity_or_task": "目标答案",
            },
            "P": {
                "personality": ["念旧"],
                "communication_preference": ["喜欢简短表达"],
                "companion_preference": ["理想助手路径"],
                "support_preference": {"late_stage": "必须给方案"},
                "taboo_responses": ["禁忌答案"],
                "boundaries": ["评分边界"],
            },
            "expected_companion_path": ["引出关东煮"],
            "director_plan": {"stress_message": "预写台词"},
            "evaluation_rubric": {"key_success": ["标准答案"]},
            "hard_fail": ["评分规则"],
        },
        "history": [],
    })
    payload = json.loads(messages[1]["content"])
    visible_case = payload["case"]
    assert visible_case["C"]["latent_story_material"] == ["关东煮"]
    assert "director_plan" not in visible_case
    assert "expected_companion_path" not in visible_case
    assert "evaluation_rubric" not in visible_case
    assert "hard_fail" not in visible_case
    assert visible_case["P"]["communication_preference"] == ["喜欢简短表达"]
    assert "companion_preference" not in visible_case["P"]
    assert "support_preference" not in visible_case["P"]
    assert "taboo_responses" not in visible_case["P"]
    assert "boundaries" not in visible_case["P"]
    assert "true_problem" not in visible_case["S"]
    assert "activity_or_task" not in visible_case["S"]


def test_thinker_prompt_keeps_spoken_interaction_stance_in_intent():
    prompt = (Path(__file__).parent / "sandbox" / "prompts" / "user_thinker_prompt.txt").read_text(encoding="utf-8")
    assert "content 必须简短保留它" in prompt.replace("`", "")
    assert "完全没有回应对方" in prompt
    assert "reply 是用户真的还想再发一条" in prompt.replace("`", "")
    assert "不要连续构造" in prompt
    assert "不要按 latent_material 的列表顺序" in prompt.replace("`", "")
