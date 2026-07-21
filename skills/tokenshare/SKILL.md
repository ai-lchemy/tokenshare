---
name: tokenshare
description: Run and operate Tokenshare, an approval-gated coding-agent controller that monitors Git repositories for markdown task queues.
---

# Tokenshare

Operate Tokenshare as a persistent process. Treat the task text as the complete source of requirements; unattended agents do not pause for clarification.

## Configure

1. Add one Git repository URL per line to `config/task_repos.md`.
2. Ensure the current user's Git credentials can clone, pull, and push each repository.
3. Put exactly one `tokenshare_tasklist.md` in each repository root or `docs/` directory.
4. Write complete tasks under `## Pending Tasks` using the documented `<task>` format.
5. Select an agent stub with `-a/--agent`, or use `TOKENSHARE_AGENT_COMMAND` for a raw command.

## Run and approve

Run `tokenshare-controller`. New remote tasks are copied into `<development-directory>/logs/tokenshare_agent_tasklist.md` as `[Unapproved]` and cannot execute until a human reviews them and enters an `approve` command.

Use `view`, `approve 1,3`, `approve 1:9`, `approve all`, or `approve all not 1,3`. Task numbers are stable and ranges are inclusive. Use `--workers N` for concurrency across repositories; only one task may execute in any repository. Use `-ni/--non-interactive-mode` to synchronize, drain previously approved work, and exit. The controller is a full-screen TUI. `--auto-attach TTY` requires a separate terminal already running a tmux client (`tmux new-session -A -s tokenshare-viewer`); plain-shell TTYs and the controller TTY are rejected.

## Task contract

Create deterministic review branches from the remote default branch and move each approved task from Pending to WIP to Done on its branch. Agents append progress and exact phase markers to the supplied local task log, leave changes uncommitted, and never push. The controller owns commits and publication.

All controller, approval, and task logs belong under `<development-directory>/logs/`. Never create status or log files inside monitored repositories.

Preserve the `allow-multiple-branches: true` configuration, managed-branch recovery, declined fingerprint tracking, indefinite agent retry, and optional `--auto-attach TTY` behavior.

## Security

Tokenshare provides no sandboxing. Agents run directly with the controller user's permissions, credentials, environment, and network access. Users must inspect tasks themselves and execute Tokenshare only in a secure VM that supplies the required isolation.

- Stop when a configured repository cannot be accessed.
- Stop if both root and `docs/` tasklists exist.
- Refuse WIP tasks on the remote default branch and resume WIP tasks on managed branches.
- Require unique titles and fresh approval whenever an unstarted remote task changes.
- Do not mark a task Done after a nonzero agent exit.
- Preserve unrelated working-tree changes and surface them as an error.
