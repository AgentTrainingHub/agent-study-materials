"""W05 查看者 CLI 检查：受控事件，真实进程和游标，禁止网络与执行派发。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
import uuid

WRAPPER = r'''
import json, runpy, socket, subprocess, sys
from pathlib import Path
entry, attempts = map(Path, sys.argv[1:3])
def forbidden(*args, **kwargs):
    with attempts.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"attempt": "network-or-child-process"}) + "\n")
    raise AssertionError("watch 只能读取事件，不得发起网络或子进程")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
subprocess.Popen = forbidden
sys.path.insert(0, str(entry.parent))
sys.argv = [str(entry), *sys.argv[3:]]
runpy.run_path(str(entry), run_name="__main__")
'''


def event(seq, name, **fields):
    turn = fields.pop("turn", 1 if name.startswith("model_") or name == "text_delta" else 0)
    call_id = fields.get("call_id")
    if name in {"model_requested", "text_delta", "tool_arguments_delta", "model_returned"}:
        span, parent = f"model:{turn}", "run"
    elif call_id is not None:
        span, parent = f"tool:{turn}:{call_id}", "run"
    else:
        span, parent = "run", None
    return {"schema_version": 1, "run_id": "controlled-watch-run", "session_id": "controlled-session",
            "seq": seq, "time": "2026-10-22T00:00:00.000Z", "elapsed_ms": seq * 10,
            "event": name, "turn": turn, "span_id": span, "parent_span_id": parent,
            "evidence_source": "controlled-fixture", **fields}


def fixture():
    return [event(1, "run_started"), event(2, "model_requested"),
            event(3, "text_delta", text="中文增量"), event(4, "model_returned", status="stop"),
            event(5, "run_finished", status="stop", exit_code=0)]


def encode(rows):
    return b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8") for row in rows)


class WatchChecks(unittest.TestCase):
    entry_point: Path
    output_root: Path

    def setUp(self):
        self.directory = self.output_root / f"{self._testMethodName}-{uuid.uuid4().hex[:8]}"
        self.directory.mkdir(parents=True)
        self.run_dir = self.directory / "run"
        self.run_dir.mkdir()
        self.events = self.run_dir / "events.jsonl"
        self.cursor = self.directory / "viewer/cursor.json"
        self.wrapper = self.directory / "watch-wrapper.py"
        self.wrapper.write_text(WRAPPER)
        self.attempts = self.directory / "execution-attempts.jsonl"
        self.number = 0
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=2)
        self.assertFalse(self.attempts.exists(), "查看者尝试网络或子进程派发")

    def command(self, follow=False, cursor=None):
        command = [sys.executable, str(self.wrapper), str(self.entry_point), str(self.attempts), "watch",
                   "--events", str(self.events), "--cursor", str(cursor or self.cursor)]
        if follow:
            command.append("--follow")
        return command

    def environment(self):
        return {key: value for key, value in os.environ.items() if not key.startswith("DEEPSEEK_")}

    def invoke(self, **options):
        self.number += 1
        result = subprocess.run(self.command(**options), env=self.environment(), capture_output=True,
                                text=True, timeout=8)
        (self.directory / f"watch-{self.number}.stdout").write_text(result.stdout)
        (self.directory / f"watch-{self.number}.stderr").write_text(result.stderr)
        (self.directory / f"watch-{self.number}.exit-code").write_text(str(result.returncode) + "\n")
        return result

    def output_rows(self, result):
        return [json.loads(line) for line in result.stdout.splitlines()]

    def cursor_value(self):
        return json.loads(self.cursor.read_text())

    def append(self, data):
        with self.events.open("ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def start(self):
        child = subprocess.Popen(self.command(follow=True), env=self.environment(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def wait_cursor(self, child, seq):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.cursor.exists() and self.cursor_value()["last_seq"] == seq:
                return
            if child.poll() is not None:
                stdout, stderr = child.communicate()
                self.fail(f"watch 提前退出 {child.returncode}: {stdout!r} {stderr!r}")
            time.sleep(0.02)
        self.fail(f"查看者没有在时限内保存连续游标 {seq}")

    def test_resume_without_execution(self):
        rows = fixture()
        self.events.write_bytes(encode(rows[:3]))
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.output_rows(first), rows[:3])
        self.assertEqual(self.cursor_value()["last_seq"], 3)
        self.append(encode(rows[3:]))
        source = self.events.read_bytes()
        second = self.invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.output_rows(second), rows[3:])
        third = self.invoke()
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(third.stdout, "")
        self.assertEqual(self.cursor_value()["last_seq"], 5)
        self.assertEqual(self.events.read_bytes(), source)

    def test_gap_preserves_cursor(self):
        rows = fixture()
        self.events.write_bytes(encode(rows[:2]))
        self.assertEqual(self.invoke().returncode, 0)
        before_cursor = self.cursor.read_bytes()
        self.append(encode([rows[3]]))
        source = self.events.read_bytes()
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(self.cursor.read_bytes(), before_cursor)
        self.assertEqual(self.events.read_bytes(), source)

    def test_duplicate_preserves_cursor(self):
        rows = fixture()
        self.events.write_bytes(encode(rows[:2]))
        self.assertEqual(self.invoke().returncode, 0)
        before_cursor = self.cursor.read_bytes()
        self.append(encode([rows[1]]))
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.cursor.read_bytes(), before_cursor)

    def test_wrong_run_and_future_cursor(self):
        self.events.write_bytes(encode(fixture()))
        self.cursor.parent.mkdir()
        for run_id, seq in [("another-run", 2), ("controlled-watch-run", 20)]:
            with self.subTest(run_id=run_id, seq=seq):
                value = {"schema_version": 1, "run_id": run_id, "last_seq": seq}
                self.cursor.write_text(json.dumps(value) + "\n")
                before = self.cursor.read_bytes()
                result = self.invoke()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.cursor.read_bytes(), before)

    def test_unknown_schema_and_event(self):
        for field, value in [("schema_version", 2), ("event", "unknown_event"), ("seq", True)]:
            with self.subTest(field=field, value=value):
                rows = fixture()
                rows[2][field] = value
                self.events.write_bytes(encode(rows))
                result = self.invoke()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue(result.stderr.strip())

    def test_partial_tail_waits_for_complete_line(self):
        rows = fixture()
        tail = encode([rows[2]])
        split = len(tail) // 2
        self.events.write_bytes(encode(rows[:2]) + tail[:split])
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output_rows(result), rows[:2])
        self.assertEqual(self.cursor_value()["last_seq"], 2)
        self.append(tail[split:] + encode(rows[3:]))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output_rows(result), rows[2:])

    def test_follow_reads_new_complete_events(self):
        rows = fixture()
        self.events.write_bytes(encode(rows[:2]))
        child = self.start()
        self.wait_cursor(child, 2)
        tail = encode([rows[2]])
        self.append(tail[:-1])
        time.sleep(0.1)
        self.assertIsNone(child.poll())
        self.assertEqual(self.cursor_value()["last_seq"], 2)
        self.append(tail[-1:] + encode(rows[3:]))
        stdout, stderr = child.communicate(timeout=5)
        (self.directory / "follow.stdout").write_text(stdout)
        (self.directory / "follow.stderr").write_text(stderr)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual([json.loads(line) for line in stdout.splitlines()], rows)
        self.assertEqual(self.cursor_value()["last_seq"], 5)

    def test_sigint_stops_only_observation(self):
        rows = fixture()
        self.events.write_bytes(encode(rows[:2]))
        source = self.events.read_bytes()
        child = self.start()
        self.wait_cursor(child, 2)
        child.send_signal(signal.SIGINT)
        stdout, stderr = child.communicate(timeout=5)
        (self.directory / "interrupted.stdout").write_text(stdout)
        (self.directory / "interrupted.stderr").write_text(stderr)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(self.events.read_bytes(), source)
        self.assertEqual(self.cursor_value()["last_seq"], 2)
        self.append(encode(rows[2:]))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output_rows(result), rows[2:])

    def test_finished_partial_tail_is_error(self):
        self.events.write_bytes(encode(fixture()) + b'{"unfinished":')
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(result.stderr.strip())

    def test_cursor_cannot_replace_run_evidence(self):
        self.events.write_bytes(encode(fixture()))
        before = self.events.read_bytes()
        for path in (self.events, self.run_dir / "cursor.json"):
            result = self.invoke(cursor=path)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(self.events.read_bytes(), before)

    def test_tool_records_remain_observations(self):
        rows = [event(1, "run_started"),
                event(2, "tool_started", turn=1, call_id="old-call", tool_name="shell", arguments={"command": "check"}),
                event(3, "tool_finished", turn=1, call_id="old-call", tool_name="shell", executed=True,
                      result={"ok": True, "artifact_dir": "controlled-past-tool-output"}),
                event(4, "run_finished", status="stop", exit_code=0)]
        self.events.write_bytes(encode(rows))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output_rows(result), rows)
        self.assertFalse(self.attempts.exists())


CASES = {name.removeprefix("test_"): name for name in dir(WatchChecks) if name.startswith("test_")}


def run_suite(entry, output_root, names=None):
    WatchChecks.entry_point = Path(entry).resolve()
    WatchChecks.output_root = Path(output_root).resolve()
    WatchChecks.output_root.mkdir(parents=True, exist_ok=False)
    selected = [CASES[name] for name in names] if names else list(CASES.values())
    suite = unittest.TestSuite(WatchChecks(name) for name in selected)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {"evidence_source": "controlled-fixture", "scope": "student-watch-cli",
               "external_model_requests": 0, "containers_started": 0, "tests": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors), "passed": result.wasSuccessful()}
    (WatchChecks.output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    args = parser.parse_args()
    result = run_suite(args.entry, args.output_dir, None if args.case == "all" else [args.case])
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
