"""Offline tests: temporary agent homes, a local bare Git remote and synthetic transcripts."""
import contextlib
import datetime
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from git_agent_history import adapters, cli, codec, handoff, prompts, secrets, service  # noqa: E402
from git_agent_history.adapters.claude_code import encode_project_dir  # noqa: E402
from git_agent_history.adapters.deepseek_harness import project_key  # noqa: E402
from git_agent_history.client import Client, redact_sqlite  # noqa: E402
from git_agent_history.store import AgentStore  # noqa: E402
from git_agent_history.util import remap_path  # noqa: E402

# Built at runtime so that the repository itself never contains token-shaped strings.
FAKE_GITHUB = 'gh' + 'p_' + 'A1b2C3d4' * 5
FAKE_ANTHROPIC = 'sk-' + 'ant-' + 'api03-' + 'x' * 40
UUID_A = '0199aaaa-1111-7222-8333-444455556666'
UUID_B = '0199bbbb-1111-7222-8333-444455556666'


def line(obj):
    return json.dumps(obj) + '\n'


class Env:
    """A fake machine: Codex, Claude Code and DeepSeek Harness homes plus a state directory."""

    def __init__(self, root: Path, name: str, remote: Path):
        self.root = root / name
        self.codex = self.root / 'codex-home'
        self.claude = self.root / 'claude-home'
        self.dsh = self.root / 'dsh-home'
        for home in (self.codex, self.claude, self.dsh):
            home.mkdir(parents=True)
        self.client = Client(self.root / 'state')
        self.name, self.remote = name, remote

    def init(self, **kwargs):
        return self.client.init(str(self.remote), device=self.name,
                                agents=['codex', 'claude-code', 'deepseek-harness'],
                                homes={'codex': str(self.codex), 'claude-code': str(self.claude),
                                       'deepseek-harness': str(self.dsh)},
                                trust_remote=True, **kwargs)

    def once(self):
        return service.once(self.client)

    def rollout(self, uuid=UUID_A, day='2026/11/12'):
        return self.codex / 'sessions' / day / ('rollout-2026-11-12T14-13-33-%s.jsonl' % uuid)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.remote = self.tmp / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        subprocess.run(['git', '-C', str(self.remote), 'symbolic-ref', 'HEAD', 'refs/heads/main'], check=True)
        patcher = mock.patch.object(adapters.base.Adapter, 'running', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, name, init=True):
        e = Env(self.tmp, name, self.remote)
        if init:
            e.init()
        return e


class CodexTests(Base):
    def make_codex(self, env):
        rollout = env.rollout()
        rollout.parent.mkdir(parents=True)
        rollout.write_text(line({'type': 'session_meta', 'payload': {'id': UUID_A, 'cwd': '/old/proj'}})
                           + line({'type': 'message', 'text': 'hello'}) + '{"partial": tr', encoding='utf-8')
        (env.codex / 'session_index.jsonl').write_text(line({'id': UUID_A, 'thread_name': 'first'}) +
                                                       line({'id': UUID_A, 'thread_name': 'demo'}))
        (env.codex / 'auth.json').write_text('{"token": "never-backed-up"}')
        db = sqlite3.connect(str(env.codex / 'state_5.sqlite'))
        db.execute('create table threads (id text primary key, rollout_path text, cwd text, title text)')
        db.execute('insert into threads values (?, ?, ?, ?)',
                   (UUID_A, str(rollout), '/old/proj/sub', 'uses ' + FAKE_GITHUB))
        db.commit()
        db.close()
        return rollout

    def test_roundtrip_with_database_and_path_mapping(self):
        a = self.env('laptop')
        rollout = self.make_codex(a)
        result = a.once()
        self.assertTrue(result['committed'])
        stored = a.client.repo / 'devices/laptop/codex'
        self.assertFalse(list(stored.rglob('auth.json*')))

        b = self.env('server')
        b.client.pull()
        target = self.tmp / 'restored'
        report = b.client.restore('laptop', 'codex', target, with_database=True,
                                  mappings=[('/old/proj', '/new/place')])
        self.assertEqual(report['restored'], 1)
        self.assertEqual(report['databases_restored'], ['state_5.sqlite'])
        restored = target / rollout.relative_to(a.codex)
        self.assertEqual(restored.read_text(), rollout.read_text().rsplit('\n', 1)[0] + '\n')
        index = (target / 'session_index.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(x)['thread_name'] for x in index], ['first', 'demo'])  # later line wins
        db = sqlite3.connect(str(target / 'state_5.sqlite'))
        path, cwd, title = db.execute('select rollout_path, cwd, title from threads').fetchone()
        db.close()
        self.assertEqual(Path(path).resolve(), restored.resolve())
        self.assertEqual(cwd, '/new/place/sub')
        self.assertNotIn(FAKE_GITHUB, title)  # database snapshots are redacted too

    def test_append_only_chunks_and_rewrite(self):
        a = self.env('box')
        rollout = self.make_codex(a)
        rollout.write_text(line({'n': 1}) + line({'n': 2}))
        a.once()
        with rollout.open('a') as f:
            f.write(line({'n': 3}))
        a.once()
        manifest = json.loads((a.client.repo / 'devices/box/codex/manifest.json').read_text())
        entry = manifest['files'][rollout.relative_to(a.codex).as_posix()]
        self.assertEqual((entry['generation'], len(entry['chunks'])), (1, 2))
        first = a.client.repo / 'devices/box/codex/files' / (rollout.relative_to(a.codex).as_posix() + '.chunks')
        self.assertEqual(gzip.decompress((first / 'g001-00002.gz').read_bytes()).decode().replace('\r\n', '\n'),
                         line({'n': 3}))
        self.assertFalse(a.once()['committed'])  # nothing changed, no commit

        rollout.write_text(line({'n': 'rewritten'}))
        os.utime(rollout, ns=(1, 1))
        a.once()
        manifest = json.loads((a.client.repo / 'devices/box/codex/manifest.json').read_text())
        entry = manifest['files'][rollout.relative_to(a.codex).as_posix()]
        self.assertEqual((entry['generation'], len(entry['chunks'])), (2, 1))
        out = self.tmp / 'out'
        a.client.restore('box', 'codex', out)
        self.assertEqual((out / rollout.relative_to(a.codex)).read_text(), line({'n': 'rewritten'}))

    def test_deleted_source_stays_restorable(self):
        a = self.env('dsw')
        rollout = self.make_codex(a)
        a.once()
        rollout.unlink()
        a.once()
        listed = a.client.sessions('dsw', 'codex')
        self.assertEqual([(s['session'], s['present_on_device']) for s in listed], [(UUID_A, False)])
        out = self.tmp / 'out'
        self.assertEqual(a.client.restore('dsw', 'codex', out)['restored'], 1)

    def test_existing_files_are_never_overwritten(self):
        a = self.env('one')
        rollout = self.make_codex(a)
        a.once()
        target = self.tmp / 'target'
        dst = target / rollout.relative_to(a.codex)
        dst.parent.mkdir(parents=True)
        dst.write_text('local version\n')
        report = a.client.restore('one', 'codex', target)
        self.assertEqual(report['conflicts_kept'], [rollout.relative_to(a.codex).as_posix()])
        self.assertEqual(dst.read_text(), 'local version\n')

    def test_live_agent_home_skips_index_and_database(self):
        a = self.env('live')
        self.make_codex(a)
        a.once()
        for name in ('session_index.jsonl', 'state_5.sqlite'):
            (a.codex / name).unlink()
        for f in (a.codex / 'sessions').rglob('*.jsonl'):
            f.unlink()
        with mock.patch.object(adapters.get('codex'), 'running', return_value=True):
            report = a.client.restore('live', 'codex', a.codex)
            self.assertEqual(report['restored'], 1)
            self.assertEqual(report['index_skipped_while_running'], ['session_index.jsonl'])
            with self.assertRaises(ValueError):
                a.client.restore('live', 'codex', a.codex, with_database=True)

    @unittest.skipUnless(codec.available(), 'no zstd backend')
    def test_zst_rollouts_are_redacted_and_recompressed(self):
        a = self.env('zst')
        path = a.codex / 'archived_sessions' / ('rollout-2026-01-01T00-00-00-%s.jsonl.zst' % UUID_B)
        path.parent.mkdir(parents=True)
        path.write_bytes(codec.compress(line({'text': 'key ' + FAKE_ANTHROPIC}).encode()))
        a.once()
        out = self.tmp / 'out'
        a.client.restore('zst', 'codex', out)
        text = codec.decompress((out / path.relative_to(a.codex)).read_bytes()).decode()
        self.assertIn('[REDACTED:anthropic_key]', text)
        self.assertNotIn(FAKE_ANTHROPIC, text)

    def test_legacy_codex_history_layout(self):
        a = self.env('reader')
        legacy = a.client.repo / 'devices/old-dsw'
        data = line({'legacy': True}).encode()
        rel = 'sessions/2026/11/12/rollout-2026-11-12T14-13-33-%s.jsonl' % UUID_A
        (legacy / 'transcripts' / rel).parent.mkdir(parents=True)
        (legacy / 'transcripts' / (rel + '.gz')).write_bytes(gzip.compress(data))
        import hashlib
        (legacy / 'manifest.json').write_text(json.dumps({
            'format': 2, 'device': 'old-dsw', 'files': {rel: {'sha256': hashlib.sha256(data).hexdigest(),
                                                              'bytes': len(data)}}}))
        out = self.tmp / 'out'
        self.assertEqual(a.client.restore('old-dsw', 'codex', out)['restored'], 1)
        self.assertEqual((out / rel).read_bytes(), data)


class ClaudeCodeTests(Base):
    def make_claude(self, env):
        project = env.claude / 'projects' / encode_project_dir('/Users/me/proj')
        session = project / (UUID_B + '.jsonl')
        session.parent.mkdir(parents=True)
        session.write_text(line({'type': 'summary'}) +
                           line({'type': 'user', 'cwd': '/Users/me/proj', 'sessionId': UUID_B,
                                 'message': {'content': 'token ' + FAKE_GITHUB}}))
        sub = project / UUID_B / 'subagents'
        sub.mkdir(parents=True)
        (sub / 'agent-1.jsonl').write_text(line({'agent': 1}))
        (sub / 'agent-1.meta.json').write_text('{"agentType": "Explore"}')
        tools = project / UUID_B / 'tool-results'
        tools.mkdir()
        (tools / 'out.txt').write_text('long tool output\n')
        (tools / 'shot.png').write_bytes(b'\x89PNG')
        (project / 'memory').mkdir()
        (project / 'memory' / 'MEMORY.md').write_text('not backed up in v0.1')
        (env.claude / 'history.jsonl').write_text(line({'display': 'hi', 'project': '/Users/me/proj'}))
        (env.claude / '.credentials.json').write_text('{"secret": true}')
        (env.claude / 'settings.json').write_text('{}')
        return session

    def test_backup_scope_redaction_and_relocation(self):
        a = self.env('mac')
        session = self.make_claude(a)
        a.once()
        stored = a.client.repo / 'devices/mac/claude-code'
        names = {p.name for p in stored.rglob('*')}
        for absent in ('.credentials.json.gz', 'settings.json.gz', 'MEMORY.md.gz', 'shot.png.gz'):
            self.assertNotIn(absent, names)
        manifest = json.loads((stored / 'manifest.json').read_text())
        self.assertEqual(manifest['files'][session.relative_to(a.claude).as_posix()]['secrets'],
                         {'github_token': 1})

        target = self.tmp / 'linux-claude'
        (target).mkdir()
        (target / 'history.jsonl').write_text(line({'display': 'existing'}))
        report = a.client.restore('mac', 'claude-code', target, mappings=[('/Users/me', '/home/me')])
        self.assertEqual(report['relocated'], 4)
        moved = target / 'projects' / encode_project_dir('/home/me/proj')
        restored = (moved / (UUID_B + '.jsonl')).read_text()
        self.assertIn('[REDACTED:github_token]', restored)
        for raw in restored.splitlines():
            json.loads(raw)
        self.assertTrue((moved / UUID_B / 'subagents' / 'agent-1.jsonl').exists())
        self.assertTrue((moved / UUID_B / 'tool-results' / 'out.txt').exists())
        history = (target / 'history.jsonl').read_text().splitlines()
        self.assertEqual(len(history), 2)
        self.assertEqual(json.loads(history[0])['display'], 'existing')

    def test_scan_never_prints_values(self):
        a = self.env('scan')
        self.make_claude(a)
        result = json.dumps(a.client.scan())
        self.assertIn('github_token', result)
        self.assertNotIn(FAKE_GITHUB, result)

    def test_startup_refills_empty_home(self):
        a = self.env('container')
        session = self.make_claude(a)
        a.once()
        import shutil
        shutil.rmtree(a.claude / 'projects')
        with mock.patch.object(service, 'start', return_value={'started': True}):
            result = service.startup(a.client)
        self.assertIn('claude-code', result['restored'])
        self.assertTrue(session.exists())


@unittest.skipUnless(codec.available(), 'no zstd backend')
class DeepSeekHarnessTests(Base):
    """Synthetic logs in the layout of DeepSeek Harness's JSONL persistence backend."""

    def make_dsh(self, env, cwd='/Users/me/proj'):
        folder = env.dsh / 'sessions' / project_key(cwd) / 'ses~005F1'   # session id "ses_1"
        folder.mkdir(parents=True)
        log = folder / 'session.v3.jsonl.zstd'
        header = {'type': 'session', 'version': 3, 'id': 'ses_1', 'createdAt': 1, 'cwd': cwd,
                  'isSeeded': False, 'delegationDepth': 0}
        log.write_bytes(codec.compress(json.dumps(header, separators=(',', ':')).encode() + b'\n')
                        + codec.compress(line({'type': 'user', 'text': 'key ' + FAKE_GITHUB}).encode()))
        (folder / 'session.lock').write_text('')
        (env.dsh / 'config.yml').write_text('apiKey: never-backed-up')
        return log

    def test_frames_are_stored_incrementally_and_restored(self):
        a = self.env('gpu')
        log = self.make_dsh(a)
        a.once()
        with log.open('ab') as f:
            f.write(codec.compress(line({'type': 'assistant', 'text': 'done'}).encode()))
            f.write(codec.compress(line({'type': 'torn'}).encode())[:7])   # frame still being written
        a.once()
        stored = a.client.repo / 'devices/gpu/deepseek-harness'
        manifest = json.loads((stored / 'manifest.json').read_text())
        rel = log.relative_to(a.dsh).as_posix()
        self.assertEqual(set(manifest['files']), {rel})                 # no lock or config files
        self.assertEqual(len(manifest['files'][rel]['chunks']), 2)
        self.assertEqual(manifest['files'][rel]['encoding'], 'zstd')

        out = self.tmp / 'out'
        a.client.restore('gpu', 'deepseek-harness', out)
        data = (out / rel).read_bytes()
        lines = codec.decompress(data).decode().splitlines()
        self.assertEqual([json.loads(x)['type'] for x in lines], ['session', 'user', 'assistant'])
        self.assertIn('[REDACTED:github_token]', lines[1])
        header_frame = codec.decompress(data[:len(codec.compress(lines[0].encode() + b'\n'))])
        self.assertEqual(json.loads(header_frame)['type'], 'session')  # header line has its own frame
        self.assertEqual([s['session'] for s in a.client.sessions('gpu', 'deepseek-harness')], ['ses_1'])

    def test_map_cwd_rewrites_header_and_folder(self):
        a = self.env('mac')
        log = self.make_dsh(a)
        a.once()
        out = self.tmp / 'linux-dsh'
        report = a.client.restore('mac', 'deepseek-harness', out, mappings=[('/Users/me', '/home/me')])
        self.assertEqual(report['relocated'], 1)
        moved = out / 'sessions' / project_key('/home/me/proj') / 'ses~005F1' / log.name
        header = json.loads(codec.decompress(moved.read_bytes()).decode().splitlines()[0])
        self.assertEqual(header['cwd'], '/home/me/proj')
        self.assertEqual(a.client.restore('mac', 'deepseek-harness', out,
                                          mappings=[('/Users/me', '/home/me')])['identical_kept'], 1)


class SyncTests(Base):
    def test_two_devices_share_one_repository(self):
        a, b = self.env('a'), self.env('b')
        for env in (a, b):
            (env.claude / 'history.jsonl').write_text(line({'display': env.name}))
        a.once()
        b.once()                        # must rebase on top of a's push
        (a.claude / 'history.jsonl').write_text(line({'display': 'a2'}))
        a.once()
        b.client.pull()
        self.assertEqual({d['device'] for d in b.client.devices()}, {'a', 'b'})
        log = subprocess.run(['git', '--git-dir', str(self.remote), 'log', '--name-only', '--format=%s'],
                             capture_output=True, text=True, check=True).stdout
        for commit in log.split('Backup ')[1:]:
            device = commit.split()[0]
            touched = [p for p in commit.splitlines()[1:] if p.startswith('devices/')]
            self.assertTrue(all(p.startswith('devices/%s/' % device) for p in touched))

    def test_device_id_collision_is_refused(self):
        a = self.env('dup')
        (a.claude / 'history.jsonl').write_text(line({'x': 1}))
        a.once()
        c = self.env('other', init=False)
        with self.assertRaises(ValueError):
            c.client.init(str(self.remote), device='dup', agents=['claude-code'],
                          homes={'claude-code': str(c.claude)}, trust_remote=True)

    def test_unsafe_manifest_paths_are_rejected(self):
        a = self.env('evil')
        (a.claude / 'history.jsonl').write_text(line({'x': 1}))
        a.once()
        path = a.client.repo / 'devices/evil/claude-code/manifest.json'
        manifest = json.loads(path.read_text())
        manifest['files']['../../outside.jsonl'] = manifest['files']['history.jsonl']
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            a.client.restore('evil', 'claude-code', self.tmp / 'out')

    def test_network_commands_tolerate_slow_large_transfers(self):
        from git_agent_history import client as client_module
        real_run, calls = client_module.run, []

        def recording_run(args, **kwargs):
            calls.append(([str(x) for x in args], kwargs.get('timeout')))
            return real_run(args, **kwargs)

        with mock.patch.object(client_module, 'run', side_effect=recording_run):
            a = self.env('net')                 # clone
            (a.claude / 'history.jsonl').write_text(line({'x': 1}))
            a.once()                            # ls-remote, push
            (a.claude / 'history.jsonl').write_text(line({'x': 1}) + line({'x': 2}))
            a.once()                            # ls-remote, fetch, push
        seen = set()
        for args, timeout in calls:
            verb = next((v for v in ('clone', 'ls-remote', 'fetch', 'push') if v in args), None)
            if args[0] != 'git' or verb is None:
                continue
            seen.add(verb)
            self.assertIn('http.version=HTTP/1.1', args, verb)
            self.assertIn('http.lowSpeedTime=300', args, verb)
            self.assertGreaterEqual(timeout, 3600, verb)
        self.assertEqual(seen, {'clone', 'ls-remote', 'fetch', 'push'})

    def test_non_github_remote_needs_explicit_trust(self):
        c = self.env('t', init=False)
        with self.assertRaises(ValueError):
            c.client.init(str(self.remote), device='t', agents=['claude-code'], homes={'claude-code': str(c.claude)})


class HandoffTests(Base):
    def test_task_passes_between_devices(self):
        mac, dsw = self.env('mac'), self.env('dsw')
        made = handoff.new(mac.client, 'Add login API', 'claude-code', 'codex',
                           'Goal: add /login.\nToken for CI: ' + FAKE_GITHUB)
        task = made['task']
        self.assertRegex(task, r'^\d{8}-add-login-api-[0-9a-f]{4}$')
        self.assertEqual((made['sync']['github'], made['secrets_redacted']), ('pushed', {'github_token': 1}))

        self.assertEqual([t['task'] for t in handoff.list_tasks(dsw.client, for_agent='codex')['tasks']], [task])
        self.assertEqual(handoff.list_tasks(dsw.client, for_agent='claude-code')['tasks'], [])
        handoff.post(dsw.client, task, 'codex', 'Tests added; please review.', to='claude-code', status='review')

        shown = handoff.show(mac.client, task)
        self.assertEqual((shown['next'], shown['status'], shown['entries']), ('claude-code', 'review', 2))
        self.assertIn('Goal: add /login.', shown['spec'])
        self.assertNotIn(FAKE_GITHUB, shown['spec'])
        self.assertEqual([(e['from'], e['device']) for e in shown['log']], [('claude-code', 'mac'), ('codex', 'dsw')])
        handoff.post(mac.client, task, 'claude-code', 'Merged.', to='human', status='done')
        self.assertEqual(handoff.list_tasks(dsw.client)['tasks'], [])
        self.assertEqual(len(handoff.list_tasks(dsw.client, include_done=True)['tasks']), 1)

        (mac.claude / 'history.jsonl').write_text(line({'x': 1}))
        mac.once()                          # backups keep working next to handoffs
        tree = subprocess.run(['git', '--git-dir', str(self.remote), 'ls-tree', '-r', '--name-only', 'main'],
                              capture_output=True, text=True, check=True).stdout.split()
        entries = [p for p in tree if '/handoff/' in p]
        self.assertEqual(len(entries), 3)
        for path in entries:                # each device wrote only into its own folder
            self.assertTrue(path.startswith('devices/mac/handoff/%s/' % task) and path.endswith('-claude-code.md')
                            or path.startswith('devices/dsw/handoff/%s/' % task) and path.endswith('-codex.md'), path)

    def test_spec_updates_and_markdown(self):
        mac = self.env('mac')
        task = handoff.new(mac.client, '整理实验结果', 'human', 'claude-code')['task']
        self.assertRegex(task, r'^\d{8}-[0-9a-f]{4}$')
        self.assertIn('## Acceptance criteria', handoff.show(mac.client, task)['spec'])   # template
        handoff.post(mac.client, task, 'claude-code', 'Compare runs A and B.', spec=True)
        shown = handoff.show(mac.client, task)
        self.assertEqual((shown['spec'], shown['spec_by']), ('Compare runs A and B.\n', 'claude-code'))
        text = handoff.render(shown)
        self.assertTrue(text.startswith('# 整理实验结果\n'))
        self.assertIn('spec updated on mac', text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['--state-dir', str(mac.client.state), 'handoff', 'show', task, '--markdown'])
        self.assertTrue(out.getvalue().startswith('# 整理实验结果\n'))

    def test_bad_input_and_foreign_files_are_rejected(self):
        mac = self.env('mac')
        with self.assertRaises(ValueError):
            handoff.post(mac.client, 'no-such-task', 'codex', 'hello')
        with self.assertRaises(ValueError):
            handoff.new(mac.client, 'x', 'codex', 'human', task='../evil')
        task = handoff.new(mac.client, 'x', 'codex', 'human', 'spec')['task']
        with self.assertRaises(ValueError):
            handoff.new(mac.client, 'x', 'codex', 'human', 'spec', task=task)
        with self.assertRaises(ValueError):
            handoff.post(mac.client, task, 'codex', '   ')
        folder = mac.client.repo / 'devices/mac/handoff' / task
        (folder / 'notes.txt').write_text('not an entry')
        (folder / '20990101T000000000000Z-codex.md').write_text('---\ntask: other-task\n---\nwrong task')
        shown = handoff.show(mac.client, task)
        self.assertEqual((shown['entries'], shown['skipped_entries'], shown['next']), (1, 2, 'human'))


class PromptTests(Base):
    """The prompt files in devices/<id>/prompts/ and the `prompts` command (docs/prompts.md)."""

    def claude_session(self, env, uuid, records, cwd='/Users/me/proj'):
        path = env.claude / 'projects' / encode_project_dir(cwd) / (uuid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as f:
            f.write(''.join(line(r) for r in records))
        return path

    @staticmethod
    def typed(content, when, **extra):
        record = {'type': 'user', 'timestamp': when, 'sessionId': UUID_B, 'cwd': '/Users/me/proj',
                  'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': content}}
        record.update(extra)
        return record

    def test_claude_prompts_are_cleaned_deduplicated_and_committed(self):
        a = self.env('mac')
        first = self.typed([{'type': 'text', 'text': '<ide_opened_file>The user opened x.py</ide_opened_file>'},
                            {'type': 'text', 'text': 'Fix the <b>bug</b>, token ' + FAKE_GITHUB}],
                           '2026-11-20T01:02:03.456Z')
        session = self.claude_session(a, UUID_B, [
            first,
            {'type': 'user', 'timestamp': '2026-11-20T01:02:05Z', 'toolUseResult': {},
             'message': {'content': [{'type': 'tool_result', 'content': 'output'}]}},
            {'type': 'user', 'isMeta': True, 'timestamp': '2026-11-20T01:02:06Z', 'message': {'content': 'meta'}},
            {'type': 'user', 'isCompactSummary': True, 'timestamp': '2026-11-20T01:03:00Z',
             'message': {'content': 'This session is being continued'}},
            self.typed('<task-notification>done</task-notification>', '2026-11-20T01:04:00Z',
                       origin={'kind': 'task-notification'}),
            {'type': 'user', 'timestamp': '2026-11-20T01:04:30Z',
             'message': {'content': [{'type': 'text', 'text': '[Request interrupted by user]'}]}},
            self.typed('<command-name>/model</command-name>\n<command-message>model</command-message>\n'
                       '<command-args>opus</command-args>', '2026-11-20T01:05:00Z'),
            {'type': 'assistant', 'timestamp': '2026-11-20T01:05:01Z', 'message': {'content': 'ok'}},
        ])
        self.claude_session(a, UUID_A, [first])                  # a forked session copies the prompt
        sub = session.parent / UUID_B / 'subagents'
        sub.mkdir(parents=True)
        (sub / 'agent-1.jsonl').write_text(line(self.typed('task for a subagent', '2026-11-20T01:06:00Z')))

        result = a.once()
        self.assertEqual(result['prompt_docs'], {'prompts': 2, 'files': 2, 'written': ['2026-11.md', 'README.md']})
        texts = [p['text'] for p in prompts.query(a.client, limit=0)['prompts']]
        self.assertEqual(texts, ['/model opus', 'Fix the <b>bug</b>, token [REDACTED:github_token]'])
        tree = subprocess.run(['git', '--git-dir', str(self.remote), 'ls-tree', '-r', '--name-only', 'main'],
                              capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual([p for p in tree if '/prompts/' in p],
                         ['devices/mac/prompts/2026-11.md', 'devices/mac/prompts/README.md'])
        month = (a.client.repo / 'devices/mac/prompts/2026-11.md').read_text(encoding='utf-8')
        self.assertIn('> Fix the &lt;b&gt;bug&lt;/b&gt;, token [REDACTED:github_token]', month)
        self.assertNotIn('ide_opened_file', month)
        self.assertEqual(a.once()['committed'], False)            # nothing new, no commit

        # A new prompt is read from the new chunk only.
        self.claude_session(a, UUID_B, [self.typed([{'type': 'image', 'source': {}},
                                                    {'type': 'text', 'text': 'what is this?'}],
                                                   '2026-12-01T08:00:00Z')])
        starts = []
        original = AgentStore.iter_lines

        def spy(store, rel, entry, start_chunk=0):
            if rel == session.relative_to(a.claude).as_posix():
                starts.append(start_chunk)
            return original(store, rel, entry, start_chunk)

        with mock.patch.object(AgentStore, 'iter_lines', spy):
            result = a.once()
        self.assertEqual(starts, [1])
        self.assertEqual(result['prompt_docs']['written'], ['2026-12.md', 'README.md'])
        newest = prompts.query(a.client, limit=1)['prompts'][0]
        self.assertEqual((newest['text'], newest['images'], newest['session']), ('what is this?', 1, UUID_B))

    def test_codex_prompt_formats(self):
        a = self.env('dsw')
        uuid_c = '0199cccc-1111-7222-8333-444455556666'

        def rollout(uuid, records):
            path = a.rollout(uuid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(line(r) for r in records), encoding='utf-8')

        def user_item(text, when):
            return {'timestamp': when, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'type': 'UserMessage', 'id': 'i', 'content': [{'type': 'text', 'text': text}]}}}

        def response(text, when):
            return {'timestamp': when, 'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}

        rollout(UUID_A, [   # current Codex apps
            {'type': 'session_meta', 'payload': {'id': UUID_A, 'cwd': '/work/app'}},
            response('<environment_context>\n  <cwd>/work/app</cwd>\n</environment_context>', '2026-11-20T01:00:00Z'),
            user_item('<in-app-browser-context source="ambient-ui-state">\nurl\n</in-app-browser-context>\n\n'
                      '# Files mentioned by the user:\n\n## spec.pdf: /tmp/spec.pdf\n\n## My request:\n'
                      'Summarise the spec', '2026-11-20T01:00:01Z'),
            response('Summarise the spec', '2026-11-20T01:00:01Z'),
            user_item('<send_user_message_question_reply>\n[{"question": "Which one?", "answer": "The first"}]\n'
                      '</send_user_message_question_reply>', '2026-11-20T01:05:00Z'),
            {'timestamp': '2026-11-20T01:05:02Z', 'type': 'event_msg', 'payload': {
                'type': 'item_completed', 'item': {'type': 'AgentMessage', 'content': [{'type': 'text', 'text': 'no'}]}}},
        ])
        rollout(UUID_B, [   # older CLI versions
            {'type': 'session_meta', 'payload': {'id': UUID_B, 'cwd': '/work/cli'}},
            {'timestamp': '2026-11-21T01:00:00Z', 'type': 'event_msg',
             'payload': {'type': 'user_message', 'message': 'old style', 'images': ['data:']}},
            response('old style', '2026-11-21T01:00:00Z'),
        ])
        rollout(uuid_c, [   # oldest: prompts exist only as response items
            {'type': 'session_meta', 'payload': {'id': uuid_c, 'cwd': '/work/old'}},
            response('# AGENTS.md instructions for /work/old\n\nbe brief', '2026-11-22T01:00:00Z'),
            response('<user_instructions>x</user_instructions>', '2026-11-22T01:00:00Z'),
            response('very old prompt', '2026-11-22T01:00:01Z'),
        ])
        (a.codex / 'history.jsonl').write_text(line({'session_id': UUID_A, 'ts': 1, 'text': 'index copy'}))
        a.once()
        got = prompts.query(a.client, limit=0)['prompts']
        self.assertEqual([(p['text'], p['cwd'], p['files'], p['images']) for p in got], [
            ('very old prompt', '/work/old', [], 0),
            ('old style', '/work/cli', [], 1),
            ('Q: Which one?\nA: The first', '/work/app', [], 0),
            ('Summarise the spec', '/work/app', ['spec.pdf'], 0)])

    def test_markdown_rendering(self):
        p = {'t': '2026-11-20T01:00:00+00:00', 'agent': 'codex', 'device': 'mac', 'cwd': '/a/proj',
             'session': UUID_A, 'text': 'see <script>\nline\\\nnext\n```\nx < y\n'}
        long = dict(p, t='2026-11-20T02:00:00+00:00', text='\n'.join('line %d' % i for i in range(30)))
        pasted = dict(p, t='2026-11-21T02:00:00+00:00', text='look:\n<pasted_content id="1">\nhello\n</pasted_content>')
        docs = prompts.render([p, long, pasted], 'My Mac', [('dsw', 'DSW')], tz=datetime.timezone.utc)
        month = docs['2026-11.md']
        self.assertIn('**01:00** · Codex · proj · <sub>0199aaaa</sub>\n\n> see &lt;script&gt;  \n> line\\\n> next  \n'
                      '> ```\n> x < y\n> ```\n', month)               # code untouched, open fence closed
        self.assertIn('<details><summary>line 0 (30 lines)</summary>\n\n> line 0  \n', month)
        self.assertIn('> look:\n>\n> _Pasted:_\n>\n> > hello', month)
        self.assertLess(month.index('2026-11-21 Saturday'), month.index('2026-11-20 Friday'))
        readme = docs['README.md']
        self.assertIn('**3 prompts** in 1 sessions, 2026-11-20 to 2026-11-21.', readme)
        self.assertIn('Other devices: [DSW](../../dsw/prompts/README.md)', readme)
        self.assertIn('| [2026-11](2026-11.md) | 3 | 3 |', readme)
        self.assertIn('- [2026-11-21 02:00](2026-11.md#2026-11-21-saturday) · Codex · proj · look: \\[pasted text\\]',
                      readme)

    def test_prompts_command_searches_every_device(self):
        mac, dsw = self.env('mac'), self.env('dsw')
        self.claude_session(mac, UUID_B, [self.typed('Fix the login bug', '2026-11-20T01:00:00Z')])
        mac.once()
        path = dsw.rollout(UUID_A)
        path.parent.mkdir(parents=True)
        path.write_text(line({'type': 'session_meta', 'payload': {'id': UUID_A, 'cwd': '/srv/app'}}) +
                        line({'timestamp': '2026-11-21T01:00:00Z', 'type': 'event_msg',
                              'payload': {'type': 'user_message', 'message': 'Deploy the fix'}}))
        dsw.once()
        mac.client.pull()
        result = prompts.query(mac.client, grep='FIX')
        self.assertEqual([(p['device'], p['agent']) for p in result['prompts']], [('dsw', 'codex'), ('mac', 'claude-code')])
        self.assertEqual(prompts.query(mac.client, device='mac', since='2026-11-22')['prompts'], [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['--state-dir', str(mac.client.state), 'prompts', '--session', UUID_A[:8], '--markdown'])
        self.assertIn('· Codex · dsw · app · <sub>0199aaaa</sub>\n\n> Deploy the fix', out.getvalue())
        readme = (mac.client.repo / 'devices/mac/prompts/README.md').read_text(encoding='utf-8')
        self.assertNotIn('Other devices', readme)                  # dsw had no prompt files yet
        mac.once()
        readme = (mac.client.repo / 'devices/mac/prompts/README.md').read_text(encoding='utf-8')
        self.assertIn('Other devices: [dsw](../../dsw/prompts/README.md)', readme)
        with self.assertRaises(ValueError):
            prompts.query(mac.client, since='last week')

    def test_a_failing_prompt_list_never_stops_the_backup(self):
        a = self.env('mac')
        self.claude_session(a, UUID_B, [self.typed('hello', '2026-11-20T01:00:00Z')])
        with mock.patch.object(prompts, 'write_docs', side_effect=KeyError('broken')):
            result = a.once()
        self.assertEqual((result['committed'], result['sync']['github']), (True, 'pushed'))
        self.assertIn('error', result['prompt_docs'])
        (a.client.local / 'prompts-cache.json').write_text('{"version": 1, "files": {"x": 1}}')
        self.assertEqual(a.once()['prompt_docs']['prompts'], 1)    # a damaged cache is rebuilt

    def test_prompt_docs_can_be_turned_off(self):
        a = self.env('mac')
        a.client.cfg['prompt_docs'] = False
        self.claude_session(a, UUID_B, [self.typed('hello', '2026-11-20T01:00:00Z')])
        self.assertNotIn('prompt_docs', a.once())
        self.assertFalse((a.client.repo / 'devices/mac/prompts').exists())


class UnitTests(unittest.TestCase):
    def test_secret_patterns(self):
        text = ('{"a": "x\\n%s", "b": "Bearer %s", "c": "risk-assessment-' % (FAKE_GITHUB, FAKE_ANTHROPIC)
                + 'y' * 40 + '"}\n').encode()
        out, counts = secrets.redact(text)
        self.assertEqual(counts, {'github_token': 1, 'anthropic_key': 1})
        json.loads(out)
        self.assertIn(b'risk-assessment', out)
        self.assertEqual(secrets.redact(b'nothing here')[1], {})
        key = b'-----BEGIN OPENSSH ' + b'PRIVATE KEY-----\\nabc\\n-----END OPENSSH ' + b'PRIVATE KEY-----'
        self.assertEqual(secrets.find(key), {'private_key': 1})

    def test_sqlite_redaction_skips_virtual_tables(self):
        db = sqlite3.connect(':memory:')
        db.execute('create table t (id integer primary key, body text, n integer)')
        db.execute('insert into t (body, n) values (?, 1)', ('see ' + FAKE_GITHUB,))
        try:
            db.execute('create virtual table f using fts5(body)')
            db.execute('insert into f (body) values (?)', ('plain words',))
        except sqlite3.OperationalError:
            pass
        self.assertEqual(redact_sqlite(db), {'github_token': 1})
        self.assertEqual(db.execute('select body from t').fetchone()[0], 'see [REDACTED:github_token]')

    def test_path_helpers(self):
        self.assertEqual(remap_path('/a/b/c', [('/a/b', '/x')]), '/x/c')
        self.assertEqual(remap_path('/a/bc', [('/a/b', '/x')]), '/a/bc')
        self.assertEqual(remap_path('C:\\work\\p', [('C:\\work', '/srv')]), '/srv/p')
        self.assertEqual(encode_project_dir('/Users/me/my.proj'), '-Users-me-my-proj')
        self.assertEqual(project_key('/Users/me/my proj'), '--Users-me-my~0020proj--')
        self.assertEqual(project_key('C:\\work\\p'), '--C-work-p--')
        self.assertEqual(project_key('/'), '--root--')


if __name__ == '__main__':
    unittest.main()
