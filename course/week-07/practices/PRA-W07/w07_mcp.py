"""官方 MCP SDK 的独立进程与 stdio 供给；模型绑定和对象许可由 Host 实现。"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from uuid import uuid4

SDK_VERSION = "1.30.0"
PROTOCOL_VERSION = "2025-11-25"
MAX_CONFIG_BYTES = 16 * 1024
MAX_WIRE_LINE = 64 * 1024
MAX_RECEIVE_BYTES = 256 * 1024
METHODS = {"tools/list", "tools/call", "resources/list", "resources/read", "prompts/list", "prompts/get"}


class McpError(ValueError):
    def __init__(self, error_class, message):
        super().__init__(message)
        self.error_class = error_class


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise McpError("invalid_json", f"重复 JSON 字段：{key}")
        result[key] = value
    return result


def _constant(value):
    raise McpError("invalid_json", f"不支持的 JSON 数值：{value}")


def strict_json(data):
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
        json.dumps(value, ensure_ascii=False).encode("utf-8")
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise McpError("invalid_json", str(error)) from error


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _regular(path, limit=None):
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise McpError("invalid_config", f"需要普通单硬链接文件：{path}")
        if limit is not None and info.st_size > limit:
            raise McpError("invalid_config", f"文件超过 {limit} 字节：{path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(limit + 1 if limit is not None else -1)
        if limit is not None and len(data) > limit:
            raise McpError("invalid_config", "读取期间文件超过限制。")
        return data
    finally:
        os.close(descriptor)


def _strings(value, label, *, maximum=32):
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) or not item or len(item.encode("utf-8")) > 1024
                   or "\x00" in item for item in value) or len(set(value)) != len(value)):
        raise McpError("invalid_config", f"{label} 需要不重复的有限字符串数组。")


def load_config(path):
    """读取可信启动配置；返回独立快照，不判断某个模型查询是否获许可。"""
    try:
        value = strict_json(_regular(path, MAX_CONFIG_BYTES).decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"schema_version", "sdk_python", "servers"}:
            raise McpError("invalid_config", "配置字段须为 schema_version、sdk_python、servers。")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise McpError("invalid_config", "不支持的配置版本。")
        interpreter = value["sdk_python"]
        if not isinstance(interpreter, str) or not Path(interpreter).is_absolute():
            raise McpError("invalid_config", "sdk_python 必须是绝对路径。")
        if not Path(interpreter).is_file() or not os.access(interpreter, os.X_OK):
            raise McpError("invalid_config", "sdk_python 不可执行。")
        servers = value["servers"]
        if not isinstance(servers, list) or not 1 <= len(servers) <= 2:
            raise McpError("invalid_config", "servers 必须包含 1～2 个服务。")
        names = set()
        for server in servers:
            if not isinstance(server, dict) or set(server) != {"id", "argv", "allowed_tools", "resource_uris"}:
                raise McpError("invalid_config", "服务配置字段不完整或包含未知项。")
            name = server["id"]
            if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,15}", name) is None or name in names:
                raise McpError("invalid_config", "服务 id 非法或重复。")
            names.add(name)
            argv = server["argv"]
            _strings(argv, "argv", maximum=16)
            if len(argv) < 2 or argv[0] != interpreter or not Path(argv[1]).is_absolute():
                raise McpError("invalid_config", "argv 须使用 sdk_python 与绝对脚本路径。")
            if not argv[1].endswith(".py"):
                raise McpError("invalid_config", "本周只启动明确的 Python 服务脚本。")
            _regular(argv[1], 256 * 1024)
            allowed = server["allowed_tools"]
            if not isinstance(allowed, dict) or set(allowed) != {"lookup"}:
                raise McpError("invalid_config", "本周只登记经审阅的 lookup 工具。")
            policy = allowed["lookup"]
            if not isinstance(policy, dict) or set(policy) != {"document_ids"}:
                raise McpError("invalid_config", "lookup 许可需要 document_ids。")
            _strings(policy["document_ids"], "document_ids")
            _strings(server["resource_uris"], "resource_uris")
        result = json.loads(canonical(value))
        result["config_digest"] = digest(value)
        return result
    except (OSError, UnicodeError, RecursionError) as error:
        raise McpError("invalid_config", str(error)) from error


def _now():
    return datetime.now(timezone.utc).isoformat()


def _narrow_env():
    return {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
            "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}


@dataclass(frozen=True)
class McpExchange:
    connection_id: str
    rpc_id: int | str | None
    method: str
    sent: bool | str
    result: dict | None
    protocol_error: dict | None
    transport_error: dict | None
    started_at: str
    finished_at: str
    elapsed_ms: float


class McpConnection:
    """一个 worker 拥有一个 SDK session；父进程只负责控制与回收。"""

    def __init__(self, server_config, *, sdk_python, evidence_dir, deadline, cancelled):
        self.server_config = json.loads(canonical(server_config))
        self.sdk_python = str(sdk_python)
        self.evidence_dir = Path(evidence_dir)
        self.deadline = deadline
        self.cancelled = cancelled
        self.connection_id = uuid4().hex
        self.process = None
        self.server_pid = None
        self.initialization = None
        self.cleanup = None
        self._buffer = bytearray()
        self._selector = selectors.DefaultSelector()
        self._wire = None
        self._worker_stderr = None
        self._seq = 0
        self._last_rpc = None
        self._last_sent = False
        self._service_start_attempted = False
        self._counts = {"mcp_requests": 0, "mcp_calls": 0, "mcp_resource_reads": 0}
        self._secrets = tuple(value for key, value in os.environ.items()
                              if key in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"} and value)

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None and self.cleanup is None

    def stats(self):
        return dict(self._counts)

    def _redact(self, text):
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _save(self, name, value):
        data = self._redact(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)) + "\n"
        with (self.evidence_dir / name).open("x", encoding="utf-8") as handle:
            handle.write(data)

    def _packet(self, packet):
        kind = packet.get("kind")
        if kind == "service_starting":
            self._service_start_attempted = True
        elif kind == "server_started":
            self.server_pid = packet["pid"]
        elif kind == "wire":
            raw = base64.b64decode(packet["raw_base64"], validate=True)
            text = raw.decode("utf-8", errors="replace")
            safe = self._redact(text)
            message = packet.get("message")
            if packet["direction"] == "send" and isinstance(message, dict) and "method" in message and "id" in message:
                self._last_rpc = message["id"]
                self._last_sent = packet.get("sent", "unknown")
                if self._last_sent is True:
                    self._counts["mcp_requests"] += 1
                    self._counts["mcp_calls"] += int(message["method"] == "tools/call")
                    self._counts["mcp_resource_reads"] += int(message["method"] == "resources/read")
            self._seq += 1
            record = {**packet, "connection_id": self.connection_id, "seq": self._seq,
                      "raw_base64": base64.b64encode(safe.encode("utf-8") if safe != text else raw).decode(),
                      "redacted": safe != text}
            self._wire.write(self._redact(canonical(record)) + "\n")
            self._wire.flush()
        elif kind == "service_cleanup":
            self._service_cleanup = packet
        return kind

    def _receive(self, until, cancelled):
        while True:
            while b"\n" in self._buffer:
                raw, _, remaining = self._buffer.partition(b"\n")
                self._buffer = bytearray(remaining)
                packet = strict_json(raw.decode("utf-8"))
                if not isinstance(packet, dict):
                    raise McpError("worker_protocol_error", "worker 消息必须为对象。")
                kind = self._packet(packet)
                if kind in {"ready", "result", "fatal"}:
                    return packet
            if cancelled():
                raise McpError("cancelled", "收到取消请求。")
            if time.monotonic() >= until:
                raise McpError("mcp_timeout", "MCP 等待超过期限。")
            readable = self._selector.select(min(0.05, max(0, until - time.monotonic())))
            if readable:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise McpError("mcp_transport_closed", "SDK worker 已关闭输出。")
                self._buffer.extend(chunk)
                if len(self._buffer) > 1024 * 1024:
                    raise McpError("worker_protocol_error", "worker 控制消息超过上限。")
            elif self.process.poll() is not None:
                raise McpError("mcp_transport_closed", "SDK worker 已退出。")

    def _send(self, body):
        self.process.stdin.write((canonical(body) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def start(self):
        if self.process is not None or self.cleanup is not None:
            raise McpError("connection_already_started", "连接只能启动一次。")
        self.evidence_dir.mkdir(parents=True, exist_ok=False)
        try:
            if self.cancelled():
                raise McpError("cancelled", "启动前已取消。")
            if time.monotonic() >= self.deadline:
                raise McpError("mcp_timeout", "启动前任务期限已到。")
            self._wire = (self.evidence_dir / "wire.jsonl").open("x", encoding="utf-8")
            self._worker_stderr = (self.evidence_dir / "worker-stderr.txt").open("xb")
            self.process = subprocess.Popen(
                [self.sdk_python, "-I", str(Path(__file__).resolve()), "--worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._worker_stderr,
                env=_narrow_env(), cwd=str(self.evidence_dir), start_new_session=True, bufsize=0)
            self._selector.register(self.process.stdout, selectors.EVENT_READ)
            self._send({"argv": self.server_config["argv"], "connection_id": self.connection_id})
            packet = self._receive(min(self.deadline, time.monotonic() + 5), self.cancelled)
            if packet["kind"] != "ready":
                raise McpError(packet.get("error_class", "mcp_start_failed"), packet.get("message", "MCP 未就绪。"))
            self.initialization = {key: value for key, value in packet.items() if key != "kind"}
            self.initialization.update(connection_id=self.connection_id, worker_pid=self.process.pid,
                                       server_pid=self.server_pid)
            self._save("initialization.json", self.initialization)
            return dict(self.initialization)
        except BaseException as error:
            self._close_after_exception(error)
            raise

    def request(self, method, params, *, deadline, cancelled):
        started_at, started = _now(), time.monotonic()
        self._last_rpc, self._last_sent = None, False
        result = protocol_error = transport_error = None
        try:
            if method not in METHODS or not isinstance(params, dict):
                raise McpError("unsupported_method", "不支持的 MCP 方法或参数形状。")
            if not self.alive or self.initialization is None:
                raise McpError("connection_closed", "MCP 连接不可用。")
            if cancelled():
                raise McpError("cancelled", "派发前已取消。")
            if time.monotonic() >= min(deadline, self.deadline):
                raise McpError("mcp_timeout", "派发前期限已到。")
            # 交给 worker 后，父进程可能在收到 wire 回执前失去连接。
            self._last_sent = "unknown"
            self._send({"method": method, "params": params})
            packet = self._receive(min(deadline, self.deadline, time.monotonic() + 5), cancelled)
            if packet["kind"] == "fatal":
                raise McpError(packet.get("error_class", "mcp_worker_failed"), packet.get("message", "SDK worker 失败。"))
            result = packet.get("result")
            protocol_error = packet.get("protocol_error")
            transport_error = packet.get("transport_error")
            if packet.get("rpc_id") != self._last_rpc:
                raise McpError("worker_protocol_error", "worker 结果与实际 RPC id 不一致。")
            if transport_error is not None:
                self.close()
        except McpError as error:
            result = protocol_error = None
            transport_error = {"class": error.error_class, "message": str(error)}
            self.close()
        except (OSError, UnicodeError) as error:
            result = protocol_error = None
            transport_error = {"class": "mcp_io_error", "message": str(error)}
            self.close()
        except BaseException as error:
            self._close_after_exception(error)
            error.mcp_exchange = McpExchange(
                self.connection_id, self._last_rpc, method, self._last_sent, None, None,
                {"class": type(error).__name__, "message": str(error)}, started_at, _now(),
                round((time.monotonic() - started) * 1000, 3))
            raise
        exchange = McpExchange(self.connection_id, self._last_rpc, method, self._last_sent,
                               result, protocol_error, transport_error, started_at, _now(),
                               round((time.monotonic() - started) * 1000, 3))
        return exchange

    def _close_after_exception(self, original):
        try:
            original.mcp_cleanup = self.close()
        except BaseException as cleanup_error:
            original.add_note(f"MCP 清理记录异常：{type(cleanup_error).__name__}: {cleanup_error}")

    def _drain(self):
        """关闭期间继续消费协议事实，避免把已经发出的调用误记为零。"""
        if self.process is None or self.process.stdout.closed:
            return
        while self._selector.select(0):
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                break
            self._buffer.extend(chunk)
        while b"\n" in self._buffer:
            raw, _, remaining = self._buffer.partition(b"\n")
            self._buffer = bytearray(remaining)
            packet = strict_json(raw.decode("utf-8"))
            self._packet(packet)

    def close(self):
        if self.cleanup is not None:
            return dict(self.cleanup)
        actions, errors = [], []
        process = self.process
        if process is not None:
            try:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                    actions.append("close_worker_stdin")
                normal_until = time.monotonic() + 0.4
                while process.poll() is None and time.monotonic() < normal_until:
                    self._drain()
                    try:
                        process.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        pass
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    actions.append("terminate_worker_for_owned_cleanup")
                until = time.monotonic() + 3.5
                while process.poll() is None and time.monotonic() < until:
                    self._drain()
                    try:
                        process.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        pass
                self._drain()
            except (OSError, ValueError) as error:
                errors.append(str(error))
            finally:
                # worker 与服务同属新建进程组；leader 已退出时仍清除其存活后代。
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    actions.append("kill_owned_group")
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(str(error))
                try:
                    process.wait(timeout=0.8)
                except (OSError, subprocess.TimeoutExpired) as error:
                    errors.append(str(error))
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
        service = getattr(self, "_service_cleanup", {})
        worker_done = process is None or process.returncode is not None
        # 服务的 wait 回执由它的直接父进程 worker 提供；killpg 成功不足以证明已回收。
        service_done = not self._service_start_attempted or service.get("cleanup_confirmed") is True
        self.cleanup = {"connection_id": self.connection_id,
                        "worker_pid": process.pid if process else None, "server_pid": self.server_pid,
                        "worker_returncode": process.returncode if process else None,
                        "server_returncode": service.get("returncode"),
                        "cleanup_confirmed": worker_done and service_done and not errors,
                        "actions": actions + service.get("actions", []), "error": errors or service.get("error")}
        self._selector.close()
        if self._wire is not None:
            self._wire.close()
        if self._worker_stderr is not None:
            self._worker_stderr.close()
        if self.evidence_dir.is_dir():
            self._save("cleanup.json", self.cleanup)
        return dict(self.cleanup)


def _worker_main():
    """以下代码只在固定 SDK 的子进程中加载。"""
    import importlib.metadata
    from contextlib import asynccontextmanager
    from datetime import timedelta

    def emit(value):
        sys.stdout.write(canonical(value) + "\n")
        sys.stdout.flush()

    try:
        version = importlib.metadata.version("mcp")
        if sys.version_info[:2] != (3, 12) or version != SDK_VERSION:
            raise McpError("sdk_version_mismatch", f"需要 Python 3.12/mcp {SDK_VERSION}，实际 {sys.version.split()[0]}/{version}")
        import anyio
        from mcp import ClientSession
        from mcp.shared.exceptions import McpError as SdkError
        from mcp.shared.message import SessionMessage
        import mcp.types as types
        config = strict_json(sys.stdin.buffer.readline().decode("utf-8"))
    except Exception as error:
        emit({"kind": "fatal", "error_class": getattr(error, "error_class", "sdk_unavailable"), "message": str(error)})
        return 2

    class Transport:
        def __init__(self):
            self.process = None
            self.last_id = None
            self.errors = {}
            self.receive_bytes = 0

        def wire(self, direction, raw, message=None, sent=None):
            packet = {"kind": "wire", "direction": direction, "monotonic": time.monotonic(),
                      "raw_base64": base64.b64encode(raw).decode(), "message": message}
            if sent is not None:
                packet["sent"] = sent
            emit(packet)

        async def read(self, incoming):
            pending = bytearray()
            async with incoming:
                try:
                    while True:
                        try:
                            chunk = await self.process.stdout.receive(16384)
                        except anyio.EndOfStream:
                            if pending:
                                self.wire("receive", bytes(pending))
                                await incoming.send(McpError("mcp_bad_frame", "EOF 前存在未结束的 JSONL。"))
                            break
                        self.receive_bytes += len(chunk)
                        pending.extend(chunk)
                        if self.receive_bytes > MAX_RECEIVE_BYTES:
                            self.wire("receive", bytes(pending))
                            await incoming.send(McpError("mcp_receive_limit", "MCP 累计响应超过上限。"))
                            break
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            raw = bytes(line) + b"\n"
                            if len(raw) > MAX_WIRE_LINE:
                                self.wire("receive", raw)
                                await incoming.send(McpError("mcp_line_limit", "MCP 报文超过上限。"))
                                return
                            try:
                                body = strict_json(line.decode("utf-8"))
                                message = types.JSONRPCMessage.model_validate(body)
                            except (ValueError, UnicodeError) as error:
                                self.wire("receive", raw)
                                await incoming.send(error)
                                return
                            self.wire("receive", raw, body)
                            if isinstance(body, dict) and "error" in body and "id" in body:
                                self.errors[body["id"]] = body["error"]
                            await incoming.send(SessionMessage(message))
                        if len(pending) > MAX_WIRE_LINE:
                            self.wire("receive", bytes(pending))
                            await incoming.send(McpError("mcp_line_limit", "MCP 报文超过上限。"))
                            break
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    return

        async def write(self, outgoing):
            async with outgoing:
                async for item in outgoing:
                    body = item.message.model_dump(mode="json", by_alias=True, exclude_none=True)
                    raw = (canonical(body) + "\n").encode("utf-8")
                    if "method" in body and "id" in body:
                        self.last_id = body["id"]
                    try:
                        if len(raw) > MAX_WIRE_LINE:
                            raise McpError("mcp_line_limit", "请求报文超过上限。")
                        await self.process.stdin.send(raw)
                    except BaseException:
                        self.wire("send", raw, body, "unknown")
                        raise
                    self.wire("send", raw, body, True)

        @asynccontextmanager
        async def connect(self):
            stderr = open("stderr.txt", "xb")
            incoming, read = anyio.create_memory_object_stream(0)
            write, outgoing = anyio.create_memory_object_stream(0)
            actions, failure = [], None
            try:
                # 继承 worker 的专属进程组，父进程的最终 killpg 同时覆盖两者。
                emit({"kind": "service_starting"})
                self.process = await anyio.open_process(config["argv"], env=_narrow_env(), stderr=stderr)
                emit({"kind": "server_started", "pid": self.process.pid})
                async with anyio.create_task_group() as group:
                    group.start_soon(self.read, incoming)
                    group.start_soon(self.write, outgoing)
                    try:
                        yield read, write
                    finally:
                        with anyio.CancelScope(shield=True):
                            try:
                                await self.process.stdin.aclose()
                                actions.append("close_service_stdin")
                                with anyio.move_on_after(0.5):
                                    await self.process.wait()
                                if self.process.returncode is None:
                                    self.process.terminate()
                                    actions.append("terminate_service")
                                    with anyio.move_on_after(0.5):
                                        await self.process.wait()
                                if self.process.returncode is None:
                                    self.process.kill()
                                    actions.append("kill_service")
                                with anyio.fail_after(1):
                                    await self.process.wait()
                            except Exception as error:
                                failure = str(error)
                            finally:
                                emit({"kind": "service_cleanup", "pid": self.process.pid,
                                      "returncode": self.process.returncode,
                                      "cleanup_confirmed": self.process.returncode is not None and failure is None,
                                      "actions": actions, "error": failure})
                                group.cancel_scope.cancel()
            finally:
                stderr.close()
                with anyio.CancelScope(shield=True):
                    for stream in (read, write, incoming, outgoing):
                        await stream.aclose()
                    if self.process is not None:
                        await self.process.aclose()

    async def run():
        import asyncio
        transport = Transport()
        result_types = {"tools/list": types.ListToolsResult, "tools/call": types.CallToolResult,
                        "resources/list": types.ListResourcesResult, "resources/read": types.ReadResourceResult,
                        "prompts/list": types.ListPromptsResult, "prompts/get": types.GetPromptResult}
        with anyio.CancelScope() as scope:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, scope.cancel)
            async with transport.connect() as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                    initialized = await session.initialize()
                    if initialized.protocolVersion != PROTOCOL_VERSION:
                        raise McpError("protocol_version_mismatch", "服务协商版本与本周固定协议不一致。")
                    emit({"kind": "ready", "sdk_version": version, "python_version": sys.version.split()[0],
                          "protocol_version": initialized.protocolVersion,
                          "capabilities": initialized.capabilities.model_dump(mode="json", by_alias=True, exclude_none=True),
                          "server_info": initialized.serverInfo.model_dump(mode="json", by_alias=True, exclude_none=True),
                          "instructions": initialized.instructions})
                    while True:
                        line = await anyio.to_thread.run_sync(sys.stdin.buffer.readline, abandon_on_cancel=True)
                        if not line:
                            break
                        body = strict_json(line.decode("utf-8"))
                        method, params = body["method"], body["params"]
                        if method not in METHODS:
                            raise McpError("unsupported_method", method)
                        transport.receive_bytes = 0
                        transport.last_id = None
                        result = protocol_error = transport_error = None
                        try:
                            request = types.ClientRequest.model_validate({"method": method, "params": params})
                            response = await session.send_request(request, result_types[method],
                                                                  request_read_timeout_seconds=timedelta(seconds=5))
                            result = response.model_dump(mode="json", by_alias=True, exclude_none=True)
                        except SdkError as error:
                            if transport.last_id in transport.errors:
                                protocol_error = transport.errors[transport.last_id]
                            else:
                                transport_error = {"class": "mcp_sdk_error", "message": str(error)}
                        emit({"kind": "result", "rpc_id": transport.last_id, "result": result,
                              "protocol_error": protocol_error, "transport_error": transport_error})
                        if transport_error is not None:
                            break
    try:
        anyio.run(run)
        return 0
    except Exception as error:
        import traceback
        traceback.print_exception(error, file=sys.stderr)
        emit({"kind": "fatal", "error_class": getattr(error, "error_class", "mcp_worker_failed"), "message": str(error)})
        return 1
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("w07_mcp.py 由宿主 Host 导入；worker 只通过固定解释器启动。")
    raise SystemExit(_worker_main())
