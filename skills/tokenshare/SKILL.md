---
name: tokenshare
description: Run and operate Tokenshare, a persistent unattended coding-agent controller that monitors configured Git repositories for markdown task queues and moves tasks through Pending, WIP, and Done. Use when a user asks to start, configure, test, troubleshoot, or explain Tokenshare autonomous repository task processing.
---

# Tokenshare

Operate Tokenshare as an unattended process. Assume the task text is the complete source of requirements; do not pause for human clarification.

## Configure

1. Add one Git repository URL per line to `config/task_repos.md` in the Tokenshare installation directory.
2. Ensure the current user's normal Git credentials can clone, pull, and push every repository.
3. Put exactly one `tokenshare_tasklist.md` in each repository root or its `docs/` directory.
4. Write complete, unambiguous tasks under `## Pending Tasks` using the documented `<task>` block format.
5. Use `-a/--agent` (or `TOKENSHARE_AGENT`) to select a stub from `skills/tokenshare/scripts/agent-stubs/`. Use `TOKENSHARE_AGENT_COMMAND` only for a raw native command.

## Run

From the Tokenshare checkout, run:

```bash
python3 scripts/tokenshare-controller.py
```

Use `--once` to clone/synchronize all repositories, drain the currently pending queue, and exit. Use `--poll-seconds` to change the default 60-second monitoring interval.

## Task contract

For each task, create a deterministic `tokenshare-dev-<task-title>-<fingerprint>` branch from the remote default branch. Claim it by moving it from Pending to WIP on that branch, maintain `docs/status_YYYY-MM-DD_HH-MM-SS.md` through `implementing`, `testing`, and `complete`, and move it to Done only after the coding agent succeeds. Push each lifecycle checkpoint to the task branch and never push, merge, or delete the remote default branch.

The coding agent must inspect repository instructions, implement the entire task, and run appropriate tests. It leaves changes uncommitted and must not push; the controller owns commits and automatically publishes the task branch. Maintainers review and merge that branch themselves. The agent must not ask questions; when details are absent, make the smallest reasonable assumption and record it in the status file.

Allow only one outstanding task branch per repository by default. A tasklist may opt into sequential creation of multiple outstanding branches with a strict `## Configuration` section containing `allow-multiple-branches: true`. Resume incomplete managed branches before claiming new work. Treat a deleted-unmerged branch as declined locally and skip the exact task fingerprint until its body changes.

When a child agent exits unsuccessfully, retry indefinitely with a 5-second incremental backoff and continue the provider's latest repository session where supported. Do not fail the task merely because of transient capacity, rate-limit, usage-limit, or network errors.

Each agent phase must append the exact phase-completion marker supplied in its prompt to `status.md` after all phase work is finished. The controller uses this handshake to advance native TUIs that remain open waiting for input.

Use `--auto-attach [TTY]` when the user wants each agent TUI shown automatically. With no value it targets the controller terminal; an explicit `/dev/pts/...` targets that terminal. Treat an invalid or disappearing requested TTY as a fatal attachment error.

## Safety

- Stop if a configured repository cannot be accessed.
- Stop if both root and `docs/` tasklists exist.
- Refuse WIP tasks on the remote default branch and resume WIP tasks on managed branches.
- Require unique task titles and block body revisions while the earlier task branch exists.
- Do not mark a task Done after a nonzero agent exit.
- Preserve unrelated working-tree changes and surface them as an error.
