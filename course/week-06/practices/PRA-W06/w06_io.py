"""W06 资源字节供给：固定目录、无链接读取与有限 frontmatter。"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import stat

INSTRUCTION_NAMES = ("AGENTS.override.md", "AGENTS.md")
SKILL_ROOT = ".my-pi/skills"
INSTRUCTION_MAX_BYTES = 8 * 1024
SKILL_MAX_BYTES = 16 * 1024
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class ResourceIOError(ValueError):
    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class


def _fail(code: str, message: str):
    raise ResourceIOError(code, message)


def _parts(relative: str) -> list[str]:
    if (not isinstance(relative, str) or not relative or "\x00" in relative
            or "\\" in relative or Path(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        _fail("resource_path_denied", "资源路径必须是无空段、点段或反斜线的相对路径。")
    return relative.split("/")


def valid_skill_name(name: str) -> bool:
    return isinstance(name, str) and len(name) <= 64 and NAME.fullmatch(name) is not None


def _file_parts(relative: str) -> tuple[list[str], int]:
    parts = _parts(relative)
    if len(parts) == 1 and parts[0] in INSTRUCTION_NAMES:
        return parts, INSTRUCTION_MAX_BYTES
    if (len(parts) in {4, 5} and parts[:2] == [".my-pi", "skills"]
            and valid_skill_name(parts[2])):
        if len(parts) == 4 and parts[3] == "SKILL.md":
            return parts, SKILL_MAX_BYTES
        if len(parts) == 5 and parts[3] == "references":
            return parts, SKILL_MAX_BYTES
    _fail("resource_path_denied", "仅允许项目说明、SKILL.md 和 references 下的一层文件。")


def _directory_parts(relative: str) -> list[str]:
    parts = _parts(relative)
    if parts == [".my-pi", "skills"]:
        return parts
    if (len(parts) == 4 and parts[:2] == [".my-pi", "skills"]
            and valid_skill_name(parts[2]) and parts[3] == "references"):
        return parts
    _fail("resource_path_denied", "目录枚举仅用于 Skills 目录或某个 Skill 的 references。")


def _io_error(error: OSError) -> ResourceIOError:
    if error.errno == errno.ENOENT:
        return ResourceIOError("resource_missing", "资源不存在。")
    if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
        return ResourceIOError("resource_path_denied", "资源路径含链接、非目录或不可访问分量。")
    return ResourceIOError("resource_io_error", f"资源访问失败：{error.strerror}。")


@contextmanager
def _directory(workspace: Path, parts: list[str]):
    descriptor = None
    try:
        try:
            descriptor = os.open(os.fspath(workspace), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise ResourceIOError("resource_workspace", "资源工作区必须是可访问的非链接目录。") from error
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as error:
        raise _io_error(error) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def list_resource_names(workspace: Path, relative: str) -> list[str]:
    """只返回目录项名称；发现策略与数量限制由调用方实现。"""
    with _directory(workspace, _directory_parts(relative)) as descriptor:
        return sorted(os.listdir(descriptor))


def read_resource_bytes(workspace: Path, relative: str) -> bytes:
    """读取固定范围内的普通单硬链接 UTF-8 文件；不执行正文。"""
    parts, maximum = _file_parts(relative)
    with _directory(workspace, parts[:-1]) as directory:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                _fail("resource_type_denied", "资源必须是普通单硬链接文件。")
            if before.st_size > maximum:
                _fail("resource_too_large", f"资源超过 {maximum} 字节上限。")
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            if any(getattr(before, key) != getattr(after, key) for key in fields):
                _fail("resource_changed", "读取期间资源发生变化。")
    if len(data) > maximum:
        _fail("resource_too_large", f"资源超过 {maximum} 字节上限。")
    try:
        data.decode("utf-8")
    except UnicodeError as error:
        raise ResourceIOError("resource_encoding", "资源必须是 UTF-8 文本。") from error
    return data


def _scalar(raw: str) -> str:
    if not raw or raw != raw.strip() or "\t" in raw:
        _fail("invalid_frontmatter", "元信息值须为单行字符串。")
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError) as error:
            raise ResourceIOError("invalid_frontmatter", "双引号字符串使用 JSON 兼容转义。") from error
        if not isinstance(value, str):
            _fail("invalid_frontmatter", "元信息值须为字符串。")
    elif raw.startswith("'"):
        if not raw.endswith("'") or len(raw) < 2:
            _fail("invalid_frontmatter", "单引号字符串没有闭合。")
        inner = raw[1:-1]
        if "'" in inner.replace("''", ""):
            _fail("invalid_frontmatter", "单引号内部须用两个单引号转义。")
        value = inner.replace("''", "'")
    else:
        if (raw[0] in "-?:,[]{}#&*!|>'\"%@`" or ": " in raw or " #" in raw
                or raw.casefold() in {"true", "false", "null", "~", "yes", "no", "on", "off"}
                or re.fullmatch(r"[-+.]?[0-9][0-9.eE_+-]*", raw)):
            _fail("invalid_frontmatter", "此 frontmatter 子集不支持该 YAML 值，请使用引号字符串。")
        value = raw
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ResourceIOError("invalid_frontmatter", "字符串含无效 Unicode。") from error
    if not value.strip() or any(char in value for char in "\r\n\t\x00"):
        _fail("invalid_frontmatter", "元信息值须为非空单行字符串。")
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """仅支持 name/description 两个标量；拒绝完整 YAML 的其他结构。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0] != "---":
        _fail("invalid_frontmatter", "SKILL.md 必须以独立的 --- 行开始。")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ResourceIOError("invalid_frontmatter", "frontmatter 缺少结束行。") from error
    metadata = {}
    for line in lines[1:end]:
        if not line or ": " not in line:
            _fail("invalid_frontmatter", "只支持 name: 字符串与 description: 字符串。")
        key, value = line.split(": ", 1)
        if key not in {"name", "description"} or key in metadata:
            _fail("invalid_frontmatter", "元信息含未知或重复字段。")
        metadata[key] = _scalar(value)
    if set(metadata) != {"name", "description"}:
        _fail("invalid_frontmatter", "name 和 description 均必填。")
    if not valid_skill_name(metadata["name"]) or len(metadata["description"]) > 1024:
        _fail("invalid_frontmatter", "Skill 名称或描述不符合长度与字符限制。")
    return metadata, "\n".join(lines[end + 1:])
