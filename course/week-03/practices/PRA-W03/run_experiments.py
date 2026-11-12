"""采集真实模型四组实验；退出 0 表示采集完成，验收另用 inspect_evidence.py。"""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
import uuid
from pathlib import Path

from prepare_workspace import prepare

CASES = {"normal": 8, "denied": 4, "stale": 4, "timeout": 8}
MARKER = b"W03_APPROVAL "


def save_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def observe_proposal(marker: dict, directory: Path, workspace: Path) -> str:
    """读完当前候选再批准，独立副本保留驱动实际看到的内容。"""
    identity = marker["proposal_id"]
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity):
        raise ValueError("CLI 返回的 proposal_id 无效")
    proposal_dir = Path(marker["proposal_dir"]).resolve(strict=True)
    if not proposal_dir.is_relative_to((directory / "runs").resolve()):
        raise ValueError("审批证据必须位于本次 runs 目录")
    raw = (proposal_dir / "proposal.json").read_bytes()
    record = json.loads(raw)
    for field in ("proposal_id", "path", "tool_name"):
        if marker[field] != record[field]:
            raise ValueError(f"审批提示与候选记录的 {field} 不一致")
    if record["path"] not in {"validator.py", "test_validator.py"}:
        raise ValueError("候选目标超出练习范围")
    before = (proposal_dir / "before.bin").read_bytes() if record["before_exists"] else None
    after = (proposal_dir / "after.bin").read_bytes()
    diff = (proposal_dir / "diff.patch").read_bytes()
    target = workspace / record["path"]
    current = target.read_bytes() if target.exists() else None
    if current != before or diff.decode("utf-8") != record["diff"]:
        raise ValueError("候选版本或预览与实际证据不一致")
    args = record["args"]
    if args["path"] != record["path"]:
        raise ValueError("候选参数指向不同目标")
    if record["tool_name"] == "edit":
        old, new = args["old_text"].encode("utf-8"), args["new_text"].encode("utf-8")
        start = before.find(old) if before is not None and old else -1
        if start < 0 or before.find(old, start + 1) >= 0:
            raise ValueError("批准前未找到唯一非空替换位置")
        expected = before[:start] + new + before[start + len(old):]
    elif record["tool_name"] == "write" and before is None:
        expected = args["content"].encode("utf-8")
    else:
        raise ValueError("候选工具或新建前提无效")
    if after != expected:
        raise ValueError("候选字节与本笔参数不一致")
    observation = directory / "driver-observations" / identity
    observation.mkdir(parents=True, exist_ok=False)
    (observation / "proposal.json").write_bytes(raw)
    if before is not None:
        (observation / "before.bin").write_bytes(before)
    (observation / "after.bin").write_bytes(after)
    (observation / "diff.patch").write_bytes(diff)
    return str(observation)


def run_case(entry: Path, runtime: Path, batch: Path, case: str, python: str) -> dict:
    directory = batch / case
    directory.mkdir()
    workspace = prepare(directory / "workspace")
    before = (workspace / "validator.py").read_bytes()
    (directory / "before-validator.py").write_bytes(before)
    materials = Path(__file__).resolve().parent
    command = [python, str(entry), "--workspace", str(workspace), "--question",
               str(materials / "inputs/question.txt"), "--output-dir", str(directory / "runs"),
               "--runtime", str(runtime), "--max-turns", str(CASES[case]),
               "--max-actions", "16", "--max-tokens", "2048", "--deadline-seconds", "300",
               "--command-timeout", "5", "--check-profile", "slow" if case == "timeout" else "normal"]
    save_json(directory / "invocation.json", {"argv": command, "cwd": str(entry.parent),
              "case": case, "approval_source": "experiment-driver", "model_request_limit": CASES[case],
              "evidence_source": "live-external-api", "started_at": time.time()})
    process = subprocess.Popen(command, cwd=entry.parent, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending_stderr = bytearray()
    approvals = []
    external_change = False
    stale_wrong_first = False
    supervisor_timeout = False
    limit = time.monotonic() + 340
    try:
        while selector.get_map():
            if time.monotonic() >= limit:
                supervisor_timeout = True
                process.send_signal(2)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                break
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(chunk)
                if key.data != "stderr":
                    continue
                pending_stderr.extend(chunk)
                while b"\n" in pending_stderr:
                    line, _, remainder = pending_stderr.partition(b"\n")
                    pending_stderr[:] = remainder
                    if not line.startswith(MARKER):
                        continue
                    proposal = json.loads(line[len(MARKER):])
                    proposal_id = proposal["proposal_id"]
                    observation = observe_proposal(proposal, directory, workspace)
                    first = not approvals
                    decision = "approve"
                    if proposal.get("path") not in {"validator.py", "test_validator.py"}:
                        decision = "deny"
                    if case == "denied":
                        decision = "deny"
                    if case == "stale" and first:
                        if proposal.get("tool_name") != "edit" or proposal.get("path") != "validator.py":
                            decision = "deny"
                            stale_wrong_first = True
                        else:
                            current = (workspace / "validator.py").read_bytes()
                            changed = current + b"\n# external change during approval\n"
                            (directory / "external-before.py").write_bytes(current)
                            (workspace / "validator.py").write_bytes(changed)
                            (directory / "external-after.py").write_bytes(changed)
                            external_change = True
                    item = {"time": time.time(), "proposal": proposal, "decision": decision,
                            "observation_directory": observation,
                            "source": "experiment-driver", "external_change_before_response": external_change}
                    approvals.append(item)
                    with (directory / "driver.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    process.stdin.write(f"{decision} {proposal_id}\n".encode())
                    process.stdin.flush()
        returncode = process.wait(timeout=10)
    finally:
        selector.close()
        if process.poll() is None:
            process.send_signal(2)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for handle in (process.stdin, process.stdout, process.stderr):
            handle.close()
        (directory / "stdout.json").write_bytes(buffers["stdout"])
        (directory / "stderr.txt").write_bytes(buffers["stderr"])
        (directory / "exit-code.txt").write_text(str(process.returncode) + "\n")
    result = json.loads(buffers["stdout"])
    assert result["exit_code"] == returncode, "stdout 与实际退出码不一致"
    after = (workspace / "validator.py").read_bytes()
    (directory / "after-validator.py").write_bytes(after)
    row = {"case": case, "directory": str(directory), "workspace": str(workspace),
           "approval_source": "experiment-driver", "approval_count": len(approvals),
           "external_change": external_change, "stale_wrong_first": stale_wrong_first,
           "supervisor_timeout": supervisor_timeout, "source_changed": before != after,
           **{name: result.get(name) for name in ("run_id", "output_directory", "evidence_source",
               "status", "exit_code", "model_calls", "mutation_successes", "usage", "task_verdict", "error")}}
    save_json(directory / "experiment.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-point", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), default=list(CASES))
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    if len(set(args.cases)) != len(args.cases):
        parser.error("同一批次不得重复 case")
    if not os.environ.get("DEEPSEEK_API_KEY") or not os.environ.get("DEEPSEEK_MODEL"):
        parser.error("请先设置 DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL")
    entry = args.entry_point.resolve(strict=True)
    runtime = args.runtime.resolve(strict=True)
    batch = args.output_dir.resolve() / f"batch-{uuid.uuid4().hex[:12]}"
    batch.mkdir(parents=True, exist_ok=False)
    print(json.dumps({"batch_directory": str(batch), "cases": args.cases,
                      "maximum_model_requests": sum(CASES[name] for name in args.cases)}, ensure_ascii=False), flush=True)
    rows = []
    for case in args.cases:
        row = run_case(entry, runtime, batch, case, args.python)
        rows.append(row)
        (batch / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if row["status"] in {"blocked", "error", "deadline_exceeded"} or row["supervisor_timeout"]:
            print("依赖、传输或总期限受阻；停止后续真实组，不自动重试。", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
