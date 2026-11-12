"""W07 CLI 检查：受控模型 SSE、真实本地 MCP 服务与 Journal；零模型 API。"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
import unittest
import uuid

MATERIALS = Path(__file__).resolve().parent
W06 = MATERIALS.parents[2] / "week-06/practices/PRA-W06"
spec = importlib.util.spec_from_file_location("w07_previous_checks", W06 / "check_cli.py")
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
call, response, read_jsonl = previous.call, previous.response, previous.read_jsonl
WORKER = previous.WORKER

# These two hooks change a real local connection or deliver a real SIGINT. They
# leave Host permission checks, producing manifests, SDK transport and cleanup live.
MCP_HOOKS = r'''
if settings.get("replace_mcp_connection") or settings.get("cancel_mcp_after_received"):
    import mcp_tools
    import signal
    import threading
    from w07_mcp import McpConnection
    original_execute_mcp = mcp_tools.McpHost.execute
    original_start_mcp = McpConnection.start
    started_connections = []
    def start_mcp(connection):
        initialization = original_start_mcp(connection)
        started_connections.append((connection, initialization))
        return initialization
    McpConnection.start = start_mcp
    hooked_mcp = False
    def execute_mcp(host, call, manifest, **kwargs):
        global hooked_mcp
        if not hooked_mcp:
            hooked_mcp = True
            connection, disclosed_initialization = next(
                item for item in started_connections if item[0].server_config["id"] == "rules")
            if settings.get("replace_mcp_connection"):
                old_id = connection.connection_id
                cleanup = connection.close()
                if not cleanup["cleanup_confirmed"]:
                    raise AssertionError("替换前旧连接必须确认回收")
                fresh = McpConnection(connection.server_config, sdk_python=connection.sdk_python,
                    evidence_dir=connection.evidence_dir.parent / "rules-replacement",
                    deadline=connection.deadline, cancelled=connection.cancelled)
                initialization = original_start_mcp(fresh)
                # Only the supplied connection object is changed. Host still holds
                # its original object; the actual new worker has a new identity.
                connection.__dict__.clear()
                connection.__dict__.update(fresh.__dict__)
                disclosed_initialization.clear()
                disclosed_initialization.update(initialization)
                observe("mcp_connection_replaced", old_id=old_id, new_id=connection.connection_id)
            if settings.get("cancel_mcp_after_received"):
                def cancel_when_received():
                    stderr = connection.evidence_dir / "stderr.txt"
                    limit = time.monotonic() + 5
                    while time.monotonic() < limit:
                        if stderr.exists() and '\"event\": \"heartbeat\"' in stderr.read_text():
                            observe("mcp_received_before_sigint", stderr=str(stderr))
                            os.kill(os.getpid(), signal.SIGINT)
                            return
                        time.sleep(0.02)
                threading.Thread(target=cancel_when_received, daemon=True).start()
        return original_execute_mcp(host, call, manifest, **kwargs)
    mcp_tools.McpHost.execute = execute_mcp
if settings.get("fail_mcp_rpc_log_after_call"):
    from w07_mcp import McpConnection
    original_mcp_request = McpConnection.request
    def request_mcp(connection, method, params, **kwargs):
        exchange = original_mcp_request(connection, method, params, **kwargs)
        if method == "tools/call":
            observe("mcp_rpc_log_failed", rpc_id=exchange.rpc_id, sent=exchange.sent)
            error = OSError("controlled supplied-transport evidence failure after actual response")
            error.mcp_exchange = exchange
            raise error
        return exchange
    McpConnection.request = request_mcp
'''
WRAPPER = previous.WRAPPER.replace('sys.argv = [str(entry), *settings["argv"]]',
                                   MCP_HOOKS + '\nsys.argv = [str(entry), *settings["argv"]]')


def lookup(server="rules", ident="query-1", document_id=None):
    return call(ident, f"mcp__{server}__lookup", {"document_id": document_id or
                ("port-format/r1" if server == "rules" else "port-checks/r1")})


class CliChecks(previous.CliChecks):
    def setUp(self):
        super().setUp()
        for path in (MATERIALS / "inputs/sample_project").iterdir():
            if path.is_file():
                shutil.copyfile(path, self.workspace / path.name)
        self.initial = self.workspace_bytes()

    def invoke(self, command, raws=(), **kwargs):
        previous.previous.previous.WORKER = WORKER
        previous.previous.previous.WRAPPER = WRAPPER
        return previous.previous.CliChecks.invoke(self, command, raws, **kwargs)

    def configure(self, *, mode="normal", control=None):
        config = {"schema_version": 1, "sdk_python": str(self.sdk_python), "servers": []}
        for name, document_id in (("rules", "port-format/r1"), ("checks", "port-checks/r1")):
            argv = [str(self.sdk_python), str(MATERIALS / "mcp_server.py"), "--server", name]
            if name == "rules":
                argv += ["--mode", mode]
                if control is not None:
                    argv += ["--control", str(control)]
            config["servers"].append({"id": name, "argv": argv,
                "allowed_tools": {"lookup": {"document_ids": [document_id]}},
                "resource_uris": [f"{name}://{document_id}"]})
        path = self.directory / "mcp.json"
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        return ["--mcp-config", str(path), "--max-tokens", "1024"]

    def evidence(self, observed):
        root = Path(observed["result"]["mcp_evidence_directory"])
        calls = [json.loads(path.read_text()) for path in sorted(root.glob("call-*.json"))]
        rpc = [json.loads(path.read_text()) for path in sorted(root.glob("rpc-*.json"))]
        return root, calls, rpc

    def wire_business(self, root):
        return [(path.parent.name, row["message"]) for path in root.glob("*/wire.jsonl")
                for row in read_jsonl(path) if row["direction"] == "send"
                and row.get("message", {}).get("method") in {"tools/call", "resources/read"}]

    def assert_clean(self, observed):
        cleanup = observed["result"]["mcp_cleanup"]
        self.assertIs(cleanup["cleanup_confirmed"], True, cleanup)
        root, _, _ = self.evidence(observed)
        for path in root.glob("*/cleanup.json"):
            item = json.loads(path.read_text())
            self.assertIs(item["cleanup_confirmed"], True, item)
            for key in ("worker_pid", "server_pid"):
                pid = item.get(key)
                if pid is not None:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
        events = read_jsonl(Path(observed["result"]["output_directory"]) / "events.jsonl")
        self.assertEqual(events[-1]["event"], "run_finished")
        return events

    def assert_blocked_feedback(self, observed, ident, error_class):
        self.assertEqual(observed["exit_code"], 1, observed["result"])
        self.assertEqual(len(observed["requests"]), 1)
        rows = read_jsonl(self.session)
        messages = [row["message"] for row in rows if row["type"] == "message"]
        feedback = [row for row in messages if row.get("tool_call_id") == ident]
        self.assertEqual(len(feedback), 1)
        self.assertEqual(json.loads(feedback[0]["content"])["error"]["class"], error_class)
        self.assertEqual(rows[-1]["type"], "run_end")
        self.assertNotEqual(rows[-1]["status"], "stop")
        self.assertEqual(self.workspace_bytes(), self.initial)
        self.assert_clean(observed)
        return feedback[0]

    def test_no_config_preserves_w06_request(self):
        frozen = self.entry_point.parent / ".artifacts/w07-before-upgrade/main.py"
        self.assertTrue(frozen.is_file(), "先运行公开 prepare_project.py 保存 W06 基线")
        raws = [response(calls=[call()]), response("已读，无修改。")]
        current = self.invoke("new", raws)
        self.assert_ok(current)
        original = self.entry_point
        try:
            self.entry_point = frozen
            baseline = self.invoke("new", raws, session=self.directory / "w06-session.jsonl")
        finally:
            self.entry_point = original
        self.assert_ok(baseline)
        self.assertEqual([item["request"] for item in current["requests"]],
                         [item["request"] for item in baseline["requests"]])
        self.assertNotIn("mcp_calls", current["result"])
        self.assertFalse(list(current["directory"].rglob("wire.jsonl")))
        self.facts = {"same_actual_requests": True, "mcp_processes_started": 0}

    def test_mcp_double_service_binding(self):
        observed = self.invoke("new", [response(calls=[lookup("rules", "rule-1"), lookup("checks", "check-1")]),
                                        response("已取得两份资料。")], extra=self.configure())
        self.assert_ok(observed)
        root, calls, rpc = self.evidence(observed)
        first, second = [item["request"] for item in observed["requests"]]
        names = {tool["function"]["name"] for tool in first["tools"]}
        self.assertTrue({"mcp__rules__lookup", "mcp__checks__lookup"} <= names)
        producing = json.loads((Path(observed["result"]["output_directory"]) / "projection-001.json").read_text())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.wire_business(root)), 2)
        for item, expected in zip(calls, ("rules", "checks")):
            alias = f"mcp__{expected}__lookup"
            self.assertEqual(item["binding"], producing["mcp_bindings"][alias])
            self.assertEqual(item["association"]["run_id"], producing["run_id"])
            self.assertEqual(item["association"]["turn"], 1)
            self.assertEqual(item["result"]["source"]["config_id"], expected)
            self.assertIs(item["exchange"]["sent"], True)
            wired = [(service, wire) for service, wire in self.wire_business(root)
                     if service == expected and wire["id"] == item["exchange"]["rpc_id"]]
            self.assertEqual(len(wired), 1)
            self.assertEqual(wired[0][1]["params"]["arguments"]["document_id"], item["result"]["data"]["document_id"])
            feedback = next(message for message in second["messages"] if message.get("tool_call_id") == item["association"]["call_id"])
            self.assertEqual(json.loads(feedback["content"])["source"], item["result"]["source"])
        self.assertNotEqual(calls[0]["binding"]["connection_id"], calls[1]["binding"]["connection_id"])
        self.assertEqual(observed["result"]["mcp_calls"], 2)
        self.assertEqual(observed["result"]["mcp_resource_reads"], 0)
        self.assert_clean(observed)
        self.assertEqual(self.workspace_bytes(), self.initial)
        self.facts = {"aliases_preserved": True, "producing_manifest_checked": True,
                      "actual_wire_ids_checked": True, "business_rpcs": 2}

    def test_mcp_replaced_connection_rejects_old_candidate(self):
        observed = self.invoke("new", [response(calls=[lookup()])], extra=self.configure(), replace_mcp_connection=True)
        self.assert_blocked_feedback(observed, "query-1", "mcp_binding_changed")
        root, calls, _ = self.evidence(observed)
        hook = next(row for row in observed["operations"] if row["operation"] == "mcp_connection_replaced")
        self.assertNotEqual(hook["old_id"], hook["new_id"])
        self.assertEqual(calls[0]["binding"]["connection_id"], hook["old_id"])
        self.assertEqual(self.wire_business(root), [])
        self.assertIs(calls[0]["result"]["mcp"]["sent"], False)
        self.facts = {"real_connection_replaced": True, "business_rpcs": 0, "old_binding_retained": True}

    def test_mcp_contract_change_rejected_before_dispatch(self):
        control = self.directory / "control.json"
        control.write_text('{}\n')
        raw = {"sse": previous.previous.frames(response(calls=[lookup()])),
               "change_resource": {"path": str(control), "text": '{"description":"Changed lookup contract"}\n'}}
        observed = self.invoke("new", [raw], extra=self.configure(control=control))
        self.assert_blocked_feedback(observed, "query-1", "mcp_binding_changed")
        root, _, _ = self.evidence(observed)
        self.assertEqual(self.wire_business(root), [])
        self.facts = {"contract_changed_after_request": True, "business_rpcs": 0}

    def test_mcp_denied_object_stops_and_pairs_error(self):
        observed = self.invoke("new", [response(calls=[lookup(document_id="private-port-plan/r1"), call("later-read")])],
                               extra=self.configure())
        self.assert_blocked_feedback(observed, "query-1", "mcp_object_denied")
        root, _, _ = self.evidence(observed)
        self.assertEqual(self.wire_business(root), [])
        self.assertFalse(any(row["operation"] == "read" for row in observed["operations"]))
        events = read_jsonl(Path(observed["result"]["output_directory"]) / "events.jsonl")
        later = [row for row in events if row["event"] == "tool_finished" and row["call_id"] == "later-read"]
        self.assertEqual(len(later), 1)
        self.assertIs(later[0]["executed"], False)
        before = self.session.read_bytes()
        rejected = self.invoke("resume", [response("不应发送")])
        self.assert_rejected(rejected, unchanged=before)
        self.facts = {"denied_object_business_rpcs": 0, "paired_error": True, "later_call_unexecuted": True,
                      "failed_session_resume_blocked": True}

    def error_mode(self, mode, error_class):
        observed = self.invoke("new", [response(calls=[lookup()])], extra=self.configure(mode=mode))
        self.assert_blocked_feedback(observed, "query-1", error_class)
        root, calls, _ = self.evidence(observed)
        self.assertEqual(len(self.wire_business(root)), 1)
        self.assertEqual(observed["result"]["mcp_calls"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["exchange"]["sent"], True)
        self.facts = {"business_rpcs": 1, "automatic_retry": False, "error_class": error_class}
        return calls[0]

    def test_mcp_business_error_is_paired(self):
        item = self.error_mode("business-error", "mcp_business_error")
        self.assertIs(item["exchange"]["result"]["isError"], True)
        self.assertIsNone(item["exchange"]["protocol_error"])

    def test_mcp_protocol_error_is_paired(self):
        item = self.error_mode("protocol-error", "mcp_protocol_error")
        self.assertIsNone(item["exchange"]["result"])
        self.assertEqual(item["exchange"]["protocol_error"]["code"], -32602)

    def test_mcp_cancel_after_service_receives(self):
        observed = self.invoke("new", [response(calls=[lookup(), call("later-read")])],
                               extra=self.configure(mode="slow"), cancel_mcp_after_received=True)
        self.assert_blocked_feedback(observed, "query-1", "cancelled")
        self.assertEqual(observed["result"]["status"], "cancelled")
        self.assertEqual(sum(row["operation"] == "mcp_received_before_sigint" for row in observed["operations"]), 1)
        root, calls, _ = self.evidence(observed)
        self.assertEqual(len(self.wire_business(root)), 1)
        self.assertEqual(observed["result"]["mcp_calls"], 1)
        stderr = root / "rules/stderr.txt"
        before = stderr.read_bytes()
        self.assertIn(b'heartbeat', before)
        time.sleep(0.15)
        self.assertEqual(stderr.read_bytes(), before)
        self.assertIs(calls[0]["exchange"]["sent"], True)
        self.assertEqual(calls[0]["result"]["control"]["effect"], "none")
        self.facts = {"service_received_before_sigint": True, "business_rpcs": 1,
                      "heartbeat_stopped": True, "owned_processes_reaped": True, "files_unchanged": True}

    def test_mcp_resource_link_is_not_automatically_read(self):
        observed = self.invoke("new", [response(calls=[lookup()]), response("已取得带来源的链接。")],
                               extra=self.configure(mode="resource-link"))
        self.assert_ok(observed)
        root, calls, _ = self.evidence(observed)
        links = [item for item in calls[0]["result"]["content"] if item["type"] == "resource_link"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["source"]["config_id"], "rules")
        self.assertEqual(observed["result"]["mcp_resource_reads"], 0)
        self.assertEqual([wire["method"] for _, wire in self.wire_business(root)], ["tools/call"])
        self.assert_clean(observed)
        self.facts = {"link_source_preserved": True, "resource_reads": 0}

    def test_mcp_explicit_resource_read_enters_request(self):
        uri = "rules://port-format/r1"
        observed = self.invoke("new", [response(calls=[call("resource-1", "mcp__rules__read_resource", {"uri": uri})]),
                                        response("资源正文已读。")], extra=self.configure())
        self.assert_ok(observed)
        root, calls, _ = self.evidence(observed)
        feedback = json.loads(next(message["content"] for message in observed["requests"][1]["request"]["messages"]
                                   if message.get("tool_call_id") == "resource-1"))
        self.assertEqual(feedback["contents"][0]["uri"], uri)
        self.assertEqual(feedback["contents"][0]["text"], calls[0]["exchange"]["result"]["contents"][0]["text"])
        self.assertEqual(feedback["source"], calls[0]["result"]["source"])
        self.assertEqual(observed["result"]["mcp_resource_reads"], 1)
        self.assertEqual(observed["result"]["mcp_calls"], 0)
        self.assert_clean(observed)
        self.facts = {"explicit_resource_read": True, "source_and_content_projected": True}

    def test_mcp_evidence_failure_retains_actual_exchange(self):
        observed = self.invoke("new", [response(calls=[lookup()])], extra=self.configure(),
                               fail_mcp_rpc_log_after_call=True)
        self.assertEqual(observed["exit_code"], 1)
        self.assertEqual(observed["result"]["status"], "persistence_error")
        exchange = observed["result"]["error"]["mcp_exchange"]
        self.assertIs(exchange["sent"], True)
        self.assertEqual(exchange["method"], "tools/call")
        self.assertEqual(exchange["result"]["structuredContent"]["document_id"], "port-format/r1")
        root, _, _ = self.evidence(observed)
        wire = self.wire_business(root)
        self.assertEqual(len(wire), 1)
        self.assertEqual(wire[0][1]["id"], exchange["rpc_id"])
        self.assertEqual(observed["result"]["mcp_calls"], 1)
        self.assertEqual(len(observed["requests"]), 1)
        self.assertEqual(self.workspace_bytes(), self.initial)
        self.assert_clean(observed)
        self.facts = {"actual_exchange_preserved": True, "business_rpcs": 1, "cleanup_confirmed": True}

    def test_mcp_full_workflow_request_budget(self):
        old = 'if not text or not text.isascii() or not text.isdecimal():'
        new = 'if not text or not text.isascii() or not text.isdecimal() or text[0] == "0":'
        old_test = "class ParsePortTest(unittest.TestCase):"
        new_test = old_test + '\n    def test_leading_zero_is_rejected(self):\n        for text in ("01", "00080"):\n            with self.subTest(text=text), self.assertRaises(ValueError):\n                parse_port(text)\n'
        raws = [response(calls=[lookup("rules", "rule-1"), lookup("checks", "check-1"),
                                call("read-validator", "read", {"path": "validator.py", "offset": 1, "limit": 80}),
                                call("read-tests", "read", {"path": "test_validator.py", "offset": 1, "limit": 80})]),
                response(calls=[call("edit-validator", "edit", {"path": "validator.py", "old_text": old, "new_text": new}),
                                call("edit-tests", "edit", {"path": "test_validator.py", "old_text": old_test, "new_text": new_test})]),
                response(calls=[call("check", "shell", {"command": "check"})]),
                response("修改已批准，受控检查边界返回成功；真实容器验收另行核对。")]
        observed = self.invoke("new", raws, extra=self.configure(),
                               question=(MATERIALS / "inputs/question-a.md").read_text())
        self.assert_ok(observed)
        self.assertEqual(len(observed["approvals"]), 2)
        self.assertEqual(sum(row["operation"] == "commit_returned" for row in observed["operations"]), 2)
        self.assertEqual(sum(row["operation"] == "shell_fixture" for row in observed["operations"]), 1)
        self.assertIn(new, (self.workspace / "validator.py").read_text())
        self.assertIn("test_leading_zero_is_rejected", (self.workspace / "test_validator.py").read_text())
        self.assertEqual(observed["result"]["mcp_calls"], 2)
        self.assert_clean(observed)
        root = Path(observed["result"]["output_directory"])
        budgets = []
        for index, sent in enumerate(observed["requests"], 1):
            manifest = json.loads((root / f"projection-{index:03d}.json").read_text())
            request = sent["request"]
            size = len(json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            estimate = (size + 2) // 3
            self.assertEqual(estimate, manifest["budget"]["estimated_input"])
            self.assertLessEqual(estimate + request["max_tokens"], 8192)
            budgets.append({"request": index, "request_bytes": size, "estimated_input": estimate,
                            "output_reserve": request["max_tokens"], "total_estimate": estimate + request["max_tokens"]})
        self.facts = {"budgets": budgets, "actual_request_count": len(observed["requests"]),
                      "approved_mutations": 2, "docker": "controlled-boundary-no-container"}

    def test_mcp_config_inside_workspace_rejected_before_journal(self):
        options = self.configure()
        inside = self.workspace / "mcp.json"
        shutil.copyfile(options[1], inside)
        options[1] = str(inside)
        observed = self.invoke("new", [response()], extra=options)
        self.assert_rejected(observed)
        self.assertFalse(self.session.exists())
        self.assertFalse(list(observed["directory"].rglob("wire.jsonl")))
        self.facts = {"journal_created": False, "mcp_processes_started": 0}


TESTS = ["test_no_config_preserves_w06_request", "test_read_view_preserves_raw_result_and_next_offset",
         "test_summary_committed_before_continuation", "test_skill_body_then_resource_enters_later_requests",
         "test_mcp_double_service_binding", "test_mcp_replaced_connection_rejects_old_candidate",
         "test_mcp_contract_change_rejected_before_dispatch", "test_mcp_denied_object_stops_and_pairs_error",
         "test_mcp_business_error_is_paired", "test_mcp_protocol_error_is_paired",
         "test_mcp_cancel_after_service_receives", "test_mcp_resource_link_is_not_automatically_read",
         "test_mcp_explicit_resource_read_enters_request", "test_mcp_config_inside_workspace_rejected_before_journal",
         "test_mcp_full_workflow_request_budget", "test_mcp_evidence_failure_retains_actual_exchange"]


def run_suite(entry, sdk_python, output_root, names=None):
    entry, sdk_python, output_root = entry.resolve(), sdk_python.absolute(), output_root.absolute()
    output_root.mkdir(parents=True, exist_ok=False)
    class Bound(CliChecks):
        pass
    Bound.entry_point, Bound.sdk_python, Bound.output_root = entry, sdk_python, output_root
    names = names or TESTS
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Bound(name) for name in names))
    report = {"evidence_source": "controlled-fixture", "external_model_requests": 0,
              "mcp_transport": "real-local-sdk-stdio", "docker": "controlled-boundary-no-container",
              "entry": str(entry), "sdk_python": str(sdk_python), "output_directory": str(output_root),
              "selected_cases": names, "tests_run": result.testsRun, "count": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "passed": result.testsRun - len({case.id() for case, _ in result.failures + result.errors}),
              "failed": len({case.id() for case, _ in result.failures + result.errors}),
              "verdict": "pass" if result.wasSuccessful() else "fail",
              "failure_details": [{"case": str(case), "traceback": error} for case, error in result.failures + result.errors]}
    (output_root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--student-dir", type=Path)
    target.add_argument("--entry", type=Path)
    parser.add_argument("--sdk-python", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case", choices=TESTS)
    args = parser.parse_args()
    entry = args.entry or args.student_dir / "main.py"
    report = run_suite(entry, args.sdk_python, args.output_dir / ("check-" + uuid.uuid4().hex[:8]),
                       [args.case] if args.case else TESTS)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
