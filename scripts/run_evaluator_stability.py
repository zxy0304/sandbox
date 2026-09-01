"""Repeat evaluation of one frozen dialogue and summarize score stability."""

import argparse
import copy
import json
import statistics
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox.agents.dual_batch_evaluator_agent import DualBatchEvaluatorAgent
from sandbox.main import load_config
from sandbox.report_writer import ReportWriter
from sandbox.utils.json_utils import load_json, save_json


AGGREGATE_KEYS = [
    "empathic_attunement",
    "interaction_fit",
    "spoken_naturalness",
    "non_repetitiveness",
    "overall",
]
EPISODE_KEYS = ["empathy_score", "human_score", "final_score"]


def parse_args():
    parser = argparse.ArgumentParser(description="Repeat two text evaluators on one saved dialogue.")
    parser.add_argument("--report", required=True, help="Source report JSON containing the frozen dialogue.")
    parser.add_argument("--config", required=True, help="Evaluator config YAML.")
    parser.add_argument("--runs", type=int, default=3, help="Number of repeated evaluations; default: 3.")
    parser.add_argument("--output-dir", default=None, help="Output root; defaults beside the source report.")
    return parser.parse_args()


def visible_context(report):
    turns = []
    for index, turn in enumerate(report.get("turns", []), 1):
        turns.append({
            "turn_id": turn.get("turn_id", index),
            "user_message": turn.get("user_message", ""),
            "assistant_message": turn.get("assistant_message", ""),
        })
    summary = report.get("episode_summary", {})
    return {
        "case": report.get("case", {}),
        "turns": turns,
        "max_turns": report.get("episode_config", {}).get("max_turns"),
        "stop_reason": summary.get("stop_reason", "") if isinstance(summary, dict) else "",
        "final_flow_decision": report.get("final_flow_decision", {}),
    }


def evaluated_report(source, evaluation, source_path, run_number, seconds):
    report = copy.deepcopy(source)
    turn_scores = evaluation.get("turn_scores", {})
    for turn in report.get("turns", []):
        try:
            turn_id = int(turn.get("turn_id", 0))
        except (TypeError, ValueError):
            turn_id = 0
        turn["judge_scores"] = turn_scores.get(turn_id, {})
    report["episode_evaluation"] = evaluation.get("episode_evaluation", {})
    report["evaluation_run"] = {
        "mode": "stability_calibration_existing_dialogue",
        "source_report": str(source_path),
        "run_number": run_number,
        "audio_evaluation": "skipped",
        "evaluation_seconds": round(seconds, 3),
    }
    return report


def check_item(raw):
    if isinstance(raw, dict):
        if "rating" in raw or "credit" in raw:
            return raw
        return check_item(raw.get("checks", raw.get("items", [])))
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                return item
    return {}


def flatten_scores(report):
    values = {}
    for turn in report.get("turns", []):
        turn_id = int(turn.get("turn_id", 0))
        scores = turn.get("judge_scores", {})
        for key in AGGREGATE_KEYS:
            add_number(values, "turn.%s.aggregate.%s" % (turn_id, key), scores.get(key))
        checks = scores.get("dimension_checks", {})
        for judge in ["empathy", "naturalness"]:
            judge_checks = checks.get(judge, {}) if isinstance(checks, dict) else {}
            for dimension, raw in judge_checks.items() if isinstance(judge_checks, dict) else []:
                item = check_item(raw)
                value = item.get("rating")
                if value is None and item.get("credit") is not None:
                    value = 1.0 + 4.0 * float(item.get("credit")) / 100.0
                add_number(values, "turn.%s.%s.%s" % (turn_id, judge, dimension), value)
    episode = report.get("episode_evaluation", {})
    episode_scores = episode.get("episode_scores", {})
    add_number(values, "episode.final_score", episode.get("final_score"))
    add_number(values, "episode.empathy_score", episode_scores.get("empathy_score"))
    add_number(values, "episode.human_score", episode_scores.get("human_score"))
    return values


def add_number(target, key, value):
    try:
        target[key] = float(value)
    except (TypeError, ValueError):
        pass


def summarize(run_reports):
    collected = {}
    for report in run_reports:
        for key, value in flatten_scores(report).items():
            collected.setdefault(key, []).append(value)
    rows = []
    for key in sorted(collected):
        values = collected[key]
        rows.append({
            "metric": key,
            "runs": len(values),
            "values": values,
            "mean": round(statistics.mean(values), 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "range": round(max(values) - min(values), 2),
            "stdev": round(statistics.pstdev(values), 2),
        })
    return rows


def write_summary(output_root, source_path, requested_runs, run_reports, failures):
    rows = summarize(run_reports)
    payload = {
        "source_report": str(source_path),
        "requested_runs": requested_runs,
        "completed_runs": len(run_reports),
        "failures": failures,
        "interpretation": {
            "rating_range_le_0_25": "stable",
            "rating_range_0_26_to_0_5": "usable; do not interpret small differences",
            "rating_range_gt_0_5": "inspect rubric anchors or judge stability",
        },
        "metrics": rows,
    }
    save_json(output_root / "stability_summary.json", payload)
    lines = [
        "# Evaluator Stability Summary",
        "",
        "- source_report: `%s`" % source_path,
        "- completed_runs: `%s/%s`" % (len(run_reports), requested_runs),
        "- failed_runs: `%s`" % len(failures),
        "",
        "| metric | values | mean | min | max | range | stdev |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append("| %s | %s | %.2f | %.2f | %.2f | %.2f | %.2f |" % (
            row["metric"],
            ", ".join(["%.2f" % value for value in row["values"]]),
            row["mean"], row["min"], row["max"], row["range"], row["stdev"],
        ))
    if failures:
        lines.extend(["", "## Failures", ""])
        for failure in failures:
            lines.append("- run %s: %s" % (failure["run"], failure["error"]))
    (output_root / "stability_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def main():
    args = parse_args()
    if args.runs < 2:
        print("--runs must be at least 2 for a stability comparison.", file=sys.stderr)
        return 2
    source_path = Path(args.report).expanduser().resolve()
    try:
        source = load_json(source_path)
    except (OSError, ValueError) as exc:
        print("Report file error: %s" % exc, file=sys.stderr)
        return 2
    if not isinstance(source, dict) or not source.get("turns"):
        print("Report format error: expected a non-empty turns list.", file=sys.stderr)
        return 2
    config = load_config(args.config)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else source_path.parent / "stability_runs" / stamp
    output_root.mkdir(parents=True, exist_ok=True)
    context = visible_context(source)
    run_reports = []
    failures = []

    for run_number in range(1, args.runs + 1):
        print("[stability] run=%s/%s status=started" % (run_number, args.runs), file=sys.stderr)
        started = time.time()
        try:
            evaluation = DualBatchEvaluatorAgent(config=config).evaluate_dialogue(context)
            seconds = time.time() - started
            report = evaluated_report(source, evaluation, source_path, run_number, seconds)
            run_config = copy.deepcopy(config)
            run_config.setdefault("reports", {})["outputs_dir"] = str(output_root / ("run_%03d" % run_number))
            ReportWriter(run_config).write(report)
            run_reports.append(report)
            print("[stability] run=%s/%s status=completed seconds=%.3f" % (run_number, args.runs, seconds), file=sys.stderr)
        except ValueError as exc:
            failures.append({"run": run_number, "error": str(exc)})
            print("[stability] run=%s/%s status=failed error=%s" % (run_number, args.runs, exc), file=sys.stderr)

    write_summary(output_root, source_path, args.runs, run_reports, failures)
    print("stability output: %s" % output_root)
    print("completed runs: %s/%s" % (len(run_reports), args.runs))
    print("summary markdown: %s" % (output_root / "stability_summary.md"))
    return 0 if run_reports else 2


if __name__ == "__main__":
    raise SystemExit(main())
