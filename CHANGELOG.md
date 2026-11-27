# Changelog

## Unreleased

- **Prompt list.** After each backup, every environment rewrites `devices/<id>/prompts/`: a README with totals, months and the latest prompts, and one Markdown file per month with every prompt typed on that device. It shows only what you typed, without tool output or IDE context. It is updated incrementally and redacted. The new `prompts` command searches the prompts of every device by text, date, agent or session. See [docs/prompts.md](docs/prompts.md).
- **Handoffs between agents.** The new `handoff new|post|list|show` commands pass tasks between agents on different machines, for example Claude Code on a laptop and Codex in a cloud container. Each task has a spec, a log and a record of whose turn it is. Entries are immutable Markdown files in each device's own folder, redacted like the history and pushed right away. See [docs/handoff.md](docs/handoff.md).
- **Large first backups now upload.** `git push`, `fetch`, `ls-remote` and `clone` no longer stop after a fixed 5 minutes (1 hour for `clone`), which made a first backup of several hundred MB fail on every retry. Git now aborts only a transfer that stalls below 1 KiB/s for 5 minutes, with a 6-hour ceiling.
- These commands use HTTP/1.1, which avoids `Error in the HTTP2 framing layer` on large pushes to GitHub.

## 0.2.0 (2026-11-18)

- **DeepSeek Harness** (`dsh`) support: backs up session logs from `$DSH_HOME/sessions` (default `~/.dsh/sessions`), including compressed `*.jsonl.zstd` logs. With `--map-cwd`, a restore rewrites each session's project path and moves it to the matching folder. New `--dsh-home` option for `init`.
- Compressed transcripts (Codex `*.jsonl.zst`, DeepSeek Harness `*.jsonl.zstd`) are now stored as incremental plaintext chunks and compressed again on restore, so active compressed logs no longer re-upload in full. Storage format 2; format 1 backups remain restorable.
- `sessions` lists each session once, even when an agent keeps several format generations.
- Shorter README that leads with letting your coding agent do the setup.

## 0.1.0 (2026-11-17)

First public version.

- Backup, sync and restore for Codex and Claude Code through a private Git repository, one folder per device.
- Append-only chunk storage for JSONL transcripts, so long sessions are not re-uploaded in full.
- Support for Codex `*.jsonl.zst` rollouts, and online snapshots of `state_*.sqlite`.
- Redaction of well-known credential formats in transcripts and database snapshots (default on).
- Restore with path mapping (`--map-cwd`), single-session restore, dry runs, and a safe mode while the agent is running.
- Background worker, macOS/Linux/Windows autostart, and a start-up hook for containers.
- Reads the repository layout of the author's earlier `codex-history` tool.
