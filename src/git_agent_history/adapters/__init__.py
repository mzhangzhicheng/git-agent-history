"""Registry of supported agents."""
from __future__ import annotations

from .base import Adapter, Source
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .deepseek_harness import DeepSeekHarnessAdapter

ADAPTERS = {a.name: a for a in (CodexAdapter(), ClaudeCodeAdapter(), DeepSeekHarnessAdapter())}


def get(name: str) -> Adapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError('Unknown agent %r; supported: %s' % (name, ', '.join(sorted(ADAPTERS))))


__all__ = ['ADAPTERS', 'Adapter', 'Source', 'get']
