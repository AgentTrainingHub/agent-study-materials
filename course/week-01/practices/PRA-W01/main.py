"""Single-call CLI. Complete build_messages before the first request."""
from __future__ import annotations

import argparse
from pathlib import Path

from w01_support import (
    ConfigurationError, build_request, emit_json, load_config, send_once, write_run,
)


def build_messages(code: str, question: str) -> list[dict]:
    raise NotImplementedError("Complete build_messages in Unit 1 before running the CLI.")


def normalize_response(raw: object) -> dict:
    usage = raw.get("usage") if isinstance(raw, dict) else None
    counts = ("prompt_tokens", "completion_tokens", "total_tokens")
    known = (
        isinstance(usage, dict)
        and all(type(usage.get(key)) is int and usage[key] >= 0 for key in counts)
        and usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    )
    result = {
        "status": "error", "text": "", "raw_stop_reason": None,
        "usage_raw": usage, "usage_status": "known" if known else "usage_unknown",
        "error": {"stage": "response", "class": "invalid_response", "message": "响应内容或结构无效。"},
    }
    if not isinstance(raw, dict):
        return result
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return result
    choice = choices[0]
    result["raw_stop_reason"] = choice.get("finish_reason")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return result
    text = message.get("content")
    if isinstance(text, str):
        result["text"] = text
    if (
        not isinstance(raw.get("model"), str) or not raw["model"].strip()
        or not isinstance(text, str) or not text.strip()
        or message.get("tool_calls") or message.get("function_call")
    ):
        return result
    if choice.get("finish_reason") == "stop":
        result.update(status="stop", error=None)
    elif choice.get("finish_reason") == "length":
        result.update(
            status="length",
            error={"stage": "generation", "class": "output_truncated", "message": "输出达到上限，回答不完整。"},
        )
    else:
        result["error"] = {
            "stage": "response", "class": "unsupported_finish_reason",
            "message": "停止原因缺失或不属于本周支持的文本响应。",
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="W01 单轮代码问答")
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--question", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=1024)
    args = parser.parse_args()
    config = None
    exchange = None
    request = None
    try:
        config = load_config(args.max_tokens)
        code = args.code.read_text(encoding="utf-8")
        question = args.question.read_text(encoding="utf-8")
        if not code.strip() or not question.strip():
            raise ConfigurationError("input_invalid", "代码和问题都必须包含非空文本。")
        request = build_request(build_messages(code, question), config)
    except (ConfigurationError, OSError, UnicodeError) as exc:
        error_class = exc.error_class if isinstance(exc, ConfigurationError) else "input_error"
        result = {
            "status": "blocked", "text": "", "raw_stop_reason": None,
            "usage_raw": None, "usage_status": "usage_unknown",
            "error": {
                "stage": "configuration" if error_class.startswith("configuration_") else "input",
                "class": error_class,
                "message": str(exc),
            },
        }
    else:
        exchange = send_once(config, request)
        if exchange.error is not None:
            result = {
                "status": "error", "text": "", "raw_stop_reason": None,
                "usage_raw": None, "usage_status": "usage_unknown", "error": exchange.error,
            }
        else:
            result = normalize_response(exchange.raw_response)
    receipt = write_run(
        args.output_dir, request=request, exchange=exchange, result=result, config=config,
    )
    emit_json({**result, "receipt_path": str(receipt.resolve())})
    return {"stop": 0, "blocked": 2}.get(result["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
