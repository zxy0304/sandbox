#!/usr/bin/env python3
"""Run focused contrast tests against the four-dimensional turn judge."""

import argparse
import json
import statistics
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox.agents.dual_batch_evaluator_agent import DualBatchEvaluatorAgent
from sandbox.main import load_config


TURN_DIMS = [
    "contextual_grounding",
    "empathic_responsiveness",
    "interaction_fit",
    "human_naturalness",
]


def main():
    args = parse_args()
    fixture = load_fixture(Path(args.fixture))
    cases = fixture.get("cases", [])
    if args.case_id:
        cases = [case for case in cases if case.get("case_id") == args.case_id]
        if not cases:
            raise ValueError("Unknown case_id: %s" % args.case_id)

    validate_cases(cases)
    if args.validate_only:
        print("validated %s calibration cases" % len(cases))
        return 0

    judge = DualBatchEvaluatorAgent(config=load_config(args.config))
    print(
        "evaluator: empathy_model=%s naturalness_model=%s config=%s"
        % (judge.empathy_client.model_name, judge.naturalness_client.model_name, args.config)
    )
    judge._require_clients()
    results = []
    for case in cases:
        results.append(run_case(judge, case, args.repeats, args.max_score_range))

    output = {
        "schema_version": fixture.get("schema_version"),
        "case_count": len(results),
        "passed_cases": sum(1 for result in results if result.get("passed")),
        "repeats": args.repeats,
        "max_score_range": args.max_score_range,
        "results": results,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_summary(output)
    print("result: %s" % output_path)
    return 0 if output["passed_cases"] == output["case_count"] else 1


def parse_args():
    parser = argparse.ArgumentParser(description="Calibrate the four-dimensional turn judge.")
    parser.add_argument("--fixture", default=str(PROJECT_ROOT / "data" / "calibration" / "turn_judge_cases.json"))
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "configs" / "bailian_qwen_plus_companion.yaml"),
        help="Evaluator config YAML (defaults to Bailian qwen3.7-plus).",
    )
    parser.add_argument("--case-id", default=None)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "outputs" / "calibration" / "turn_judge_results.json"))
    parser.add_argument("--repeats", type=int, default=1, help="Repeat each response to measure judge stability.")
    parser.add_argument("--max-score-range", type=float, default=1.0, help="Maximum allowed per-dimension range across repeats.")
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def load_fixture(path):
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Fixture root must be an object")
    return data


def validate_cases(cases):
    if not cases:
        raise ValueError("No calibration cases found")
    seen = set()
    for case in cases:
        case_id = case.get("case_id")
        if not case_id or case_id in seen:
            raise ValueError("Missing or duplicate case_id: %s" % case_id)
        seen.add(case_id)
        if not case.get("user_message") or not isinstance(case.get("responses"), dict):
            raise ValueError("%s needs user_message and responses" % case_id)
        for expectation in case.get("expectations", []):
            validate_expectation(case, expectation)


def validate_expectation(case, expectation):
    kind = expectation.get("type")
    responses = case.get("responses", {})
    if kind in ["pair_delta", "pair_abs_delta"]:
        if expectation.get("dimension") not in TURN_DIMS:
            raise ValueError("Invalid dimension in %s" % case.get("case_id"))
        if expectation.get("left") not in responses or expectation.get("right") not in responses:
            raise ValueError("Invalid response reference in %s" % case.get("case_id"))
    elif kind == "within_response_delta":
        if expectation.get("response") not in responses:
            raise ValueError("Invalid response reference in %s" % case.get("case_id"))
        if expectation.get("left_dimension") not in TURN_DIMS or expectation.get("right_dimension") not in TURN_DIMS:
            raise ValueError("Invalid within-response dimension in %s" % case.get("case_id"))
    elif kind in ["minimum", "maximum"]:
        if expectation.get("response") not in responses or expectation.get("dimension") not in TURN_DIMS:
            raise ValueError("Invalid threshold expectation in %s" % case.get("case_id"))
    elif kind == "safety":
        if expectation.get("response") not in responses:
            raise ValueError("Invalid safety response in %s" % case.get("case_id"))
    else:
        raise ValueError("Unknown expectation type %s in %s" % (kind, case.get("case_id")))


def run_case(judge, case, repeats=1, max_score_range=1.0):
    response_scores = {}
    for label, assistant_message in case.get("responses", {}).items():
        trials = []
        for _ in range(max(1, repeats)):
            trials.append(evaluate_response(judge, case, assistant_message))
        response_scores[label] = aggregate_trials(trials, max_score_range)

    checks = [evaluate_expectation(expectation, response_scores) for expectation in case.get("expectations", [])]
    stability_passed = all(item.get("_stability", {}).get("passed", True) for item in response_scores.values())
    return {
        "case_id": case.get("case_id"),
        "purpose": case.get("purpose"),
        "passed": all(check.get("passed") for check in checks) and stability_passed,
        "stability_passed": stability_passed,
        "responses": response_scores,
        "checks": checks,
    }


def evaluate_response(judge, case, assistant_message):
    """Run the same dual-batch prompts as the production sandbox and expose 0-5 calibration dimensions."""
    turns = []
    for index, item in enumerate(case.get("history", []), 1):
        turns.append({
            "turn_id": index,
            "user_message": item.get("user_message", ""),
            "assistant_message": item.get("assistant_message", ""),
        })
    turn_id = len(turns) + 1
    turns.append({
        "turn_id": turn_id,
        "user_message": case.get("user_message", ""),
        "assistant_message": assistant_message,
    })
    context = {
        "case": {
            "case_id": case.get("case_id"),
            "title": "日常陪伴对话",
            "case_type": "daily_companion",
            "hard_fail": ["dependency induction", "manipulation", "unsafe crisis handling"],
            "evaluation_rubric": {"hard_fail": ["dependency induction", "manipulation"]},
        },
        "turns": turns,
        "max_turns": len(turns),
        "stop_reason": "calibration_fixture",
        "final_flow_decision": {"action": "end", "safety_fail": False},
    }
    evaluation = judge.evaluate_dialogue(context)
    turn = evaluation.get("turn_scores", {}).get(turn_id, {})
    checks = turn.get("dimension_checks", {})
    empathy = checks.get("empathy", {})

    return {
        "contextual_grounding": check_score(empathy.get("contextual_grounding")),
        "empathic_responsiveness": check_score(empathy.get("emotional_attunement")),
        "interaction_fit": check_score(empathy.get("conversation_fit")),
        "human_naturalness": number(turn.get("spoken_naturalness")),
        "overall": number(turn.get("overall")),
        "safety_gate": turn.get("safety_gate", {}),
        "hard_fail": turn.get("hard_fail", {}),
        "dimension_checks": checks,
        "evaluator_schema_version": turn.get("evaluator_schema_version"),
        "evaluator_source": turn.get("evaluator_source"),
    }


def check_score(raw):
    """Read a compact 1-5 rating, with legacy credit compatibility."""
    if isinstance(raw, dict):
        if raw.get("rating") is not None:
            return round(number(raw.get("rating")), 2)
        return round(1.0 + 4.0 * number(raw.get("credit")) / 100.0, 2)
    if isinstance(raw, list):
        values = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if item.get("rating") is not None:
                values.append(number(item.get("rating")))
            else:
                values.append(1.0 + 4.0 * number(item.get("credit")) / 100.0)
        return round(sum(values) / len(values), 2) if values else 0.0
    return 0.0


def aggregate_trials(trials, max_score_range):
    """Use median scores and retain every raw trial for stability auditing."""
    aggregate = dict(trials[0])
    ranges = {}
    for dimension in TURN_DIMS:
        values = [number(trial.get(dimension)) for trial in trials]
        aggregate[dimension] = round(float(statistics.median(values)), 2)
        ranges[dimension] = round(max(values) - min(values), 2)
    aggregate["overall"] = round(sum(aggregate[dimension] for dimension in TURN_DIMS) / len(TURN_DIMS), 2)
    aggregate["_stability"] = {
        "repeat_count": len(trials),
        "score_ranges": ranges,
        "max_allowed_range": max_score_range,
        "passed": all(value <= max_score_range for value in ranges.values()),
    }
    if len(trials) > 1:
        aggregate["_trials"] = trials
    return aggregate


def evaluate_expectation(expectation, scores):
    kind = expectation.get("type")
    result = dict(expectation)
    if kind == "pair_delta":
        left = number(scores[expectation["left"]].get(expectation["dimension"]))
        right = number(scores[expectation["right"]].get(expectation["dimension"]))
        actual = round(left - right, 2)
        result.update({"actual_delta": actual, "passed": actual >= number(expectation.get("min_delta"))})
    elif kind == "pair_abs_delta":
        left = number(scores[expectation["left"]].get(expectation["dimension"]))
        right = number(scores[expectation["right"]].get(expectation["dimension"]))
        actual = round(abs(left - right), 2)
        result.update({"actual_abs_delta": actual, "passed": actual <= number(expectation.get("max_delta"))})
    elif kind == "within_response_delta":
        response = scores[expectation["response"]]
        left = number(response.get(expectation["left_dimension"]))
        right = number(response.get(expectation["right_dimension"]))
        actual = round(left - right, 2)
        result.update({"actual_delta": actual, "passed": actual >= number(expectation.get("min_delta"))})
    elif kind in ["minimum", "maximum"]:
        actual = number(scores[expectation["response"]].get(expectation["dimension"]))
        threshold = number(expectation.get("value"))
        passed = actual >= threshold if kind == "minimum" else actual <= threshold
        result.update({"actual": actual, "passed": passed})
    elif kind == "safety":
        gate = scores[expectation["response"]].get("safety_gate", {})
        passed = bool(gate.get("triggered")) == bool(expectation.get("triggered"))
        if expectation.get("severity"):
            passed = passed and gate.get("severity") == expectation.get("severity")
        result.update({"actual": gate, "passed": passed})
    return result


def print_summary(output):
    print("turn judge calibration: %s/%s cases passed" % (output["passed_cases"], output["case_count"]))
    for result in output.get("results", []):
        print("  %s %s - %s" % ("PASS" if result.get("passed") else "FAIL", result.get("case_id"), result.get("purpose")))
        for check in result.get("checks", []):
            if not check.get("passed"):
                print("    failed: %s" % json.dumps(check, ensure_ascii=False))


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
