#!/usr/bin/env python3
"""Run emotional sandbox cases against AneAgent's real local chat API."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SANDBOX_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SANDBOX_ROOT.parents[1]
DEFAULT_ANE_DIR = WORKSPACE_ROOT / "agent" / "AneAgent"
if str(SANDBOX_ROOT) not in sys.path:
    sys.path.insert(0, str(SANDBOX_ROOT))

from sandbox.agents.base_agent import BaseAgent
from sandbox.case_loader import load_all_cases, load_case
from sandbox.dialogue_runner import DialogueRunner
from sandbox.main import (
    agent_stack_summary,
    build_runner_agents,
    load_config,
    print_run_summary,
    summary_row,
    write_batch_summary,
)
from sandbox.report_writer import ReportWriter


class AneHttpCompanionAgent(BaseAgent):
    """Adapter from DialogueRunner's visible context to AneAgent chat episodes."""

    def __init__(self, base_url: str, timeout: int = 180):
        super().__init__(name="ane_agent")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.episode_id = ""
        self.last_schema_failure = {}

    def generate(self, context):
        self.validate_context(context, ["visible_history", "current_user_message"])
        if not context.get("visible_history") or not self.episode_id:
            state = self._request("POST", "/api/episodes", {
                "title": "emotional_sandbox_evaluation",
                "mode": "chat",
            })
            self.episode_id = str(state.get("episode", {}).get("id", ""))
            if not self.episode_id:
                raise ValueError("AneAgent did not return an episode id.")

        state = self._request(
            "POST",
            "/api/episodes/%s/messages" % self.episode_id,
            {"message": str(context.get("current_user_message", ""))},
        )
        reply, candidate_id, turn_id = self._latest_reply(state)
        if not reply:
            self.last_schema_failure = {
                "reason": "ane_agent_missing_candidate",
                "episode_id": self.episode_id,
                "last_state": state,
            }
            raise ValueError("AneAgent response did not contain a generated candidate.")
        self.last_schema_failure = {}
        return {
            "assistant_message": reply,
            "metadata": {
                "agent_type": "ane_agent_http",
                "provider": "AneAgent",
                "model_name": self._model_name(state),
                "episode_id": self.episode_id,
                "turn_id": turn_id,
                "candidate_id": candidate_id,
                "used_hidden_state": False,
                "used_private_fields": False,
            },
        }

    def test_connection(self):
        """Fail before simulator calls if AneAgent's reply-model credentials are invalid."""
        result = self._request("POST", "/api/settings/test", {})
        if result.get("ok") is not True:
            raise ValueError("AneAgent connection test did not return ok=true.")
        return result

    def _request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValueError("AneAgent HTTP %s: %s" % (exc.code, detail[:1000])) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValueError("AneAgent request failed: %s" % exc) from exc
        if not isinstance(payload, dict):
            raise ValueError("AneAgent returned a non-object response.")
        return payload

    def _latest_reply(self, state):
        turns = state.get("turns", []) if isinstance(state, dict) else []
        if not turns:
            return "", "", ""
        turn = turns[-1]
        batches = turn.get("batches", []) if isinstance(turn, dict) else []
        candidates = batches[-1].get("candidates", []) if batches else []
        candidates = sorted(
            [item for item in candidates if isinstance(item, dict)],
            key=lambda item: int(item.get("displayed_position", 999) or 999),
        )
        if not candidates:
            return "", "", str(turn.get("id", ""))
        candidate = candidates[0]
        return (
            str(candidate.get("content", "")).strip(),
            str(candidate.get("id", "")),
            str(turn.get("id", "")),
        )

    def _model_name(self, state):
        turns = state.get("turns", []) if isinstance(state, dict) else []
        if turns and turns[-1].get("batches"):
            return str(turns[-1]["batches"][-1].get("model_name", ""))
        return ""


class AneServer:
    """Start AneAgent on an isolated copy of its configured database."""

    def __init__(self, ane_dir: Path, runtime_dir: Path, port: int):
        self.ane_dir = ane_dir.resolve()
        self.runtime_dir = runtime_dir.resolve()
        self.port = port
        self.process = None
        self.log_handle = None
        self.base_url = "http://127.0.0.1:%s" % port

    def __enter__(self):
        source_db = self.ane_dir / "data" / "eve-signal.db"
        if not source_db.exists():
            raise FileNotFoundError("AneAgent database not found: %s" % source_db)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        runtime_db = self.runtime_dir / "ane-evaluation.db"
        if runtime_db.exists():
            runtime_db.unlink()
        with sqlite3.connect(str(source_db)) as source_conn:
            with sqlite3.connect(str(runtime_db)) as target_conn:
                source_conn.backup(target_conn)
        python = self.ane_dir / ".venv" / "bin" / "python"
        if not python.exists():
            python = Path(sys.executable)
        env = os.environ.copy()
        # AneAgent's credential resolver treats DASHSCOPE_API_KEY as a legacy
        # reply-model override even when its configured api_base is DeepSeek.
        # Keep sandbox evaluator credentials out of the child process and use
        # AneAgent's copied database setting by default. An explicit, separate
        # override remains available without exposing the key on the command line.
        ane_api_key = env.pop("ANE_AGENT_API_KEY", "")
        env.pop("QWEN_API_KEY", None)
        env.pop("DASHSCOPE_API_KEY", None)
        if ane_api_key:
            env["QWEN_API_KEY"] = ane_api_key
        env.update({
            "OVO_HOST": "127.0.0.1",
            "OVO_PORT": str(self.port),
            "OVO_DB_PATH": str(runtime_db),
        })
        self.log_handle = (self.runtime_dir / "ane-server.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(python), "app.py"],
            cwd=str(self.ane_dir),
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("AneAgent server exited early; see %s" % (self.runtime_dir / "ane-server.log"))
            try:
                with urlopen(self.base_url + "/api/bootstrap", timeout=1):
                    return self
            except (URLError, TimeoutError):
                time.sleep(0.25)
        raise RuntimeError("Timed out waiting for AneAgent at %s" % self.base_url)

    def __exit__(self, exc_type, exc, traceback):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log_handle is not None:
            self.log_handle.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Evaluate AneAgent with emotional_sandbox_V2_new.")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--case", default="daily_008", help="One sandbox case id or YAML path.")
    selector.add_argument("--cases", nargs="+", help="Selected case ids or YAML paths.")
    selector.add_argument("--case-dir", help="Run every YAML file in this directory.")
    selector.add_argument("--all", action="store_true", help="Run all default sandbox cases.")
    parser.add_argument("--config", default="configs/bailian_qwen_plus_companion.yaml")
    parser.add_argument("--output-dir", default="outputs/ane_agent")
    parser.add_argument("--ane-dir", default=str(DEFAULT_ANE_DIR))
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--no-evaluator", action="store_true")
    return parser.parse_args()


def selected_cases(args):
    if args.all:
        return load_all_cases()
    if args.case_dir:
        return load_all_cases(args.case_dir)
    if args.cases:
        return [load_case(case_ref) for case_ref in args.cases]
    return [load_case(args.case)]


def main():
    args = parse_args()
    config = load_config(args.config)
    config.setdefault("reports", {})["outputs_dir"] = args.output_dir
    config.setdefault("tts", {})["enabled"] = False
    config.setdefault("audio_evaluation", {})["enabled"] = False
    if args.no_evaluator:
        config.setdefault("evaluation", {})["enabled"] = False
    if args.max_turns:
        config.setdefault("episode", {})["max_turns"] = args.max_turns
        config["episode"]["override_case_max_turns"] = True

    cases = selected_cases(args)
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = SANDBOX_ROOT / output_dir
    runtime_dir = output_dir / "_ane_runtime"
    writer = ReportWriter(config)
    batch_items = []

    with AneServer(Path(args.ane_dir), runtime_dir, args.port) as server:
        ane_agent = AneHttpCompanionAgent(server.base_url)
        connection = ane_agent.test_connection()
        print("AneAgent connection ok: model=%s" % connection.get("model_name", "unknown"))
        base_agents = build_runner_agents(config, "llm")
        base_agents["companion_agent"] = ane_agent
        runner = DialogueRunner(config=config, agents=base_agents)
        for case in cases:
            try:
                report = runner.run_episode(case)
                report["agent_name"] = "AneAgent"
                stack = agent_stack_summary("llm", config)
                stack["companion_agent"] = "AneAgent"
                stack["companion_transport"] = "local_http_chat"
                report["agent_stack"] = stack
                paths = writer.write(report)
                batch_items.append({
                    "row": summary_row(report, "AneAgent"),
                    "report": report,
                    "paths": paths,
                })
                print_run_summary(report, paths)
            except (OSError, ValueError) as exc:
                print("Run failed for case %s:\n%s" % (case.get("case_id", "unknown"), exc), file=sys.stderr)
                paths = writer.write_failure(runner.failure_report(case, exc))
                print("failure diagnostics: %s" % paths, file=sys.stderr)

    if len(batch_items) > 1:
        write_batch_summary(output_dir, batch_items)
    return 0 if batch_items else 2


if __name__ == "__main__":
    raise SystemExit(main())
