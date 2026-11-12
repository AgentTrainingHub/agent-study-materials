"""W09 供给：存储格式与冻结 Proposal 的安全读取；不判断执行许可。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType

import w03_files as files
from w08_io import SupportError, canonical, transaction, _pairs, _constant


def decode(data):
    """计划快照和冻结 diff 可超过回执服务的 16 KiB；总量仍有界。"""
    if len(data) > 1024 * 1024:
        raise SupportError("workflow_json_too_large", "workflow JSON 超过 1 MiB。")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        canonical(value)
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise SupportError("invalid_json", str(error)) from error


SCHEMA = {
    "workflow_meta": "CREATE TABLE workflow_meta(schema_version INTEGER NOT NULL)",
    "workflow_states": """CREATE TABLE workflow_states(
        state_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,anchor_id TEXT NOT NULL,
        branch_id TEXT NOT NULL,revision INTEGER NOT NULL,state_json TEXT NOT NULL,created_unix REAL NOT NULL)""",
    "workflow_tasks": """CREATE TABLE workflow_tasks(
        budget_group_id TEXT PRIMARY KEY REFERENCES budget_groups(budget_group_id),session_id TEXT NOT NULL,
        branch_id TEXT NOT NULL,origin_run_id TEXT NOT NULL,status TEXT NOT NULL,cancelled INTEGER NOT NULL,
        current_wait_id TEXT,config_json TEXT NOT NULL)""",
    "workflow_waits": """CREATE TABLE workflow_waits(
        wait_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,branch_id TEXT NOT NULL,
        budget_group_id TEXT NOT NULL REFERENCES budget_groups(budget_group_id),kind TEXT NOT NULL,
        origin_json TEXT NOT NULL,status TEXT NOT NULL,decision_json TEXT,permit_id TEXT UNIQUE,
        permit_epoch INTEGER,result_json TEXT,result_entry_id TEXT,resolution_json TEXT)""",
    "workflow_commands": """CREATE TABLE workflow_commands(
        command_id TEXT PRIMARY KEY,command_hash TEXT NOT NULL,kind TEXT NOT NULL,receipt_json TEXT NOT NULL)""",
}
COLUMNS = {
    "workflow_meta": ["schema_version"],
    "workflow_states": ["state_id", "session_id", "anchor_id", "branch_id", "revision", "state_json", "created_unix"],
    "workflow_tasks": ["budget_group_id", "session_id", "branch_id", "origin_run_id", "status", "cancelled", "current_wait_id", "config_json"],
    "workflow_waits": ["wait_id", "session_id", "branch_id", "budget_group_id", "kind", "origin_json", "status", "decision_json", "permit_id", "permit_epoch", "result_json", "result_entry_id", "resolution_json"],
    "workflow_commands": ["command_id", "command_hash", "kind", "receipt_json"],
}
SHA = re.compile(r"[0-9a-f]{64}\Z")
PROPOSAL_FIELDS = {"proposal_id", "run_id", "call_id", "tool_name", "args", "path",
                   "workspace_identity", "before_identity", "before_sha256", "after_sha256", "diff",
                   "proposal_dir", "binding", "before_exists"}


def install_schema(connection):
    """只安装或核对扩展格式；不修改 W08 元数据和原有表。"""
    with transaction(connection):
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        found = present.intersection(SCHEMA)
        if not found:
            for statement in SCHEMA.values():
                connection.execute(statement)
            connection.execute("INSERT INTO workflow_meta VALUES(1)")
        elif found != set(SCHEMA):
            raise SupportError("workflow_schema_invalid", "workflow 扩展表不完整。")
        for table, expected in COLUMNS.items():
            columns = [row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")]
            if columns != expected:
                raise SupportError("workflow_schema_invalid", "workflow 扩展表字段不匹配。")
        versions = [row[0] for row in connection.execute("SELECT schema_version FROM workflow_meta")]
        if versions != [1]:
            raise SupportError("workflow_schema_invalid", "只支持唯一 workflow_schema=1。")


def _regular_bytes(directory, name, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise SupportError("candidate_invalid", "候选供给仅允许有界、普通且单硬链接的文件。")
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        if (len(data) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise SupportError("candidate_invalid", "读取候选期间文件发生变化。")
        return data


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise SupportError("candidate_invalid", "候选路径必须是原绝对目录。")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def load_proposal(path):
    """重建原字节与原身份；调用者仍须比较 SQL 绑定并 verify_proposal。"""
    path = Path(path)
    if path.name != "proposal.json" or path.parent.parent.name != "proposals":
        raise SupportError("candidate_invalid", "候选只能读取原 proposals 目录下的 proposal.json。")
    descriptor = None
    try:
        descriptor = _directory(path.parent)
        value = decode(_regular_bytes(descriptor, "proposal.json", 512 * 1024))
        if not isinstance(value, dict) or set(value) != PROPOSAL_FIELDS:
            raise SupportError("candidate_invalid", "候选字段必须完整且没有未知字段。")
        if (value["proposal_dir"] != str(path.parent) or value["proposal_id"] != path.parent.name
                or value["run_id"] != path.parent.parent.parent.name
                or not re.fullmatch(r"proposal-[0-9a-f]{32}", value["proposal_id"])
                or not isinstance(value["call_id"], str) or not value["call_id"].strip()
                or value["tool_name"] not in {"edit", "write"} or type(value["before_exists"]) is not bool):
            raise SupportError("candidate_invalid", "候选身份、目录或原工具绑定不匹配。")
        name, args = value["tool_name"], value["args"]
        fields = {"path", "old_text", "new_text"} if name == "edit" else {"path", "content"}
        if (not isinstance(args, dict) or set(args) != fields
                or any(not isinstance(item, str) for item in args.values())
                or args["path"] != value["path"] or value["path"] not in files.ALLOWED_FILES
                or value["before_exists"] != (name == "edit")):
            raise SupportError("candidate_invalid", "候选原参数或目标字段无效。")
        for key, size in (("workspace_identity", 2), ("before_identity", 4)):
            identity = value[key]
            if key == "before_identity" and not value["before_exists"]:
                if identity is not None:
                    raise SupportError("candidate_invalid", "新建候选不能携带旧文件身份。")
            elif (not isinstance(identity, list) or len(identity) != size
                  or any(type(item) is not int or item < 0 for item in identity)):
                raise SupportError("candidate_invalid", "候选目录或文件身份格式无效。")
        before = _regular_bytes(descriptor, "before.bin", files.MAX_FILE_BYTES) if value["before_exists"] else None
        if before is None:
            try:
                os.stat("before.bin", dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SupportError("candidate_invalid", "新建候选不能带有 before.bin。")
        after = _regular_bytes(descriptor, "after.bin", files.MAX_FILE_BYTES)
        patch = _regular_bytes(descriptor, "diff.patch", 256 * 1024).decode("utf-8")
        for content in (before, after):
            if content is not None:
                files._validate_bytes(content)
        if (not patch or patch != value["diff"]
                or value["before_sha256"] != (hashlib.sha256(before).hexdigest() if before is not None else None)
                or value["after_sha256"] != hashlib.sha256(after).hexdigest()
                or not isinstance(value["binding"], str) or not SHA.fullmatch(value["binding"])):
            raise SupportError("candidate_invalid", "候选前后字节、diff 或摘要不匹配。")
        proposal = files.Proposal(value["proposal_id"], files.FileSnapshot(value["path"], before,
            tuple(value["before_identity"]) if value["before_identity"] is not None else None),
            after, name, MappingProxyType(dict(args)), path.parent, value["run_id"], value["call_id"],
            tuple(value["workspace_identity"]), patch, value["binding"])
        if files._binding(proposal) != proposal.binding:
            raise SupportError("candidate_invalid", "候选完整 binding 不匹配。")
        return proposal
    except (OSError, UnicodeError, ValueError, files.ToolError) as error:
        raise SupportError("candidate_invalid", f"冻结候选无法安全读取：{type(error).__name__}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
