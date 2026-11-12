"""W03 的文件快照、逐笔审批与提交原语。候选匹配由主循环完成。"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import select
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from uuid import uuid4

from w03_support import MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_PAGE_LINES, ToolControl, ToolError

ALLOWED_FILES = frozenset({"validator.py", "test_validator.py"})
MAX_APPROVAL_BYTES = 256


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    data: bytes | None
    identity: tuple | None


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    before: FileSnapshot
    after: bytes
    name: str
    args: Mapping[str, str]
    directory: Path
    run_id: str
    call_id: str
    workspace_identity: tuple
    diff_text: str
    binding: str


class _CommitNotApplied(ToolError):
    """仅在仍能证明目标尚未提交时使用。"""


def _path(path: str) -> str:
    if (not isinstance(path, str) or not path or "\x00" in path or "\\" in path
            or Path(path).is_absolute() or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ToolError("path_denied", "path 必须是工作区内不含 .、.. 或符号链接的相对路径。")
    if path not in ALLOWED_FILES:
        raise ToolError("path_denied", "本周仅允许 validator.py 与 test_validator.py。")
    return path


def _validate_bytes(data: bytes) -> None:
    if not isinstance(data, bytes):
        raise ToolError("invalid_arguments", "候选内容必须是 UTF-8 字节。")
    if len(data) > MAX_FILE_BYTES:
        raise ToolError("file_too_large", "文件超过 64 KiB 上限。")
    if any(len(line) > MAX_LINE_BYTES for line in data.splitlines()):
        raise ToolError("line_too_long", "单行超过 4 KiB 上限。")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolError("invalid_encoding", "文件必须是 UTF-8 文本。") from error


def _io_error(error: OSError) -> ToolError:
    if error.errno == errno.ENOENT:
        return ToolError("file_not_found", "工作区内找不到该文件。")
    if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
        return ToolError("path_denied", "路径含符号链接、不可访问目录或权限拒绝。")
    return ToolError("file_access_failed", f"文件访问失败：{error.strerror}。")


def _root(workspace: Path) -> int:
    return os.open(os.fspath(workspace), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _root_identity(descriptor: int) -> tuple:
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino


def _snapshot_at(directory: int, path: str, *, allow_missing: bool) -> FileSnapshot:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        if allow_missing:
            return FileSnapshot(path, None, None)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        initial = os.fstat(handle.fileno())
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise ToolError("file_type_denied", "只允许普通、单硬链接 UTF-8 文件。")
        if initial.st_size > MAX_FILE_BYTES:
            raise ToolError("file_too_large", "文件超过 64 KiB 上限。")
        data = handle.read(MAX_FILE_BYTES + 1)
        final = os.fstat(handle.fileno())
        if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns, initial.st_nlink) != (
                final.st_size, final.st_mtime_ns, final.st_ctime_ns, final.st_nlink):
            raise ToolError("target_changed", "读取期间目标发生变化，请重新读取。")
    _validate_bytes(data)
    identity = (final.st_dev, final.st_ino, stat.S_IMODE(final.st_mode), final.st_nlink)
    return FileSnapshot(path, data, identity)


def snapshot(workspace: Path, path: str, *, allow_missing: bool = False) -> FileSnapshot:
    """快照不跟随目标链接；父目录由练习准备阶段提供。"""
    _path(path)
    try:
        directory = _root(workspace)
        try:
            return _snapshot_at(directory, path, allow_missing=allow_missing)
        finally:
            os.close(directory)
    except OSError as error:
        raise _io_error(error) from error


def safe_read(workspace: Path, path: str, offset: int = 1, limit: int = 40) -> dict:
    if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= MAX_PAGE_LINES:
        raise ToolError("invalid_arguments", "offset 必须从 1 开始，limit 必须为 1～80 的整数。")
    current = snapshot(workspace, path)
    assert current.data is not None
    text = current.data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    # Unicode 分隔符仍属于源码内容，不能让后续引用行号偏移。
    lines = text.split("\n") if text else []
    if lines and lines[-1] == "":
        lines.pop()
    if offset > max(1, len(lines)):
        raise ToolError("offset_out_of_range", "offset 超过文件的最后一行。")
    end = min(len(lines), offset - 1 + limit)
    truncated = end < len(lines)
    return {"ok": True, "path": path, "offset": offset, "limit": limit,
            "lines": [{"line": index + 1, "text": lines[index]} for index in range(offset - 1, end)],
            "total_lines": len(lines), "truncated": truncated,
            "next_offset": end + 1 if truncated else None}


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _bound_fields(proposal: Proposal) -> dict:
    return {"proposal_id": proposal.proposal_id, "run_id": proposal.run_id, "call_id": proposal.call_id,
            "tool_name": proposal.name, "args": dict(proposal.args), "path": proposal.before.path,
            "workspace_identity": proposal.workspace_identity, "before_identity": proposal.before.identity,
            "before_sha256": _digest(proposal.before.data), "after_sha256": _digest(proposal.after),
            "diff": proposal.diff_text, "proposal_dir": str(proposal.directory)}


def _binding(proposal: Proposal) -> str:
    encoded = json.dumps(_bound_fields(proposal), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _save_once(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o444)


def prepare_mutation(workspace: Path, call_id: str, name: str, args: dict, before: FileSnapshot,
                     after: bytes, diff_text: str, log, turn: int) -> Proposal:
    """冻结调用方已生成的候选；不实现 old_text 查找或 diff 算法。"""
    from dataclasses import replace

    if name not in {"edit", "write"} or not isinstance(args, dict):
        raise ToolError("invalid_arguments", "变更工具必须是 edit 或 write。")
    expected_keys = {"path", "old_text", "new_text"} if name == "edit" else {"path", "content"}
    if set(args) != expected_keys or any(not isinstance(value, str) for value in args.values()):
        raise ToolError("invalid_arguments", "变更参数的字段和类型不符合工具契约。")
    _path(args["path"])
    if before.path != args["path"] or not isinstance(diff_text, str) or not diff_text:
        raise ToolError("invalid_arguments", "候选需要同一目标的快照和完整 diff。")
    if name == "write" and before.data is not None:
        raise ToolError("target_exists", "write 仅用于新建文件，已有目标保持原样。")
    if name == "edit" and before.data is None:
        raise ToolError("file_not_found", "edit 需要已存在的目标。")
    _validate_bytes(after)
    if before.data is not None:
        _validate_bytes(before.data)
    try:
        descriptor = _root(workspace)
        try:
            identity = _root_identity(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise _io_error(error) from error
    proposal_id = "proposal-" + uuid4().hex
    directory = Path(log.directory) / "proposals" / proposal_id
    directory.mkdir(parents=True, exist_ok=False)
    proposal = Proposal(proposal_id, before, after, name, MappingProxyType(dict(args)), directory,
                        log.run_id, call_id, identity, diff_text, "")
    proposal = replace(proposal, binding=_binding(proposal))
    _save_once(directory / "after.bin", after)
    if before.data is not None:
        _save_once(directory / "before.bin", before.data)
    _save_once(directory / "diff.patch", diff_text.encode("utf-8"))
    _save_once(directory / "proposal.json", (json.dumps(
        {**_bound_fields(proposal), "binding": proposal.binding, "before_exists": before.data is not None},
        ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    log.event("mutation_proposed", turn=turn, proposal_id=proposal_id, call_id=call_id,
              tool_name=name, path=before.path, proposal_dir=str(directory))
    return proposal


def verify_proposal(workspace: Path, proposal: Proposal) -> FileSnapshot:
    """批准绑定检查：对象与完整字节均须和展示的候选一致。"""
    if _binding(proposal) != proposal.binding:
        raise _CommitNotApplied("approval_invalid", "提案内容已改变，原批准不能使用。")
    try:
        _path(proposal.before.path)
        descriptor = _root(workspace)
        try:
            if _root_identity(descriptor) != proposal.workspace_identity:
                raise _CommitNotApplied("target_changed", "练习目录对象已改变。")
            current = _snapshot_at(descriptor, proposal.before.path, allow_missing=True)
        finally:
            os.close(descriptor)
    except (OSError, ToolError) as error:
        if isinstance(error, _CommitNotApplied):
            raise
        raise _CommitNotApplied("target_changed", f"批准目标无法按原对象核对：{error}") from error
    if current != proposal.before:
        raise _CommitNotApplied("target_changed", "目标对象或字节已改变，旧提案失效；外部内容保留。")
    return current


def _check_running(deadline: float, cancelled: Callable[[], bool], *, waiting: bool = False) -> None:
    if cancelled():
        raise _CommitNotApplied("cancelled", "已请求取消，未启动下一次文件提交。")
    if time.monotonic() >= deadline:
        raise _CommitNotApplied("approval_expired" if waiting else "deadline_exceeded",
                                "等待批准超时。" if waiting else "任务时间预算已用尽。")


def commit_proposal(workspace: Path, proposal: Proposal, *, deadline: float,
                    cancelled: Callable[[], bool],
                    on_commit_start: Callable[[], None] | None = None) -> FileSnapshot:
    """只执行已获批准的候选。调用者承担审批；异常后不能默认无效果。"""
    _check_running(deadline, cancelled)
    verify_proposal(workspace, proposal)
    directory = _root(workspace)
    temporary = ".w03-" + uuid4().hex
    temp_created = False
    try:
        if _root_identity(directory) != proposal.workspace_identity:
            raise _CommitNotApplied("target_changed", "练习目录对象已改变。")
        mode = proposal.before.identity[2] if proposal.before.identity else 0o644
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode, dir_fd=directory)
        temp_created = True
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            _check_running(deadline, cancelled)
            handle.write(proposal.after)
            handle.flush()
            os.fsync(handle.fileno())
        _check_running(deadline, cancelled)
        verify_proposal(workspace, proposal)
        _check_running(deadline, cancelled)
        # 专用练习目录内的最后复核不承诺抵抗恶意并发写入的原子比较交换。
        if on_commit_start is not None:
            on_commit_start()
        if proposal.before.data is None:
            try:
                os.link(temporary, proposal.before.path, src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError as error:
                raise _CommitNotApplied("target_changed", "批准后目标已出现，独占新建拒绝覆盖。") from error
        else:
            os.replace(temporary, proposal.before.path, src_dir_fd=directory, dst_dir_fd=directory)
            temp_created = False
        if temp_created:
            os.unlink(temporary, dir_fd=directory)
            temp_created = False
        os.fsync(directory)
        observed = _snapshot_at(directory, proposal.before.path, allow_missing=False)
        if observed.data != proposal.after:
            raise RuntimeError("提交后回读与批准候选不同，效果需要独立核对。")
        return observed
    finally:
        try:
            if temp_created:
                os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)


def _read_decision(proposal: Proposal, deadline: float, cancelled: Callable[[], bool]) -> str:
    """逐字节读取一行，避免预读下一笔审批；管道和终端都受同一期限约束。"""
    try:
        descriptor = sys.stdin.fileno()
    except (OSError, ValueError) as error:
        raise _CommitNotApplied("approval_invalid", "审批输入没有可等待的文件描述符。") from error
    line = bytearray()
    while True:
        _check_running(deadline, cancelled, waiting=True)
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([descriptor], [], [], min(0.05, remaining))
        if not ready:
            continue
        chunk = os.read(descriptor, 1)
        if not chunk:
            raise _CommitNotApplied("approval_invalid", "审批输入已结束，本次提案未批准。")
        line.extend(chunk)
        if len(line) > MAX_APPROVAL_BYTES:
            raise _CommitNotApplied("approval_invalid", "审批输入超过长度上限，本次提案未批准。")
        if chunk == b"\n":
            break
    _check_running(deadline, cancelled, waiting=True)
    try:
        text = bytes(line).decode("utf-8").removesuffix("\n").removesuffix("\r")
    except UnicodeDecodeError as error:
        raise _CommitNotApplied("approval_invalid", "审批输入不是 UTF-8 文本。") from error
    if text == "approve " + proposal.proposal_id:
        return "approve"
    if text == "deny " + proposal.proposal_id:
        return "deny"
    raise _CommitNotApplied("approval_invalid", "审批格式或 proposal_id 不匹配，本次运行停止。")


def _failure(proposal: Proposal, error_class: str, message: str, *, effect: str = "none") -> dict:
    return {"ok": False, "path": proposal.before.path, "proposal_id": proposal.proposal_id,
            "error": {"class": error_class, "message": message},
            "control": asdict(ToolControl(blocks_run=True, effect=effect, status=error_class))}


def approve_and_commit(workspace: Path, proposal: Proposal, *, deadline: float,
                       cancelled: Callable[[], bool], log, turn: int) -> dict:
    """显示同一候选、读取当前 ID 的决定、最终复核，再提交并保存观察。"""
    entering_commit = False
    commit_attempted = False
    confirmed = False

    def commit_start() -> None:
        nonlocal commit_attempted
        log.event("commit_started", turn=turn, proposal_id=proposal.proposal_id,
                  call_id=proposal.call_id, path=proposal.before.path)
        commit_attempted = True

    try:
        _check_running(deadline, cancelled, waiting=True)
        if _binding(proposal) != proposal.binding:
            raise _CommitNotApplied("approval_invalid", "提案内容已改变，原预览不能使用。")
        # 独占创建收据阻止重放同一提案；无跨重启审批恢复功能。
        with (proposal.directory / "approval-requested.json").open("x", encoding="utf-8") as handle:
            json.dump({"proposal_id": proposal.proposal_id, "binding": proposal.binding}, handle)
            handle.write("\n")
        log.event("approval_requested", turn=turn, proposal_id=proposal.proposal_id,
                  call_id=proposal.call_id, path=proposal.before.path)
        print(f"\n{proposal.name} {proposal.before.path}\n{proposal.diff_text}", file=sys.stderr, flush=True)
        print(f"输入 approve {proposal.proposal_id} 或 deny {proposal.proposal_id}",
              file=sys.stderr, flush=True)
        marker = {"proposal_id": proposal.proposal_id, "path": proposal.before.path,
                  "tool_name": proposal.name, "proposal_dir": str(proposal.directory)}
        print("W03_APPROVAL " + json.dumps(marker, ensure_ascii=False), file=sys.stderr, flush=True)
        decision = _read_decision(proposal, deadline, cancelled)
        with (proposal.directory / "decision.json").open("x", encoding="utf-8") as handle:
            json.dump({"proposal_id": proposal.proposal_id, "decision": decision,
                       "input": decision + " " + proposal.proposal_id,
                       "binding": proposal.binding}, handle, ensure_ascii=False)
            handle.write("\n")
        log.event("approval_decided", turn=turn, proposal_id=proposal.proposal_id, decision=decision)
        if decision == "deny":
            raise _CommitNotApplied("approval_denied", "本次修改已拒绝，运行停止。")
        _check_running(deadline, cancelled)
        verify_proposal(workspace, proposal)
        entering_commit = True
        observed = commit_proposal(workspace, proposal, deadline=deadline, cancelled=cancelled,
                                   on_commit_start=commit_start)
        confirmed = True
        log.event("commit_finished", turn=turn, proposal_id=proposal.proposal_id,
                  call_id=proposal.call_id, path=observed.path, bytes_written=len(proposal.after),
                  after_sha256=_digest(observed.data), identity=observed.identity)
        _check_running(deadline, cancelled)
        return {"ok": True, "path": observed.path, "proposal_id": proposal.proposal_id,
                "bytes_written": len(proposal.after), "diff": proposal.diff_text,
                "commit_attempted": commit_attempted,
                "control": asdict(ToolControl(effect="applied"))}
    except _CommitNotApplied as error:
        result = _failure(proposal, error.error_class, str(error), effect="applied" if confirmed else "none")
    except FileExistsError as error:
        result = _failure(proposal, "effect_unknown" if entering_commit else "approval_invalid",
                          f"{type(error).__name__}: {error}", effect="unknown" if entering_commit else "none")
    except Exception as error:
        result = _failure(proposal, "effect_unknown" if entering_commit else "approval_invalid",
                          f"{type(error).__name__}: {error}", effect="unknown" if entering_commit else "none")
    result["commit_attempted"] = commit_attempted
    log.event("file_mutation_blocked", turn=turn, proposal_id=proposal.proposal_id,
              call_id=proposal.call_id, path=proposal.before.path, result=result)
    return result
