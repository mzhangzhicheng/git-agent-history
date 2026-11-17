"""Storage of one agent's files inside devices/<device>/<agent>/ of the history repository.

Append-only JSONL transcripts are stored as a sequence of gzip chunks: each checkpoint
adds only the lines written since the previous one, so Git does not keep a new full copy
of a long session every few minutes. Anything else is stored as a single gzip blob.
See docs/design.md for the manifest format.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from . import codec, secrets
from .util import (MAX_CHUNK_PLAINTEXT, READ_SIZE, file_range, gzip_read_into, gzip_write, now,
                   read_json, safe_rel, sha256_bytes, write_json)

FORMAT = 1
MAX_BLOB_SOURCE = 512 * 1024 * 1024
_CHUNK_NAME = re.compile(r'g[0-9]{3,6}-[0-9]{5}\.gz')


class AgentStore:
    def __init__(self, root: Path, tmp_dir: Path):
        self.root = root
        self.tmp_dir = tmp_dir

    # -- manifest -------------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.root / 'manifest.json'

    def load(self) -> dict:
        return read_json(self.manifest_path, None) or {'format': FORMAT, 'files': {}, 'databases': {}}

    def save(self, manifest: dict) -> None:
        write_json(self.manifest_path, manifest)

    def _chunk_dir(self, rel: str) -> Path:
        return self.root / 'files' / (str(safe_rel(rel)) + '.chunks')

    def _blob_path(self, rel: str, entry: dict) -> Path:
        stored = entry.get('path') or ('files/' + rel + '.gz')
        return self.root / str(safe_rel(stored))

    def exists(self, rel: str, entry: dict) -> bool:
        if entry.get('kind') == 'jsonl':
            return all((self._chunk_dir(rel) / c['name']).exists() for c in entry.get('chunks', []))
        return self._blob_path(rel, entry).exists()

    # -- backup ---------------------------------------------------------------
    def backup_jsonl(self, rel: str, src: Path, entry, transform):
        """Store complete lines of src. Returns (entry, changed); entry None if nothing to store."""
        size = src.stat().st_size
        same_kind = bool(entry) and entry.get('kind') == 'jsonl'
        old_bytes = entry.get('bytes', 0) if same_kind else 0
        old_sha = entry.get('sha256') if same_kind else None
        total, pos, prefix_sha, found = hashlib.sha256(), 0, None, {}
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix='jsonl-', dir=str(self.tmp_dir))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, 'wb') as out, src.open('rb') as inp:
                remaining, pending = size, b''
                while remaining > 0:
                    data = inp.read(min(READ_SIZE, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    lines = (pending + data).split(b'\n')
                    pending = lines.pop()  # incomplete last line: wait for the writer
                    for line in lines:
                        stored, counts = transform(line + b'\n')
                        secrets.merge_counts(found, counts)
                        if old_bytes and prefix_sha is None and pos + len(stored) > old_bytes:
                            prefix_sha = 'misaligned'
                        out.write(stored)
                        total.update(stored)
                        pos += len(stored)
                        if old_bytes and prefix_sha is None and pos == old_bytes:
                            prefix_sha = total.hexdigest()
            full_sha = total.hexdigest()
            if pos == 0:
                return entry, False
            if same_kind and pos == old_bytes and full_sha == old_sha and self.exists(rel, entry):
                return entry, False
            if same_kind and old_bytes and prefix_sha == old_sha and pos > old_bytes \
                    and self.exists(rel, entry):
                generation = entry['generation']
                chunks = list(entry['chunks'])
                chunks += self._write_chunks(rel, tmp, old_bytes, pos, generation, len(chunks))
            else:
                generation = (entry.get('generation', 0) if same_kind else 0) + 1
                old_dir = self._chunk_dir(rel)
                if old_dir.exists():
                    shutil.rmtree(old_dir)
                chunks = self._write_chunks(rel, tmp, 0, pos, generation, 0)
            return {'kind': 'jsonl', 'generation': generation, 'chunks': chunks, 'bytes': pos,
                    'sha256': full_sha, 'secrets': found, 'present': True, 'updated_at': now()}, True
        finally:
            tmp.unlink(missing_ok=True)

    def _write_chunks(self, rel, tmp: Path, start: int, end: int, generation: int, index: int):
        chunks, directory = [], self._chunk_dir(rel)
        while start < end:
            stop = min(end, start + MAX_CHUNK_PLAINTEXT)
            index += 1
            name = 'g%03d-%05d.gz' % (generation, index)
            digest = hashlib.sha256()

            def parts(a=start, b=stop, d=digest):
                for block in file_range(tmp, a, b):
                    d.update(block)
                    yield block

            gzip_write(directory / name, parts())
            chunks.append({'name': name, 'bytes': stop - start, 'sha256': digest.hexdigest()})
            start = stop
        return chunks

    def backup_blob(self, rel: str, src: Path, entry, transform, zstd_file=False):
        """Store a whole file. .zst content is decompressed for scanning when possible."""
        if src.stat().st_size > MAX_BLOB_SOURCE:
            raise ValueError('File too large to back up: ' + rel)
        data = src.read_bytes()
        scan = 'done'
        found = {}
        if zstd_file:
            plain = None
            if codec.available():
                try:
                    plain = codec.decompress(data)
                except Exception:  # corrupt or still being written: keep the original bytes
                    plain = None
            if plain is None:
                scan = 'skipped'
            else:
                stored_plain, found = transform(plain)
                if stored_plain != plain:
                    data = codec.compress(stored_plain)
        else:
            data, found = transform(data)
        digest = sha256_bytes(data)
        if entry and entry.get('kind') == 'blob' and entry.get('sha256') == digest \
                and self.exists(rel, entry):
            return entry, False
        new = {'kind': 'blob', 'bytes': len(data), 'sha256': digest, 'secrets': found,
               'secret_scan': scan, 'present': True, 'updated_at': now()}
        gzip_write(self._blob_path(rel, new), [data])
        return new, True

    def backup_database(self, name: str, snapshot: Path, entry):
        data_sha = hashlib.sha256()
        with snapshot.open('rb') as f:
            for block in iter(lambda: f.read(READ_SIZE), b''):
                data_sha.update(block)
        digest = data_sha.hexdigest()
        path = 'databases/' + name + '.gz'
        if entry and entry.get('sha256') == digest and (self.root / path).exists():
            return entry, False
        gzip_write(self.root / path, file_range(snapshot, 0, snapshot.stat().st_size))
        return {'sha256': digest, 'bytes': snapshot.stat().st_size, 'path': path,
                'snapshot_at': now()}, True

    # -- restore --------------------------------------------------------------
    def materialize(self, rel: str, entry: dict, dst: Path) -> None:
        """Rebuild the stored file at dst and verify every checksum."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        whole = hashlib.sha256()
        with dst.open('wb') as out:
            if entry.get('kind') == 'jsonl':
                for chunk in entry.get('chunks', []):
                    if not _CHUNK_NAME.fullmatch(chunk.get('name', '')):
                        raise ValueError('Unsafe chunk name in manifest')
                    part = hashlib.sha256()

                    class _Tee:
                        def write(self, data, p=part):
                            p.update(data)
                            whole.update(data)
                            out.write(data)

                    gzip_read_into(self._chunk_dir(rel) / chunk['name'], _Tee())
                    if part.hexdigest() != chunk['sha256']:
                        raise ValueError('Checksum mismatch in ' + rel)
            else:
                gzip_read_into(self._blob_path(rel, entry), out, whole)
        if whole.hexdigest() != entry['sha256']:
            raise ValueError('Checksum mismatch in ' + rel)

    def materialize_database(self, name: str, entry: dict, dst: Path) -> None:
        digest = hashlib.sha256()
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open('wb') as out:
            gzip_read_into(self.root / str(safe_rel(entry.get('path') or ('databases/' + name + '.gz'))),
                           out, digest)
        if digest.hexdigest() != entry['sha256']:
            raise ValueError('Checksum mismatch in database ' + name)
