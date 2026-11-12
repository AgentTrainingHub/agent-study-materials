"""W03 文件供给件的定向机制检查；使用实际文件与管道，不调用模型。"""
from __future__ import annotations

import contextlib
from dataclasses import replace
import difflib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

MATERIALS = Path(__file__).resolve().parent
REPOSITORY = MATERIALS.parents[3]
for path in (REPOSITORY / "course/week-01/practices/PRA-W01",
             REPOSITORY / "course/week-02/practices/PRA-W02"):
    sys.path.insert(0, str(path))

import w03_files as files


class Log:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir()
        self.run_id = "controlled-file-run"
        self.events = []

    def event(self, kind: str, **fields):
        event = {"event": kind, "evidence_source": "controlled-fixture", **fields}
        self.events.append(event)
        with (self.directory / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(event) + "\n")


@contextlib.contextmanager
def pipe_input(data: bytes | None):
    reader, writer = os.pipe()
    stream = os.fdopen(reader, "r", encoding="utf-8")
    try:
        if data is not None:
            os.write(writer, data)
            os.close(writer)
            writer = None
        with patch.object(sys, "stdin", stream):
            yield
    finally:
        stream.close()
        if writer is not None:
            os.close(writer)


class FileContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="w03-file-check-")
        self.base = Path(self.temporary.name)
        self.work = self.base / "project"
        self.work.mkdir()
        self.log = Log(self.base / "evidence")
        self.target = self.work / "validator.py"
        self.target.write_bytes(b"old\n")
        self.target.chmod(0o750)

    def tearDown(self):
        self.temporary.cleanup()

    def proposal(self, *, path="validator.py", after=b"new\n", name="edit"):
        before = files.snapshot(self.work, path, allow_missing=name == "write")
        args = {"path": path, "old_text": "old", "new_text": "new"} if name == "edit" else {
            "path": path, "content": after.decode()}
        diff = "".join(difflib.unified_diff((before.data or b"").decode().splitlines(True),
                                          after.decode().splitlines(True), fromfile=path, tofile=path))
        return files.prepare_mutation(self.work, "call-1", name, args, before, after, diff, self.log, 1)

    def decide(self, proposal, *, text=None, deadline=None, cancelled=lambda: False):
        incoming = ((text if text is not None else "approve " + proposal.proposal_id) + "\n").encode()
        output = io.StringIO()
        with pipe_input(incoming), contextlib.redirect_stderr(output):
            result = files.approve_and_commit(self.work, proposal,
                deadline=deadline if deadline is not None else time.monotonic() + 2,
                cancelled=cancelled, log=self.log, turn=1)
        return result, output.getvalue()

    def test_approved_edit_preserves_bytes_mode_and_full_preview(self):
        original = b"\xef\xbb\xbfhead\r\nold\r\ntail"
        updated = b"\xef\xbb\xbfhead\r\nnew\r\ntail"
        self.target.write_bytes(original)
        proposal = self.proposal(after=updated)
        result, preview = self.decide(proposal)
        self.assertTrue(result["ok"])
        self.assertEqual(result["control"]["effect"], "applied")
        self.assertTrue(result["commit_attempted"])
        self.assertEqual(self.target.read_bytes(), updated)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o750)
        self.assertIn(proposal.diff_text, preview)
        marker = next(line for line in preview.splitlines() if line.startswith("W03_APPROVAL "))
        self.assertEqual(json.loads(marker[13:])["proposal_id"], proposal.proposal_id)
        self.assertEqual((proposal.directory / "before.bin").read_bytes(), original)
        self.assertEqual((proposal.directory / "after.bin").read_bytes(), updated)

    def test_replaced_object_with_identical_bytes_invalidates_approval(self):
        proposal = self.proposal()
        other = self.work / "outside.tmp"
        other.write_bytes(self.target.read_bytes())
        os.replace(other, self.target)
        result, _ = self.decide(proposal)
        self.assertEqual(result["error"]["class"], "target_changed")
        self.assertEqual(result["control"]["effect"], "none")
        self.assertEqual(self.target.read_bytes(), b"old\n")

    def test_changed_bytes_preserved_and_old_approval_stops(self):
        proposal = self.proposal()
        self.target.write_bytes(b"external update\n")
        with patch.object(files, "commit_proposal", wraps=files.commit_proposal) as commit:
            result, _ = self.decide(proposal)
        self.assertEqual(result["error"]["class"], "target_changed")
        self.assertTrue(result["control"]["blocks_run"])
        self.assertFalse(result["commit_attempted"])
        self.assertEqual(self.target.read_bytes(), b"external update\n")
        self.assertEqual(commit.call_count, 0)

    def test_new_file_and_creation_race_never_overwrite(self):
        proposal = self.proposal(path="test_validator.py", name="write", after=b"assert True\n")
        original_link = os.link

        def race(source, target, **kwargs):
            (self.work / "test_validator.py").write_bytes(b"external new file\n")
            return original_link(source, target, **kwargs)

        with patch.object(files.os, "link", side_effect=race):
            result, _ = self.decide(proposal)
        self.assertEqual(result["error"]["class"], "target_changed")
        self.assertEqual(result["control"]["effect"], "none")
        self.assertTrue(result["commit_attempted"])
        self.assertEqual((self.work / "test_validator.py").read_bytes(), b"external new file\n")
        self.assertEqual(list(self.work.glob(".w03-*")), [])

    def test_create_only_success(self):
        proposal = self.proposal(path="test_validator.py", name="write", after=b"assert True\n")
        result, _ = self.decide(proposal)
        self.assertTrue(result["ok"])
        self.assertEqual((self.work / "test_validator.py").read_bytes(), b"assert True\n")
        self.assertEqual((self.work / "test_validator.py").stat().st_nlink, 1)
        with self.assertRaises(files.ToolError) as error:
            self.proposal(path="test_validator.py", name="write", after=b"changed\n")
        self.assertEqual(error.exception.error_class, "target_exists")

    def test_denial_wrong_id_bad_format_eof_and_oversized_input(self):
        for mode in ("deny", "wrong_id", "bad_format", "eof", "oversized"):
            with self.subTest(mode=mode):
                proposal = self.proposal()
                if mode == "eof":
                    with pipe_input(b""), contextlib.redirect_stderr(io.StringIO()):
                        result = files.approve_and_commit(self.work, proposal,
                            deadline=time.monotonic() + 2, cancelled=lambda: False, log=self.log, turn=1)
                else:
                    text = {"deny": "deny " + proposal.proposal_id, "wrong_id": "approve other",
                            "bad_format": "yes", "oversized": "x" * 300}[mode]
                    result, _ = self.decide(proposal, text=text)
                self.assertFalse(result["ok"])
                self.assertTrue(result["control"]["blocks_run"])
                self.assertEqual(result["control"]["effect"], "none")
                self.assertFalse(result["commit_attempted"])
                self.assertEqual(self.target.read_bytes(), b"old\n")

    def test_idle_stdin_obeys_deadline(self):
        proposal = self.proposal()
        started = time.monotonic()
        with pipe_input(None), contextlib.redirect_stderr(io.StringIO()):
            result = files.approve_and_commit(self.work, proposal, deadline=started + 0.08,
                cancelled=lambda: False, log=self.log, turn=1)
        self.assertEqual(result["error"]["class"], "approval_expired")
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(self.target.read_bytes(), b"old\n")

    def test_idle_stdin_observes_cancel(self):
        proposal = self.proposal()
        cancelled = threading.Event()
        timer = threading.Timer(0.05, cancelled.set)
        timer.start()
        try:
            with pipe_input(None), contextlib.redirect_stderr(io.StringIO()):
                result = files.approve_and_commit(self.work, proposal, deadline=time.monotonic() + 2,
                    cancelled=cancelled.is_set, log=self.log, turn=1)
        finally:
            timer.cancel()
            timer.join()
        self.assertEqual(result["error"]["class"], "cancelled")
        self.assertEqual(self.target.read_bytes(), b"old\n")

    def test_candidate_args_are_immutable_and_changed_candidate_cannot_execute(self):
        proposal = self.proposal()
        with self.assertRaises(TypeError):
            proposal.args["new_text"] = "different"
        changed = replace(proposal, after=b"different\n")
        result, _ = self.decide(changed)
        self.assertEqual(result["error"]["class"], "approval_invalid")
        self.assertEqual(self.target.read_bytes(), b"old\n")

    def test_same_proposal_cannot_replay(self):
        proposal = self.proposal()
        first, _ = self.decide(proposal)
        self.assertTrue(first["ok"])
        with patch.object(files, "commit_proposal", wraps=files.commit_proposal) as commit:
            second, _ = self.decide(proposal)
        self.assertEqual(second["error"]["class"], "approval_invalid")
        self.assertEqual(commit.call_count, 0)

    def test_real_commit_then_exception_is_unknown(self):
        proposal = self.proposal()
        original = files.commit_proposal
        actual_commits = []

        def lose_receipt(*args, **kwargs):
            observed = original(*args, **kwargs)
            actual_commits.append(observed)
            raise RuntimeError("controlled failure after actual file commit")

        with patch.object(files, "commit_proposal", side_effect=lose_receipt):
            result, _ = self.decide(proposal)
        self.assertEqual(self.target.read_bytes(), b"new\n")
        self.assertEqual(len(actual_commits), 1)
        self.assertEqual(result["error"]["class"], "effect_unknown")
        self.assertEqual(result["control"]["effect"], "unknown")
        self.assertTrue(result["control"]["blocks_run"])
        self.assertTrue(result["commit_attempted"])
        self.assertEqual(sum(event["event"] == "commit_started" for event in self.log.events), 1)
        self.assertFalse(any(event["event"] == "commit_finished" for event in self.log.events))

    def test_path_and_link_boundaries(self):
        for path in ("../validator.py", "/tmp/validator.py", "./validator.py", "a/../validator.py",
                     "folder/validator.py", "check.py", "validator.py/", "validator\\py"):
            with self.subTest(path=path), self.assertRaises(files.ToolError):
                files.snapshot(self.work, path)
        self.target.unlink()
        outside = self.base / "outside.txt"
        outside.write_text("private sentinel")
        self.target.symlink_to(outside)
        with self.assertRaises(files.ToolError):
            files.snapshot(self.work, "validator.py")
        self.target.unlink()
        os.link(outside, self.target)
        with self.assertRaises(files.ToolError) as error:
            files.snapshot(self.work, "validator.py")
        self.assertEqual(error.exception.error_class, "file_type_denied")
        self.target.unlink()
        os.mkfifo(self.target)
        with self.assertRaises(files.ToolError):
            files.snapshot(self.work, "validator.py")

    def test_utf8_size_lines_and_paginated_read(self):
        for data, expected in ((b"\xff", "invalid_encoding"), (b"x" * 65537, "file_too_large"),
                               (b"x" * 4097, "line_too_long")):
            with self.subTest(expected=expected):
                self.target.write_bytes(data)
                with self.assertRaises(files.ToolError) as error:
                    files.snapshot(self.work, "validator.py")
                self.assertEqual(error.exception.error_class, expected)
        self.target.write_bytes(b"first\r\nsecond\nthird")
        read = files.safe_read(self.work, "validator.py", 2, 1)
        self.assertEqual(read["lines"], [{"line": 2, "text": "second"}])
        self.assertEqual(read["next_offset"], 3)
        self.target.write_text("# comment\u2028still comment\nport = 80\n", encoding="utf-8")
        read = files.safe_read(self.work, "validator.py", 2, 1)
        self.assertEqual(read["lines"], [{"line": 2, "text": "port = 80"}])
        self.assertEqual(read["total_lines"], 2)
        for offset, limit in ((True, 1), (1, True), (0, 1), (1, 81)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(files.ToolError):
                files.safe_read(self.work, "validator.py", offset, limit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
