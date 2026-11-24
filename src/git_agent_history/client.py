"""One environment's view of the history repository: configuration, checkpoints, sync, restore."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid

from . import __version__, adapters, codec, secrets
from .store import FORMAT, AgentStore
from .util import (chmod_private, now, read_json, run, safe_rel, valid_device_id, write_json)

CONFIG_VERSION = 1
MARKER = 'git-agent-history.json'
PROJECT_URL = 'https://github.com/mzhangzhicheng/git-agent-history'
DEFAULTS = {'interval': 300, 'db_interval': 21600, 'secrets': 'redact', 'push': True}
# A first backup or a new machine can move hundreds of MB, so network commands get no short
# wall-clock limit. Git aborts a transfer that stalls below 1 KiB/s for 5 minutes instead.
# HTTP/1.1 avoids "Error in the HTTP2 framing layer" on large pushes to GitHub.
NETWORK_GIT_CONFIG = ('-c', 'http.version=HTTP/1.1', '-c', 'http.lowSpeedLimit=1024', '-c', 'http.lowSpeedTime=300')
NETWORK_TIMEOUT = 6 * 3600  # last resort, e.g. for an SSH remote that hangs
_GITHUB = re.compile(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)'
                     r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?')

VAULT_README = """# Private coding-agent history

This repository is written by [git-agent-history]({url}). It holds raw conversation
transcripts from coding agents, one folder per device under `devices/`.

Keep this repository **private**. Transcripts can contain source code, file contents,
command output and anything pasted into a conversation.

Each environment commits only to its own `devices/<device-id>/` folder. Do not edit
these files by hand; use `git-agent-history devices`, `sessions` and `restore`.
""".format(url=PROJECT_URL)


def default_state_dir() -> Path:
    return Path(os.environ.get('GIT_AGENT_HISTORY_HOME') or Path.home() / '.git-agent-history')


def parse_remote(value: str):
    """Return (clone URL, 'owner/name' if hosted on github.com else None)."""
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        return 'https://github.com/%s.git' % value, value
    match = _GITHUB.fullmatch(value)
    return value, (match.group(1) if match else None)


def describe_error(exc: BaseException) -> str:
    """Error text that never includes subprocess output (it may contain credentials)."""
    if os.environ.get('GIT_AGENT_HISTORY_DEBUG'):
        detail = getattr(exc, 'stderr', '') or ''
        return '%s: %s %s' % (type(exc).__name__, exc, detail)
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = [str(c) for c in exc.cmd]
        if '-C' in cmd:
            i = cmd.index('-C')
            del cmd[i:i + 2]
        name = ' '.join([Path(cmd[0]).name] + cmd[1:2]) if cmd else 'command'
        return '%s failed (exit %s); check login, network and permissions' % (name, exc.returncode)
    return type(exc).__name__ + ': check login, network and permissions'


class Client:
    def __init__(self, state_dir=None):
        self.state = Path(state_dir or default_state_dir()).expanduser().resolve()
        self.config_path = self.state / 'config.json'
        self.repo = self.state / 'repo'
        self.local = self.state / 'local'
        self.status_file = self.local / 'status.json'
        self.cfg = read_json(self.config_path)
        key = hashlib.sha256(str(self.state).encode()).hexdigest()[:16]
        # Locks and SQLite snapshots stay on local disk even if the state dir is on a NAS.
        self.runtime = Path(tempfile.gettempdir()) / ('git-agent-history-' + key)
        self.runtime.mkdir(parents=True, exist_ok=True)

    # -- basics -----------------------------------------------------------------
    def git(self, *args, **kwargs):
        return run(['git', '-C', self.repo, *args], **kwargs)

    def net_git(self, *args, **kwargs):
        """A git command in the history repository that talks to the remote (ls-remote, fetch, push)."""
        return run(['git', *NETWORK_GIT_CONFIG, '-C', self.repo, *args], timeout=NETWORK_TIMEOUT, **kwargs)

    def require_config(self):
        if not self.cfg:
            raise ValueError('Not initialised; run `git-agent-history init --repo OWNER/NAME` first')
        self.device = valid_device_id(self.cfg['device'])
        self.device_dir = self.repo / 'devices' / self.device
        return self.cfg

    def agent_home(self, name: str) -> Path:
        if self.cfg and name in self.cfg.get('agents', {}):
            return Path(self.cfg['agents'][name]['home'])
        return adapters.get(name).default_home().resolve()

    def write_launcher(self) -> Path:
        """A tiny script services can run, whether installed with pip or run from a checkout."""
        package_parent = Path(__file__).resolve().parent.parent
        path = self.state / 'launch.py'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('import sys\nsys.path.insert(0, %r)\nfrom git_agent_history.cli import main\nmain()\n'
                        % str(package_parent), encoding='utf-8')
        chmod_private(path)
        return path

    def update_status(self, **values):
        status = read_json(self.status_file, {})
        status.update(values)
        write_json(self.status_file, status)
        return status

    def record_error(self, exc: BaseException):
        return self.update_status(time=now(), error=type(exc).__name__, message=describe_error(exc),
                                  last_error=now())

    # -- init -------------------------------------------------------------------
    def init(self, repo=None, device=None, label=None, agents=None, homes=None, secrets_mode='redact',
             offline=False, create=False, trust_remote=False):
        if self.cfg:
            if device and device != self.cfg['device']:
                raise ValueError('This state directory is already registered as %s; use another '
                                 '--state-dir for a second environment' % self.cfg['device'])
            self.require_config()
            self.write_launcher()
            return self.cfg
        if not repo:
            raise ValueError('Pass --repo OWNER/NAME (a private GitHub repository) or a git URL')
        url, github_repo = parse_remote(repo)
        if not github_repo and not trust_remote:
            raise ValueError('Privacy of non-GitHub remotes cannot be checked automatically; '
                             're-run with --trust-remote if you are sure it is private')
        secrets.transformer(secrets_mode)
        if create:
            if not github_repo:
                raise ValueError('--create only works with GitHub repositories')
            if run(['gh', 'repo', 'view', github_repo], check=False).returncode != 0:
                run(['gh', 'repo', 'create', github_repo, '--private',
                     '--description', 'Private coding-agent conversation history (git-agent-history)'])
        if github_repo and not offline:
            self.check_github_private(github_repo)

        agents_cfg = self._agents_config(agents, homes or {})
        self.state.mkdir(parents=True, exist_ok=True)
        try:
            self.state.chmod(0o700)
        except OSError:
            pass
        if not (self.repo / '.git').exists():
            if offline:
                run(['git', 'init', '-q', self.repo])
                self.git('remote', 'add', 'origin', url)
            else:
                run(['git', *NETWORK_GIT_CONFIG, 'clone', '-q', url, self.repo], timeout=NETWORK_TIMEOUT)
        branch = 'main'
        head = self.git('symbolic-ref', '--short', 'refs/remotes/origin/HEAD', check=False).stdout.strip()
        if head.startswith('origin/'):
            branch = head[len('origin/'):]
        if self.git('rev-parse', '--verify', '--quiet', 'HEAD', check=False).returncode != 0:
            self.git('symbolic-ref', 'HEAD', 'refs/heads/' + branch)

        device = valid_device_id(device or (re.sub(r'[^a-zA-Z0-9._-]', '-', socket.gethostname())[:45].strip('.-')
                                            or 'device') + '-' + uuid.uuid4().hex[:8])
        if (self.repo / 'devices' / device).exists():
            raise ValueError('Device ID %s already exists in the history repository; choose another '
                             '(use `restore --from-device %s` to import its history)' % (device, device))
        self.cfg = dict(DEFAULTS, version=CONFIG_VERSION, device=device, client_id=uuid.uuid4().hex,
                        label=label or device, remote=url, github_repo=github_repo,
                        privacy='github' if github_repo else 'trusted', branch=branch,
                        agents=agents_cfg, secrets=secrets_mode, push=not offline, created_at=now())
        write_json(self.config_path, self.cfg)
        self.require_config()
        for key, value in (('user.name', 'git-agent-history'), ('user.email', 'git-agent-history@localhost'),
                           ('core.autocrlf', 'false'), ('pack.threads', '2'), ('commit.gpgsign', 'false')):
            self.git('config', key, value)
        self.write_launcher()
        return self.cfg

    def _agents_config(self, names, homes):
        explicit = bool(names)
        names = names or sorted(adapters.ADAPTERS)
        result = {}
        for name in names:
            adapter = adapters.get(name)
            home = Path(homes.get(name) or adapter.default_home()).expanduser().resolve()
            if home.is_dir() or explicit:
                result[name] = {'home': str(home)}
        if not result:
            raise ValueError('No supported agent history found (looked for %s). Pass --agents and the '
                             'matching --*-home option.' % ', '.join(
                                 str(a.default_home()) for a in adapters.ADAPTERS.values()))
        return result

    # -- privacy and identity ---------------------------------------------------------
    def check_github_private(self, github_repo):
        if not shutil.which('gh'):
            raise ValueError('GitHub CLI (gh) is required to confirm that the repository is private')
        info = json.loads(run(['gh', 'repo', 'view', github_repo, '--json', 'nameWithOwner,isPrivate']).stdout)
        if info.get('nameWithOwner', '').lower() != github_repo.lower() or not info.get('isPrivate'):
            raise ValueError('Refusing to upload: %s must be a private repository' % github_repo)

    def verify_private(self):
        self.require_config()
        origin = self.git('remote', 'get-url', 'origin').stdout.strip()
        if origin != self.cfg['remote']:
            raise ValueError('The history repository remote changed unexpectedly; refusing to push')
        if self.cfg.get('privacy') == 'github':
            self.check_github_private(self.cfg['github_repo'])
        elif self.cfg.get('privacy') != 'trusted':
            raise ValueError('Unknown privacy setting in config')

    def check_identity(self, remote=False):
        rel = 'devices/%s/device.json' % self.device
        if remote:
            result = self.git('show', 'origin/%s:%s' % (self.cfg['branch'], rel), check=False)
            info = json.loads(result.stdout) if result.returncode == 0 else None
        else:
            info = read_json(self.repo / rel)
        if info and info.get('client_id') != self.cfg['client_id']:
            raise ValueError('Device ID collision: another environment already owns ' + self.device)

    # -- checkpoint -------------------------------------------------------------------
    def snapshot_sqlite(self, src: Path) -> Path:
        out_path = self.runtime / ('snapshot-' + src.name)
        out_path.unlink(missing_ok=True)
        inp = sqlite3.connect(src.as_uri() + '?mode=ro', uri=True, timeout=5)
        out = sqlite3.connect(str(out_path))
        try:
            deadline = time.monotonic() + 30

            def progress(*_):
                if time.monotonic() > deadline:
                    raise TimeoutError('SQLite backup timed out')

            inp.backup(out, pages=128, progress=progress, sleep=0.05)
            if out.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite integrity check failed')
            out.execute('pragma journal_mode=DELETE')
            if self.cfg['secrets'] == 'redact':
                redact_sqlite(out)
            out.execute('vacuum')
        finally:
            out.close()
            inp.close()
        chmod_private(out_path)
        return out_path

    def checkpoint(self):
        self.require_config()
        self.check_identity()
        transform = secrets.transformer(self.cfg['secrets'])
        cache = read_json(self.local / 'cache.json', {})
        db_times = read_json(self.local / 'db-snapshots.json', {})
        tmp = self.runtime / 'tmp'
        summary, changed_any = {}, False
        for name, agent_cfg in sorted(self.cfg['agents'].items()):
            adapter = adapters.get(name)
            home = Path(agent_cfg['home'])
            store = AgentStore(self.device_dir / name, tmp)
            manifest = store.load()
            files = manifest.setdefault('files', {})
            databases = manifest.setdefault('databases', {})
            changed, seen, errors = False, set(), []
            if home.is_dir():
                for src in adapter.sources(home):
                    key = name + ':' + src.rel
                    try:
                        st = src.path.stat()
                    except FileNotFoundError:
                        continue
                    seen.add(src.rel)
                    signature = [st.st_mtime_ns, st.st_size]
                    entry = files.get(src.rel)
                    if entry and cache.get(key) == signature and store.exists(src.rel, entry):
                        continue
                    try:
                        new, did = self._backup_file(store, src, entry, transform)
                    except FileNotFoundError:
                        seen.discard(src.rel)
                        continue
                    except ValueError as exc:
                        errors.append(str(exc))
                        continue
                    if new is not None:
                        files[src.rel] = new
                        changed = changed or did
                        cache[key] = signature
            for rel, entry in files.items():
                present = rel in seen
                if entry.get('present', True) != present:
                    entry['present'] = present
                    changed = True
            dbs = adapter.databases(home) if home.is_dir() else []
            if dbs and time.time() - db_times.get(name, 0) >= self.cfg.get('db_interval', 21600):
                for db in dbs:
                    snapshot = None
                    try:
                        snapshot = self.snapshot_sqlite(db)
                        new, did = store.backup_database(db.name, snapshot, databases.get(db.name))
                        databases[db.name] = new
                        changed = changed or did
                    except (sqlite3.Error, TimeoutError, ValueError) as exc:
                        # Transcripts must still be uploaded if an optional database fails.
                        errors.append('database %s skipped: %s' % (db.name, type(exc).__name__))
                    finally:
                        if snapshot:
                            snapshot.unlink(missing_ok=True)
                db_times[name] = time.time()
            if changed or not store.manifest_path.exists():
                manifest.update(format=FORMAT, agent=name, home=str(home), updated_at=now())
                store.save(manifest)
                changed_any = True
            found = {}
            for entry in files.values():
                secrets.merge_counts(found, entry.get('secrets') or {})
            summary[name] = {'files': len(files), 'present': sum(1 for e in files.values() if e.get('present')),
                             'secrets_' + ('redacted' if self.cfg['secrets'] == 'redact' else 'found'): found,
                             'errors': errors}
        self._write_device_info(changed_any)
        write_json(self.local / 'cache.json', cache)
        write_json(self.local / 'db-snapshots.json', db_times)
        committed = self._commit()
        self.update_status(time=now(), device=self.device, last_checkpoint=now(), agents=summary,
                           tool_version=__version__)
        return {'device': self.device, 'committed': committed, 'agents': summary}

    def _backup_file(self, store, src, entry, transform):
        if src.kind == 'jsonl':
            return store.backup_jsonl(src.rel, src.path, entry, transform)
        if src.kind == 'zst':
            # Store the decompressed lines so that a growing session only adds new chunks.
            try:
                plain = codec.decompress_complete(src.path.read_bytes()) if codec.available() else None
            except (ValueError, OSError, subprocess.SubprocessError, RuntimeError):
                plain = None
            if plain is None:
                return store.backup_blob(src.rel, src.path, entry, transform, raw=True)
            tmp = self.runtime / 'tmp' / 'zst-plain.jsonl'
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(plain)
            try:
                new, did = store.backup_jsonl(src.rel, tmp, entry, transform)
            finally:
                tmp.unlink(missing_ok=True)
            if new is not None and new.get('encoding') != 'zstd':
                new = dict(new, encoding='zstd')
                did = True
            return new, did
        return store.backup_blob(src.rel, src.path, entry, transform)

    def _write_device_info(self, changed):
        path = self.device_dir / 'device.json'
        old = read_json(path, {})
        info = {'format': FORMAT, 'device': self.device, 'client_id': self.cfg['client_id'],
                'label': self.cfg['label'], 'platform': platform.system(), 'hostname': socket.gethostname(),
                'registered_at': self.cfg['created_at'], 'agents': sorted(self.cfg['agents']),
                'tool': 'git-agent-history', 'tool_version': __version__}
        comparable = {k: v for k, v in old.items() if k != 'updated_at'}
        if changed or comparable != info:
            info['updated_at'] = now()
            write_json(path, info)

    def _commit(self):
        for name, content in ((MARKER, '{\n  "format": %d,\n  "tool": "git-agent-history",\n  "url": "%s"\n}\n'
                               % (FORMAT, PROJECT_URL)),
                              ('README.md', VAULT_README), ('.gitattributes', '* -text\n')):
            path = self.repo / name
            if not path.exists():
                path.write_bytes((content).encode('utf-8'))
        own = 'devices/' + self.device
        self.git('add', '-A', '--', own, MARKER, 'README.md', '.gitattributes')
        staged = self.git('diff', '--cached', '--name-only').stdout.splitlines()
        shared = {MARKER, 'README.md', '.gitattributes'}
        if any(not (p.startswith(own + '/') or p in shared) for p in staged):
            raise ValueError('Unrelated staged files in the history repository; refusing to commit')
        if not staged:
            return False
        self.git('commit', '-q', '-m', 'Backup %s %s' % (self.device, now()))
        return True

    # -- sync ---------------------------------------------------------------------------
    def _has_head(self):
        return self.git('rev-parse', '--verify', '--quiet', 'HEAD', check=False).returncode == 0

    def _integrate_remote(self):
        """Fetch the remote branch and put local commits on top. Returns False if it is empty."""
        branch = self.cfg['branch']
        if not self.net_git('ls-remote', '--heads', 'origin', branch).stdout.strip():
            return False
        self.net_git('fetch', '-q', 'origin', branch)
        if not self._has_head():
            self.git('checkout', '-q', '-B', branch, 'origin/' + branch)
            return True
        rebased = self.git('rebase', '-q', 'origin/' + branch, check=False)
        if rebased.returncode:
            self.git('rebase', '--abort', check=False)
            raise ValueError('Rebase conflict in the history repository; local commits kept. '
                             'Another environment may be writing to this device folder.')
        return True

    def sync(self):
        self.require_config()
        self.verify_private()
        if self.git('status', '--porcelain', '--untracked-files=no').stdout.strip():
            raise ValueError('History repository has uncommitted edits; sync postponed')
        for attempt in range(5):
            if self._integrate_remote():
                self.check_identity(remote=True)
            if not self._has_head():
                return self.update_status(github='nothing to push yet')
            pushed = self.net_git('push', '-q', 'origin', 'HEAD:refs/heads/' + self.cfg['branch'], check=False)
            if pushed.returncode == 0:
                status = read_json(self.status_file, {})
                for key in ('error', 'message', 'last_error'):
                    status.pop(key, None)
                status.update(last_push=now(), github='pushed')
                write_json(self.status_file, status)
                return status
            time.sleep(0.5 * (attempt + 1))
        raise ValueError('Push failed after 5 attempts; local commits kept for the next run')

    def pull(self):
        self.require_config()
        self.verify_private()
        self._integrate_remote()
        return self.devices()

    # -- reading the repository --------------------------------------------------------
    def devices(self):
        result = []
        base = self.repo / 'devices'
        for folder in sorted(base.iterdir()) if base.is_dir() else []:
            info = read_json(folder / 'device.json')
            legacy = None if info else read_json(folder / 'manifest.json')
            if not info and not legacy:
                continue
            entry = {k: (info or legacy).get(k) for k in ('device', 'label', 'platform', 'hostname', 'updated_at')}
            entry['agents'] = {}
            for name in adapters.ADAPTERS:
                try:
                    adapter, _, manifest = self.load_agent(folder.name, name)
                except ValueError:
                    continue
                files = manifest.get('files', {})
                entry['agents'][name] = {
                    'sessions': len({adapter_session_id(adapter, r) for r in files} - {None}),
                    'files': len(files), 'updated_at': manifest.get('updated_at')}
            entry['this_environment'] = bool(self.cfg) and folder.name == self.cfg.get('device')
            result.append(entry)
        return result

    def load_agent(self, device, agent):
        device = valid_device_id(device)
        adapter = adapters.get(agent)
        root = self.repo / 'devices' / device
        tmp = self.runtime / 'tmp'
        store = AgentStore(root / agent, tmp)
        manifest = read_json(store.manifest_path)
        if manifest:
            return adapter, store, manifest
        legacy = read_json(root / 'manifest.json')
        if agent == 'codex' and legacy and legacy.get('format') == 2 and 'files' in legacy:
            # Layout written by codex-history, the private tool this project grew out of.
            files = {rel: {'kind': 'blob', 'sha256': info['sha256'], 'bytes': info.get('bytes'),
                           'path': 'transcripts/' + rel + '.gz', 'present': True}
                     for rel, info in legacy['files'].items()}
            databases = {name: {'sha256': info['sha256'], 'path': 'state/' + name + '.gz'}
                         for name, info in legacy.get('databases', {}).items()}
            return adapter, AgentStore(root, tmp), {'files': files, 'databases': databases,
                                                    'home': legacy.get('source_home'),
                                                    'updated_at': legacy.get('updated_at')}
        raise ValueError('No %s history for device %s; run `pull` then `devices`' % (agent, device))

    def sessions(self, device, agent=None):
        names = [agent] if agent else sorted(adapters.ADAPTERS)
        result = []
        for name in names:
            try:
                adapter, _, manifest = self.load_agent(device, name)
            except ValueError:
                if agent:
                    raise
                continue
            latest = {}
            for rel, entry in sorted(manifest.get('files', {}).items()):
                session = adapter_session_id(adapter, rel)
                if session:  # sorted order: a later format generation of a session replaces earlier ones
                    latest[session] = {'agent': name, 'session': session, 'path': rel, 'bytes': entry.get('bytes'),
                                       'present_on_device': entry.get('present', True),
                                       'updated_at': entry.get('updated_at')}
            result.extend(latest.values())
        return result

    # -- restore ------------------------------------------------------------------------
    def restore(self, device, agent, target, with_database=False, mappings=(), session=None, dry_run=False):
        adapter, store, manifest = self.load_agent(device, agent)
        target = Path(target).expanduser().resolve()
        live = target == self.agent_home(agent).resolve() and adapter.running()
        files = manifest.get('files', {})
        for rel in files:
            if not adapter.allowed(safe_rel(rel)):
                raise ValueError('Unexpected file in %s manifest: %s' % (agent, rel))
        selected = [rel for rel in sorted(files) if not _superseded(rel, files)]
        if session:
            selected = [r for r in selected if session in r and not adapter.index_kind(r)]
            if not selected:
                raise ValueError('No stored file matches session %s' % session)
        if with_database and live:
            raise ValueError('Close %s before restoring its database into the live home' % adapter.display_name)
        valid_db = getattr(adapter, 'valid_database_name', lambda name: False)
        if with_database and target.is_dir() and any(valid_db(p.name) for p in target.iterdir()):
            raise ValueError('Database restore needs a target without existing databases; '
                             'restore without --with-database or use an empty target')
        report = {'from_device': device, 'agent': agent, 'target': str(target), 'dry_run': dry_run,
                  'restored': 0, 'relocated': 0, 'identical_kept': 0, 'conflicts_kept': [],
                  'index_lines_added': 0, 'index_skipped_while_running': [], 'databases_restored': []}
        with tempfile.TemporaryDirectory(prefix='gah-restore-') as td:
            staged = Path(td)
            for rel in selected:
                store.materialize(rel, files[rel], staged / rel)
            plan = adapter.target_paths(staged, selected, mappings)
            for rel in selected:
                dst_rel = plan[rel]
                if not adapter.allowed(safe_rel(dst_rel)):
                    raise ValueError('Refusing unexpected restore path ' + dst_rel)
                src, dst = staged / rel, target / dst_rel
                kind = adapter.index_kind(rel)
                if kind:
                    if live:
                        report['index_skipped_while_running'].append(rel)
                    elif not dry_run:
                        report['index_lines_added'] += merge_index(src, dst, by_id=kind == 'id')
                    continue
                encoded = files[rel].get('encoding') == 'zstd'
                if dst.exists():
                    if _same(dst, src, encoded):
                        report['identical_kept'] += 1
                    else:
                        report['conflicts_kept'].append(dst_rel)
                    continue
                if not dry_run:
                    if encoded:
                        src.write_bytes(codec.encode(src.read_bytes(), adapter.zstd_header_frame))
                    _copy_new(src, dst)
                report['restored'] += 1
                report['relocated'] += dst_rel != rel
            if with_database:
                for name, info in sorted(manifest.get('databases', {}).items()):
                    if not getattr(adapter, 'valid_database_name', lambda n: False)(name):
                        raise ValueError('Unexpected database name ' + name)
                    local = staged / '.databases' / name
                    store.materialize_database(name, info, local)
                    adapter.rewrite_database(local, manifest.get('home') or '', target, mappings)
                    if not dry_run:
                        _copy_new(local, target / name)
                    report['databases_restored'].append(name)
        if report['conflicts_kept']:
            report['note'] = ('Existing files that differ were kept untouched. Restore into an empty '
                              'folder to compare them.')
        return report

    # -- secrets scan ---------------------------------------------------------------------
    def scan(self):
        from . import codec
        self.require_config()
        result = {}
        for name, agent_cfg in sorted(self.cfg['agents'].items()):
            adapter, home = adapters.get(name), Path(agent_cfg['home'])
            findings, totals, skipped = [], {}, 0
            for src in adapter.sources(home) if home.is_dir() else []:
                data = src.path.read_bytes()
                if src.kind == 'zst':
                    data = codec.decompress(data) if codec.available() else None
                    if data is None:
                        skipped += 1
                        continue
                counts = secrets.find(data)
                if counts:
                    findings.append({'path': src.rel, 'kinds': counts})
                    secrets.merge_counts(totals, counts)
            result[name] = {'files_with_findings': findings, 'totals': totals,
                            'zst_files_not_scanned': skipped}
        return result

    def status(self):
        from .service import worker_alive
        self.require_config()
        result = read_json(self.status_file, {})
        result.update(worker_alive=worker_alive(self), push=self.cfg.get('push'), device=self.device,
                      state_dir=str(self.state), agents_configured=self.cfg['agents'])
        return result


def adapter_session_id(adapter, rel):
    fn = getattr(adapter, 'session_id', None)
    return fn(rel) if fn else None


def _superseded(rel, files):
    """Skip an old copy when the agent re-encoded a session (x.jsonl <-> x.jsonl.zst)."""
    if files[rel].get('present', True):
        return False
    twin = rel[:-4] if rel.endswith('.zst') else rel + '.zst'
    return bool(files.get(twin, {}).get('present', False))


def _same(dst: Path, src: Path, encoded: bool) -> bool:
    if not encoded:
        return _sha(dst) == _sha(src)
    try:
        return codec.decompress_complete(dst.read_bytes()) == src.read_bytes()
    except (ValueError, OSError, subprocess.SubprocessError, RuntimeError):
        return False


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _copy_new(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + '.gah-restore-tmp')
    shutil.copyfile(src, tmp)
    chmod_private(tmp)
    tmp.replace(dst)


def merge_index(src: Path, dst: Path, by_id: bool) -> int:
    """Append backup records missing from dst; never remove or reorder existing lines."""

    def key(line):
        if by_id:
            try:
                obj = json.loads(line)
                return obj.get('id') or obj.get('session_id') or line
            except ValueError:
                return line
        return line

    original = dst.read_text(encoding='utf-8') if dst.exists() else ''
    if original and not original.endswith('\n'):
        raise ValueError('%s ends in a partial record; close the agent and retry' % dst.name)
    # Agents append a new record when a session changes (later lines win), so keep every
    # backup line of a session the target does not know, in order, including repeats.
    existing = {key(line) for line in original.splitlines() if line.strip()}
    additions = [line for line in src.read_text(encoding='utf-8').splitlines()
                 if line.strip() and key(line) not in existing]
    if additions:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + '.gah-restore-tmp')
        tmp.write_bytes((original + '\n'.join(additions) + '\n').encode('utf-8'))
        chmod_private(tmp)
        tmp.replace(dst)
    return len(additions)


def redact_sqlite(db) -> dict:
    """Redact secrets in text columns of a snapshot copy (virtual tables are left alone)."""
    counts = {}

    def has_secret(value):
        return 1 if isinstance(value, str) and secrets.find(value.encode('utf-8', 'surrogatepass')) else 0

    def clean(value):
        if not isinstance(value, str):
            return value
        new, found = secrets.redact(value.encode('utf-8', 'surrogatepass'))
        secrets.merge_counts(counts, found)
        return new.decode('utf-8', 'surrogatepass')

    db.create_function('gah_has_secret', 1, has_secret)
    db.create_function('gah_redact', 1, clean)
    rows = db.execute("select name, sql from sqlite_master where type='table'").fetchall()
    virtual = [n for n, sql in rows if (sql or '').upper().startswith('CREATE VIRTUAL TABLE')]
    for table, _ in rows:
        if table.startswith('sqlite_') or any(table == v or table.startswith(v + '_') for v in virtual):
            continue
        qt = '"%s"' % table.replace('"', '""')
        for column in [r[1] for r in db.execute('pragma table_info(%s)' % qt)]:
            qc = '"%s"' % column.replace('"', '""')
            db.execute('update %s set %s = gah_redact(%s) where typeof(%s) = \'text\' and gah_has_secret(%s)'
                       % (qt, qc, qc, qc, qc))
    db.commit()
    return counts
