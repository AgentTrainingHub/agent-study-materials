"""W04 多轮请求传输；沿用 W02 的受监督 worker 与 W03 工具供给。"""
import json
import subprocess
import time
from dataclasses import asdict
from w01_support import Config, Exchange, sanitize, utc_now
from w03_support import *
from w02_support import _provider_command, _worker_environment, CLEANUP_GRACE_SECONDS
from w04_protocol import validate_messages, validate_transcript


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

