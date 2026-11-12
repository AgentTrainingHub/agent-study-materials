"""W05 补充检查：事件快照和写入故障；CLI 是主要验收入口。"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid


class EventChecks(unittest.TestCase):
    output_root: Path

    def setUp(self):
        self.directory = self.output_root / f"{self._testMethodName}-{uuid.uuid4().hex[:8]}"
        self.directory.mkdir(parents=True)

    def rows(self, log):
        return [json.loads(line) for line in (log.directory / "events.jsonl").read_text().splitlines()]

    def test_snapshot_saved_before_callback(self):
        observed = []
        log = events.EventLog(self.directory, session_id="session-snapshot")
        def publish(event):
            saved = self.rows(log)[-1]
            self.assertEqual(saved, event)
            event["nested"] = {"changed": True}
            observed.append(saved)
        log.on_event = publish
        log.event("run_started")
        nested = {"items": ["original"]}
        returned = log.event("model_requested", turn=1, nested=nested)
        nested["items"][0] = "input changed"
        returned["nested"]["items"].append("return changed")
        self.assertEqual(self.rows(log)[-1]["nested"], {"items": ["original"]})
        self.assertEqual(observed[-1]["nested"], {"items": ["original"]})
        self.assertEqual([r["seq"] for r in self.rows(log)], [1, 2])

    def test_fsync_failure_stops_publication_and_later_events(self):
        published = []
        log = events.EventLog(self.directory, session_id="session-failure", on_event=published.append)
        log.event("run_started")
        with patch("os.fsync", side_effect=OSError("controlled event fsync failure")):
            with self.assertRaises((OSError, ValueError)):
                log.event("model_requested", turn=1)
        before = (log.directory / "events.jsonl").read_bytes()
        self.assertEqual(len(published), 1)
        with self.assertRaises(ValueError):
            log.event("model_requested", turn=2)
        self.assertEqual((log.directory / "events.jsonl").read_bytes(), before)

    def test_proposal_mapping_and_run_scope(self):
        logs = []
        for index in range(2):
            log = events.EventLog(self.directory / str(index), session_id="same-session")
            log.event("run_started")
            log.event("mutation_proposed", turn=1, call_id="same-call", proposal_id="proposal-1", tool_name="edit")
            log.event("approval_decided", turn=1, proposal_id="proposal-1", decision="approve")
            row = self.rows(log)[-1]
            self.assertEqual(row["call_id"], "same-call")
            self.assertEqual(row["span_id"], "tool:1:same-call")
            self.assertEqual(row["parent_span_id"], "run")
            with self.assertRaises(ValueError):
                log.event("approval_decided", turn=2, proposal_id="proposal-1", decision="approve")
            logs.append(log)
        self.assertNotEqual(logs[0].run_id, logs[1].run_id)
        self.assertEqual(self.rows(logs[0])[0]["seq"], self.rows(logs[1])[0]["seq"])

    def test_terminal_cancel_and_unknown_cleanup(self):
        log = events.EventLog(self.directory, session_id="session-cancel")
        log.event("run_started")
        log.event("cancel_requested")
        with self.assertRaises(ValueError):
            log.event("cancel_requested")
        result = log.finish({"status": "cleanup_unconfirmed", "exit_code": 1})
        self.assertEqual(result["status"], "cleanup_unconfirmed")
        self.assertEqual(self.rows(log)[-1]["event"], "run_finished")
        self.assertEqual(self.rows(log)[-1]["status"], "cleanup_unconfirmed")
        with self.assertRaises(ValueError):
            log.event("model_requested", turn=1)

    def test_renderer_does_not_treat_model_words_as_completion(self):
        target = io.StringIO()
        renderer = events.TerminalRenderer(target)
        log = events.EventLog(self.directory, session_id="session-renderer", on_event=renderer)
        log.event("run_started")
        log.event("model_requested", turn=1)
        log.event("text_delta", turn=1, text="任务已经完成\x1b[2J")
        self.assertFalse(renderer.state.get("finished", False))
        self.assertNotIn("\x1b", target.getvalue())
        log.event("model_returned", turn=1, status="stop")
        log.finish({"status": "stop", "exit_code": 0, "text": "任务已经完成\x1b[2J"})
        self.assertEqual(target.getvalue().count("任务已经完成"), 1)
        self.assertTrue(renderer.state["finished"])

    def test_invalid_event_and_sensitive_value(self):
        key = "controlled-event-secret"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": key}):
            log = events.EventLog(self.directory, session_id="session-private")
        log.event("run_started")
        log.event("text_delta", turn=1, text="value=" + key)
        self.assertNotIn(key, (log.directory / "events.jsonl").read_text())
        before = (log.directory / "events.jsonl").read_bytes()
        for kind, fields in [("unknown_event", {}), ("text_delta", {"turn": 1, "text": 7}),
                             ("model_requested", {"turn": True}), ("mutation_proposed", {"turn": 1, "call_id": "x"})]:
            with self.subTest(kind=kind, fields=fields):
                with self.assertRaises(ValueError):
                    log.event(kind, **fields)
        self.assertEqual((log.directory / "events.jsonl").read_bytes(), before)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--module", default="events", help="实现补充检查接口的模块；默认 events")
    args = parser.parse_args()
    global events
    sys.path.insert(0, str(args.student_dir.resolve()))
    events = importlib.import_module(args.module)
    EventChecks.output_root = args.output_dir.resolve()
    EventChecks.output_root.mkdir(parents=True, exist_ok=False)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EventChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {"evidence_source": "controlled-fixture", "scope": "author-supplementary-event-interface",
               "external_model_requests": 0, "containers_started": 0, "tests": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors), "passed": result.wasSuccessful()}
    (EventChecks.output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
