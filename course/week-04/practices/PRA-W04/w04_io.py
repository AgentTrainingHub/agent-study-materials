"""W04 的有界 JSONL 文件访问；记录含义由本地存储模块校验。"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from w02_support import ProtocolError

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024


class StorageError(ProtocolError):
    pass


def session_path(path: Path, workspace: Path | None = None) -> Path:
    """解析父目录，保留末级名称供 O_NOFOLLOW 检查。"""
    try:
        path = Path(path).expanduser().absolute()
        path = path.parent.resolve() / path.name
        if workspace is not None:
            root = Path(workspace).resolve(strict=True)
            if not root.is_dir():
                raise StorageError("workspace_invalid", "工作区必须是目录。")
            if path.is_relative_to(root):
                raise StorageError("session_in_workspace", "会话文件必须位于工具工作区之外。")
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, StorageError):
            raise
        raise StorageError("session_path_invalid", f"会话或工作区路径无效：{error}") from error
    return path


def _regular(descriptor: int) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StorageError("session_file_type", "会话和锁文件只允许普通、单硬链接文件。")
    return info


class JSONLFile:
    def __init__(self, path: Path, handle):
        self.path = path
        self._handle = handle
        self.failed = False

    def read_bytes(self) -> bytes:
        try:
            info = _regular(self._handle.fileno())
            if info.st_size > MAX_FILE_BYTES:
                raise StorageError("session_too_large", "会话超过 16 MiB 上限。")
            self._handle.seek(0)
            data = self._handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise StorageError("session_too_large", "会话超过 16 MiB 上限。")
            if any(len(line) > MAX_LINE_BYTES for line in data.splitlines(keepends=True)):
                raise StorageError("session_line_too_large", "会话单行超过 256 KiB 上限。")
            return data
        except OSError as error:
            raise StorageError("session_io", f"会话读取失败：{error}") from error

    def append_line(self, data: bytes) -> None:
        """失败后禁止继续追加；调用方必须停止本次运行。"""
        if self.failed:
            raise StorageError("storage_failed", "此前持久化失败，本次存储已停止。")
        try:
            if not isinstance(data, bytes) or not data.endswith(b"\n") or b"\n" in data[:-1]:
                raise StorageError("invalid_record_line", "追加内容必须是单行并以换行结束。")
            if len(data) > MAX_LINE_BYTES:
                raise StorageError("session_line_too_large", "会话单行超过 256 KiB 上限。")
            info = _regular(self._handle.fileno())
            if info.st_size + len(data) > MAX_FILE_BYTES:
                raise StorageError("session_too_large", "追加将超过 16 MiB 上限。")
            self._handle.seek(0, os.SEEK_END)
            written = self._handle.write(data)
            if written != len(data):
                raise OSError("JSONL short write")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as error:
            self.failed = True
            raise StorageError("session_io", f"会话追加失败：{error}") from error
        except BaseException:
            self.failed = True
            raise


@contextmanager
def open_jsonl(path: Path, *, create: bool = False, workspace: Path | None = None):
    """稳定侧锁覆盖整个 with；同一会话的合作进程不能交错写入。"""
    path = session_path(path, workspace)
    lock_fd = descriptor = None
    try:
        try:
            if create:
                path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            lock_fd = os.open(str(path) + ".lock", flags | os.O_CREAT, 0o600)
            _regular(lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StorageError("session_busy", "会话正在被另一进程使用。") from error
            descriptor = os.open(path, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
            _regular(descriptor)
        except OSError as error:
            raise StorageError("session_io", f"会话文件访问失败：{error}") from error
        with os.fdopen(descriptor, "r+b", buffering=0) as handle:
            descriptor = None
            yield JSONLFile(path, handle)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if lock_fd is not None:
            os.close(lock_fd)
