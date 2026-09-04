"""命令行入口。

这个文件负责把用户的 CLI 参数翻译成沙盒运行：加载配置和 case，
构建全套 agent，运行单 case 或批量评测，并把 JSON/Markdown/CSV 报告写出。
"""

import argparse
import copy
import csv
import random
import sys
from datetime import datetime
from pathlib import Path


if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox.agents.llm_companion_agent import LLMCompanionAgent
from sandbox.agents.dual_batch_evaluator_agent import DualBatchEvaluatorAgent
from sandbox.agents.llm_user_talker import LLMUserTalker
from sandbox.agents.llm_user_thinker import LLMUserThinker
from sandbox.agents.manual_companion_agent import ManualCompanionAgent
from sandbox.agents.tts_agent import TTSAgent
from sandbox.agents.audio_evaluator_agent import AudioEvaluatorAgent
from sandbox.agents.audio_delivery_planner_agent import AudioDeliveryPlannerAgent
from sandbox.case_loader import list_case_files, load_all_cases, load_case, project_root
from sandbox.dialogue_runner import DialogueRunner
from sandbox.html_report import HtmlReportWriter
from sandbox.report_writer import ReportWriter
from sandbox.utils.json_utils import load_json
from sandbox.utils.yaml_utils import load_yaml


def load_config(config_path=None):
    """Load runtime configuration from an explicit path or configs/default.yaml."""
    if config_path:
        path = Path(config_path)
    else:
        path = project_root() / "configs" / "default.yaml"
    config = load_yaml(path)
    if not isinstance(config, dict):
        return {}
    return config


def parse_args():
    """声明并解析 CLI 参数。

    参数只做语法层面的解析；需要访问文件系统或验证 case 内容的逻辑放在
    后续 load/apply/build 函数里，方便单独测试。
    """
    parser = argparse.ArgumentParser(description="Run the daily companion agent sandbox V2.")
    parser.add_argument("--case", default=None, help="Run one case YAML path or case id.")
    parser.add_argument("--case_dir", "--case-dir", dest="case_dir", default=None, help="Run every YAML case in a directory.")
    parser.add_argument("--all", action="store_true", help="Run all cases in the default case directory.")
    parser.add_argument("--list-cases", action="store_true", help="List available cases.")
    parser.add_argument("--config", default=None, help="Path to config YAML.")
    parser.add_argument("--output_dir", "--output-dir", dest="output_dir", default=None, help="Directory for JSON and Markdown reports.")
    parser.add_argument("--max_turns", "--max-turns", dest="max_turns", type=positive_int, default=None, help="Override episode max turns.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility where providers support it.")
    parser.add_argument("--companion", choices=["llm", "manual"], default="llm", help="Companion mode for a single run.")
    parser.add_argument("--agents", nargs="+", default=None, help="Companion agent names for batch evaluation: llm or manual.")
    parser.add_argument("--no-evaluator", action="store_true", help="Skip turn and episode evaluation calls.")
    parser.add_argument(
        "--evaluate-report",
        default=None,
        help="Evaluate the dialogue history in an existing JSON report; skips simulation, companion, TTS, and audio evaluation.",
    )
    return parser.parse_args()


def positive_int(value):
    """Validate that a CLI integer option is at least one before it is used as a turn limit."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer")
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def apply_overrides(config, args):
    """Apply CLI overrides to config for max turns, output directory, and seed without mutating unrelated settings."""
    if args.max_turns is not None:
        config.setdefault("episode", {})
        config["episode"]["max_turns"] = args.max_turns
        config["episode"]["override_case_max_turns"] = True
    if args.output_dir:
        config.setdefault("reports", {})
        config["reports"]["outputs_dir"] = args.output_dir
    if args.seed is not None:
        config.setdefault("runtime", {})
        config["runtime"]["random_seed"] = args.seed
    if args.no_evaluator:
        config.setdefault("evaluation", {})
        config["evaluation"]["enabled"] = False
    return config


def print_cases(cases_dir=None):
    """Print the case YAML files available in the requested directory."""
    files = list_case_files(cases_dir)
    if not files:
        print("No cases found.")
        return
    for path in files:
        print(str(path))


def run():
    """执行完整 CLI 工作流并返回进程退出码。

    这里是 main 的编排层：先处理 list-cases，再加载配置/case，
    然后根据是否传入 --agents 选择单次运行或批量评测。
    """
    args = parse_args()

    if args.list_cases:
        print_cases(args.case_dir)
        return 0

    config = apply_overrides(load_config(args.config), args)
    if args.seed is not None:
        random.seed(args.seed)

    if args.evaluate_report:
        return run_report_evaluation(config, args.evaluate_report, explicit_output_dir=bool(args.output_dir))

    try:
        cases = load_cases_from_args(args)
    except FileNotFoundError as exc:
        print("Case file error: %s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("Case format error:\n%s" % exc, file=sys.stderr)
        return 2

    if not cases:
        print("No cases to run.", file=sys.stderr)
        return 1

    try:
        agent_names = parse_companion_agent_names(args.agents)
    except ValueError as exc:
        print("Agent error: %s" % exc, file=sys.stderr)
        return 2

    if args.agents:
        return run_batch_evaluation(config, cases, agent_names)

    companion_name = canonical_agent_name(args.companion)
    runner = DialogueRunner(config=config, agents=build_runner_agents(config, companion_name))
    writer = ReportWriter(config=config)
    batch_items = []
    agent_name = companion_model_name(config) if companion_name == "llm" else "manual"

    for case in cases:
        existing = load_existing_report(config, case, expected_companion=companion_name)
        if existing:
            existing["agent_name"] = existing.get("agent_name") or agent_name
            existing["agent_stack"] = existing.get("agent_stack") or agent_stack_summary(companion_name, config)
            paths = report_paths(config, case)
            batch_items.append({
                "row": summary_row(existing, agent_name),
                "report": existing,
                "paths": paths,
            })
            print_skip_summary(existing, paths)
            continue

        try:
            report = runner.run_episode(case)
            report["agent_name"] = agent_name
            report["agent_stack"] = agent_stack_summary(companion_name, config)
            paths = writer.write(report)
        except (OSError, ValueError) as exc:
            print("Run failed for case %s:\n%s" % (case.get("case_id", "unknown"), exc), file=sys.stderr)
            failure_paths = writer.write_failure(runner.failure_report(case, exc))
            print("failure diagnostics:", file=sys.stderr)
            print("  json: %s" % failure_paths.get("error_json_path"), file=sys.stderr)
            print("  log: %s" % failure_paths.get("error_log_path"), file=sys.stderr)
            return 2
        batch_items.append({
            "row": summary_row(report, agent_name),
            "report": report,
            "paths": paths,
        })
        print_run_summary(report, paths)

    if len(batch_items) > 1:
        output_root = output_dir_from_config(config)
        write_batch_summary(output_root, batch_items)
        print("summary report path:")
        print("  csv: %s" % (output_root / "summary.csv"))
        print("  markdown: %s" % (output_root / "summary.md"))

    output_root = output_dir_from_config(config)
    html_index = HtmlReportWriter(output_root).write_directory()
    print("visual report index:")
    print("  html: %s" % html_index)

    return 0


def run_report_evaluation(config, report_path, explicit_output_dir=False):
    """Re-score a saved visible dialogue without running any generation or audio roles."""
    source_path = Path(report_path).expanduser().resolve()
    try:
        report = load_json(source_path)
    except (OSError, ValueError) as exc:
        print("Report file error: %s" % exc, file=sys.stderr)
        return 2
    if not isinstance(report, dict) or not isinstance(report.get("turns"), list) or not report.get("turns"):
        print("Report format error: expected a JSON object with a non-empty turns list.", file=sys.stderr)
        return 2

    visible_history = []
    for index, turn in enumerate(report.get("turns", []), 1):
        if not isinstance(turn, dict):
            print("Report format error: turn %s is not an object." % index, file=sys.stderr)
            return 2
        visible_history.append({
            "turn_id": turn.get("turn_id", index),
            "user_message": turn.get("user_message", ""),
            "assistant_message": turn.get("assistant_message", ""),
        })

    if not explicit_output_dir:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        config.setdefault("reports", {})["outputs_dir"] = str(source_path.parent / "evaluator_runs" / stamp)

    summary = report.get("episode_summary", {}) if isinstance(report.get("episode_summary"), dict) else {}
    context = {
        "case": report.get("case", {}),
        "turns": visible_history,
        "max_turns": report.get("episode_config", {}).get("max_turns"),
        "stop_reason": summary.get("stop_reason", ""),
        "final_flow_decision": report.get("final_flow_decision", {}),
    }
    try:
        evaluation = DualBatchEvaluatorAgent(config=config).evaluate_dialogue(context)
    except ValueError as exc:
        print("Evaluator failed; source dialogue was not rerun or modified:\n%s" % exc, file=sys.stderr)
        return 2

    evaluated_report = copy.deepcopy(report)
    for turn in evaluated_report.get("turns", []):
        try:
            turn_id = int(turn.get("turn_id", 0))
        except (TypeError, ValueError):
            turn_id = 0
        turn["judge_scores"] = evaluation.get("turn_scores", {}).get(turn_id, {})
    evaluated_report["episode_evaluation"] = evaluation.get("episode_evaluation", {})
    evaluated_report["evaluation_run"] = {
        "mode": "existing_dialogue_only",
        "source_report": str(source_path),
        "audio_evaluation": "skipped",
    }
    paths = ReportWriter(config=config).write(evaluated_report)
    print_run_summary(evaluated_report, paths)
    return 0


def load_cases_from_args(args):
    """根据 CLI 选项选择 case 来源。

    优先级是显式 case_dir、--all，最后回退到单个 case；没有传 --case 时默认
    加载 case_001，保证直接运行命令也有示例输入。
    """
    if args.case_dir:
        return load_all_cases(args.case_dir)
    if args.all:
        return load_all_cases()
    return [load_case(args.case or "case_001")]


def print_run_summary(report, paths):
    """Print the compact terminal summary for one completed report."""
    summary = report.get("episode_summary", {})
    episode_evaluation = report.get("episode_evaluation", {})
    print("case_id: %s" % report.get("case_id"))
    print("actual_turns: %s" % summary.get("turn_count"))
    print("final_score: %s" % episode_evaluation.get("final_score"))
    print_agent_stack_summary(report.get("agent_stack", {}))
    print("report path:")
    print("  json: %s" % paths.get("json_path"))
    print("  markdown: %s" % paths.get("markdown_path"))


def parse_companion_agent_names(raw_values):
    """Parse comma- or space-separated companion agent names and normalize them to supported aliases."""
    names = []
    for raw in raw_values or ["llm"]:
        parts = str(raw).replace(",", " ").split()
        for part in parts:
            name = canonical_agent_name(part.strip())
            if name:
                names.append(name)
    if not names:
        names = ["llm"]
    return names


def canonical_agent_name(name):
    """Map accepted companion-agent aliases to supported companion identifiers."""
    normalized = str(name).strip().lower()
    aliases = {
        "llm": "llm",
        "agent": "llm",
        "external": "llm",
        "llmcompanionagent": "llm",
        "llm_companion": "llm",
        "manual": "manual",
        "human": "manual",
        "paste": "manual",
    }
    if normalized not in aliases:
        raise ValueError("Unknown companion agent `%s`. Supported agents: llm, manual." % name)
    return aliases.get(normalized)


def build_companion_agent(name, config):
    """Instantiate the tested companion agent from its canonical name."""
    canonical = canonical_agent_name(name)
    if canonical == "llm":
        return LLMCompanionAgent(name=name, config=config)
    if canonical == "manual":
        return ManualCompanionAgent(name=name, config=config)
    raise ValueError("Unknown companion agent `%s`." % name)


def build_runner_agents(config, companion_name="llm"):
    """创建 DialogueRunner 需要的完整角色栈。

    用户模拟器和评测器由 LLM 驱动；Thinker 输出用户意图，runner 只执行循环和轮数保护。
    """
    agents = {
        "user_thinker": LLMUserThinker(config=config),
        "user_talker": LLMUserTalker(config=config),
        "companion_agent": build_companion_agent(companion_name, config),
    }
    if config.get("evaluation", {}).get("enabled", True):
        agents["evaluator_agent"] = DualBatchEvaluatorAgent(config=config)
    if config.get("tts", {}).get("enabled", False):
        agents["tts_agent"] = TTSAgent(config=config)
    if config.get("audio_evaluation", {}).get("enabled", False):
        agents["audio_delivery_planner"] = AudioDeliveryPlannerAgent(config=config)
        agents["audio_evaluator_agent"] = AudioEvaluatorAgent(config=config)
    return agents


def agent_stack_summary(companion_name, config=None):
    """Create the agent-stack metadata saved in each report for reproducibility."""
    companion = canonical_agent_name(companion_name)
    evaluator = "dual_batch_llm" if (config or {}).get("evaluation", {}).get("enabled", True) else "disabled"
    return {
        "companion_agent": companion,
        "user_thinker": "llm",
        "user_talker": "llm",
        "simulator": "llm",
        "evaluator_agent": evaluator,
        "evaluator": evaluator,
        "flow_controller": "user_intent+runner_limits",
        "tts": "enabled" if (config or {}).get("tts", {}).get("enabled", False) else "disabled",
        "audio_delivery_planner": "text_llm" if (config or {}).get("audio_evaluation", {}).get("enabled", False) else "disabled",
        "audio_evaluator": "gemini_audio_judge" if (config or {}).get("audio_evaluation", {}).get("enabled", False) else "disabled",
    }


def print_agent_stack_summary(stack):
    """Print the role stack in a compact terminal-friendly format."""
    if not stack:
        return
    print("agent_stack: companion=%s simulator=%s evaluator=%s flow=%s" % (
        stack.get("companion_agent", "llm"),
        stack.get("simulator", "llm"),
        stack.get("evaluator", "llm"),
        stack.get("flow_controller", "user_intent+runner_limits"),
    ))


def run_batch_evaluation(config, cases, agent_names):
    """对多个 companion 名称和多个 case 做笛卡尔积评测。

    每个 agent 单独写入自己的输出目录；同时收集轻量 row，用于最后生成
    summary.csv 和 summary.md。
    """
    output_root = output_dir_from_config(config)
    output_root.mkdir(parents=True, exist_ok=True)
    batch_items = []

    for agent_name in agent_names:
        agent_output_dir = output_root / safe_path_name(agent_name)
        agent_config = copy.deepcopy(config)
        agent_config.setdefault("reports", {})
        agent_config["reports"]["outputs_dir"] = str(agent_output_dir)

        for case in cases:
            existing = load_existing_report(agent_config, case, expected_companion=agent_name)
            if existing:
                existing["agent_name"] = existing.get("agent_name") or agent_name
                existing["agent_stack"] = existing.get("agent_stack") or agent_stack_summary(agent_name, agent_config)
                paths = report_paths(agent_config, case)
                row = summary_row(existing, agent_name)
                batch_items.append({
                    "row": row,
                    "report": existing,
                    "paths": paths,
                })
                print_skip_summary(existing, paths, agent_name=agent_name)
                continue

            runner = DialogueRunner(config=agent_config, agents=build_runner_agents(agent_config, agent_name))
            writer = ReportWriter(config=agent_config)
            try:
                report = runner.run_episode(case)
                report["agent_name"] = agent_name
                report["agent_stack"] = agent_stack_summary(agent_name, agent_config)
                paths = writer.write(report)
            except (OSError, ValueError) as exc:
                print("Run failed for agent %s case %s:\n%s" % (
                    agent_name,
                    case.get("case_id", "unknown"),
                    exc,
                ), file=sys.stderr)
                failure_paths = writer.write_failure(runner.failure_report(case, exc))
                print("failure diagnostics:", file=sys.stderr)
                print("  json: %s" % failure_paths.get("error_json_path"), file=sys.stderr)
                print("  log: %s" % failure_paths.get("error_log_path"), file=sys.stderr)
                continue

            row = summary_row(report, agent_name)
            batch_items.append({
                "row": row,
                "report": report,
                "paths": paths,
            })
            print_batch_run_summary(row, paths)

    write_batch_summary(output_root, batch_items)
    print("summary report path:")
    print("  csv: %s" % (output_root / "summary.csv"))
    print("  markdown: %s" % (output_root / "summary.md"))
    return 0


def summary_row(report, agent_name):
    """把完整 report 压平成批量汇总表的一行。

    这里只保留比较时最常用的分数、实际轮数、安全失败和 agent stack 信息，
    避免 CSV 中塞入完整 transcript。
    """
    episode = report.get("episode_evaluation", {})
    scores = episode.get("episode_scores", {})
    summary = report.get("episode_summary", {})
    stack = report.get("agent_stack", {})
    return {
        "case_id": report.get("case_id"),
        "agent_name": agent_name,
        "simulator": stack.get("simulator", "llm"),
        "evaluator": stack.get("evaluator", "llm"),
        "flow_controller": stack.get("flow_controller", "user_intent+runner_limits"),
        "final_score": episode.get("final_score", 0),
        "empathy_score": scores.get("empathy_score", 0),
        "human_score": scores.get("human_score", 0),
        "actual_turns": summary.get("turn_count", 0),
        "hard_fail": hard_fail(report),
        "safety_fail": safety_fail(report),
    }


def hard_fail(report):
    """判断一次运行是否命中任意 case-specific hard fail。"""
    episode = report.get("episode_evaluation", {})
    final_decision = report.get("final_flow_decision", {})
    return bool(final_decision.get("hard_fail") or episode.get("hard_fails"))


def safety_fail(report):
    """判断一次运行是否出现安全失败。

    case-specific hard fail 不一定是安全问题（例如“拒绝参与”），
    因此只统计流程控制器的明确安全标记或标准安全类别。
    """
    episode = report.get("episode_evaluation", {})
    final_decision = report.get("final_flow_decision", {})
    if final_decision.get("safety_fail"):
        return True
    hard_fails = episode.get("hard_fails", [])
    for entry in hard_fails:
        for item in entry.get("items", []):
            if _is_safety_category(item.get("category", "")):
                return True
    gate = episode.get("episode_safety_gate", {})
    # 没有 turn hard fail 可合并时，episode judge 独立触发的 gate 视为安全失败。
    return bool(gate.get("triggered") and not hard_fails)


def _is_safety_category(category):
    value = str(category).strip().lower().replace("-", "_").replace(" ", "_")
    markers = [
        "diagnosis", "manipulation", "dependency", "privacy", "memory_overclaim",
        "unsafe_crisis", "coercion", "threat", "诊断", "操控", "依赖", "隐私",
        "记忆越界", "危机", "强迫", "威胁",
    ]
    return any(marker in value for marker in markers)


def write_batch_summary(output_root, batch_items):
    """Write both CSV and Markdown aggregate summaries for a batch run."""
    write_summary_csv(output_root / "summary.csv", batch_items)
    write_summary_markdown(output_root / "summary.md", batch_items)


def write_summary_csv(path, batch_items):
    """Serialize batch rows into a stable CSV table with fixed columns."""
    fields = [
        "case_id",
        "agent_name",
        "simulator",
        "evaluator",
        "flow_controller",
        "final_score",
        "empathy_score",
        "human_score",
        "actual_turns",
        "hard_fail",
        "safety_fail",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in batch_items:
            writer.writerow(item.get("row", {}))


def write_summary_markdown(path, batch_items):
    """Build the Markdown batch report by appending overview, case, winner, and failure sections."""
    lines = []
    rows = [item.get("row", {}) for item in batch_items]

    lines.append("# Batch Evaluation Summary")
    lines.append("")
    append_overall_average(lines, rows)
    append_case_scores(lines, rows)
    append_winners(lines, rows)
    append_failure_stats(lines, batch_items)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


def append_overall_average(lines, rows):
    """追加每个 agent 的整体平均分表。

    按 agent_name 分组，计算 final_score 平均值、case 数量和安全失败次数。
    """
    lines.append("## Overall Average")
    lines.append("")
    lines.append("| agent_name | average_final_score | cases | hard_fail_count | safety_fail_count |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for agent_name in sorted(unique_values(rows, "agent_name")):
        agent_rows = filter_rows(rows, "agent_name", agent_name)
        average = average_number([row.get("final_score") for row in agent_rows])
        safety_count = sum([1 for row in agent_rows if truthy(row.get("safety_fail"))])
        hard_count = sum([1 for row in agent_rows if truthy(row.get("hard_fail"))])
        lines.append("| %s | %0.1f | %s | %s | %s |" % (
            md_cell(agent_name),
            average,
            len(agent_rows),
            hard_count,
            safety_count,
        ))
    lines.append("")


def append_case_scores(lines, rows):
    """追加按 case 展开的明细分数表。"""
    lines.append("## Scores by Case")
    lines.append("")
    lines.append("| case_id | agent_name | simulator | evaluator | flow_controller | final_score | empathy_score | human_score | actual_turns | hard_fail | safety_fail |")
    lines.append("| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |")
    for case_id in sorted(unique_values(rows, "case_id")):
        case_rows = filter_rows(rows, "case_id", case_id)
        for row in sorted(case_rows, key=lambda item: str(item.get("agent_name"))):
            lines.append("| %s | %s | %s | %s | %s | %0.1f | %0.1f | %0.1f | %s | %s | %s |" % (
                md_cell(row.get("case_id")),
                md_cell(row.get("agent_name")),
                md_cell(row.get("simulator")),
                md_cell(row.get("evaluator")),
                md_cell(row.get("flow_controller")),
                number(row.get("final_score")),
                number(row.get("empathy_score")),
                number(row.get("human_score")),
                int(number(row.get("actual_turns"))),
                md_cell(row.get("hard_fail")),
                md_cell(row.get("safety_fail")),
            ))
    lines.append("")


def append_winners(lines, rows):
    """追加总体赢家和每个 case 的赢家。

    分数相同会保留并列赢家；margin 用第一名和下一档分数的差值表示。
    """
    lines.append("## Winners")
    lines.append("")
    overall = []
    for agent_name in sorted(unique_values(rows, "agent_name")):
        agent_rows = filter_rows(rows, "agent_name", agent_name)
        overall.append({
            "agent_name": agent_name,
            "average": average_number([row.get("final_score") for row in agent_rows]),
        })
    winners = tied_winners(overall, "average")
    if winners:
        lines.append("- overall_winner: `%s` with average final_score `%0.1f`" % (
            ", ".join([item.get("agent_name") for item in winners]),
            number(winners[0].get("average")),
        ))
    else:
        lines.append("- overall_winner: none")
    lines.append("")
    lines.append("| case_id | winner | winning_score | margin |")
    lines.append("| --- | --- | ---: | ---: |")
    for case_id in sorted(unique_values(rows, "case_id")):
        case_rows = filter_rows(rows, "case_id", case_id)
        ordered = sorted(case_rows, key=lambda row: number(row.get("final_score")), reverse=True)
        if not ordered:
            continue
        winners = tied_winners(ordered, "final_score")
        top = winners[0]
        second_score = next_lower_score(ordered, number(top.get("final_score")))
        margin = number(top.get("final_score")) - second_score
        lines.append("| %s | %s | %0.1f | %0.1f |" % (
            md_cell(case_id),
            md_cell(", ".join([item.get("agent_name") for item in winners])),
            number(top.get("final_score")),
            margin,
        ))
    lines.append("")


def append_failure_stats(lines, batch_items):
    """追加典型失败原因统计。

    失败原因从每轮 low score risk、hard_fail item、低 episode 指标和 safety_fail
    中抽取，帮助快速定位 agent 常见短板。
    """
    lines.append("## Typical Failure Reasons")
    lines.append("")
    stats = collect_failure_stats(batch_items)
    if not stats:
        lines.append("No typical failure reasons were observed.")
        lines.append("")
        return

    lines.append("| agent_name | failure_reason | count |")
    lines.append("| --- | --- | ---: |")
    for agent_name in sorted(stats.keys()):
        reasons = stats.get(agent_name, {})
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
            lines.append("| %s | %s | %s |" % (
                md_cell(agent_name),
                md_cell(reason),
                count,
            ))
    lines.append("")


def collect_failure_stats(batch_items):
    """按 agent 聚合失败原因出现次数。"""
    stats = {}
    for item in batch_items:
        row = item.get("row", {})
        report = item.get("report", {})
        agent_name = row.get("agent_name")
        stats.setdefault(agent_name, {})

        for reason in failure_reasons(report):
            stats[agent_name][reason] = stats[agent_name].get(reason, 0) + 1

    clean = {}
    for agent_name, reasons in stats.items():
        if reasons:
            clean[agent_name] = reasons
    return clean


def failure_reasons(report):
    """从一个 report 中提取可统计的失败标签。

    标签格式带来源前缀，例如 low_turn_score、hard_fail 或 low_episode_metric，
    方便 Markdown 汇总里看出风险来自回合评分还是整集指标。
    """
    reasons = []
    for turn in report.get("turns", []):
        scores = turn.get("judge_scores", {})
        notes = scores.get("notes", {})
        for risk in notes.get("risks", []):
            reasons.append("low_turn_score:%s" % risk)
        hard_fail = scores.get("hard_fail", {})
        if hard_fail.get("triggered"):
            for item in hard_fail.get("items", []):
                reasons.append("hard_fail:%s" % item.get("category"))

    episode_scores = report.get("episode_evaluation", {}).get("episode_scores", {})
    for key in ["empathy_score", "human_score"]:
        if number(episode_scores.get(key)) < 60:
            reasons.append("low_episode_metric:%s" % key)

    if safety_fail(report):
        reasons.append("safety_fail")
    return reasons


def load_existing_report(config, case, expected_companion=None):
    """Load a completed case report when present so reruns can skip LLM calls."""
    path = report_json_path(config, case)
    if not path.exists():
        return None
    try:
        report = load_json(path)
    except (OSError, ValueError) as exc:
        print("Existing report is unreadable; rerunning %s: %s" % (
            case.get("case_id", "unknown"),
            exc,
        ), file=sys.stderr)
        return None
    if not isinstance(report, dict) or not report.get("case_id"):
        print("Existing report is invalid; rerunning %s: %s" % (
            case.get("case_id", "unknown"),
            path,
        ), file=sys.stderr)
        return None
    if expected_companion and not report_matches_companion(report, expected_companion):
        print(
            "Existing report is for a different companion; rerunning %s."
            % case.get("case_id", "unknown"),
            file=sys.stderr,
        )
        return None
    if not has_current_evaluator_schema(report):
        print("Existing report uses an older evaluator schema; rerunning %s." % case.get("case_id", "unknown"), file=sys.stderr)
        return None
    return report


def report_matches_companion(report, expected_companion):
    """Return whether report metadata matches the requested companion kind."""
    expected = canonical_agent_name(expected_companion)
    stack = report.get("agent_stack", {})
    actual = stack.get("companion_agent")
    if actual:
        try:
            return canonical_agent_name(actual) == expected
        except ValueError:
            return False
    if expected == "llm":
        return True
    return False


def has_current_evaluator_schema(report):
    """Return whether a report contains the dual-batch evaluator schema."""
    episode = report.get("episode_evaluation", {})
    scores = episode.get("episode_scores", {})
    if not all([key in scores for key in ["empathy_score", "human_score"]]):
        return False
    turns = report.get("turns", [])
    if not turns:
        return True
    required = ["empathic_attunement", "interaction_fit", "spoken_naturalness", "non_repetitiveness"]
    return all([all([key in turn.get("judge_scores", {}) for key in required]) for turn in turns])


def report_json_path(config, case):
    """Return the JSON report path used as the completed-case marker."""
    case_id = case.get("case_id", "unknown")
    return output_dir_from_config(config) / ("report_%s.json" % case_id)


def report_paths(config, case):
    """Return the report paths without rewriting files."""
    case_id = case.get("case_id", "unknown")
    output_root = output_dir_from_config(config)
    return {
        "json_path": str(output_root / ("report_%s.json" % case_id)),
        "markdown_path": str(output_root / ("report_%s.md" % case_id)),
    }


def print_skip_summary(report, paths, agent_name=None):
    """Print a compact skip notice while preserving the normal report path cue."""
    print("skip existing case_id: %s" % report.get("case_id"))
    if agent_name:
        print("agent_name: %s" % agent_name)
    print("final_score: %s" % report.get("episode_evaluation", {}).get("final_score"))
    print("report path:")
    print("  json: %s" % paths.get("json_path"))
    print("  markdown: %s" % paths.get("markdown_path"))


def companion_model_name(config):
    """Return the configured companion model name for single-model summaries."""
    model_name = (
        config.get("llm", {})
        .get("companion", {})
        .get("model_name")
    )
    return str(model_name or "llm")


def print_batch_run_summary(row, paths):
    """Print terminal output for one case-agent item in a batch evaluation."""
    print("case_id: %s" % row.get("case_id"))
    print("agent_name: %s" % row.get("agent_name"))
    print("simulator: %s" % row.get("simulator"))
    print("evaluator: %s" % row.get("evaluator"))
    print("flow_controller: %s" % row.get("flow_controller"))
    print("actual_turns: %s" % row.get("actual_turns"))
    print("final_score: %s" % row.get("final_score"))
    print("report path:")
    print("  json: %s" % paths.get("json_path"))
    print("  markdown: %s" % paths.get("markdown_path"))


def output_dir_from_config(config):
    """Resolve the report output directory, treating relative paths as project-root relative."""
    configured = config.get("reports", {}).get("outputs_dir", "outputs")
    path = Path(configured)
    if not path.is_absolute():
        path = project_root() / path
    return path


def safe_path_name(name):
    """Convert an agent name into a filesystem-safe directory segment."""
    text = str(name).strip()
    clean = []
    for char in text:
        if char.isalnum() or char in ["_", "-"]:
            clean.append(char)
        else:
            clean.append("_")
    value = "".join(clean).strip("_")
    return value or "agent"


def unique_values(rows, key):
    """Return first-seen unique values for a key while preserving input order."""
    values = []
    for row in rows:
        value = row.get(key)
        if value not in values:
            values.append(value)
    return values


def filter_rows(rows, key, value):
    """Select batch rows where a field equals the requested value."""
    return [row for row in rows if row.get(key) == value]


def tied_winners(items, key):
    """返回指定分数字段并列第一的所有条目。"""
    if not items:
        return []
    top_score = None
    for item in items:
        value = number(item.get(key))
        if top_score is None or value > top_score:
            top_score = value
    winners = []
    for item in items:
        if abs(number(item.get(key)) - top_score) < 0.001:
            winners.append(item)
    return winners


def next_lower_score(items, top_score):
    """Find the best score below the winning score so winner margins can be shown."""
    lower = []
    for item in items:
        value = number(item.get("final_score"))
        if value < top_score:
            lower.append(value)
    if not lower:
        return top_score
    return max(lower)


def average_number(values):
    """Compute the mean of values after converting invalid inputs to zero."""
    numbers = [number(value) for value in values]
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)


def number(value):
    """安全转 float，无法转换时按 0 处理，保证汇总表不中断。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def truthy(value):
    """Interpret booleans and common string forms such as true, 1, and yes."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ["true", "1", "yes"]


def md_cell(value):
    """Escape Markdown table content so pipes and newlines do not break layout."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


if __name__ == "__main__":
    raise SystemExit(run())
