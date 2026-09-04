"""Refresh-safe Streamlit demo for live companion episodes."""

import copy
import json
import threading
import time
import uuid
from pathlib import Path

import streamlit as st

from sandbox.case_loader import list_case_files, load_case
from sandbox.dialogue_runner import DialogueRunner
from sandbox.main import build_runner_agents, load_config
from sandbox.report_writer import ReportWriter


PROJECT_ROOT = Path(__file__).resolve().parent


@st.cache_resource
def job_store():
    """Keep background jobs alive across reruns and browser refreshes."""
    return {"jobs": {}, "lock": threading.RLock()}


def main():
    st.set_page_config(page_title="情感陪伴语音沙盒", page_icon="🎧", layout="wide")
    st.title("情感陪伴语音评测 Demo")
    st.caption("实时观看模拟对话、用户内心 OS、TTS 音频，以及 episode 最终评分。")
    options = sidebar_controls()
    active_run_id = query_run_id()
    if st.button("开始新运行", type="primary", use_container_width=True):
        active_run_id = start_background_job(*options)
        st.query_params["run_id"] = active_run_id
        st.rerun()
    if active_run_id:
        render_live_job(active_run_id)
    else:
        st.info("选择参数后点击“开始新运行”。运行期间可以安全刷新页面。")


def sidebar_controls():
    st.sidebar.header("运行设置")
    config_files = sorted((PROJECT_ROOT / "configs").glob("*.yaml"))
    config_names = [path.name for path in config_files]
    qwen_default = "bailian_qwen_plus_companion.yaml"
    default_index = config_names.index(qwen_default) if qwen_default in config_names else 0
    selected_config = st.sidebar.selectbox("配置", config_names, index=default_index)
    case_files = list_case_files(PROJECT_ROOT / "data" / "cases")
    case_labels = [path.stem for path in case_files]
    selected_case = st.sidebar.selectbox("Case", case_labels)
    max_turns = st.sidebar.slider("最大轮数", 1, 30, 5)
    enable_tts = st.sidebar.checkbox("生成并播放 Qwen 语音", value=True)
    enable_audio_judge = st.sidebar.checkbox("运行 Qwen 音频评分", value=True)
    if enable_audio_judge and not enable_tts:
        st.sidebar.warning("音频评分需要先生成语音，运行时会自动跳过。")
    return (
        PROJECT_ROOT / "configs" / selected_config,
        case_files[case_labels.index(selected_case)],
        max_turns,
        enable_tts,
        enable_audio_judge,
    )


def query_run_id():
    value = st.query_params.get("run_id", "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "")


def start_background_job(config_path, case_path, max_turns, enable_tts, enable_audio_judge):
    run_id = "%s-%s" % (Path(case_path).stem, uuid.uuid4().hex[:10])
    job = {
        "run_id": run_id,
        "status": "starting",
        "created_at": time.time(),
        "updated_at": time.time(),
        "turns": [],
        "report": None,
        "error": "",
    }
    store = job_store()
    with store["lock"]:
        store["jobs"][run_id] = job
    thread = threading.Thread(
        target=run_job,
        args=(store, run_id, config_path, case_path, max_turns, enable_tts, enable_audio_judge),
        name="companion-demo-%s" % run_id,
        daemon=True,
    )
    thread.start()
    return run_id


def run_job(store, run_id, config_path, case_path, max_turns, enable_tts, enable_audio_judge):
    """Run independently from the browser and persist every completed turn."""
    update_job(store, run_id, status="running")
    try:
        config = load_config(PROJECT_ROOT / "configs" / "default.yaml")
        if Path(config_path).name != "default.yaml":
            config = deep_merge(config, load_config(config_path))
        config.setdefault("episode", {})
        config["episode"]["max_turns"] = max_turns
        config["episode"]["override_case_max_turns"] = True
        config.setdefault("tts", {})["enabled"] = bool(enable_tts)
        config.setdefault("audio_evaluation", {})["enabled"] = bool(enable_tts and enable_audio_judge)
        config.setdefault("reports", {})
        config["reports"]["outputs_dir"] = str(PROJECT_ROOT / "outputs" / "demo" / run_id)
        case = load_case(case_path)

        def on_turn(turn):
            with store["lock"]:
                job = store["jobs"].get(run_id)
                if job is not None:
                    job["turns"].append(copy.deepcopy(turn))
                    job["updated_at"] = time.time()

        agents = build_runner_agents(config, "llm")
        agents["on_turn"] = on_turn
        report = DialogueRunner(config=config, agents=agents).run_episode(case)
        report["agent_name"] = config.get("llm", {}).get("companion", {}).get("model_name", "llm")
        paths = ReportWriter(config=config).write(report)
        report["demo_report_paths"] = paths
        update_job(store, run_id, status="completed", report=report, turns=report.get("turns", []))
    except Exception as exc:
        update_job(store, run_id, status="failed", error="%s: %s" % (exc.__class__.__name__, exc))


def update_job(store, run_id, **values):
    with store["lock"]:
        job = store["jobs"].get(run_id)
        if job is not None:
            job.update(values)
            job["updated_at"] = time.time()


def job_snapshot(run_id):
    store = job_store()
    with store["lock"]:
        job = store["jobs"].get(run_id)
        return copy.deepcopy(job) if job else None


@st.fragment(run_every="1s")
def render_live_job(run_id):
    """Poll server-side state without restarting the job."""
    job = job_snapshot(run_id)
    if not job:
        st.error("找不到运行 `%s`。服务端可能已重启，请重新运行。" % run_id)
        return
    status = job.get("status")
    if status in ("starting", "running"):
        st.info("⏳ 后台运行中，刷新不会中断 · run_id: `%s` · 已完成 %s 轮" % (
            run_id, len(job.get("turns", []))
        ))
    elif status == "completed":
        st.success("✅ 对话与评分完成 · run_id: `%s`" % run_id)
    else:
        st.error("❌ 运行失败：%s" % job.get("error", "未知错误"))
    for turn in job.get("turns", []):
        render_turn(turn, live=status in ("starting", "running"))
    if status == "completed" and job.get("report"):
        st.divider()
        render_score_report(job["report"])
        render_downloads(job["report"])


def render_turn(turn, live=False):
    st.markdown("#### 第 %s 轮%s" % (turn.get("turn_id"), " · 实时" if live else ""))
    user_col, assistant_col = st.columns(2)
    with user_col:
        st.markdown("**👤 用户**")
        st.info(turn.get("user_message", ""))
        with st.expander("💭 用户内心 OS"):
            private = turn.get("user_private_state", {})
            st.markdown("**内心反应：** %s" % private.get("inner_reaction", "未提供"))
            st.markdown("**表达意图：** %s" % format_value(private.get("intent")))
    with assistant_col:
        st.markdown("**🤖 助手**")
        st.success(turn.get("assistant_message", ""))
        audio = turn.get("audio", {})
        audio_path = Path(str(audio.get("path", ""))) if audio.get("path") else None
        if audio_path and audio_path.exists():
            st.audio(str(audio_path))
        elif audio:
            st.warning("语音生成失败：%s" % audio.get("error", "未知错误"))


def render_score_report(report):
    st.header("评分报告")
    episode = report.get("episode_evaluation", {})
    summary = report.get("episode_summary", {})
    cols = st.columns(4)
    cols[0].metric("最终文本分", display_score(episode.get("final_score")))
    cols[1].metric("实际轮数", summary.get("turn_count", len(report.get("turns", []))))
    cols[2].metric("共情分", display_score(episode.get("episode_scores", {}).get("empathy_score")))
    cols[3].metric("文本口语分", display_score(episode.get("episode_scores", {}).get("human_score")))
    rows = []
    for turn in report.get("turns", []):
        text_scores = turn.get("judge_scores", {})
        audio_eval = turn.get("audio", {}).get("evaluation", {})
        rows.append({
            "轮次": turn.get("turn_id"),
            "共情": text_scores.get("empathic_attunement"),
            "互动适配": text_scores.get("interaction_fit"),
            "文本口语自然度": text_scores.get("spoken_naturalness"),
            "语音自然度": audio_eval.get("audio_naturalness"),
            "语音口语化": audio_eval.get("audio_colloquialness"),
            "语音综合": audio_eval.get("overall"),
        })
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    with st.expander("查看音频评分明细"):
        for turn in report.get("turns", []):
            passes = turn.get("audio", {}).get("evaluation", {}).get("passes", [])
            if passes:
                st.markdown("**第 %s 轮**" % turn.get("turn_id"))
                st.dataframe(passes, use_container_width=True, hide_index=True)


def render_downloads(report):
    paths = report.get("demo_report_paths", {})
    st.download_button(
        "下载 JSON 报告",
        data=json.dumps(report, ensure_ascii=False, indent=2, default=str),
        file_name="report_%s.json" % report.get("case_id", "demo"),
        mime="application/json",
    )
    md_path = Path(str(paths.get("markdown_path", ""))) if paths.get("markdown_path") else None
    if md_path and md_path.exists():
        st.download_button(
            "下载 Markdown 报告",
            data=md_path.read_text(encoding="utf-8"),
            file_name=md_path.name,
            mime="text/markdown",
        )


def format_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "未提供")


def display_score(value):
    if value is None:
        return "—"
    try:
        return "%.1f" % float(value)
    except (TypeError, ValueError):
        return str(value)


def deep_merge(base, override):
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


if __name__ == "__main__":
    main()
