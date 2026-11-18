# Adding support for another agent

An adapter tells git-agent-history three things: where an agent keeps its history, which
of those files are conversations, and how to put them back. Everything else (chunking,
redaction, Git, restore safety) is shared.

## 1. Research the storage format

Before writing code, document the following in the pull request:

- The default home folder and the environment variable that overrides it.
- Which files hold conversations, and whether they are append-only JSONL, rewritten
  JSON, compressed files or databases.
- Which files hold credentials or settings and must never be read.
- How the agent finds sessions again (directory scan, index file, database), so that
  restored files show up.
- The agent version you inspected.

Use your own installation or the agent's public documentation and source. Do not copy
real transcripts into the repository; write small synthetic fixtures instead.

## 2. Implement the adapter

Create `src/git_agent_history/adapters/<agent>.py` with a subclass of `Adapter`
(see `base.py`) and register it in `adapters/__init__.py`.

| Member | Purpose |
|---|---|
| `name` | Stable folder name in the history repository, for example `opencode`. It must never change once released. |
| `display_name`, `env_var`, `default_home()` | Where the history lives. |
| `sources(home)` | Yield `Source(rel, path, kind)` with `kind` set to `jsonl` (append-only lines), `blob` (whole file) or `zst` (Zstandard-compressed JSONL). Use `walk_files`, which skips symlinks. Yield only conversation files. |
| `zstd_header_frame` | Set to `True` if the agent expects the first line of a `zst` file in its own frame (DeepSeek Harness does). |
| `allowed(rel)` | Allow-list for restore. It must accept exactly the paths that `sources` can produce. |
| `index_kind(rel)` | `'lines'` or `'id'` for shared index files that are merged on restore, otherwise `None`. |
| `session_id(rel)` | Session ID for listing, or `None` for files that are not sessions. |
| `is_running_process(exe, args)` | Detects a running agent, so that restores into its live home stay safe. |
| `target_paths(staged, rels, mappings)` | Optional. Remaps restore paths, for example when folders are named after the project path. |
| `databases(home)`, `valid_database_name`, `rewrite_database` | Optional. SQLite files to snapshot and how to fix paths in them after restore. |

If an agent keeps conversations only inside a database, snapshot the database and treat
a restore with `--with-database` as the main path. Explain the limits in the README.

## 3. Test

Add tests to `tests/` that build a fake home in a temporary folder and cover:

- what is and is not backed up (credentials must not appear in the repository),
- a backup and restore round trip with byte-for-byte comparison,
- path mapping, if the adapter remaps paths.

Run `python3 -m unittest discover -s tests -v`. Tests must stay offline.

## 4. Document

Add the agent to the "Supported agents" tables in `README.md` and `README.zh-CN.md`,
noting the version you tested.
