"""固定语料的安全字节快照与 SQLite 原子发布；不判断分块或来源适用性。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import time

MAX_JSON_BYTES = 2 * 1024 * 1024


class RetrievalError(Exception):
    def __init__(self, code, message, *, evidence=None):
        super().__init__(message)
        self.code = self.error_class = code
        self.evidence = evidence


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
    except (ValueError, UnicodeError, TypeError, RecursionError) as error:
        raise RetrievalError('invalid_json', str(error)) from error


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('重复 JSON 字段：' + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError('JSON 非有限常量：' + value)
    if not isinstance(data, (str, bytes)) or len(data) > MAX_JSON_BYTES:
        raise RetrievalError('invalid_json', 'JSON 输入类型或尺寸无效。')
    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        canonical(result)
        return result
    except (ValueError, UnicodeError, TypeError, RecursionError) as error:
        raise RetrievalError('invalid_json', str(error)) from error


def write_json(path, value):
    with Path(path).open('xb') as stream:
        stream.write(canonical(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def object_identity(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns, 'nlink': info.st_nlink}


@dataclass(frozen=True)
class Snapshot:
    path: str
    data: bytes
    sha256: str
    object: dict
    observed_at: float

    def metadata(self):
        return {'path': self.path, 'sha256': self.sha256, 'object': self.object,
                'observed_at': self.observed_at}


def snapshot(root, relative, *, max_bytes=8192):
    root = Path(root).absolute()
    rel = PurePosixPath(relative) if isinstance(relative, str) else None
    if (rel is None or not relative or rel.is_absolute() or '\\' in relative
            or any(part in {'', '.', '..'} for part in relative.split('/'))):
        raise RetrievalError('unsafe_path', '需要不含跳转分量的相对路径。')
    if '..' in root.parts:
        raise RetrievalError('unsafe_path', '根目录不可含 ..。')
    directory = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    target = None
    try:
        for part in (*root.parts[1:], *rel.parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        target = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(target)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RetrievalError('unsafe_source', '来源必须为普通单硬链接文件。')
        if before.st_size > max_bytes:
            raise RetrievalError('source_too_large', '来源超过字节上限。')
        content = bytearray()
        while True:
            chunk = os.read(target, min(65536, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise RetrievalError('source_too_large', '读取期间来源超过字节上限。')
        after = os.fstat(target)
        current = os.stat(rel.parts[-1], dir_fd=directory, follow_symlinks=False)
        if object_identity(before) != object_identity(after) or object_identity(after) != object_identity(current):
            raise RetrievalError('source_changed', '读取期间来源对象或字节版本变化。')
        data = bytes(content)
        return Snapshot(relative, data, hashlib.sha256(data).hexdigest(), object_identity(after), time.time())
    except OSError as error:
        raise RetrievalError('unsafe_or_missing_source', f'{relative}: {error}') from error
    finally:
        if target is not None:
            os.close(target)
        os.close(directory)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS generations(generation_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,published_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(generation_id TEXT NOT NULL,chunk_id TEXT NOT NULL,payload_json TEXT NOT NULL,vector_json TEXT NOT NULL,
 PRIMARY KEY(generation_id,chunk_id),FOREIGN KEY(generation_id) REFERENCES generations(generation_id));
CREATE TABLE IF NOT EXISTS queries(query_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,observed_at REAL NOT NULL);
'''


class IndexStore:
    def __init__(self, path, *, create=False):
        self.path = Path(path).absolute()
        if self.path.is_symlink() or self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1):
            raise RetrievalError('unsafe_index', '索引必须为普通单硬链接文件。')
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=1)
            self.connection.executescript(SCHEMA)
            self.connection.execute("INSERT OR IGNORE INTO meta VALUES ('schema','1')")
            self.connection.commit()
        else:
            if not self.path.is_file():
                raise RetrievalError('index_missing', '需要已有完整索引。')
            self.connection = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=1)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        try:
            row = self.connection.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None or row[0] != '1':
                raise RetrievalError('invalid_index', '索引 schema 不匹配。')
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def active_id(self):
        row = self.connection.execute("SELECT value FROM meta WHERE key='active_generation'").fetchone()
        return row[0] if row else None

    def publish(self, generation, chunks, vectors, *, before_commit):
        if len(chunks) != len(vectors):
            raise RetrievalError('invalid_generation', '片段与向量数量不同。')
        ident = generation['generation_id']
        try:
            self.connection.execute('BEGIN IMMEDIATE')
            self.connection.execute('INSERT INTO generations VALUES (?,?,?)',
                (ident, canonical(generation).decode(), time.time()))
            self.connection.executemany('INSERT INTO chunks VALUES (?,?,?,?)', [
                (ident, item['chunk_id'], canonical(item).decode(), canonical(vector).decode())
                for item, vector in zip(chunks, vectors, strict=True)])
            before_commit()
            self.connection.execute("INSERT INTO meta VALUES ('active_generation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ident,))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def generation(self, generation_id=None):
        ident = generation_id or self.active_id()
        row = self.connection.execute('SELECT payload_json FROM generations WHERE generation_id=?', (ident,)).fetchone()
        if row is None:
            raise RetrievalError('index_missing', '索引没有指定的完整代次。')
        items = self.connection.execute('SELECT payload_json,vector_json FROM chunks WHERE generation_id=? ORDER BY chunk_id', (ident,)).fetchall()
        return decode(row[0]), [decode(item[0]) for item in items], [decode(item[1]) for item in items]

    def save_query(self, record):
        with self.connection:
            self.connection.execute('INSERT INTO queries VALUES (?,?,?)',
                                    (record['query_id'], canonical(record).decode(), record['observed_at']))

    def inspect(self):
        return {'schema': 1, 'active_generation': self.active_id(),
                'generations': [decode(row[0]) for row in self.connection.execute('SELECT payload_json FROM generations ORDER BY published_at,generation_id')],
                'queries': [decode(row[0]) for row in self.connection.execute('SELECT payload_json FROM queries ORDER BY observed_at,query_id')]}
