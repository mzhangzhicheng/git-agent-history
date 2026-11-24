# Handoffs between agents

Agents on different machines cannot see each other's conversations. For example, Claude Code may run
on your laptop while Codex runs in a cloud container. `git-agent-history handoff` passes tasks
and their context between them, through the same private repository that holds the history.
Every environment that takes part needs `git-agent-history init` with the same history repository.

## How it works

A **task** has an ID, a title, a status and a **spec**. It also has a **log**, and `next` names
whose turn it is.

- **Participants**: `claude-code`, `codex`, `deepseek-harness` and `human` (you).
- **Spec**: the current description of the task: goal, acceptance criteria, roles, context and
  open questions. Someone who reads only the spec should be able to start. When it changes, post a
  new spec; old versions stay in the log.
- **Notes**: log entries. A note usually hands the turn to someone else.
- **Turns**: only the participant named in `next` works on the task. Everyone else only reads it.
- **Status**: `open`, `doing`, `blocked`, `review` or `done`.

## Commands

All commands print JSON, except `show --markdown`.

```sh
# start a task (without --file/--message, the spec is a template to fill in)
git-agent-history handoff new --title "Add login API" --from claude-code --to codex --file spec.md

# what is waiting for me?
git-agent-history handoff list --for codex

# read a task
git-agent-history handoff show TASK --markdown

# hand it back
git-agent-history handoff post TASK --from codex --to claude-code --status review --file note.md

# replace the spec
git-agent-history handoff post TASK --from claude-code --spec --file spec.md
```

`--file -` reads standard input. Every command first pulls the other devices' entries. A write
is committed to this device's folder and pushed right away, so you do not have to wait for the
next backup run. If the remote cannot be reached:

- reads use local data and report that under `pull`;
- writes are committed locally, and the background worker pushes them later.

## Instructions for agents

**Who you are.** Use `claude-code` if you are Claude Code, `codex` if you are Codex, and
`deepseek-harness` if you are DeepSeek Harness.

**Picking up.** At the start of a session, or when the user mentions a handoff:

1. Run `git-agent-history handoff list --for <you>`.
2. For each task listed, run `git-agent-history handoff show TASK --markdown`. Read the spec, then the latest notes.
3. If the task names a project repository and branch, fetch it and check out that branch.
4. Tell the user in two or three sentences what you are about to do, then do it.

**Handing over.**

1. If the goal, decisions or context changed, post a new spec with `--spec`. Rewrite it as a
   whole; do not append to it.
2. Post a note. Say what you did and what changed: repository, branch, commit and files. Say what
   the next participant should do and what to watch out for, such as anything unverified or
   different between environments.
3. Pass the turn with `--to` and set `--status`. Use `--to human --status blocked` when the user
   has to decide something, and `--to human --status done` when the task is finished.

**Writing.**

- Write for a reader who cannot see your conversation, terminal or machine. Give paths,
  commands, versions and exact error messages.
- Say what the other environment lacks, for example local data, `.env` files or services that only
  run on your machine.
- Never include tokens or passwords; name the environment variable instead. Redaction is a safety
  net, not a reason to paste secrets.
- The project's code travels through its own repository. Push your branch before you hand off and
  name it in the note. Each entry is limited to 256 KiB, so link to files instead of pasting them.

## Telling your agents about it

Agents follow this only if they know about it. Use one or both of these:

- Install the skill on each machine with `npx skills add mzhangzhicheng/git-agent-history`.
- Add a line to the agent's instructions file. That is `AGENTS.md` for Codex (global
  `~/.codex/AGENTS.md`, or the project's own) and `CLAUDE.md` for Claude Code:

  ```markdown
  At the start of a task, run `git-agent-history handoff list --for codex` (use your own name) and follow
  docs/handoff.md in https://github.com/mzhangzhicheng/git-agent-history for any task listed.
  ```

## Storage

Each entry is an immutable Markdown file in the writer's own device folder:

```text
devices/<device-id>/handoff/<task-id>/<YYYYMMDDTHHMMSSffffffZ>-<participant>.md
```

```markdown
---
task: 20261124-add-login-api-3f2a
title: Add login API
kind: spec
from: claude-code
to: codex
status: open
created_at: 2026-11-24T07:12:03+00:00
---

## Goal
...
```

- Entries are ordered by the UTC time stamp in the file name. If two machines' clocks disagree,
  entries written seconds apart can appear out of order.
- The newest entry gives the title, status and `next`. The newest `spec` entry is the spec.
- Entries never change once written, and devices never write each other's folders, so handoffs
  cannot cause sync conflicts.
- They are redacted and kept private exactly like the history.
