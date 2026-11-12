"""从 CLI 和实际传输边界检查 W02；所有模型响应均为明确标记的受控输入。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
SENTINEL = "w02-controlled-secret-do-not-log"
USAGE = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
SOURCE = "controlled-fixture"


def call(call_id="read-1", name="read", arguments=None):
    if arguments is None:
        arguments = {"path": "validator.py"}
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def response(text="必须为十进制数字；取值 1～65535，依据第 2～6 行。", *, calls=None, finish=None):
    message = {"role": "assistant", "content": text if calls is None else None}
    if calls is not None:
        message["tool_calls"] = calls
    return {
        "id": "controlled-response",
        "model": "controlled-actual-model",
        "choices": [{"message": message, "finish_reason": finish or ("tool_calls" if calls is not None else "stop")}],
        "usage": dict(USAGE),
    }


WORKER = r'''
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

scenario_path, audit_path = map(Path, sys.argv[1:3])
incoming = json.load(sys.stdin)
scenario = json.loads(scenario_path.read_text())
rows = audit_path.read_bytes().splitlines() if audit_path.exists() else []
index = len(rows)
if index >= len(scenario):
    raise RuntimeError("受控响应已用完；CLI 可能产生了额外请求")
item = scenario[index]
with audit_path.open("a") as target:
    target.write(json.dumps({"request": incoming["request"], "pid": os.getpid(),
                             "index": index, "started": time.monotonic()}, ensure_ascii=False) + "\n")
    target.flush()
time.sleep(item.get("delay", 0))
now = datetime.now(timezone.utc).isoformat()
exchange = {
    "request_payload": incoming["request"],
    "raw_response": item.get("raw"),
    "has_response": "raw" in item,
    "http_status": item.get("http_status", 200 if "raw" in item else None),
    "request_id": "controlled-request-" + str(index + 1),
    "error": item.get("error"),
    "started_at": now, "finished_at": now, "elapsed_ms": 1,
}
print(json.dumps(exchange, ensure_ascii=False).replace(incoming["config"]["api_key"], "[REDACTED]"))
'''


WRAPPER = r'''
import json, os, runpy, socket, sys, threading, time
from pathlib import Path
from unittest.mock import patch

project, worker, scenario, audit, reads, options = map(Path, sys.argv[1:7])
settings = json.loads(options.read_text())
sys.path.insert(0, str(project))
import w02_support

original_read = w02_support._read_bytes
original_safe_read = w02_support.safe_read
intervals = reads.with_name("read-interval-audit.jsonl")
observation_lock = threading.Lock()
active = {"safe_read": 0, "body_read": 0}
next_invocation = 0

def observe(boundary, phase, invocation=None):
    global next_invocation
    # 锁只保护审计记录；文件读取发生在锁外，不能由检查器把并发实现串行化。
    with observation_lock:
        if phase == "enter":
            next_invocation += 1
            invocation = f"{os.getpid()}:{next_invocation}"
            active[boundary] += 1
        else:
            active[boundary] -= 1
        with intervals.open("a") as target:
            target.write(json.dumps({"boundary": boundary, "phase": phase,
                                     "invocation": invocation, "pid": os.getpid(),
                                     "thread": threading.get_ident(), "time_ns": time.monotonic_ns(),
                                     "active": active[boundary]}) + "\n")
    return invocation

def observed_safe_read(workspace, path, offset=1, limit=40):
    invocation = observe("safe_read", "enter")
    try:
        return original_safe_read(workspace, path, offset, limit)
    finally:
        observe("safe_read", "exit", invocation)

def observed_read(fd, maximum):
    invocation = observe("body_read", "enter")
    try:
        with observation_lock:
            with reads.open("a") as target:
                target.write(json.dumps({"fd": fd, "maximum": maximum, "time": time.monotonic()}) + "\n")
        if settings.get("read_delay"):
            time.sleep(settings["read_delay"])
        return original_read(fd, maximum)
    finally:
        observe("body_read", "exit", invocation)

def network_forbidden(*args, **kwargs):
    raise AssertionError("受控检查禁止真实网络")

sys.argv = [str(project / "main.py"), *settings["argv"]]
with patch.object(w02_support, "_provider_command", return_value=[sys.executable, str(worker), str(scenario), str(audit)]), \
     patch.object(w02_support, "EVIDENCE_SOURCE", "controlled-fixture"), \
     patch.object(w02_support, "safe_read", side_effect=observed_safe_read), \
     patch.object(w02_support, "_read_bytes", side_effect=observed_read), \
     patch.object(socket.socket, "connect", side_effect=network_forbidden):
    runpy.run_path(str(project / "main.py"), run_name="__main__")
'''


HELPER_WRAPPER = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import w02_support as support
job = json.loads(Path(sys.argv[2]).read_text())
try:
    if job["operation"] == "transcript":
        value = support.validate_transcript(job["messages"])
    elif job["operation"] == "read":
        value = support.safe_read(Path(job["workspace"]), **job["arguments"])
    else:
        raise ValueError("未知检查操作")
    print(json.dumps({"accepted": True, "value": value}, ensure_ascii=False))
except (support.ProtocolError, support.ToolError) as error:
    print(json.dumps({"accepted": False, "error_class": error.error_class, "message": str(error)}, ensure_ascii=False))
'''


class CliContractTests(unittest.TestCase):
    project: Path
    output_root: Path

    def case_dir(self):
        directory = self.output_root / f"{self._testMethodName}-{uuid.uuid4().hex[:8]}"
        directory.mkdir(parents=True)
        return directory

    def run_cli(self, raws=None, *, scenario=None, environment=None, max_turns=4,
                deadline=180, feedback="full", output_dir=None, question=None,
                read_delay=0, workspace_setup=None, max_tokens=1024):
        directory = self.case_dir()
        workspace = directory / "workspace"
        shutil.copytree(MATERIALS / "inputs/sample_project", workspace)
        if workspace_setup:
            workspace_setup(workspace)
        before = {str(p.relative_to(workspace)): p.read_bytes()
                  for p in workspace.rglob("*") if p.is_file() and not p.is_symlink()}
        question_file = directory / "question.txt"
        question_file.write_text(question if question is not None else (MATERIALS / "inputs/question.txt").read_text(), encoding="utf-8")
        worker = directory / "controlled_worker.py"
        worker.write_text(WORKER, encoding="utf-8")
        wrapper = directory / "wrapper.py"
        wrapper.write_text(WRAPPER, encoding="utf-8")
        scenario_file = directory / "scenario.json"
        if scenario is None:
            scenario = [{"raw": raw} for raw in (raws if raws is not None else [response(calls=[call()]), response()])]
        scenario_file.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        audit = directory / "transport-audit.jsonl"
        reads = directory / "body-read-audit.jsonl"
        options = directory / "options.json"
        options.write_text(json.dumps({"read_delay": read_delay, "argv": [
            "--workspace", str(workspace), "--question", str(question_file),
            "--output-dir", str(output_dir or directory / "runs"),
            "--max-turns", str(max_turns), "--max-tokens", str(max_tokens),
            "--deadline-seconds", str(deadline), "--feedback-mode", feedback,
        ]}), encoding="utf-8")
        env = {
            "DEEPSEEK_API_KEY": SENTINEL, "DEEPSEEK_MODEL": "controlled-request-model",
            "DEEPSEEK_TIMEOUT_SECONDS": "60", "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(environment or {})
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, str(wrapper), str(self.project), str(worker), str(scenario_file),
             str(audit), str(reads), str(options)],
            capture_output=True, text=True, env=env, timeout=12,
            cwd=str(directory),
        )
        elapsed = time.monotonic() - started
        self.assertNotIn(SENTINEL, completed.stdout + completed.stderr, "stdout/stderr 泄漏凭据")
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        try:
            output = json.loads(completed.stdout)
        except json.JSONDecodeError:
            self.fail(f"stdout 必须只含一个 JSON 对象：{completed.stdout!r}; stderr={completed.stderr!r}")
        self.assertEqual(completed.returncode, output["exit_code"])
        run_dir = Path(output["output_directory"])
        self.assertEqual(run_dir.name, output["run_id"])
        self.assertEqual(json.loads((run_dir / "result.json").read_text()), output)
        self.assertEqual(output["evidence_source"], SOURCE)
        self.assertEqual(output["verdict"], output["task_verdict"])
        self.assertIn(output["task_verdict"], {"fail", "unevaluated"}, "机械检查不能代替人工答案核验")
        events = [json.loads(line) for line in (run_dir / "events.jsonl").read_bytes().splitlines()]
        self.assertEqual(events[0]["event"], "run_started")
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        for event in events:
            self.assertEqual(event["run_id"], output["run_id"])
            self.assertEqual(event["evidence_source"], SOURCE)
            self.assertIn("turn", event)
            self.assertTrue(event["time"])
        observed = [json.loads(line) for line in audit.read_bytes().splitlines()] if audit.exists() else []
        body_reads = [json.loads(line) for line in reads.read_bytes().splitlines()] if reads.exists() else []
        interval_file = directory / "read-interval-audit.jsonl"
        intervals = [json.loads(line) for line in interval_file.read_bytes().splitlines()] if interval_file.exists() else []
        self.assertEqual(output["model_calls"], len(observed))
        for index, row in enumerate(observed, 1):
            turn_dir = run_dir / f"turn-{index:03d}"
            stored = json.loads((turn_dir / "request.json").read_text())
            self.assertEqual(stored, self.clean(row["request"]), "保存请求与实际传输 payload 不一致")
            receipt = json.loads((turn_dir / "receipt.json").read_text())
            self.assertIn("actual_model", receipt)
            self.assertIn("request_id", receipt)
            self.assertIn("finish_reason", receipt)
            self.assertIn("usage", receipt)
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(SENTINEL, path.read_text(), f"凭据泄漏到 {path.name}")
        after = {str(p.relative_to(workspace)): p.read_bytes()
                 for p in workspace.rglob("*") if p.is_file() and not p.is_symlink()}
        self.assertEqual(before, after, "只读工具修改了工作区")
        evidence = {
            "evidence_source": SOURCE, "execution": "ran", "coverage": "hit",
            "exit_code": completed.returncode, "elapsed_seconds": elapsed,
            "stdout": output, "stderr": completed.stderr,
            "observed_requests": len(observed), "actual_body_reads": len(body_reads),
            "max_active_reads": {boundary: max((item["active"] for item in intervals if item["boundary"] == boundary), default=0)
                                 for boundary in ("safe_read", "body_read")},
        }
        (directory / "check-observation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        return {"code": completed.returncode, "output": output, "events": events,
                "observed": observed, "reads": body_reads, "read_intervals": intervals, "directory": directory,
                "run_dir": run_dir, "elapsed": elapsed}

    @staticmethod
    def clean(value):
        if isinstance(value, str):
            return value.replace(SENTINEL, "[REDACTED]")
        if isinstance(value, dict):
            return {k: CliContractTests.clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [CliContractTests.clean(v) for v in value]
        return value

    def helper(self, job):
        directory = self.case_dir()
        wrapper = directory / "helper-wrapper.py"
        wrapper.write_text(HELPER_WRAPPER)
        options = directory / "job.json"
        options.write_text(json.dumps(job))
        completed = subprocess.run([sys.executable, str(wrapper), str(self.project), str(options)],
                                   text=True, capture_output=True, timeout=5,
                                   env={"PYTHONDONTWRITEBYTECODE": "1"}, cwd=directory)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        (directory / "helper-observation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result

    def tool_messages(self, result, request_index=1):
        return [m for m in result["observed"][request_index]["request"]["messages"] if m["role"] == "tool"]

    def finished(self, result):
        return [event for event in result["events"] if event["event"] == "tool_finished"]

    def test_normal_read_actual_payload_and_unique_runs(self):
        shared = self.output_root / "same-output-root"
        result = self.run_cli(output_dir=shared)
        self.assertEqual((result["code"], result["output"]["status"]), (0, "stop"))
        self.assertEqual(result["output"]["task_verdict"], "unevaluated")
        self.assertEqual((result["output"]["read_successes"], result["output"]["visible_read_successes"]), (1, 1))
        self.assertEqual(result["output"]["coverage"], "hit")
        self.assertEqual(result["output"]["execution"], "ran")
        self.assertEqual(len(result["reads"]), 1)
        initial = result["observed"][0]["request"]
        self.assertEqual(initial["model"], "controlled-request-model")
        self.assertEqual(initial["temperature"], 0)
        self.assertIs(initial["stream"], False)
        self.assertEqual(initial["thinking"], {"type": "disabled"})
        self.assertEqual(initial["max_tokens"], 1024)
        self.assertEqual([t["function"]["name"] for t in initial["tools"]], ["read"])
        self.assertNotIn("return port", json.dumps(initial["messages"]))
        messages = self.tool_messages(result)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["tool_call_id"], "read-1")
        payload = json.loads(messages[0]["content"])
        self.assertTrue(payload["ok"])
        self.assertIn("65535", json.dumps(payload))
        self.assertEqual(payload["lines"][0]["line"], 1)
        first_files = {str(p.relative_to(result["run_dir"])): p.read_bytes()
                       for p in result["run_dir"].rglob("*") if p.is_file()}
        again = self.run_cli(output_dir=shared)
        self.assertNotEqual(result["output"]["run_id"], again["output"]["run_id"])
        self.assertEqual(len(list(shared.iterdir())), 2)
        self.assertEqual(first_files, {str(p.relative_to(result["run_dir"])): p.read_bytes()
                                     for p in result["run_dir"].rglob("*") if p.is_file()})

    def test_omit_first_keeps_protocol_and_allows_reread(self):
        result = self.run_cli([response(calls=[call("first")]), response(calls=[call("second")]), response()], feedback="omit-first")
        self.assertEqual(result["code"], 0)
        first = self.tool_messages(result, 1)[0]
        self.assertEqual(first["tool_call_id"], "first")
        self.assertNotIn("65535", first["content"])
        self.assertNotIn("return port", first["content"])
        projected = [e for e in result["events"] if e["event"] == "feedback_projected"]
        self.assertEqual([e["omitted"] for e in projected], [True, False])
        self.assertEqual(projected[0]["message"], first)
        actual = self.finished(result)[0]["result"]
        self.assertIn("65535", json.dumps(actual))
        second = self.tool_messages(result, 2)[-1]
        self.assertIn("65535", second["content"])
        self.assertEqual(result["output"]["read_successes"], 2)
        self.assertEqual(result["output"]["visible_read_successes"], 1)
        self.assertEqual(result["output"]["task_verdict"], "unevaluated")

    def test_omitted_only_observation_cannot_claim_task_success(self):
        result = self.run_cli(feedback="omit-first")
        self.assertEqual((result["code"], result["output"]["status"]), (0, "stop"))
        self.assertEqual(result["output"]["read_successes"], 1)
        self.assertEqual(result["output"]["visible_read_successes"], 0)
        self.assertEqual(result["output"]["task_verdict"], "fail")

    def test_unknown_tool_is_paired_error_and_loop_continues(self):
        result = self.run_cli([response(calls=[call(name="write")]), response()])
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["reads"]), 0)
        self.assertFalse(result["read_intervals"], "未知工具进入了底层读取")
        self.assertFalse(self.finished(result)[0]["executed"])
        feedback = self.tool_messages(result)[0]
        self.assertEqual(feedback["tool_call_id"], "read-1")
        self.assertFalse(json.loads(feedback["content"])["ok"])
        self.assertEqual(json.loads(feedback["content"])["error"]["class"], "unknown_tool")
        self.assertEqual(result["output"]["task_verdict"], "fail")

    def test_bad_arguments_and_paths_execute_no_file_read(self):
        cases = ["{", "[]", {}, {"path": 12}, {"path": "validator.py", "offset": True},
                 {"path": "validator.py", "limit": 0}, {"path": "validator.py", "limit": 81},
                 {"path": "validator.py", "extra": 1}, {"path": "../validator.py"},
                 {"path": "/etc/passwd"}, {"path": "validator.py", "offset": 0},
                 {"path": "validator.py", "offset": "1"}, {"path": "validator.py", "limit": True},
                 {"path": "validator.py", "offset": 1.5},
                 '{"path":"validator.py","offset":' + "9" * 5000 + "}",
                 "[" * 10000 + "0" + "]" * 10000, '{"path":"\\ud800"}']
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli([response(calls=[call(arguments=arguments)]), response()])
                self.assertEqual(result["code"], 0)
                self.assertEqual(len(result["reads"]), 0)
                self.assertFalse(result["read_intervals"], "非法参数必须在 safe_read 调用前拒绝")
                self.assertEqual(len(self.finished(result)), 1)
                self.assertIs(self.finished(result)[0]["executed"], False)
                feedback = self.tool_messages(result)[0]
                self.assertEqual(feedback["tool_call_id"], "read-1")
                self.assertFalse(json.loads(feedback["content"])["ok"])
                expected = "path_denied" if isinstance(arguments, dict) and arguments.get("path") in {
                    "../validator.py", "/etc/passwd"
                } else "invalid_arguments"
                self.assertEqual(json.loads(feedback["content"])["error"]["class"], expected)

    def test_missing_file_error_returns_to_model(self):
        result = self.run_cli([response(calls=[call(arguments={"path": "missing.py"})]), response()])
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["reads"]), 0)
        feedback = json.loads(self.tool_messages(result)[0]["content"])
        self.assertFalse(feedback["ok"])
        self.assertEqual(feedback["error"]["class"], "file_not_found")
        self.assertEqual(len([item for item in result["read_intervals"]
                              if item["boundary"] == "safe_read" and item["phase"] == "enter"]), 1)
        self.assertIs(self.finished(result)[0]["executed"], True)
        self.assertEqual(result["output"]["read_successes"], 0)

    def test_length_keeps_ids_but_executes_zero_tools(self):
        result = self.run_cli([response(calls=[call("truncated-a"), call("truncated-b")], finish="length")])
        self.assertEqual((result["code"], result["output"]["status"]), (1, "length"))
        self.assertEqual(result["output"]["error"]["class"], "output_truncated")
        self.assertEqual(len(result["reads"]), 0)
        self.assertEqual(len(result["observed"]), 1)
        self.assertFalse(any(e["event"] == "tool_started" for e in result["events"]))
        finished = self.finished(result)
        self.assertEqual([e["call_id"] for e in finished], ["truncated-a", "truncated-b"])
        self.assertTrue(all(e["executed"] is False and e["result"]["ok"] is False for e in finished))
        for event in finished:
            self.assertEqual(event["local_message"]["tool_call_id"], event["call_id"])
            self.assertEqual(json.loads(event["local_message"]["content"]), event["result"])
            self.assertEqual(event["result"]["error"]["class"], "output_truncated")

    def test_last_model_turn_does_not_execute_unreturnable_tool(self):
        result = self.run_cli([response(calls=[call()])], max_turns=1)
        self.assertEqual((result["code"], result["output"]["status"]), (1, "turn_limit"))
        self.assertEqual(len(result["reads"]), 0)
        self.assertEqual(len(result["observed"]), 1)
        self.assertEqual([event["call_id"] for event in self.finished(result)], ["read-1"])
        self.assertTrue(all(e["executed"] is False for e in self.finished(result)))
        event = self.finished(result)[0]
        self.assertEqual(event["local_message"]["tool_call_id"], "read-1")
        self.assertEqual(json.loads(event["local_message"]["content"]), event["result"])
        self.assertEqual(event["result"]["error"]["class"], "turn_limit")
        loop = self.run_cli([response(calls=[call("a")]), response(calls=[call("b")])], max_turns=2)
        self.assertEqual(loop["output"]["status"], "turn_limit")
        self.assertEqual(len(loop["reads"]), 1)
        self.assertEqual(len(loop["observed"]), 2)

    def test_invalid_ids_and_oversized_batch_rejected_before_execution(self):
        missing = call()
        del missing["id"]
        for calls in ([call("dup"), call("dup")], [call("")], [missing], [call(str(i)) for i in range(9)]):
            with self.subTest(calls=calls):
                result = self.run_cli([response(calls=calls)])
                self.assertEqual((result["code"], result["output"]["status"]), (1, "error"))
                self.assertEqual(len(result["reads"]), 0)
                self.assertEqual(len(result["observed"]), 1)
        repeated = self.run_cli([response(calls=[call("same")]), response(calls=[call("same")])])
        self.assertEqual((repeated["code"], repeated["output"]["status"]), (1, "error"))
        self.assertEqual(len(repeated["reads"]), 1, "历史调用 ID 重复时执行了新工具")

    def test_multicall_batch_is_serial_and_fully_paired(self):
        calls = [call("a", arguments={"path": "validator.py", "offset": 1, "limit": 2}),
                 call("b", name="unknown"), call("c", arguments={"path": "validator.py", "offset": 3, "limit": 2})]
        result = self.run_cli([response(calls=calls), response()], read_delay=0.1)
        self.assertEqual(result["code"], 0)
        # 读取边界由检查器记录，独立于自行写出的 tool_started/tool_finished。
        for boundary in ("safe_read", "body_read"):
            observed = sorted((item for item in result["read_intervals"] if item["boundary"] == boundary),
                              key=lambda item: item["time_ns"])
            self.assertEqual([item["phase"] for item in observed], ["enter", "exit", "enter", "exit"],
                             f"{boundary} 实际调用发生重叠或缺少完整进入/退出证据")
            self.assertEqual([item["active"] for item in observed], [1, 0, 1, 0],
                             f"{boundary} 出现多个同时活动的调用")
            for enter, leave in zip(observed[::2], observed[1::2]):
                self.assertEqual((enter["invocation"], enter["pid"], enter["thread"]),
                                 (leave["invocation"], leave["pid"], leave["thread"]))
                self.assertLess(enter["time_ns"], leave["time_ns"])
        messages = self.tool_messages(result)
        self.assertEqual([m["tool_call_id"] for m in messages], ["a", "b", "c"])
        self.assertEqual([json.loads(m["content"])["ok"] for m in messages], [True, False, True])
        self.assertEqual(len(result["reads"]), 2)
        finished = self.finished(result)
        self.assertEqual([e["call_id"] for e in finished], ["a", "b", "c"])
        pending = None
        for event in result["events"]:
            if event["event"] == "tool_started":
                self.assertIsNone(pending, "同批工具出现了重叠执行")
                pending = event["call_id"]
            elif event["event"] == "tool_finished" and event["executed"]:
                self.assertEqual(pending, event["call_id"])
                pending = None
        self.assertIsNone(pending)

    def test_stop_without_read_is_generation_success_and_task_failure(self):
        result = self.run_cli([response()])
        self.assertEqual((result["code"], result["output"]["status"]), (0, "stop"))
        self.assertEqual(result["output"]["task_verdict"], "fail")
        self.assertEqual(result["output"]["coverage"], "not_covered")
        self.assertEqual(result["output"]["read_successes"], 0)
        self.assertEqual(len(result["reads"]), 0)

    def test_invalid_response_and_finish_reason_never_execute(self):
        cases = [[], {}, {"model": "x", "choices": []}, response(text=""), response(text=None),
                 response(finish="new_reason"), response(calls=[], finish="tool_calls"),
                 response(calls=[call()], finish="stop")]
        function_call = response()
        function_call["choices"][0]["message"]["function_call"] = {"name": "read", "arguments": "{}"}
        cases.append(function_call)
        missing_model = response()
        missing_model["model"] = ""
        cases.append(missing_model)
        for raw in cases:
            with self.subTest(raw=raw):
                result = self.run_cli([raw])
                self.assertEqual((result["code"], result["output"]["status"]), (1, "error"))
                self.assertEqual(len(result["reads"]), 0)

    def test_unknown_usage_survives_valid_text_and_aggregation(self):
        cases = [None, {}, {"prompt_tokens": 20}, {**USAGE, "prompt_tokens": True},
                 {**USAGE, "total_tokens": 1}, {**USAGE, "completion_tokens": -1}]
        for usage in cases:
            with self.subTest(usage=usage):
                raw = response(calls=[call()])
                if usage is None:
                    del raw["usage"]
                else:
                    raw["usage"] = usage
                result = self.run_cli([raw, response()])
                self.assertEqual(result["code"], 0)
                self.assertEqual(result["output"]["usage"]["usage_status"], "usage_unknown")
                receipt = json.loads((result["run_dir"] / "turn-001/receipt.json").read_text())
                self.assertEqual(receipt["usage"]["usage_status"], "usage_unknown")
                if usage is None:
                    self.assertIsNone(receipt["usage"]["total_tokens"])
                if usage and type(usage.get("prompt_tokens")) is int:
                    self.assertEqual(receipt["usage"]["prompt_tokens"], usage["prompt_tokens"])
                if usage and type(usage.get("prompt_tokens")) is bool:
                    self.assertIsNone(receipt["usage"]["prompt_tokens"])

    def test_known_usage_and_provider_metadata(self):
        result = self.run_cli()
        self.assertEqual(result["output"]["usage"], {"usage_status": "known", "prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60})
        for index in (1, 2):
            receipt = json.loads((result["run_dir"] / f"turn-{index:03d}/receipt.json").read_text())
            self.assertEqual(receipt["actual_model"], "controlled-actual-model")
            self.assertEqual(receipt["request_id"], f"controlled-request-{index}")
            self.assertEqual(receipt["http_status"], 200)

    def test_illegal_transcripts_are_refused_before_send(self):
        initial = [{"role": "system", "content": "Read files before answering."},
                   {"role": "user", "content": "read validator.py"}]
        assistant = {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]}
        feedback_a = {"role": "tool", "tool_call_id": "a", "content": "{}"}
        feedback_b = {"role": "tool", "tool_call_id": "b", "content": "{}"}
        valid = initial + [assistant, feedback_a, feedback_b]
        self.assertTrue(self.helper({"operation": "transcript", "messages": valid})["accepted"])
        invalid = [initial + [assistant], initial + [assistant, feedback_a], initial + [feedback_a],
                   initial + [assistant, feedback_a, feedback_a], initial + [assistant, feedback_a, initial[0]],
                   initial + [{**assistant, "tool_calls": [call("a"), call("a")]}],
                   valid + [{"role": "assistant", "content": None, "tool_calls": [call("a")]}, feedback_a]]
        for messages in invalid:
            with self.subTest(messages=messages):
                self.assertFalse(self.helper({"operation": "transcript", "messages": messages})["accepted"])

    def test_request_timeout_stops_worker_without_retry(self):
        result = self.run_cli(scenario=[{"raw": response(), "delay": 4}],
                              environment={"DEEPSEEK_TIMEOUT_SECONDS": "0.2"})
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["output"]["error"]["class"], "provider_timeout")
        self.assertLess(result["elapsed"], 3)
        self.assertEqual(len(result["observed"]), 1)
        self.assertFalse((result["run_dir"] / "turn-001/raw-response.json").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(result["observed"][0]["pid"], 0)

    def test_task_deadline_stops_worker_and_local_steps(self):
        result = self.run_cli(scenario=[{"raw": response(), "delay": 4}], deadline=0.2)
        self.assertEqual((result["code"], result["output"]["status"]), (1, "deadline_exceeded"))
        self.assertLess(result["elapsed"], 3)
        with self.assertRaises(ProcessLookupError):
            os.kill(result["observed"][0]["pid"], 0)
        stepped = self.run_cli([response(calls=[call("a"), call("b")])], deadline=0.25, read_delay=0.3)
        self.assertEqual(stepped["output"]["status"], "deadline_exceeded")
        self.assertEqual(len(stepped["reads"]), 1)
        self.assertEqual(len(stepped["observed"]), 1)

    def test_configuration_and_input_fail_before_transport(self):
        for options in ({"environment": {"DEEPSEEK_API_KEY": ""}},
                        {"environment": {"DEEPSEEK_MODEL": ""}},
                        {"environment": {"DEEPSEEK_API_BASE": "https://example.invalid"}},
                        {"environment": {"DEEPSEEK_TIMEOUT_SECONDS": "nan"}},
                        {"max_turns": 0}, {"deadline": -1}, {"max_tokens": 0}, {"question": ""}):
            with self.subTest(options=options):
                result = self.run_cli([], **options)
                self.assertEqual((result["code"], result["output"]["status"]), (2, "blocked"))
                self.assertEqual(result["output"]["execution"], "blocked")
                self.assertEqual(result["output"]["task_verdict"], "unevaluated")
                self.assertEqual(len(result["observed"]), 0)
                self.assertEqual(len(result["reads"]), 0)

    def test_transport_errors_keep_error_visible_and_never_retry(self):
        for error_class, status in (("authentication_failed", 401), ("rate_limited", 429), ("network_error", None)):
            with self.subTest(error_class=error_class):
                item = {"error": {"stage": "http" if status else "transport", "class": error_class, "message": "controlled failure"}, "http_status": status}
                if status:
                    item["raw"] = {"error": {"message": "controlled failure"}}
                result = self.run_cli(scenario=[item])
                self.assertEqual((result["code"], result["output"]["status"]), (1, "error"))
                self.assertEqual(result["output"]["error"]["class"], error_class)
                self.assertEqual(len(result["observed"]), 1)
                self.assertEqual(len(result["reads"]), 0)
        failed_feedback = self.run_cli(scenario=[
            {"raw": response(calls=[call()])},
            {"http_status": 429, "raw": {"error": {"message": "controlled"}},
             "error": {"stage": "http", "class": "rate_limited", "message": "controlled"}},
        ])
        self.assertEqual(failed_feedback["output"]["read_successes"], 1)
        self.assertEqual(failed_feedback["output"]["visible_read_successes"], 0,
                         "后续请求失败，不能据此证明模型处理了文件正文")
        self.assertEqual(failed_feedback["output"]["task_verdict"], "fail")

    def test_redaction_and_model_claim_does_not_modify_files(self):
        text = f"已把 validator.py 的上限改成 1。调试凭据：{SENTINEL}"
        result = self.run_cli([response(calls=[call()]), response(text)],
                              question=f"读取 validator.py。凭据样例：{SENTINEL}")
        self.assertEqual(result["code"], 0)
        self.assertIn("已把", result["output"]["text"])
        self.assertIn("65535", (result["directory"] / "workspace/validator.py").read_text())

    def test_safe_read_rejects_unsafe_files_and_paths(self):
        workspace = self.case_dir() / "workspace"
        workspace.mkdir()
        (workspace / "normal.txt").write_text("normal\n")
        (workspace / "directory").mkdir()
        (workspace / "invalid.txt").write_bytes(b"\xff")
        (workspace / "large.txt").write_bytes(b"a\n" * (32768 + 1))
        (workspace / "long.txt").write_bytes(b"x" * 4097)
        (workspace / "link.txt").symlink_to(workspace / "normal.txt")
        (workspace / "linked-dir").symlink_to(workspace, target_is_directory=True)
        os.mkfifo(workspace / "fifo")
        cases = ["directory", "invalid.txt", "large.txt", "long.txt", "link.txt", "\ud800",
                 "linked-dir/normal.txt", "fifo", "../normal.txt", str(workspace / "normal.txt")]
        for path in cases:
            with self.subTest(path=path):
                result = self.helper({"operation": "read", "workspace": str(workspace), "arguments": {"path": path}})
                self.assertFalse(result["accepted"])

    def test_pagination_uses_real_line_numbers_and_bounded_pages(self):
        workspace = self.case_dir() / "workspace"
        workspace.mkdir()
        (workspace / "lines.txt").write_text("\n".join(f"line-{i}" for i in range(1, 102)) + "\n")
        first = self.helper({"operation": "read", "workspace": str(workspace),
                             "arguments": {"path": "lines.txt", "offset": 2, "limit": 3}})
        self.assertTrue(first["accepted"])
        data = first["value"]
        self.assertEqual(data["lines"], [{"line": i, "text": f"line-{i}"} for i in (2, 3, 4)])
        self.assertEqual(data["total_lines"], 101)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["next_offset"], 5)
        last = self.helper({"operation": "read", "workspace": str(workspace),
                            "arguments": {"path": "lines.txt", "offset": 100, "limit": 80}})["value"]
        self.assertEqual([line["line"] for line in last["lines"]], [100, 101])
        self.assertFalse(last["truncated"])
        self.assertIsNone(last["next_offset"])
        for args in ({"offset": 102}, {"limit": 81}, {"offset": True}):
            result = self.helper({"operation": "read", "workspace": str(workspace), "arguments": {"path": "lines.txt", **args}})
            self.assertFalse(result["accepted"])
        for newline in ("\n", "\r\n", "\r"):
            source = f"# comment\u2028still comment{newline}port = 80{newline}"
            (workspace / "unicode.py").write_bytes(source.encode("utf-8"))
            result = self.helper({"operation": "read", "workspace": str(workspace),
                                  "arguments": {"path": "unicode.py"}})["value"]
            self.assertEqual(result["total_lines"], 2)
            self.assertEqual(result["lines"], [{"line": 1, "text": "# comment\u2028still comment"},
                                               {"line": 2, "text": "port = 80"}])
        def add_unicode_comment(workspace):
            path = workspace / "validator.py"
            path.write_text("# comment\u2028still comment\n" + path.read_text(), encoding="utf-8")

        result = self.run_cli(workspace_setup=add_unicode_comment)
        feedback = json.loads(self.tool_messages(result)[0]["content"])
        self.assertEqual(feedback["total_lines"], 8)
        self.assertEqual(feedback["lines"][0], {"line": 1, "text": "# comment\u2028still comment"})
        self.assertEqual(feedback["lines"][1]["text"], "def parse_port(value: str) -> int:")
        self.assertEqual(result["output"]["visible_read_successes"], 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--test", choices=unittest.defaultTestLoader.getTestCaseNames(CliContractTests),
                        help="只运行指定检查方法；省略时运行全部检查")
    args = parser.parse_args()
    project = args.project.resolve()
    missing = [name for name in ("main.py", "w01_support.py", "w02_support.py") if not (project / name).is_file()]
    if missing:
        parser.error("工程目录缺少文件：" + ", ".join(missing))
    directory = args.output_dir.resolve() / f"check-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    CliContractTests.project = project
    CliContractTests.output_root = directory
    suite = (unittest.TestSuite([CliContractTests(args.test)]) if args.test
             else unittest.defaultTestLoader.loadTestsFromTestCase(CliContractTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {
        "evidence_source": SOURCE, "execution": "ran", "coverage": "hit",
        "verdict": "pass" if result.wasSuccessful() else "fail",
        "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "selected_test": args.test,
        "task_verdict": "unevaluated", "project": str(project), "output_directory": str(directory),
        "failure_details": [{"test": str(test), "traceback": detail} for test, detail in result.failures + result.errors],
    }
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
