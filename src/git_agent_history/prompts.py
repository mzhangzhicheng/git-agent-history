"""A readable list of every prompt the user typed, built from the stored transcripts.

After each checkpoint, an environment rewrites Markdown files for its own device:

    devices/<device>/prompts/README.md     totals, months and the latest prompts
    devices/<device>/prompts/YYYY-MM.md    every prompt of one month, newest day first

`git-agent-history prompts` searches the prompts of every device. Parsed prompts are cached
in local/prompts-cache.json, so a run reads only the chunks added since the previous one.
See docs/prompts.md.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import zlib

from . import adapters, secrets
from .util import chmod_private, read_json, safe_rel, valid_device_id, write_json

CACHE_VERSION = 1
FOLDER = 'prompts'
PROJECT_URL = 'https://github.com/mzhangzhicheng/git-agent-history'
SHOWN_CHARS = 20000                  # longer prompts are cut in the Markdown files
FOLD_CHARS, FOLD_LINES = 1500, 20    # longer prompts are folded behind <details>
LATEST = 30
_DOC_NAME = re.compile(r'README\.md|[0-9]{4}-[0-9]{2}\.md')
_DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
_FRACTION = re.compile(r'\.([0-9]+)')
_FENCE = re.compile(r' {0,3}(`{3,}|~{3,})')
_INLINE = re.compile(r'([\\`*_\[\]<>#|~])')
# Claude Code wraps pasted text as <pasted_content id="..">..</pasted_content id="..">; the end may be missing.
_PASTED = re.compile(r'<pasted_content(?:\s[^>]*)?>(.*?)(?:</pasted_content(?:\s[^>]*)?>|\Z)', re.S)


# -- reading --------------------------------------------------------------------------
def _utc(value):
    """An aware UTC datetime from an ISO time stamp or a Unix time in s or ms; else None."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return dt.datetime.fromtimestamp(value / 1000 if value > 1e11 else value, dt.timezone.utc)
        if isinstance(value, str) and value.strip():
            text = re.sub(r'[Zz]$', '+00:00', value.strip())
            text = _FRACTION.sub(lambda m: '.' + (m.group(1) + '000000')[:6], text, count=1)
            when = dt.datetime.fromisoformat(text)
            return (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _record(prompt: dict) -> dict:
    """The cached form of a prompt returned by an adapter."""
    when = _utc(prompt.get('time'))
    rec = {'t': when.isoformat(timespec='seconds') if when else None,
           'text': prompt['text'] if isinstance(prompt.get('text'), str) else ''}
    for key in ('session', 'cwd', 'source'):
        if isinstance(prompt.get(key), str) and prompt[key]:
            rec[key] = prompt[key]
    if isinstance(prompt.get('images'), int) and prompt['images'] > 0:
        rec['images'] = prompt['images']
    files = [f for f in prompt.get('files') or [] if isinstance(f, str)]
    if files:
        rec['files'] = files
    return rec


def _parse(adapter, store, rel, entry, cached):
    """The cache record of one stored file. Reads only chunks added since `cached` was made."""
    if not (isinstance(cached, dict) and isinstance(cached.get('prompts'), list)):
        cached = None
    if cached and cached.get('sha256') == entry.get('sha256'):
        return cached
    chunks = entry.get('chunks') or []
    start, state, found = 0, {'session': adapter.session_id(rel)}, []
    done = cached.get('chunks', 0) if cached else 0
    if (cached and entry.get('kind') == 'jsonl' and cached.get('generation') == entry.get('generation')
            and 0 < done <= len(chunks) and cached.get('last') == chunks[done - 1].get('sha256')):
        # Checkpoints store whole lines, so the old chunks end at a line boundary.
        start, state, found = done, dict(cached.get('state') or {}), list(cached.get('prompts') or [])
    markers = adapter.prompt_markers
    for line in store.iter_lines(rel, entry, start):
        if markers and not any(m in line for m in markers):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            prompt = adapter.prompt(record, state)
            if prompt:
                found.append(_record(prompt))
    return {'sha256': entry.get('sha256'), 'generation': entry.get('generation'), 'chunks': len(chunks),
            'last': chunks[-1].get('sha256') if chunks else None, 'state': state, 'prompts': found}


def _devices(client):
    names = []
    base = client.repo / 'devices'
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        try:
            names.append(valid_device_id(folder.name))
        except ValueError:
            continue
    return names


def collect(client, devices=None, agents=None):
    """(prompts oldest first without copies, errors) for the given devices and agents (default: all)."""
    path = client.local / 'prompts-cache.json'
    try:
        cache = read_json(path, None) or {}
    except ValueError:
        cache = {}
    if cache.get('version') != CACHE_VERSION or not isinstance(cache.get('files'), dict):
        cache = {'version': CACHE_VERSION, 'files': {}}
    files, dirty, errors, result, seen = cache['files'], False, [], [], set()
    for device in devices or _devices(client):
        for name in agents or list(adapters.ADAPTERS):
            try:
                adapter, store, manifest = client.load_agent(device, name)
            except ValueError:
                continue
            prefix, used = '%s/%s/' % (device, name), set()
            for rel, entry in sorted((manifest.get('files') or {}).items()):
                key = prefix + rel
                try:
                    if not (adapter.prompt_file(rel) and adapter.allowed(safe_rel(rel))):
                        continue
                    record = _parse(adapter, store, rel, entry, files.get(key))
                except (OSError, ValueError, EOFError, zlib.error) as exc:
                    errors.append('%s: %s' % (key, type(exc).__name__))
                    record = files.get(key)                # keep what was read before, if anything
                    if not (isinstance(record, dict) and isinstance(record.get('prompts'), list)):
                        continue
                used.add(key)
                if record is not files.get(key):
                    files[key], dirty = record, True
                kinds = {p.get('source') for p in record['prompts']}
                keep = next((s for s in adapter.prompt_sources if s in kinds), None)
                fallback = (_utc(entry.get('updated_at')) or _utc(manifest.get('updated_at')))
                for p in record['prompts']:
                    if keep and p.get('source') not in (None, keep):
                        continue
                    when = p.get('t') or (fallback.isoformat(timespec='seconds') if fallback else None)
                    if not when or (prefix, when, p['text']) in seen:   # forked sessions copy prompts
                        continue
                    seen.add((prefix, when, p['text']))
                    result.append(dict(p, t=when, device=device, agent=name))
            for key in [k for k in files if k.startswith(prefix) and k not in used]:
                del files[key]
                dirty = True
    if dirty:
        write_json(path, cache)
    result.sort(key=lambda p: p['t'])
    return result, errors


# -- Markdown ---------------------------------------------------------------------------
def _local(iso, tz=None):
    when = _utc(iso)
    return when.astimezone(tz) if when else None


def _offset(tz=None) -> str:
    off = dt.datetime.now(dt.timezone.utc).astimezone(tz).utcoffset() or dt.timedelta(0)
    minutes = int(off.total_seconds() // 60)
    return 'UTC%s%02d:%02d' % ('+' if minutes >= 0 else '-', abs(minutes) // 60, abs(minutes) % 60)


def _inline(text: str, limit=None) -> str:
    """One line of plain text, safe inside Markdown."""
    text = ' '.join(text.split())
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + '…'
    return _INLINE.sub(r'\\\1', text.replace('&', '&amp;'))


def _project(cwd: str) -> str:
    return re.split(r'[\\/]', cwd.rstrip('\\/'))[-1] or cwd


def _plain(text: str) -> str:
    return _PASTED.sub(' [pasted text] ', text)


def _quote(text: str) -> str:
    """Text as a Markdown block quote. Pasted text (Claude Code's <pasted_content>) is nested."""
    out = []
    for i, part in enumerate(_PASTED.split(text)):
        part = part.strip('\n')
        if not part.strip():
            continue
        if out:
            out.append('>')
        if i % 2:
            out.extend(['> _Pasted:_', '>'] + ['> ' + line for line in _quote_lines(part)])
        else:
            out.extend(_quote_lines(part))
    return '\n'.join(out) or '> _(no text)_'


def _quote_lines(text: str):
    """Quoted lines: line breaks kept, HTML shown as text, code fences closed."""
    lines, out, fence = text.split('\n'), [], None
    for i, line in enumerate(lines):
        line = line.rstrip()
        match = _FENCE.match(line)
        if fence:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) \
                    and not line.strip()[len(match.group(1)):]:
                fence = None
            out.append('> ' + line if line else '>')
        elif match:
            fence = match.group(1)
            out.append('> ' + line)
        elif not line.strip():
            out.append('>')
        else:
            following = lines[i + 1].strip() if i + 1 < len(lines) else ''
            hard_break = following and not line.endswith('\\')   # "\" already is a line break
            out.append('> ' + html.escape(line, quote=False) + ('  ' if hard_break else ''))
    if fence:
        out.append('> ' + fence)
    return out


def _day(when) -> str:
    return '%s %s' % (when.date().isoformat(), _DAYS[when.weekday()])


def _anchor(heading: str) -> str:
    """The anchor GitHub gives a heading."""
    return re.sub(r'[^\w\- ]', '', heading.lower()).replace(' ', '-')


def _entry(p, tz=None, device_label=None) -> str:
    when = _local(p['t'], tz)
    head = ['**%s**' % when.strftime('%H:%M'), adapters.get(p['agent']).display_name]
    if device_label:
        head.append(_inline(device_label))
    if p.get('cwd'):
        head.append(_inline(_project(p['cwd'])))
    if p.get('images'):
        head.append('%d image%s' % (p['images'], 's' if p['images'] > 1 else ''))
    if p.get('files'):
        head.append('files: ' + _inline(', '.join(p['files']), 120))
    if p.get('session'):
        head.append('<sub>%s</sub>' % html.escape(p['session'][:8]))
    text, note = p['text'], ''
    if len(text) > SHOWN_CHARS:
        text = text[:SHOWN_CHARS]
        note = ('\n\n_%d more characters not shown: `git-agent-history prompts --session %s`_'
                % (len(p['text']) - SHOWN_CHARS, (p.get('session') or '')[:8]))
    body = _quote(text)
    if len(text) > FOLD_CHARS or text.count('\n') >= FOLD_LINES:
        first = next((x for x in _plain(text).split('\n') if x.strip()), '')
        summary = '%s (%d lines)' % (html.escape(' '.join(first.split())[:100], quote=False),
                                     text.count('\n') + 1)
        body = '<details><summary>%s</summary>\n\n%s\n\n</details>' % (summary, body)
    return ' · '.join(head) + '\n\n' + body + note


def _by_day(prompts, tz=None):
    """[(heading, prompts in time order)] with the newest day first."""
    days = {}
    for p in prompts:
        days.setdefault(_day(_local(p['t'], tz)), []).append(p)
    return sorted(days.items(), reverse=True)


def render_list(prompts, labels=None, tz=None) -> str:
    """Prompts grouped by day, newest day first; with `labels`, each entry names its device."""
    out = []
    for heading, items in _by_day(prompts, tz):
        out.extend(['## ' + heading, ''])
        for p in items:
            out.extend([_entry(p, tz, (labels or {}).get(p['device'], p['device']) if labels else None), ''])
    return '\n'.join(out)


def render(prompts, label, others=(), tz=None) -> dict:
    """The Markdown files of one device: {'README.md': text, 'YYYY-MM.md': text, ...}."""
    offset, title = _offset(tz), _inline(label)
    months = {}
    for p in prompts:
        months.setdefault(_local(p['t'], tz).strftime('%Y-%m'), []).append(p)
    docs = {}
    for month, items in months.items():
        docs[month + '.md'] = '\n'.join([
            '# Prompts · %s · %s' % (title, month), '',
            '[All months](README.md) · %d prompts · newest day first · times are %s' % (len(items), offset), '',
            render_list(items, tz=tz)]).rstrip('\n') + '\n'
    agents = [a for a in adapters.ADAPTERS if any(p['agent'] == a for p in prompts)]
    out = ['# Prompts · %s' % title, '',
           'Every prompt typed on this device into a coding agent, taken from the backed-up transcripts.',
           '[git-agent-history](%s) rewrites these files after each backup; do not edit them by hand.' % PROJECT_URL,
           'Times are %s.' % offset, '']
    if prompts:
        first, last = _local(prompts[0]['t'], tz), _local(prompts[-1]['t'], tz)
        sessions = {(p['agent'], p.get('session')) for p in prompts}
        out.extend(['**%d prompts** in %d sessions, %s to %s.' % (len(prompts), len(sessions),
                                                                  first.date().isoformat(), last.date().isoformat()), ''])
    else:
        out.extend(['No prompts yet.', ''])
    if others:
        out.extend(['Other devices: ' + ' · '.join('[%s](../../%s/%s/README.md)' % (_inline(name), device, FOLDER)
                                                   for device, name in others), ''])
    if months:
        names = [adapters.get(a).display_name for a in agents]
        out.extend(['## Months', '', '| Month | Prompts | %s |' % ' | '.join(names),
                    '|---|--:|%s|' % '|'.join('--:' for _ in names)])
        for month in sorted(months, reverse=True):
            items = months[month]
            out.append('| [%s](%s.md) | %d | %s |' % (month, month, len(items), ' | '.join(
                str(sum(1 for p in items if p['agent'] == a)) for a in agents)))
        out.extend(['', '## Latest', ''])
        for p in reversed(prompts[-LATEST:]):
            when = _local(p['t'], tz)
            where = (' · ' + _inline(_project(p['cwd']))) if p.get('cwd') else ''
            out.append('- [%s](%s.md#%s) · %s%s · %s' % (
                when.strftime('%Y-%m-%d %H:%M'), when.strftime('%Y-%m'), _anchor(_day(when)),
                adapters.get(p['agent']).display_name, where, _inline(_plain(p['text']), 100) or '_(no text)_'))
        out.append('')
    docs['README.md'] = '\n'.join(out)
    return docs


# -- commands ---------------------------------------------------------------------------
def _labels(client):
    labels = {}
    for device in _devices(client):
        try:
            info = read_json(client.repo / 'devices' / device / 'device.json', {}) or {}
        except ValueError:
            info = {}
        labels[device] = info.get('label') or device
    return labels


def write_docs(client) -> dict:
    """Rewrite this device's prompt files where their content changed (called after a checkpoint)."""
    prompts, errors = collect(client, [client.device])
    labels = _labels(client)
    others = [(device, labels[device]) for device in sorted(labels) if device != client.device
              and (client.repo / 'devices' / device / FOLDER / 'README.md').is_file()]
    docs = render(prompts, client.cfg.get('label') or client.device, others)
    transform = secrets.transformer(client.cfg['secrets'])
    folder = client.device_dir / FOLDER
    written, removed = [], []
    for name, text in sorted(docs.items()):
        data = transform(text.encode('utf-8'))[0]
        path = folder / name
        if path.is_file() and not path.is_symlink() and path.read_bytes() == data:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_bytes(data)
        chmod_private(tmp)
        tmp.replace(path)
        written.append(name)
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        if path.name not in docs and (_DOC_NAME.fullmatch(path.name) or path.name.endswith('.md.tmp')):
            path.unlink()
            removed.append(path.name)
    result = {'prompts': len(prompts), 'files': len(docs)}
    for key, value in (('written', written), ('removed', removed), ('errors', errors)):
        if value:
            result[key] = value
    return result


def query(client, device=None, agent=None, session=None, since=None, grep=None, limit=50, markdown=False):
    """Prompts of every device (or one), newest first, for `git-agent-history prompts`."""
    try:
        start = dt.date.fromisoformat(since) if since else None
    except ValueError:
        raise ValueError('Use --since YYYY-MM-DD')
    prompts, errors = collect(client, [valid_device_id(device)] if device else None, [agent] if agent else None)
    needle = grep.casefold() if grep else None
    found = [p for p in reversed(prompts)
             if (not start or _local(p['t']).date() >= start)
             and (not session or (p.get('session') or '').startswith(session))
             and (not needle or needle in p['text'].casefold())]
    shown = found[:limit] if limit and limit > 0 else found
    if markdown:
        labels = _labels(client)
        text = render_list(sorted(shown, key=lambda p: p['t']), labels) if shown else 'No matching prompts.\n'
        return '%s\n_%d of %d matching prompts · times are %s_\n' % (text.rstrip('\n') + '\n', len(shown),
                                                                      len(found), _offset())
    return {'prompts': [{'time': _local(p['t']).isoformat(), 'device': p['device'], 'agent': p['agent'],
                         'cwd': p.get('cwd'), 'session': p.get('session'), 'text': p['text'],
                         'images': p.get('images', 0), 'files': p.get('files', [])} for p in shown],
            'shown': len(shown), 'matching': len(found), 'errors': errors}
