# Changelog

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
