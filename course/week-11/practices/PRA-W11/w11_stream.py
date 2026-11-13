"""W11 HTTP worker gate；有效性判据由固定本地 memory 模块提供。"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from w01_support import Config, ENDPOINT, NoRedirectHandler, classify_http_status, sanitize, utc_now
from w02_support import CLEANUP_GRACE_SECONDS, _worker_environment
from w11_io import gate, utc_now, RetrievalError

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_WORKER_LINE_BYTES = 64 * 1024


@dataclass(frozen=True)
class StreamExchange:
    request_payload: dict
    raw_chunks: tuple[bytes, ...]
    arrivals: tuple[dict, ...]
    http_status: int | None
    request_id: str | None
    error: dict | None
    started_at: str
    finished_at: str
    elapsed_ms: int
    memory_gate: dict


def redact_stream_bytes(raw: bytes, key: str) -> bytes:
    """对整条字节流脱敏，保留非 UTF-8 字节供失败诊断。"""
    if key:
        raw = raw.replace(key.encode("utf-8"), b"[REDACTED]")
    raw = re.sub(rb"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", b"[REDACTED]", raw)
    return re.sub(rb"\bsk-[A-Za-z0-9_-]{12,}\b", b"[REDACTED]", raw)


def _stream_worker_command():
    return [sys.executable, str(Path(__file__).resolve()), "--stream-worker"]


def _stop_worker(process):
    if process.poll() is not None:
        return True
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait(timeout=CLEANUP_GRACE_SECONDS)
        return True
    try:
        process.wait(timeout=CLEANUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            process.wait(timeout=CLEANUP_GRACE_SECONDS)
            return True
        try:
            process.wait(timeout=CLEANUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def call_model_stream(config, request, deadline, *, on_chunk, cancelled, memory_binding,
                      validator="memory.validate_worker_binding", on_gate=None) -> StreamExchange:
    if validator != "memory.validate_worker_binding":
        raise ValueError("validator 必须使用固定 memory.validate_worker_binding")
    started = time.monotonic()
    started_at = utc_now()
    request_snapshot = json.loads(json.dumps(request, ensure_ascii=False, allow_nan=False))
    envelope = json.dumps({"config": asdict(config), "request": request_snapshot,
                           "memory_binding": memory_binding, "deadline": deadline},
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    chunks, arrivals = [], []
    status = request_id = error = None
    process = None
    terminal = False
    headers_seen = False
    total = 0
    previous_arrival = -1
    callback_error = None
    memory_gate = {"memory_binding": memory_binding, "sent_status": "not_started",
                   "gate_checked_at": None, "transport_started_at": None, "headers_at": None}
    until = min(deadline, started + config.timeout_seconds)

    def snapshot():
        return StreamExchange(sanitize(request_snapshot, (config.api_key,)), tuple(chunks),
                              tuple(dict(item) for item in arrivals), status, request_id,
                              sanitize(error, (config.api_key,)), started_at, utc_now(),
                              round((time.monotonic() - started) * 1000), dict(memory_gate))

    def fail(kind, message, *, stage="transport"):
        return {"stage": stage, "class": kind, "message": message}

    def stop_requested():
        nonlocal callback_error
        try:
            return cancelled()
        except BaseException as exc:
            callback_error = exc
            raise

    try:
        if stop_requested():
            error = fail("cancelled", "模型请求启动前已取消。")
            return snapshot()
        if started >= until:
            error = fail("deadline_exceeded", "模型请求启动前时间预算已用尽。")
            return snapshot()
        process = subprocess.Popen(_stream_worker_command(), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=_worker_environment())
        pending = bytearray()
        sent = 0
        with selectors.DefaultSelector() as selector:
            for handle, mode, name in ((process.stdin, selectors.EVENT_WRITE, "input"),
                                       (process.stdout, selectors.EVENT_READ, "output"),
                                       (process.stderr, selectors.EVENT_READ, "error")):
                os.set_blocking(handle.fileno(), False)
                selector.register(handle, mode, name)
            while selector.get_map():
                if stop_requested():
                    error = fail("cancelled", "已请求取消模型传输。")
                    break
                if time.monotonic() >= until:
                    kind = "deadline_exceeded" if deadline <= started + config.timeout_seconds else "provider_timeout"
                    error = fail(kind, "模型传输到达截止时间。")
                    break
                for selected, _mask in selector.select(min(0.05, max(0, until - time.monotonic()))):
                    handle = selected.fileobj
                    if selected.data == "input":
                        try:
                            sent += os.write(handle.fileno(), envelope[sent:sent + 65536])
                        except BrokenPipeError:
                            selector.unregister(handle)
                            handle.close()
                            continue
                        if sent == len(envelope):
                            selector.unregister(handle)
                            handle.close()
                        continue
                    data = os.read(handle.fileno(), 65536)
                    if not data:
                        selector.unregister(handle)
                        continue
                    if selected.data == "error":
                        # stderr is drained but never copied into user evidence; it may contain credentials.
                        continue
                    pending.extend(data)
                    while b"\n" in pending:
                        boundary = pending.index(b"\n")
                        line = bytes(pending[:boundary])
                        del pending[:boundary + 1]
                        if len(line) > MAX_WORKER_LINE_BYTES:
                            raise ValueError("worker record too large")
                        item = json.loads(line)
                        if not isinstance(item, dict) or terminal:
                            raise ValueError("worker record out of order")
                        kind = item.get("type")
                        if kind == "memory_gate":
                            if not isinstance(item.get("record"), dict):
                                raise ValueError("invalid memory gate record")
                            memory_gate.update(item["record"])
                            if on_gate is not None:
                                try:
                                    on_gate(dict(memory_gate))
                                except BaseException as exc:
                                    callback_error = exc
                                    error = fail(getattr(exc,"error_class","callback_failed"),"gate observer failed")
                                    break
                        elif kind == "headers" and not headers_seen:
                            if type(item.get("status")) is not int or not 100 <= item["status"] <= 599:
                                raise ValueError("invalid HTTP status")
                            if item.get("request_id") is not None and not isinstance(item["request_id"], str):
                                raise ValueError("invalid request ID")
                            status, request_id = item["status"], item.get("request_id")
                            headers_seen = True
                        elif kind == "chunk" and headers_seen:
                            chunk = base64.b64decode(item["data"], validate=True)
                            arrival = item["arrival_ms"]
                            if not chunk or type(arrival) is not int or arrival < previous_arrival:
                                raise ValueError("invalid arrival")
                            previous_arrival = arrival
                            total += len(chunk)
                            if total > MAX_RESPONSE_BYTES:
                                error = fail("response_too_large", "响应超过 2 MiB 上限。")
                                break
                            chunks.append(chunk)
                            arrivals.append({"seq": len(chunks), "elapsed_ms": arrival, "byte_count": len(chunk)})
                            if status == 200:
                                try:
                                    on_chunk(chunk)
                                except BaseException as exc:
                                    callback_error = exc
                                    error = fail(getattr(exc, "error_class", "callback_failed"),
                                                 "流处理回调失败，正在回收传输进程。", stage="response")
                                    break
                        elif kind == "end" and headers_seen:
                            terminal = True
                            if status != 200:
                                error = fail(classify_http_status(status), f"Provider HTTP 状态为 {status}。", stage="http")
                        elif kind == "error":
                            if not isinstance(item.get("class"), str) or not isinstance(item.get("message"), str):
                                raise ValueError("invalid worker error")
                            terminal = True
                            error = fail(item["class"], item["message"])
                        else:
                            raise ValueError("worker record out of order")
                    if error or callback_error:
                        break
                    if len(pending) > MAX_WORKER_LINE_BYTES:
                        raise ValueError("worker record too large")
                if error or callback_error:
                    break
            if not error:
                if pending or not terminal or not headers_seen:
                    error = fail("provider_worker_failed", "传输进程输出不完整或顺序无效。")
                else:
                    try:
                        code = process.wait(timeout=max(0, min(CLEANUP_GRACE_SECONDS, until - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        code = None
                    if code != 0:
                        error = fail("provider_worker_failed", "传输进程未正常退出。")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        error = (fail("provider_worker_failed", "传输进程启动、读取或输出格式失败。")
                 if callback_error is None else fail(
                     getattr(callback_error, "error_class", "callback_failed"),
                     "取消状态回调失败，正在回收传输进程。", stage="control"))
    except BaseException:
        if callback_error is None:
            raise
        error = fail(getattr(callback_error, "error_class", "callback_failed"),
                     "取消状态回调失败，正在回收传输进程。", stage="control")
    finally:
        if process is not None:
            confirmed = _stop_worker(process)
            memory_gate.update(worker_pid=process.pid,worker_exit_code=process.returncode,cleanup_confirmed=confirmed)
            for handle in (process.stdin, process.stdout, process.stderr):
                if handle is not None and not handle.closed:
                    handle.close()
            if error is not None:
                error["cleanup_confirmed"] = confirmed
            elif not confirmed:
                error = fail("cleanup_unconfirmed", "传输进程回收尚未确认。")
                error["cleanup_confirmed"] = False
    if process is not None and error is not None and memory_gate["sent_status"] == "not_started" and not terminal:
        memory_gate["sent_status"] = "unknown"
    if memory_gate["sent_status"] == "started" and not headers_seen:
        memory_gate["sent_status"] = "unknown"
    exchange = snapshot()
    if callback_error is not None:
        callback_error.stream_exchange = exchange
        raise callback_error
    return exchange


def _stream_worker():
    envelope = json.load(sys.stdin)
    config = Config(**envelope["config"])
    body = json.dumps(envelope["request"], ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json",
        "Accept": "text/event-stream", "User-Agent": "agent-study-pra-w05/1"})
    started = time.monotonic()
    from memory import validate_worker_binding
    binding = envelope["memory_binding"]
    deadline = envelope["deadline"]
    memory_gate = {"memory_binding": binding, "sent_status": "not_started",
                   "gate_checked_at": None, "transport_started_at": None, "headers_at": None}
    total = 0
    headers_sent = False

    def emit(item):
        print(json.dumps(item, ensure_ascii=False), flush=True)

    try:
        opener = urllib.request.build_opener(NoRedirectHandler())
        with gate(binding["db_path"], deadline) as timing:
            memory_gate.update(timing)
            validate_worker_binding(binding, now=utc_now())
            memory_gate["gate_checked_at"] = utc_now()
            if time.monotonic() >= deadline:
                raise RetrievalError("deadline_exceeded", "HTTP 交接前运行已到期。")
            memory_gate.update(transport_started_at=utc_now(), sent_status="started")
            emit({"type":"memory_gate", "record":memory_gate})
            try:
                response = opener.open(request, timeout=min(30, config.timeout_seconds,
                                                           max(.001,deadline-time.monotonic())))
            except urllib.error.HTTPError as exc:
                response = exc
            memory_gate.update(headers_at=utc_now(), sent_status="response_headers")
            emit({"type":"memory_gate", "record":memory_gate})
        with response:
            emit({"type": "headers", "status": response.status,
                  "request_id": sanitize(response.headers.get("x-request-id"), (config.api_key,))})
            headers_sent = True
            while True:
                chunk = response.read1(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    emit({"type": "error", "class": "response_too_large", "message": "响应超过 2 MiB 上限。"})
                    return 0
                emit({"type": "chunk", "data": base64.b64encode(chunk).decode("ascii"),
                      "arrival_ms": round((time.monotonic() - started) * 1000)})
            emit({"type": "end"})
    except RetrievalError as exc:
        memory_gate.update(getattr(exc,"memory_gate",{}))
        emit({"type":"memory_gate", "record":memory_gate})
        emit({"type":"error", "class":exc.code, "message":str(exc)})
    except (TimeoutError, socket.timeout):
        emit({"type": "error", "class": "provider_timeout", "message": "模型传输超时。"})
    except http.client.IncompleteRead as exc:
        if headers_sent and exc.partial and total + len(exc.partial) <= MAX_RESPONSE_BYTES:
            emit({"type": "chunk", "data": base64.b64encode(exc.partial).decode("ascii"),
                  "arrival_ms": round((time.monotonic() - started) * 1000)})
        emit({"type": "error", "class": "network_error", "message": "模型响应在读取期间中断。"})
    except (OSError, http.client.HTTPException, urllib.error.URLError):
        emit({"type": "error", "class": "network_error", "message": "模型传输连接或读取失败。"})
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--stream-worker"]:
        raise SystemExit("w11_stream.py 是供给模块；请运行自己实现的 main.py。")
    raise SystemExit(_stream_worker())
