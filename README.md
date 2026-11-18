# git-agent-history

**Back up, sync and restore your Codex / Claude Code / DeepSeek Harness conversations with one private Git repository.**

[简体中文](README.zh-CN.md)

Coding agents keep their history only on the machine they ran on. A new laptop, a
rebuilt container, or the agent's own cleanup wipes it out: Claude Code, for example,
keeps transcripts for only [30 days](https://code.claude.com/docs/en/settings-reference#cleanupperioddays) by default.
`git-agent-history` runs in the background, backs them up to a private GitHub repository you own,
and restores them on any machine.

- **Just a private Git repo.** No server and no account.
- **One folder per device.** Devices never conflict, and only new content is uploaded.
- **Native files.** Restored sessions can be resumed directly, and old project paths can be mapped to new ones.
- **Safe defaults.** It refuses public repos, redacts common credentials before committing, and never overwrites existing files when restoring.

## 🤖 Let your agent set it up (recommended)

On the machine you want to back up, paste this into Codex, Claude Code or DeepSeek Harness:

> Set up https://github.com/mzhangzhicheng/git-agent-history on this machine so that my
> agent conversations are backed up automatically to my private GitHub repository
> `YOUR_NAME/agent-history` (create it if missing). Follow `docs/agent-setup.md` in that
> repository and enable autostart. If GitHub login is needed, let me finish it in the
> browser; never ask for or print tokens.

Do this once on each machine. You can also install it as a skill with `npx skills add mzhangzhicheng/git-agent-history`.

## Manual setup

Requires Python 3.9+, git and the [GitHub CLI](https://cli.github.com/).

```sh
gh auth login --web && gh auth setup-git
pipx install git+https://github.com/mzhangzhicheng/git-agent-history

git-agent-history init --repo YOUR_NAME/agent-history --create   # register this machine
git-agent-history once        # back up now
git-agent-history autostart   # run in the background and on login; syncs every 5 minutes
git-agent-history status
```

**Cloud containers.** First `export GIT_AGENT_HISTORY_HOME=/persistent/disk/.git-agent-history`.
Then add the command printed by `git-agent-history startup-command` to the platform's start-up script.
After every rebuild, it refills this machine's history from the backup and keeps syncing.

## Restore

```sh
git-agent-history pull && git-agent-history devices          # list devices
git-agent-history export  --from-device DEVICE --agent codex --target ./inspect   # look first
git-agent-history restore --from-device DEVICE --agent claude-code --target ~/.claude \
  --map-cwd /Users/me/code=/home/me/code                     # if project paths changed
```

- Existing files are never overwritten. Add `--session ID` to restore a single conversation.
- After restoring into a running agent's home, restart the agent.

## Supported agents

| Agent | What is backed up |
|---|---|
| Codex | sessions, session index and a `state_*.sqlite` snapshot from `~/.codex` |
| Claude Code | sessions (including subagents) from `~/.claude/projects`, and `history.jsonl` |
| DeepSeek Harness | session logs from `~/.dsh/sessions` |

Credentials, settings, plugins, attachments and project code are **not** backed up.

## Security

- Every upload first checks that the repository is private.
- Common credentials (OpenAI, Anthropic, GitHub, AWS and other tokens, private keys) are replaced with `[REDACTED]` before committing. This is a safety net; keep the repo private.
- The repository is not encrypted. Anyone with access to it can read the conversations.

## Related projects

- [Entire CLI](https://github.com/entireio/cli) and [SpecStory](https://github.com/specstoryai/getspecstory) record agent sessions inside each project repository.
- [claude-code-sync](https://github.com/perfectra1n/claude-code-sync), [claude-sync](https://github.com/tawanorg/claude-sync) and [repo-sessions](https://github.com/firish/repo-sessions) sync Claude Code (and Codex) sessions.
- [agentctxsync](https://github.com/westsource/agentctxsync) is a self-hosted server that shares one session pool between agents.
- [cli-continues](https://github.com/yigitkonur/cli-continues) and [cross_agent_session_resumer](https://github.com/Dicklesworthstone/cross_agent_session_resumer) hand a session over to another agent.

This project does one thing: back up every agent session on every machine, and it needs nothing but a private Git repository.

See [docs/design.md](docs/design.md) for details.

## License

[MIT](LICENSE) © 2026 Zhicheng Zhang. Codex, Claude Code and DeepSeek Harness belong to OpenAI, Anthropic and DeepSeek respectively. This project is independent and not affiliated with them.
