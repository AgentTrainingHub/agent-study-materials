"""W04 CLI 机制检查：受控模型禁网络；实际子进程、JSONL、文件与审批。"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
W03 = MATERIALS.parents[2] / "week-03/practices/PRA-W03"
SOURCE = "controlled-fixture"
SECRET = "w04-controlled-secret-do-not-log"
UI_MARKER = "UI_ONLY_7be41"
A_TEXT, B_TEXT, C_TEXT = "A_FINAL_43f21", "B_FINAL_9ce72", "C_FINAL_6db18"
EXTERNAL = "# EXTERNAL_CURRENT_FILE_5db81\n"


def call(ident="call-1", name="read", arguments=None):
    if arguments is None:
        arguments = {"path": "validator.py"}
    return {"id": ident, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def response(text=A_TEXT, *, calls=None, finish=None):
    message = {"role": "assistant", "content": text if calls is None else None}
    if calls is not None:
        message["tool_calls"] = calls
    return {"id": "controlled-response", "model": "controlled-actual-model",
            "choices": [{"message": message, "finish_reason": finish or ("tool_calls" if calls is not None else "stop")}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


WORKER = r'''
import json, os, socket, sys
from datetime import datetime, timezone
from pathlib import Path
def forbidden(*args, **kwargs):
    raise AssertionError("W04 受控 worker 禁止真实网络")
socket.socket.connect = forbidden
scenario, audit = map(Path, sys.argv[1:3])
incoming = json.load(sys.stdin)
items = json.loads(scenario.read_text())
index = len(audit.read_bytes().splitlines()) if audit.exists() else 0
with audit.open("a", encoding="utf-8") as target:
    target.write(json.dumps({"request": incoming["request"], "pid": os.getpid(), "index": index}) + "\n")
if index >= len(items):
    raise RuntimeError("受控响应已用完，出现额外模型请求")
now = datetime.now(timezone.utc).isoformat()
value = {"request_payload": incoming["request"], "raw_response": items[index],
         "has_response": True, "http_status": 200, "request_id": "controlled-" + str(index),
         "error": None, "started_at": now, "finished_at": now, "elapsed_ms": 1}
print(json.dumps(value).replace(incoming["config"]["api_key"], "[REDACTED]"))
'''


WRAPPER = r'''
import copy, json, os, runpy, socket, sys, time
from pathlib import Path
from unittest.mock import patch
entry, worker, scenario, audit, operations, options = map(Path, sys.argv[1:7])
settings = json.loads(options.read_text())
sys.path.insert(0, str(entry.parent))
import w02_support
import w04_support
import w04_io
import w03_files as files
import w03_shell as shell
original_read, original_commit = files.safe_read, files.commit_proposal
original_model = w04_support.call_model_once
original_append = w04_io.JSONLFile.append_line
commits_returned = 0
def observe(kind, **fields):
    with operations.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"operation": kind, "time_ns": time.monotonic_ns(), **fields}) + "\n")
def read(workspace, *args, **kwargs):
    observe("read", path=args[0] if args else kwargs.get("path"))
    return original_read(workspace, *args, **kwargs)
def commit(workspace, proposal, **kwargs):
    global commits_returned
    observe("commit_enter", proposal_id=proposal.proposal_id)
    result = original_commit(workspace, proposal, **kwargs)
    commits_returned += 1
    observe("commit_returned", proposal_id=proposal.proposal_id)
    if settings.get("after_commit_exception"):
        raise OSError("受控注入：真实提交后成功回执丢失")
    return result
def append_line(storage, data):
    if not settings.get("feedback_fsync_failure") or not commits_returned:
        return original_append(storage, data)
    row = json.loads(data)
    if row.get("type") == "message" and row.get("message", {}).get("role") == "tool":
        def fail_fsync(descriptor):
            observe("feedback_fsync_failure", call_id=row["message"]["tool_call_id"],
                    appended_bytes=len(data), descriptor_matches=descriptor == storage._handle.fileno())
            raise OSError("受控注入：真实提交后的工具反馈 fsync 失败")
        with patch.object(w04_io.os, "fsync", side_effect=fail_fsync):
            return original_append(storage, data)
    return original_append(storage, data)
def preflight(runtime):
    observe("docker_preflight_fixture")
    return "controlled-docker-boundary-no-container"
def run_check(workspace, image, evidence_dir, **kwargs):
    observe("shell_fixture")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    inputs = evidence_dir / "input"
    inputs.mkdir()
    paths = {}
    for name in ("validator.py", "test_validator.py"):
        source = workspace / name
        if source.exists():
            target = inputs / name
            target.write_bytes(source.read_bytes())
            paths[name] = str(target)
    return {"ok": True, "exit_code": 0, "new_tests_ran": "test_validator.py" in paths,
            "fixed_checks_passed": True, "input_files": paths, "artifact_dir": str(evidence_dir),
            "stdout": "受控 Docker 边界，未启动容器", "stderr": "", "capture_complete": True,
            "control": {"blocks_run": False, "effect": "none", "scope": "container_copy", "status": None}}
def model(config, request, deadline):
    request = copy.deepcopy(request)
    if settings.get("mutant_ui"):
        request["messages"][-1]["content"] += "\nUI_ONLY_7be41"
    if settings.get("mutant_branch"):
        request["messages"][-1:-1] = [
            {"role": "user", "content": "B_OFF_BRANCH_QUEST"},
            {"role": "assistant", "content": "B_FINAL_9ce72"}]
    return original_model(config, request, deadline)
def forbidden(*args, **kwargs):
    raise AssertionError("W04 受控 CLI 检查禁止真实网络")
sys.argv = [str(entry), *settings["argv"]]
with patch.object(w04_support, "_provider_command", return_value=[sys.executable, str(worker), str(scenario), str(audit)]), \
     patch.object(w02_support, "EVIDENCE_SOURCE", "controlled-fixture"), \
     patch.object(w04_support, "call_model_once", side_effect=model), \
     patch.object(files, "safe_read", side_effect=read), \
     patch.object(files, "commit_proposal", side_effect=commit), \
     patch.object(w04_io.JSONLFile, "append_line", new=append_line), \
     patch.object(shell, "preflight", side_effect=preflight), \
     patch.object(shell, "run_check", side_effect=run_check), \
     patch.object(socket.socket, "connect", side_effect=forbidden):
    runpy.run_path(str(entry), run_name="__main__")
'''


def read_jsonl(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []


class CliContractTests(unittest.TestCase):
    entry_point: Path
    output_root: Path

    def setUp(self):
        self.directory = self.output_root / f"{self._testMethodName}-{uuid.uuid4().hex[:8]}"
        self.directory.mkdir(parents=True)
        self.workspace = self.directory / "workspace"
        shutil.copytree(W03 / "inputs/sample_project", self.workspace)
        self.session = self.directory / "session.jsonl"
        self.initial = self.workspace_bytes()
        self.invocations = []
        self.facts = {}

    def tearDown(self):
        (self.directory / "facts.json").write_text(json.dumps(self.facts, ensure_ascii=False, indent=2) + "\n")

    def workspace_bytes(self):
        return {p.name: p.read_bytes() for p in self.workspace.iterdir() if p.is_file()}

    def invoke(self, command, raws=(), *, question="读取并解释当前文件，不要修改。", session=None,
               extra=(), approval="approve", no_config=False, **settings):
        number = len(self.invocations) + 1
        directory = self.directory / f"cli-{number:02d}-{command}"
        directory.mkdir()
        question_path = directory / "question.txt"
        question_path.write_text(question, encoding="utf-8")
        argv = [command, "--session", str(session or self.session)]
        if command in {"new", "resume"}:
            argv += ["--workspace", str(self.workspace), "--question", str(question_path),
                     "--output-dir", str(directory / "runs"), "--runtime", str(directory / "runtime.json"),
                     "--max-turns", str(settings.pop("max_turns", 8)), "--deadline-seconds", "12"]
        argv += list(extra)
        settings["argv"] = argv
        for name, contents in (("worker.py", WORKER), ("wrapper.py", WRAPPER)):
            (directory / name).write_text(contents)
        (directory / "scenario.json").write_text(json.dumps(list(raws), ensure_ascii=False))
        (directory / "options.json").write_text(json.dumps(settings))
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get("PATH", "")}
        if not no_config:
            env.update(DEEPSEEK_API_KEY=SECRET, DEEPSEEK_MODEL="controlled-request-model", DEEPSEEK_TIMEOUT_SECONDS="2")
        audit, operations = directory / "transport.jsonl", directory / "operations.jsonl"
        process = subprocess.Popen([sys.executable, str(directory / "wrapper.py"), str(self.entry_point),
                                    str(directory / "worker.py"), str(directory / "scenario.json"),
                                    str(audit), str(operations), str(directory / "options.json")],
                                   cwd=directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8")
        out, err, pending = [], [], queue.Queue()
        def reader(stream, destination, notify=False):
            for line in stream:
                destination.append(line)
                if notify:
                    pending.put(line)
        threads = [threading.Thread(target=reader, args=(process.stdout, out), daemon=True),
                   threading.Thread(target=reader, args=(process.stderr, err, True), daemon=True)]
        for thread in threads:
            thread.start()
        approvals = []
        started = time.monotonic()
        try:
            while process.poll() is None or not pending.empty():
                if time.monotonic() - started > 20:
                    self.fail("受控 CLI 超过 20 秒监督上限")
                try:
                    line = pending.get(timeout=0.03)
                except queue.Empty:
                    continue
                if not line.startswith("W03_APPROVAL "):
                    continue
                prompt = json.loads(line[len("W03_APPROVAL "):])
                proposal_dir = Path(prompt["proposal_dir"])
                proposal = json.loads((proposal_dir / "proposal.json").read_text())
                candidate = (proposal_dir / "after.bin").read_bytes()
                diff = (proposal_dir / "diff.patch").read_bytes()
                observed = directory / "driver-observations" / prompt["proposal_id"]
                observed.mkdir(parents=True)
                shutil.copytree(proposal_dir, observed, dirs_exist_ok=True)
                decision = f"{approval} {prompt['proposal_id']}"
                approvals.append({"prompt": prompt, "decision": decision,
                                  "candidate_bytes": len(candidate), "diff_bytes": len(diff), "proposal": proposal})
                if process.stdin and not process.stdin.closed:
                    process.stdin.write(decision + "\n")
                    process.stdin.flush()
            process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        stdout, stderr = "".join(out), "".join(err)
        (directory / "stdout.txt").write_text(stdout)
        (directory / "stderr.txt").write_text(stderr)
        evidence = {"pid": process.pid, "exit_code": process.returncode, "argv": argv,
                    "approvals": approvals, "requests": read_jsonl(audit), "operations": read_jsonl(operations)}
        (directory / "driver.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        self.invocations.append(directory)
        self.assertNotIn(SECRET, stdout + stderr + json.dumps(evidence), "凭据哨兵进入公开输出或证据")
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            self.fail(f"stdout 应为单个 JSON：{stdout!r}；stderr={stderr[-1500:]!r}；{exc}")
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("exit_code"), process.returncode)
        return {**evidence, "result": result, "directory": directory}

    def assert_ok(self, observed):
        self.assertEqual(observed["exit_code"], 0, observed["result"])

    def inspect(self, **kwargs):
        value = self.invoke("inspect", no_config=True, **kwargs)
        self.assert_ok(value)
        self.assertEqual(value["requests"], [])
        self.assertEqual(value["operations"], [])
        return value["result"]

    def projection_evidence(self, observed):
        run = Path(observed["result"]["output_directory"])
        rows = read_jsonl(self.session)
        indexed = {row["entry_id"]: row for row in rows}
        projections = sorted(run.glob("projection-*.json"))
        self.assertEqual(len(projections), len(observed["requests"]))
        for number, (projection_path, sent) in enumerate(zip(projections, observed["requests"]), 1):
            projection = json.loads(projection_path.read_text())
            actual = json.loads((run / f"turn-{number:03d}" / "request.json").read_text())
            self.assertEqual(actual, sent["request"], "实际 worker 请求与证据不同")
            self.assertEqual(projection["messages"], actual["messages"], "投影未对应实际发送消息")
            self.assertEqual(len(projection["messages"]), len(projection["source_ids"]))
            self.assertEqual(projection["session_id"], observed["result"]["session_id"])
            self.assertEqual(projection["run_id"], observed["result"]["run_id"])
            for index, (message, source) in enumerate(zip(projection["messages"], projection["source_ids"])):
                self.assertIn(source, indexed)
                row = indexed[source]
                self.assertEqual(row["type"], "header" if index == 0 else "message")
                self.assertEqual(row["system"] if index == 0 else row["message"], message)

    def completed_head(self, value):
        head = value["result"]["head_id"]
        indexed = {row["entry_id"]: row for row in read_jsonl(self.session)}
        self.assertEqual(indexed[head]["type"], "run_end")
        self.assertEqual(indexed[head]["status"], "stop")
        return head

    def prepare_read(self, text=A_TEXT):
        value = self.invoke("new", [response(calls=[call()]), response(text)])
        self.assert_ok(value)
        self.assertEqual(sum(row["operation"] == "read" for row in value["operations"]), 1)
        feedback = value["requests"][1]["request"]["messages"][-1]
        self.assertEqual(feedback["role"], "tool")
        actual_lines = json.loads(feedback["content"])["lines"]
        expected_lines = [{"line": index + 1, "text": text}
                          for index, text in enumerate(
                              line.rstrip("\r\n") for line in
                              io.StringIO(self.initial["validator.py"].decode(), newline="").readlines()[:40])]
        self.assertEqual(actual_lines, expected_lines, "模型反馈未包含实际文件内容")
        self.projection_evidence(value)
        return value

    def assert_rejected(self, observed, *, unchanged=None):
        self.assertEqual(observed["exit_code"], 2, observed["result"])
        self.assertEqual(observed["requests"], [], "预检拒绝后仍请求模型")
        self.assertFalse(any(row["operation"] != "docker_preflight_fixture" for row in observed["operations"]),
                         "预检拒绝后仍执行工具")
        self.assertIsInstance(observed["result"].get("error"), dict)
        if unchanged is not None:
            self.assertEqual(self.session.read_bytes(), unchanged)

    def test_new_completed(self):
        path = self.workspace / "validator.py"
        path.write_bytes("# Unicode in comment: \u2028 remains on one source line\n".encode() + path.read_bytes())
        self.initial = self.workspace_bytes()
        value = self.prepare_read()
        state = self.inspect()
        self.assertEqual(state["head"], self.completed_head(value))
        self.assertEqual(state["messages"][-1], {"role": "assistant", "content": A_TEXT})
        self.assertEqual(len(state["source_ids"]), len(state["messages"]))
        self.assertEqual(self.workspace_bytes(), self.initial)
        original = self.session.read_bytes()
        self.assertTrue(original.endswith(b"\n"))
        self.assert_rejected(self.invoke("new", [response()]), unchanged=original)
        self.facts = {"normal_stop_saved": True, "read_count": 1, "source_ids_checked": True,
                      "file_unchanged": True, "exclusive_create": True}

    def test_resume_new_process(self):
        first = self.prepare_read()
        prefix = self.session.read_bytes()
        second = self.invoke("resume", [response(calls=[call()]), response(B_TEXT)], question="B_SECOND_QUESTION")
        self.assert_ok(second)
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertNotEqual(first["result"]["run_id"], second["result"]["run_id"])
        self.assertEqual(first["result"]["session_id"], second["result"]["session_id"])
        self.assertTrue(self.session.read_bytes().startswith(prefix))
        messages = second["requests"][0]["request"]["messages"]
        self.assertEqual(messages[-2], {"role": "assistant", "content": A_TEXT})
        self.assertEqual(messages[-1], {"role": "user", "content": "B_SECOND_QUESTION"})
        self.projection_evidence(second)
        self.assertEqual(self.inspect()["messages"][-1]["content"], B_TEXT)
        self.facts = {"new_process": True, "session_identity_retained": True, "new_run": True,
                      "prefix_retained": True, "final_assistant_reused": True, "call_id_reuse_across_batches": True}

    def ui_case(self, *, mutant=False):
        self.prepare_read()
        before = self.inspect()
        note = self.invoke("note", extra=["--text", UI_MARKER], no_config=True)
        self.assert_ok(note)
        self.assertEqual(note["requests"], [])
        self.assertEqual(note["operations"], [])
        original = self.session.read_bytes()
        visible, hidden = self.inspect(), self.inspect(extra=["--hide-ui"])
        self.assertEqual(self.session.read_bytes(), original)
        self.assertIn(UI_MARKER, json.dumps(visible["entries"]))
        self.assertNotIn(UI_MARKER, json.dumps(hidden["entries"]))
        self.assertEqual(before["head"], visible["head"])
        self.assertEqual(visible["messages"], hidden["messages"])
        self.assertEqual(visible["source_ids"], hidden["source_ids"])
        resumed = self.invoke("resume", [response(B_TEXT)], question="UI_RESUME_QUESTION", mutant_ui=mutant)
        self.assert_ok(resumed)
        self.assertNotIn(UI_MARKER, json.dumps(resumed["requests"]), "UI 注记泄漏到模型请求")
        self.projection_evidence(resumed)
        self.facts = {"note_persisted": True, "hide_ui_only_affects_display": True,
                      "head_unchanged_by_note": True, "ui_not_in_actual_request": True}

    def test_ui_display_projection(self):
        self.ui_case()

    def branch_case(self, *, mutant=False):
        first = self.prepare_read()
        h1 = self.completed_head(first)
        second = self.invoke("resume", [response(B_TEXT)], question="B_OFF_BRANCH_QUEST")
        self.assert_ok(second)
        branch = self.invoke("branch", extra=["--from-entry", h1], no_config=True)
        self.assert_ok(branch)
        self.assertEqual(branch["requests"], [])
        self.assertEqual(branch["operations"], [])
        state = self.inspect()
        self.assertIn(B_TEXT, json.dumps(state["entries"]))
        self.assertNotIn(B_TEXT, json.dumps(state["messages"]))
        self.assertNotIn("B_OFF_BRANCH_QUEST", json.dumps(state["messages"]))
        third = self.invoke("resume", [response(C_TEXT)], question="C_BRANCH_QUESTION", mutant_branch=mutant)
        self.assert_ok(third)
        request = third["requests"][0]["request"]
        self.assertIn(A_TEXT, json.dumps(request))
        self.assertNotIn(B_TEXT, json.dumps(request), "另一分支消息进入模型请求")
        self.assertNotIn("B_OFF_BRANCH_QUEST", json.dumps(request), "另一分支用户消息进入模型请求")
        self.projection_evidence(third)
        self.facts = {"old_branch_retained_in_journal": True, "selected_branch_persisted": True,
                      "off_branch_excluded_from_actual_request": True}

    def test_completed_turn_branch(self):
        self.branch_case()

    def test_branch_keeps_files(self):
        first = self.prepare_read()
        h1 = self.completed_head(first)
        mutation = call("edit-1", "edit", {"path": "validator.py", "old_text": "1 < port < 65535",
                                           "new_text": "1 <= port <= 65535"})
        second = self.invoke("resume", [response(calls=[mutation]), response(B_TEXT)], question="修正闭区间。")
        self.assert_ok(second)
        self.assertEqual(len(second["approvals"]), 1)
        self.assertEqual(sum(row["operation"] == "commit_returned" for row in second["operations"]), 1)
        committed = self.workspace_bytes()
        self.assertIn(b"1 <= port <= 65535", committed["validator.py"])
        self.assert_ok(self.invoke("branch", extra=["--from-entry", h1], no_config=True))
        self.assertEqual(self.workspace_bytes(), committed, "branch 回滚了实际文件")
        with (self.workspace / "validator.py").open("ab") as target:
            target.write(EXTERNAL.encode())
        current = self.workspace_bytes()
        third = self.invoke("resume", [response(calls=[call()]), response(C_TEXT)], question="重新读取并解释当前文件。")
        self.assert_ok(third)
        self.assertNotIn(EXTERNAL.strip(), json.dumps(third["requests"][0]["request"]))
        self.assertIn(EXTERNAL.strip(), json.dumps(third["requests"][1]["request"]))
        self.assertNotIn(B_TEXT, json.dumps(third["requests"]))
        self.assertEqual(self.workspace_bytes(), current)
        self.projection_evidence(third)
        self.facts = {"one_approved_commit": True, "branch_did_not_change_file": True,
                      "external_change_only_after_actual_read": True, "off_branch_excluded": True}

    def write_rows(self, rows):
        self.session.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))

    def corruption(self, change):
        original = self.session.read_bytes()
        self.session.write_bytes(change(original))
        corrupt = self.session.read_bytes()
        value = self.invoke("resume", [response(B_TEXT)])
        self.assert_rejected(value, unchanged=corrupt)
        inspected = self.invoke("inspect", no_config=True)
        self.assert_rejected(inspected, unchanged=corrupt)
        self.assertEqual(self.workspace_bytes(), self.initial)
        self.session.write_bytes(original)

    def test_bad_tail_rejected(self):
        self.prepare_read()
        for label, change in (("partial_json", lambda data: data + b'{"type":'),
                              ("missing_newline", lambda data: data[:-1]),
                              ("empty_line", lambda data: data + b"\n")):
            with self.subTest(label=label):
                self.corruption(change)
        self.facts = {"partial_json_rejected": True, "missing_newline_rejected": True,
                      "empty_line_rejected": True, "original_bytes_preserved_by_cli": True}

    def test_invalid_parent_rejected(self):
        self.prepare_read()
        def corrupt(data, kind):
            rows = [json.loads(line) for line in data.splitlines()]
            if kind == "unknown":
                rows[1]["parent_id"] = "absent-parent"
            elif kind == "future":
                rows[1]["parent_id"] = rows[-1]["entry_id"]
            else:
                rows[1]["parent_id"] = rows[1]["entry_id"]
            return ("".join(json.dumps(row) + "\n" for row in rows)).encode()
        for kind in ("unknown", "future", "self"):
            with self.subTest(kind=kind):
                self.corruption(lambda data: corrupt(data, kind))
        self.facts = {"unknown_parent_rejected": True, "future_parent_rejected": True,
                      "self_parent_rejected": True, "zero_execution": True}

    def test_pairing_incomplete_rejected(self):
        first = self.prepare_read()
        original = self.session.read_bytes()
        rows = read_jsonl(self.session)
        tool = next(row for row in rows if row.get("message", {}).get("role") == "tool")
        tool["message"]["tool_call_id"] = "orphan-result"
        self.write_rows(rows)
        corrupt = self.session.read_bytes()
        self.assert_rejected(self.invoke("resume", [response()]), unchanged=corrupt)
        self.assert_rejected(self.invoke("inspect", no_config=True), unchanged=corrupt)
        self.session.write_bytes(original)
        second = self.invoke("resume", [response(B_TEXT)], question="SECOND_COMPLETED_QUESTION")
        self.assert_ok(second)
        rows = read_jsonl(self.session)
        self.assertEqual(rows[-1]["type"], "run_end")
        self.assertNotEqual(rows[-1]["entry_id"], first["result"]["head_id"])
        self.write_rows(rows[:-1])
        incomplete = self.session.read_bytes()
        state = self.inspect()
        self.assertTrue(state["blocked_reason"])
        self.assert_rejected(self.invoke("resume", [response()]), unchanged=incomplete)
        self.assert_rejected(self.invoke("branch", extra=["--from-entry", first["result"]["head_id"]], no_config=True),
                             unchanged=incomplete)
        self.assert_rejected(self.invoke("note", extra=["--text", UI_MARKER], no_config=True), unchanged=incomplete)
        self.facts = {"orphan_tool_rejected": True, "incomplete_run_inspectable": True,
                      "incomplete_run_resume_branch_note_rejected": True, "zero_execution_after_rejection": True}

    def test_record_schema_rejected(self):
        self.prepare_read()
        def change_row(data, kind):
            rows = [json.loads(line) for line in data.splitlines()]
            if kind == "duplicate_id":
                rows[1]["entry_id"] = rows[0]["entry_id"]
            elif kind == "unknown_version":
                rows[0]["schema_version"] = 999
            elif kind == "unknown_type":
                rows[-1]["type"] = "unsupported_record"
            elif kind == "unknown_field":
                rows[1]["surprise"] = True
            elif kind == "invalid_type":
                rows[1]["run_id"] = False
            text = "".join(json.dumps(row) + "\n" for row in rows)
            if kind == "duplicate_field":
                text = text.replace('{"', '{"type":"header","', 1)
            elif kind == "nan":
                text = text.replace('"schema_version": 1', '"schema_version": NaN', 1)
            return text.encode()
        for kind in ("duplicate_id", "unknown_version", "unknown_type", "unknown_field", "invalid_type", "duplicate_field", "nan"):
            with self.subTest(kind=kind):
                self.corruption(lambda data: change_row(data, kind))

    def test_invalid_branch_points(self):
        self.prepare_read()
        rows = read_jsonl(self.session)
        invalid = [row["entry_id"] for row in rows if row["type"] != "run_end"] + ["missing-entry"]
        original = self.session.read_bytes()
        for ident in invalid:
            with self.subTest(entry=ident):
                self.assert_rejected(self.invoke("branch", extra=["--from-entry", ident], no_config=True), unchanged=original)

    def test_nonstop_blocks_later_commands(self):
        self.prepare_read()
        previous_head = self.inspect()["head"]
        mutation = call("deny-edit", "edit", {"path": "validator.py", "old_text": "1 < port < 65535",
                                              "new_text": "1 <= port <= 65535"})
        value = self.invoke("resume", [response(calls=[mutation, call("skipped-read")])], approval="deny")
        self.assertEqual(value["exit_code"], 1)
        self.assertEqual(value["result"]["status"], "approval_denied")
        self.assertEqual(len(value["requests"]), 1)
        self.assertFalse(any(row["operation"] in {"commit_enter", "read"} for row in value["operations"]))
        self.assertEqual(self.workspace_bytes(), self.initial)
        state = self.inspect()
        self.assertTrue(state["blocked_reason"])
        self.assertFalse(any(row.get("message", {}).get("role") == "tool" and
                             row["message"].get("tool_call_id") in {"deny-edit", "skipped-read"}
                             for row in state["entries"]))
        original = self.session.read_bytes()
        for command, extra in (("resume", []), ("branch", ["--from-entry", previous_head]), ("note", ["--text", "note"])):
            self.assert_rejected(self.invoke(command, [response()] if command == "resume" else [], extra=extra), unchanged=original)

    def test_actual_write_unknown_cannot_resume(self):
        first = self.prepare_read()
        mutation = call("lost-edit", "edit", {"path": "validator.py", "old_text": "1 < port < 65535",
                                              "new_text": "1 <= port <= 65535"})
        value = self.invoke("resume", [response(calls=[mutation, call("skipped-read")])], after_commit_exception=True)
        self.assertEqual(value["exit_code"], 1)
        self.assertEqual(value["result"]["status"], "effect_unknown")
        self.assertIn(b"1 <= port <= 65535", self.workspace_bytes()["validator.py"])
        self.assertEqual(sum(row["operation"] == "commit_returned" for row in value["operations"]), 1)
        original = self.session.read_bytes()
        self.assert_rejected(self.invoke("resume", [response()]), unchanged=original)
        self.assert_rejected(self.invoke("branch", extra=["--from-entry", first["result"]["head_id"]]), unchanged=original)

    def test_feedback_fsync_failure_preserves_execution_evidence(self):
        first = self.prepare_read()
        mutation = call("persisted-edit", "edit", {"path": "validator.py", "old_text": "1 < port < 65535",
                                                   "new_text": "1 <= port <= 65535"})
        value = self.invoke("resume", [response(calls=[mutation, call("unexecuted-read")])],
                            feedback_fsync_failure=True)
        self.assertEqual(value["exit_code"], 1)
        self.assertEqual(value["result"]["status"], "persistence_error")
        self.assertEqual(len(value["requests"]), 1, "持久化失败后仍请求模型")
        self.assertEqual(sum(row["operation"] == "commit_returned" for row in value["operations"]), 1)
        faults = [row for row in value["operations"] if row["operation"] == "feedback_fsync_failure"]
        self.assertEqual(len(faults), 1, "未命中工具反馈 fsync 故障")
        self.assertTrue(faults[0]["descriptor_matches"])
        self.assertFalse(any(row["operation"] in {"read", "shell_fixture"} for row in value["operations"]),
                         "持久化失败后仍分发后续工具")
        expected = dict(self.initial)
        expected["validator.py"] = expected["validator.py"].replace(b"1 < port < 65535", b"1 <= port <= 65535")
        self.assertEqual(self.workspace_bytes(), expected)
        run = Path(value["result"]["output_directory"])
        events = read_jsonl(run / "events.jsonl")
        finished = [row for row in events if row["event"] == "tool_finished"]
        self.assertEqual([row["executed"] for row in finished if row["call_id"] == "persisted-edit"], [True],
                         "已实际提交的工具被重复标记为未执行")
        self.assertEqual([row["executed"] for row in finished if row["call_id"] == "unexecuted-read"], [False])
        state = self.inspect()
        self.assertTrue(state["blocked_reason"])
        original = self.session.read_bytes()
        self.assert_rejected(self.invoke("resume", [response()]), unchanged=original)
        self.assert_rejected(self.invoke("branch", extra=["--from-entry", first["result"]["head_id"]]),
                             unchanged=original)
        self.assertEqual(self.workspace_bytes(), expected)
        self.facts = {"actual_commit": 1, "actual_feedback_fsync_failure": 1, "later_execution": 0,
                      "executed_event_not_rewritten": True, "resume_branch_blocked": True}

    def test_provider_extra_index_is_normalized(self):
        provider_call = call()
        provider_call["index"] = 0
        value = self.invoke("new", [response(calls=[provider_call]), response(A_TEXT)])
        self.assert_ok(value)
        self.assertEqual(sum(row["operation"] == "read" for row in value["operations"]), 1)
        canonical_call = value["requests"][1]["request"]["messages"][-2]["tool_calls"][0]
        self.assertEqual(canonical_call, call())
        run = Path(value["result"]["output_directory"])
        raw = json.loads((run / "turn-001" / "raw-response.json").read_text())
        self.assertEqual(raw["choices"][0]["message"]["tool_calls"][0], provider_call,
                         "原始 Provider 回执被规范化覆盖")
        self.projection_evidence(value)
        self.assertEqual(self.workspace_bytes(), self.initial)
        self.facts = {"provider_index_preserved_in_raw": True, "canonical_request_call": True, "actual_read": 1}

    def test_length_no_tool_execution(self):
        value = self.invoke("new", [response(calls=[call()], finish="length")])
        self.assertEqual(value["exit_code"], 1)
        self.assertEqual(value["result"]["status"], "length")
        self.assertFalse(any(row["operation"] in {"read", "commit_enter", "shell_fixture"} for row in value["operations"]))
        self.assertTrue(self.inspect()["blocked_reason"])
        self.assert_rejected(self.invoke("resume", [response()]))

    def test_same_batch_duplicate_call_ids(self):
        value = self.invoke("new", [response(calls=[call(), call()])])
        self.assertEqual(value["exit_code"], 1)
        self.assertEqual(len(value["requests"]), 1)
        self.assertFalse(any(row["operation"] == "read" for row in value["operations"]))

    def test_invalid_tool_arguments_return_paired_feedback(self):
        arguments = [
            '{"path":' + '[' * 10000 + '0' + ']' * 10000 + '}',
            '{"path":"validator.py","offset":' + '9' * 5000 + '}',
        ]
        for index, argument in enumerate(arguments):
            with self.subTest(case=index):
                self.session = self.directory / f"invalid-arguments-{index}.jsonl"
                proposal = call(f"invalid-{index}")
                proposal["function"]["arguments"] = argument
                value = self.invoke("new", [response(calls=[proposal]), response(A_TEXT)])
                self.assert_ok(value)
                self.assertEqual(len(value["requests"]), 2)
                self.assertFalse(any(row["operation"] in {"read", "commit_enter", "shell_fixture"}
                                     for row in value["operations"]))
                feedback = value["requests"][1]["request"]["messages"][-1]
                self.assertEqual(feedback["tool_call_id"], proposal["id"])
                self.assertEqual(json.loads(feedback["content"])["error"]["class"], "invalid_arguments")
                self.projection_evidence(value)
                self.completed_head(value)
                self.assertEqual(self.workspace_bytes(), self.initial)
        self.facts = {"parser_limits_return_paired_errors": True, "tool_execution": 0,
                      "normal_stop_persisted": True}

    def test_workspace_binding(self):
        self.prepare_read()
        other = self.directory / "other-workspace"
        shutil.copytree(self.workspace, other)
        original = self.session.read_bytes()
        value = self.invoke("resume", [response()], extra=["--workspace", str(other)])
        self.assert_rejected(value, unchanged=original)
        self.assertEqual(self.workspace_bytes(), self.initial)

    def test_mutant_ui_is_detected(self):
        with self.assertRaisesRegex(AssertionError, "UI 注记泄漏"):
            self.ui_case(mutant=True)
        self.facts = {"mutant": "ui_in_actual_request", "detected": True}

    def test_mutant_off_branch_is_detected(self):
        with self.assertRaisesRegex(AssertionError, "另一分支消息进入"):
            self.branch_case(mutant=True)
        self.facts = {"mutant": "off_branch_in_actual_request", "detected": True}


REGRESSION_TESTS = [
    "test_new_completed", "test_resume_new_process", "test_ui_display_projection",
    "test_completed_turn_branch", "test_branch_keeps_files", "test_bad_tail_rejected",
    "test_invalid_parent_rejected", "test_pairing_incomplete_rejected",
]


def run_suite(entry, output_root, names=None):
    class BoundTests(CliContractTests):
        pass
    BoundTests.entry_point, BoundTests.output_root = entry.resolve(), output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    selected = names or unittest.defaultTestLoader.getTestCaseNames(CliContractTests)
    suite = unittest.TestSuite(BoundTests(name) for name in selected)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {"evidence_source": SOURCE, "model": "controlled-worker-no-network", "external_model_requests": 0,
               "shell": "controlled-boundary-no-container", "files": "actual-filesystem",
               "entry_point": str(entry.resolve()), "output_directory": str(output_root.resolve()),
               "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "verdict": "pass" if result.wasSuccessful() else "fail",
               "failure_details": [{"test": str(test), "traceback": detail} for test, detail in result.failures + result.errors]}
    (output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-point", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--test", choices=unittest.defaultTestLoader.getTestCaseNames(CliContractTests))
    args = parser.parse_args()
    if not args.entry_point.is_file():
        parser.error("entry-point 必须为可运行 CLI 文件")
    directory = args.output_dir.resolve() / f"check-{uuid.uuid4().hex[:8]}"
    summary = run_suite(args.entry_point, directory, [args.test] if args.test else None)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
