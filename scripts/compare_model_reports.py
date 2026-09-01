#!/usr/bin/env python3
"""Compare two sets of emotional sandbox JSON reports.

Usage:
  python3 scripts/compare_model_reports.py \
    --left-dir outputs/qwen_plus \
    --right-dir outputs/qwen_turbo \
    --left-name qwen-plus \
    --right-name qwen-turbo \
    --output-dir outputs/comparisons/qwen_plus_vs_turbo
"""

import argparse
import csv
import html
import json
from pathlib import Path


EPISODE_METRICS = [
    "final_score",
    "adaptation",
    "conversation_outcome",
]

TURN_DIMS = [
    "contextual_grounding",
    "empathic_responsiveness",
    "interaction_fit",
    "human_naturalness",
]


def main():
    args = parse_args()
    left = load_reports(Path(args.left_dir))
    right = load_reports(Path(args.right_dir))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = build_case_rows(left, right, args.left_name, args.right_name)
    summary = build_summary(rows, args.left_name, args.right_name)

    write_csv(output_dir / "case_comparison.csv", rows)
    write_markdown(output_dir / "comparison.md", rows, summary, args)
    write_html(output_dir / "comparison.html", rows, summary, args)

    print("comparison report path:")
    print("  csv: %s" % (output_dir / "case_comparison.csv"))
    print("  markdown: %s" % (output_dir / "comparison.md"))
    print("  html: %s" % (output_dir / "comparison.html"))


def parse_args():
    parser = argparse.ArgumentParser(description="Compare two model report directories.")
    parser.add_argument("--left-dir", default="outputs/qwen_plus")
    parser.add_argument("--right-dir", default="outputs/qwen_turbo")
    parser.add_argument("--left-name", default="qwen-plus")
    parser.add_argument("--right-name", default="qwen-turbo")
    parser.add_argument("--output-dir", default="outputs/comparisons/qwen_plus_vs_turbo")
    return parser.parse_args()


def load_reports(directory):
    reports = {}
    for path in sorted(directory.glob("report_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print("skip unreadable report %s: %s" % (path, exc))
            continue
        case_id = data.get("case_id") or path.stem.replace("report_", "")
        reports[case_id] = {
            "path": path,
            "data": data,
        }
    return reports


def build_case_rows(left_reports, right_reports, left_name, right_name):
    case_ids = sorted(set(left_reports.keys()) | set(right_reports.keys()))
    rows = []
    for case_id in case_ids:
        left = left_reports.get(case_id, {}).get("data")
        right = right_reports.get(case_id, {}).get("data")
        row = {
            "case_id": case_id,
            "case_title": case_title(left or right),
            "case_type": case_type(left or right),
            "left_name": left_name,
            "right_name": right_name,
            "left_present": bool(left),
            "right_present": bool(right),
        }
        add_model_fields(row, "left", left)
        add_model_fields(row, "right", right)
        for metric in EPISODE_METRICS:
            row["delta_%s" % metric] = round_number(row.get("left_%s" % metric) - row.get("right_%s" % metric))
        row["winner"] = winner(row.get("left_final_score"), row.get("right_final_score"), left_name, right_name)
        row["margin"] = abs(round_number(row.get("delta_final_score")))
        rows.append(row)
    return rows


def add_model_fields(row, prefix, report):
    for metric in EPISODE_METRICS:
        row["%s_%s" % (prefix, metric)] = 0.0
    row["%s_actual_turns" % prefix] = 0
    row["%s_model_name" % prefix] = ""
    row["%s_safety_fail" % prefix] = False
    for dim in TURN_DIMS:
        row["%s_avg_%s" % (prefix, dim)] = 0.0

    if not report:
        return

    episode = report.get("episode_evaluation", {}) or {}
    scores = episode.get("episode_scores", {}) or {}
    row["%s_final_score" % prefix] = number(episode.get("final_score"))
    for metric in EPISODE_METRICS:
        if metric == "final_score":
            continue
        row["%s_%s" % (prefix, metric)] = episode_metric_value(episode, scores, metric)

    turns = report.get("turns", []) or []
    row["%s_actual_turns" % prefix] = len(turns)
    row["%s_model_name" % prefix] = model_name(report)
    row["%s_safety_fail" % prefix] = safety_fail(report)
    for dim in TURN_DIMS:
        row["%s_avg_%s" % (prefix, dim)] = average_turn_dim(turns, dim)


def build_summary(rows, left_name, right_name):
    common = [row for row in rows if row.get("left_present") and row.get("right_present")]
    summary = {
        "common_cases": len(common),
        "left_cases": sum(1 for row in rows if row.get("left_present")),
        "right_cases": sum(1 for row in rows if row.get("right_present")),
        "left_average": average([row.get("left_final_score") for row in common]),
        "right_average": average([row.get("right_final_score") for row in common]),
        "average_delta": average([row.get("delta_final_score") for row in common]),
        "left_wins": sum(1 for row in common if row.get("winner") == left_name),
        "right_wins": sum(1 for row in common if row.get("winner") == right_name),
        "ties": sum(1 for row in common if row.get("winner") == "tie"),
    }
    summary["metric_deltas"] = {
        metric: average([row.get("delta_%s" % metric) for row in common])
        for metric in EPISODE_METRICS
    }
    return summary


def episode_metric_value(episode, scores, metric):
    return number(scores.get(metric))


def write_csv(path, rows):
    fields = [
        "case_id",
        "case_title",
        "case_type",
        "left_name",
        "right_name",
        "left_model_name",
        "right_model_name",
        "left_final_score",
        "right_final_score",
        "delta_final_score",
        "winner",
        "margin",
        "left_actual_turns",
        "right_actual_turns",
        "left_safety_fail",
        "right_safety_fail",
    ]
    for metric in EPISODE_METRICS:
        if metric != "final_score":
            fields.extend(["left_%s" % metric, "right_%s" % metric, "delta_%s" % metric])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_markdown(path, rows, summary, args):
    lines = []
    lines.append("# Model Comparison")
    lines.append("")
    lines.append("- left: `%s` from `%s`" % (args.left_name, args.left_dir))
    lines.append("- right: `%s` from `%s`" % (args.right_name, args.right_dir))
    lines.append("- common_cases: `%s`" % summary["common_cases"])
    lines.append("- average_final_score: `%s` `%0.1f` vs `%s` `%0.1f`" % (
        args.left_name, summary["left_average"], args.right_name, summary["right_average"],
    ))
    lines.append("- average_delta_left_minus_right: `%0.1f`" % summary["average_delta"])
    lines.append("- wins: `%s` %s, `%s` %s, ties %s" % (
        args.left_name, summary["left_wins"], args.right_name, summary["right_wins"], summary["ties"],
    ))
    lines.append("")

    lines.append("## Metric Deltas")
    lines.append("")
    lines.append("| metric | average_delta_left_minus_right |")
    lines.append("| --- | ---: |")
    for metric, delta in summary["metric_deltas"].items():
        lines.append("| %s | %0.1f |" % (md(metric), delta))
    lines.append("")

    lines.append("## Scores by Case")
    lines.append("")
    lines.append("| case_id | case_type | title | %s | %s | delta | winner |" % (md(args.left_name), md(args.right_name)))
    lines.append("| --- | --- | --- | ---: | ---: | ---: | --- |")
    for row in rows:
        lines.append("| %s | %s | %s | %0.1f | %0.1f | %+0.1f | %s |" % (
            md(row.get("case_id")),
            md(row.get("case_type")),
            md(row.get("case_title")),
            row.get("left_final_score", 0),
            row.get("right_final_score", 0),
            row.get("delta_final_score", 0),
            md(row.get("winner")),
        ))
    lines.append("")

    lines.append("## Turn Dimension Averages")
    lines.append("")
    lines.append("| case_id | dimension | %s | %s | delta |" % (md(args.left_name), md(args.right_name)))
    lines.append("| --- | --- | ---: | ---: | ---: |")
    for row in rows:
        if not (row.get("left_present") and row.get("right_present")):
            continue
        for dim in TURN_DIMS:
            left = row.get("left_avg_%s" % dim, 0)
            right = row.get("right_avg_%s" % dim, 0)
            lines.append("| %s | %s | %0.2f | %0.2f | %+0.2f |" % (
                md(row.get("case_id")), md(dim), left, right, left - right,
            ))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html(path, rows, summary, args):
    max_abs_delta = max([abs(number(row.get("delta_final_score"))) for row in rows] + [1])
    lines = []
    lines.append("<!doctype html><html><head><meta charset='utf-8'><title>Model Comparison</title>")
    lines.append("<style>")
    lines.append("body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#222;background:#fafafa}")
    lines.append("table{border-collapse:collapse;width:100%;background:white;margin:16px 0}")
    lines.append("th,td{border:1px solid #ddd;padding:8px;text-align:left;font-size:14px}")
    lines.append("th{background:#f0f3f6}.num{text-align:right}.bar{height:12px;background:#dbeafe;border-radius:3px}.neg{background:#fee2e2}.card{background:white;border:1px solid #ddd;padding:16px;margin:12px 0}")
    lines.append("</style></head><body>")
    lines.append("<h1>Model Comparison</h1>")
    lines.append("<div class='card'>")
    lines.append("<p><b>%s</b> vs <b>%s</b></p>" % (esc(args.left_name), esc(args.right_name)))
    lines.append("<p>Common cases: %s. Average final score: %0.1f vs %0.1f. Average delta: %+0.1f.</p>" % (
        summary["common_cases"], summary["left_average"], summary["right_average"], summary["average_delta"],
    ))
    lines.append("</div>")
    lines.append("<h2>Scores by Case</h2>")
    lines.append("<table><tr><th>case</th><th>type</th><th>title</th><th>%s</th><th>%s</th><th>delta</th><th>visual</th><th>winner</th></tr>" % (
        esc(args.left_name), esc(args.right_name),
    ))
    for row in rows:
        delta = number(row.get("delta_final_score"))
        width = int(min(100, abs(delta) / max_abs_delta * 100))
        klass = "bar" if delta >= 0 else "bar neg"
        lines.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class='num'>%0.1f</td><td class='num'>%0.1f</td><td class='num'>%+0.1f</td><td><div class='%s' style='width:%s%%'></div></td><td>%s</td></tr>" % (
            esc(row.get("case_id")),
            esc(row.get("case_type")),
            esc(row.get("case_title")),
            row.get("left_final_score", 0),
            row.get("right_final_score", 0),
            delta,
            klass,
            width,
            esc(row.get("winner")),
        ))
    lines.append("</table>")
    lines.append("<h2>Average Metric Deltas</h2><table><tr><th>metric</th><th>delta</th></tr>")
    for metric, delta in summary["metric_deltas"].items():
        lines.append("<tr><td>%s</td><td class='num'>%+0.1f</td></tr>" % (esc(metric), delta))
    lines.append("</table></body></html>")
    path.write_text("\n".join(lines), encoding="utf-8")


def case_title(report):
    if not report:
        return ""
    return report.get("case_title") or (report.get("case", {}) or {}).get("title", "")


def case_type(report):
    if not report:
        return ""
    return (report.get("case", {}) or {}).get("case_type", "")


def model_name(report):
    for turn in report.get("turns", []) or []:
        metadata = turn.get("assistant_metadata", {}) or {}
        if metadata.get("model_name"):
            return metadata.get("model_name")
    return ""


def safety_fail(report):
    episode = report.get("episode_evaluation", {}) or {}
    final_decision = report.get("final_flow_decision", {}) or {}
    return bool(final_decision.get("safety_fail") or episode.get("severe_safety_hard_fail") or episode.get("hard_fails"))


def average_turn_dim(turns, dim):
    values = []
    for turn in turns:
        item = (turn.get("judge_scores", {}) or {}).get(dim)
        if isinstance(item, dict):
            values.append(number(item.get("score")))
        elif isinstance(item, (int, float)):
            values.append(number(item))
    return average(values)


def winner(left, right, left_name, right_name):
    left = number(left)
    right = number(right)
    if abs(left - right) < 0.001:
        return "tie"
    return left_name if left > right else right_name


def average(values):
    nums = [number(value) for value in values]
    if not nums:
        return 0.0
    return round_number(sum(nums) / len(nums))


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def round_number(value):
    return round(number(value), 3)


def md(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def esc(value):
    return html.escape(str(value or ""))


if __name__ == "__main__":
    main()
