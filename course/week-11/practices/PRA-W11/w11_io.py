"""W11 固定存储、跨进程锁和发布原语；有效记忆与证据政策由本地工程实现。"""
from __future__ import annotations
from hashlib import sha256
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from uuid import uuid4

from w10_io import RetrievalError, canonical, decode, digest

SCHEMA = '''
CREATE TABLE meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL);
CREATE TABLE memories (memory_id TEXT PRIMARY KEY, key TEXT NOT NULL, version INTEGER NOT NULL,
 status TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(key,version));
CREATE TABLE commands (command_id TEXT PRIMARY KEY, parameters TEXT NOT NULL, receipt TEXT NOT NULL);
CREATE TABLE audit (sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL);
'''


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError('UTC required')
        return parsed
    except (ValueError, TypeError, AttributeError) as exc:
        raise RetrievalError('invalid_time', '时间必须是带时区 UTC。') from exc


def identity(st):
    return {'device': st.st_dev, 'inode': st.st_ino}


def safe_path(path, *, exists=True, directory=False):
    path = Path(os.path.abspath(path))
    original = Path(path)
    for part in original.parents:
        if part.is_symlink():
            raise RetrievalError('unsafe_path', '路径祖先不允许符号链接。')
    try:
        st = path.lstat()
    except FileNotFoundError:
        if exists:
            raise RetrievalError('missing_file', '所需路径不存在。')
        if not path.parent.is_dir():
            raise RetrievalError('unsafe_path', '目标父目录不存在。')
        return path
    valid = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode) and st.st_nlink == 1
    if not valid:
        raise RetrievalError('unsafe_path', '需要普通、无链接对象。')
    return path


def checked_json(path, *, max_bytes=2*1024*1024):
    path = safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        raw = os.read(fd, max_bytes + 1)
        after = os.fstat(fd)
        current = path.lstat()
        if (len(raw) > max_bytes or any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_nlink')) or identity(after) != identity(current)
                or current.st_nlink != 1):
            raise RetrievalError('file_changed', '读取对象变化或超过上限。')
        return decode(raw)
    finally:
        os.close(fd)


def database_identity(db):
    path = safe_path(db)
    return identity(path.stat())


def connect(db):
    path = safe_path(db)
    before = database_identity(path)
    for suffix in ('-wal', '-shm', '-journal'):
        if os.path.lexists(str(path)+suffix):
            safe_path(str(path)+suffix)
    connection = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True,
                                 isolation_level=None, timeout=0)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA read_uncommitted=OFF')
    if database_identity(path) != before:
        connection.close()
        raise RetrievalError('database_changed', '数据库对象在打开期间变化。')
    return connection


def metadata(connection):
    try:
        return decode(connection.execute('SELECT payload FROM meta WHERE singleton=1').fetchone()[0])
    except (sqlite3.Error, TypeError) as exc:
        raise RetrievalError('invalid_memory_database', '数据库未初始化或 schema 无效。') from exc


def save_metadata(connection, value):
    connection.execute('UPDATE meta SET payload=? WHERE singleton=1', (canonical(value).decode(),))


@contextmanager
def transaction(db):
    connection = connect(db)
    try:
        connection.execute('BEGIN IMMEDIATE')
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_database(db, workspace):
    workspace = safe_path(workspace, directory=True)
    db = safe_path(db, exists=False)
    if db.is_relative_to(workspace):
        raise RetrievalError('unsafe_path', '记忆库必须位于 workspace 外。')
    lock = Path(str(db)+'.gate')
    safe_path(lock, exists=False)
    fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except BaseException:
        # 未初始化的独占空文件保留，不能覆盖其他对象或假报初始化成功。
        raise
    connection = connect(db)
    try:
        connection.executescript(SCHEMA)
        meta = {'schema': 1, 'db_id': 'mdb_'+uuid4().hex, 'project_id': 'prj_'+uuid4().hex,
                'workspace': str(workspace), 'workspace_object': identity(workspace.stat()),
                'db_path': str(db), 'db_object': identity(db.stat()), 'gate_path': str(lock),
                'gate_object': identity(lock.stat()), 'control_revision': 0, 'effective_epoch': 0,
                'created_at': utc_now()}
        connection.execute('INSERT INTO meta VALUES(1,?)', (canonical(meta).decode(),))
    finally:
        connection.close()
    return meta


def check_database(db, workspace=None):
    db = safe_path(db)
    connection = connect(db)
    try:
        meta = metadata(connection)
    finally:
        connection.close()
    root = safe_path(workspace or meta['workspace'], directory=True)
    lock = safe_path(str(db)+'.gate')
    if (meta['db_path'] != str(db) or meta['db_object'] != identity(db.stat())
            or meta['workspace'] != str(root) or meta['workspace_object'] != identity(root.stat())
            or meta['gate_path'] != str(lock) or meta['gate_object'] != identity(lock.stat())):
        raise RetrievalError('memory_project_mismatch', '数据库、锁或 workspace 身份与初始化记录不同。')
    return meta


@contextmanager
def gate(db_path, deadline, cancelled=lambda: False):
    requested = utc_now()
    meta = check_database(db_path)
    fd = os.open(meta['gate_path'], os.O_RDWR | os.O_NOFOLLOW)
    acquired = False
    timing = {'requested_at': requested, 'gate_wait_started_at': utc_now(), 'gate_acquired_at': None}
    try:
        if identity(os.fstat(fd)) != meta['gate_object']:
            raise RetrievalError('memory_project_mismatch', '锁对象在打开期间变化。')
        while True:
            if cancelled():
                raise RetrievalError('cancelled', '等待记忆 gate 时取消。')
            if time.monotonic() >= deadline:
                raise RetrievalError('gate_busy', '记忆 gate 等待预算已用完。')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(min(.02, max(0, deadline-time.monotonic())))
        check_database(db_path)
        timing['gate_acquired_at'] = utc_now()
        yield timing
    except BaseException as exc:
        exc.memory_gate = dict(timing)
        raise
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def append_audit(connection, kind, value):
    connection.execute('INSERT INTO audit(kind,payload) VALUES(?,?)', (kind, canonical(value).decode()))


def publish_answer(path, text, *, db, binding, validator, deadline, cancelled, check):
    path = safe_path(path, exists=False)
    if not isinstance(text, str) or not text.strip():
        raise RetrievalError('invalid_answer', '答案需要非空文字。')
    raw = text.encode('utf-8')
    with gate(db, deadline, cancelled) as timing:
        validator(binding, now=utc_now())
        result = check()
        if cancelled() or time.monotonic() >= deadline:
            raise RetrievalError('cancelled', '发布前已取消或到期。')
        temporary = path.parent / ('.answer-'+uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {**timing, 'published_at': utc_now(), 'path': str(path),
                'text_sha256': sha256(raw).hexdigest(), 'byte_count': len(raw), 'validation': result}


class StateCell:
    """单行持久状态原语；计量与状态准入规则由调用者决定。"""
    def __init__(self,path,initial=None):
        self.path=safe_path(path,exists=initial is None)
        if initial is not None:
            fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
            connection=sqlite3.connect(self.path,isolation_level=None)
            try:
                connection.execute('CREATE TABLE state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
                connection.execute('INSERT INTO state VALUES(1,?)',(canonical(initial).decode(),))
            finally:connection.close()
        self.object=identity(self.path.stat())

    @contextmanager
    def transaction(self):
        safe_path(self.path)
        if identity(self.path.stat())!=self.object:raise RetrievalError('research_state_changed','持久状态文件对象变化。')
        connection=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=0)
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback();raise
        finally:connection.close()

    def read(self):
        with self.transaction() as connection:
            return decode(connection.execute('SELECT payload FROM state WHERE singleton=1').fetchone()[0])

    def write(self,value):
        with self.transaction() as connection:
            connection.execute('UPDATE state SET payload=? WHERE singleton=1',(canonical(value).decode(),))
