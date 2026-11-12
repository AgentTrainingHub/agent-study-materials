"""从同一 CLI 入口检查确定性行为；不调用真实模型。"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib
import io
import json
import os
import runpy
import sys
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest.mock import patch

MATERIALS = Path(__file__).resolve().parent
SENTINEL = "w01-test-secret-must-never-appear"
USAGE = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


def response_body(text="0.75；whole 为零时抛出 ZeroDivisionError。", finish="stop"):
    return {
        "id": "controlled-response",
        "model": "controlled-actual-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": dict(USAGE),
    }


class ControlledResponse:
    status = 200
    headers = {"x-request-id": "controlled-request"}

    def __init__(self, body, read_error=None):
        self.body = body
        self.read_error = read_error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.body


class CliContractTests(unittest.TestCase):
    project: Path
    output_root: Path
    support = None

    def run_cli(
        self, raw=None, *, transport_error=None, env=None, max_tokens=1024,
        question=None, code=None, output_dir=None, read_error=None,
    ):
        case_dir = self.output_root / f"{self._testMethodName}-{uuid.uuid4().hex[:8]}"
        case_dir.mkdir(parents=True)
        code_file = case_dir / "input.py"
        code_file.write_text(
            code if code is not None else (MATERIALS / "inputs/ratio.py").read_text(),
            encoding="utf-8",
        )
        question_file = case_dir / "question.txt"
        question_file.write_text(
            question if question is not None else (MATERIALS / "inputs/question.txt").read_text(),
            encoding="utf-8",
        )
        before_code = code_file.read_bytes()
        if raw is None:
            raw = response_body()
        body = raw if isinstance(raw, bytes) else json.dumps(raw, ensure_ascii=False).encode()
        observed = []

        def controlled_open(request, timeout):
            observed.append({"body": json.loads(request.data), "url": request.full_url, "timeout": timeout})
            if transport_error is not None:
                raise transport_error
            return ControlledResponse(body, read_error)

        argv = [
            str(self.project / "main.py"), "--code", str(code_file),
            "--question", str(question_file), "--output-dir", str(output_dir or case_dir / "runs"),
            "--max-tokens", str(max_tokens),
        ]
        environment = {
            "DEEPSEEK_API_KEY": SENTINEL, "DEEPSEEK_MODEL": "controlled-request-model",
            "DEEPSEEK_TIMEOUT_SECONDS": "60",
        }
        environment.update(env or {})
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", argv),
            patch.object(self.support, "EVIDENCE_SOURCE", "controlled-fixture"),
            patch("urllib.request.OpenerDirector.open", side_effect=controlled_open),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                runpy.run_path(str(self.project / "main.py"), run_name="__main__")
            except SystemExit as exc:
                exit_code = exc.code
            else:
                exit_code = 0
        self.assertEqual(before_code, code_file.read_bytes(), "CLI 修改了练习源文件")
        self.assertNotIn(SENTINEL, stdout.getvalue() + stderr.getvalue())
        output = json.loads(stdout.getvalue())
        receipt_path = Path(output["receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["evidence_source"], "controlled-fixture")
        self.assertEqual(receipt["task_verdict"], "not_evaluated")
        self.assertEqual(receipt_path.parent.name, receipt["run_id"])
        for name in ("status", "text", "raw_stop_reason", "usage_raw", "usage_status", "error"):
            self.assertEqual(receipt[name], output[name], f"stdout 与回执的 {name} 不一致")
        self.assertEqual(receipt["attempts"], len(observed))
        self.assertLessEqual(len(observed), 1, "一次 CLI 发出了多次请求")
        self.assertGreaterEqual(receipt["elapsed_ms"], 0)
        self.assertTrue(receipt["started_at"])
        self.assertTrue(receipt["finished_at"])
        for evidence in receipt_path.parent.iterdir():
            text = evidence.read_text()
            self.assertNotIn(SENTINEL, text, f"凭据泄漏到 {evidence.name}")
            self.assertNotIn(f"Bearer {SENTINEL}", text)
        artifacts = receipt["artifacts"]
        if observed:
            self.assertEqual(observed[0]["url"], self.support.ENDPOINT)
            stored = json.loads((receipt_path.parent / artifacts["observable_request"]).read_text())
            self.assertEqual(stored, self.support.sanitize(observed[0]["body"], (SENTINEL,)))
            self.assertEqual(receipt["execution_state"], "ran")
            if receipt["artifacts"]["raw_response"] is not None:
                raw_path = receipt_path.parent / receipt["artifacts"]["raw_response"]
                self.assertTrue(raw_path.is_file())
                if transport_error is None and read_error is None:
                    try:
                        expected_raw = json.loads(body)
                        json.dumps(expected_raw, ensure_ascii=False).encode("utf-8")
                    except (ValueError, RecursionError):
                        expected_raw = {"unparsed_body": body.decode("utf-8", errors="replace")}
                    self.assertEqual(
                        json.loads(raw_path.read_text()),
                        self.support.sanitize(expected_raw, (SENTINEL,)),
                        "原始响应证据与传输返回内容不一致",
                    )
        else:
            self.assertIsNone(artifacts["observable_request"])
            self.assertIsNone(artifacts["raw_response"])
            self.assertEqual(receipt["execution_state"], "blocked")
        (case_dir / "cli-result.json").write_text(
            json.dumps({
                "evidence_source": "controlled-fixture", "execution": "ran",
                "coverage": "hit", "exit_code": exit_code, "stdout": output,
                "stderr": stderr.getvalue(),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        return exit_code, output, receipt, observed

    def test_stop_and_actual_request(self):
        shared_output = self.output_root / "repeated-run-output"
        code, output, receipt, observed = self.run_cli(output_dir=shared_output)
        self.assertEqual((code, output["status"]), (0, "stop"))
        self.assertEqual(output["raw_stop_reason"], "stop")
        self.assertEqual(output["usage_status"], "known")
        self.assertEqual(output["usage_raw"], USAGE)
        self.assertIn("0.75", output["text"])
        body = observed[0]["body"]
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(body["stream"], False)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(set(body), {"model", "messages", "max_tokens", "stream", "temperature", "thinking"})
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        self.assertTrue(all(set(message) == {"role", "content"} for message in body["messages"]))
        self.assertNotIn("tools", body)
        self.assertIn((MATERIALS / "inputs/ratio.py").read_text().strip(), body["messages"][1]["content"])
        self.assertIn((MATERIALS / "inputs/question.txt").read_text().strip(), body["messages"][1]["content"])
        self.assertEqual(receipt["requested_model"], "controlled-request-model")
        self.assertEqual(receipt["actual_model"], "controlled-actual-model")
        self.assertEqual(receipt["request_id"], "controlled-request")
        self.assertEqual(receipt["http_status"], 200)
        first_receipt = Path(output["receipt_path"])
        first_files = {path.name: path.read_bytes() for path in first_receipt.parent.iterdir()}
        next_code, next_output, next_receipt, _ = self.run_cli(
            response_body("第二次受控回答"), output_dir=shared_output,
        )
        self.assertEqual(next_code, 0)
        self.assertNotEqual(receipt["run_id"], next_receipt["run_id"])
        self.assertNotEqual(output["receipt_path"], next_output["receipt_path"])
        self.assertEqual(first_receipt.parent.parent, Path(next_output["receipt_path"]).parent.parent)
        self.assertEqual(len(list(shared_output.iterdir())), 2)
        self.assertEqual(
            first_files, {path.name: path.read_bytes() for path in first_receipt.parent.iterdir()},
            "同一输出目录再次运行时覆盖了已有证据",
        )

    def test_length_keeps_partial_text(self):
        code, output, _, _ = self.run_cli(response_body("尚未完成的解释", "length"))
        self.assertEqual((code, output["status"]), (1, "length"))
        self.assertEqual(output["text"], "尚未完成的解释")
        self.assertEqual(output["raw_stop_reason"], "length")
        self.assertEqual(output["error"]["stage"], "generation")
        self.assertEqual(output["error"]["class"], "output_truncated")

    def test_missing_usage_keeps_valid_answer(self):
        raw = response_body()
        del raw["usage"]
        code, output, receipt, _ = self.run_cli(raw)
        self.assertEqual((code, output["status"]), (0, "stop"))
        self.assertEqual(output["usage_status"], "usage_unknown")
        self.assertIsNone(output["usage_raw"])
        self.assertIsNone(receipt["usage_raw"])

    def test_incomplete_usage_is_unknown(self):
        for usage in ({}, {"prompt_tokens": 20}, {**USAGE, "total_tokens": 0}, {**USAGE, "prompt_tokens": True}):
            with self.subTest(usage=usage):
                raw = response_body()
                raw["usage"] = usage
                code, output, _, _ = self.run_cli(raw)
                self.assertEqual(code, 0)
                self.assertEqual(output["usage_status"], "usage_unknown")
                self.assertEqual(output["usage_raw"], usage)

    def test_unknown_and_missing_finish_reason(self):
        for reason in ("something-new", None, "tool_calls", "content_filter"):
            with self.subTest(reason=reason):
                code, output, _, _ = self.run_cli(response_body(finish=reason))
                self.assertEqual((code, output["status"]), (1, "error"))
                self.assertEqual(output["raw_stop_reason"], reason)
        tool_fields = {
            "tool_calls": [{"id": "controlled-tool", "type": "function", "function": {"name": "write", "arguments": "{}"}}],
            "function_call": {"name": "write", "arguments": "{}"},
        }
        for field, value in tool_fields.items():
            with self.subTest(field=field):
                raw = response_body(finish="stop")
                raw["choices"][0]["message"][field] = value
                code, output, _, _ = self.run_cli(raw)
                self.assertEqual((code, output["status"]), (1, "error"))
                self.assertEqual(output["raw_stop_reason"], "stop")

    def test_empty_and_nontext_content(self):
        for content in ("", "   ", None, [{"text": "fragment"}]):
            with self.subTest(content=content):
                code, output, _, _ = self.run_cli(response_body(text=content))
                self.assertEqual((code, output["status"]), (1, "error"))

    def test_invalid_response_shape(self):
        cases = [[], {}, {"choices": []}, {"model": "x", "choices": [None]}]
        for message in (None, [], {"content": "answer"}, {"role": "user", "content": "answer"}):
            raw = response_body()
            raw["choices"][0]["message"] = message
            cases.append(raw)
        for raw in cases:
            with self.subTest(raw=raw):
                code, output, _, _ = self.run_cli(raw)
                self.assertEqual((code, output["status"]), (1, "error"))
                self.assertEqual(output["error"]["stage"], "response")
                self.assertEqual(output["error"]["class"], "invalid_response")

    def test_missing_actual_model(self):
        for model in (None, "", "  "):
            with self.subTest(model=model):
                raw = response_body()
                raw["model"] = model
                code, output, _, _ = self.run_cli(raw)
                self.assertEqual((code, output["status"]), (1, "error"))

    def test_invalid_json(self):
        for body in (
            b"not valid JSON", b"[" * 10000 + b"0" + b"]" * 10000, b"9" * 5000,
            json.dumps(response_body(text="\ud800")).encode("utf-8"),
        ):
            with self.subTest(body_length=len(body)):
                code, output, receipt, _ = self.run_cli(body)
                self.assertEqual((code, output["status"]), (1, "error"))
                self.assertEqual(output["error"]["class"], "invalid_json")
                self.assertIsNotNone(receipt["artifacts"]["raw_response"])

    def test_timeout_without_fabricated_response(self):
        code, output, receipt, observed = self.run_cli(transport_error=TimeoutError())
        self.assertEqual((code, output["status"]), (1, "error"))
        self.assertEqual(output["error"]["class"], "provider_timeout")
        self.assertEqual(len(observed), 1)
        self.assertIsNone(receipt["http_status"])
        self.assertIsNone(receipt["artifacts"]["raw_response"])
        code, output, receipt, observed = self.run_cli(read_error=TimeoutError("read timeout"))
        self.assertEqual((code, output["status"]), (1, "error"))
        self.assertEqual(output["error"]["class"], "provider_timeout")
        self.assertEqual(receipt["http_status"], 200)
        self.assertEqual(len(observed), 1)
        self.assertIsNone(receipt["artifacts"]["raw_response"])

    def test_network_error(self):
        for error in (urllib.error.URLError("offline"), ConnectionResetError("connection reset")):
            code, output, _, _ = self.run_cli(transport_error=error)
            self.assertEqual((code, output["status"]), (1, "error"))
            self.assertEqual(output["error"]["class"], "network_error")
        code, output, receipt, _ = self.run_cli(read_error=http.client.IncompleteRead(b'{"model":', 10))
        self.assertEqual((code, output["status"]), (1, "error"))
        self.assertEqual(output["error"]["class"], "network_error")
        self.assertEqual(receipt["http_status"], 200)
        self.assertIsNone(receipt["artifacts"]["raw_response"])

    def test_http_error_preserves_status_and_no_retry(self):
        for status, expected in ((401, "authentication_failed"), (429, "rate_limited"), (503, "provider_unavailable")):
            with self.subTest(status=status):
                exc = urllib.error.HTTPError(
                    self.support.ENDPOINT, status, "controlled HTTP error", {},
                    io.BytesIO(b'{"error":{"message":"controlled"}}'),
                )
                code, output, receipt, observed = self.run_cli(transport_error=exc)
                self.assertEqual((code, output["status"]), (1, "error"))
                self.assertEqual(output["error"]["class"], expected)
                self.assertEqual(receipt["http_status"], status)
                self.assertEqual(len(observed), 1)
                self.assertIsNotNone(receipt["artifacts"]["raw_response"])
        for read_error, expected in ((TimeoutError("read timeout"), "provider_timeout"),
                                     (http.client.IncompleteRead(b"failure", 10), "network_error")):
            with self.subTest(read_error=type(read_error).__name__):
                body = io.BytesIO()
                body.read = lambda error=read_error: (_ for _ in ()).throw(error)
                exc = urllib.error.HTTPError(self.support.ENDPOINT, 503, "controlled", {}, body)
                code, output, receipt, observed = self.run_cli(transport_error=exc)
                self.assertEqual((code, output["status"]), (1, "error"))
                self.assertEqual(output["error"]["stage"], "transport")
                self.assertEqual(output["error"]["class"], expected)
                self.assertEqual(receipt["http_status"], 503)
                self.assertEqual(len(observed), 1)
                self.assertIsNone(receipt["artifacts"]["raw_response"])

    def test_configuration_blocks_before_request(self):
        cases = (
            {"DEEPSEEK_API_KEY": ""}, {"DEEPSEEK_MODEL": ""},
            {"DEEPSEEK_TIMEOUT_SECONDS": "nan"}, {"DEEPSEEK_TIMEOUT_SECONDS": "0"},
            {"DEEPSEEK_TIMEOUT_SECONDS": "121"}, {"DEEPSEEK_TIMEOUT_SECONDS": "oops"},
            {"DEEPSEEK_API_BASE": "http://localhost:8000"},
        )
        for env in cases:
            with self.subTest(env=env):
                code, output, _, observed = self.run_cli(env=env)
                self.assertEqual((code, output["status"]), (2, "blocked"))
                self.assertEqual(observed, [])

    def test_token_limit_blocks_before_request(self):
        for limit in (0, -1, 4097):
            with self.subTest(limit=limit):
                code, output, _, observed = self.run_cli(max_tokens=limit)
                self.assertEqual((code, output["status"]), (2, "blocked"))
                self.assertEqual(observed, [])

    def test_blank_input_blocks_before_request(self):
        for inputs in ({"question": "   \n"}, {"code": ""}, {"code": " \n"}):
            with self.subTest(inputs=inputs):
                code, output, _, observed = self.run_cli(**inputs)
                self.assertEqual((code, output["status"]), (2, "blocked"))
                self.assertEqual(observed, [])

    def test_secret_redaction_in_answer_and_request(self):
        raw = response_body(text=f"泄漏哨兵 {SENTINEL} Bearer {SENTINEL}")
        code, output, _, _ = self.run_cli(raw, question=f"测试哨兵 {SENTINEL}")
        self.assertEqual(code, 0)
        self.assertIn("[REDACTED]", output["text"])

    def test_claimed_file_edit_has_no_external_effect(self):
        code, output, _, _ = self.run_cli(response_body(text="已修改 input.py，解决了除零问题。"))
        self.assertEqual(code, 0)
        self.assertIn("已修改", output["text"])
        # run_cli 对每个用例均逐字节核对输入文件，任务判定继续保留 not_evaluated。

    def test_two_questions_are_single_variable(self):
        _, _, _, first = self.run_cli()
        variant = (MATERIALS / "inputs/question-suggestion.txt").read_text()
        _, _, _, second = self.run_cli(question=variant)
        before, after = first[0]["body"], second[0]["body"]
        self.assertEqual({k: v for k, v in before.items() if k != "messages"},
                         {k: v for k, v in after.items() if k != "messages"})
        self.assertNotEqual(before["messages"], after["messages"])
        self.assertIn("return part / whole", json.dumps(after["messages"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    for name in ("main.py", "w01_support.py"):
        if not (project / name).is_file():
            parser.error(f"--project 中缺少 {name}")
    sys.path.insert(0, str(project))
    CliContractTests.support = importlib.import_module("w01_support")
    CliContractTests.project = project
    CliContractTests.output_root = args.output_root.resolve() / f"check-{uuid.uuid4().hex[:8]}"
    CliContractTests.output_root.mkdir(parents=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CliContractTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {
        "evidence_source": "controlled-fixture",
        "execution": "ran",
        "coverage": "hit",
        "verdict": "pass" if result.wasSuccessful() else "fail",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "real_api_calls": 0,
        "project": str(project),
        "evidence_root": str(CliContractTests.output_root),
    }
    (CliContractTests.output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
