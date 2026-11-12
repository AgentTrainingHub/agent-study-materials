"""使用真实本地 MCP 服务验证公开底座；不请求模型。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import unittest
from uuid import uuid4

MATERIALS = Path(__file__).resolve().parent


class SdkChecks(unittest.TestCase):
    def setUp(self):
        self.directory = self.output / self._testMethodName
        self.directory.mkdir()
        self.connections = []
        self.serial = 0

    def tearDown(self):
        cleanups = [connection.close() for connection in self.connections]
        (self.directory / "cleanup-results.json").write_text(json.dumps(cleanups, indent=2) + "\n")
        self.assertTrue(all(item["cleanup_confirmed"] for item in cleanups), cleanups)
        for item in cleanups:
            for key in ("worker_pid", "server_pid"):
                pid = item[key]
                if pid is None:
                    continue
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def connect(self, *, server="rules", mode="normal", sdk_python=None):
        self.serial += 1
        python = str(sdk_python or self.sdk_python)
        spec = {"id": server, "argv": [python, str(MATERIALS / "mcp_server.py"), "--server", server, "--mode", mode],
                "allowed_tools": {"lookup": {"document_ids": ["port-format/r1" if server == "rules" else "port-checks/r1"]}},
                "resource_uris": [f"{server}://port-format/r1"]}
        connection = self.module.McpConnection(spec, sdk_python=python,
                                               evidence_dir=self.directory / f"connection-{self.serial}",
                                               deadline=time.monotonic() + 30, cancelled=lambda: False)
        self.connections.append(connection)
        connection.start()
        return connection

    def request(self, connection, method, params, *, cancelled=lambda: False, timeout=5):
        result = connection.request(method, params, deadline=time.monotonic() + timeout, cancelled=cancelled)
        with (self.directory / "exchanges.jsonl").open("a") as handle:
            handle.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
        return result

    def lookup(self, connection, document_id="port-format/r1", **options):
        return self.request(connection, "tools/call", {"name": "lookup", "arguments": {"document_id": document_id}}, **options)

    def test_versions_and_wire_identity(self):
        connection = self.connect()
        self.assertEqual(connection.initialization["sdk_version"], "1.30.0")
        self.assertEqual(connection.initialization["protocol_version"], "2025-11-25")
        listed = self.request(connection, "tools/list", {})
        called = self.lookup(connection)
        self.assertEqual(listed.result["tools"][0]["name"], "lookup")
        self.assertEqual(called.result["structuredContent"]["revision"], "r1")
        self.assertTrue(called.sent)
        wire = [json.loads(line) for line in (connection.evidence_dir / "wire.jsonl").read_text().splitlines()]
        sends = [item["message"] for item in wire if item["direction"] == "send"]
        self.assertEqual(sends[0]["method"], "initialize")
        self.assertEqual(sends[1]["method"], "notifications/initialized")
        self.assertTrue(any(item.get("id") == called.rpc_id and item.get("method") == "tools/call" for item in sends))
        self.assertEqual(connection.stats(), {"mcp_requests": 3, "mcp_calls": 1, "mcp_resource_reads": 0})
        self.assertEqual(connection.close()["worker_returncode"], 0)

    def test_three_objects_and_pagination(self):
        connection = self.connect()
        first = self.request(connection, "resources/list", {})
        second = self.request(connection, "resources/list", {"cursor": first.result["nextCursor"]})
        self.assertEqual(first.result["nextCursor"], "page-2")
        self.assertNotIn("nextCursor", second.result)
        resource = self.request(connection, "resources/read", {"uri": "rules://port-format/r1"})
        called = self.lookup(connection)
        listed = self.request(connection, "prompts/list", {})
        prompt = self.request(connection, "prompts/get", {"name": "explain_rule", "arguments": {"document_id": "port-format/r1"}})
        self.assertEqual(resource.result["contents"][0]["text"], called.result["structuredContent"]["text"])
        self.assertEqual(listed.result["prompts"][0]["name"], "explain_rule")
        self.assertEqual(prompt.result["messages"][0]["role"], "user")
        self.assertEqual(connection.stats()["mcp_resource_reads"], 1)

    def test_business_error_is_result(self):
        result = self.lookup(self.connect(mode="business-error"))
        self.assertTrue(result.result["isError"])
        self.assertIsNone(result.protocol_error)
        self.assertIsNone(result.transport_error)

    def test_protocol_error_is_separate(self):
        result = self.lookup(self.connect(mode="protocol-error"))
        self.assertIsNone(result.result)
        self.assertIsNone(result.transport_error)
        self.assertEqual(result.protocol_error["code"], -32602)

    def test_crash_does_not_kill_other_service(self):
        failed = self.connect(mode="crash")
        other = self.connect(server="checks")
        result = self.lookup(failed)
        self.assertIsNotNone(result.transport_error)
        self.assertTrue(result.sent)
        healthy = self.lookup(other, "port-checks/r1")
        self.assertFalse(healthy.result["isError"])
        self.assertEqual(healthy.result["structuredContent"]["document_id"], "port-checks/r1")
        self.assertEqual(failed.stats()["mcp_calls"], 1)
        self.assertEqual(other.stats()["mcp_calls"], 1)

    def test_cancel_after_service_received(self):
        connection = self.connect(mode="slow")
        stderr = connection.evidence_dir / "stderr.txt"
        def cancelled():
            return stderr.exists() and '"event": "heartbeat"' in stderr.read_text()
        result = self.lookup(connection, cancelled=cancelled)
        self.assertEqual(result.transport_error["class"], "cancelled")
        self.assertTrue(result.sent)
        self.assertTrue(connection.close()["cleanup_confirmed"])
        before = stderr.read_bytes()
        time.sleep(0.12)
        self.assertEqual(stderr.read_bytes(), before)
        self.assertEqual(connection.stats()["mcp_calls"], 1)

    def test_timeout_keeps_sent_fact(self):
        connection = self.connect(mode="slow")
        result = self.lookup(connection, timeout=0.15)
        self.assertEqual(result.transport_error["class"], "mcp_timeout")
        self.assertTrue(result.sent)
        self.assertIsNone(result.result)

    def test_bad_frame_stops(self):
        connection = self.connect(mode="bad-frame")
        result = self.lookup(connection)
        self.assertIsNotNone(result.transport_error)
        self.assertIsNone(result.result)
        self.assertEqual(connection.stats()["mcp_calls"], 1)

    def test_oversized_wire_stops(self):
        result = self.lookup(self.connect(mode="oversized"))
        self.assertIsNotNone(result.transport_error)
        self.assertIsNone(result.result)

    def test_result_validation_is_host_work(self):
        result = self.lookup(self.connect(mode="bad-output"))
        self.assertEqual(result.result["structuredContent"]["revision"], 12)
        self.assertIsNone(result.transport_error)

    def test_resource_link_is_not_read(self):
        connection = self.connect(mode="resource-link")
        result = self.lookup(connection)
        self.assertEqual(result.result["content"][1]["type"], "resource_link")
        self.assertEqual(connection.stats()["mcp_resource_reads"], 0)

    def test_callback_exception_reaps(self):
        connection = self.connect(mode="slow")
        stderr = connection.evidence_dir / "stderr.txt"
        def cancelled():
            if '"event": "heartbeat"' in stderr.read_text():
                raise RuntimeError("controlled_callback_failure")
            return False
        with self.assertRaisesRegex(RuntimeError, "controlled_callback_failure") as caught:
            self.lookup(connection, cancelled=cancelled)
        self.assertTrue(caught.exception.mcp_cleanup["cleanup_confirmed"])
        self.assertIs(caught.exception.mcp_exchange.sent, True)
        self.assertEqual(caught.exception.mcp_exchange.method, "tools/call")
        self.assertEqual(connection.stats()["mcp_calls"], 1)

    def test_narrow_environment(self):
        marker = "w07-controlled-key-" + uuid4().hex
        old = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = marker
        try:
            connection = self.connect()
            self.lookup(connection)
            connection.close()
            stderr = (connection.evidence_dir / "stderr.txt").read_text()
            self.assertIn('"inherited_model_key": false', stderr)
            self.assertTrue(all(marker not in path.read_text() for path in connection.evidence_dir.glob("*.txt")))
        finally:
            if old is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = old

    def test_config_snapshot_and_rejections(self):
        path = self.directory / "mcp.json"
        source = {"schema_version": 1, "sdk_python": str(self.sdk_python), "servers": [{
            "id": "rules", "argv": [str(self.sdk_python), str(MATERIALS / "mcp_server.py"), "--server", "rules"],
            "allowed_tools": {"lookup": {"document_ids": ["port-format/r1"]}}, "resource_uris": []}]}
        path.write_text(json.dumps(source))
        result = self.module.load_config(path)
        self.assertEqual(result["servers"][0]["id"], "rules")
        self.assertEqual(len(result["config_digest"]), 64)
        source["servers"][0]["env"] = {"DEEPSEEK_API_KEY": "synthetic"}
        path.write_text(json.dumps(source))
        with self.assertRaises(self.module.McpError):
            self.module.load_config(path)
        path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(self.module.McpError):
            self.module.load_config(path)
        link = self.directory / "link.json"
        link.symlink_to(path)
        with self.assertRaises(self.module.McpError):
            self.module.load_config(link)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    parser.add_argument("--sdk-python", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    module_spec = importlib.util.spec_from_file_location("w07_student_mcp", args.student_dir / "w07_mcp.py")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    SdkChecks.module, SdkChecks.sdk_python, SdkChecks.output = module, args.sdk_python.absolute(), output
    suite = (unittest.TestSuite([SdkChecks(args.case)]) if args.case
             else unittest.defaultTestLoader.loadTestsFromTestCase(SdkChecks))
    with (output / "checks.txt").open("w") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    failed_cases = {case.id() for case, _ in result.failures + result.errors}
    report = {"evidence_source": "local-mcp", "external_model_requests": 0,
              "count": result.testsRun, "passed": result.testsRun - len(failed_cases),
              "failed": len(failed_cases),
              "failures": [{"case": str(case), "detail": detail} for case, detail in result.failures + result.errors]}
    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
