# Setup guide for coding agents

You are a coding agent, and the user asked you to set up git-agent-history on this machine,
or to restore history with it. Carry out the steps; do not only explain them. Keep
existing environments and data intact.

## Rules

- **Never ask for, read, print or store tokens.** If GitHub login is needed, run
  `gh auth login --hostname github.com --web` and let the user finish in the browser.
- **The history repository must be private.** Do not pass `--trust-remote` unless the
  user explicitly confirms that a non-GitHub remote is private.
- **One state directory per environment.** If a machine has several agent homes, for
  example two `CODEX_HOME`s, each one gets its own `--state-dir` and device ID. Never copy
  another machine's state directory.
- **Never** force-push, rewrite history, delete anything under `devices/`, or edit
  another device's folder.
- **Do not restore** unless the user asks for it.

## 1. Prerequisites

Check `python3 --version` (3.9+), `git --version` and `gh --version`. Install what is
missing with the system package manager, or ask the user if you lack permission. Then:

```sh
gh config set git_protocol https --host github.com
gh auth status || gh auth login --hostname github.com --web
gh auth setup-git
```

## 2. Install

Prefer `pipx install git+https://github.com/mzhangzhicheng/git-agent-history`. If pipx is
unavailable, clone the repository to a persistent folder and run
`<clone>/bin/git-agent-history`. Run `git-agent-history --help` and the `--help` of each
subcommand you use; do not guess flags.

## 3. Choose the state directory

- Laptop or desktop: use the default (`~/.git-agent-history`).
- Container or cloud instance: use a folder on the persistent disk. Ask the user which
  path survives a rebuild if it is not obvious. Then pass `--state-dir <path>` to
  **every** command, or export `GIT_AGENT_HISTORY_HOME=<path>`.

If `<state-dir>/config.json` already exists, this environment is registered. Skip `init`
and keep its device ID.

## 4. Register

```sh
git-agent-history init --repo OWNER/NAME --label "<readable name>"
```

- Use the repository the user named. Add `--create` only if the user wants it created.
- Leave out `--device` so that a unique ID is generated, unless the user wants a
  specific name.
- If an agent home is not in its default place, pass `--codex-home` or `--claude-home`
  (for example the user's `CODEX_HOME` or `CLAUDE_CONFIG_DIR`).
- Keep the default `--secrets redact` unless the user decides otherwise.

## 5. First backup and background worker

```sh
git-agent-history once        # expect "github": "pushed"
git-agent-history start
git-agent-history status      # expect worker_alive: true and a recent last_push
```

Report any `errors` listed per agent in the output, and the counts under
`secrets_redacted`. Never print the secret values themselves.

## 6. Start automatically

- macOS, Windows, or Linux with systemd: `git-agent-history autostart`.
- Containers without systemd: run `git-agent-history startup-command` and tell the user
  exactly where to paste the printed command (the platform's start-up script setting).
  Check that the state directory and the printed paths are on the persistent disk.

## 7. Restore (only on request)

1. `git-agent-history pull`, then `devices`, then `sessions --device <id> --agent <agent>`.
2. Prefer `export --target <empty folder>` first and show the user what is there.
3. For a real restore, use `restore --from-device <id> --agent <agent> --target <agent home>`.
   - Use `--map-cwd OLD=NEW` when project folders moved, for example `/Users/a=/home/a`.
   - Use `--session <id>` to restore a single conversation.
   - Use `--with-database` (Codex only) only for an empty target, and only if the user
     wants the saved index.
   - Use `--dry-run` first when unsure.
4. Report `restored`, `conflicts_kept` and `index_skipped_while_running`. Tell the user
   to restart the agent afterwards.
