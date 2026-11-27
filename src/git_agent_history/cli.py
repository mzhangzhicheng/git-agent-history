"""Command-line interface. Every command prints JSON so that people and agents can read it."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys

from . import __version__, adapters, handoff, prompts, service
from .client import Client, describe_error
from .secrets import MODES
from .util import file_lock, write_json


class _Text(str):
    """A result printed as plain text instead of JSON."""


def _body_args(parser, required=False):
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument('--message', '-m', help='the text (Markdown)')
    group.add_argument('--file', help='read the text from a file, or from standard input with -')


def _body(args):
    if args.file == '-':
        return sys.stdin.read()
    if args.file:
        with open(args.file, encoding='utf-8') as f:
            return f.read()
    return args.message

DESCRIPTION = ('Back up, sync and restore coding-agent conversation history (Codex, Claude Code, '
               'DeepSeek Harness) '
               'through a private Git repository, one folder per device.')


def _mappings(values):
    pairs = []
    for value in values:
        old, sep, new = value.partition('=')
        if not sep or not old or not new:
            raise ValueError('Use --map-cwd OLD=NEW')
        pairs.append((old, new))
    return pairs


def build_parser():
    parser = argparse.ArgumentParser(prog='git-agent-history', description=DESCRIPTION)
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    parser.add_argument('--state-dir', help='per-environment state folder (default: $GIT_AGENT_HISTORY_HOME '
                                            'or ~/.git-agent-history)')
    sub = parser.add_subparsers(dest='action', metavar='command')
    sub.required = True

    p = sub.add_parser('init', help='register this environment and clone the private history repository')
    p.add_argument('--repo', help='OWNER/NAME on GitHub, or any git URL')
    p.add_argument('--device', help='device ID (default: hostname plus a random suffix)')
    p.add_argument('--label', help='human-readable name for this environment')
    p.add_argument('--agents', help='comma-separated: ' + ', '.join(sorted(adapters.ADAPTERS)) +
                                    ' (default: every agent whose home exists)')
    p.add_argument('--codex-home', help='Codex home (default: $CODEX_HOME or ~/.codex)')
    p.add_argument('--claude-home', help='Claude Code home (default: $CLAUDE_CONFIG_DIR or ~/.claude)')
    p.add_argument('--dsh-home', help='DeepSeek Harness home (default: $DSH_HOME or ~/.dsh)')
    p.add_argument('--secrets', choices=MODES, default='redact',
                   help='redact known credential formats before committing (default), only count them, or off')
    p.add_argument('--create', action='store_true', help='create the private GitHub repository if missing')
    p.add_argument('--offline', action='store_true', help='commit locally only; enable pushing later with connect')
    p.add_argument('--trust-remote', action='store_true', help='allow a non-GitHub remote you know is private')

    sub.add_parser('once', help='checkpoint now and push')
    sub.add_parser('run', help='run the backup loop in the foreground')
    sub.add_parser('start', help='start the backup loop in the background')
    sub.add_parser('stop', help='stop the background loop')
    sub.add_parser('status', help='show last checkpoint/push and worker state')
    sub.add_parser('autostart', help='install a login service (launchd, systemd --user, Task Scheduler)')
    sub.add_parser('startup-command', help='print the command for a container start-up hook')
    sub.add_parser('startup', help='container hook: refill an empty agent home from this device, then start')
    sub.add_parser('connect', help='verify privacy and enable pushing')
    sub.add_parser('disable', help='stop pushing (local commits continue)')
    sub.add_parser('pull', help='fetch other devices\' history')
    sub.add_parser('devices', help='list devices in the history repository')
    sub.add_parser('scan', help='count credential-like strings in local history (values are never printed)')

    p = sub.add_parser('sessions', help='list stored sessions of a device')
    p.add_argument('--device', help='device ID (default: this environment)')
    p.add_argument('--agent', choices=sorted(adapters.ADAPTERS))

    p = sub.add_parser('prompts', help='search the prompts you typed, on every device, newest first')
    p.add_argument('--device', help='only this device ID (default: every device; run `pull` first)')
    p.add_argument('--agent', choices=sorted(adapters.ADAPTERS))
    p.add_argument('--session', help='only this session (an ID prefix is enough)')
    p.add_argument('--since', metavar='YYYY-MM-DD', help='only prompts from this local date on')
    p.add_argument('--grep', metavar='TEXT', help='only prompts containing TEXT (ignores case)')
    p.add_argument('--limit', type=int, default=50, help='at most N prompts; 0 for all (default: 50)')
    p.add_argument('--markdown', action='store_true', help='print readable Markdown instead of JSON')

    for name, text in (('restore', 'restore a device\'s history into an agent home'),
                       ('export', 'same as restore; use it to unpack into a separate folder')):
        p = sub.add_parser(name, help=text)
        p.add_argument('--from-device', required=True)
        p.add_argument('--agent', required=True, choices=sorted(adapters.ADAPTERS))
        p.add_argument('--target', required=True, help='agent home or an empty folder')
        p.add_argument('--session', help='only files of this session ID')
        p.add_argument('--with-database', action='store_true',
                       help='also restore the saved SQLite index (target must have none)')
        p.add_argument('--map-cwd', action='append', default=[], metavar='OLD=NEW',
                       help='rewrite project paths that moved (repeatable)')
        p.add_argument('--dry-run', action='store_true')

    p = sub.add_parser('handoff', help='pass tasks and context between agents and devices')
    hs = p.add_subparsers(dest='handoff_action', metavar='subcommand')
    hs.required = True
    participants = list(handoff.PARTICIPANTS)
    q = hs.add_parser('new', help='start a task; the text is its spec (default: a template to fill in)')
    q.add_argument('--title', required=True)
    q.add_argument('--from', dest='sender', required=True, choices=participants)
    q.add_argument('--to', required=True, choices=participants, help='who works on it first')
    q.add_argument('--id', dest='task', help='task ID (default: date, title and a random suffix)')
    _body_args(q)
    q = hs.add_parser('post', help='add a note, or a new spec, to a task and optionally pass the turn')
    q.add_argument('task')
    q.add_argument('--from', dest='sender', required=True, choices=participants)
    q.add_argument('--to', choices=participants, help='whose turn it is next (default: unchanged)')
    q.add_argument('--status', choices=handoff.STATUSES, help='new status (default: unchanged)')
    q.add_argument('--spec', action='store_true', help="the text replaces the task's spec")
    _body_args(q, required=True)
    q = hs.add_parser('list', help='list tasks from all devices, most recently updated first')
    q.add_argument('--for', dest='for_agent', choices=participants, help='only tasks whose turn is theirs')
    q.add_argument('--all', action='store_true', help='include done tasks')
    q = hs.add_parser('show', help="show a task's spec and log")
    q.add_argument('task')
    q.add_argument('--markdown', action='store_true', help='print readable Markdown instead of JSON')
    return parser


def _handoff(client, args):
    action = args.handoff_action
    if action == 'new':
        return handoff.new(client, args.title, args.sender, args.to, _body(args), args.task)
    if action == 'post':
        return handoff.post(client, args.task, args.sender, _body(args), args.to, args.status, args.spec)
    if action == 'list':
        return handoff.list_tasks(client, args.for_agent, args.all)
    result = handoff.show(client, args.task)
    return _Text(handoff.render(result)) if args.markdown else result


def dispatch(args):
    client = Client(args.state_dir)
    action = args.action
    if action == 'init':
        homes = {'codex': args.codex_home, 'claude-code': args.claude_home, 'deepseek-harness': args.dsh_home}
        agents = [a.strip() for a in args.agents.split(',') if a.strip()] if args.agents else None
        return client.init(args.repo, args.device, args.label, agents, {k: v for k, v in homes.items() if v},
                           args.secrets, args.offline, args.create, args.trust_remote)
    if action == 'devices':
        return client.devices()
    if action in ('restore', 'export'):
        with file_lock(client.runtime / 'write.lock', wait=True):
            return client.restore(args.from_device, args.agent, args.target, args.with_database,
                                  _mappings(args.map_cwd), args.session, args.dry_run)
    client.require_config()
    if action == 'sessions':
        return client.sessions(args.device or client.device, args.agent)
    if action == 'prompts':
        with file_lock(client.runtime / 'write.lock', wait=True):
            result = prompts.query(client, args.device, args.agent, args.session, args.since, args.grep,
                                   args.limit, args.markdown)
        return _Text(result.rstrip('\n')) if args.markdown else result
    if action == 'once':
        return service.once(client)
    if action == 'run':
        service.daemon(client)
        return None
    if action == 'start':
        return service.start(client)
    if action == 'stop':
        return service.stop(client)
    if action == 'status':
        return client.status()
    if action == 'autostart':
        return service.autostart(client)
    if action == 'startup-command':
        return service.startup_command(client)
    if action == 'startup':
        return service.startup(client)
    if action == 'pull':
        with file_lock(client.runtime / 'write.lock', wait=True):
            return client.pull()
    if action == 'scan':
        return client.scan()
    if action == 'handoff':
        return _handoff(client, args)
    if action == 'connect':
        client.verify_private()
        client.cfg['push'] = True
        write_json(client.config_path, client.cfg)
        return service.once(client)
    if action == 'disable':
        client.cfg['push'] = False
        write_json(client.config_path, client.cfg)
        return {'push': False}
    raise ValueError('Unknown command ' + action)


def main(argv=None):
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        result = dispatch(args)
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(describe_error(exc), file=sys.stderr)
        sys.exit(1)
    if isinstance(result, _Text):
        print(result)
    elif result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
