"""W04 存储补充检查：检查 Journal 与供给 I/O；无模型、无 Docker。"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SYSTEM = {"role": "system", "content": "限定工作区内处理任务。"}


class StorageChecks(unittest.TestCase):
    student_dir: Path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.work = self.base / "work"
        self.work.mkdir()
        self.path = self.base / "state/session.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def completed(self):
        with Journal.create(self.path, self.work, SYSTEM) as journal:
            journal.begin("A")
            journal.append_message({"role": "user", "content": "A 的问题"})
            journal.append_message({"role": "assistant", "content": "A 的回答"})
            end = journal.end("stop")
        return end

    def test_returned_data_and_input_owned(self):
        with Journal.create(self.path, self.work, SYSTEM) as journal:
            journal.begin("A")
            message = {"role": "user", "content": "original"}
            journal.append_message(message)
            message["content"] = "mutated"
            messages, ids = journal.project()
            messages[1]["content"] = "changed return"
            ids.clear()
            journal.header["system"]["content"] = "changed header"
            journal.records.clear()
            journal.inspect()["messages"].clear()
            self.assertEqual(journal.project()[0][1]["content"], "original")
            self.assertEqual(journal.header["system"], SYSTEM)

    def test_links_rejected(self):
        self.completed()
        alias = self.base / "alias.jsonl"
        alias.symlink_to(self.path)
        with self.assertRaises(ProtocolError):
            with Journal.open(alias):
                pass
        hard = self.base / "hard.jsonl"
        os.link(self.path, hard)
        with self.assertRaises(ProtocolError):
            with Journal.open(self.path):
                pass

    def test_limits_before_append_or_parse(self):
        self.completed()
        header = json.loads(self.path.read_bytes().split(b"\n", 1)[0])
        header["system"]["content"] = "\ud800"
        bad_values = [
            json.dumps(header).encode() + b"\n",
            b'{"schema_version":' + b"9" * 5000 + b'}\n',
            b'{"value":' + b"[" * 10000 + b"0" + b"]" * 10000 + b'}\n',
        ]
        malformed = self.base / "parser-limits.jsonl"
        for raw in bad_values:
            with self.subTest(bytes=len(raw)):
                malformed.write_bytes(raw)
                with self.assertRaises(ProtocolError) as caught:
                    with Journal.open(malformed):
                        pass
                self.assertEqual(caught.exception.error_class, "invalid_json")
                self.assertIn("1", str(caught.exception))
                self.assertEqual(malformed.read_bytes(), raw)
        with open_jsonl(self.path) as storage:
            before = self.path.read_bytes()
            with self.assertRaises(ProtocolError):
                storage.append_line(b"x" * MAX_LINE_BYTES + b"\n")
            self.assertEqual(self.path.read_bytes(), before)
        with self.path.open("r+b") as handle:
            handle.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaises(ProtocolError):
            with Journal.open(self.path):
                pass

    def test_fsync_failure_stops_further_appends(self):
        with Journal.create(self.path, self.work, SYSTEM) as journal:
            journal.begin("A")
            with patch("w04_io.os.fsync", side_effect=OSError("controlled fsync failure")):
                with self.assertRaises(ProtocolError):
                    journal.append_message({"role": "user", "content": "question"})
            for operation in (lambda: journal.end("error"), lambda: journal.append_message(
                    {"role": "user", "content": "must not continue"}), journal.project):
                with self.assertRaises(ProtocolError):
                    operation()
        with Journal.open(self.path) as journal:
            self.assertIn("unfinished_run", journal.inspect()["blocked_reason"])

    def test_other_process_busy_then_released(self):
        self.completed()
        code = """import sys
from pathlib import Path
from journal import Journal
from w02_support import ProtocolError
try:
    with Journal.open(Path(sys.argv[1])) as journal:
        print(journal.session_id)
except ProtocolError as error:
    print(error.error_class)
    raise SystemExit(2)
"""
        env = {**os.environ, "PYTHONPATH": str(self.student_dir)}
        with Journal.open(self.path):
            result = subprocess.run([sys.executable, "-c", code, str(self.path)], env=env,
                                    cwd=self.base, capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout.strip()), (2, "session_busy"), result.stderr)
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], env=env,
                                cwd=self.base, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True,
                        help="包含 journal.py 和 W01–W04 支持模块的工程目录")
    args = parser.parse_args()
    directory = args.student_dir.resolve()
    modules = ("journal", "w02_support", "w04_io", "w04_protocol")
    for name in modules:
        if not (directory / (name + ".py")).is_file():
            parser.error(f"工程目录缺少 {name}.py：{directory}")
    sys.path.insert(0, str(directory))
    loaded = {name: importlib.import_module(name) for name in modules}
    for name, module in loaded.items():
        if Path(module.__file__).resolve().parent != directory:
            parser.error(f"{name} 未从指定工程目录导入")
    global Journal, ProtocolError, MAX_FILE_BYTES, MAX_LINE_BYTES, open_jsonl
    Journal = loaded["journal"].Journal
    ProtocolError = loaded["w02_support"].ProtocolError
    io = loaded["w04_io"]
    MAX_FILE_BYTES, MAX_LINE_BYTES, open_jsonl = io.MAX_FILE_BYTES, io.MAX_LINE_BYTES, io.open_jsonl
    StorageChecks.student_dir = directory
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(StorageChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {"evidence_source": "controlled-storage", "student_directory": str(directory),
               "external_model_requests": 0, "docker_runs": 0, "tests_run": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors),
               "verdict": "pass" if result.wasSuccessful() else "fail"}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
