"""Pass tasks and context between agents and devices through the history repository.

Every entry is an immutable Markdown file in the writer's own device folder,

    devices/<device>/handoff/<task-id>/<stamp>-<participant>.md

so devices never write the same path. A task is every entry with that task ID across all
devices, ordered by the UTC stamp in the file name. The newest entry carries the task's
title, status and whose turn it is (`to`). The newest `spec` entry is the task's current
specification; `note` entries form the log.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import uuid

from . import secrets
from .client import describe_error
from .util import chmod_private, file_lock, valid_device_id

PARTICIPANTS = ('claude-code', 'codex', 'deepseek-harness', 'human')
STATUSES = ('open', 'doing', 'blocked', 'review', 'done')
KINDS = ('spec', 'note')
FOLDER = 'handoff'
MAX_BODY = 256 * 1024
_FIELDS = ('task', 'title', 'kind', 'from', 'to', 'status', 'created_at')
_TASK_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
_ENTRY = re.compile(r'(\d{8}T\d{12}Z)-(%s)\.md' % '|'.join(map(re.escape, PARTICIPANTS)))

TEMPLATE = """## Goal

What to do and why, in two to five sentences.

## Acceptance criteria

- [ ] A result that can be checked, e.g. `pytest tests/test_api.py` passes

## Roles

- claude-code: responsibility; environment and what it can reach (e.g. MacBook, local data in ~/data)
- codex: responsibility; environment and what it can reach (e.g. cloud container, repository only, no .env)
- human: decides scope and accepts the result

## Context

- Project: repository URL, branch and folders where the work happens
- Decisions: decision, with the reason
- Constraints:
- How to run and check:

## Open questions

- [ ] Question (for human / claude-code / codex)
"""


def valid_task_id(value: str) -> str:
    if not _TASK_ID.fullmatch(value or ''):
        raise ValueError('Task ID must be 1-64 lowercase letters, digits or hyphens')
    return value


def make_task_id(title: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40].strip('-')
    day = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')
    return '-'.join(part for part in (day, slug, uuid.uuid4().hex[:4]) if part)


def _lock(client):
    return file_lock(client.runtime / 'write.lock', wait=True)


def _refresh(client) -> dict:
    """Bring in other devices' entries. A failure is reported, not raised: local data still works."""
    if not client.cfg.get('push'):
        return {'pulled': False, 'reason': 'pushing is disabled for this environment'}
    try:
        client.verify_private()
        client._integrate_remote()
        return {'pulled': True}
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return {'pulled': False, 'error': describe_error(exc)}


def _parse(text: str):
    if not text.startswith('---\n'):
        return None
    head, sep, body = text[4:].partition('\n---\n')
    if not sep:
        return None
    fields = {}
    for line in head.splitlines():
        key, sep, value = line.partition(': ')
        if sep and key in _FIELDS:
            fields[key] = value.strip()
    return fields, body.strip('\n') + '\n'


def _entries(client, task=None):
    """All valid entries, optionally of one task, oldest first, and the number skipped."""
    found, skipped = [], 0
    base = client.repo / 'devices'
    for device_dir in sorted(base.iterdir()) if base.is_dir() else []:
        root = device_dir / FOLDER
        try:
            valid_device_id(device_dir.name)
        except ValueError:
            continue
        if not root.is_dir() or root.is_symlink():
            continue
        for task_dir in sorted(root.iterdir()):
            if task_dir.is_symlink() or not task_dir.is_dir() or not _TASK_ID.fullmatch(task_dir.name):
                continue
            if task and task_dir.name != task:
                continue
            for path in sorted(task_dir.iterdir()):
                match = _ENTRY.fullmatch(path.name)
                if (not match or path.is_symlink() or not path.is_file()
                        or path.stat().st_size > MAX_BODY + 4096):
                    skipped += 1
                    continue
                parsed = _parse(path.read_text(encoding='utf-8', errors='replace'))
                fields = parsed[0] if parsed else {}
                if (fields.get('task') != task_dir.name or fields.get('kind') not in KINDS
                        or fields.get('from') != match.group(2) or fields.get('to') not in PARTICIPANTS
                        or fields.get('status') not in STATUSES or not fields.get('title')):
                    skipped += 1
                    continue
                entry = {k: fields.get(k, '') for k in _FIELDS}
                entry.update(device=device_dir.name, stamp=match.group(1), body=parsed[1],
                             file=path.relative_to(client.repo).as_posix())
                found.append(entry)
    found.sort(key=lambda e: (e['stamp'], e['device'], e['from']))
    return found, skipped


def _summary(task, entries):
    last = entries[-1]
    return {'task': task, 'title': last['title'], 'status': last['status'], 'next': last['to'],
            'updated_at': last['created_at'], 'created_at': entries[0]['created_at'],
            'created_by': entries[0]['from'], 'entries': len(entries),
            'devices': sorted({e['device'] for e in entries})}


def _write(client, refreshed, fields, body):
    """Store one entry in this device's folder, commit it, and push if enabled."""
    client.check_identity()
    raw = body.encode('utf-8')
    if len(raw) > MAX_BODY:
        raise ValueError('Handoff text is larger than 256 KiB; link to files instead of pasting them')
    data, found = secrets.transformer(client.cfg['secrets'])(raw)
    when = dt.datetime.now(dt.timezone.utc)
    fields = dict(fields, created_at=when.isoformat(timespec='seconds'))
    if '\n' in fields['title'] or '\r' in fields['title']:
        raise ValueError('The title must be a single line')
    path = (client.device_dir / FOLDER / fields['task'] /
            ('%s-%s.md' % (when.strftime('%Y%m%dT%H%M%S%fZ'), fields['from'])))
    if path.exists():
        raise ValueError('An entry with this time stamp already exists; try again')
    path.parent.mkdir(parents=True, exist_ok=True)
    header = '---\n' + ''.join('%s: %s\n' % (k, fields[k]) for k in _FIELDS) + '---\n\n'
    path.write_bytes(header.encode('utf-8') + data.strip(b'\n') + b'\n')
    chmod_private(path)
    client._write_device_info(False)
    client._commit('Handoff %s: %s -> %s (%s)' % (fields['task'], fields['from'], fields['to'], fields['status']))
    result = {'task': fields['task'], 'kind': fields['kind'], 'from': fields['from'], 'next': fields['to'],
              'status': fields['status'], 'file': path.relative_to(client.repo).as_posix(),
              'secrets_redacted': found, 'pull': refreshed}
    if client.cfg.get('push'):
        try:
            status = client.sync()
            result['sync'] = {'github': status.get('github'), 'last_push': status.get('last_push')}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            result['sync'] = {'error': describe_error(exc),
                              'note': 'committed locally; the next backup run pushes it'}
    return result


def new(client, title, sender, to, body=None, task=None):
    """Start a task. The body is its first spec; without one, a template to fill in."""
    client.require_config()
    title = (title or '').strip()
    if not title:
        raise ValueError('A task needs a title')
    with _lock(client):
        refreshed = _refresh(client)
        task = valid_task_id(task) if task else make_task_id(title)
        if _entries(client, task)[0]:
            raise ValueError('Task %s already exists; use `handoff post` to add to it' % task)
        fields = {'task': task, 'title': title, 'kind': 'spec', 'from': sender, 'to': to, 'status': 'open'}
        return _write(client, refreshed, fields, body if body and body.strip() else TEMPLATE)


def post(client, task, sender, body, to=None, status=None, spec=False):
    """Add a note (or, with spec=True, a new spec) to a task and optionally pass the turn."""
    client.require_config()
    task = valid_task_id(task)
    if not (body or '').strip():
        raise ValueError('Nothing to post: pass --message or --file')
    with _lock(client):
        refreshed = _refresh(client)
        entries = _entries(client, task)[0]
        if not entries:
            raise ValueError('Unknown task %s; see `git-agent-history handoff list --all`' % task)
        last = entries[-1]
        fields = {'task': task, 'title': last['title'], 'kind': 'spec' if spec else 'note', 'from': sender,
                  'to': to or last['to'], 'status': status or last['status']}
        return _write(client, refreshed, fields, body)


def list_tasks(client, for_agent=None, include_done=False):
    """Tasks across all devices, most recently updated first."""
    client.require_config()
    with _lock(client):
        refreshed = _refresh(client)
        entries, skipped = _entries(client)
    by_task = {}
    for entry in entries:
        by_task.setdefault(entry['task'], []).append(entry)
    tasks = [_summary(task, items) for task, items in by_task.items()]
    tasks = [t for t in tasks if (include_done or t['status'] != 'done')
             and (not for_agent or t['next'] == for_agent)]
    tasks.sort(key=lambda t: by_task[t['task']][-1]['stamp'], reverse=True)
    return {'tasks': tasks, 'skipped_entries': skipped, 'pull': refreshed}


def show(client, task):
    """A task's current spec and its full log."""
    client.require_config()
    task = valid_task_id(task)
    with _lock(client):
        refreshed = _refresh(client)
        entries, skipped = _entries(client, task)
    if not entries:
        raise ValueError('Unknown task %s; see `git-agent-history handoff list --all`' % task)
    specs = [e for e in entries if e['kind'] == 'spec']
    result = _summary(task, entries)
    result.update(spec=specs[-1]['body'], spec_by=specs[-1]['from'], spec_at=specs[-1]['created_at'],
                  log=[{k: e[k] for k in ('created_at', 'kind', 'from', 'to', 'status', 'device', 'body')}
                       for e in entries],
                  skipped_entries=skipped, pull=refreshed)
    return result


def render(result) -> str:
    """Readable Markdown for `handoff show --markdown`."""
    out = ['# %s' % result['title'], '',
           '`%s` · status **%s** · next **%s** · updated %s' % (result['task'], result['status'], result['next'],
                                                                result['updated_at']), '',
           '## Spec', '', '_%s, %s_' % (result['spec_by'], result['spec_at']), '', result['spec'].rstrip(), '',
           '## Log', '']
    for e in result['log']:
        out.append('### %s · %s → %s · %s' % (e['created_at'], e['from'], e['to'], e['status']))
        out.append('')
        out.append('_spec updated on %s_' % e['device'] if e['kind'] == 'spec' else '_on %s_' % e['device'])
        out.append('')
        if e['kind'] == 'note':
            out.extend([e['body'].rstrip(), ''])
    if not result['pull'].get('pulled'):
        out.extend(['> Not refreshed from the remote: %s' % (result['pull'].get('error') or result['pull'].get('reason')), ''])
    return '\n'.join(out)
