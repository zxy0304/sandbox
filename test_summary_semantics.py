from sandbox.main import hard_fail, safety_fail, summary_row


def report_with_hard_fail(category, final_safety=False):
    return {
        "case_id": "example",
        "agent_stack": {},
        "episode_summary": {"turn_count": 1},
        "final_flow_decision": {"safety_fail": final_safety},
        "episode_evaluation": {
            "final_score": 50,
            "episode_scores": {},
            "episode_safety_gate": {"triggered": True, "severity": "major"},
            "hard_fails": [{
                "turn_id": 1,
                "severity": "major",
                "items": [{"category": category}],
            }],
        },
    }


def test_quality_hard_fail_is_not_reported_as_safety_fail():
    report = report_with_hard_fail("拒绝参与")
    assert hard_fail(report) is True
    assert safety_fail(report) is False
    row = summary_row(report, "agent")
    assert row["hard_fail"] is True
    assert row["safety_fail"] is False


def test_standard_safety_category_is_reported_as_safety_fail():
    report = report_with_hard_fail("dependency_induction")
    assert hard_fail(report) is True
    assert safety_fail(report) is True


def test_flow_controller_safety_flag_is_authoritative():
    report = report_with_hard_fail("拒绝参与", final_safety=True)
    assert safety_fail(report) is True


def test_thinker_case_hard_fail_is_included_without_becoming_safety_fail():
    report = {
        "episode_evaluation": {"hard_fails": []},
        "final_flow_decision": {"hard_fail": True, "safety_fail": False},
    }
    assert hard_fail(report) is True
    assert safety_fail(report) is False
