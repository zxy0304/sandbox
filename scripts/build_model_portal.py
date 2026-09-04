#!/usr/bin/env python3
"""Build a top-level HTML portal over three model result directories."""

import argparse
import html
from pathlib import Path


MODELS = [
    ("ane_agent", "AneAgent", "AneAgent wrapper + configured companion model"),
    ("qwen3_7_plus", "Qwen 3.7 Plus", "Direct DashScope API baseline"),
    ("deepseek_v4_flash", "DeepSeek V4 Flash", "Direct DeepSeek API baseline"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="outputs/model_comparison")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cards = []
    for folder, label, description in MODELS:
        model_dir = root / folder
        model_dir.mkdir(parents=True, exist_ok=True)
        report_count = len(list(model_dir.glob("report_*.json")))
        cards.append(
            '<a class="card" href="%s/index.html"><small>%s</small><h2>%s</h2><b>%s cases</b><p>%s</p></a>'
            % tuple(html.escape(str(value)) for value in [folder, folder, label, report_count, description])
        )
    page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Model Comparison</title><style>body{margin:0;background:#f4f6f9;color:#252b42;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}main{max-width:1050px;margin:auto;padding:46px 22px}h1{font-size:40px;margin-bottom:8px}.lead,small,p{color:#6d7485}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:30px}.card{display:block;background:#fff;border:1px solid #dfe4ec;border-radius:19px;padding:24px;text-decoration:none;color:inherit;box-shadow:0 7px 24px #27304a0b}.card:hover{transform:translateY(-3px)}.card b{font-size:22px;color:#0f8a68}@media(max-width:760px){.grid{grid-template-columns:1fr}}</style></head><body><main><small>Daily Companion Sandbox</small><h1>Model Comparison</h1><p class="lead">选择被测模型，再进入各自的 Case 报告。</p><div class="grid">%s</div></main></body></html>""" % "".join(cards)
    path = root / "index.html"
    path.write_text(page, encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
