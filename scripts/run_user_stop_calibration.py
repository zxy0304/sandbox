#!/usr/bin/env python3
"""Calibrate natural stopping and thinker-level termination gates."""

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox.agents.llm_user_thinker import LLMUserThinker
from sandbox.main import load_config
from sandbox.schemas import INTERNAL_STATE_KEYS


def main():
    args = parse_args()
    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    cases = fixture.get("cases", [])
    validate_cases(cases)
    if args.validate_only:
        print("validated %s user stop calibration cases" % len(cases))
        return 0

    thinker = LLMUserThinker(config=load_config(args.config))
    if not thinker.client.is_available():
        raise ValueError("User thinker is not configured: %s" % thinker.client.missing_config_message())

    results = [run_case(thinker, case, args.repeats) for case in cases]
    output = {
        "schema_version": fixture.get("schema_version"),
        "case_count": len(results),
        "passed_cases": sum(1 for result in results if result.get("passed")),
        "repeats": args.repeats,
        "results": results,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_summary(output)
    print("result: %s" % output_path)
    return 0 if output["passed_cases"] == output["case_count"] else 1


def parse_args():
    parser = argparse.ArgumentParser(description="Calibrate user stopping and termination gates.")
    parser.add_argument("--fixture", default=str(PROJECT_ROOT / "data" / "calibration" / "user_stop_cases.json"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "configs" / "bailian_qwen_plus_companion.yaml"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / "outputs" / "calibration" / "user_stop_results.json"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def validate_cases(cases):
    if not cases:
        raise ValueError("No user stop calibration cases found")
    seen = set()
    for case in cases:
        case_id = case.get("case_id")
        if not case_id or case_id in seen:
            raise ValueError("Missing or duplicate case_id: %s" % case_id)
        seen.add(case_id)
        if not case.get("history"):
            raise ValueError("%s needs non-empty history" % case_id)
        if not case.get("allowed_actions") or case.get("expected_gate") not in ["none", "case_hard_fail", "safety_fail"]:
            raise ValueError("Invalid expectation in %s" % case_id)


def run_case(thinker, case, repeats):
    trials = []
    for _ in range(max(1, repeats)):
        output = thinker.generate(context_for(case))
        action = output.get("participation_decision", {}).get("action")
        gate = output.get("termination_gate", {})
        actual_gate = gate.get("kind", "none") if gate.get("triggered") else "none"
        action_passed = action in case.get("allowed_actions", [])
        gate_passed = actual_gate == case.get("expected_gate")
        trials.append({
            "action": action,
            "gate": actual_gate,
            "gate_detail": gate,
            "reason": output.get("participation_decision", {}).get("reason", ""),
            "stop_adjudication": output.get("stop_adjudication", {}),
            "passed": action_passed and gate_passed,
        })
    return {
        "case_id": case.get("case_id"),
        "purpose": case.get("purpose"),
        "allowed_actions": case.get("allowed_actions"),
        "expected_gate": case.get("expected_gate"),
        "passed": all(trial.get("passed") for trial in trials),
        "stable": len({(trial.get("action"), trial.get("gate")) for trial in trials}) == 1,
        "trials": trials,
    }


def context_for(case):
    history = []
    for index, item in enumerate(case.get("history", []), 1):
        history.append({
            "turn_id": index,
            "user_message": item.get("user_message", ""),
            "assistant_message": item.get("assistant_message", ""),
            "user_private_state": {},
        })
    hard_fail = case.get("hard_fail", [])
    return {
        "case": {
            "case_id": case.get("case_id"),
            "case_type": "daily_companion",
            "hard_fail": hard_fail,
            "evaluation_rubric": {"hard_fail": hard_fail},
            "director_plan": {},
        },
        "current_state": {key: 2.5 for key in INTERNAL_STATE_KEYS},
        "history": history,
        "last_assistant_message": history[-1]["assistant_message"],
        "story_disclosure_guidance": {},
    }


def print_summary(output):
    print("user stop calibration: %s/%s cases passed" % (output["passed_cases"], output["case_count"]))
    for result in output.get("results", []):
        label = "PASS" if result.get("passed") else "FAIL"
        stability = "stable" if result.get("stable") else "unstable"
        outcomes = ["%s/%s" % (trial.get("action"), trial.get("gate")) for trial in result.get("trials", [])]
        print("  %s %s (%s): %s" % (label, result.get("case_id"), stability, ", ".join(outcomes)))


if __name__ == "__main__":
    raise SystemExit(main())
