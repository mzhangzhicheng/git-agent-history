"""Zstandard support for agents that keep transcripts as *.jsonl.zst / *.jsonl.zstd.

Backends, in order: Python 3.14+ `compression.zstd`, the `zstandard` package, the `zstd`
command-line tool. Without any of them, compressed files are stored byte-for-byte.
"""
from __future__ import annotations

import io
import shutil
import struct
import subprocess

_MAGIC = 0xFD2FB528


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


def decompress(data: bytes) -> bytes:
    """Decompress one or more concatenated frames."""
    kind, mod = _backend()
    if kind == 'stdlib':
        return mod.decompress(data)
    if kind == 'zstandard':
        return mod.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True).read()
    if kind == 'cli':
        return subprocess.run([mod, '-d', '-q', '-c'], input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=True, timeout=600).stdout
    raise ValueError('No Zstandard backend available')


def compress(data: bytes) -> bytes:
    """One checksummed frame."""
    kind, mod = _backend()
    if kind == 'stdlib':
        return mod.compress(data, options={mod.CompressionParameter.compression_level: 3,
                                           mod.CompressionParameter.checksum_flag: 1})
    if kind == 'zstandard':
        return mod.ZstdCompressor(level=3, write_checksum=True).compress(data)
    if kind == 'cli':
        return subprocess.run([mod, '-3', '-q', '-c', '--check'], input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=True, timeout=600).stdout
    raise ValueError('No Zstandard backend available')


def complete_length(data: bytes) -> int:
    """Length of the leading run of complete frames (RFC 8878); a torn last frame is excluded."""
    pos, size = 0, len(data)
    while pos + 4 <= size:
        magic = struct.unpack_from('<I', data, pos)[0]
        if magic & 0xFFFFFFF0 == 0x184D2A50:                      # skippable frame
            if pos + 8 > size:
                break
            end = pos + 8 + struct.unpack_from('<I', data, pos + 4)[0]
            if end > size:
                break
            pos = end
            continue
        if magic != _MAGIC:
            raise ValueError('Not a Zstandard stream')
        if pos + 5 > size:
            break
        descriptor = data[pos + 4]
        single_segment = descriptor & 0x20
        fcs = descriptor >> 6
        header = (0 if single_segment else 1) + (0, 1, 2, 4)[descriptor & 3] + \
            ((1 if single_segment else 0) if fcs == 0 else 1 << fcs)
        cur = pos + 5 + header
        while cur + 3 <= size:
            block = int.from_bytes(data[cur:cur + 3], 'little')
            block_type = (block >> 1) & 3
            if block_type == 3:
                raise ValueError('Corrupt Zstandard block')
            cur += 3 + (1 if block_type == 1 else block >> 3)
            if block & 1:
                break
        else:
            break                                                  # header of a block is missing
        cur += 4 if descriptor & 0x04 else 0                      # content checksum
        if cur > size:
            break
        pos = cur
    return pos


def decompress_complete(data: bytes) -> bytes:
    """Decompress every complete frame, ignoring a frame that is still being written."""
    end = complete_length(data)
    return decompress(data[:end]) if end else b''


def encode(plain: bytes, header_frame: bool) -> bytes:
    """Re-encode plaintext JSONL; optionally put the first line in its own frame (DeepSeek Harness)."""
    if header_frame and b'\n' in plain:
        cut = plain.index(b'\n') + 1
        return compress(plain[:cut]) + (compress(plain[cut:]) if cut < len(plain) else b'')
    return compress(plain)
