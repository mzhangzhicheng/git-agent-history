# Changelog

## 0.1.0 (unreleased)

First public version.

- Backup, sync and restore for Codex and Claude Code through a private Git repository, one folder per device.
- Append-only chunk storage for JSONL transcripts, so long sessions are not re-uploaded in full.
- Support for Codex `*.jsonl.zst` rollouts, and online snapshots of `state_*.sqlite`.
- Redaction of well-known credential formats in transcripts and database snapshots (default on).
- Restore with path mapping (`--map-cwd`), single-session restore, dry runs, and a safe mode while the agent is running.
- Background worker, macOS/Linux/Windows autostart, and a start-up hook for containers.
- Reads the repository layout of the author's earlier `codex-history` tool.
