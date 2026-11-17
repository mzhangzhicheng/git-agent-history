"""Background worker, start/stop, and login/startup integration."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time

from . import adapters
from .util import file_lock, now, read_json, run, write_json

_ERRORS = (BlockingIOError, OSError, ValueError, subprocess.SubprocessError, sqlite3.Error)


def worker_alive(client) -> bool:
    try:
        with file_lock(client.runtime / 'daemon.lock'):
            return False
    except (BlockingIOError, OSError):
        return True


def once(client):
    with file_lock(client.runtime / 'write.lock', wait=True):
        result = client.checkpoint()
        if client.cfg.get('push'):
            result['sync'] = client.sync()
        return result


def daemon(client):
    """Checkpoint and push every `interval` seconds until a stop request appears."""
    client.require_config()
    try:
        guard = file_lock(client.runtime / 'daemon.lock')
        guard.__enter__()
    except (BlockingIOError, OSError):
        return  # another worker already serves this state directory
    stop_file = client.local / 'stop-request'
    try:
        write_json(client.local / 'worker.json', {'pid': os.getpid(), 'started_at': now()})
        next_run = 0.0
        while not stop_file.exists():
            if time.monotonic() >= next_run:
                try:
                    client.cfg = read_json(client.config_path)  # pick up connect/disable without restart
                    once(client)
                except _ERRORS as exc:
                    client.record_error(exc)
                next_run = time.monotonic() + max(30, int(client.cfg.get('interval', 300)))
            time.sleep(1)
    finally:
        guard.__exit__(None, None, None)


def command(client, action='run'):
    launcher = client.write_launcher()
    return [sys.executable, str(launcher), '--state-dir', str(client.state), action]


def start(client):
    client.require_config()
    client.local.mkdir(parents=True, exist_ok=True)
    (client.local / 'stop-request').unlink(missing_ok=True)
    options = {'stdin': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options['start_new_session'] = True
    with (client.local / 'worker.log').open('a', encoding='utf-8') as log:
        proc = subprocess.Popen(command(client), stdout=log, stderr=log, **options)
    return {'requested_pid': proc.pid, 'device': client.device}


def stop(client):
    client.require_config()
    client.local.mkdir(parents=True, exist_ok=True)
    request = client.local / 'stop-request'
    request.touch()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not worker_alive(client):
                return {'stopped': True}
            time.sleep(0.25)
        raise ValueError('Worker still busy; try again after the current upload finishes')
    finally:
        request.unlink(missing_ok=True)


def startup_command(client):
    cmd = command(client, 'startup')
    return subprocess.list2cmdline(cmd) if os.name == 'nt' else shlex.join(cmd)


def startup(client):
    """Container start hook: refill an empty agent home from this device's own backup, then start."""
    client.require_config()
    restored = {}
    with file_lock(client.runtime / 'write.lock', wait=True):
        for name, agent_cfg in client.cfg['agents'].items():
            adapter, home = adapters.get(name), Path(agent_cfg['home'])
            if home.is_dir() and adapter.has_transcripts(home):
                continue
            try:
                client.load_agent(client.device, name)
            except ValueError:
                continue  # nothing backed up yet for this agent
            with_db = bool(getattr(adapter, 'valid_database_name', None)) and not adapter.databases(home)
            restored[name] = client.restore(client.device, name, home, with_database=with_db)
    return {'restored': restored, 'worker': start(client)}


def autostart(client):
    client.require_config()
    if sys.platform != 'darwin' and os.name != 'nt' and not Path('/run/systemd/system').exists():
        raise ValueError('No systemd here (typical for containers). Put the output of '
                         '`startup-command` into your platform\'s start-up script instead.')
    if worker_alive(client):
        stop(client)
    cmd = command(client)
    name = 'git-agent-history-' + client.device
    if sys.platform == 'darwin':
        import plistlib
        label = 'local.' + name
        path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({
            'Label': label, 'ProgramArguments': cmd, 'RunAtLoad': True, 'KeepAlive': True,
            'EnvironmentVariables': {'PATH': os.environ.get('PATH', '')},
            'StandardOutPath': str(client.local / 'service.log'),
            'StandardErrorPath': str(client.local / 'service.log')}))
        run(['launchctl', 'bootout', 'gui/%d' % os.getuid(), path], check=False)
        run(['launchctl', 'bootstrap', 'gui/%d' % os.getuid(), path])
        return {'installed': str(path), 'scope': 'macOS LaunchAgent (user login)'}
    if os.name == 'nt':
        run(['schtasks', '/Create', '/F', '/SC', 'ONLOGON', '/TN', name, '/TR', subprocess.list2cmdline(cmd)])
        start(client)
        return {'installed': name, 'scope': 'Windows scheduled task (user logon)'}
    path = Path.home() / '.config/systemd/user' / (name + '.service')
    path.parent.mkdir(parents=True, exist_ok=True)
    quoted = ' '.join('"%s"' % x.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') for x in cmd)
    path.write_text('[Unit]\nDescription=git-agent-history backup (%s)\nAfter=network-online.target\n'
                    '[Service]\nType=simple\nExecStart=%s\nRestart=on-failure\nRestartSec=15\n'
                    'Environment="PATH=%s"\n[Install]\nWantedBy=default.target\n'
                    % (client.device, quoted, os.environ.get('PATH', '').replace('%', '%%')),
                    encoding='utf-8')
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', '--now', name + '.service'])
    return {'installed': str(path),
            'scope': 'systemd user service; run `loginctl enable-linger` to keep it running after logout'}
