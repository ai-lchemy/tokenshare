# Tokenshare

Tokenshare is a persistent Python controller plus AIML skill that monitors Git repositories for autonomous coding tasks. It uses the current user's existing Git authentication, clones configured repositories beneath `~/tokenshare-dev`, and processes one task at a time.

## Repository layout

```text
config/task_repos.md
scripts/tokenshare-controller.py
skills/tokenshare/SKILL.md
tests/test_tokenshare_controller.py
install.sh
```

## Quick start

1. Add one repository URL per line to `config/task_repos.md`.
2. Add `tokenshare_tasklist.md` to the root or `docs/` directory of every configured repository.
3. Ensure `git clone`, `git pull`, and `git push` already work with your normal credentials.
4. Start the controller:

```bash
python scripts/tokenshare-controller.py
```

For a finite local test, use:

```bash
python scripts/tokenshare-controller.py --once
```

The default agent command is `codex exec --full-auto -`. Override it with `TOKENSHARE_AGENT_COMMAND` or `--agent-command`.

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
| `TOKENSHARE_CONFIG` | `config/task_repos.md` | Repository-list path |
| `TOKENSHARE_WORKSPACE` | `~/tokenshare-dev` | Clone parent directory |
| `TOKENSHARE_AGENT_COMMAND` | `codex exec --full-auto -` | Agent command; prompt is sent on stdin |
| `TOKENSHARE_POLL_SECONDS` | `60` | Monitor interval |

Command-line flags override environment variables. Run `python scripts/tokenshare-controller.py --help` for all options.

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"
python -m py_compile scripts/tokenshare-controller.py
```
