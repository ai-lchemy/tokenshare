# Tokenshare

Tokenshare is a persistent Python controller plus AIML skill that monitors Git repositories for autonomous coding tasks. It uses the current user's existing Git authentication, clones configured repositories beneath the checkout's `dev/` directory, and processes one task at a time.

## Repository layout

```text
config/task_repos.md      # the repository URL list that matters
dev/                      # cloned task repositories only
scripts/tokenshare-controller.py
skills/tokenshare/SKILL.md
tests/test_tokenshare_controller.py
install.sh
```

## Quick start

1. Add one repository URL per line to `/home/kasm-user/dev/tokenshare/config/task_repos.md`.
2. Add `tokenshare_tasklist.md` to the root or `docs/` directory of every configured repository.
3. Ensure `git clone`, `git pull`, and `git push` already work with your normal credentials.
4. Start the controller:

```bash
python3 scripts/tokenshare-controller.py
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

Coding agents leave their changes uncommitted. After implementation and testing both succeed, the controller commits the complete result and asks for approval before pushing it. Use `--auto-push` or `TOKENSHARE_AUTO_PUSH=1` to pre-approve successful pushes for unattended operation. A failed implementation is never included in a coding-change push.

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

Section names and task states are exact. Each task moves `Pending -> WIP -> Done`. During execution the controller creates `docs/status_YYYY-MM-DD_HH-MM-SS.md` and records `implementing`, `testing`, then `complete`. A failed agent leaves the task WIP.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `TOKENSHARE_CONFIG` | `/home/kasm-user/dev/tokenshare/config/task_repos.md` | Repository-list path |
| `TOKENSHARE_WORKSPACE` | `/home/kasm-user/dev/tokenshare/dev` | Clone parent directory |
| `TOKENSHARE_AGENT_COMMAND` | `codex --full-auto` | Native agent TUI command |
| `TOKENSHARE_AGENT` | unset | Agent stub name or executable path |
| `TOKENSHARE_POLL_SECONDS` | `60` | Monitor interval |
| `TOKENSHARE_AUTO_PUSH` | unset | Set to `1`, `true`, or `yes` to pre-approve successful pushes |
| `TOKENSHARE_AUTO_ATTACH` | unset | Current terminal or explicit TTY for automatic agent attachment |

Command-line flags override environment variables. Run `python scripts/tokenshare-controller.py --help` for all options.

## Tests

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m py_compile scripts/tokenshare-controller.py
```
