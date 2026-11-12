"""W05 CLI 检查：SSE 受控字节禁网；文件、批准、SIGINT 和子进程真实。"""
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
import unittest
import uuid

MATERIALS = Path(__file__).resolve().parent
W04 = MATERIALS.parents[2] / "week-04/practices/PRA-W04"
spec = importlib.util.spec_from_file_location("w04_cli_checks", W04 / "check_cli.py")
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
call, response, read_jsonl = previous.call, previous.response, previous.read_jsonl


def frame(delta=None, *, finish=None, usage=None, ident="controlled-response", model="controlled-actual-model"):
    row = {"id": ident, "model": model, "choices": [
        {"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        row["usage"] = usage
    return "data: " + json.dumps(row, ensure_ascii=False) + "\n\n"


def frames(value):
    message = value["choices"][0]["message"]
    result = [frame({"role": "assistant"})]
    if message.get("content"):
        text = message["content"]
        midpoint = max(1, len(text) // 2)
        result.extend(frame({"content": part}) for part in (text[:midpoint], text[midpoint:]) if part)
    for index, item in enumerate(message.get("tool_calls", [])):
        function = item["function"]
        result.append(frame({"tool_calls": [{"index": index, "id": item["id"], "type": "function",
                                             "function": function}]}))
    result.append(frame(finish=value["choices"][0]["finish_reason"], usage=value.get("usage")))
    result.append("data: [DONE]\n\n")
    return result


WORKER = r'''
import base64, json, os, signal, socket, sys, time
from pathlib import Path
def forbidden(*args, **kwargs):
    raise AssertionError("W05 controlled worker forbids network")
socket.socket.connect = forbidden
scenario, audit = map(Path, sys.argv[1:3])
incoming = json.load(sys.stdin)
items = json.loads(scenario.read_text())
index = len(audit.read_bytes().splitlines()) if audit.exists() else 0
with audit.open("a", encoding="utf-8") as target:
    target.write(json.dumps({"request": incoming["request"], "pid": os.getpid(), "index": index}) + "\n")
if index >= len(items):
    raise RuntimeError("controlled responses exhausted: extra model request")
item = items[index]
states = audit.with_name("worker-state.jsonl")
def record(phase, **fields):
    with states.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"phase": phase, "pid": os.getpid(), "index": index,
                                 "time_ns": time.monotonic_ns(), **fields}) + "\n")
def emit(value):
    print(json.dumps(value), flush=True)
emit({"type": "headers", "status": 200, "request_id": "controlled-" + str(index)})
started = time.monotonic()
for number, block in enumerate(item["sse"]):
    raw = block.encode("utf-8")
    # Split each frame and, when present, a UTF-8 character across actual worker records.
    non_ascii = next((i for i, byte in enumerate(raw) if byte >= 128), None)
    cut = non_ascii + 1 if non_ascii is not None else min(11, len(raw))
    for chunk in (raw[:cut], raw[cut:]):
        if chunk:
            emit({"type": "chunk", "data": base64.b64encode(chunk).decode(),
                  "arrival_ms": int((time.monotonic() - started) * 1000)})
    record("frame_sent", frame=number)
    if item.get("pause_after") == number:
        record("waiting", frame=number)
        if item.get("cancel_parent"):
            time.sleep(0.08)
            os.kill(os.getppid(), signal.SIGINT)
            record("sigint_sent", target=os.getppid())
        time.sleep(item.get("pause_seconds", 5))
record("stream_complete")
emit({"type": "end"})
'''

# Reuse the W04 filesystem/approval observers, replacing its entire-response worker
# with raw SSE records. The old model entry explicitly fails if called.
WRAPPER = previous.WRAPPER.replace(
    "import copy, json, os, runpy, socket, sys, time",
    "import copy, json, os, runpy, signal, socket, sys, time")
WRAPPER = WRAPPER.replace("import w04_support\n", "import w04_support\nimport w05_support\n")
WRAPPER = WRAPPER.replace(
    "original_model = w04_support.call_model_once",
    "original_model = w04_support.call_model_once\noriginal_decision = files._read_decision")
WRAPPER = WRAPPER.replace(
    previous.WRAPPER[previous.WRAPPER.index("def model("):previous.WRAPPER.index("def forbidden(")],
    "def model(config, request, deadline):\n    raise AssertionError('W05 must use raw SSE transport')\n")
WRAPPER = WRAPPER.replace(
    "def forbidden(*args, **kwargs):\n    raise AssertionError(\"W04 受控 CLI 检查禁止真实网络\")",
    """def decision(proposal, deadline, cancelled):
    if settings.get('cancel_approval'):
        observe('approval_sigint', pid=os.getpid(), proposal_id=proposal.proposal_id)
        os.kill(os.getpid(), signal.SIGINT)
    return original_decision(proposal, deadline, cancelled)
def forbidden(*args, **kwargs):
    raise AssertionError('W05 controlled CLI forbids network')""")
WRAPPER = WRAPPER.replace(
    'patch.object(w04_support, "_provider_command", return_value=',
    'patch.object(w05_support, "_stream_worker_command", return_value=')
WRAPPER = WRAPPER.replace(
    'patch.object(files, "safe_read", side_effect=read),',
    'patch.object(files, "_read_decision", side_effect=decision), \\\n     patch.object(files, "safe_read", side_effect=read),')
previous.WORKER, previous.WRAPPER = WORKER, WRAPPER


class CliChecks(previous.CliContractTests):
    def invoke(self, command, raws=(), **kwargs):
        scripts = [item if "sse" in item else {"sse": frames(item)} for item in raws]
        return super().invoke(command, scripts, **kwargs)

    def events(self, value):
        return read_jsonl(Path(value["result"]["output_directory"]) / "events.jsonl")

    def assert_no_execution(self, value):
        forbidden = {"read", "commit_enter", "shell_fixture"}
        self.assertFalse(any(row["operation"] in forbidden for row in value["operations"]),
                         "完整流接纳前发生了工具执行")
        self.assertEqual(self.workspace_bytes(), self.initial)
        assistants = [row for row in read_jsonl(self.session)
                      if row.get("message", {}).get("role") == "assistant"]
        self.assertEqual(assistants, [], "异常流被写入完整 assistant 历史")

    def assert_worker_reaped(self, value):
        for request in value["requests"]:
            with self.assertRaises(ProcessLookupError, msg="模型 worker 未回收"):
                os.kill(request["pid"], 0)

    def test_stream_complete_before_tools(self):
        value = self.prepare_read("中文流完成。")
        states = read_jsonl(value["directory"] / "worker-state.jsonl")
        completed = next(row["time_ns"] for row in states
                         if row["index"] == 0 and row["phase"] == "stream_complete")
        reads = [row for row in value["operations"] if row["operation"] == "read"]
        self.assertTrue(all(row["time_ns"] >= completed for row in reads), "完整流接纳前发生了工具执行")
        self.assertTrue(all(row["request"]["stream"] is True for row in value["requests"]))
        self.assertTrue(all(row["request"]["stream_options"]["include_usage"] is True
                            for row in value["requests"]))
        events = self.events(value)
        self.assertEqual([row["seq"] for row in events], list(range(1, len(events) + 1)))
        self.assertTrue(any(row["event"] == "text_delta" for row in events))
        self.assertTrue(any(row["event"] == "tool_arguments_delta" for row in events))
        self.assertEqual(events[-1]["event"], "run_finished")
        stderr = (value["directory"] / "stderr.txt").read_text()
        self.assertEqual(stderr.count("中文流完成。"), 1, "最终回答重复显示")
        self.assert_worker_reaped(value)
        self.facts = {"raw_sse_worker": True, "accepted_before_execution": True,
                      "utf8_split": True, "worker_reaped": True}

    def test_stream_error_no_execution(self):
        valid = frames(response(calls=[call()]))
        duplicate = frames(response(calls=[call(), call()]))
        cases = {
            "missing_done": valid[:-1],
            "missing_finish": valid[:-2] + [valid[-1]],
            "bad_json": valid[:2] + ["data: {broken\n\n"],
            "identity_change": valid[:2] + [frame({}, ident="other-response"), *valid[2:]],
            "duplicate_call_id": duplicate,
            "length": frames(response(calls=[call()], finish="length")),
        }
        for name, chunks in cases.items():
            with self.subTest(case=name):
                self.session = self.directory / (name + ".jsonl")
                value = self.invoke("new", [{"sse": chunks}])
                self.assertEqual(value["exit_code"], 1, value["result"])
                self.assertEqual(len(value["requests"]), 1)
                self.assert_no_execution(value)
                self.assert_worker_reaped(value)
                run = Path(value["result"]["output_directory"])
                self.assertTrue((run / "turn-001/stream.raw").stat().st_size)
                self.assertTrue(self.inspect()["blocked_reason"])
        self.facts = {"cases": list(cases), "later_requests": 0, "tool_execution": 0,
                      "assistant_saved": 0, "raw_stream_preserved": True}

    def test_missing_usage_stays_unknown(self):
        raw = response("usage 缺失的完整结果")
        raw.pop("usage")
        value = self.invoke("new", [raw])
        self.assert_ok(value)
        usage = value["result"]["usage"]
        self.assertIn("unknown", json.dumps(usage).lower(), "缺失 usage 未标记未知")
        self.assertNotEqual(usage.get("prompt_tokens"), 0, "缺失 usage 被补成零")
        self.facts = {"complete_message_accepted": True, "usage_unknown": True}

    def test_cancel_model_sigint(self):
        script = {"sse": frames(response(calls=[call()])), "pause_after": 1, "cancel_parent": True}
        value = self.invoke("new", [script])
        self.assertEqual(value["exit_code"], 1, value["result"])
        self.assertEqual(len(value["requests"]), 1)
        self.assert_no_execution(value)
        states = read_jsonl(value["directory"] / "worker-state.jsonl")
        self.assertTrue(any(row["phase"] == "sigint_sent" for row in states))
        self.assert_worker_reaped(value)
        events = self.events(value)
        self.assertEqual(sum(row["event"] == "cancel_requested" for row in events), 1)
        self.assertEqual(events[-1]["event"], "run_finished")
        original = self.session.read_bytes()
        self.assert_rejected(self.invoke("resume", [response()]), unchanged=original)
        self.facts = {"actual_sigint": True, "partial_arguments_seen": True,
                      "worker_reaped": True, "tool_execution": 0, "resume_blocked": True}

    def test_cancel_approval_sigint(self):
        mutation = call("pending-edit", "edit", {"path": "validator.py", "old_text": "1 < port < 65535",
                                                   "new_text": "1 <= port <= 65535"})
        value = self.invoke("new", [response(calls=[mutation, call("later-read")])], cancel_approval=True)
        self.assertEqual(value["exit_code"], 1, value["result"])
        self.assertEqual(len(value["requests"]), 1)
        self.assertTrue(any(row["operation"] == "approval_sigint" for row in value["operations"]))
        self.assertFalse(any(row["operation"] in {"commit_enter", "read", "shell_fixture"}
                             for row in value["operations"]), "取消批准后仍然派发工具")
        self.assertEqual(self.workspace_bytes(), self.initial)
        events = self.events(value)
        self.assertEqual(sum(row["event"] == "cancel_requested" for row in events), 1)
        self.assertTrue(self.inspect()["blocked_reason"])
        self.facts = {"actual_sigint_at_approval": True, "file_unchanged": True,
                      "later_dispatch": 0, "session_blocked": True}

    def test_cancel_after_commit_keeps_effect(self):
        mutation = call("applied-edit", "edit", {"path": "validator.py", "old_text": "1 < port < 65535",
                                                   "new_text": "1 <= port <= 65535"})
        script = {"sse": frames(response(calls=[call("later-read")])),
                  "pause_after": 1, "cancel_parent": True}
        value = self.invoke("new", [response(calls=[mutation]), script])
        self.assertEqual(value["exit_code"], 1, value["result"])
        self.assertEqual(len(value["requests"]), 2)
        self.assertEqual(sum(row["operation"] == "commit_returned" for row in value["operations"]), 1)
        self.assertFalse(any(row["operation"] == "read" for row in value["operations"]),
                         "取消后仍分发后续读取")
        expected = dict(self.initial)
        expected["validator.py"] = expected["validator.py"].replace(b"1 < port < 65535", b"1 <= port <= 65535")
        self.assertEqual(self.workspace_bytes(), expected, "取消改变了已经生效的文件事实")
        self.assert_worker_reaped(value)
        original = self.session.read_bytes()
        self.assert_rejected(self.invoke("resume", [response()]), unchanged=original)
        self.facts = {"actual_commit": 1, "actual_sigint": True, "later_read": 0,
                      "applied_change_retained": True, "resume_blocked": True}

    def watch(self, source, cursor, *, follow=False, stop_after_output=False):
        directory = self.directory / ("watch-" + uuid.uuid4().hex[:8])
        directory.mkdir()
        argv = ["watch", "--events", str(source), "--cursor", str(cursor)]
        if follow:
            argv.append("--follow")
        for name, body in (("worker.py", WORKER), ("wrapper.py", WRAPPER)):
            (directory / name).write_text(body)
        (directory / "scenario.json").write_text("[]")
        (directory / "options.json").write_text(json.dumps({"argv": argv}))
        audit, operations = directory / "transport.jsonl", directory / "operations.jsonl"
        command = [sys.executable, str(directory / "wrapper.py"), str(self.entry_point),
                   str(directory / "worker.py"), str(directory / "scenario.json"),
                   str(audit), str(operations), str(directory / "options.json")]
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        process = subprocess.Popen(command, env=env, cwd=directory, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        prefix = b""
        try:
            if stop_after_output:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(5), "watch 未显示已保存事件")
                    prefix = os.read(process.stdout.fileno(), 65536)
                    self.assertIn(b"\n", prefix)
                process.send_signal(signal.SIGINT)
            out, err = process.communicate(timeout=6)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)
        stdout, stderr = (prefix + out).decode(), err.decode()
        (directory / "stdout.txt").write_text(stdout)
        (directory / "stderr.txt").write_text(stderr)
        result = {"exit_code": process.returncode, "requests": read_jsonl(audit),
                  "operations": read_jsonl(operations), "stdout": stdout, "stderr": stderr,
                  "directory": str(directory)}
        (directory / "driver.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(result["requests"], [], "watch 回放触发模型请求")
        self.assertEqual(result["operations"], [], "watch 回放触发工具执行")
        return result

    def test_watch_resume_is_read_only(self):
        value = self.prepare_read()
        original_journal, original_files = self.session.read_bytes(), self.workspace_bytes()
        rows = self.events(value)
        source, cursor = self.directory / "watch-source/events.jsonl", self.directory / "cursor.json"
        source.parent.mkdir()
        source.write_text("".join(json.dumps(row) + "\n" for row in rows[:3]))
        first = self.watch(source, cursor, follow=True, stop_after_output=True)
        self.assertEqual(first["exit_code"], 0, first)
        saved = json.loads(cursor.read_text())
        self.assertGreaterEqual(saved["last_seq"], 1)
        with source.open("a") as target:
            target.write("".join(json.dumps(row) + "\n" for row in rows[3:]))
        before = source.read_bytes()
        second = self.watch(source, cursor)
        self.assertEqual(second["exit_code"], 0, second)
        shown = [json.loads(line) for line in second["stdout"].splitlines()]
        self.assertEqual([row["seq"] for row in shown], list(range(saved["last_seq"] + 1, len(rows) + 1)))
        third = self.watch(source, cursor)
        self.assertEqual(third["exit_code"], 0)
        self.assertEqual(third["stdout"], "")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.session.read_bytes(), original_journal)
        self.assertEqual(self.workspace_bytes(), original_files)
        self.facts = {"watch_sigint": True, "continuous_cursor": True, "replay_requests": 0,
                      "replay_tools": 0, "journal_unchanged": True, "files_unchanged": True}

    def test_watch_rejects_invalid_order(self):
        value = self.prepare_read()
        rows = self.events(value)
        cases = {"gap": [rows[0], *rows[2:]], "duplicate": [rows[0], rows[0], *rows[1:]],
                 "wrong_run": [rows[0], {**rows[1], "run_id": "other-run"}, *rows[2:]]}
        for name, records in cases.items():
            with self.subTest(case=name):
                source = self.directory / name / "events.jsonl"
                source.parent.mkdir()
                cursor = self.directory / f"{name}-cursor.json"
                source.write_text("".join(json.dumps(row) + "\n" for row in records))
                before = source.read_bytes()
                value = self.watch(source, cursor)
                self.assertEqual(value["exit_code"], 2, value)
                self.assertEqual(source.read_bytes(), before)
                if cursor.exists():
                    self.assertLessEqual(json.loads(cursor.read_text())["last_seq"], 1)
        self.facts = {"bad_order_rejected": list(cases), "source_unchanged": True,
                      "cursor_did_not_skip_error": True}


REGRESSION_TESTS = previous.REGRESSION_TESTS
W05_TESTS = ["test_stream_complete_before_tools", "test_stream_error_no_execution",
             "test_missing_usage_stays_unknown", "test_cancel_model_sigint", "test_cancel_approval_sigint",
             "test_cancel_after_commit_keeps_effect",
             "test_watch_resume_is_read_only", "test_watch_rejects_invalid_order"]
TESTS = REGRESSION_TESTS + W05_TESTS


def run_suite(entry, output, names=None):
    class Bound(CliChecks):
        pass
    Bound.entry_point, Bound.output_root = entry.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    selected = names or TESTS
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Bound(name) for name in selected))
    summary = {"evidence_source": "controlled-fixture", "external_model_requests": 0,
               "transport": "raw-sse-worker-no-network", "shell": "controlled-boundary-no-container",
               "signals": "actual-SIGINT", "files": "actual-filesystem",
               "entry": str(entry.resolve()), "output_directory": str(output.resolve()),
               "selected_cases": selected, "tests_run": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors),
               "verdict": "pass" if result.wasSuccessful() else "fail",
               "failure_details": [{"test": str(test), "traceback": trace}
                                   for test, trace in result.failures + result.errors]}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case", choices=TESTS)
    args = parser.parse_args()
    if not args.entry.is_file():
        parser.error("entry 必须为可运行 CLI")
    result = run_suite(args.entry, args.output_dir / ("check-" + uuid.uuid4().hex[:8]),
                       [args.case] if args.case else None)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
