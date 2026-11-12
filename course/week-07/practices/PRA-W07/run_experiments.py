"""采集 W07 三个独立会话；保存真实模型、MCP、批准与取消证据。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

from prepare_workspace import prepare
from prepare_mcp_config import prepare as prepare_config

MATERIALS = Path(__file__).resolve().parent
W06 = MATERIALS.parents[2] / "week-06/practices/PRA-W06"
LIMITS = {"A": 8, "B": 2, "C": 2}
MAXIMUM_REQUESTS = 12
NORMAL_MAX_TOKENS = 1024
CONTEXT_TOKENS = 8192


def load_previous():
    spec = importlib.util.spec_from_file_location("w07_previous_driver", W06 / "run_experiments.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def server_observations(directory):
    observations = []
    for path in sorted((directory / "runs").glob("mcp-preflight-*/rules/stderr.txt")):
        for line in path.read_bytes().split(b"\n")[:-1]:
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                continue  # SDK 的普通诊断不属于服务的到达回执。
            if isinstance(item, dict) and item.get("server") == "rules":
                observations.append({"path": str(path), "record": item})
    return observations


def collect(command, directory, workspace, phase, approval_driver, previous_calls):
    previous = load_previous()
    directory.mkdir(parents=True, exist_ok=False)
    invocation = {"argv": command, "cwd": str(Path(command[1]).parent), "phase": phase,
                  "started_at": time.time(), "evidence_source": "live-external-api",
                  "model_request_limit": LIMITS[phase], "previous_model_calls": previous_calls}
    process = subprocess.Popen(command, cwd=invocation["cwd"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    invocation["pid"] = process.pid
    previous.save_json(directory / "invocation.json", invocation)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending, approvals, observed = bytearray(), [], []
    stop_reason, cancellation = None, None
    deadline = time.monotonic() + 340
    try:
        while selector.get_map() or process.poll() is None:
            calls = [event for event in previous.request_events(directory)
                     if event["event"] == "model_requested"]
            for event in calls[len(observed):]:
                item = {"observed_at": time.time(), "seq": event["seq"], "turn": event["turn"],
                        "purpose": event["purpose"], "run_id": event["run_id"]}
                observed.append(item)
                with (directory / "request-observations.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            if stop_reason is None:
                if len(calls) > LIMITS[phase] or previous_calls + len(calls) > MAXIMUM_REQUESTS:
                    stop_reason = "request_budget_exceeded"
                elif time.monotonic() >= deadline:
                    stop_reason = "supervisor_timeout"
                if stop_reason:
                    previous.stop_process(process)
                    deadline = time.monotonic() + 5
            elif time.monotonic() >= deadline:
                break
            if phase == "C" and cancellation is None and process.poll() is None and stop_reason is None:
                server = server_observations(directory)
                arrived = [item for item in server if item["record"].get("event") == "lookup_received"
                           and item["record"].get("document_id") == "port-format/r1"]
                heartbeats = [item for item in server if item["record"].get("event") == "heartbeat"
                              and item["record"].get("document_id") == "port-format/r1"]
                if arrived and heartbeats:
                    cancellation = {"signal": "SIGINT", "pid": process.pid, "sent_at": time.time(),
                                    "sent_monotonic": time.monotonic(), "arrival": arrived[0],
                                    "heartbeat": heartbeats[0], "source": "experiment-driver"}
                    previous.save_json(directory / "cancellation.json", cancellation)
                    process.send_signal(signal.SIGINT)
                    deadline = time.monotonic() + 15
            for key, _ in selector.select(timeout=0.05):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(chunk)
                if key.data != "stderr":
                    continue
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, tail = pending.partition(b"\n")
                    pending[:] = tail
                    if not line.startswith(b"W03_APPROVAL "):
                        continue
                    proposal = json.loads(line[len(b"W03_APPROVAL "):])
                    observation = approval_driver.observe_proposal(proposal, directory, workspace)
                    decision = "approve" if phase == "A" and stop_reason is None else "deny"
                    item = {"time": time.time(), "proposal": proposal, "decision": decision,
                            "observation_directory": observation, "source": "experiment-driver",
                            "external_change_before_response": False}
                    approvals.append(item)
                    with (directory / "driver.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    process.stdin.write(f"{decision} {proposal['proposal_id']}\n".encode())
                    process.stdin.flush()
        process.wait(timeout=5)
        final_calls = [event for event in previous.request_events(directory) if event["event"] == "model_requested"]
        for event in final_calls[len(observed):]:
            item = {"observed_at": time.time(), "seq": event["seq"], "turn": event["turn"],
                    "purpose": event["purpose"], "run_id": event["run_id"]}
            observed.append(item)
            with (directory / "request-observations.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        if len(final_calls) > LIMITS[phase] or previous_calls + len(final_calls) > MAXIMUM_REQUESTS:
            stop_reason = "request_budget_exceeded"
    finally:
        selector.close()
        previous.stop_process(process)
        for handle in (process.stdin, process.stdout, process.stderr):
            handle.close()
        (directory / "stdout.json").write_bytes(buffers["stdout"])
        (directory / "stderr.txt").write_bytes(buffers["stderr"])
        (directory / "exit-code.txt").write_text(f"{process.returncode}\n")
        previous.save_json(directory / "supervision.json", {"stop_reason": stop_reason,
                           "request_observations": observed, "approval_count": len(approvals),
                           "cancellation_sent": cancellation is not None})
    result = json.loads(buffers["stdout"])
    if result.get("exit_code") != process.returncode:
        raise ValueError("CLI JSON 与实际退出码不同")
    process_observations = []
    for path in sorted((directory / "runs").glob("mcp-preflight-*/*/initialization.json")):
        initialization = json.loads(path.read_text())
        for field in ("worker_pid", "server_pid"):
            pid = initialization.get(field)
            if type(pid) is not int or pid <= 0:
                raise ValueError("MCP 初始化缺少实际 PID")
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            process_observations.append({"source": str(path), "field": field, "pid": pid,
                                         "alive": alive, "observed_at": time.time()})
    previous.save_json(directory / "process-observations.json", process_observations)
    return {**result, "phase": phase, "directory": str(directory), "pid": process.pid,
            "supervisor_stop_reason": stop_reason, "approval_count": len(approvals),
            "cancellation_sent": cancellation is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    parser.add_argument("--sdk-python", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY") or not os.environ.get("DEEPSEEK_MODEL"):
        parser.error("先在当前 shell 设置 DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL")
    student, runtime = args.student_dir.resolve(strict=True), args.runtime.resolve(strict=True)
    entry = student / "main.py"
    for name in ("w07_mcp.py", "mcp_server.py", "runtime/worker.py"):
        if (student / name).read_bytes() != (MATERIALS / name).read_bytes():
            parser.error("先安装本周 MCP 供给与固定检查 worker")
    previous = load_previous()
    w04 = previous.load_file("w07_w04_driver", previous.W04 / "run_experiments.py")
    approval_driver = w04.load_w03_driver()
    batch = args.output_dir.resolve() / ("batch-" + uuid.uuid4().hex[:12])
    batch.mkdir(parents=True, exist_ok=False)
    workspace = prepare(batch / "workspace")
    (batch / "inputs").mkdir()
    for source in (MATERIALS / "inputs").glob("question-*.md"):
        (batch / "inputs" / source.name).write_bytes(source.read_bytes())
    prepare_config(student, args.sdk_python, batch / "normal-config.json")
    prepare_config(student, args.sdk_python, batch / "slow-config.json", rules_mode="slow")
    previous.save_json(batch / "experiment.json", {"evidence_source": "live-external-api", "limits": LIMITS,
                       "maximum_model_requests": MAXIMUM_REQUESTS, "normal_max_tokens": NORMAL_MAX_TOKENS,
                       "context_tokens": CONTEXT_TOKENS, "summary_max_tokens": 1024, "automatic_retries": 0,
                       "workspace": str(workspace), "started_at": time.time()})
    previous.save_json(batch / "runtime.json", json.loads(runtime.read_text()))
    (batch / "runtime-worker.py").write_bytes((student / "runtime/worker.py").read_bytes())
    print(json.dumps({"batch_directory": str(batch), "maximum_model_requests": MAXIMUM_REQUESTS}), flush=True)
    rows, total = [], 0
    try:
        for phase in LIMITS:
            previous.snapshot(workspace, batch / f"before-{phase}")
            session = batch / (f"session-{phase}.jsonl")
            config = batch / ("slow-config.json" if phase == "C" else "normal-config.json")
            command = [args.python, str(entry), "new", "--session", str(session), "--workspace", str(workspace),
                       "--question", str(batch / "inputs" / f"question-{phase.lower()}.md"),
                       "--output-dir", str(batch / phase / "runs"), "--runtime", str(runtime),
                       "--mcp-config", str(config), "--max-turns", str(LIMITS[phase]), "--max-actions", "16",
                       "--max-tokens", str(NORMAL_MAX_TOKENS), "--context-tokens", str(CONTEXT_TOKENS),
                       "--deadline-seconds", "300", "--command-timeout", "10"]
            try:
                row = collect(command, batch / phase, workspace, phase, approval_driver, total)
            finally:
                previous.snapshot(workspace, batch / f"after-{phase}")
                directory = batch / phase
                directory.mkdir(exist_ok=True)
                (directory / "after-session.jsonl").write_bytes(session.read_bytes() if session.exists() else b"")
            rows.append(row)
            calls = row.get("model_calls")
            if type(calls) is not int or not 0 <= calls <= LIMITS[phase]:
                raise ValueError("实际请求数无效或超过阶段上限")
            total += calls
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if row["supervisor_stop_reason"] or (phase == "A" and row["status"] != "stop"):
                return 1
            if not row.get("mcp_cleanup", {}).get("cleanup_confirmed"):
                return 1
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        previous.save_json(batch / "driver-error.json", {"class": type(error).__name__, "message": str(error),
                           "completed_phases": [row["phase"] for row in rows]})
        raise
    finally:
        previous.save_json(batch / "summary.json", rows)


if __name__ == "__main__":
    raise SystemExit(main())
