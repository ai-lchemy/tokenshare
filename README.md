# Tokenshare

Tokenshare connects repository owners who define work with agent owners who review and run it. A persistent Python controller watches Git repositories for autonomous coding tasks, runs approved tasks in a user-selected development directory, and publishes completed work on dedicated review branches.

## Repository-owner workflow

Repository owners publish tasks for an agent owner's controller to discover. They do not need to install or run the controller.

1. Create exactly one `tokenshare_tasklist.md`, either at the repository root or under `docs/`.
2. Add uniquely titled, decision-complete tasks under `## Pending Tasks`. Include everything an unattended coding agent needs: scope, behavior, constraints, edge cases, tests, acceptance criteria, and relevant file or interface context.
3. Review, commit, and push the tasklist to the remote default branch through the repository's normal Git workflow.
4. After the agent owner approves and runs the task, review the published `tokenshare-dev-...` branch normally.

You can create and populate `tokenshare_tasklist.md` manually; using the Tokenshare skill is optional. Start with this template:

```markdown
# Tokenshare Tasklist

## Configuration

allow-multiple-branches: false

## Pending Tasks

### <task> [Pending] Concise Unique Title

#### Objective

Describe the intended outcome and why it matters.

#### Requirements

- State decision-complete functional and technical requirements.

#### Validation

- State exact tests and observable acceptance criteria.

#### Out of scope

- Record important exclusions when needed.

### </task>

## WIP Tasks

## Completed Tasks
```

Omit empty task headings, but keep the tasklist section names and task states exact. Titles must be unique across Pending, WIP, and Completed tasks.

Alternatively, install the skill and use it in Plan mode:

```text
/plan $tokenshare -ct Add the task idea here
/plan $tokenshare -gt Exact Existing Task Title
```

`-ct/--create-task` inspects the repository and proposes a complete Pending task. `-gt/--grill-task` challenges and refines an existing local Pending task by title or position. After explicit approval in execution mode, either command edits only the local tasklist; it never commits or pushes.

Editing or removing an unstarted remote task invalidates the previous snapshot. The agent owner's controller imports the changed task as Unapproved and requires fresh approval.

## Agent-owner quick start

Agent owners run the controller in the trusted environment where coding agents execute.

1. Run `python3 install.py`. This installs the controller, `prompt_toolkit` when needed, and the Tokenshare skill for Codex, Claude Code, and OpenCode.
2. Add one repository URL per line to `config/task_repos.md`.
3. Confirm each repository has exactly one `tokenshare_tasklist.md` at its root or under `docs/`.
4. Ensure the current user's Git credentials can clone, pull, and push each repository.
5. Start the controller and review imported tasks before approving them:

```bash
tokenshare-controller
```

Set the development directory noninteractively during installation:

```bash
python3 install.py -dd /absolute/path/to/tokenshare_dev
```

To synchronize repositories, finish previously approved work, and exit without approving new tasks:

```bash
tokenshare-controller --non-interactive-mode
```

`--dangerously-skip-approvals` immediately runs imported tasks. Use it only when every task is authored and trusted by the agent owner. `-ch/--clear-history` clears controller state, the intake queue, and repository task logs without altering repositories, remote branches, or the controller audit log.

## Security model

Tokenshare does not sandbox repositories, tasks, coding agents, commands, credentials, or network access. Agents inherit the controller process's permissions and environment. Inspect tasks yourself and run Tokenshare only in a secure, disposable VM with appropriate isolation.

The remote tasklist is the trust boundary: a repository owner publishes a task, the controller imports it as Unapproved, and the agent owner decides whether it is safe to execute. The agent owner should send ambiguous tasks back for refinement rather than rewriting imported task text.

## Approval controller

The full-screen TUI contains task and activity panes, command input, and a status bar. Drag the pane divider or use Ctrl-Up/Ctrl-Down to resize it.

```text
view
approve 1,3,7
approve 1:9,11:15
approve all
approve all not 1,3
help
quit
```

Ranges are inclusive. Approval numbers are temporary and apply only to Unapproved tasks; after approval, remaining tasks are renumbered from 1. `view` shows each task's repository, author, approval state, lifecycle state, and title. Imported snapshots are stored at `<development-directory>/logs/agent/tokenshare_agent_tasklist.md` for full review.

Use `--workers N` to process different repositories concurrently. Only one task may run per repository. `allow-multiple-branches: true` permits another task to be published while an earlier review branch remains unmerged.

## Agents, branches, and logs

The default agent is `codex --dangerously-bypass-approvals-and-sandbox` in a detached tmux session. Select a bundled stub with `-a/--agent`, provide `--agent-command`, or use `--no-tmux` for tests and noninteractive agents.

For optional automatic viewing, start a separate tmux client and pass its TTY to the controller:

```bash
# Viewer terminal
tmux new-session -A -s tokenshare-viewer
tty

# Controller terminal; use the path printed by tty
tokenshare-controller --auto-attach /dev/pts/9
```

The viewer supports tmux copy mode (`q` exits it) and `Ctrl-b d` detaches without stopping the agent. Plain-shell TTYs and the controller's own TTY are rejected.

For each approved task, the controller creates `tokenshare-dev-<task-title>-<fingerprint>` from the remote default branch, moves the task from Pending to WIP to Done, runs implementation and tests, commits the result, and pushes the review branch. It never pushes to, merges, or deletes the remote default branch. Failed agent sessions retry with incremental backoff and resume native sessions when supported.

Durable logs remain outside monitored repositories:

```text
<development-directory>/logs/agent/tokenshare-controller.log
<development-directory>/logs/agent/tokenshare_agent_tasklist.md
<development-directory>/logs/repos/<task-branch>_log.md
```

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `TOKENSHARE_CONFIG` | `<installation>/config/task_repos.md` | Repository-list path |
| `TOKENSHARE_WORKSPACE` | Installed development directory | Clone parent and log directory |
| `TOKENSHARE_AGENT_COMMAND` | `codex --dangerously-bypass-approvals-and-sandbox` | Native agent command |
| `TOKENSHARE_AGENT` | unset | Agent stub name or path |
| `TOKENSHARE_POLL_SECONDS` | `60` | Remote polling interval |
| `TOKENSHARE_WORKERS` | `1` | Maximum concurrent repositories |
| `TOKENSHARE_AUTO_ATTACH` | unset | Automatic viewer TTY |
| `TOKENSHARE_STATE` | `~/.config/tokenshare/state.json` | Controller state path |

Command-line flags override environment variables. Run `tokenshare-controller --help` for details.

## Tests

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m py_compile scripts/tokenshare-controller.py
```
