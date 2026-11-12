"""W03 共用记录、传输和宿主停止信号。"""
from dataclasses import dataclass

from w02_support import (  # noqa: F401
    MAX_BATCH_CALLS, MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_PAGE_LINES,
    DeadlineExceeded, ProtocolError, ToolError, RunLog,
    aggregate_usage, call_model_once, check_deadline, make_tool_message,
    usage_from_response, validate_transcript,
)


@dataclass(frozen=True)
class ToolControl:
    blocks_run: bool = False
    effect: str = "none"
    scope: str = "workspace"
    status: str | None = None
