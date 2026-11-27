# Prompt list

Every environment keeps a readable list of the prompts you typed on it. The list lives in the
history repository, so you can read it on GitHub from any machine. It is rewritten after every
backup, so it stays up to date by itself.

```text
devices/<device-id>/prompts/README.md     totals, a table of months and the latest 30 prompts
devices/<device-id>/prompts/2026-11.md    every prompt of one month, newest day first
```

Open `devices/<device-id>/prompts/README.md` in your private repository on GitHub to start. It
links to the month files and to the lists of your other devices. Each entry shows the time, the
agent, the project folder and the first eight characters of the session ID:

```markdown
## 2026-11-27 Friday

**10:40** · Claude Code · my-project · <sub>3f9c2e1d</sub>

> Add a dark mode to the settings page
```

Prompts longer than 20 lines or 1,500 characters are folded. Prompts longer than 20,000
characters are cut; `prompts --session` prints them in full.

## Searching

`git-agent-history prompts` searches the prompts of every device in the repository, newest first.
Run `pull` first to include other devices' latest backups.

```sh
git-agent-history pull
git-agent-history prompts --grep "dark mode"                  # JSON
git-agent-history prompts --since 2026-11-01 --agent codex --markdown
git-agent-history prompts --session 3f9c2e1d --limit 0        # a whole session, full text
```

| Option | Meaning |
|---|---|
| `--device ID` | Only one device. |
| `--agent NAME` | `codex`, `claude-code` or `deepseek-harness`. |
| `--session ID` | Only one session. A prefix of the ID is enough. |
| `--since YYYY-MM-DD` | Only prompts from this local date on. |
| `--grep TEXT` | Only prompts that contain TEXT, ignoring case. |
| `--limit N` | At most N prompts (default 50); `0` shows all. |
| `--markdown` | Readable Markdown instead of JSON. |

## What counts as a prompt

Only what you typed. Agent replies, tool output and context that an agent or editor adds are left out.

- **Claude Code:** user messages in the main session transcripts. Left out are tool results,
  subagent transcripts, meta and summary records, task notifications and interruptions, as well as
  the `<ide_opened_file>`, `<ide_selection>` and `<system-reminder>` blocks that the IDE extensions
  add. A slash command appears as `/name args`. A forked or resumed session copies earlier
  prompts, and those copies are listed once.
- **Codex:** `UserMessage` items of current Codex apps, or `user_message` events of older
  versions. For the oldest rollouts, which have neither, user messages without injected
  context (`<environment_context>`, `AGENTS.md` instructions and the like) are used. The
  "Files mentioned by the user" header becomes a file list, browser context is dropped, and
  answers to Codex's questions appear as `Q:`/`A:` lines.
- **DeepSeek Harness:** best effort. A record whose type or role is `user` counts, unless it is
  a tool result. This has not yet been checked against real logs.

Images are counted, not shown. History index files (`history.jsonl`) are not used, because the
transcripts are complete and the indexes are not.

## Details

- **Time zone.** Times use the time zone of the machine that writes the list. In a
  container that runs on UTC, set `TZ` (for example `TZ=Asia/Shanghai`) in the start-up script.
- **Cost.** Parsed prompts are cached in `local/prompts-cache.json` in the state directory.
  Each run reads only the chunks added since the previous one. The first run reads every stored
  transcript once, which takes a few seconds per gigabyte.
- **Commits.** The files are rewritten only when their content changes, so idle backups still
  make no commits. They are redacted like the transcripts.
- **Ownership.** Each environment writes only its own `devices/<id>/prompts/`. A device's list
  appears after that device runs a version with this feature.
- **Turning it off.** Set `"prompt_docs": false` in the environment's `config.json` (in the state
  directory), then delete `devices/<id>/prompts/` if you no longer want it. `prompts` still works.
- **Privacy.** The list shows in plain text what the stored transcripts already contain. Keep
  the repository private.
