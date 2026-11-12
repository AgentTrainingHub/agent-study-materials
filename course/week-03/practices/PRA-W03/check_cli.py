"""W03 受控 CLI 验收：模型和 Docker 返回为夹具，审批及文件提交为实际执行。"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
SOURCE = "controlled-fixture"
SENTINEL = "w03-controlled-secret-do-not-log"
OLD, NEW = "1 < port < 65535", "1 <= port <= 65535"
TEST_SOURCE = "import unittest\nfrom validator import parse_port\n\nclass Boundaries(unittest.TestCase):\n    def test_edges(self):\n        self.assertEqual(parse_port('1'), 1)\n        self.assertEqual(parse_port('65535'), 65535)\n"


def call(call_id="read-1", name="read", arguments=None):
    arguments = {"path": "validator.py"} if arguments is None else arguments
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}


def edit(call_id="edit-1", old=OLD, new=NEW):
    return call(call_id, "edit", {"path": "validator.py", "old_text": old, "new_text": new})


def write(call_id="write-1", content=TEST_SOURCE):
    return call(call_id, "write", {"path": "test_validator.py", "content": content})


def response(text="已核对实际返回；人工解释仍待完成。", *, calls=None, finish=None):
    message = {"role": "assistant", "content": text if calls is None else None}
    if calls is not None:
        message["tool_calls"] = calls
    return {"id": "controlled-response", "model": "controlled-actual-model",
            "choices": [{"message": message, "finish_reason": finish or ("tool_calls" if calls is not None else "stop")}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


WORKER = r'''
import json, os, socket, sys, time
from datetime import datetime, timezone
from pathlib import Path

def forbidden(*args, **kwargs):
    raise AssertionError("受控模型 worker 禁止真实网络")
socket.socket.connect = forbidden
scenario_path, audit_path = map(Path, sys.argv[1:3])
incoming = json.load(sys.stdin)
scenario = json.loads(scenario_path.read_text())
index = len(audit_path.read_text().splitlines()) if audit_path.exists() else 0
if index >= len(scenario):
    raise RuntimeError("受控响应已用完，出现额外模型调用")
item = scenario[index]
with audit_path.open("a") as target:
    target.write(json.dumps({"request": incoming["request"], "pid": os.getpid(), "index": index}) + "\n")
time.sleep(item.get("delay", 0))
now = datetime.now(timezone.utc).isoformat()
value = {"request_payload": incoming["request"], "raw_response": item.get("raw"),
         "has_response": "raw" in item, "http_status": 200 if "raw" in item else None,
         "request_id": "controlled-request-" + str(index + 1), "error": item.get("error"),
         "started_at": now, "finished_at": now, "elapsed_ms": 1}
print(json.dumps(value).replace(incoming["config"]["api_key"], "[REDACTED]"))
'''


WRAPPER = r'''
import json, os, runpy, signal, socket, sys, time
from pathlib import Path
from unittest.mock import patch

project, worker, scenario, audit, operations, options = map(Path, sys.argv[1:7])
settings = json.loads(options.read_text())
sys.path.insert(0, str(project))
import w02_support
import w03_files as files
import w03_shell as shell

original_commit, original_read = files.commit_proposal, files.safe_read
original_approve = files.approve_and_commit
shell_calls = 0
def observe(kind, **fields):
    with operations.open("a") as target:
        target.write(json.dumps({"operation": kind, "time_ns": time.monotonic_ns(), **fields}) + "\n")

def commit(workspace, proposal, **kwargs):
    observe("commit_attempt", proposal_id=proposal.proposal_id, path=proposal.before.path)
    result = original_commit(workspace, proposal, **kwargs)
    observe("commit_returned", proposal_id=proposal.proposal_id, path=proposal.before.path)
    if settings.get("cancel_after_commit"):
        os.kill(os.getpid(), signal.SIGINT)
    if settings.get("after_commit_exception"):
        raise OSError("受控注入：文件已经真实提交，成功回执在返回前丢失")
    return result

def approve(workspace, proposal, **kwargs):
    result = original_approve(workspace, proposal, **kwargs)
    if settings.get("mutant_retry") and result.get("control", {}).get("effect") == "unknown":
        observe("mutant_retry", proposal_id=proposal.proposal_id)
        try:
            files.commit_proposal(workspace, proposal, deadline=kwargs["deadline"], cancelled=kwargs["cancelled"])
        except (files.ToolError, OSError) as exc:
            observe("mutant_retry_failed", error_class=type(exc).__name__)
    return result

def read(workspace, *args, **kwargs):
    observe("read_enter")
    try:
        return original_read(workspace, *args, **kwargs)
    finally:
        observe("read_exit")

def preflight(runtime):
    if settings.get("docker_missing"):
        raise files.ToolError("docker_unavailable", "受控预检：Docker 不可用")
    return "controlled-docker-boundary-no-container"

def run_check(workspace, image, evidence_dir, **kwargs):
    global shell_calls
    observe("shell_fixture", profile=kwargs["profile"])
    evidence_dir.mkdir(parents=True, exist_ok=False)
    inputs = evidence_dir / "input"
    inputs.mkdir()
    paths = {}
    for name in ("validator.py", "test_validator.py"):
        path = workspace / name
        if path.exists():
            copy = inputs / name
            copy.write_bytes(path.read_bytes())
            paths[name] = str(copy)
    if settings.get("change_after_check"):
        with (workspace / "validator.py").open("ab") as target:
            target.write(b"\n# changed after copied check\n")
    statuses = settings.get("shell_statuses")
    status = statuses[shell_calls] if statuses else settings.get("shell_status")
    shell_calls += 1
    blocking = status in {"command_timeout", "cancelled", "output_limit", "cleanup_unconfirmed"}
    ok = status is None
    return {"ok": ok, "exit_code": 0 if ok else 1, "new_tests_ran": ok and "test_validator.py" in paths,
            "fixed_checks_passed": ok, "input_files": paths, "artifact_dir": str(evidence_dir),
            "stdout": "受控 Docker 边界返回；本检查未启动容器", "stderr": "", "capture_complete": True,
            "error": None if ok else {"class": status, "message": "受控 Shell 控制分支"},
            "control": {"blocks_run": blocking, "effect": "unknown" if status == "cleanup_unconfirmed" else "none",
                        "scope": "container_copy", "status": status if blocking else None}}

def forbidden(*args, **kwargs):
    raise AssertionError("受控 CLI 检查禁止真实网络")

sys.argv = [str(project / "main.py"), *settings["argv"]]
with patch.object(w02_support, "_provider_command", return_value=[sys.executable, str(worker), str(scenario), str(audit)]), \
     patch.object(w02_support, "EVIDENCE_SOURCE", "controlled-fixture"), \
     patch.object(files, "commit_proposal", side_effect=commit), \
     patch.object(files, "approve_and_commit", side_effect=approve), \
     patch.object(files, "safe_read", side_effect=read), \
     patch.object(shell, "preflight", side_effect=preflight), \
     patch.object(shell, "run_check", side_effect=run_check), \
     patch.object(socket.socket, "connect", side_effect=forbidden):
    if settings.get("mutant_skip_version"):
        with patch.object(files, "verify_proposal", side_effect=lambda workspace, proposal: proposal.before):
            runpy.run_path(str(project / "main.py"), run_name="__main__")
    else:
        runpy.run_path(str(project / "main.py"), run_name="__main__")
'''


class CliContractTests(unittest.TestCase):
    project: Path
    output_root: Path

    def case_dir(self):
        path = self.output_root / f"{self._testMethodName}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True)
        return path

    def run_cli(self, raws, *, approval="approve", max_turns=8, max_actions=16,
                deadline=10, workspace_setup=None, environment=None, **settings):
        directory = self.case_dir()
        workspace = directory / "workspace"
        shutil.copytree(MATERIALS / "inputs/sample_project", workspace)
        if workspace_setup:
            workspace_setup(workspace)
        before = self.file_bytes(workspace)
        question = directory / "question.txt"
        question.write_text("读取源码，修正端口闭区间，新建 unittest 边界测试并检查。", encoding="utf-8")
        worker, wrapper = directory / "worker.py", directory / "wrapper.py"
        worker.write_text(WORKER)
        wrapper.write_text(WRAPPER)
        scenario = directory / "scenario.json"
        scenario.write_text(json.dumps([{"raw": raw} for raw in raws]))
        audit, operations = directory / "transport.jsonl", directory / "operations.jsonl"
        options = directory / "options.json"
        output_dir = workspace / "forbidden-output" if settings.get("output_inside_workspace") else directory / "runs"
        settings["argv"] = ["--workspace", str(workspace), "--question", str(question),
                            "--output-dir", str(output_dir), "--runtime", str(directory / "runtime.json"),
                            "--max-turns", str(max_turns), "--max-actions", str(max_actions),
                            "--deadline-seconds", str(deadline)]
        options.write_text(json.dumps(settings))
        env = {"DEEPSEEK_API_KEY": SENTINEL, "DEEPSEEK_MODEL": "controlled-request-model",
               "DEEPSEEK_TIMEOUT_SECONDS": "2", "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(environment or {})
        process = subprocess.Popen([sys.executable, str(wrapper), str(self.project), str(worker),
                                    str(scenario), str(audit), str(operations), str(options)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", env=env, cwd=directory)
        out, err, incoming = [], [], queue.Queue()
        def reader(stream, target, notify=False):
            for line in stream:
                target.append(line)
                if notify:
                    incoming.put(line)
        threads = [threading.Thread(target=reader, args=(process.stdout, out), daemon=True),
                   threading.Thread(target=reader, args=(process.stderr, err, True), daemon=True)]
        for thread in threads:
            thread.start()
        approvals, external = [], None
        started = time.monotonic()
        try:
            while process.poll() is None or not incoming.empty():
                if time.monotonic() - started > 15:
                    self.fail("CLI 超过受控检查总等待上限")
                try:
                    line = incoming.get(timeout=0.05)
                except queue.Empty:
                    continue
                if not line.startswith("W03_APPROVAL "):
                    continue
                marker = json.loads(line.removeprefix("W03_APPROVAL "))
                proposal_dir = Path(marker["proposal_dir"])
                self.assertTrue(proposal_dir.is_dir())
                records = sorted(str(p.name) for p in proposal_dir.iterdir())
                self.assertTrue(records, "必须先保存真实提案，再由外部驱动决定")
                proposal = json.loads((proposal_dir / "proposal.json").read_text())
                candidate = (proposal_dir / "after.bin").read_bytes()
                diff = (proposal_dir / "diff.patch").read_bytes().decode("utf-8")
                self.assertEqual(proposal["proposal_id"], marker["proposal_id"])
                self.assertEqual(proposal["path"], marker["path"])
                self.assertEqual(proposal["diff"], diff)
                self.assertIn(diff.replace("\r\n", "\n"), "".join(err), "完整 diff 必须在审批 marker 前展示")
                record = {"marker": marker, "observed_proposal_files": records,
                          "observed_candidate_bytes": len(candidate), "observed_diff": diff,
                          "source": "author-controlled-driver", "time_ns": time.monotonic_ns()}
                approvals.append(record)
                if approval == "stale":
                    target = workspace / marker["path"]
                    external = target.read_bytes() + b"\n# external modification during approval\n"
                    target.write_bytes(external)
                    record["external_change"] = "after-preview-before-approval"
                elif approval == "create-race":
                    external = b"# externally created\n"
                    (workspace / marker["path"]).write_bytes(external)
                if approval == "eof":
                    process.stdin.close()
                elif approval == "cancel":
                    process.send_signal(signal.SIGINT)
                elif approval != "wait":
                    answer = "deny" if approval == "deny" else "approve"
                    token = "wrong-proposal-id" if approval == "wrong" else marker["proposal_id"]
                    process.stdin.write(f"{answer} {token}\n")
                    process.stdin.flush()
            process.wait(timeout=1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if not process.stdin.closed:
                process.stdin.close()
            for thread in threads:
                thread.join(timeout=1)
            process.stdout.close()
            process.stderr.close()
        stdout, stderr = "".join(out), "".join(err)
        self.assertNotIn(SENTINEL, stdout + stderr)
        self.assertTrue(stdout.strip(), stderr)
        try:
            output = json.loads(stdout)
        except json.JSONDecodeError:
            self.fail(f"stdout 必须是单个最终 JSON；stdout={stdout!r}; stderr={stderr!r}")
        self.assertEqual(process.returncode, output["exit_code"])
        if settings.get("output_inside_workspace"):
            self.assertEqual(output["status"], "blocked")
            self.assertEqual(process.returncode, 2)
            self.assertIsNone(output["output_directory"])
            self.assertIsNone(output["run_id"])
            self.assertEqual(output["model_calls"], 0)
            self.assertEqual(output["evidence_source"], SOURCE)
            self.assertFalse(output_dir.exists(), "无效输出位置不应创建任何证据目录")
            self.assertFalse(audit.exists())
            self.assertFalse(operations.exists())
            self.assertEqual(self.file_bytes(workspace), before)
            (directory / "check-observation.json").write_text(json.dumps({
                "evidence_source": SOURCE, "stdout": output, "stderr": stderr,
                "workspace_entries": sorted(p.name for p in workspace.iterdir()),
                "observed_requests": 0, "forbidden_output_created": False}, ensure_ascii=False, indent=2) + "\n")
            return {"output": output, "directory": directory}
        run_dir = Path(output["output_directory"])
        self.assertEqual(json.loads((run_dir / "result.json").read_text()), output)
        self.assertEqual(output["evidence_source"], SOURCE)
        self.assertIn(output["task_verdict"], {"fail", "unevaluated"})
        events = self.jsonl(run_dir / "events.jsonl")
        observed, ops = self.jsonl(audit), self.jsonl(operations)
        self.assertEqual(output["model_calls"], len(observed))
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[0]["event"], "run_started")
        self.assertEqual(events[-1]["event"], "run_finished")
        for index, row in enumerate(observed, 1):
            stored = json.loads((run_dir / f"turn-{index:03d}" / "request.json").read_text())
            self.assertEqual(stored, row["request"])
            for message in stored["messages"]:
                if message["role"] == "tool":
                    content = json.loads(message["content"])
                    self.assertNotIn("control", content, "宿主控制信号不应回填模型")
                    self.assertNotIn("input_files", content)
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(SENTINEL.encode(), path.read_bytes(), f"凭据泄漏：{path}")
        after = self.file_bytes(workspace)
        result = {"output": output, "code": process.returncode, "events": events, "observed": observed,
                  "operations": ops, "before": before, "after": after, "approvals": approvals,
                  "external": external, "directory": directory, "run_dir": run_dir, "stderr": stderr}
        observation = {"evidence_source": SOURCE, "model": "controlled-worker-no-network",
                       "shell": "controlled-boundary-no-container", "files": "actual-filesystem",
                       "stdout": output, "stderr": stderr, "approvals": approvals,
                       "observed_requests": len(observed), "operations": ops,
                       "before": {k: v.decode("utf-8", errors="replace") for k, v in before.items()},
                       "after": {k: v.decode("utf-8", errors="replace") for k, v in after.items()}}
        (directory / "check-observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2) + "\n")
        return result

    @staticmethod
    def jsonl(path):
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    @staticmethod
    def file_bytes(workspace):
        return {p.name: p.read_bytes() for p in workspace.iterdir() if p.is_file() and not p.is_symlink()}

    @staticmethod
    def operations(result, name):
        return [item for item in result["operations"] if item["operation"] == name]

    @staticmethod
    def finished(result):
        return [e for e in result["events"] if e["event"] == "tool_finished"]

    def assert_unknown_stopped(self, result):
        self.assertEqual(result["output"]["status"], "effect_unknown")
        self.assertEqual(result["code"], 1)
        self.assertEqual(len(self.operations(result, "commit_attempt")), 1, "效果未知后再次尝试写入")
        self.assertEqual(len(self.operations(result, "commit_returned")), 1)
        self.assertEqual(len(result["observed"]), 1)
        self.assertEqual(result["output"]["action_count"], 1)
        self.assertEqual(result["after"]["validator.py"], result["before"]["validator.py"].replace(OLD.encode(), NEW.encode()))
        self.assertNotIn("test_validator.py", result["after"])
        self.assertFalse(self.operations(result, "read_enter"))
        result_record = self.finished(result)[0]["result"]
        self.assertEqual(result_record["control"]["effect"], "unknown")
        self.assertTrue(result_record["control"]["blocks_run"])

    def assert_stale_preserved(self, result):
        self.assertEqual(result["after"]["validator.py"], result["external"], "旧批准覆盖了外部新字节")
        self.assertEqual(result["output"]["status"], "target_changed")
        self.assertEqual(result["code"], 1)
        self.assertEqual(len(self.operations(result, "commit_returned")), 0)
        self.assertEqual(len(result["observed"]), 1)
        self.assertFalse(self.operations(result, "read_enter"))

    def test_complete_controlled_flow_and_process_gates(self):
        result = self.run_cli([response(calls=[call()]), response(calls=[edit(), write()]),
                               response(calls=[call("check-1", "shell", {"command": "check"})]), response()])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["output"]["task_verdict"], "unevaluated")
        self.assertTrue(all(result["output"]["process_gates"].values()))
        self.assertEqual(result["output"]["read_successes"], 1)
        self.assertEqual(result["output"]["visible_read_successes"], 1)
        self.assertEqual(result["output"]["mutation_successes"], 2)
        self.assertEqual(result["output"]["action_count"], 4)
        self.assertEqual(len(self.operations(result, "commit_returned")), 2)
        self.assertEqual(result["after"]["test_validator.py"], TEST_SOURCE.encode())
        self.assertEqual(result["after"]["validator.py"], result["before"]["validator.py"].replace(OLD.encode(), NEW.encode()))
        self.assertEqual(len(result["approvals"]), 2)
        self.assertNotIn("def parse_port", json.dumps(result["observed"][0]["request"]["messages"]))
        self.assertIn("def parse_port", json.dumps(result["observed"][1]["request"]["messages"]))

    def test_stale_approval_preserves_actual_external_bytes(self):
        result = self.run_cli([response(calls=[edit(), call("later-read")]), response()], approval="stale")
        self.assert_stale_preserved(result)

    def test_actual_write_then_lost_receipt_stops(self):
        result = self.run_cli([response(calls=[edit(), write(), call("later-read")]), response()], after_commit_exception=True)
        self.assert_unknown_stopped(result)

    def test_cancel_after_confirmed_commit_keeps_applied_effect(self):
        result = self.run_cli([response(calls=[edit(), write()]), response()], cancel_after_commit=True)
        self.assertEqual(result["output"]["status"], "cancelled")
        self.assertEqual(result["output"]["mutation_successes"], 1)
        self.assertEqual(result["after"]["validator.py"], result["before"]["validator.py"].replace(OLD.encode(), NEW.encode()))
        self.assertEqual(len(self.operations(result, "commit_returned")), 1)
        self.assertNotIn("test_validator.py", result["after"])

    def test_mutant_retry_after_unknown_is_rejected(self):
        result = self.run_cli([response(calls=[edit(), write(), call("later-read")]), response()],
                              after_commit_exception=True, mutant_retry=True)
        with self.assertRaises(AssertionError):
            self.assert_unknown_stopped(result)
        self.assertGreater(len(self.operations(result, "commit_attempt")), 1)
        (result["directory"] / "mutation-verdict.json").write_text(json.dumps({"mutation": "retry-after-unknown", "rejected": True}))

    def test_mutant_skipped_version_check_is_rejected(self):
        result = self.run_cli([response(calls=[edit(), call("later-read")]), response()],
                              approval="stale", mutant_skip_version=True)
        with self.assertRaises(AssertionError):
            self.assert_stale_preserved(result)
        self.assertNotEqual(result["after"]["validator.py"], result["external"])
        (result["directory"] / "mutation-verdict.json").write_text(json.dumps({"mutation": "skip-version-check", "rejected": True}))

    def test_approval_rejection_invalid_eof_timeout_and_cancel(self):
        for mode, status in (("deny", "approval_denied"), ("wrong", "approval_invalid"),
                             ("eof", "approval_invalid"), ("wait", "approval_expired"), ("cancel", "cancelled")):
            with self.subTest(mode=mode):
                result = self.run_cli([response(calls=[edit(), write()]), response()], approval=mode,
                                      deadline=0.8 if mode == "wait" else 10)
                self.assertEqual(result["output"]["status"], status)
                self.assertEqual(result["before"], result["after"])
                self.assertEqual(len(result["observed"]), 1)
                self.assertFalse(self.operations(result, "commit_attempt"))
                self.assertEqual(result["output"]["action_count"], 1)

    def test_create_race_never_overwrites(self):
        result = self.run_cli([response(calls=[write(), edit()]), response()], approval="create-race")
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["after"]["test_validator.py"], result["external"])
        self.assertEqual(result["after"]["validator.py"], result["before"]["validator.py"])
        self.assertFalse(self.operations(result, "commit_returned"))

    def test_unique_byte_edit_preserves_bom_crlf_and_no_final_newline(self):
        data = b"\xef\xbb\xbf# prefix\r\nvalue = 'aaa'\r\n# suffix"
        result = self.run_cli([response(calls=[edit(old="value = 'aaa'", new="value = 'bbb'")]), response()],
                              workspace_setup=lambda p: (p / "validator.py").write_bytes(data))
        self.assertEqual(result["after"]["validator.py"], data.replace(b"aaa", b"bbb"))
        self.assertIn("No newline", result["stderr"])
        source = "# comment\u2028still comment\nport = 80\n".encode("utf-8")
        result = self.run_cli([response(calls=[edit(old="80", new="81")]), response()],
                              workspace_setup=lambda p: (p / "validator.py").write_bytes(source))
        self.assertEqual(result["after"]["validator.py"], source.replace(b"80", b"81"))
        self.assertIn("@@ -1,2 +1,2 @@", result["stderr"])
        self.assertNotIn("No newline at end of file", result["stderr"])

    def test_zero_multiple_overlap_empty_and_noop_rejected(self):
        cases = [("absent", "new", "match_not_found", b"aaa\n"),
                 ("aa", "new", "match_not_unique", b"aaa\n"),
                 ("same", "new", "match_not_unique", b"same\nsame\n"),
                 ("", "new", "invalid_arguments", b"aaa\n"),
                 ("aaa", "aaa", "invalid_arguments", b"aaa\n")]
        for old, new, error, data in cases:
            with self.subTest(error=error, old=old):
                result = self.run_cli([response(calls=[edit(old=old, new=new)]), response()],
                                      workspace_setup=lambda p, data=data: (p / "validator.py").write_bytes(data))
                self.assertEqual(self.finished(result)[0]["result"]["error"]["class"], error)
                self.assertEqual(result["after"], result["before"])
                self.assertFalse(result["approvals"])

    def test_strict_arguments_unknown_tools_and_command_allowlist(self):
        proposals = [call("bad-1", "read", {"path": "validator.py", "offset": True}),
                     call("bad-2", "read", {"path": "../validator.py"}),
                     call("bad-3", "read", {"path": "validator.py", "extra": 1}),
                     call("bad-4", "edit", {"path": "validator.py", "old_text": OLD}),
                     call("bad-5", "shell", {"command": "python validator.py"}),
                     call("bad-6", "unknown", {}),
                     call("bad-7", "read", '{"path":"validator.py","path":"test_validator.py"}')]
        result = self.run_cli([response(calls=proposals), response()])
        self.assertEqual(result["output"]["action_count"], len(proposals))
        self.assertEqual(result["before"], result["after"])
        self.assertFalse(result["operations"])
        self.assertTrue(all(not e["result"]["ok"] for e in self.finished(result)))
        for malformed in ("[" * 10000 + "0" + "]" * 10000,
                          '{"path":"validator.py","offset":' + "9" * 5000 + "}"):
            with self.subTest(argument=malformed[:40]):
                result = self.run_cli([response(calls=[call("malformed", "read", malformed)]), response()])
                self.assertEqual(result["code"], 0)
                self.assertEqual(self.finished(result)[0]["result"]["error"]["class"], "invalid_arguments")
                self.assertEqual(result["before"], result["after"])
                self.assertFalse(result["operations"])

    def test_length_last_turn_and_action_budget_whole_batch_zero(self):
        for kwargs, finish, status in (({}, "length", "length"), ({"max_turns": 1}, None, "turn_limit"),
                                       ({"max_actions": 1}, None, "action_limit")):
            with self.subTest(status=status):
                result = self.run_cli([response(calls=[edit(), call()], finish=finish)], **kwargs)
                self.assertEqual(result["output"]["status"], status)
                self.assertEqual(result["output"]["action_count"], 0)
                self.assertEqual(result["before"], result["after"])
                self.assertFalse(result["operations"])
                self.assertFalse(result["approvals"])
                self.assertEqual(len(self.finished(result)), 2)

    def test_historical_and_same_batch_ids_preflight(self):
        for raws, expected_reads in (([response(calls=[call("same"), call("same")])], 0),
                                     ([response(calls=[call("same")]), response(calls=[edit(), call("same")])], 1)):
            with self.subTest(reads=expected_reads):
                result = self.run_cli(raws)
                self.assertEqual(result["code"], 1)
                self.assertEqual(len(self.operations(result, "read_enter")), expected_reads)
                self.assertFalse(result["approvals"])
                self.assertEqual(result["before"], result["after"])

    def test_no_progress_only_failed_mutation_at_same_version(self):
        result = self.run_cli([response(calls=[edit("bad-1", "missing", "x")]),
                               response(calls=[edit("bad-2", "missing", "x"), call("unreached")]), response()])
        self.assertEqual(result["output"]["status"], "no_progress")
        self.assertEqual(len(result["observed"]), 2)
        self.assertEqual(result["output"]["action_count"], 2)
        self.assertFalse(result["operations"])
        continued = self.run_cli([response(calls=[edit("bad", "missing", "x")]),
                                  response(calls=[edit("good"), call("again-1"), call("again-2")]), response()])
        self.assertEqual(continued["code"], 0)
        self.assertEqual(continued["output"]["read_successes"], 2)

    def test_existing_write_and_candidate_limits_no_approval(self):
        for proposal, setup in ((write(), lambda p: (p / "test_validator.py").write_text("# keep\n")),
                                (write(content="x" * 4097), None),
                                (write(content="x\n" * 32769), None),
                                (write(content="\ud800"), None)):
            with self.subTest(content=proposal["function"]["arguments"][:60]):
                result = self.run_cli([response(calls=[proposal]), response()], workspace_setup=setup)
                self.assertEqual(result["before"], result["after"])
                self.assertFalse(result["approvals"])
                self.assertFalse(self.finished(result)[0]["result"]["ok"])

    def test_empty_new_file_has_preview_but_no_test_success(self):
        result = self.run_cli([response(calls=[write(content="")]), response()])
        self.assertEqual(result["after"]["test_validator.py"], b"")
        self.assertEqual(len(result["approvals"]), 1)
        self.assertEqual(len(self.operations(result, "commit_returned")), 1)
        self.assertIn("new file mode", result["stderr"])
        self.assertEqual(result["output"]["task_verdict"], "fail")

    def test_new_target_version_allows_previously_failed_edit(self):
        result = self.run_cli([response(calls=[edit("fail", "absent", "present")]),
                               response(calls=[edit("add", OLD, NEW + "  # absent")]),
                               response(calls=[edit("retry", "absent", "present")]), response()])
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(self.operations(result, "commit_returned")), 2)
        self.assertIn(b"# present", result["after"]["validator.py"])

    def test_check_result_does_not_cover_later_file_version(self):
        result = self.run_cli([response(calls=[call(), edit(), write()]),
                               response(calls=[call("check", "shell", {"command": "check"})]), response()],
                              change_after_check=True)
        self.assertEqual(result["code"], 0)
        self.assertFalse(result["output"]["process_gates"]["checked_version_matches"])
        self.assertEqual(result["output"]["task_verdict"], "fail")

    def test_shell_control_stops_batch_and_model(self):
        for status in ("command_timeout", "cancelled", "output_limit", "cleanup_unconfirmed"):
            with self.subTest(status=status):
                result = self.run_cli([response(calls=[call("check", "shell", {"command": "check"}), edit()]), response()],
                                      shell_status=status)
                self.assertEqual(result["output"]["status"], status)
                self.assertEqual(result["output"]["action_count"], 1)
                self.assertEqual(len(result["observed"]), 1)
                self.assertFalse(result["approvals"])
                self.assertEqual(result["after"], result["before"])

    def test_last_failed_check_invalidates_previous_success(self):
        result = self.run_cli([response(calls=[call(), edit(), write()]),
                               response(calls=[call("first-check", "shell", {"command": "check"})]),
                               response(calls=[call("last-check", "shell", {"command": "check"})]), response()],
                              shell_statuses=[None, "command_failed"])
        self.assertEqual(result["code"], 0)
        self.assertFalse(result["output"]["process_gates"]["last_check_succeeded"])
        self.assertEqual(result["output"]["task_verdict"], "fail")

    def test_nonzero_check_can_continue_and_usage_missing_stays_unknown(self):
        raw = response()
        del raw["usage"]
        result = self.run_cli([response(calls=[call("check", "shell", {"command": "check"})]),
                               response(calls=[call()]), raw], shell_status="command_failed")
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["output"]["read_successes"], 1)
        self.assertEqual(result["output"]["usage"]["usage_status"], "usage_unknown")
        self.assertIsNone(result["output"]["usage"]["total_tokens"])
        self.assertEqual(result["output"]["task_verdict"], "fail")

    def test_docker_preflight_blocked_zero_model_and_no_host_fallback(self):
        result = self.run_cli([response()], docker_missing=True)
        self.assertEqual(result["code"], 2)
        self.assertEqual(result["output"]["status"], "blocked")
        self.assertFalse(result["observed"])
        self.assertFalse(result["operations"])
        self.assertEqual(result["after"], result["before"])

    def test_workspace_output_rejected_before_log_directory_creation(self):
        self.run_cli([response()], output_inside_workspace=True)

    def test_text_only_stop_does_not_pass_task(self):
        result = self.run_cli([response("已经修好且所有测试通过。")])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["output"]["task_verdict"], "fail")
        self.assertEqual(result["before"], result["after"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--test", choices=unittest.defaultTestLoader.getTestCaseNames(CliContractTests))
    args = parser.parse_args()
    project = args.project.resolve()
    required = ("main.py", "w01_support.py", "w02_support.py", "w03_support.py", "w03_files.py", "w03_shell.py")
    missing = [name for name in required if not (project / name).is_file()]
    if missing:
        parser.error("工程目录缺少文件：" + ", ".join(missing))
    directory = args.output_dir.resolve() / f"check-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    CliContractTests.project, CliContractTests.output_root = project, directory
    suite = unittest.TestSuite([CliContractTests(args.test)]) if args.test else unittest.defaultTestLoader.loadTestsFromTestCase(CliContractTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {"evidence_source": SOURCE, "model": "controlled-worker-no-network",
               "shell": "controlled-boundary-no-container", "files": "actual-filesystem",
               "execution": "ran", "coverage": "hit", "task_verdict": "unevaluated",
               "verdict": "pass" if result.wasSuccessful() else "fail", "tests_run": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors), "selected_test": args.test,
               "project": str(project), "output_directory": str(directory),
               "failure_details": [{"test": str(test), "traceback": detail} for test, detail in result.failures + result.errors]}
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
