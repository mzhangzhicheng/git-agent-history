"""Redact well-known credential formats before transcripts leave the machine.

The patterns cover token prefixes that providers document publicly. They are a
safety net, not a complete scanner; see README for dedicated tools.
"""
from __future__ import annotations

import re

# (kind, literal hints, pattern, needs a token boundary before the match)
# Every pattern starts with a literal so the regex engine can search for it quickly.
_PATTERNS = (
    ('private_key', (b'PRIVATE KEY-----',),
     rb'-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----.{0,20000}?-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----', False),
    ('anthropic_key', (b'sk-ant-',), rb'sk-ant-[A-Za-z0-9_-]{20,}', True),
    ('openai_key', (b'sk-',), rb'sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}', True),
    ('github_token', (b'ghp_', b'gho_', b'ghu_', b'ghs_', b'ghr_'), rb'gh[pousr]_[A-Za-z0-9]{36,}', True),
    ('github_token', (b'github_pat_',), rb'github_pat_[A-Za-z0-9_]{50,}', True),
    ('aws_access_key_id', (b'AKIA', b'ASIA'), rb'A[KS]IA[0-9A-Z]{16}(?![0-9A-Za-z])', True),
    ('google_api_key', (b'AIza',), rb'AIza[0-9A-Za-z_-]{35}', True),
    ('slack_token', (b'xox',), rb'xox[abposr]-[A-Za-z0-9-]{10,}', True),
    ('stripe_live_key', (b'k_live_',), rb'[rs]k_live_[0-9A-Za-z]{16,}', True),
    ('huggingface_token', (b'hf_',), rb'hf_[A-Za-z0-9]{30,}', True),
    ('jwt', (b'eyJ',), rb'eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}', True),
)
_COMPILED = tuple((kind, hints, re.compile(pattern, re.S), boundary)
                  for kind, hints, pattern, boundary in _PATTERNS)

MODES = ('redact', 'warn', 'off')


def _boundary_ok(data: bytes, start: int) -> bool:
    """A token must not continue a longer word, except right after a JSON escape such as \\n."""
    if start == 0:
        return True
    prev = data[start - 1:start]
    if not (prev.isalnum() or prev == b'_'):
        return True
    return start >= 2 and data[start - 2:start - 1] == b'\\' and prev in b'nrtbf'


def _process(data: bytes, replace: bool):
    counts = {}
    for kind, hints, regex, boundary in _COMPILED:
        if not any(hint in data for hint in hints):
            continue

        def handle(match, kind=kind, boundary=boundary):
            if boundary and not _boundary_ok(match.string, match.start()):
                return match.group(0)
            counts[kind] = counts.get(kind, 0) + 1
            return b'[REDACTED:' + kind.encode() + b']' if replace else match.group(0)

        if replace:
            data = regex.sub(handle, data)
        else:
            for match in regex.finditer(data):
                handle(match)
    return data, counts


def find(data: bytes) -> dict:
    """Return {kind: count} without exposing the matched values."""
    return _process(data, replace=False)[1]


def redact(data: bytes):
    """Replace secrets with [REDACTED:kind]. Output stays valid inside JSON strings."""
    return _process(data, replace=True)


def transformer(mode: str):
    """Return fn(bytes) -> (bytes_to_store, {kind: count}) for the configured mode."""
    if mode == 'redact':
        return redact
    if mode == 'warn':
        return lambda data: (data, find(data))
    if mode == 'off':
        return lambda data: (data, {})
    raise ValueError('secrets mode must be one of: ' + ', '.join(MODES))


def merge_counts(total: dict, extra: dict) -> dict:
    for key, value in extra.items():
        total[key] = total.get(key, 0) + value
    return total
