---
name: git-agent-history
description: Back up, sync and restore coding-agent conversation history (Codex, Claude Code, DeepSeek Harness) through the user's private Git repository, one folder per device, and hand tasks between agents on different machines. Use when the user wants agent chat history saved across machines or containers, restored from another device, or moved to a new path; when they want to hand a task to another agent or pick one up (handoff, 交接, 接手); or at the start of a session on a machine where git-agent-history is set up, to check for tasks waiting for you.
---

# git-agent-history

git-agent-history copies the conversation history of Codex, Claude Code and DeepSeek Harness into a
private Git repository that the user owns. Every machine or container writes to its own
`devices/<device-id>/` folder. History can then be restored on any other machine.

Project: https://github.com/mzhangzhicheng/git-agent-history

## When to use

- The user wants their agent conversations backed up or synced across machines.
- The user lost history (rebuilt container, new laptop, or Claude Code's 30-day cleanup) and wants it back from another device.
- The user moved projects to new paths and wants old sessions to appear there.

## How

Follow `docs/agent-setup.md` in the project repository step by step. Key rules:

- Never ask for, print or store tokens. GitHub login happens in the user's browser with `gh auth login --web`.
- The history repository must be private, and each environment gets its own state directory and device ID.
- Never force-push or modify other devices' folders. Restore only when asked, and prefer `export` into an empty folder first.

Every command prints JSON:

```sh
git-agent-history init --repo OWNER/NAME --label "my laptop"
git-agent-history once && git-agent-history start && git-agent-history status
git-agent-history pull && git-agent-history devices
git-agent-history restore --from-device ID --agent codex|claude-code|deepseek-harness --target PATH [--map-cwd OLD=NEW] [--session ID] [--dry-run]
git-agent-history prompts [--grep TEXT] [--since YYYY-MM-DD] [--device ID] [--agent NAME] [--session ID] [--markdown]
```

When the user wants to find or review something they asked an agent earlier, run `pull`, then
`prompts --grep`. Each device's full list is also in `devices/<id>/prompts/README.md` of the
history repository (see `docs/prompts.md`).

## Handoffs

Agents on different machines pass tasks through the same repository. Follow `docs/handoff.md`.
In short:

- Your name is `claude-code`, `codex` or `deepseek-harness`; the user is `human`.
- Picking up: run `handoff list --for <you>`, then `handoff show TASK --markdown`. Read the spec,
  then the latest notes, and tell the user what you will do.
- Handing over: if the context changed, post a new spec with `--spec`. Then post a note with what
  you did, the repository, branch and files, and what comes next. Pass the turn with
  `--to` and set `--status`.
- Write for a reader who cannot see your conversation or your machine, and never include secrets.

```sh
git-agent-history handoff new --title TITLE --from claude-code --to codex --file spec.md
git-agent-history handoff list --for codex
git-agent-history handoff show TASK --markdown
git-agent-history handoff post TASK --from codex --to claude-code --status review --file note.md
```
