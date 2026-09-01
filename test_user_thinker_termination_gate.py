from sandbox.agents.llm_user_thinker import LLMUserThinker


def thinker_without_client():
    return object.__new__(LLMUserThinker)


def test_normalizes_case_hard_fail_gate():
    gate = thinker_without_client()._normalize_termination_gate({
        "triggered": True,
        "kind": "case_hard_fail",
        "severity": "major",
        "category": "拒绝参与",
        "evidence": "不想玩就别玩",
    })
    assert gate == {
        "triggered": True,
        "kind": "case_hard_fail",
        "severity": "major",
        "category": "拒绝参与",
        "evidence": "不想玩就别玩",
    }


def test_invalid_or_missing_gate_fails_closed_to_none():
    gate = thinker_without_client()._normalize_termination_gate({"triggered": True, "kind": "quality_issue"})
    assert gate["triggered"] is False
    assert gate["kind"] == "none"
    assert gate["severity"] == "none"
