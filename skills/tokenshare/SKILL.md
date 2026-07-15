---
name: tokenshare
description: Run and operate Tokenshare, a persistent unattended coding-agent controller that monitors configured Git repositories for markdown task queues and moves tasks through Pending, WIP, and Done. Use when a user asks to start, configure, test, troubleshoot, or explain Tokenshare autonomous repository task processing.
---

# Tokenshare

Operate Tokenshare as an unattended process. Assume the task text is the complete source of requirements; do not pause for human clarification.

## Configure

1. Add one Git repository URL per line to `config/task_repos.md`.
2. Ensure the current user's normal Git credentials can clone, pull, and push every repository.
3. Put exactly one `tokenshare_tasklist.md` in each repository root or its `docs/` directory.
4. Write complete, unambiguous tasks under `## Pending Tasks` using the documented `<task>` block format.
5. Set `TOKENSHARE_AGENT_COMMAND` when the coding-agent command is not `codex exec --full-auto -`.

## Run

From the Tokenshare checkout, run:

```bash
python scripts/tokenshare-controller.py
```

Use `--once` to clone/synchronize all repositories, drain the currently pending queue, and exit. Use `--poll-seconds` to change the default 60-second monitoring interval.

## Task contract

For each task, claim it by moving it from Pending to WIP, maintain `docs/status_YYYY-MM-DD_HH-MM-SS.md` through `implementing`, `testing`, and `complete`, and move it to Done only after the coding agent succeeds. Work on only one task at a time. Leave failed work in WIP and report the error.

The coding agent must inspect repository instructions, implement the entire task, run appropriate tests, commit its code changes, and push using the repository's existing Git authentication. It must not ask questions; when details are absent, make the smallest reasonable assumption and record it in the status file or commit message.

## Safety

- Stop if a configured repository cannot be accessed.
- Stop if both root and `docs/` tasklists exist.
- Refuse a new Pending task while any WIP task exists.
- Do not mark a task Done after a nonzero agent exit.
- Preserve unrelated working-tree changes and surface them as an error.
