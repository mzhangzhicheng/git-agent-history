# Design

This document is for people who want to understand, audit or extend the storage format.
For usage, see the [README](../README.md).

## Goals and non-goals

Goals:

- Back up every conversation of every supported agent on a machine, continuously and unattended.
- Store backups from many machines in one private Git repository without conflicts.
- Restore native files that the agent can open again, on any machine and under any path.
- Need nothing beyond Python's standard library, `git` and (for GitHub) `gh`.

Non-goals:

- Running agents, converting between agent formats, or providing a UI.
- Backing up project source code, attachments or agent configuration.
- Encryption at rest. This is planned as an option; until then the repository must be private.

## Terms

| Term | Meaning |
|---|---|
| history repository | The user's private Git repository that receives backups. |
| environment | One agent installation being backed up. A machine with two `CODEX_HOME`s has two environments. |
| state directory | Per-environment local folder (`~/.git-agent-history` by default): `config.json`, the clone in `repo/`, and `local/` for status and caches. It is never committed. |
| device ID | Name of an environment's folder in the history repository: `devices/<device-id>/`. |
| adapter | Agent-specific code: where files live, which files to back up, how to restore them. |

## Repository layout

```text
git-agent-history.json             {"format": 2, "tool": "git-agent-history", "url": ...}
README.md                          reminder that the repository must stay private
.gitattributes                     "* -text": never convert line endings
devices/<device-id>/device.json    owner client_id, label, platform, hostname, agents
devices/<device-id>/<agent>/manifest.json
devices/<device-id>/<agent>/files/<rel>.chunks/gGGG-NNNNN.gz   append-only JSONL chunks
devices/<device-id>/<agent>/files/<rel>.gz                     whole-file blobs
devices/<device-id>/<agent>/databases/<name>.gz                SQLite snapshots
devices/<device-id>/handoff/<task-id>/<stamp>-<participant>.md handoff entries (see docs/handoff.md)
```

Handoff entries are immutable Markdown files with a small front matter. Like everything else,
they live in the writer's own device folder. Readers merge entries with the same task ID across
devices, ordered by the UTC stamp in the file name. They are not part of the manifest, so older
versions of the tool ignore them, and the storage `format` stays 2.

`<rel>` is the file's POSIX path relative to the agent home, for example
`sessions/2026/11/17/rollout-…-<uuid>.jsonl` or `projects/-Users-me-code/<uuid>.jsonl`.

## Manifest (format 2)

```json
{
  "format": 2,
  "agent": "codex",
  "home": "/Users/me/.codex",
  "updated_at": "2026-11-17T08:00:00+00:00",
  "files": {
    "sessions/2026/11/17/rollout-….jsonl": {
      "kind": "jsonl",
      "generation": 1,
      "chunks": [{"name": "g001-00001.gz", "bytes": 1900000, "sha256": "…"},
                 {"name": "g001-00002.gz", "bytes": 70000, "sha256": "…"}],
      "bytes": 1970000,
      "sha256": "<sha256 of the concatenated plaintext>",
      "secrets": {"github_token": 1},
      "present": true,
      "updated_at": "…"
    },
    "archived_sessions/rollout-….jsonl.zst": {
      "kind": "jsonl", "encoding": "zstd", "generation": 1, "chunks": ["…"],
      "bytes": 52000, "sha256": "…", "secrets": {}, "present": true, "updated_at": "…"
    }
  },
  "databases": {
    "state_5.sqlite": {"path": "databases/state_5.sqlite.gz", "bytes": 2100000, "sha256": "…",
                       "snapshot_at": "…"}
  }
}
```

All hashes cover the **stored** content, which is the content after redaction. `present`
says whether the file still existed in the agent home at the last checkpoint.

## Checkpoint algorithm

For every file an adapter reports:

1. Skip the file if its `(mtime_ns, size)` matches the local cache and every stored piece exists.
2. **JSONL.** Read up to the size observed at the start and drop a trailing incomplete
   line. The writer may be mid-record, so that line waits for the next checkpoint.
   Redact each line.
   - If the redacted stream equals what is stored, nothing changes.
   - If the stored bytes are an exact prefix of it (same SHA-256 at a line boundary),
     write only the new bytes as the next chunk of the same generation.
   - Otherwise, for example after a rewrite or truncation, start a new generation: delete
     the old chunk files and store the whole stream. Git history keeps the old ones.
   - A chunk holds at most 64 MiB of plaintext, which keeps every stored file below
     GitHub's 100 MB limit.
3. **Zstandard JSONL** (Codex `*.jsonl.zst`, DeepSeek Harness `*.jsonl.zstd`). Decompress
   every complete frame and ignore a torn last frame that is still being written. Then
   store the plaintext exactly like JSONL above, with `encoding: "zstd"`, so that a log that
   grows by appended frames only adds chunks. Restore compresses the file again into
   checksummed frames. For DeepSeek Harness the header line gets its own first frame, as
   its reader expects. This needs a Zstandard backend: Python 3.14 `compression.zstd`,
   the `zstandard` package, or the `zstd` CLI. Without one, the file is stored as an
   unredacted blob with `secret_scan: "skipped"`.
4. **Blob.** Store the whole file when its (redacted) SHA-256 changes.
5. Files no longer reported by the adapter keep their entry with `present: false`.
6. **Databases.** At most every `db_interval` seconds (default 6 hours), copy each database
   with SQLite's online backup API into local temporary storage, then run
   `pragma quick_check`. Redact text columns of ordinary tables; virtual tables and their
   shadow tables are left alone. Finally `VACUUM` and store the result if its hash changed.

The manifest and `device.json` are rewritten only when something changed, so idle
checkpoints produce no commits. The commit stages only `devices/<own-id>/` and the three
shared root files, and refuses to commit if anything else is staged.

## Sync algorithm

1. Confirm the `origin` URL still matches the configuration. For GitHub, confirm with
   `gh repo view --json isPrivate` that the repository is private.
2. If the remote branch exists, fetch it. Check that `devices/<own-id>/device.json` on the
   remote still carries this environment's `client_id`. Rebase local commits onto it.
   Because environments write to disjoint folders, a rebase conflict means two
   environments share a device ID; the rebase is aborted and the error reported.
3. Push `HEAD` to the branch. If the push is rejected because another device pushed
   first, repeat from step 2, up to five attempts. Nothing is ever force-pushed.

## Restore algorithm

1. Validate every manifest path: relative POSIX, no `..`, no drive letters, and allowed
   by the adapter's allow-list. Validate every chunk name.
2. Rebuild each selected file in a temporary folder and verify every chunk hash and
   the whole-file hash.
3. Ask the adapter where each file belongs on the target. Claude Code uses this to move
   project folders under `--map-cwd`.
4. Place the files:
   - Index files (`session_index.jsonl`, `history.jsonl`) are merged by appending records
     the target does not have.
   - Other files are written only if the destination does not exist.
   - If the agent is running and the target is its live home, index merges and database
     restores are skipped.
5. With `--with-database`, and only into a target without databases, restore SQLite
   snapshots. `rollout_path` is re-rooted at the target using its `sessions/` or
   `archived_sessions/` segment, and `cwd` is remapped with `--map-cwd`.

## Concurrency

- One background worker per state directory, enforced by a lock file in the system
  temporary folder. That folder is local disk even if the state directory sits on a
  network share.
- `once`, `restore`, `pull` and the worker share a second lock, so checkpoints and
  restores never interleave.
- Different environments never write the same paths. A device ID collision is detected
  through `client_id` before every commit and push.

## Compatibility

Format 1 (v0.1.0) stored `.jsonl.zst` files as blobs. Format 2 readers handle both
kinds, and the next checkpoint converts changed files.

`restore` also reads the layout of the earlier `codex-history` tool, in which
`devices/<id>/manifest.json` points to `transcripts/<rel>.gz` and `state/<db>.gz`. It
treats those files as Codex blobs.
