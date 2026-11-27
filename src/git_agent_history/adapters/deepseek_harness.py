"""DeepSeek Harness (`dsh`) sessions stored by its JSONL persistence backend.

Default location: $DSH_HOME/sessions (DSH_HOME defaults to ~/.dsh), laid out as
`sessions/<project-key>/<encoded-session-id>/session[.vN].jsonl[.zstd]`.
Compressed logs are a series of appended Zstandard frames; the first frame holds only
the header line. Lock and temporary files are skipped, as is everything outside
sessions/ (credentials, settings, caches).

Sessions kept by the optional SQLite backend, or under a custom `root`, are not found
automatically; point --dsh-home at a folder whose `sessions/` is that root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re

from ..util import remap_path
from .base import Adapter, Source, walk_files

_LOG = re.compile(r'session(?:\.v[1-9][0-9]*)?\.jsonl(\.zstd)?')
_ESCAPE = re.compile(r'~([0-9A-F]{4})')


def project_key(cwd: str) -> str:
    """Folder name DeepSeek Harness uses for a project path."""
    out, separator = [], False
    for ch in cwd:
        if ch in '/\\:':
            if not separator:
                out.append('-')
            separator = True
        else:
            out.append(ch if ch != '~' and re.fullmatch(r'[A-Za-z0-9._-]', ch) else '~%04X' % ord(ch))
            separator = False
    return '--%s--' % (''.join(out).lstrip('-') or 'root')[:251]


class DeepSeekHarnessAdapter(Adapter):
    name = 'deepseek-harness'
    display_name = 'DeepSeek Harness'
    env_var = 'DSH_HOME'
    zstd_header_frame = True

    def default_home(self) -> Path:
        value = os.environ.get('DSH_HOME', '').strip()
        return Path(value or Path.home() / '.dsh').expanduser()

    def sources(self, home: Path):
        for path in walk_files(home / 'sessions'):
            rel = PurePosixPath(path.relative_to(home).as_posix())
            if self.allowed(rel):
                yield Source(str(rel), path, 'zst' if rel.name.endswith('.zstd') else 'jsonl')

    def allowed(self, rel: PurePosixPath) -> bool:
        return len(rel.parts) == 4 and rel.parts[0] == 'sessions' and bool(_LOG.fullmatch(rel.name))

    def session_id(self, rel: str):
        parts = PurePosixPath(rel).parts
        if len(parts) != 4:
            return None
        return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), parts[2])

    def is_running_process(self, exe: str, args: str) -> bool:
        exe = exe.lower()
        return exe in ('dsh', 'dsh.exe') or '@deepseek-ai/dsh' in args

    # Best effort: the record layout has not been checked against real logs yet. A prompt is a
    # record with type or role "user" whose text is not a tool result.
    prompt_markers = (b'"user"', b'"session"')

    def prompt_file(self, rel: str) -> bool:
        return self.session_id(rel) is not None

    def prompt(self, record, state):
        if record.get('type') == 'session':
            if isinstance(record.get('cwd'), str):
                state['cwd'] = record['cwd']
            return None
        message = record.get('message') if isinstance(record.get('message'), dict) else {}
        if 'user' not in (record.get('type'), record.get('role'), message.get('role')):
            return None
        content = next((v for v in (record.get('text'), record.get('content'), message.get('content'))
                        if v is not None), None)
        texts = [content] if isinstance(content, str) else []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict):
                if block.get('type') in ('tool_result', 'tool-result'):
                    return None
                if isinstance(block.get('text'), str):
                    texts.append(block['text'])
        text = '\n\n'.join(texts).strip()
        if not text:
            return None
        when = next((record.get(k) for k in ('timestamp', 'createdAt', 'time', 'ts') if record.get(k)), None)
        return {'time': when, 'text': text, 'session': state.get('session'), 'cwd': state.get('cwd')}

    def target_paths(self, staged: Path, rels, mappings):
        """Rewrite each session header's cwd and move the session to the matching project folder."""
        result = {rel: rel for rel in rels}
        if not mappings:
            return result
        for rel in rels:
            path = staged / rel
            first, sep, rest = path.read_bytes().partition(b'\n')
            try:
                header = json.loads(first)
            except ValueError:
                continue
            cwd = header.get('cwd') if isinstance(header, dict) else None
            if not isinstance(cwd, str) or not cwd:
                continue
            new_cwd = remap_path(cwd, mappings)
            if new_cwd == cwd:
                continue
            header['cwd'] = new_cwd
            line = json.dumps(header, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            path.write_bytes(line + sep + rest)
            parts = PurePosixPath(rel).parts
            result[rel] = str(PurePosixPath(parts[0], project_key(new_cwd), *parts[2:]))
        return result
