# Tokenshare

Tokenshare is a persistent Python controller and AIML skill that watches Git repositories for autonomous coding tasks. It clones configured repositories beneath a user-selected development directory and publishes completed tasks on dedicated review branches.

## Security model

Tokenshare does not sandbox repositories, tasks, coding agents, commands, credentials, or network access. Coding agents execute directly with the permissions and environment of the controller process. Inspect tasks yourself and run Tokenshare only inside a secure, disposable VM that provides the isolation appropriate for your repositories and credentials.

## Quick start

1. Run `python3 install.py`. The installer records the development directory, installs the controller and skill, and installs `prompt_toolkit` when necessary.
2. Add one repository URL per line to `config/task_repos.md`.
3. Add `tokenshare_tasklist.md` to the root or `docs/` directory of every configured repository.
4. Ensure ordinary `git clone`, `git pull`, and `git push` work with the VM user's credentials.
5. Start the interactive controller:

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

## Approval controller

The interactive `prompt_toolkit` interface uses one full-screen renderer with a task pane, scrollable activity pane, fixed command input, and status bar. Background updates never write directly over the `tokenshare>` prompt. The status bar shows active repositories, worker use, uptime, and idle time; the original shell screen returns when the controller exits.

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

Task numbers are stable and never reused. Ranges are inclusive. `view` orders Pending/Unapproved tasks first, followed by Pending/Approved, WIP, and Done, and shows each task's repository, author, approval, lifecycle state, title, and number.

After each fetch, the controller compares the remotely tracked tasklist with its local intake state. New tasks are copied to `<development-directory>/logs/tokenshare_agent_tasklist.md` with source metadata and an `[Unapproved]` tag. Review the complete task there in an editor, then approve it from the controller. Editing or removing an unstarted remote task revokes its old snapshot; edited content receives a new number and requires fresh approval.

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

The controller switches that tmux client between its viewer shell and agent sessions. Mouse-wheel scrolling enters tmux copy mode with up to 50,000 lines of history; `q` leaves copy mode and `Ctrl-b d` detaches the viewer without stopping the agent. Ctrl-C continues to exit the controller when used in the controller TUI. Plain-shell TTYs and the controller's own TTY are rejected because they cannot safely host a remotely spawned full-screen client.

For every approved task, the controller creates `tokenshare-dev-<task-title>-<fingerprint>` from the remote default branch. It moves the task from Pending to WIP on that branch, runs implementation and testing phases, moves it to Done, commits the result, and pushes the review branch. It never pushes, merges, or deletes the remote default branch.

Agents leave code changes uncommitted and append progress plus their phase-completion handshake to the task log supplied by the controller. Failed agent sessions retry indefinitely with incremental backoff and resume native sessions when supported.

All durable logging stays outside monitored repositories:

```text
<development-directory>/logs/tokenshare-controller.log
<development-directory>/logs/tokenshare_agent_tasklist.md
<development-directory>/logs/<task-branch>_log.md
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

## Tests

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m py_compile scripts/tokenshare-controller.py
```
