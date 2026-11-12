"""W01 的环境、单次传输与记录辅助能力；消息构造和结果判断由本地工程实现。

配置校验、脱敏与错误分类沿用 api-foundation 的做法；本模块独立可复制，
不沿用连通性工具的 usage 必须存在规则，也不依赖整个课程仓库。
"""
from __future__ import annotations

import json
import http.client
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENDPOINT = "https://api.deepseek.com/chat/completions"
EVIDENCE_SOURCE = "live-external-api"


class ConfigurationError(ValueError):
    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    model: str
    max_tokens: int
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class Exchange:
    request_payload: dict
    raw_response: Any
    has_response: bool
    http_status: int | None
    request_id: str | None
    error: dict | None
    started_at: str
    finished_at: str
    elapsed_ms: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_config(max_tokens: int) -> Config:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    model = os.environ.get("DEEPSEEK_MODEL", "").strip()
    if not key or not model:
        raise ConfigurationError(
            "configuration_missing",
            "必须设置 DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL。",
        )
    if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
        raise ConfigurationError("configuration_invalid", "max_tokens 必须为 1～4096 的整数。")
    base = os.environ.get("DEEPSEEK_API_BASE", "").strip()
    if base and base.rstrip("/") not in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
        raise ConfigurationError("configuration_invalid", "本周只使用固定的 DeepSeek 官方 HTTPS 端点。")
    try:
        timeout = float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "60"))
    except ValueError as error:
        raise ConfigurationError("configuration_invalid", "超时必须为有效秒数。") from error
    if not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ConfigurationError("configuration_invalid", "超时必须大于 0 且不超过 120 秒。")
    return Config(key, model, max_tokens, timeout)


def build_request(messages: list[dict], config: Config) -> dict:
    if not isinstance(messages, list) or not messages:
        raise ConfigurationError("configuration_invalid", "消息列表不能为空。")
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in {"system", "user", "assistant"}
            or not isinstance(message.get("content"), str)
            or not message["content"].strip()
            or set(message) != {"role", "content"}
        ):
            raise ConfigurationError("configuration_invalid", "本周消息只含 role 和非空 content。")
    return {
        "model": config.model,
        "messages": [{"role": item["role"], "content": item["content"]} for item in messages],
        "temperature": 0,
        "max_tokens": config.max_tokens,
        "stream": False,
        "thinking": {"type": "disabled"},
    }


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classify_http_status(code: int) -> str:
    if code in (408, 504):
        return "provider_timeout"
    if code >= 500:
        return "provider_unavailable"
    return {
        401: "authentication_failed",
        402: "balance_insufficient",
        403: "permission_denied",
        429: "rate_limited",
    }.get(code, "provider_rejected_request")


def sanitize(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    names = {"authorization", "api-key", "api_key", "apikey", "deepseek_api_key"}
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in names else sanitize(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "[REDACTED]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)


def _decode_body(body: bytes) -> tuple[Any, dict | None]:
    text = body.decode("utf-8", errors="replace")
    try:
        raw = json.loads(text)
        json.dumps(raw, ensure_ascii=False).encode("utf-8")
        return raw, None
    except (ValueError, RecursionError):
        return {"unparsed_body": text}, {
            "stage": "response",
            "class": "invalid_json",
            "message": "Provider 返回了无效 JSON。",
        }


def send_once(config: Config, request: dict) -> Exchange:
    """在唯一传输边界保存实际请求体；不执行自动重试。"""
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "agent-study-pra-w01/1",
        },
        method="POST",
    )
    started_at = utc_now()
    start = time.perf_counter()
    raw = None
    has_response = False
    http_status = None
    request_id = None
    error = None
    try:
        opener = urllib.request.build_opener(NoRedirectHandler())
        try:
            response = opener.open(http_request, timeout=config.timeout_seconds)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            http_status = response.status
            request_id = response.headers.get("x-request-id")
            raw, error = _decode_body(response.read())
            has_response = True
        if http_status != 200:
            error = {
                "stage": "http", "class": classify_http_status(http_status),
                "message": f"Provider HTTP 状态为 {http_status}。",
            }
    except (TimeoutError, socket.timeout):
        error = {"stage": "transport", "class": "provider_timeout", "message": "模型请求超时。"}
    except urllib.error.URLError as exc:
        timeout = isinstance(exc.reason, (TimeoutError, socket.timeout))
        error = {
            "stage": "transport",
            "class": "provider_timeout" if timeout else "network_error",
            "message": str(exc.reason),
        }
    except (OSError, http.client.HTTPException) as exc:
        error = {"stage": "transport", "class": "network_error", "message": str(exc)}
    return Exchange(
        request_payload=json.loads(body),
        raw_response=raw,
        has_response=has_response,
        http_status=http_status,
        request_id=request_id,
        error=error,
        started_at=started_at,
        finished_at=utc_now(),
        elapsed_ms=round((time.perf_counter() - start) * 1000),
    )


def write_run(
    output_dir: Path, *, request: dict | None, exchange: Exchange | None,
    result: dict, config: Config | None = None,
) -> Path:
    run_id = f"run-{uuid.uuid4()}"
    directory = Path(output_dir) / run_id
    directory.mkdir(parents=True, exist_ok=False)
    secrets = (os.environ.get("DEEPSEEK_API_KEY", "").strip(),)
    if config is not None:
        secrets += (config.api_key,)

    def write(name: str, value: Any) -> None:
        (directory / name).write_text(
            json.dumps(sanitize(value, secrets), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # 记录传输边界实际使用的数据；调用者随后修改 request 不会改写证据。
    sent = exchange.request_payload if exchange is not None else None
    if sent is not None:
        write("observable-request.json", sent)
    if exchange is not None and exchange.has_response:
        write("raw-response.json", exchange.raw_response)
    raw = exchange.raw_response if exchange is not None else None
    now = utc_now()
    receipt = {
        "schema_version": "w01-1",
        "run_id": run_id,
        "evidence_source": EVIDENCE_SOURCE,
        "execution_state": "blocked" if exchange is None else "ran",
        "started_at": exchange.started_at if exchange else now,
        "finished_at": exchange.finished_at if exchange else now,
        "elapsed_ms": exchange.elapsed_ms if exchange else 0,
        "attempts": 1 if exchange else 0,
        "endpoint": ENDPOINT,
        "requested_model": sent.get("model") if sent else (config.model if config else None),
        "actual_model": raw.get("model") if isinstance(raw, dict) else None,
        "http_status": exchange.http_status if exchange else None,
        "request_id": (
            exchange.request_id or (raw.get("id") if isinstance(raw, dict) else None)
        ) if exchange else None,
        "timeout_seconds": config.timeout_seconds if config else None,
        **result,
        "task_verdict": "not_evaluated",
        "artifacts": {
            "observable_request": "observable-request.json" if sent is not None else None,
            "raw_response": "raw-response.json" if exchange and exchange.has_response else None,
        },
    }
    write("receipt.json", receipt)
    return directory / "receipt.json"


def emit_json(value: dict) -> None:
    secret = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    print(json.dumps(sanitize(value, (secret,)), ensure_ascii=False, indent=2))
