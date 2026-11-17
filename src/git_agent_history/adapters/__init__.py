"""Registry of supported agents."""
from __future__ import annotations

from .base import Adapter, Source
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

ADAPTERS = {a.name: a for a in (CodexAdapter(), ClaudeCodeAdapter())}


def get(name: str) -> Adapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError('Unknown agent %r; supported: %s' % (name, ', '.join(sorted(ADAPTERS))))


__all__ = ['ADAPTERS', 'Adapter', 'Source', 'get']
