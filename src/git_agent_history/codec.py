"""Optional Zstandard support for agents that compress old transcripts (e.g. *.jsonl.zst).

Order of preference: Python 3.14+ `compression.zstd`, the `zstandard` package, the
`zstd` command-line tool. Without any of them, .zst files are stored byte-for-byte.
"""
from __future__ import annotations

import io
import shutil
import subprocess


def _backend():
    try:
        from compression import zstd  # type: ignore[import-not-found]
        return 'stdlib', zstd
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore[import-not-found]
        return 'zstandard', zstandard
    except ImportError:
        pass
    exe = shutil.which('zstd')
    if exe:
        return 'cli', exe
    return None, None


def available() -> bool:
    return _backend()[0] is not None


def decompress(data: bytes):
    kind, mod = _backend()
    if kind == 'stdlib':
        return mod.decompress(data)
    if kind == 'zstandard':
        reader = mod.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True)
        return reader.read()
    if kind == 'cli':
        return subprocess.run([mod, '-d', '-q', '-c'], input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=True, timeout=600).stdout
    return None


def compress(data: bytes):
    kind, mod = _backend()
    if kind == 'stdlib':
        return mod.compress(data, level=3)
    if kind == 'zstandard':
        return mod.ZstdCompressor(level=3).compress(data)
    if kind == 'cli':
        return subprocess.run([mod, '-3', '-q', '-c'], input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=True, timeout=600).stdout
    return None
