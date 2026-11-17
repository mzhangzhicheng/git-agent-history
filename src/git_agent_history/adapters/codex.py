"""OpenAI Codex CLI / IDE / app-server local history ($CODEX_HOME, default ~/.codex).

Backed up: sessions/ and archived_sessions/ rollouts (*.jsonl and *.jsonl.zst),
session_index.jsonl, history.jsonl and an online snapshot of state_*.sqlite.
Never backed up: auth.json, config.toml, logs, caches and every other file.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import sqlite3

from ..util import remap_path
from .base import Adapter, Source, walk_files

_INDEXES = {'session_index.jsonl': 'id', 'history.jsonl': 'lines'}
_DB = re.compile(r'state_[0-9]+\.sqlite')
_SESSION = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(?:\.zst)?$')


def _relocate_rollout(value: str, old_home: str, target: Path) -> str:
    """Re-root a stored rollout path at the restore target, whatever the old home looked like."""
    norm = value.replace('\\', '/')
    for marker in ('/archived_sessions/', '/sessions/'):
        index = norm.rfind(marker)
        if index >= 0:
            return str(target.joinpath(*PurePosixPath(norm[index + 1:]).parts))
    return remap_path(value, [(old_home, str(target))]) if old_home else value


class CodexAdapter(Adapter):
    name = 'codex'
    display_name = 'Codex'
    env_var = 'CODEX_HOME'

    def default_home(self) -> Path:
        return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()

    def sources(self, home: Path):
        for folder in ('sessions', 'archived_sessions'):
            for path in walk_files(home / folder):
                rel = path.relative_to(home).as_posix()
                if rel.endswith('.jsonl'):
                    yield Source(rel, path, 'jsonl')
                elif rel.endswith('.jsonl.zst'):
                    yield Source(rel, path, 'zst')
        for name in _INDEXES:
            path = home / name
            if path.is_file() and not path.is_symlink():
                yield Source(name, path, 'jsonl')

    def allowed(self, rel: PurePosixPath) -> bool:
        if rel.parts[0] in ('sessions', 'archived_sessions') and len(rel.parts) > 1:
            return rel.name.endswith('.jsonl') or rel.name.endswith('.jsonl.zst')
        return len(rel.parts) == 1 and rel.name in _INDEXES

    def index_kind(self, rel: str):
        return _INDEXES.get(rel)

    def session_id(self, rel: str):
        match = _SESSION.search(rel)
        return match.group(1) if match and not self.index_kind(rel) else None

    def is_running_process(self, exe: str, args: str) -> bool:
        exe = exe.lower()
        return exe in ('codex', 'codex.exe') or (exe.startswith('codex') and 'app-server' in args)

    def sqlite_home(self, home: Path) -> Path:
        # Codex lets config.toml `sqlite_home` override CODEX_SQLITE_HOME.
        location = Path(os.environ.get('CODEX_SQLITE_HOME') or home).expanduser()
        config = home / 'config.toml'
        if config.is_file():
            try:
                import tomllib
                settings = tomllib.loads(config.read_text(encoding='utf-8'))
                if settings.get('sqlite_home'):
                    location = Path(settings['sqlite_home']).expanduser()
            except (ImportError, ValueError, OSError):
                pass
        return location

    def databases(self, home: Path):
        location = self.sqlite_home(home)
        if not location.is_dir():
            return []
        return [p for p in sorted(location.iterdir()) if _DB.fullmatch(p.name) and p.is_file()]

    @staticmethod
    def valid_database_name(name: str) -> bool:
        return bool(_DB.fullmatch(name))

    def rewrite_database(self, path: Path, old_home: str, target: Path, mappings) -> None:
        """Point thread rollout paths at the restored home; optionally move project cwd."""
        db = sqlite3.connect(str(path))
        try:
            cols = {row[1] for row in db.execute('pragma table_info(threads)')}
            if {'id', 'rollout_path'} <= cols:
                for tid, value in db.execute('select id, rollout_path from threads').fetchall():
                    if value:
                        db.execute('update threads set rollout_path=? where id=?',
                                   (_relocate_rollout(value, old_home, target), tid))
            if mappings and {'id', 'cwd'} <= cols:
                for tid, value in db.execute('select id, cwd from threads').fetchall():
                    if value:
                        db.execute('update threads set cwd=? where id=?', (remap_path(value, mappings), tid))
            db.commit()
            if db.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise ValueError('Restored database failed integrity check')
        finally:
            db.close()
