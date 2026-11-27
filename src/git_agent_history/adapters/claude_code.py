"""Anthropic Claude Code local history ($CLAUDE_CONFIG_DIR, default ~/.claude).

Backed up: projects/<project>/<session>.jsonl transcripts, subagent transcripts and
metadata, text tool results saved next to a session, and history.jsonl (prompt history).
Never backed up: credentials, settings, plugins, file-history, shell snapshots, images.

Claude Code deletes transcripts older than `cleanupPeriodDays` (30 by default) at start-up;
files already in the history repository stay there and can be restored.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re

from ..util import remap_path
from .base import Adapter, Source, strip_blocks, walk_files

_BLOB_DIRS = {'subagents': ('.json',), 'tool-results': ('.txt', '.json')}
# Context that Claude Code and its IDE extensions add to a prompt; the user did not type it.
_CONTEXT_TAGS = ('ide_opened_file', 'ide_selection', 'ide_diagnostics', 'system-reminder')
# Records of type "user" that the user did not type.
_NOT_TYPED = ('[Request interrupted by user', '<task-notification>', '<local-command-stdout>',
              '<local-command-stderr>', '<local-command-caveat>', '<bash-stdout>', '<bash-stderr>')
_COMMAND = re.compile(r'<command-name>(.*?)</command-name>', re.S)
_COMMAND_ARGS = re.compile(r'<command-args>(.*?)</command-args>', re.S)


def encode_project_dir(cwd: str) -> str:
    """Claude Code names a project folder after its cwd with non-alphanumerics replaced by '-'."""
    return re.sub(r'[^A-Za-z0-9]', '-', cwd)


class ClaudeCodeAdapter(Adapter):
    name = 'claude-code'
    display_name = 'Claude Code'
    env_var = 'CLAUDE_CONFIG_DIR'

    def default_home(self) -> Path:
        return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser()

    def sources(self, home: Path):
        projects = home / 'projects'
        for path in walk_files(projects):
            rel = PurePosixPath(path.relative_to(home).as_posix())
            kind = self._kind(rel)
            if kind:
                yield Source(str(rel), path, kind)
        history = home / 'history.jsonl'
        if history.is_file() and not history.is_symlink():
            yield Source('history.jsonl', history, 'jsonl')

    @staticmethod
    def _kind(rel: PurePosixPath):
        parts = rel.parts
        if len(parts) < 3 or parts[0] != 'projects':
            return None
        if len(parts) == 3 and rel.suffix == '.jsonl':          # projects/<p>/<session>.jsonl
            return 'jsonl'
        if len(parts) == 5 and parts[3] in _BLOB_DIRS:          # projects/<p>/<session>/<dir>/<file>
            if parts[3] == 'subagents' and rel.suffix == '.jsonl':
                return 'jsonl'
            if rel.suffix in _BLOB_DIRS[parts[3]]:
                return 'blob'
        return None

    def allowed(self, rel: PurePosixPath) -> bool:
        if str(rel) == 'history.jsonl':
            return True
        return self._kind(rel) is not None

    def index_kind(self, rel: str):
        return 'lines' if rel == 'history.jsonl' else None

    def session_id(self, rel: str):
        p = PurePosixPath(rel)
        return p.stem if len(p.parts) == 3 and p.parts[0] == 'projects' and p.suffix == '.jsonl' else None

    def is_running_process(self, exe: str, args: str) -> bool:
        exe = exe.lower()
        return exe in ('claude', 'claude.exe') or '@anthropic-ai/claude-code' in args

    # A typed prompt is a "user" record without tool results. Newer versions mark it with
    # origin.kind == "human"; meta records, compaction summaries and subagents are skipped.
    # Forked and resumed sessions copy earlier prompts; the prompt list removes duplicates.
    prompt_markers = (b'"user"',)

    def prompt_file(self, rel: str) -> bool:
        return self.session_id(rel) is not None

    def prompt(self, record, state):
        if record.get('type') != 'user' or 'toolUseResult' in record:
            return None
        if any(record.get(k) for k in ('isMeta', 'isSidechain', 'isCompactSummary', 'isVisibleInTranscriptOnly')):
            return None
        origin = record.get('origin')
        if isinstance(origin, dict) and origin.get('kind') not in (None, 'human'):
            return None
        if record.get('promptSource') == 'system':
            return None
        content = record.get('message', {}).get('content') if isinstance(record.get('message'), dict) else None
        texts, images = [], 0
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                kind = block.get('type') if isinstance(block, dict) else None
                if kind == 'tool_result':
                    return None
                if kind == 'text' and isinstance(block.get('text'), str):
                    texts.append(block['text'])
                elif kind == 'image':
                    images += 1
        else:
            return None
        text = strip_blocks('\n\n'.join(texts), _CONTEXT_TAGS)
        command = _COMMAND.search(text)
        if command:                                   # a slash command such as /model
            args = _COMMAND_ARGS.search(text)
            text = (command.group(1).strip() + ' ' + (args.group(1).strip() if args else '')).strip()
        if text.startswith(_NOT_TYPED) or not (text or images):
            return None
        return {'time': record.get('timestamp'), 'text': text, 'session': state.get('session'),
                'cwd': record.get('cwd'), 'images': images}

    @staticmethod
    def _cwd_of(path: Path):
        """Read the working directory recorded in a transcript, if any."""
        try:
            with path.open('rb') as f:
                for _ in range(200):
                    line = f.readline()
                    if not line:
                        return None
                    if b'"cwd"' not in line:
                        continue
                    try:
                        value = json.loads(line).get('cwd')
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(value, str) and value:
                        return value
        except OSError:
            return None
        return None

    def target_paths(self, staged: Path, rels, mappings):
        """Move project folders according to --map-cwd OLD=NEW (transcript text is unchanged)."""
        result = {rel: rel for rel in rels}
        if not mappings:
            return result
        renamed = {}
        for rel in rels:
            p = PurePosixPath(rel)
            if len(p.parts) == 3 and p.parts[0] == 'projects' and p.parts[1] not in renamed:
                cwd = self._cwd_of(staged / rel)
                if cwd:
                    new_cwd = remap_path(cwd, mappings)
                    if new_cwd != cwd:
                        renamed[p.parts[1]] = encode_project_dir(new_cwd)
        for rel in rels:
            p = PurePosixPath(rel)
            if p.parts[0] == 'projects' and len(p.parts) > 2 and p.parts[1] in renamed:
                result[rel] = str(PurePosixPath('projects', renamed[p.parts[1]], *p.parts[2:]))
        return result
