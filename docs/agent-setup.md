# Setup guide for coding agents

The user asked you to set up git-agent-history on this machine, or to restore history
with it. Do the steps yourself instead of only explaining them. Every command prints JSON.

## Rules

- **Never ask for, print or store tokens.** If GitHub login is needed, run `gh auth login --hostname github.com --web` and let the user finish it in the browser.
- **Keep the history repository private.** Do not use `--trust-remote` unless the user confirms that a non-GitHub remote is private.
- **One state directory per environment.** Never copy another machine's state directory. If `<state-dir>/config.json` exists, the machine is already registered: skip `init`.
- **Leave other data alone.** Never force-push, rewrite history, or touch other devices' folders under `devices/`.
- **Restore only when asked.**

## Set up

1. **Check the tools.** Make sure Python 3.9+, `git` and `gh` are installed; install missing ones if you can. Then run `gh auth status || gh auth login --hostname github.com --web` and `gh auth setup-git`.
2. **Install.** Run `pipx install git+https://github.com/mzhangzhicheng/git-agent-history`. If pipx is missing, clone the repository to a persistent folder and use `<clone>/bin/git-agent-history`. Check `--help` before using a flag.
3. **On a container or cloud instance,** put the state directory on the persistent disk. Ask the user if unsure which path survives a rebuild. Then run `export GIT_AGENT_HISTORY_HOME=<persistent path>/.git-agent-history`, or pass `--state-dir` to every command.
4. **Register this machine.** Run `git-agent-history init --repo OWNER/NAME --label "<readable name>"`.
   - Add `--create` if the repository does not exist yet.
   - Let the device ID be generated.
   - Codex, Claude Code and DeepSeek Harness are detected in their default homes. If an agent lives elsewhere, pass `--codex-home`, `--claude-home` or `--dsh-home`. Use `CODEX_HOME`, `CLAUDE_CONFIG_DIR` or `DSH_HOME` if they are set.
5. **Run the first backup.** Run `git-agent-history once` and expect `"github": "pushed"`. With a large history it can upload hundreds of MB and take many minutes; if your shell limits command time, run it in the background instead of letting it be killed. Report any per-agent `errors` and the counts under `secrets_redacted`, but never the values.
6. **Keep it running.**
   - With launchd, systemd or Windows: run `git-agent-history autostart`, then `status`, and expect `worker_alive: true`.
   - In a container without systemd: run `git-agent-history start`, then `startup-command`. Tell the user exactly where to paste the printed command (the platform's start-up script), and check that the printed paths are on the persistent disk.

## Restore

1. Run `git-agent-history pull`, then `devices`, then `sessions --device <id> --agent <agent>`. The agent is one of `codex`, `claude-code` or `deepseek-harness`.
2. Prefer `export --target <empty folder>` first, and show the user what is there.
3. Run `restore --from-device <id> --agent <agent> --target <agent home>`. Use `--map-cwd OLD=NEW` when project folders moved, `--session <id>` for one conversation, and `--dry-run` when unsure. Use `--with-database` (Codex only) only for an empty target.
4. Report `restored` and `conflicts_kept`. Ask the user to restart the agent.
