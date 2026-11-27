"""Interface every agent adapter implements. See docs/adding-an-agent.md."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re

from ..util import running_processes


class Source:
    """A file to back up: `rel` is its POSIX path relative to the agent home."""
    __slots__ = ('rel', 'path', 'kind')

    def __init__(self, rel: str, path: Path, kind: str):
        self.rel, self.path, self.kind = rel, path, kind  # kind: 'jsonl' | 'blob' | 'zst'


class Adapter:
    name = ''           # stable identifier used in the history repository
    display_name = ''
    env_var = ''        # environment variable that overrides the default home
    zstd_header_frame = False  # re-encode .zst files with the first line in its own frame

    def default_home(self) -> Path:
        raise NotImplementedError

    def sources(self, home: Path):
        """Yield Source objects. Must never follow symlinks or leave `home`."""
        raise NotImplementedError

    def allowed(self, rel: PurePosixPath) -> bool:
        """Allow-list used when restoring paths read from a manifest."""
        raise NotImplementedError

    def index_kind(self, rel: str):
        """'lines' or 'id' for shared index files that are merged on restore, else None."""
        return None

    def is_running_process(self, exe: str, args: str) -> bool:
        return False

    def running(self) -> bool:
        return any(self.is_running_process(exe, args) for exe, args in running_processes())

    def databases(self, home: Path):
        """SQLite databases worth snapshotting (online backup API)."""
        return []

    def target_paths(self, staged: Path, rels, mappings):
        """Map stored paths to restore paths for --map-cwd. May also edit the staged files."""
        return {rel: rel for rel in rels}

    def rewrite_database(self, path: Path, old_home: str, target: Path, mappings) -> None:
        pass

    def has_transcripts(self, home: Path) -> bool:
        return any(s.kind in ('jsonl', 'zst') and self.index_kind(s.rel) is None
                   for s in self.sources(home))

    # -- prompt list (docs/prompts.md) -----------------------------------------------
    # Only lines that contain one of these byte strings are parsed; an empty tuple parses all.
    prompt_markers = ()

    def prompt_file(self, rel: str) -> bool:
        """Whether a stored file is a main conversation whose user prompts are listed."""
        return False

    def prompt(self, record: dict, state: dict):
        """The prompt the user typed in one transcript record, or None.

        Returns a dict with `text` and optionally `time`, `session`, `cwd`, `images`, `files`
        and `source`. If a file yields prompts from several sources, only those of the first
        one in `prompt_sources` are kept. `state` lives for one file and starts as
        {'session': session_id(rel)}; use it for values such as the session's cwd.
        """
        return None

    prompt_sources = ()


def strip_blocks(text: str, tags) -> str:
    """Remove <tag ...>...</tag> blocks that an agent wraps around a prompt as context."""
    for tag in tags:
        text = re.sub(r'<%s(?:\s[^>]*)?>.*?</%s>' % (re.escape(tag), re.escape(tag)), '', text, flags=re.S)
    return text.strip()


def walk_files(root: Path):
    """Yield regular files below root; symlinked files and directories are skipped."""
    if not root.is_dir() or root.is_symlink():
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file():
                yield path
