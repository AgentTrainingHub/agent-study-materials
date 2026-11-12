"""本机练习回执服务；效果是自身 SQLite 中的一条记录。"""
from __future__ import annotations

import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time
from uuid import uuid4

from w08_io import (MAX_JSON_BYTES, MAX_PAYLOAD_BYTES, SupportError, canonical, connect,
                    decode, digest, process_identity, transaction, write_json)

KEY = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
SCHEMA = """
CREATE TABLE metadata(schema_version INTEGER NOT NULL,receiver_id TEXT NOT NULL);
CREATE TABLE fences(resource TEXT PRIMARY KEY,epoch INTEGER NOT NULL);
CREATE TABLE effects(op_key TEXT PRIMARY KEY,resource TEXT NOT NULL,payload_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL,receipt TEXT NOT NULL UNIQUE,epoch INTEGER NOT NULL);
CREATE TABLE requests(sequence INTEGER PRIMARY KEY AUTOINCREMENT,route TEXT NOT NULL,
 resource TEXT,epoch INTEGER,op_key TEXT,outcome TEXT NOT NULL,http_status INTEGER NOT NULL);
"""


def key(value):
    return isinstance(value, str) and KEY.fullmatch(value) is not None


def validate_payload(value):
    if (not isinstance(value, dict) or set(value) != {"text", "workspace_id", "check_evidence_id", "checked_files"}
            or not isinstance(value["text"], str) or not value["text"].strip() or len(value["text"]) > 512
            or not key(value["workspace_id"]) or not key(value["check_evidence_id"])
            or len(canonical(value)) > MAX_PAYLOAD_BYTES):
        raise SupportError("invalid_payload", "结果内容或检查来源字段无效。")
    files = value["checked_files"]
    if (not isinstance(files, dict) or not files or not set(files) <= {"validator.py", "test_validator.py"}
            or any(not isinstance(version, str) or not SHA.fullmatch(version) for version in files.values())):
        raise SupportError("invalid_payload", "checked_files 只接受固定练习文件及 SHA256。")


def initialize(path):
    connection = connect(path)
    try:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not tables:
            connection.executescript(SCHEMA)
            with transaction(connection):
                connection.execute("INSERT INTO metadata VALUES(1,?)", (uuid4().hex,))
        metadata = connection.execute("SELECT * FROM metadata").fetchall()
        if len(metadata) != 1 or metadata[0]["schema_version"] != 1:
            raise SupportError("receiver_schema_mismatch", "服务库版本不受支持。")
        return metadata[0]["receiver_id"]
    finally:
        connection.close()


def response_for(connection, route, value, receiver_id):
    expected = {"receiver_id", "resource"}
    expected |= {"epoch"} if route == "/fence" else {"op_key"}
    if route == "/apply":
        expected |= {"epoch", "payload", "payload_hash"}
    if route not in {"/fence", "/lookup", "/apply"}:
        return 404, {"error": "unknown_route", "receiver_id": receiver_id}, False
    if not isinstance(value, dict) or set(value) != expected or not key(value.get("resource")):
        return 400, {"error": "invalid_request", "receiver_id": receiver_id}, False
    if value["receiver_id"] != receiver_id:
        return 409, {"error": "receiver_mismatch", "receiver_id": receiver_id}, False
    resource = value["resource"]
    if route in {"/fence", "/apply"} and (type(value["epoch"]) is not int or not 1 <= value["epoch"] <= 2**63-1):
        return 400, {"error": "invalid_epoch", "receiver_id": receiver_id}, False
    base = {"receiver_id": receiver_id, "resource": resource}
    if route == "/fence":
        row = connection.execute("SELECT epoch FROM fences WHERE resource=?", (resource,)).fetchone()
        if row and value["epoch"] < row["epoch"]:
            return 409, {**base, "error": "stale_epoch", "epoch": row["epoch"]}, False
        connection.execute("INSERT INTO fences VALUES(?,?) ON CONFLICT(resource) DO UPDATE SET epoch=excluded.epoch",
                           (resource, value["epoch"]))
        return 200, {**base, "epoch": value["epoch"]}, False
    if not key(value["op_key"]):
        return 400, {**base, "error": "invalid_key"}, False
    base["op_key"] = value["op_key"]
    row = connection.execute("SELECT * FROM effects WHERE op_key=?", (value["op_key"],)).fetchone()
    if row and row["resource"] != resource:
        return 409, {**base, "error": "resource_conflict"}, False
    if route == "/lookup":
        return 200, ({**base, "state": "applied", "payload_hash": row["payload_hash"], "receipt": row["receipt"]}
                     if row else {**base, "state": "absent"}), False
    try:
        validate_payload(value["payload"])
    except SupportError as exc:
        return 400, {**base, "error": exc.code}, False
    if not isinstance(value["payload_hash"], str) or value["payload_hash"] != digest(value["payload"]):
        return 400, {**base, "error": "payload_hash_mismatch"}, False
    fence = connection.execute("SELECT epoch FROM fences WHERE resource=?", (resource,)).fetchone()
    if fence is None or value["epoch"] != fence["epoch"]:
        return 409, {**base, "error": "stale_epoch"}, False
    if row:
        if row["payload_hash"] != value["payload_hash"] or row["payload_json"] != canonical(value["payload"]).decode():
            return 409, {**base, "error": "payload_conflict"}, False
        return 200, {**base, "state": "applied", "payload_hash": row["payload_hash"], "receipt": row["receipt"]}, False
    receipt = uuid4().hex
    connection.execute("INSERT INTO effects VALUES(?,?,?,?,?,?)",
                       (value["op_key"], resource, canonical(value["payload"]).decode(),
                        value["payload_hash"], receipt, value["epoch"]))
    return 200, {**base, "state": "applied", "payload_hash": value["payload_hash"], "receipt": receipt}, True


def serve(directory, hold_after_effect):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / "receiver.lock").open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    database = directory / "receiver.sqlite3"
    if database.is_symlink() or (database.exists() and database.stat().st_nlink != 1):
        raise SupportError("invalid_database", "服务库不能使用链接。")
    receiver_id = initialize(database)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(fmt % args, file=sys.stderr, flush=True)

        def do_POST(self):
            self.connection.settimeout(3)
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 < length <= MAX_JSON_BYTES or self.headers.get("Transfer-Encoding") is not None:
                    raise SupportError("invalid_length", "请求长度不受支持。")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise SupportError("incomplete_body", "请求正文不完整。")
                value = decode(raw)
            except (ValueError, OSError, SupportError) as exc:
                status, result, created = 400, {"error": getattr(exc, "code", "invalid_request"),
                                               "receiver_id": receiver_id}, False
                value = {}
            else:
                connection = connect(database)
                try:
                    with transaction(connection):
                        status, result, created = response_for(connection, self.path, value, receiver_id)
                        fields = value if isinstance(value, dict) else {}
                        connection.execute("INSERT INTO requests(route,resource,epoch,op_key,outcome,http_status) VALUES(?,?,?,?,?,?)",
                                           (self.path, fields.get("resource") if isinstance(fields.get("resource"), str) else None,
                                            fields.get("epoch") if type(fields.get("epoch")) is int and -2**63 <= fields["epoch"] < 2**63 else None,
                                            fields.get("op_key") if isinstance(fields.get("op_key"), str) else None,
                                            result.get("error", result.get("state", "fenced")), status))
                finally:
                    connection.close()
            if created and hold_after_effect:
                marker = directory / "after-effect-commit.json"
                try:
                    write_json(marker, {"receiver_id": receiver_id, "pid": os.getpid(),
                                        "op_key": result["op_key"], "receipt": result["receipt"],
                                        "boundary": "after_effect_commit", "committed": True})
                except FileExistsError:
                    # 同一个启动配置仅在首条新效果停住，后续请求仍可查询。
                    pass
                else:
                    end = time.monotonic() + 30
                    while not (directory / "release-after-effect").exists():
                        if time.monotonic() >= end:
                            write_json(directory / "fault-timeout.json", {"boundary": "after_effect_commit"})
                            break
                        time.sleep(0.02)
            raw = canonical(result)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError) as exc:
                print(f"response_not_delivered: {type(exc).__name__}", file=sys.stderr, flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    manifest = {"schema_version": 1, "receiver_id": receiver_id,
                "url": f"http://127.0.0.1:{server.server_port}", "pid": os.getpid(),
                "process_identity": process_identity(os.getpid()), "database": str(database)}
    pending = directory / ("receiver-" + uuid4().hex + ".tmp")
    write_json(pending, manifest)
    os.replace(pending, directory / "receiver.json")
    def stop(_signal, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--hold-after-effect", action="store_true")
    args = parser.parse_args()
    serve(args.directory, args.hold_after_effect)
