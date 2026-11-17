# git-agent-history

**Git-backed backup, sync and restore for coding-agent conversations: Codex and Claude Code today, more agents planned.**

[简体中文](README.zh-CN.md) · [Design](docs/design.md) · [Agent setup guide](docs/agent-setup.md) · [Adding an agent](docs/adding-an-agent.md)

Coding agents keep their conversation history on the local disk of whatever machine
they ran on. That history disappears when a cloud container is rebuilt, when a laptop
is replaced, or when the agent cleans up after itself. For example, Claude Code deletes
transcripts older than [`cleanupPeriodDays`](https://code.claude.com/docs/en/settings-reference#cleanupperioddays),
which defaults to 30 days.

`git-agent-history` copies those conversations into **one private Git repository that
you own**. Every computer, server or container writes to its own `devices/<device-id>/`
folder, so environments never overwrite each other. You can restore any device's
history to any other machine, and remap project paths along the way.

- **Git only.** You need a private GitHub repository (or any git remote you trust). There is no server, database or account to run.
- **One folder per device.** Each environment only commits under its own folder. Concurrent pushes rebase and retry, and nothing is force-pushed.
- **Whole-machine backup.** The tool backs up the agent's entire history, not one project, so it still works after a container or disk is wiped.
- **Native format.** Restored files are the agent's own files (`*.jsonl`, `*.jsonl.zst`, Codex's SQLite index). Agents can resume them directly. No Markdown export is involved.
- **Compact history.** Transcripts are append-only, so each checkpoint stores only the new lines. A long session is not re-uploaded every few minutes.
- **Safe by default.** The tool refuses public repositories. Known credential formats are redacted before commit, and existing local files are never overwritten on restore.
- **Agent-friendly.** Every command prints JSON. [`docs/agent-setup.md`](docs/agent-setup.md) and a [skill](skills/git-agent-history/SKILL.md) let your coding agent do the setup for you.
- **No dependencies.** Python 3.9+ standard library, `git`, and the GitHub CLI (`gh`) for the privacy check.

> Status: **v0.1, early.** The test suite runs in CI on Linux, macOS and Windows with Python
> 3.9–3.14. End-to-end trials with real Codex 0.169 and Claude Code 2.1 history have so far
> been run on macOS. Please report problems with your agent version in an issue.

## Supported agents

| Agent | What is backed up | Status |
|---|---|---|
| [Codex](https://developers.openai.com/codex) (CLI, IDE, app) | `sessions/`, `archived_sessions/` (`.jsonl` and `.jsonl.zst`), `session_index.jsonl`, `history.jsonl`, snapshot of `state_*.sqlite` | ✅ v0.1 |
| [Claude Code](https://code.claude.com/docs) | `projects/*/<session>.jsonl`, subagent transcripts and metadata, text tool results, `history.jsonl` | ✅ v0.1 |
| DeepSeek Harness (`dsh`) | session logs | planned |
| OpenCode, Gemini CLI | session storage | planned |

The tool never touches credentials and configuration: `auth.json`, `.credentials.json`,
`config.toml`, `settings.json`, plugins, logs and caches are not read. See
[What is not backed up](#what-is-not-backed-up).

## Quick start

```sh
# 1. Tools: Python 3.9+, git, GitHub CLI
gh auth login --web && gh auth setup-git

# 2. Install (PyPI release coming; until then from GitHub)
pipx install git+https://github.com/mzhangzhicheng/git-agent-history
#    ...or run it from a checkout without installing:
#    git clone https://github.com/mzhangzhicheng/git-agent-history && alias git-agent-history="$PWD/git-agent-history/bin/git-agent-history"

# 3. Register this machine; --create makes the private repo if it does not exist
git-agent-history init --repo YOUR_NAME/agent-history --create --label "work laptop"

# 4. First backup, then keep running in the background
git-agent-history once
git-agent-history start
git-agent-history autostart      # launchd / systemd --user / Task Scheduler
git-agent-history status
```

`init` detects which agents are installed (`$CODEX_HOME` or `~/.codex`, and
`$CLAUDE_CONFIG_DIR` or `~/.claude`). Use `--agents codex,claude-code`,
`--codex-home` or `--claude-home` to choose explicitly. By default the worker takes a
checkpoint and pushes every 5 minutes. Because git puts it on the `PATH`, you can also
type `git agent-history ...`.

### Let your coding agent set it up

Paste this into Codex, Claude Code or another agent on the new machine:

> Set up https://github.com/mzhangzhicheng/git-agent-history on this machine so that my
> coding-agent conversations are backed up to my private GitHub repository
> `YOUR_NAME/agent-history`. Read `docs/agent-setup.md` in that repository and follow it.
> If GitHub login is needed, let me finish it in the browser; never ask for or print tokens.

You can also install it as a skill with `npx skills add mzhangzhicheng/git-agent-history`.

### Containers and cloud GPU instances

Container images often lack systemd and lose `$HOME` on rebuild. In that case:

1. Put the state directory on the persistent disk: `export GIT_AGENT_HISTORY_HOME=/mnt/data/.git-agent-history`, or pass `--state-dir`.
2. Run `git-agent-history startup-command` and paste its output into the platform's start-up script.

On every start, `startup` refills an empty agent home from **this device's own**
backup. It never restores another device automatically. Then it starts the worker.

## Restore

```sh
git-agent-history pull                       # fetch other devices
git-agent-history devices                    # what is there
git-agent-history sessions --device old-dsw-1a2b3c4d --agent codex

# Look first: unpack into an empty folder (safe while the agent runs)
git-agent-history export --from-device old-dsw-1a2b3c4d --agent codex --target ./restored

# Restore into a real home, moving project paths
git-agent-history restore --from-device macbook-5e6f7a8b --agent claude-code \
  --target ~/.claude --map-cwd /Users/me/code=/home/me/code

# Only one session
git-agent-history restore --from-device macbook-5e6f7a8b --agent codex --target ~/.codex \
  --session 0199aaaa-1111-7222-8333-444455556666
```

Restore rules:

- **Existing files are never overwritten.** Differing files are listed under `conflicts_kept`. Restore into an empty folder to compare them.
- **Index files are merged.** `session_index.jsonl` and `history.jsonl` only gain records the target does not have.
- **Running agents are left alone.** If the agent is running and the target is its live home, the index merge and database restore are skipped, and only new transcript files are added. Restart the agent afterwards.
- **`--with-database` (Codex) needs an empty target.** It restores the saved SQLite index only into a target that has none. Thread file paths are re-rooted to the target, and `--map-cwd` also rewrites project directories.
- **Claude Code relocation.** `--map-cwd` moves each project folder to the name Claude Code expects for the new path. Transcript text is not changed.
- **`--dry-run`** reports what would happen without writing anything.

## How it works

```text
your private repo
├── git-agent-history.json            # marker
├── README.md                          # "keep this private"
└── devices/
    ├── macbook-5e6f7a8b/
    │   ├── device.json                # label, platform, owner client id
    │   ├── codex/
    │   │   ├── manifest.json          # every file: size, sha256, chunks, present?
    │   │   ├── files/sessions/2026/11/17/rollout-…jsonl.chunks/g001-00001.gz
    │   │   ├── files/archived_sessions/rollout-…jsonl.zst.gz
    │   │   └── databases/state_5.sqlite.gz
    │   └── claude-code/
    │       ├── manifest.json
    │       └── files/projects/-Users-me-code/<session>.jsonl.chunks/…
    └── aliyun-dsw-1a2b3c4d/
        └── …
```

- **Checkpoints.** Each checkpoint copies only complete lines, so a half-written record is never saved. If a file grew, only the new bytes go into a new gzip chunk. If it was rewritten, a new generation replaces the old chunks; Git history still has them.
- **Deleted sources stay backed up.** Files that disappear from the agent home, such as Claude Code's 30-day cleanup, remain in the repository marked `present: false`, and you can still restore them.
- **Databases.** SQLite indexes are copied with SQLite's online backup API, checked with `quick_check`, redacted, and committed at most every 6 hours (`db_interval`).
- **Checksums.** Every stored piece has a SHA-256 in the manifest. Restore verifies all of them.

Details are in [docs/design.md](docs/design.md).

## Security and privacy

Transcripts can contain source code, file contents, command output and anything you
pasted, including credentials. So:

- `init` and every push check through `gh` that the GitHub repository is **private**. Non-GitHub remotes need `--trust-remote`.
- With `--secrets redact` (the default), well-known credential formats are replaced with `[REDACTED:<kind>]` in transcripts and database snapshots before they are committed. These include private keys and OpenAI, Anthropic, GitHub, AWS, Google, Slack, Stripe and Hugging Face tokens, plus JWTs. `warn` only counts them and `off` disables the check. `git-agent-history scan` shows counts per file without printing any value.
- Redaction is a safety net, not a guarantee: it can miss secrets and can change look-alike text. For thorough scanning, use a dedicated tool such as [gitleaks](https://github.com/gitleaks/gitleaks) on the repository.
- The repository is not encrypted. Anyone with access to it can read your conversations.
- Status files and error messages never include subprocess output, which might contain credentials. Set `GIT_AGENT_HISTORY_DEBUG=1` to see it.

### What is not backed up

Credentials and settings of every agent, Codex `logs_*`/`goals_*`/`memories_*` databases,
Claude Code `memory/`, `file-history/`, `plugins/`, images and other attachments, and
your project source code. Back up your code with its own Git repository.

## Related projects

Other projects work on agent history from different angles. Pick what fits; several combine well with this one.

| Project | Approach |
|---|---|
| [Entire CLI](https://github.com/entireio/cli) | Captures agent sessions alongside commits inside each project repository, so you can see why code changed. |
| [SpecStory](https://github.com/specstoryai/getspecstory) | Saves agent sessions as Markdown in the project's `.specstory/` folder. |
| [claude-code-sync](https://github.com/perfectra1n/claude-code-sync) | Rust CLI that syncs Claude Code history through a git repository. |
| [claude-sync](https://github.com/tawanorg/claude-sync) | Syncs Claude Code sessions through Cloudflare R2 with end-to-end encryption. |
| [repo-sessions](https://github.com/firish/repo-sessions) | Syncs Claude Code and Codex sessions per project through a private git vault and git hooks. |
| [agentctxsync](https://github.com/westsource/agentctxsync) | Self-hosted server that shares one session pool between several agents and devices. |
| [cli-continues](https://github.com/yigitkonur/cli-continues) | Hands a session over from one coding agent to another. |
| [cross_agent_session_resumer](https://github.com/Dicklesworthstone/cross_agent_session_resumer) | Converts session formats between providers through a canonical representation. |
| [coding_agent_session_search](https://github.com/Dicklesworthstone/coding_agent_session_search) | Indexes and searches local session history across many agents. |
| [Agent Sessions](https://github.com/jazzyalex/agent-sessions) | macOS app to browse and search local agent sessions. |

**Why another one?** This project is deliberately narrow: whole-machine, multi-agent,
multi-device backup that needs nothing but a private Git repository and restores
native files. It does not run agents, convert formats or provide a UI. For moving a
task from one agent to another, use a hand-off tool from the table above.

## Roadmap

- [ ] DeepSeek Harness, OpenCode and Gemini CLI adapters
- [ ] `handoff` command that delegates to an existing cross-agent tool
- [ ] Partial clone and sparse checkout for large repositories with many devices
- [ ] Optional encryption of stored files
- [ ] PyPI release

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests are offline. They use temporary folders, a local bare repository and synthetic
transcripts, never real history. See [AGENTS.md](AGENTS.md) and
[docs/adding-an-agent.md](docs/adding-an-agent.md).

## Origin

This project grew out of the author's private `codex-history` tool. It was written after a cloud GPU
development container (Alibaba Cloud PAI-DSW) was re-provisioned and the Codex history
on it was lost. `restore` still reads that tool's repository layout.

## License and trademarks

[MIT](LICENSE) © 2026 Zhicheng Zhang.

Codex is a product of OpenAI and Claude Code is a product of Anthropic. DeepSeek, OpenCode
and Gemini are trademarks of their respective owners. This project is independent and
not affiliated with or endorsed by any of them.
