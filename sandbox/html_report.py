"""Self-contained HTML visualization for sandbox reports."""

import html
import json
from pathlib import Path


STATE_LABELS = [
    ("valence", "情绪"), ("clarity", "清晰"), ("engagement", "投入"),
    ("trust", "信任"), ("comfort", "舒适"), ("agency", "自主"),
    ("connection", "连接"), ("task_progress", "进展"),
]

DIMENSION_LABELS = {
    "emotional_attunement": "情绪贴合", "contextual_grounding": "具体倾听",
    "conversation_fit": "对话适配", "continuation_affordance": "接续能力",
    "spoken_immediacy": "口语即时感", "scene_tone_fit": "场景语气",
    "repetition_burden": "避免复述", "template_variation": "表达变化",
}


def esc(value):
    return html.escape(str(value if value is not None else "")).replace("\n", "<br>")


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def score_class(value):
    value = number(value)
    return "good" if value >= 4 else "mid" if value >= 3 else "low"


def state_chart(report, width=920, height=300):
    turns = report.get("turns", [])
    initial = report.get("initial_state", {})
    points = [(0, initial)] + [(int(t.get("turn_id", i + 1)), t.get("state_after", {})) for i, t in enumerate(turns)]
    if not points:
        return ""
    left, right, top, bottom = 48, 20, 20, 38
    colors = ["#e6578c", "#536dfe", "#e99335", "#11a683", "#8b5cf6", "#0891b2", "#e85d04", "#64748b"]
    max_turn = max(p[0] for p in points) or 1

    def xy(turn, value):
        x = left + turn / max_turn * (width - left - right)
        y = top + (5 - number(value)) / 5 * (height - top - bottom)
        return x, y

    parts = ['<svg viewBox="0 0 %s %s" role="img" aria-label="user state trajectory">' % (width, height)]
    for value in range(6):
        _, y = xy(0, value)
        parts.append('<line class="grid" x1="%s" y1="%.1f" x2="%s" y2="%.1f"/><text class="axis" x="18" y="%.1f">%s</text>' % (left, y, width-right, y, y+4, value))
    for turn, _state in points:
        x, _ = xy(turn, 0)
        parts.append('<text class="axis" x="%.1f" y="%s" text-anchor="middle">T%s</text>' % (x, height-10, turn))
    for index, (key, label) in enumerate(STATE_LABELS):
        coords = " ".join("%.1f,%.1f" % xy(turn, state.get(key, 0)) for turn, state in points)
        color = colors[index]
        parts.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.5"><title>%s</title></polyline>' % (coords, color, esc(label)))
        for turn, state in points:
            x, y = xy(turn, state.get(key, 0))
            parts.append('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"><title>%s T%s: %.2f</title></circle>' % (x, y, color, esc(label), turn, number(state.get(key))))
    parts.append("</svg>")
    legend = "".join('<span><i style="background:%s"></i>%s</span>' % (colors[i], esc(label)) for i, (_key, label) in enumerate(STATE_LABELS))
    return '<div class="legend">%s</div>%s' % (legend, "".join(parts))


def dimension_bars(scores):
    dimensions = scores.get("subdimensions", {}) if isinstance(scores, dict) else {}
    labels = [
        ("emotional_attunement", "情绪贴合"), ("contextual_grounding", "具体倾听"),
        ("conversation_fit", "对话适配"), ("continuation_affordance", "接续能力"),
        ("spoken_immediacy", "口语即时感"), ("scene_tone_fit", "场景语气"),
        ("repetition_burden", "避免复述"), ("template_variation", "表达变化"),
    ]
    rows = []
    for key, label in labels:
        value = number(dimensions.get(key), 0)
        rows.append('<div class="barrow"><span>%s</span><div class="track"><i style="width:%.1f%%"></i></div><b>%.2f</b></div>' % (esc(label), max(0, min(100, value / 5 * 100)), value))
    return "".join(rows)


def evidence_html(scores):
    evidence = scores.get("evidence", {}) if isinstance(scores, dict) else {}
    limitations = scores.get("limitations", {}) if isinstance(scores, dict) else {}
    chunks = []
    identity_claims = scores.get("identity_claims", []) if isinstance(scores, dict) else []
    if identity_claims:
        chunks.append("<h4>身份呈现诊断（不计分）</h4><ul>%s</ul>" % "".join(
            "<li>%s</li>" % esc(item) for item in identity_claims
        ))
    for title, source in [("评分证据", evidence), ("局限与问题", limitations)]:
        values = []
        if isinstance(source, dict):
            for group, items in source.items():
                for item in items if isinstance(items, list) else [items]:
                    if item:
                        values.append("%s：%s" % (group, item))
        if values:
            chunks.append("<h4>%s</h4><ul>%s</ul>" % (title, "".join("<li>%s</li>" % esc(v) for v in values)))
    checks = scores.get("dimension_checks", {}) if isinstance(scores, dict) else {}
    rows = []
    if isinstance(checks, dict):
        for judge, dimensions in checks.items():
            if not isinstance(dimensions, dict):
                continue
            for key, raw in dimensions.items():
                for item in raw if isinstance(raw, list) else [raw]:
                    if not isinstance(item, dict):
                        continue
                    rating, reason = item.get("rating"), item.get("evidence", "")
                    if rating is None and not reason:
                        continue
                    rows.append("<tr><td>%s</td><td>%s</td><td><b>%s</b></td><td>%s</td><td>%s</td></tr>" % (
                        esc("共情" if judge == "empathy" else "自然度" if judge == "naturalness" else judge),
                        esc(DIMENSION_LABELS.get(key, key)),
                        esc("—" if rating is None else "%.2f" % number(rating)),
                        esc(item.get("result", "")), esc(reason),
                    ))
    if rows:
        chunks.append("<h4>逐维度评分原因</h4><div class=tablewrap><table><thead><tr><th>评审</th><th>维度</th><th>分数</th><th>档位</th><th>证据与诊断</th></tr></thead><tbody>%s</tbody></table></div>" % "".join(rows))
    return "".join(chunks) or "<p class=muted>本轮没有评分证据。</p>"


def episode_diagnostics_html(evaluation):
    """Render whole-dialogue evidence and limitations from both judges."""
    chunks = []
    for title, field in [("整段评分证据", "evidence"), ("整段局限与问题", "notes")]:
        source = evaluation.get(field, {}) if isinstance(evaluation, dict) else {}
        groups = []
        if isinstance(source, dict):
            for judge, items in source.items():
                values = [item for item in (items if isinstance(items, list) else [items]) if item]
                if values:
                    label = "共情评审" if judge == "empathy" else "自然度评审" if judge == "naturalness" else judge
                    groups.append("<div class=diagnostic><h4>%s</h4><ul>%s</ul></div>" % (
                        esc(label), "".join("<li>%s</li>" % esc(item) for item in values)
                    ))
        if groups:
            chunks.append("<h3>%s</h3><div class=diagnosticgrid>%s</div>" % (title, "".join(groups)))
    return "".join(chunks) or "<p class=muted>整段评审未返回证据。</p>"


def turn_html(turn):
    scores = turn.get("judge_scores", {})
    private = turn.get("user_private_state", {})
    intent = private.get("intent", {})
    total = scores.get("total_score", scores.get("overall"))
    badge = '<span class="score %s">%s</span>' % (score_class(total), "未评分" if total is None else "%.2f / 5" % number(total))
    safety = scores.get("safety_gate", {}) if isinstance(scores, dict) else {}
    safety_note = '<span class="danger">安全 Gate：%s</span>' % esc(safety.get("severity")) if safety.get("triggered") else ""
    metadata = turn.get("assistant_metadata", {})
    return """
    <section class="turn">
      <div class="turnhead"><h3>第 %s 轮</h3><div>%s %s</div></div>
      <div class="bubble user"><small>模拟用户</small>%s</div>
      <div class="bubble agent"><small>Companion · %s</small>%s</div>
      <details><summary>用户内心与意图</summary><p><b>反应：</b>%s</p><p><b>意图：</b>%s</p><p><b>状态变化：</b>%s</p></details>
      <details><summary>本轮维度分数</summary><div class="bars">%s</div></details>
      <details open><summary>评分证据与诊断</summary>%s</details>
    </section>
    """ % (
        esc(turn.get("turn_id")), badge, safety_note,
        esc(turn.get("user_message")), esc(metadata.get("model_name", "configured model")), esc(turn.get("assistant_message")),
        esc(private.get("inner_reaction", "")), esc(intent), esc(turn.get("state_delta", {})),
        dimension_bars(scores), evidence_html(scores),
    )


def build_case_html(report):
    evaluation = report.get("episode_evaluation", {})
    episode_scores = evaluation.get("episode_scores", {})
    final = evaluation.get("final_score")
    status = evaluation.get("status", "completed")
    summary = report.get("episode_summary", {})
    stack = report.get("agent_stack", {})
    turns = "".join(turn_html(turn) for turn in report.get("turns", []))
    safety = evaluation.get("episode_safety_gate", {})
    safety_text = "未触发" if not safety.get("triggered") else "%s（已封顶：%s）" % (safety.get("severity"), evaluation.get("safety_cap_applied"))
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>%s · Companion 评测</title>
<style>
:root{--ink:#252b42;--muted:#6d7485;--paper:#f4f6f9;--card:#fff;--line:#dfe4ec;--green:#0f8a68;--pink:#d74f7b}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;line-height:1.65}main{max-width:1080px;margin:auto;padding:34px 22px 80px}.hero,.panel,.turn{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;margin:18px 0;box-shadow:0 7px 24px #27304a0b}h1{font-size:34px;margin:4px 0}h2{margin-top:42px}.muted,small{color:var(--muted)}.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:22px}.stat{background:#f5f7fb;border-radius:13px;padding:15px}.stat b{font-size:23px;display:block}.turnhead{display:flex;justify-content:space-between;align-items:center}.turnhead h3{margin:0}.bubble{padding:16px 18px;border-radius:14px;margin:13px 0}.bubble small{display:block;font-weight:700;margin-bottom:5px}.user{background:#f0f2f7;margin-right:9%%}.agent{background:#e8f7f2;margin-left:9%%}.score{border-radius:999px;padding:5px 10px;font-weight:750}.good{background:#dff5ec;color:#08795c}.mid{background:#fff1ca;color:#8a5a00}.low{background:#ffe3ea;color:#aa294f}.danger{color:#b42345;font-weight:700;margin-left:8px}details{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}summary{cursor:pointer;font-weight:700}.barrow{display:grid;grid-template-columns:120px 1fr 44px;gap:9px;align-items:center;margin:8px 0;font-size:13px}.track{height:10px;background:#e8ebf1;border-radius:8px;overflow:hidden}.track i{display:block;height:100%%;background:#536dfe;border-radius:8px}.legend{display:flex;flex-wrap:wrap;gap:13px;margin-bottom:8px}.legend i{display:inline-block;width:9px;height:9px;border-radius:50%%;margin-right:5px}.grid{stroke:#e8ebf1}.axis{font-size:11px;fill:#7a8293}svg{width:100%%;height:auto}ul{padding-left:22px}.tablewrap{overflow-x:auto}table{width:100%%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:9px 10px}th{background:#f5f7fb;white-space:nowrap}.diagnosticgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.diagnostic{background:#f7f8fb;border-radius:13px;padding:12px 16px}.diagnostic h4{margin:0 0 6px}@media(max-width:760px){.stats,.diagnosticgrid{grid-template-columns:1fr}.bubble{margin-left:0;margin-right:0}.barrow{grid-template-columns:95px 1fr 40px}}
</style></head><body><main>
<section class="hero"><div class="muted">Daily Companion Sandbox · Visual Evaluation Report</div><h1>%s</h1><p>%s</p><div class="muted">被测：%s · 用户/评测：%s · %s 轮 · 终止：%s</div>
<div class="stats"><div class="stat">最终分<b>%s</b></div><div class="stat">共情分<b>%.2f</b></div><div class="stat">自然度<b>%.2f</b></div><div class="stat">安全 Gate<b style="font-size:16px">%s</b></div><div class="stat">状态<b style="font-size:16px">%s</b></div></div></section>
<section class="panel"><h2>用户状态轨迹</h2><p class="muted">0–5 分；轨迹来自用户模拟器的主观反应，不是 Evaluator 分数。</p>%s</section>
<section class="panel"><h2>整段评分与诊断</h2>%s</section>
<h2>逐轮对话与评测证据</h2>%s
<section class="panel"><h2>阅读边界</h2><p>本报告用于对照单个 Case 下的对话过程。分数范围为 1–5；安全 Gate 可对最终分封顶。不同 Evaluator 版本的历史结果不应直接混合比较。</p></section>
</main></body></html>""" % (
        esc(report.get("case_id")), esc(report.get("case_title", report.get("case_id"))),
        esc(report.get("case", {}).get("S", {}).get("scene", "")), esc(stack.get("companion_agent", "AneAgent")),
        esc(stack.get("infrastructure_model", stack.get("user_thinker", "DeepSeek Pro"))), summary.get("turn_count", len(report.get("turns", []))), esc(summary.get("stop_reason")),
        "未评分" if final is None else "%.2f / 5" % number(final), number(episode_scores.get("empathy_score")),
        number(episode_scores.get("human_score")), esc(safety_text), esc(status), state_chart(report), episode_diagnostics_html(evaluation), turns,
    )


class HtmlReportWriter:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)

    def write_case(self, report):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / ("report_%s.html" % report.get("case_id", "unknown"))
        path.write_text(build_case_html(report), encoding="utf-8")
        return path

    def write_index(self, batch_items):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        cards = []
        for item in batch_items:
            report = item.get("report", {})
            evaluation = report.get("episode_evaluation", {})
            score = evaluation.get("final_score")
            case_id = report.get("case_id", "unknown")
            cards.append('<a class="card" href="report_%s.html"><small>%s</small><h2>%s</h2><b class="%s">%s</b><p>%s 轮 · %s</p></a>' % (
                esc(case_id), esc(case_id), esc(report.get("case_title", "")), score_class(score),
                "未评分" if score is None else "%.2f / 5" % number(score),
                report.get("episode_summary", {}).get("turn_count", 0), esc(report.get("episode_summary", {}).get("stop_reason", "")),
            ))
        model_names = []
        for item in batch_items:
            report = item.get("report", {})
            name = report.get("agent_name") or report.get("agent_stack", {}).get("companion_agent")
            if name and name not in model_names:
                model_names.append(str(name))
        heading = " / ".join(model_names) if model_names else self.output_dir.name
        empty = "" if cards else "<p>暂无已完成 Case；运行后会自动出现在这里。</p>"
        page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>%s 评测总览</title><style>body{margin:0;background:#f4f6f9;color:#252b42;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}main{max-width:1050px;margin:auto;padding:40px 22px}h1{font-size:36px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.card{display:block;background:white;border:1px solid #dfe4ec;border-radius:17px;padding:20px;text-decoration:none;color:inherit;box-shadow:0 6px 22px #27304a0b}.card:hover{transform:translateY(-2px)}small,p{color:#6d7485}.card b{font-size:24px}.good{color:#08795c}.mid{color:#9b6500}.low{color:#b42345}@media(max-width:760px){.grid{grid-template-columns:1fr}}</style></head><body><main><small>Daily Companion Sandbox</small><h1>%s</h1><p>点击 Case 查看逐轮对话、用户状态轨迹、分项评分与证据。</p>%s<div class="grid">%s</div></main></body></html>""" % (esc(heading), esc(heading), empty, "".join(cards))
        path = self.output_dir / "index.html"
        path.write_text(page, encoding="utf-8")
        return path

    def write_directory(self):
        """Regenerate case HTML and an index from every JSON report in a directory."""
        items = []
        for path in sorted(self.output_dir.glob("report_*.json")):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(report, dict) or not report.get("case_id"):
                continue
            self.write_case(report)
            items.append({"report": report})
        return self.write_index(items)
