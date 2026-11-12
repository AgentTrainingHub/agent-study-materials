"""W05 U1 公开解析检查：构造 SSE 字节，无网络、无工具执行。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import random
import sys
import unittest


def chunk(delta=None, reason=None, **changes):
    value = {"id": "response-1", "model": "fixture-model",
             "choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}]}
    value.update(changes)
    return value


def frame(value, newline=b"\n"):
    return b"data: " + json.dumps(value, ensure_ascii=False).encode("utf-8") + newline * 2


def wire(*values, newline=b"\n", done=True):
    return b"".join(frame(value, newline) for value in values) + (b"data: [DONE]" + newline * 2 if done else b"")


def tool(index=0, call_id="call-a", name="read", arguments="{}"):
    result = {"index": index, "function": {"name": name, "arguments": arguments}}
    if call_id is not None:
        result.update(id=call_id, type="function")
    return result


class StreamChecks(unittest.TestCase):
    def parse(self, data, sizes=None):
        assembler = StreamAssembler()
        previews = []
        offset = 0
        for size in sizes or [len(data)]:
            previews.extend(assembler.feed(data[offset:offset + size]))
            offset += size
        if offset < len(data):
            previews.extend(assembler.feed(data[offset:]))
        return assembler.finish(), previews

    def reject(self, data):
        with self.assertRaises(StreamError) as caught:
            self.parse(data)
        self.assertIsInstance(caught.exception.error_class, str)
        self.assertTrue(caught.exception.error_class)

    def test_current_deepseek_usage_on_final_choice(self):
        usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        result, preview = self.parse(wire(chunk({"role": "assistant", "content": "结果"}),
                                          chunk(reason="stop", usage=usage)))
        self.assertEqual(result["choices"][0]["message"], {"role": "assistant", "content": "结果"})
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result["usage"], usage)
        self.assertEqual(preview, [{"event": "text_delta", "text": "结果"}])

    def test_every_byte_boundary_preserves_utf8_and_json(self):
        data = wire(chunk({"content": "中文🙂a\\b\n第二行"}), chunk(reason="stop"))
        expected, _ = self.parse(data)
        for split in range(1, len(data)):
            with self.subTest(split=split):
                actual, _ = self.parse(data, [split])
                self.assertEqual(actual, expected)
        self.assertEqual(self.parse(data, [1] * len(data))[0], expected)

    def test_crlf_comments_unknown_fields_and_multiple_data_lines(self):
        item = json.dumps(chunk({"content": "甲"}), ensure_ascii=False)
        cut = item.index('"choices"')
        data = (b": heartbeat\r\nevent: message\r\nid: display-only\r\n" +
                b"data: " + item[:cut].encode() + b"\r\ndata: " + item[cut:].encode() + b"\r\n\r\n" +
                wire(chunk(reason="stop"), newline=b"\r\n"))
        result, _ = self.parse(data, [1] * len(data))
        self.assertEqual(result["id"], "response-1")
        self.assertEqual(result["choices"][0]["message"]["content"], "甲")

    def test_frame_waits_for_empty_line(self):
        assembler = StreamAssembler()
        raw = frame(chunk({"content": "preview"}))
        self.assertEqual(assembler.feed(raw[:-1]), [])
        self.assertEqual(assembler.feed(raw[-1:]), [{"event": "text_delta", "text": "preview"}])
        with self.assertRaises(StreamError):
            assembler.finish()

    def test_interleaved_tools_and_incremental_names(self):
        data = wire(chunk({"tool_calls": [tool(1, "call-b", "re", '{"path":'),
                                           tool(0, "call-a", "re", '{"path":"a"}')] }),
                    chunk({"tool_calls": [tool(0, None, "ad", " "), tool(1, None, "ad", '"b"}')] }),
                    chunk(reason="tool_calls"))
        random_sizes = [random.Random(12 + i).randint(1, 17) for i in range(len(data))]
        result, previews = self.parse(data, random_sizes[:40])
        calls = result["choices"][0]["message"]["tool_calls"]
        self.assertEqual([call["id"] for call in calls], ["call-a", "call-b"])
        self.assertEqual([call["function"]["name"] for call in calls], ["read", "read"])
        self.assertEqual([call["function"]["arguments"] for call in calls], ['{"path":"a"} ', '{"path":"b"}'])
        self.assertTrue(all(p["event"] == "tool_arguments_delta" for p in previews))
        self.assertEqual("".join(p["arguments"] for p in previews if p["tool_index"] == 1), '{"path":"b"}')

    def test_json_prefix_is_preview_until_complete_response(self):
        assembler = StreamAssembler()
        preview = assembler.feed(frame(chunk({"tool_calls": [tool(arguments="1")]})))
        self.assertEqual(preview[0]["arguments"], "1")
        self.assertTrue(all(p["event"] in {"text_delta", "tool_arguments_delta"} for p in preview))
        assembler.feed(frame(chunk({"tool_calls": [tool(call_id=None, name="", arguments="0")]})))
        assembler.feed(wire(chunk(reason="tool_calls")))
        self.assertEqual(assembler.finish()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], "10")

    def test_length_keeps_reason_for_external_dispatch_gate(self):
        result, _ = self.parse(wire(chunk({"tool_calls": [tool(arguments='{"path":"a"}')]}),
                                    chunk(reason="length")))
        self.assertEqual(result["choices"][0]["finish_reason"], "length")
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], '{"path":"a"}')

    def test_missing_done(self):
        self.reject(wire(chunk({"content": "已出现"}), chunk(reason="stop"), done=False))

    def test_missing_finish_reason(self):
        self.reject(wire(chunk({"content": "可见片段"})))

    def test_incomplete_final_frame(self):
        valid = wire(chunk({"content": "x"}), chunk(reason="stop"))
        for tail in (b"data: [DONE]", b"data: [DONE]\n", b"data: {", b"data: \xe4"):
            self.reject(wire(chunk(reason="stop"), done=False) + tail)
        self.reject(valid + b"data: ")

    def test_malformed_json_encoding_and_duplicate_keys(self):
        for raw in (b"data: {\n\n", b"data: \xff\n\n", b"data: []\n\n",
                    b'data: {"choices":[],"choices":[]}\n\n',
                    b'data: {"model":"\\ud800"}\n\n', b"data: NaN\n\n",
                    b'data: {"value":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n\n"):
            with self.subTest(raw=raw[:40]):
                self.reject(raw)

    def test_conflicting_response_identity(self):
        for field in ("id", "model"):
            self.reject(wire(chunk({"content": "a"}), chunk(reason="stop", **{field: "other"})))
        self.reject(wire(chunk(reason="stop", id="", model="")))

    def test_tool_identity_conflicts(self):
        self.reject(wire(chunk({"tool_calls": [tool()]}),
                         chunk({"tool_calls": [tool(call_id="changed", name="", arguments="")]}),
                         chunk(reason="tool_calls")))
        self.reject(wire(chunk({"tool_calls": [tool(0), tool(1)]}), chunk(reason="tool_calls")))

    def test_invalid_tool_index_and_incomplete_identity(self):
        for index in (-1, True, 0.0, "0", 8):
            self.reject(wire(chunk({"tool_calls": [tool(index)]}), chunk(reason="tool_calls")))
        self.reject(wire(chunk({"tool_calls": [tool(1)]}), chunk(reason="tool_calls")))
        self.reject(wire(chunk({"tool_calls": [tool(call_id=None)]}), chunk(reason="tool_calls")))

    def test_invalid_choice_and_finish_shape(self):
        for choices in ([], [dict(index=True, delta={})], [dict(index=1, delta={})], [{}, {}], "wrong"):
            self.reject(wire(chunk(reason="stop", choices=choices)))
        for reason in ("unknown", {}, []):
            self.reject(wire(chunk(reason=reason)))
        self.reject(wire(chunk(reason="tool_calls")))
        self.reject(wire(chunk({"tool_calls": [tool()]}), chunk(reason="stop")))

    def test_missing_invalid_and_compatible_usage(self):
        for usage in (None, {"prompt_tokens": -1}, {"total_tokens": True}, "unknown"):
            result, _ = self.parse(wire(chunk({"content": "x"}), chunk(reason="stop", usage=usage)))
            self.assertEqual(result["usage"], usage)
        usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        result, _ = self.parse(wire(chunk(reason="stop"), {"id": "response-1", "model": "fixture-model", "choices": [], "usage": usage}))
        self.assertEqual(result["usage"], usage)

    def test_data_after_done_or_finish_rejected(self):
        self.reject(wire(chunk(reason="stop")) + frame(chunk({"content": "late"})))
        self.reject(wire(chunk(reason="stop"), chunk({"content": "late"})))
        self.reject(wire(chunk(reason="stop"), chunk(reason="length")))

    def test_size_limits(self):
        self.reject(b":" + b"x" * (256 * 1024) + b"\n\n")
        self.reject((b":" + b"x" * 1024 + b"\n\n") * 2050)

    def test_previews_and_final_are_independent_snapshots(self):
        assembler = StreamAssembler()
        first = assembler.feed(frame(chunk({"tool_calls": [tool(arguments="1")]})))
        saved = json.loads(json.dumps(first))
        assembler.feed(frame(chunk({"tool_calls": [tool(call_id=None, name="", arguments="0")]})))
        self.assertEqual(first, saved)
        first[0]["arguments"] = "edited"
        assembler.feed(wire(chunk(reason="tool_calls")))
        result = assembler.finish()
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], "10")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--student-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--case")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    module_path = args.student_dir.resolve() / "stream.py"
    spec = importlib.util.spec_from_file_location("w05_checked_stream", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(args.student_dir.resolve()))
    sys.modules[spec.name] = module
    capture = io.StringIO()
    try:
        with redirect_stdout(capture):
            spec.loader.exec_module(module)
            globals().update(StreamAssembler=module.StreamAssembler, StreamError=module.StreamError)
            names = unittest.defaultTestLoader.getTestCaseNames(StreamChecks)
            if args.case and args.case not in names:
                raise ValueError("未知 --case")
            suite = unittest.TestSuite(StreamChecks(name) for name in ([args.case] if args.case else names))
            result = unittest.TextTestRunner(stream=capture, verbosity=2).run(suite)
        summary = {"evidence_source": "controlled-fixture", "external_model_requests": 0,
                   "count": result.testsRun, "passed": result.wasSuccessful(),
                   "failures": len(result.failures), "errors": len(result.errors),
                   "scope": "U1 parser; dispatcher and CLI gates checked separately"}
    except (OSError, ValueError, AttributeError, ImportError) as error:
        summary = {"evidence_source": "controlled-fixture", "external_model_requests": 0,
                   "count": 0, "passed": False, "error": type(error).__name__, "message": str(error)}
    (args.output_dir / "check-stream.log").write_text(capture.getvalue(), encoding="utf-8")
    (args.output_dir / "check-stream.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
