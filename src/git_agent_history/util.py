"""Small standard-library helpers shared by the client."""
from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

READ_SIZE = 1024 * 1024
# GitHub rejects files above 100 MB; stay well below that for every stored file.
MAX_STORED_BYTES = 90 * 1024 * 1024
# Plaintext bytes per stored chunk. Even incompressible data stays below MAX_STORED_BYTES.
MAX_CHUNK_PLAINTEXT = 64 * 1024 * 1024


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def run(args, cwd=None, check=True, timeout=300, env=None):
    return subprocess.run([str(x) for x in args], cwd=cwd, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=timeout, env=env)


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8'))
    chmod_private(tmp)
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gzip_write(dst: Path, parts) -> None:
    """Write an iterable of byte strings to a deterministic gzip file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + '.tmp')
    with tmp.open('wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0, compresslevel=6) as out:
            for part in parts:
                out.write(part)
        raw.flush()
        os.fsync(raw.fileno())
    if tmp.stat().st_size > MAX_STORED_BYTES:
        tmp.unlink()
        raise ValueError('Compressed file would exceed 90 MiB: ' + dst.name)
    chmod_private(tmp)
    tmp.replace(dst)


def gzip_read_into(src: Path, out, digest=None) -> int:
    """Decompress src into the open binary file `out`; return plaintext size."""
    size = 0
    with gzip.open(src, 'rb') as inp:
        while True:
            data = inp.read(READ_SIZE)
            if not data:
                return size
            if digest is not None:
                digest.update(data)
            out.write(data)
            size += len(data)


def file_range(path: Path, start: int, end: int):
    """Yield the bytes of path[start:end] in blocks."""
    with path.open('rb') as f:
        f.seek(start)
        remaining = end - start
        while remaining > 0:
            data = f.read(min(READ_SIZE, remaining))
            if not data:
                raise ValueError('File shrank while it was being stored')
            remaining -= len(data)
            yield data


def valid_device_id(value: str) -> str:
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}', value or '') or value in ('.', '..'):
        raise ValueError('Device ID must be 1-80 letters, numbers, dots, underscores or hyphens')
    return value


def safe_rel(value: str) -> PurePosixPath:
    """Reject absolute paths, traversal and Windows separators in stored paths."""
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or '\0' in value:
        raise ValueError('Unsafe path in backup manifest: %r' % (value,))
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ('', '.', '..') for part in p.parts) or str(p) != value:
        raise ValueError('Unsafe path in backup manifest: %r' % (value,))
    return p


def remap_path(value: str, pairs) -> str:
    """Replace the first matching OLD prefix with NEW, respecting path boundaries."""
    for old, new in pairs:
        base = old.rstrip('/\\')
        if value == old or value == base or value.startswith(base + '/') or value.startswith(base + '\\'):
            suffix = value[len(base):].lstrip('/\\')
            if not suffix:
                return new
            sep = '\\' if ('\\' in new and '/' not in new) else '/'
            return new.rstrip('/\\') + sep + suffix.replace('\\', '/').replace('/', sep)
    return value


@contextlib.contextmanager
def file_lock(path: Path, wait=False):
    """Process-wide exclusive lock. Raises BlockingIOError/OSError if busy and not waiting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        if os.name == 'nt':
            import msvcrt
            if f.seek(0, 2) == 0:
                f.write(b'0')
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def running_processes():
    """Yield (executable basename, full command line) pairs; fail closed on errors."""
    if os.name == 'nt':
        result = run(['tasklist', '/FO', 'CSV', '/NH'])
        for line in result.stdout.splitlines():
            name = line.split('","', 1)[0].strip('"')
            yield name.lower(), name.lower()
        return
    result = run(['ps', '-axo', 'comm=,args='])
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if parts:
            yield Path(parts[0]).name, (parts[1] if len(parts) > 1 else '')
