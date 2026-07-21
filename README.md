# Tokenshare

Tokenshare is a persistent Python controller and AIML skill that connects repository owners who define work with agent owners who review and run that work. It watches Git repositories for autonomous coding tasks, clones configured repositories beneath a user-selected development directory, and publishes completed tasks on dedicated review branches.

## Who uses Tokenshare

Tokenshare has two distinct human workflows. The same person may fill both roles, but the handoff and responsibilities remain separate:

| Role | Uses | Responsibility |
| --- | --- | --- |
| **Repository owner** | A repository's `tokenshare_tasklist.md` and, optionally, the `$tokenshare` skill's `-ct/--create-task` and `-gt/--grill-task` commands | Defines decision-complete tasks, reviews the proposed tasklist edit, and manually commits and pushes the tasklist to the repository's remote default branch. The repository owner does not use the controller to approve or execute work. |
| **Agent owner** | `tokenshare-controller`, its approval TUI, agent configuration, local intake/task logs, and optional tmux viewer | Chooses which repositories to monitor, inspects every imported task, approves trusted work, monitors execution, and reviews controller output. The controller commits the implementation and pushes a dedicated review branch; the agent owner does not author or edit the repository owner's task on intake. |

The remote tasklist is the boundary between the roles. A repository owner publishes a task; an agent owner's controller imports a snapshot as Unapproved; the agent owner reviews and approves it; then the controller runs an unattended coding agent and publishes the result for normal code review. Repository ownership and agent ownership do not imply trust: the agent owner is responsible for deciding whether a remote task is safe to execute.

## Security model

Tokenshare does not sandbox repositories, tasks, coding agents, commands, credentials, or network access. Coding agents execute directly with the permissions and environment of the controller process. Inspect tasks yourself and run Tokenshare only inside a secure, disposable VM that provides the isolation appropriate for your repositories and credentials.

## Agent-owner quick start

1. Run `python3 install.py`. The installer records the development directory, installs the controller and skill, and installs `prompt_toolkit` when necessary.
2. Add one repository URL per line to `config/task_repos.md`.
3. Confirm that every configured repository has exactly one `tokenshare_tasklist.md`, either at its root or in `docs/`.
4. Ensure ordinary `git clone`, `git pull`, and `git push` work with the VM user's credentials.
5. Start the interactive controller and review imported tasks before approving them:

```bash
tokenshare-controller
```

The development directory may be supplied noninteractively:

```bash
python3 install.py -dd /absolute/path/to/tokenshare_dev
```

For automation, synchronize repositories, drain work that was already approved, and exit:

```bash
tokenshare-controller --non-interactive-mode
```

Fresh remote tasks are always imported as Unapproved, so noninteractive mode never approves them.

To bypass review and immediately run every imported task, pass
`--dangerously-skip-approvals`. This is intentionally dangerous: an untrusted task can
run commands with the controller user's permissions, credentials, environment, and network
access. Use it only for tasks authored by the owner of the agent.

To silently reset local controller history and exit, run `tokenshare-controller -ch` (or
`--clear-history`). This deletes the configured `state.json`, the local intake queue, and
repository task logs. It does not alter cloned repositories or remote branches, and it never
deletes or truncates the controller audit log. It does not initialize an agent, worker, TUI,
or attachment viewer.

## Repository-owner workflow

Repository owners create the work that an agent owner's controller will later discover. They do not need to install or run the controller.

1. Add exactly one `tokenshare_tasklist.md` at the repository root or under `docs/`, using the format below.
2. Write a uniquely titled task under `## Pending Tasks`. Treat its text as the complete contract for an unattended coding agent: include scope, behavior, constraints, edge cases, testing expectations, acceptance criteria, and useful file or interface context. The implementation agent cannot stop to ask the repository owner clarifying questions.
3. Optionally use the Tokenshare skill in Plan mode to turn an idea into a stronger task:

```text
/plan $tokenshare -ct Add the task idea here
/plan $tokenshare -gt Exact Existing Task Title
```

`-ct/--create-task` inspects the repository, asks planning questions, and proposes a complete Pending task. `-gt/--grill-task` repeatedly challenges and refines an existing local Pending task by title or position. Both commands show the exact proposed tasklist edit for approval and only modify the local tasklist after explicit approval in execution mode. They never commit or push.

4. Review the resulting task text, then manually commit and push the tasklist change to the repository's remote default branch using the repository's normal Git and review process. Tokenshare does not publish repository-owner task edits.
5. Wait for the agent owner to review and approve the imported task. After execution, review the controller-published `tokenshare-dev-...` branch through the repository's normal code-review and merge process.

Editing or removing an unstarted task on the remote default branch invalidates the agent owner's previous snapshot. The changed task is imported again as Unapproved and must receive fresh approval.

## Agent-owner workflow

Agent owners operate the trusted environment in which coding agents run. Their workflow begins only after a repository owner has pushed a task to a configured remote repository.

1. Configure the repositories, Git credentials, coding agent, worker count, and optional tmux viewer in the secure VM where the controller will run.
2. Start `tokenshare-controller`. It fetches configured repositories and copies new remote tasks into `<development-directory>/logs/agent/tokenshare_agent_tasklist.md` with source metadata and an `[Unapproved]` tag.
3. Read the complete imported task and verify its source and safety. Use `view` to inspect the queue, then approve only trusted tasks with the TUI's `approve` commands. If the repository owner changes an unstarted task, review and approve the new snapshot again.
4. Monitor lifecycle state and activity in the TUI. Optionally attach a separate tmux viewer to observe the active coding-agent session. The controller handles retries and phase transitions while keeping durable logs outside monitored repositories.
5. Let the controller create the task branch, run implementation and tests, commit the completed changes, and push the dedicated review branch. The controller never merges or modifies the remote default branch; review and merge remain part of the repository's normal process.

The agent owner should not rewrite imported task text to make it executable. Missing or ambiguous requirements should go back to the repository owner, who can refine and republish the task. `--dangerously-skip-approvals` removes this human review boundary and is appropriate only when every imported task is authored and trusted by the agent owner.

## Approval controller

The interactive `prompt_toolkit` interface uses one full-screen renderer with a task pane, scrollable activity pane, fixed command input, and status bar. Drag the divider between Tasks and Activity (or use Ctrl-Up/Ctrl-Down) to give either pane more room. Background updates never write directly over the `tokenshare>` prompt. The status bar shows active repositories, worker use, uptime, and idle time; the original shell screen returns when the controller exits.

Commands:

```text
view
approve 1,3,7
approve 1:9,11:15
approve all
approve all not 1,3
help
quit
```

Ranges are inclusive. `view` orders Pending/Unapproved tasks first, followed by Pending/Approved,
WIP, and Done, and shows each task's repository, author, approval, lifecycle state, and title.
Approval numbers are temporary and appear only on unapproved tasks. After any approval, approved
tasks lose their numbers and the remaining unapproved tasks are renumbered contiguously from 1.
The Tasks pane always displays those numbered tasks first.

After each fetch, the controller compares the remotely tracked tasklist with its local intake state. New tasks are copied to `<development-directory>/logs/agent/tokenshare_agent_tasklist.md` with source metadata and an `[Unapproved]` tag. Review the complete task there in an editor, then approve it from the controller. Editing or removing an unstarted remote task revokes its old snapshot; edited content receives a new number and requires fresh approval.

Use `--workers N` to execute tasks from different repositories concurrently. The controller always limits a repository to one executing task, regardless of the worker count. The existing `allow-multiple-branches: true` tasklist setting controls whether another task may be published while an earlier review branch remains unmerged.

## Agent and branch workflow

The default agent is `codex --dangerously-bypass-approvals-and-sandbox` in a detached tmux session. Supported agents receive their noninteractive permission-bypass flags so they can update the external task log; this is why the controller must run inside a secure VM. Select a bundled stub with `-a/--agent`, provide a raw command with `--agent-command`, or use `--no-tmux` for tests and noninteractive agents.

Automatic attachment requires a separate terminal that is already running a tmux client. This avoids making the terminal's shell and agent compete for input and terminal modes:

```bash
# In the separate agent-viewer terminal:
tmux new-session -A -s tokenshare-viewer
tty

# In the controller terminal, using the path printed above:
tokenshare-controller --auto-attach /dev/pts/9
```

The controller switches that tmux client between its viewer shell and agent sessions. Mouse-wheel scrolling enters tmux copy mode with up to 50,000 lines of history; `q` leaves copy mode and `Ctrl-b d` detaches the viewer without stopping the agent. Ctrl-C continues to exit the controller when used in the controller TUI. Plain-shell TTYs and the controller's own TTY are rejected. If setup is missing, the controller exits with the exact tmux and `tty` commands required.

When a task starts or resumes, `tokenshare-controller.log` records its repository, author,
title, branch, and start mode for auditing.

For every approved task, the controller creates `tokenshare-dev-<task-title>-<fingerprint>` from the remote default branch. It moves the task from Pending to WIP on that branch, runs implementation and testing phases, moves it to Done, commits the result, and pushes the review branch. It never pushes, merges, or deletes the remote default branch.

Agents leave code changes uncommitted and append progress plus their phase-completion handshake to the task log supplied by the controller. Failed agent sessions retry indefinitely with incremental backoff and resume native sessions when supported.

All durable logging stays outside monitored repositories:

```text
<development-directory>/logs/agent/tokenshare-controller.log
<development-directory>/logs/agent/tokenshare_agent_tasklist.md
<development-directory>/logs/repos/<task-branch>_log.md
```

No `status.md`, status file, or log directory is written into a remote repository.

## Tasklist format

```markdown
# Tokenshare Tasklist
## Pending Tasks
### <task> [Pending] Implement Awesome Feature
- Include every requirement the unattended coding agent needs.
### </task>
## WIP Tasks
## Completed Tasks
```

Section names and task states are exact. Task titles must be unique.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `TOKENSHARE_CONFIG` | `<installation>/config/task_repos.md` | Repository-list path |
| `TOKENSHARE_WORKSPACE` | Installed development directory | Clone parent and local-log directory |
| `TOKENSHARE_AGENT_COMMAND` | `codex --dangerously-bypass-approvals-and-sandbox` | Native agent command |
| `TOKENSHARE_AGENT` | unset | Agent stub name or path |
| `TOKENSHARE_POLL_SECONDS` | `60` | Remote polling interval |
| `TOKENSHARE_WORKERS` | `1` | Maximum concurrent repositories |
| `TOKENSHARE_AUTO_ATTACH` | unset | Automatic agent attachment target |
| `TOKENSHARE_STATE` | `~/.config/tokenshare/state.json` | Stable IDs, scans, and publication state |

Command-line flags override environment variables. Run `tokenshare-controller --help` for details.

## Task authoring skill commands

In Plan mode, use `$tokenshare -ct [IDEA]` to create a decision-complete local Pending task, or
`$tokenshare -gt [TITLE|POSITION]` to repeatedly grill and refine an existing local Pending task.
Both workflows inspect the repository, conduct Q&A, and present the exact tasklist edit for
approval. They modify only the local tasklist after approval and never commit or push it.

## Tests

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m py_compile scripts/tokenshare-controller.py
```
