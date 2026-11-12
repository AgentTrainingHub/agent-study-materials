"""采集真实模型的观察、续聊修复与旧位置分支；本脚本不判定答案语义。"""
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
W03 = MATERIALS.parents[2] / "week-03/practices/PRA-W03"
LIMITS = {"A": 4, "B": 8, "C": 4}
FILES = ("validator.py", "test_validator.py")


def load_w03_driver():
    # 复用 W03 对实际候选字节的独立观察；不导入被测工程。
    sys.path.insert(0, str(W03))
    try:
        spec = importlib.util.spec_from_file_location("w03_experiment_driver", W03 / "run_experiments.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def save_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def snapshot(workspace, destination):
    destination.mkdir(parents=True, exist_ok=False)
    present = []
    for name in FILES:
        source = workspace / name
        if source.exists():
            (destination / name).write_bytes(source.read_bytes())
            present.append(name)
    save_json(destination / "files.json", present)


def stop_process(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def collect(command, directory, workspace, *, phase=None, driver=None):
    """每次调用创建新进程，保存实际 stdout、退出码与宿主审批观察。"""
    directory.mkdir(parents=True, exist_ok=False)
    invocation = {"argv": command, "cwd": str(Path(command[1]).parent),
                  "phase": phase, "started_at": time.time(),
                  "evidence_source": "live-external-api" if phase else "local-session-command",
                  "model_request_limit": LIMITS.get(phase, 0)}
    process = subprocess.Popen(command, cwd=invocation["cwd"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    invocation["pid"] = process.pid
    save_json(directory / "invocation.json", invocation)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending = bytearray()
    approvals = []
    supervisor_timeout = False
    deadline = time.monotonic() + (340 if phase else 30)
    try:
        while selector.get_map() or process.poll() is None:
            if time.monotonic() >= deadline and not supervisor_timeout:
                supervisor_timeout = True
                stop_process(process)
                deadline = time.monotonic() + 5
            elif supervisor_timeout and time.monotonic() >= deadline:
                break
            for key, _ in selector.select(timeout=0.2):
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
                    if not phase:
                        raise ValueError("本地会话命令意外请求文件审批")
                    proposal = json.loads(line[len(b"W03_APPROVAL "):])
                    observation = driver.observe_proposal(proposal, directory, workspace)
                    decision = "approve" if phase == "B" else "deny"
                    item = {"time": time.time(), "proposal": proposal, "decision": decision,
                            "observation_directory": observation, "source": "experiment-driver",
                            "external_change_before_response": False}
                    approvals.append(item)
                    with (directory / "driver.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    process.stdin.write(f"{decision} {proposal['proposal_id']}\n".encode())
                    process.stdin.flush()
        process.wait(timeout=5)
    finally:
        selector.close()
        stop_process(process)
        for handle in (process.stdin, process.stdout, process.stderr):
            handle.close()
        (directory / "stdout.json").write_bytes(buffers["stdout"])
        (directory / "stderr.txt").write_bytes(buffers["stderr"])
        (directory / "exit-code.txt").write_text(f"{process.returncode}\n")
    result = json.loads(buffers["stdout"])
    if result.get("exit_code") != process.returncode:
        raise ValueError("CLI JSON 与实际退出码不同")
    return {"phase": phase, "directory": str(directory), "pid": process.pid,
            "supervisor_timeout": supervisor_timeout, "approval_count": len(approvals), **result}


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
    driver = load_w03_driver()
    batch = args.output_dir.resolve() / f"batch-{uuid.uuid4().hex[:12]}"
    batch.mkdir(parents=True, exist_ok=False)
    workspace = driver.prepare(batch / "workspace")
    session = batch / "session.jsonl"
    token = uuid.uuid4().hex
    expected = {"evidence_source": "experiment-driver-observations", "workspace": str(workspace),
                "session": str(session), "ui_marker": f"W04_UI_ONLY_{token}",
                "external_comment": f"# W04_EXTERNAL_CURRENT_{token}",
                "limits": LIMITS, "phases": {}, "controls": {}}
    snapshot(workspace, batch / "initial")
    rows = []

    def local(name, command, *arguments):
        directory = batch / name
        before = session.read_bytes()
        row = collect([args.python, str(entry), command, "--session", str(session), *arguments],
                      directory, workspace)
        (directory / "before-session.jsonl").write_bytes(before)
        (directory / "after-session.jsonl").write_bytes(session.read_bytes())
        expected["controls"][name] = {"directory": str(directory), "pid": row["pid"]}
        if row["exit_code"] or row["supervisor_timeout"]:
            raise ValueError(f"{command} 未完成；停止后续实验，不自动重试")
        return row

    try:
        print(json.dumps({"batch_directory": str(batch), "maximum_model_requests": 16}, ensure_ascii=False), flush=True)
        for phase in LIMITS:
            if phase == "B":
                local("note", "note", "--text", expected["ui_marker"])
                local("inspect-visible", "inspect")
                local("inspect-hidden", "inspect", "--hide-ui")
            elif phase == "C":
                local("branch", "branch", "--from-entry", rows[0]["head_id"])
                snapshot(workspace, batch / "before-external")
                path = workspace / "validator.py"
                with path.open("ab") as handle:
                    handle.write(("\n" + expected["external_comment"] + "\n").encode("utf-8"))
                snapshot(workspace, batch / "after-external")
            snapshot(workspace, batch / f"before-{phase}")
            command = [args.python, str(entry), "new" if phase == "A" else "resume",
                       "--session", str(session), "--workspace", str(workspace),
                       "--question", str(MATERIALS / "inputs" / f"question-{phase.lower()}.txt"),
                       "--output-dir", str(batch / phase / "runs"), "--runtime", str(runtime),
                       "--max-turns", str(LIMITS[phase]), "--max-actions", "16", "--max-tokens", "2048",
                       "--deadline-seconds", "300", "--command-timeout", "5", "--check-profile", "normal"]
            row = collect(command, batch / phase, workspace, phase=phase, driver=driver)
            snapshot(workspace, batch / f"after-{phase}")
            if session.exists():
                (batch / phase / "after-session.jsonl").write_bytes(session.read_bytes())
            rows.append(row)
            expected["phases"][phase] = {key: row.get(key) for key in ("pid", "run_id", "head_id", "session_id")}
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if row["status"] != "stop" or row["exit_code"] != 0 or row["supervisor_timeout"]:
                return 1
        local("inspect-final", "inspect")
        return 0
    finally:
        save_json(batch / "summary.json", rows)
        save_json(batch / "expected-state.json", expected)


if __name__ == "__main__":
    raise SystemExit(main())
