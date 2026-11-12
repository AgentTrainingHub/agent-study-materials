"""W02 的安全读取、消息序列核验、受监督传输与证据记录。

loop、工具注册、参数门禁和 dispatch 由本地工程实现。文件、分页和调用批次
上限是本周实现约束；它们不等同于 Pi 的工具输出限制。read 在主进程内
运行，参数不接收 Config 或凭据；路径约束不提供进程环境或内存隔离。
"""
from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from w01_support import Config, Exchange, sanitize, send_once, utc_now

EVIDENCE_SOURCE = "live-external-api"
MAX_FILE_BYTES = 64 * 1024
MAX_LINE_BYTES = 4 * 1024
MAX_PAGE_LINES = 80
MAX_BATCH_CALLS = 8
CLEANUP_GRACE_SECONDS = 0.25
OMITTED_CONTENT = "[read status: ok, file content omitted]"


class ProtocolError(ValueError):
    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class


class ToolError(ValueError):
    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class


class DeadlineExceeded(TimeoutError):
    pass


def check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("任务时间预算已用尽。")


def validate_transcript(messages: list[dict]) -> None:
    """拒绝未配齐反馈的下一请求；不自动补全消息。"""
    if not isinstance(messages, list) or len(messages) < 2:
        raise ProtocolError("invalid_messages", "请求必须包含 system 和 user 消息。")
    if [m.get("role") if isinstance(m, dict) else None for m in messages[:2]] != ["system", "user"]:
        raise ProtocolError("invalid_messages", "前两条消息必须是 system、user。")
    pending: set[str] = set()
    seen: set[str] = set()
    last_role = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ProtocolError("invalid_messages", "消息必须是对象。")
        role = message.get("role")
        if pending and role != "tool":
            raise ProtocolError("missing_tool_feedback", "当前工具批次的反馈尚未配齐。")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ProtocolError("unpaired_tool_feedback", "tool 消息没有唯一待配对的调用 ID。")
            if not isinstance(message.get("content"), str) or not message["content"]:
                raise ProtocolError("invalid_messages", "tool content 必须是非空字符串。")
            if set(message) != {"role", "tool_call_id", "content"}:
                raise ProtocolError("invalid_messages", "tool 消息字段不符合 W02 契约。")
            pending.remove(call_id)
        elif role == "assistant":
            if index < 2 or last_role not in {"user", "tool"}:
                raise ProtocolError("invalid_messages", "assistant 的前置消息不合法。")
            if message.get("function_call") is not None:
                raise ProtocolError("invalid_messages", "W02 不使用旧 function_call 协议。")
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not 1 <= len(calls) <= MAX_BATCH_CALLS:
                raise ProtocolError("invalid_tool_calls", "继续请求中的 assistant 必须携带完整工具批次。")
            if set(message) - {"role", "content", "tool_calls"}:
                raise ProtocolError("invalid_messages", "assistant 消息含未支持字段。")
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ProtocolError("invalid_messages", "assistant content 类型无效。")
            for call in calls:
                if not isinstance(call, dict):
                    raise ProtocolError("invalid_tool_calls", "工具调用必须是对象。")
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
                    raise ProtocolError("invalid_call_id", "调用 ID 缺失或重复。")
                function = call.get("function")
                if (call.get("type") != "function" or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str) or not function["name"].strip()
                        or not isinstance(function.get("arguments"), str)):
                    raise ProtocolError("invalid_tool_calls", "工具调用的函数字段不完整。")
                seen.add(call_id)
                pending.add(call_id)
        elif role in {"system", "user"}:
            if index > 1 or set(message) != {"role", "content"}:
                raise ProtocolError("invalid_messages", "W02 的 system/user 只在初始位置出现。")
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                raise ProtocolError("invalid_messages", "system/user content 不能为空。")
        else:
            raise ProtocolError("invalid_messages", "未知消息角色。")
        last_role = role
    if pending:
        raise ProtocolError("missing_tool_feedback", "当前工具批次的反馈尚未配齐。")
    if last_role not in {"user", "tool"}:
        raise ProtocolError("invalid_messages", "请求必须结束于 user 或配齐的 tool 消息。")


def _read_bytes(fd: int, maximum: int) -> bytes:
    chunks = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(fd, min(8192, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def safe_read(workspace: Path, path: str, offset: int = 1, limit: int = 40) -> dict:
    """通过目录文件描述符逐级打开，拒绝符号链接和特殊文件。"""
    if (not isinstance(path, str) or not path or "\x00" in path or "\\" in path
            or Path(path).is_absolute() or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ToolError("path_denied", "path 必须为工作区内的相对路径，不能包含 .. 或符号链接。")
    if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= MAX_PAGE_LINES:
        raise ToolError("invalid_arguments", "offset 必须从 1 开始，limit 必须为 1～80 的整数。")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ToolError("path_denied", "path 必须能编码为 UTF-8。") from error
    opened = []
    try:
        directory = os.open(str(workspace), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened.append(directory)
        parts = path.split("/")
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            opened.append(directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        opened.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ToolError("file_type_denied", "read 只接受普通文本文件。")
        if info.st_size > MAX_FILE_BYTES:
            raise ToolError("file_too_large", "文件超过 W02 的 64 KiB 上限。")
        content = _read_bytes(fd, MAX_FILE_BYTES)
        if len(content) > MAX_FILE_BYTES:
            raise ToolError("file_too_large", "读取期间文件超过 W02 的 64 KiB 上限。")
        if any(len(line) > MAX_LINE_BYTES for line in content.splitlines()):
            raise ToolError("line_too_long", "单行超过 W02 的 4 KiB 上限。")
        try:
            text = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            # str.splitlines 还会拆开源码正文中的 Unicode 分隔符，导致引用行号偏移。
            lines = text.split("\n") if text else []
            if lines and lines[-1] == "":
                lines.pop()
        except UnicodeDecodeError as error:
            raise ToolError("invalid_encoding", "文件必须是 UTF-8 文本。") from error
        if offset > max(1, len(lines)):
            raise ToolError("offset_out_of_range", "offset 超过文件的最后一行。")
        end = min(len(lines), offset - 1 + limit)
        truncated = end < len(lines)
        return {
            "ok": True, "path": path, "offset": offset, "limit": limit,
            "lines": [{"line": i + 1, "text": lines[i]} for i in range(offset - 1, end)],
            "total_lines": len(lines), "truncated": truncated,
            "next_offset": end + 1 if truncated else None,
        }
    except OSError as error:
        if error.errno == errno.ENOENT:
            raise ToolError("file_not_found", "工作区内找不到该文件。") from error
        if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            raise ToolError("path_denied", "路径含符号链接、不可访问目录或权限拒绝。") from error
        raise ToolError("read_failed", f"读取失败：{error.strerror}。") from error
    finally:
        for fd in reversed(opened):
            os.close(fd)


def make_tool_message(call_id: str, result: dict) -> dict:
    if not isinstance(call_id, str) or not call_id.strip():
        raise ProtocolError("invalid_call_id", "tool 消息需要原始调用 ID。")
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False)}


def usage_from_response(raw: dict) -> dict:
    usage = raw.get("usage") if isinstance(raw, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    result = {name: value if type(value := usage.get(name)) is int and value >= 0 else None
              for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    known = all(value is not None for value in result.values())
    known = known and result["prompt_tokens"] + result["completion_tokens"] == result["total_tokens"]
    return {"usage_status": "known" if known else "usage_unknown", **result}


def aggregate_usage(turns: list[dict]) -> dict:
    known = bool(turns) and all(turn.get("usage_status") == "known" for turn in turns)
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    return {"usage_status": "known" if known else "usage_unknown",
            **{field: sum(turn[field] for turn in turns) if known else None for field in fields}}


def _provider_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--provider-worker"]


def _worker_environment() -> dict:
    # 传输组件只接收必要的进程环境；认证信息通过私有 stdin 单独传递。
    allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def call_model_once(config: Config, request: dict, deadline: float) -> Exchange:
    validate_transcript(request.get("messages"))
    check_deadline(deadline)
    started = time.monotonic()
    started_at = utc_now()
    remaining = deadline - started
    budget = min(config.timeout_seconds, remaining)
    deadline_limited = remaining <= config.timeout_seconds
    envelope = json.dumps({"config": asdict(config), "request": request}, ensure_ascii=False)
    error = None
    worker_result = None
    try:
        process = subprocess.Popen(_provider_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", env=_worker_environment())
        try:
            output, _ = process.communicate(envelope, timeout=budget)
        except subprocess.TimeoutExpired:
            process.terminate()
            cleanup_confirmed = True
            try:
                process.communicate(timeout=CLEANUP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.communicate(timeout=CLEANUP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    cleanup_confirmed = False
            cleanup_confirmed = cleanup_confirmed and process.poll() is not None
            error = {"stage": "transport", "class": "deadline_exceeded" if deadline_limited else "provider_timeout",
                     "cleanup_confirmed": cleanup_confirmed,
                     "message": "模型请求到达截止时间；传输进程已退出。" if cleanup_confirmed
                     else "模型请求到达截止时间；已发出终止信号，进程回收尚未确认。"}
        else:
            if process.returncode != 0:
                error = {"stage": "transport", "class": "provider_worker_failed", "message": "传输进程异常退出。"}
            else:
                try:
                    worker_result = Exchange(**json.loads(output))
                except (json.JSONDecodeError, TypeError):
                    error = {"stage": "transport", "class": "provider_worker_failed", "message": "传输进程返回格式无效。"}
    except OSError as exc:
        error = {"stage": "transport", "class": "provider_worker_failed", "message": str(exc)}
    if worker_result is not None:
        # 记录的是 worker 真正送入 send_once 的 payload，校验传输组件未更改它。
        expected = sanitize(request, (config.api_key,))
        if worker_result.request_payload != expected:
            error = {"stage": "transport", "class": "request_mismatch", "message": "传输记录与待发送 payload 不一致。"}
        else:
            return worker_result
    return Exchange(request, None, False, None, None, error, started_at, utc_now(),
                    round((time.monotonic() - started) * 1000))


class RunLog:
    def __init__(self, output_dir: Path):
        self.run_id = f"run-{uuid.uuid4()}"
        self.directory = (Path(output_dir) / self.run_id).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.seq = 0
        self.secrets = (os.environ.get("DEEPSEEK_API_KEY", "").strip(),)

    def _write_json(self, path: Path, value: Any) -> None:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(sanitize(value, self.secrets), handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    def event(self, kind: str, *, turn: int = 0, **fields) -> dict:
        self.seq += 1
        event = sanitize({"run_id": self.run_id, "evidence_source": EVIDENCE_SOURCE, "seq": self.seq,
                          "time": utc_now(), "event": kind, "turn": turn, **fields}, self.secrets)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def save_turn(self, turn: int, request: dict, exchange: Exchange, normalized: dict) -> None:
        directory = self.directory / f"turn-{turn:03d}"
        directory.mkdir(exist_ok=False)
        self._write_json(directory / "request.json", exchange.request_payload)
        if exchange.has_response:
            self._write_json(directory / "raw-response.json", exchange.raw_response)
        self._write_json(directory / "receipt.json", {
            "run_id": self.run_id, "evidence_source": EVIDENCE_SOURCE, "turn": turn,
            "requested_model": request.get("model"), "actual_model": normalized.get("actual_model"),
            "request_id": exchange.request_id or (exchange.raw_response.get("id") if isinstance(exchange.raw_response, dict) else None),
            "request_delivery": "response_received" if exchange.has_response else "unconfirmed",
            "http_status": exchange.http_status, "finish_reason": normalized.get("finish_reason"),
            "usage": normalized.get("usage"), "error": normalized.get("error"),
            "started_at": exchange.started_at, "finished_at": exchange.finished_at, "elapsed_ms": exchange.elapsed_ms,
        })

    def finish(self, result: dict) -> dict:
        result = sanitize({**result, "run_id": self.run_id, "evidence_source": EVIDENCE_SOURCE,
                           "output_directory": str(self.directory)}, self.secrets)
        self.event("run_finished", status=result["status"], exit_code=result["exit_code"])
        self._write_json(self.directory / "result.json", result)
        return result


def _provider_worker() -> int:
    envelope = json.load(sys.stdin)
    config = Config(**envelope["config"])
    exchange = send_once(config, envelope["request"])
    print(json.dumps(sanitize(asdict(exchange), (config.api_key,)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--provider-worker"]:
        raise SystemExit("w02_support.py 是供给模块；请运行自己实现的 main.py。")
    raise SystemExit(_provider_worker())
