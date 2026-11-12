"""本机回执实验的存储和传输供给；不包含恢复决策或工具调度。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

MAX_JSON_BYTES = 16384
MAX_PAYLOAD_BYTES = 8192


class SupportError(Exception):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON value: " + value)


def decode(raw):
    if len(raw) > MAX_JSON_BYTES:
        raise SupportError("json_too_large", "JSON 超过 16 KiB。")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_constant)
        canonical(value)
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise SupportError("invalid_json", str(exc)) from exc


def read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SupportError("invalid_file", "JSON 必须为无硬链接的普通文件。")
        return decode(handle.read(MAX_JSON_BYTES + 1))


def write_json(path, value):
    with Path(path).open("xb") as handle:
        handle.write(canonical(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def process_identity(pid):
    value = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                           capture_output=True, text=True, check=False)
    if value.returncode not in (0, 1):
        raise SupportError("process_probe_failed", value.stderr.strip())
    return value.stdout.strip() or None


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def connect(path):
    connection = sqlite3.connect(path, isolation_level=None, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


STATE_SCHEMA = """
CREATE TABLE metadata(schema_version INTEGER NOT NULL, session_id TEXT NOT NULL, workspace TEXT NOT NULL);
CREATE TABLE owner(session_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, owner_identity TEXT NOT NULL);
CREATE TABLE operations(
 op_key TEXT PRIMARY KEY, origin_run_id TEXT NOT NULL, assistant_id TEXT NOT NULL,
 call_id TEXT NOT NULL, receiver_id TEXT NOT NULL, resource TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('intent','result_saved','journaled','effect_unknown')),
 result_json TEXT, result_entry_id TEXT,
 UNIQUE(origin_run_id,assistant_id,call_id));
CREATE TABLE budget_groups(
 budget_group_id TEXT PRIMARY KEY, max_turns INTEGER NOT NULL, max_actions INTEGER NOT NULL,
 deadline_unix REAL NOT NULL, model_calls INTEGER NOT NULL DEFAULT 0,
 action_count INTEGER NOT NULL DEFAULT 0, service_requests INTEGER NOT NULL DEFAULT 0,
 summary_calls INTEGER NOT NULL DEFAULT 0);
CREATE TABLE request_reservations(
 budget_group_id TEXT NOT NULL REFERENCES budget_groups(budget_group_id),
 request_index INTEGER NOT NULL, run_id TEXT NOT NULL, purpose TEXT NOT NULL,
 max_tokens INTEGER NOT NULL, sent TEXT NOT NULL, usage_json TEXT,
 PRIMARY KEY(budget_group_id,request_index));
"""


@contextmanager
def open_state(path, *, session_id, workspace, create=False):
    path, workspace = Path(path).absolute(), Path(workspace).resolve(strict=True)
    if not session_id or not workspace.is_dir() or path.resolve().is_relative_to(workspace):
        raise SupportError("invalid_binding", "状态库需绑定会话并位于练习工作区之外。")
    if create:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SupportError("invalid_state_file", "状态库必须为无链接的普通文件。")
    connection = connect(path)
    try:
        if create:
            connection.executescript(STATE_SCHEMA)
            with transaction(connection):
                connection.execute("INSERT INTO metadata VALUES(1,?,?)", (session_id, str(workspace)))
        rows = connection.execute("SELECT * FROM metadata").fetchall()
        if len(rows) != 1 or tuple(rows[0]) != (1, session_id, str(workspace)):
            raise SupportError("state_binding_mismatch", "状态库版本、会话或工作区不匹配。")
        yield connection
    finally:
        connection.close()


def load_receiver(path):
    value = read_json(path)
    expected = {"schema_version", "receiver_id", "url", "pid", "process_identity", "database"}
    if not isinstance(value, dict) or set(value) != expected or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SupportError("invalid_receiver", "回执服务配置字段或版本错误。")
    if not isinstance(value["url"], str):
        raise SupportError("invalid_receiver", "服务 URL 必须为字符串。")
    parts = urlsplit(value["url"])
    try:
        valid_port = parts.port is not None and 1 <= parts.port <= 65535
    except ValueError as exc:
        raise SupportError("invalid_receiver", "端口无效。") from exc
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or not valid_port
            or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username is not None):
        raise SupportError("invalid_receiver", "服务必须是固定的 127.0.0.1 HTTP 入口。")
    if (not isinstance(value["receiver_id"], str) or not value["receiver_id"]
            or type(value["pid"]) is not int or value["pid"] < 1
            or not isinstance(value["process_identity"], str) or not value["process_identity"]
            or not isinstance(value["database"], str) or not Path(value["database"]).is_absolute()):
        raise SupportError("invalid_receiver", "服务身份或数据库路径无效。")
    return value


def _http_worker():
    request = decode(sys.stdin.buffer.read(MAX_JSON_BYTES + 1))
    url = urlsplit(request["url"])
    connection = http.client.HTTPConnection("127.0.0.1", url.port, timeout=request["timeout"])
    def emit(value):
        print(canonical(value).decode(), flush=True)
    emit({"stage": "started"})
    try:
        connection.request("POST", request["route"], body=canonical(request["payload"]),
                           headers={"Content-Type": "application/json"})
        emit({"stage": "sent"})
        response = connection.getresponse()
        body = response.read(MAX_JSON_BYTES + 1)
        emit({"stage": "response", "http_status": response.status, "body": decode(body)})
    except (OSError, http.client.HTTPException, SupportError, ValueError) as exc:
        emit({"stage": "error", "error": type(exc).__name__, "message": str(exc)})
    finally:
        connection.close()


class ReceiverClient:
    """每次调用只派发一次；名额由调用方的持久预算预留函数提供。"""
    def __init__(self, manifest_path, *, evidence_dir, reserve_request):
        self.manifest = load_receiver(manifest_path)
        self.directory = Path(evidence_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.reserve_request = reserve_request

    def _call(self, route, payload, *, deadline, cancelled=lambda: False):
        if route not in {"/fence", "/lookup", "/apply"} or not math.isfinite(deadline):
            raise SupportError("invalid_request", "请求方法或截止时间无效。")
        if cancelled() or time.monotonic() >= deadline:
            raise SupportError("not_dispatched", "请求派发前已取消或到期。", {"sent": False})
        raw = canonical({"url": self.manifest["url"], "route": route,
                         "payload": {**payload, "receiver_id": self.manifest["receiver_id"]},
                         "timeout": min(30.0, deadline - time.monotonic())})
        if len(raw) > MAX_JSON_BYTES:
            raise SupportError("request_too_large", "请求超过传输上限。", {"sent": False})
        index = self.reserve_request()
        if type(index) is not int or index < 1:
            raise SupportError("invalid_reservation", "请求预留必须返回正整数序号。")
        directory = self.directory / f"service-{index:03d}"
        directory.mkdir(exist_ok=False)
        write_json(directory / "request.json", decode(raw))
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "http-worker"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={key: value for key, value in os.environ.items()
                                        if key in {"PATH", "LANG", "LC_ALL"}})
        stop_reason, first, actions = None, True, []
        interrupted = None
        pid_identity = None
        try:
            pid_identity = process_identity(process.pid)
            write_json(directory / "worker.json", {"pid": process.pid, "process_identity": pid_identity})
            while True:
                try:
                    stdout, stderr = process.communicate(input=raw if first else None, timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    if cancelled() or time.monotonic() >= deadline:
                        stop_reason = "cancelled" if cancelled() else "deadline_exceeded"
                        break
        except BaseException as exc:
            interrupted = exc
            stop_reason = "interrupted"
        finally:
            if process.poll() is None:
                process.terminate()
                actions.append("terminate")
                try:
                    stdout, stderr = process.communicate(timeout=0.3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    actions.append("kill")
                    try:
                        stdout, stderr = process.communicate(timeout=1)
                    except subprocess.TimeoutExpired as exc:
                        stdout, stderr = exc.output or b"", exc.stderr or b""
                        stop_reason = "cleanup_unconfirmed"
            elif stop_reason:
                stdout, stderr = process.communicate()
        cleanup = {"pid": process.pid, "process_identity": pid_identity,
                   "returncode": process.returncode, "actions": actions,
                   "cleanup_confirmed": process.returncode is not None}
        messages = [decode(line) for line in stdout.splitlines()]
        sent = True if any(m.get("stage") == "sent" for m in messages) else ("unknown" if messages else False)
        responses = [m for m in messages if m.get("stage") == "response"]
        evidence = {"request_index": index, "route": route, "sent": sent,
                    "messages": messages, "cleanup": cleanup, "stop_reason": stop_reason}
        write_json(directory / "exchange.json", evidence)
        (directory / "stderr.txt").write_bytes(stderr)
        if interrupted is not None:
            raise interrupted
        if stop_reason:
            raise SupportError(stop_reason, "本机请求已停止，效果需独立查证。", evidence)
        if len(responses) != 1 or process.returncode != 0:
            raise SupportError("transport_error", "未取得完整本机服务响应。", evidence)
        response = responses[0]
        if response["http_status"] != 200:
            body = response["body"]
            code = body.get("error", "http_error") if isinstance(body, dict) else "http_error"
            raise SupportError(code, "本机服务拒绝请求。", evidence)
        body = response["body"]
        if not isinstance(body, dict) or body.get("receiver_id") != self.manifest["receiver_id"]:
            raise SupportError("receiver_mismatch", "响应服务身份不匹配。", evidence)
        return body

    def fence(self, resource, epoch, **options):
        return self._call("/fence", {"resource": resource, "epoch": epoch}, **options)

    def lookup(self, operation, **options):
        return self._call("/lookup", {"resource": operation["resource"], "op_key": operation["op_key"]}, **options)

    def apply(self, operation, epoch, **options):
        return self._call("/apply", {key: operation[key] for key in
                                   ("resource", "op_key", "payload", "payload_hash")} | {"epoch": epoch}, **options)


if __name__ == "__main__":
    if sys.argv[1:] != ["http-worker"]:
        raise SystemExit("仅支持内部 http-worker 入口")
    _http_worker()
