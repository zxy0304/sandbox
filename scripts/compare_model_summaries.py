#!/usr/bin/env python3
"""Compare two single-model summary.csv files from full sandbox runs.

Usage:
  python3 scripts/compare_model_summaries.py \
    --left-summary outputs/qwen_plus/summary.csv \
    --right-summary outputs/qwen_turbo/summary.csv \
    --left-name qwen-plus \
    --right-name qwen-turbo \
    --output-dir outputs/comparisons/qwen_plus_vs_turbo_summary
"""

import argparse
import csv
import html
from pathlib import Path


METRICS = [
    "final_score",
    "adaptation",
    "conversation_outcome",
]


def main():
    args = parse_args()
    left_rows = read_summary(Path(args.left_summary))
    right_rows = read_summary(Path(args.right_summary))
    rows = compare_rows(left_rows, right_rows, args.left_name, args.right_name)
    summary = summarize(rows, args.left_name, args.right_name)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "summary_comparison.csv", rows)
    write_markdown(output_dir / "summary_comparison.md", rows, summary, args)
    write_html(output_dir / "summary_comparison.html", rows, summary, args)

    print("summary comparison path:")
    print("  csv: %s" % (output_dir / "summary_comparison.csv"))
    print("  markdown: %s" % (output_dir / "summary_comparison.md"))
    print("  html: %s" % (output_dir / "summary_comparison.html"))


def parse_args():
    parser = argparse.ArgumentParser(description="Compare two model summary.csv files.")
    parser.add_argument("--left-summary", default="outputs/qwen_plus/summary.csv")
    parser.add_argument("--right-summary", default="outputs/qwen_turbo/summary.csv")
    parser.add_argument("--left-name", default="qwen-plus")
    parser.add_argument("--right-name", default="qwen-turbo")
    parser.add_argument("--output-dir", default="outputs/comparisons/qwen_plus_vs_turbo_summary")
    return parser.parse_args()


def read_summary(path):
    rows = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            case_id = row.get("case_id")
            if case_id:
                rows[case_id] = row
    return rows


def compare_rows(left_rows, right_rows, left_name, right_name):
    case_ids = sorted(set(left_rows) | set(right_rows))
    rows = []
    for case_id in case_ids:
        left = left_rows.get(case_id, {})
        right = right_rows.get(case_id, {})
        row = {
            "case_id": case_id,
            "left_present": bool(left),
            "right_present": bool(right),
            "left_name": left_name,
            "right_name": right_name,
            "left_actual_turns": number(left.get("actual_turns")),
            "right_actual_turns": number(right.get("actual_turns")),
            "left_safety_fail": truthy(left.get("safety_fail")),
            "right_safety_fail": truthy(right.get("safety_fail")),
        }
        for metric in METRICS:
            row["left_%s" % metric] = number(left.get(metric))
            row["right_%s" % metric] = number(right.get(metric))
            row["delta_%s" % metric] = round_number(row["left_%s" % metric] - row["right_%s" % metric])
        row["winner"] = winner(row["left_final_score"], row["right_final_score"], left_name, right_name)
        rows.append(row)
    return rows


def summarize(rows, left_name, right_name):
    common = [row for row in rows if row["left_present"] and row["right_present"]]
    summary = {
        "common_cases": len(common),
        "left_cases": sum(1 for row in rows if row["left_present"]),
        "right_cases": sum(1 for row in rows if row["right_present"]),
        "left_average_final_score": average([row["left_final_score"] for row in common]),
        "right_average_final_score": average([row["right_final_score"] for row in common]),
        "average_delta_final_score": average([row["delta_final_score"] for row in common]),
        "left_wins": sum(1 for row in common if row["winner"] == left_name),
        "right_wins": sum(1 for row in common if row["winner"] == right_name),
        "ties": sum(1 for row in common if row["winner"] == "tie"),
        "metric_deltas": {
            metric: average([row["delta_%s" % metric] for row in common])
            for metric in METRICS
        },
    }
    summary["largest_left_edges"] = sorted(
        common,
        key=lambda row: row["delta_final_score"],
        reverse=True,
    )[:5]
    summary["largest_right_edges"] = sorted(
        common,
        key=lambda row: row["delta_final_score"],
    )[:5]
    return summary


def write_csv(path, rows):
    fields = [
        "case_id",
        "left_present",
        "right_present",
        "left_name",
        "right_name",
        "left_final_score",
        "right_final_score",
        "delta_final_score",
        "winner",
        "left_actual_turns",
        "right_actual_turns",
        "left_safety_fail",
        "right_safety_fail",
    ]
    for metric in METRICS:
        if metric != "final_score":
            fields.extend(["left_%s" % metric, "right_%s" % metric, "delta_%s" % metric])

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_markdown(path, rows, summary, args):
    metric_rows = metric_average_rows([row for row in rows if row["left_present"] and row["right_present"]])
    lines = []
    lines.append("# Summary Comparison")
    lines.append("")
    lines.append("- left_summary: `%s`" % args.left_summary)
    lines.append("- right_summary: `%s`" % args.right_summary)
    lines.append("- common_cases: `%s`" % summary["common_cases"])
    lines.append("- average_final_score: `%s` `%0.1f` vs `%s` `%0.1f`" % (
        args.left_name,
        summary["left_average_final_score"],
        args.right_name,
        summary["right_average_final_score"],
    ))
    lines.append("- average_delta_left_minus_right: `%+0.1f`" % summary["average_delta_final_score"])
    lines.append("- wins: `%s` %s, `%s` %s, ties %s" % (
        args.left_name,
        summary["left_wins"],
        args.right_name,
        summary["right_wins"],
        summary["ties"],
    ))
    lines.append("")
    lines.append("## Average Metric Deltas")
    lines.append("")
    lines.append("| metric | %s | %s | delta_left_minus_right |" % (md(args.left_name), md(args.right_name)))
    lines.append("| --- | ---: | ---: | ---: |")
    for row in metric_rows:
        lines.append("| %s | %0.1f | %0.1f | %+0.1f |" % (
            md(row["metric"]),
            row["left"],
            row["right"],
            row["delta"],
        ))
    lines.append("")
    lines.append("## Largest Margins")
    lines.append("")
    lines.append("| case_id | delta_left_minus_right | winner |")
    lines.append("| --- | ---: | --- |")
    for row in sorted(rows, key=lambda item: abs(item["delta_final_score"]), reverse=True)[:10]:
        lines.append("| %s | %+0.1f | %s |" % (
            md(row["case_id"]),
            row["delta_final_score"],
            md(row["winner"]),
        ))
    lines.append("")
    lines.append("## Case Results")
    lines.append("")
    lines.append("| case_id | %s | %s | delta | winner |" % (md(args.left_name), md(args.right_name)))
    lines.append("| --- | ---: | ---: | ---: | --- |")
    for row in rows:
        lines.append("| %s | %0.1f | %0.1f | %+0.1f | %s |" % (
            md(row["case_id"]),
            row["left_final_score"],
            row["right_final_score"],
            row["delta_final_score"],
            md(row["winner"]),
        ))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html(path, rows, summary, args):
    common = [row for row in rows if row["left_present"] and row["right_present"]]
    metric_rows = metric_average_rows(common)
    lines = []
    lines.append("<!doctype html><html><head><meta charset='utf-8'><title>Summary Comparison</title>")
    lines.append("<style>%s</style>" % dashboard_css())
    lines.append("</head><body><h1>Summary Comparison</h1>")
    lines.append("<p class='subtle'>Left summary: <code>%s</code><br>Right summary: <code>%s</code></p>" % (
        esc(args.left_summary),
        esc(args.right_summary),
    ))
    lines.append("<section class='kpis'>")
    lines.append(kpi("Common cases", summary["common_cases"]))
    lines.append(kpi(args.left_name + " avg", "%0.1f" % summary["left_average_final_score"]))
    lines.append(kpi(args.right_name + " avg", "%0.1f" % summary["right_average_final_score"]))
    lines.append(kpi("Delta", "%+0.1f" % summary["average_delta_final_score"]))
    lines.append(kpi("Wins", "%s / %s / %s" % (summary["left_wins"], summary["right_wins"], summary["ties"]), "%s / %s / tie" % (args.left_name, args.right_name)))
    lines.append("</section>")

    lines.append("<section class='grid two'>")
    lines.append("<div class='panel'><h2>Average Metric Radar</h2>%s</div>" % radar_svg(metric_rows, args.left_name, args.right_name))
    lines.append("<div class='panel'><h2>Final Score by Case</h2>%s</div>" % line_chart_svg(common, args.left_name, args.right_name))
    lines.append("</section>")

    lines.append("<section class='grid two'>")
    lines.append("<div class='panel'><h2>Average Metric Deltas</h2>%s</div>" % metric_delta_bars(metric_rows, args.left_name, args.right_name))
    lines.append("<div class='panel'><h2>Largest Case Margins</h2>%s</div>" % largest_margins_table(summary, args.left_name, args.right_name))
    lines.append("</section>")

    lines.append("<section class='panel'><h2>Case Results</h2>%s</section>" % case_results_table(rows, args.left_name, args.right_name))
    lines.append("<section class='panel'><h2>Case x Metric Delta Heatmap</h2><p class='subtle'>Positive means %s is higher; negative means %s is higher.</p>%s</section>" % (
        esc(args.left_name),
        esc(args.right_name),
        heatmap_table(common),
    ))
    lines.append("</body></html>")
    path.write_text("\n".join(lines), encoding="utf-8")


def metric_average_rows(rows):
    result = []
    for metric in METRICS:
        left = average([row["left_%s" % metric] for row in rows])
        right = average([row["right_%s" % metric] for row in rows])
        result.append({
            "metric": metric,
            "left": left,
            "right": right,
            "delta": round_number(left - right),
        })
    return result


def dashboard_css():
    return """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;background:#f7f7f5;color:#202124}
h1{margin-bottom:4px}h2{font-size:18px;margin:0 0 12px}.subtle{color:#666;font-size:13px}
code{background:#eee;padding:1px 4px;border-radius:4px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:18px 0}
.kpi,.panel{background:white;border:1px solid #ddd;border-radius:8px;padding:14px}.kpi .label{color:#666;font-size:12px}.kpi .value{font-size:26px;font-weight:650;margin-top:4px}.kpi .hint{color:#777;font-size:12px}
.grid{display:grid;gap:16px;margin:16px 0}.two{grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}
table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #ddd;padding:7px 8px;font-size:13px}th{background:#f0f1f2;text-align:left}.num{text-align:right;font-variant-numeric:tabular-nums}
.barwrap{height:13px;background:#eee;border-radius:4px;overflow:hidden}.barpos{height:100%;background:#3b82f6}.barneg{height:100%;background:#ef4444}
.tag{display:inline-block;border-radius:999px;padding:2px 8px;font-size:12px;background:#eee}.left{color:#1d4ed8}.right{color:#b91c1c}.tie{color:#666}
.heat-pos{background:#dbeafe}.heat-neg{background:#fee2e2}.heat-mid{background:#f8fafc}
svg text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
"""


def kpi(label, value, hint=""):
    return "<div class='kpi'><div class='label'>%s</div><div class='value'>%s</div><div class='hint'>%s</div></div>" % (
        esc(label),
        esc(value),
        esc(hint),
    )


def radar_svg(metric_rows, left_name, right_name):
    metrics = [row for row in metric_rows if row["metric"] != "final_score"]
    if not metrics:
        return "<p class='subtle'>No common metrics.</p>"
    size = 360
    center = size / 2
    radius = 125
    rings = []
    for scale in [0.25, 0.5, 0.75, 1.0]:
        rings.append("<circle cx='%s' cy='%s' r='%s' fill='none' stroke='#ddd'/>" % (center, center, radius * scale))
    axes = []
    left_points = []
    right_points = []
    for index, row in enumerate(metrics):
        angle = -3.14159 / 2 + 2 * 3.14159 * index / len(metrics)
        x = center + radius * cos(angle)
        y = center + radius * sin(angle)
        label_x = center + (radius + 24) * cos(angle)
        label_y = center + (radius + 24) * sin(angle)
        axes.append("<line x1='%s' y1='%s' x2='%s' y2='%s' stroke='#e5e7eb'/>" % (center, center, x, y))
        axes.append("<text x='%s' y='%s' font-size='10' text-anchor='middle'>%s</text>" % (label_x, label_y, esc(short_metric(row["metric"]))))
        left_points.append(point_on_axis(center, radius, angle, row["left"]))
        right_points.append(point_on_axis(center, radius, angle, row["right"]))
    return """
<svg viewBox='0 0 360 360' width='100%%' height='360' role='img'>
%s%s
<polygon points='%s' fill='rgba(37,99,235,.22)' stroke='#2563eb' stroke-width='2'/>
<polygon points='%s' fill='rgba(220,38,38,.16)' stroke='#dc2626' stroke-width='2'/>
<text x='18' y='26' font-size='12' fill='#2563eb'>%s</text>
<text x='18' y='44' font-size='12' fill='#dc2626'>%s</text>
</svg>
""" % ("".join(rings), "".join(axes), " ".join(left_points), " ".join(right_points), esc(left_name), esc(right_name))


def point_on_axis(center, radius, angle, value):
    distance = radius * max(0, min(100, number(value))) / 100.0
    return "%0.1f,%0.1f" % (center + distance * cos(angle), center + distance * sin(angle))


def line_chart_svg(rows, left_name, right_name):
    if not rows:
        return "<p class='subtle'>No common cases.</p>"
    width = 720
    height = 300
    pad_l = 48
    pad_r = 20
    pad_t = 24
    pad_b = 72
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    step = plot_w / max(1, len(rows) - 1)
    left_points = []
    right_points = []
    labels = []
    for i, row in enumerate(rows):
        x = pad_l + i * step
        left_y = pad_t + plot_h * (5 - row["left_final_score"]) / 4
        right_y = pad_t + plot_h * (5 - row["right_final_score"]) / 4
        left_points.append("%0.1f,%0.1f" % (x, left_y))
        right_points.append("%0.1f,%0.1f" % (x, right_y))
        labels.append("<text x='%0.1f' y='%s' font-size='11' text-anchor='end' transform='rotate(-35 %0.1f %s)'>%s</text>" % (
            x, height - 28, x, height - 28, esc(row["case_id"]),
        ))
    grid = []
    for score in [0, 25, 50, 75, 100]:
        y = pad_t + plot_h * (100 - score) / 100
        grid.append("<line x1='%s' y1='%s' x2='%s' y2='%s' stroke='#e5e7eb'/><text x='8' y='%s' font-size='11'>%s</text>" % (pad_l, y, width - pad_r, y, y + 4, score))
    return """
<svg viewBox='0 0 720 300' width='100%%' height='300' role='img'>
%s
<polyline points='%s' fill='none' stroke='#2563eb' stroke-width='2.5'/>
<polyline points='%s' fill='none' stroke='#dc2626' stroke-width='2.5'/>
%s
<text x='58' y='18' font-size='12' fill='#2563eb'>%s</text>
<text x='150' y='18' font-size='12' fill='#dc2626'>%s</text>
</svg>
""" % ("".join(grid), " ".join(left_points), " ".join(right_points), "".join(labels), esc(left_name), esc(right_name))


def metric_delta_bars(metric_rows, left_name, right_name):
    max_delta = max([abs(row["delta"]) for row in metric_rows] + [1])
    lines = ["<table><tr><th>metric</th><th class='num'>%s</th><th class='num'>%s</th><th class='num'>delta</th><th>visual</th></tr>" % (esc(left_name), esc(right_name))]
    for row in sorted(metric_rows, key=lambda item: abs(item["delta"]), reverse=True):
        width = int(abs(row["delta"]) / max_delta * 100)
        klass = "barpos" if row["delta"] >= 0 else "barneg"
        lines.append("<tr><td>%s</td><td class='num'>%0.1f</td><td class='num'>%0.1f</td><td class='num'>%+0.1f</td><td><div class='barwrap'><div class='%s' style='width:%s%%'></div></div></td></tr>" % (
            esc(row["metric"]), row["left"], row["right"], row["delta"], klass, width,
        ))
    lines.append("</table>")
    return "".join(lines)


def largest_margins_table(summary, left_name, right_name):
    lines = ["<table><tr><th>direction</th><th>case_id</th><th class='num'>delta</th><th>winner</th></tr>"]
    for row in summary.get("largest_left_edges", []):
        lines.append("<tr><td>%s edge</td><td>%s</td><td class='num'>%+0.1f</td><td><span class='tag left'>%s</span></td></tr>" % (
            esc(left_name), esc(row["case_id"]), row["delta_final_score"], esc(left_name),
        ))
    for row in summary.get("largest_right_edges", []):
        if row["delta_final_score"] >= 0:
            continue
        lines.append("<tr><td>%s edge</td><td>%s</td><td class='num'>%+0.1f</td><td><span class='tag right'>%s</span></td></tr>" % (
            esc(right_name), esc(row["case_id"]), row["delta_final_score"], esc(right_name),
        ))
    lines.append("</table>")
    return "".join(lines)


def case_results_table(rows, left_name, right_name):
    max_delta = max([abs(row["delta_final_score"]) for row in rows] + [1])
    lines = ["<table><tr><th>case_id</th><th class='num'>%s</th><th class='num'>%s</th><th class='num'>delta</th><th>visual</th><th>winner</th><th class='num'>turns</th><th>safety</th></tr>" % (esc(left_name), esc(right_name))]
    for row in rows:
        delta = row["delta_final_score"]
        width = int(abs(delta) / max_delta * 100)
        klass = "barpos" if delta >= 0 else "barneg"
        winner_class = "tie" if row["winner"] == "tie" else ("left" if row["winner"] == left_name else "right")
        safety = []
        if row["left_safety_fail"]:
            safety.append(left_name)
        if row["right_safety_fail"]:
            safety.append(right_name)
        lines.append("<tr><td>%s</td><td class='num'>%0.1f</td><td class='num'>%0.1f</td><td class='num'>%+0.1f</td><td><div class='barwrap'><div class='%s' style='width:%s%%'></div></div></td><td><span class='tag %s'>%s</span></td><td class='num'>%s/%s</td><td>%s</td></tr>" % (
            esc(row["case_id"]),
            row["left_final_score"],
            row["right_final_score"],
            delta,
            klass,
            width,
            winner_class,
            esc(row["winner"]),
            int(row["left_actual_turns"]),
            int(row["right_actual_turns"]),
            esc(", ".join(safety) if safety else "none"),
        ))
    lines.append("</table>")
    return "".join(lines)


def heatmap_table(rows):
    if not rows:
        return "<p class='subtle'>No common cases.</p>"
    metrics = [metric for metric in METRICS if metric != "final_score"]
    max_abs = max([abs(row["delta_%s" % metric]) for row in rows for metric in metrics] + [1])
    lines = ["<div style='overflow:auto'><table><tr><th>case_id</th>"]
    for metric in metrics:
        lines.append("<th title='%s'>%s</th>" % (esc(metric), esc(short_metric(metric))))
    lines.append("</tr>")
    for row in rows:
        lines.append("<tr><td>%s</td>" % esc(row["case_id"]))
        for metric in metrics:
            delta = row["delta_%s" % metric]
            intensity = abs(delta) / max_abs
            klass = "heat-mid"
            if delta > 0.01:
                klass = "heat-pos"
            elif delta < -0.01:
                klass = "heat-neg"
            alpha = 0.25 + 0.65 * intensity
            style = "opacity:%0.2f" % alpha
            lines.append("<td class='num %s' style='%s'>%+0.1f</td>" % (klass, style, delta))
        lines.append("</tr>")
    lines.append("</table></div>")
    return "".join(lines)


def short_metric(metric):
    aliases = {
        "adaptation": "adaptation",
        "conversation_outcome": "outcome",
        "final_score": "final",
    }
    return aliases.get(metric, metric)


def sin(value):
    # Small wrapper avoids adding a heavy dependency while keeping imports local.
    import math
    return math.sin(value)


def cos(value):
    import math
    return math.cos(value)


def winner(left, right, left_name, right_name):
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
    return round(float(value), 3)


def truthy(value):
    return str(value).strip().lower() in ["1", "true", "yes", "y"]


def md(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def esc(value):
    return html.escape(str(value or ""))


if __name__ == "__main__":
    main()
