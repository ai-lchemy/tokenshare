---
name: tokenshare
description: Run and operate Tokenshare, an approval-gated coding-agent controller, and author or repeatedly refine local repository tasks with -ct/--create-task and -gt/--grill-task. Use for controller operation, task queue management, Plan-mode task creation, and detailed task Q&A.
---

# Tokenshare

Operate Tokenshare as a persistent process. Treat the task text as the complete source of requirements; unattended agents do not pause for clarification.

## Commands

Interpret these as Tokenshare skill arguments, not `tokenshare-controller` flags:

- `-ct [IDEA]`, `--create-task [IDEA]`: plan and add a local Pending task.
- `-gt [TITLE|POSITION]`, `--grill-task [TITLE|POSITION]`: plan and refine a local Pending task.

Reject unknown skill options. For either authoring command, read
[`references/task-authoring.md`](references/task-authoring.md) completely before proceeding.

### Create a task

1. Require Plan mode. If it is not active, tell the user to invoke `/plan $tokenshare -ct ...` and stop without editing.
2. Confirm the current directory is the intended Git repository. Locate exactly one tasklist at the root or `docs/`; reject two. If none exists, plan a root tasklist with `allow-multiple-branches: false` and the required sections.
3. Inspect relevant repository code and documentation before asking questions. Resolve goal, success criteria, scope, constraints, interfaces, data flow, failure behavior, tests, acceptance criteria, and non-goals.
4. Produce a decision-complete plan containing the exact target path and complete Pending task block. Do not edit in Plan mode.
5. After the user explicitly approves and execution mode is active, create or update only the local tasklist. Never commit or push.

### Grill a task

1. Require Plan mode. If it is not active, tell the user to invoke `/plan $tokenshare -gt ...` and stop without editing.
2. Parse Pending tasks in file order and assign temporary 1-based positions for selection only. Accept an exact title or position. If omitted or ambiguous, show numbered titles and ask the user to choose.
3. Inspect the selected task and relevant repository implementation. Challenge ambiguity, missing decisions, conflicting requirements, unsafe assumptions, incomplete edge cases, and unverifiable acceptance criteria.
4. Preserve existing requirements unless the user explicitly supersedes them. Permit a title change only after confirmation and reject duplicate titles.
5. Produce a decision-complete replacement block and target path. After explicit approval in execution mode, replace only that Pending block without committing or pushing.
6. Treat every invocation as another refinement pass over the latest local content; never impose a maximum number of passes.

## Configure

1. Add one Git repository URL per line to `config/task_repos.md`.
2. Ensure the current user's Git credentials can clone, pull, and push each repository.
3. Put exactly one `tokenshare_tasklist.md` in each repository root or `docs/` directory.
4. Write complete tasks under `## Pending Tasks` using the documented `<task>` format.
5. Select an agent stub with `-a/--agent`, or use `TOKENSHARE_AGENT_COMMAND` for a raw command.

## Run and approve

Run `tokenshare-controller`. New remote tasks are copied into `<development-directory>/logs/agent/tokenshare_agent_tasklist.md` as `[Unapproved]` and cannot execute until a human reviews them and enters an `approve` command.

Use `view`, `approve 1,3`, `approve 1:9`, `approve all`, or `approve all not 1,3`. Approval numbers are temporary and belong only to unapproved tasks; after approval the remaining tasks are renumbered contiguously. Use `--workers N` for concurrency across repositories; only one task may execute in any repository. Use `-ni/--non-interactive-mode` to synchronize, drain previously approved work, and exit. The controller is a full-screen TUI. `--auto-attach TTY` requires a separate terminal already running `tmux new-session -A -s tokenshare-viewer`; plain-shell TTYs and the controller TTY are rejected with setup guidance.

`--dangerously-skip-approvals` immediately approves imported tasks without human review. Use it only for tasks authored by the agent owner. `-ch/--clear-history` silently clears local state, queue history, and repository task logs, then exits without initializing agents or viewers; it must preserve the controller audit log.

## Task contract

Create deterministic review branches from the remote default branch and move each approved task from Pending to WIP to Done on its branch. Agents append progress and exact phase markers to the supplied local task log, leave changes uncommitted, and never push. The controller owns commits and publication.

Controller logs and the intake queue belong under `<development-directory>/logs/agent/`; repository task logs belong under `<development-directory>/logs/repos/`. Never create status or log files inside monitored repositories.

Preserve the `allow-multiple-branches: true` configuration, managed-branch recovery, declined fingerprint tracking, indefinite agent retry, and optional `--auto-attach TTY` behavior.

## Security

Tokenshare provides no sandboxing. Agents run directly with the controller user's permissions, credentials, environment, and network access. Users must inspect tasks themselves and execute Tokenshare only in a secure VM that supplies the required isolation.

- Stop when a configured repository cannot be accessed.
- Stop if both root and `docs/` tasklists exist.
- Refuse WIP tasks on the remote default branch and resume WIP tasks on managed branches.
- Require unique titles and fresh approval whenever an unstarted remote task changes.
- Do not mark a task Done after a nonzero agent exit.
- Preserve unrelated working-tree changes and surface them as an error.
