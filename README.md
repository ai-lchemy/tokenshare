# Tokenshare

Tokenshare is a persistent Python controller plus AIML skill that monitors Git repositories for autonomous coding tasks. It uses the current user's existing Git authentication, clones configured repositories beneath a user-selected development directory, and publishes each completed task on its own review branch.

## Repository layout

```text
config/task_repos.md      # the repository URL list that matters
scripts/tokenshare-controller.py
skills/tokenshare/SKILL.md
tests/test_tokenshare_controller.py
install.py
```

## Quick start

1. Run `python3 install.py`. Enter the absolute development directory in which task repositories should be cloned, or press Enter for `~/tokenshare_dev/`.
2. Add one repository URL per line to `config/task_repos.md` in the Tokenshare installation directory.
3. Add `tokenshare_tasklist.md` to the root or `docs/` directory of every configured repository.
4. Ensure `git clone`, `git pull`, and `git push` already work with your normal credentials.
5. Start the controller:

```bash
python3 scripts/tokenshare-controller.py
```

The development directory can also be supplied noninteractively. It must be absolute, and is created when necessary:

```bash
python3 install.py -dd /absolute/path/to/tokenshare_dev
```

For a finite local test, use:

```bash
python3 scripts/tokenshare-controller.py --once
```

The default agent command is the native `codex --full-auto` TUI in a detached tmux session. Set `TOKENSHARE_AGENT_COMMAND` to `claude`, `opencode`, or another interactive coding-agent command. Attach while it runs with the session command shown by the controller. `--no-tmux` is available for tests and noninteractive automation.

The controller suppresses directory and permission prompts for supported coding agents without modifying their global configuration. Codex receives a one-run trusted-project override, Claude Code receives `--dangerously-skip-permissions`, and OpenCode receives `--auto` plus an inline allow-all permission policy (including external-directory access). Provider-prefixed stubs such as `claude-sonnet.sh` and `opencode-gpt.sh` receive the corresponding flags automatically.

Select a command stub by name with `-a`/`--agent`. Names are resolved from `skills/tokenshare/scripts/agent-stubs/`, with or without the `.sh` extension. An executable path is also accepted:

```bash
python3 scripts/tokenshare-controller.py -a codex-gpt-56-sol
python3 scripts/tokenshare-controller.py --agent /path/to/custom-agent-stub
```

`--agent-command` remains available as the mutually exclusive raw-command override.

The controller keeps the active repository, task summary, uptime, and idle time pinned to the bottom of the terminal. Agent output stays in tmux; the controller feed only displays timestamped `status.md` changes and idle repository checks.

If an agent exits with an error—such as a capacity, rate-limit, usage-limit, or network failure—the controller waits 5 seconds and continues its native session. Repeated failures wait 10, 15, 20 seconds, and so on until the session succeeds. Codex uses `resume --last`, Claude Code uses `--continue`, and OpenCode uses `--continue`; unknown custom agents are restarted with the original prompt.

Native TUIs do not need to exit when their work is done. Each phase prompt supplies a phase-specific completion marker that the agent appends to `status.md` only after finishing. The controller detects that handshake, closes the child tmux session, moves from implementation to testing, and then finalizes the task after the testing handshake.

Use `--auto-attach` to show every agent phase and retry in the controller's current terminal, or provide an exact terminal such as `--auto-attach /dev/pts/3`. `TOKENSHARE_AUTO_ATTACH` accepts `current`, a truthy value, or a TTY path. Existing tmux clients are switched to the agent and restored afterward; plain terminals are attached directly. Invalid or disappearing targets stop processing instead of silently running detached. `--auto-attach` cannot be combined with `--no-tmux`.

Coding agents leave their changes uncommitted. The controller creates a branch named `tokenshare-dev-<task-title>-<fingerprint>` from the remote default branch, then pushes the WIP claim, testing status, failure status, and completed result to that branch. It never pushes, merges, or deletes the remote default branch. Maintainers can inspect the branch directly or open a pull/merge request using their hosting service. This uses ordinary Git operations and does not depend on a GitHub, GitLab, or Bitbucket API.

By default, an unmerged Tokenshare branch blocks later tasks in the same repository while other repositories continue. To permit several completed review branches at once, add this before the task sections:

```markdown
## Configuration

allow-multiple-branches: true
```

Agents still run sequentially, and every branch starts independently from the latest remote default branch. Multiple branches can therefore require tasklist conflict resolution when merged out of order. Configuration keys and boolean values are validated strictly.

Incomplete branches resume automatically after a restart. A branch deleted without being merged is treated as declined, and its unchanged task is skipped using local state in `~/.config/tokenshare/state.json`. Editing the task body makes it eligible again. Task titles must be unique and act as stable identities; a rename intentionally creates a different task.

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

Section names and task states are exact. Each task moves `Pending -> WIP -> Done` on its review branch; the default branch remains Pending until a maintainer merges that branch. During execution the controller creates `docs/status_YYYY-MM-DD_HH-MM-SS.md` with the task fingerprint and branch name, then records `implementing`, `testing`, and `complete`. A failed task remains WIP on its branch.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `TOKENSHARE_CONFIG` | `<installation directory>/config/task_repos.md` | Repository-list path |
| `TOKENSHARE_WORKSPACE` | development directory selected during installation (default `~/tokenshare_dev`) | Clone parent directory |
| `TOKENSHARE_AGENT_COMMAND` | `codex --full-auto` | Native agent TUI command |
| `TOKENSHARE_AGENT` | unset | Agent stub name or executable path |
| `TOKENSHARE_POLL_SECONDS` | `60` | Monitor interval |
| `TOKENSHARE_AUTO_ATTACH` | unset | Current terminal or explicit TTY for automatic agent attachment |
| `TOKENSHARE_STATE` | `~/.config/tokenshare/state.json` | Local branch publication and decline state |

Command-line flags override environment variables. Run `python scripts/tokenshare-controller.py --help` for all options.

## Tests

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m py_compile scripts/tokenshare-controller.py
```
