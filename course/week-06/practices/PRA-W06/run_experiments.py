"""一次采集 W06 连续 A/B/C；保存原始证据，答案语义另行核对。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import selectors
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
W04 = MATERIALS.parents[2] / "week-04/practices/PRA-W04"
LIMITS = {"A": 2, "B": 8, "C": 4}
CONTEXT_TOKENS = {"A": 8192, "B": 7800, "C": 7300}
MAXIMUM_REQUESTS = sum(LIMITS.values())
NORMAL_MAX_TOKENS = 1024


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def snapshot(workspace, destination):
    """保留代码和全部项目资料；新增路径也进入独立观察。"""
    destination.mkdir(parents=True, exist_ok=False)
    names = []
    for source in sorted(workspace.rglob("*")):
        if source.is_symlink():
            raise ValueError(f"工作区出现链接，停止采集：{source}")
        if source.is_file():
            name = source.relative_to(workspace).as_posix()
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            names.append(name)
    save_json(destination / "files.json", names)


def request_events(directory):
    paths = list((directory / "runs").glob("*/events.jsonl"))
    if len(paths) > 1:
        raise ValueError("一个阶段产生了多个 run，停止采集")
    if not paths:
        return []
    raw = paths[0].read_bytes()
    # 正在追加的最后半行还不能证明事件已发布。
    return [json.loads(line) for line in raw.split(b"\n")[:-1] if line]


def stop_process(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def collect(command, directory, workspace, phase, driver, previous_calls):
    directory.mkdir(parents=True, exist_ok=False)
    invocation = {"argv": command, "cwd": str(Path(command[1]).parent), "phase": phase,
                  "started_at": time.time(), "evidence_source": "live-external-api",
                  "model_request_limit": LIMITS[phase], "previous_model_calls": previous_calls}
    process = subprocess.Popen(command, cwd=invocation["cwd"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    invocation["pid"] = process.pid
    save_json(directory / "invocation.json", invocation)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending, approvals, observed = bytearray(), [], []
    stop_reason = None
    deadline = time.monotonic() + 340
    try:
        while selector.get_map() or process.poll() is None:
            if stop_reason is None:
                calls = [event for event in request_events(directory) if event["event"] == "model_requested"]
                for event in calls[len(observed):]:
                    item = {"observed_at": time.time(), "seq": event["seq"], "turn": event["turn"],
                            "purpose": event["purpose"], "run_id": event["run_id"]}
                    observed.append(item)
                    with (directory / "request-observations.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                if len(calls) > LIMITS[phase] or previous_calls + len(calls) > MAXIMUM_REQUESTS:
                    stop_reason = "request_budget_exceeded"
                elif time.monotonic() >= deadline:
                    stop_reason = "supervisor_timeout"
                if stop_reason:
                    stop_process(process)
                    deadline = time.monotonic() + 5
            elif time.monotonic() >= deadline:
                break
            for key, _ in selector.select(timeout=0.1):
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
                    observation = driver.observe_proposal(proposal, directory, workspace)
                    decision = "approve" if phase == "B" and stop_reason is None else "deny"
                    item = {"time": time.time(), "proposal": proposal, "decision": decision,
                            "observation_directory": observation, "source": "experiment-driver",
                            "external_change_before_response": False}
                    approvals.append(item)
                    with (directory / "driver.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    process.stdin.write(f"{decision} {proposal['proposal_id']}\n".encode())
                    process.stdin.flush()
        process.wait(timeout=5)
        calls = [event for event in request_events(directory) if event["event"] == "model_requested"]
        for event in calls[len(observed):]:
            item = {"observed_at": time.time(), "seq": event["seq"], "turn": event["turn"],
                    "purpose": event["purpose"], "run_id": event["run_id"]}
            observed.append(item)
            with (directory / "request-observations.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        if len(calls) > LIMITS[phase] or previous_calls + len(calls) > MAXIMUM_REQUESTS:
            stop_reason = "request_budget_exceeded"
    finally:
        selector.close()
        stop_process(process)
        for handle in (process.stdin, process.stdout, process.stderr):
            handle.close()
        (directory / "stdout.json").write_bytes(buffers["stdout"])
        (directory / "stderr.txt").write_bytes(buffers["stderr"])
        (directory / "exit-code.txt").write_text(f"{process.returncode}\n")
        save_json(directory / "supervision.json", {"stop_reason": stop_reason,
                  "request_observations": observed, "approval_count": len(approvals)})
    result = json.loads(buffers["stdout"])
    if result.get("exit_code") != process.returncode:
        raise ValueError("CLI JSON 与实际退出码不同")
    return {**result, "phase": phase, "directory": str(directory), "pid": process.pid,
            "supervisor_timeout": stop_reason == "supervisor_timeout",
            "supervisor_stop_reason": stop_reason, "approval_count": len(approvals)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY") or not os.environ.get("DEEPSEEK_MODEL"):
        parser.error("请先在当前 shell 设置 DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL")
    entry, runtime = args.entry.resolve(strict=True), args.runtime.resolve(strict=True)
    worker = entry.parent / "runtime/worker.py"
    if not worker.is_file() or worker.read_bytes() != (MATERIALS / "runtime/worker.py").read_bytes():
        parser.error("工程目录尚未安装 W06 的 ASCII 检查 worker；先运行 prepare_project.py")
    previous = load_file("w06_previous_driver", W04 / "run_experiments.py")
    driver = previous.load_w03_driver()
    prepare = load_file("w06_workspace", MATERIALS / "prepare_workspace.py").prepare
    batch = args.output_dir.resolve() / f"batch-{uuid.uuid4().hex[:12]}"
    batch.mkdir(parents=True, exist_ok=False)
    workspace = prepare(batch / "workspace")
    session = batch / "session.jsonl"
    (batch / "inputs").mkdir()
    for source in (MATERIALS / "inputs").glob("*.md"):
        (batch / "inputs" / source.name).write_bytes(source.read_bytes())
    history = (MATERIALS / "inputs/historical-port-export.tsv").read_text(encoding="utf-8")
    (batch / "inputs/historical-port-export.tsv").write_text(history, encoding="utf-8")
    question_a = (batch / "inputs/question-a.md").read_text(encoding="utf-8")
    if question_a.count("{{HISTORICAL_EXPORT}}") != 1:
        raise ValueError("A 输入需要恰好一个历史清单位置")
    (batch / "inputs/question-a.md").write_text(question_a.replace("{{HISTORICAL_EXPORT}}", history), encoding="utf-8")
    (batch / "runtime-worker.py").write_bytes(worker.read_bytes())
    save_json(batch / "runtime.json", json.loads(runtime.read_text(encoding="utf-8")))
    save_json(batch / "experiment.json", {"evidence_source": "live-external-api", "limits": LIMITS,
              "context_tokens": CONTEXT_TOKENS, "maximum_model_requests": MAXIMUM_REQUESTS, "automatic_retries": 0,
              "normal_max_tokens": NORMAL_MAX_TOKENS, "summary_max_tokens": 1024, "read_view_lines": 80,
              "workspace": str(workspace), "session": str(session), "started_at": time.time()})
    rows, total = [], 0
    snapshot(workspace, batch / "initial")
    print(json.dumps({"batch_directory": str(batch), "maximum_model_requests": MAXIMUM_REQUESTS}, ensure_ascii=False), flush=True)
    try:
        for phase in LIMITS:
            if phase == "C":
                agents = workspace / "AGENTS.md"
                with agents.open("ab") as handle:
                    handle.write(b"\n" + (batch / "inputs/project-conflict.md").read_bytes())
            snapshot(workspace, batch / f"before-{phase}")
            before_session = session.read_bytes() if session.exists() else b""
            command = [args.python, str(entry), "new" if phase == "A" else "resume",
                       "--session", str(session), "--workspace", str(workspace),
                       "--question", str(batch / "inputs" / f"question-{phase.lower()}.md"),
                       "--output-dir", str(batch / phase / "runs"), "--runtime", str(runtime),
                       "--max-turns", str(LIMITS[phase]), "--max-actions", "16", "--max-tokens", str(NORMAL_MAX_TOKENS),
                       "--context-tokens", str(CONTEXT_TOKENS[phase]), "--read-view-lines", "80",
                       "--deadline-seconds", "300", "--command-timeout", "10", "--check-profile", "normal"]
            try:
                row = collect(command, batch / phase, workspace, phase, driver, total)
            finally:
                directory = batch / phase
                directory.mkdir(exist_ok=True)
                (directory / "before-session.jsonl").write_bytes(before_session)
                (directory / "after-session.jsonl").write_bytes(session.read_bytes() if session.exists() else b"")
                snapshot(workspace, batch / f"after-{phase}")
            rows.append(row)
            calls = row.get("model_calls")
            if type(calls) is not int or calls < 0 or calls > LIMITS[phase]:
                raise ValueError("请求计数无效或超过阶段上限；停止后续阶段")
            total += calls
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if row["status"] != "stop" or row["exit_code"] != 0 or row["supervisor_stop_reason"]:
                return 1
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        save_json(batch / "driver-error.json", {"class": type(error).__name__, "message": str(error),
                                               "completed_phases": [r["phase"] for r in rows]})
        raise
    finally:
        save_json(batch / "summary.json", rows)


if __name__ == "__main__":
    raise SystemExit(main())
