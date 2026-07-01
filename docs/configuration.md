# token-share operator guide

`token-share` is a deterministic shell runner for a GitLab-hosted markdown task queue. Install it by copying `tokenshare.sh` to `~/tokenshare/tokenshare.sh` and making it executable.

## Configuration

Set these environment variables before running the script:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `TASK_LIST_REPO_URL` | Yes | none | GitLab repository URL containing the markdown task list. |
| `TASK_LIST_FILE` | No | `TASKS.md` | Markdown file to poll inside the task-list repo. |
| `TASK_LIST_REPO_DIR` | No | `~/tokenshare/TASK_REPO` | Local clone path for the task-list repo. |
| `TASK_TARGET_REPO_URL` | No | none | Fallback target repo URL when a task omits `Repo`. |
| `TASK_TARGET_REPO_DIR` | No | `~/tokenshare/target-repo` | Local clone path for the development repo. |
| `TOK_SHARE_AGENT` | No | `tokenshare-agent` | Command invoked from the target repo worktree. |
| `TOK_SHARE_SLEEP_SECONDS` | No | `300` | Idle sleep interval when no pending tasks exist. |
| `TOK_SHARE_BRANCH` | No | current default | Optional branch to check out and pull for the task-list repo. |
| `TOK_SHARE_ONCE` | No | `0` | Set to `1` for a single polling iteration, useful in tests or cron probes. |

Example:

```bash
mkdir -p ~/tokenshare
cp ./tokenshare.sh ~/tokenshare/tokenshare.sh
chmod +x ~/tokenshare/tokenshare.sh
TASK_LIST_REPO_URL=git@gitlab.com:example/token-share-tasks.git \
TOK_SHARE_AGENT=tokenshare-agent \
~/tokenshare/tokenshare.sh
```

## Task format

Tasks must be markdown blocks delimited by `tokenshare-task:start` and `tokenshare-task:end` comments. Blank lines and arbitrary markdown are allowed inside a block.

```markdown
<!-- tokenshare-task:start -->
- State: Pending
- Repo: git@gitlab.com:example/product.git
- Title: Add the billing export endpoint

Implement the requested endpoint, update tests, and run /git-commit.
<!-- tokenshare-task:end -->
```

Valid states are exactly `Pending`, `WIP`, and `Done`. The runner always selects the bottom-most valid `Pending` task in the file.

## Safety guarantees

- The parser ignores malformed blocks, unknown states, unterminated blocks, and tasks missing the documented state line.
- The script changes only the first `- State:` marker inside the selected task block.
- State transitions are constrained to `Pending -> WIP` before agent execution and `WIP -> Done` after agent completion.
- The task-list repository is fetched and pulled before every selection and again before the final `Done` transition.
- The target repository is cloned into `~/tokenshare/target-repo`, and development happens in a new Git worktree named `tokenshare_YYYY-MM-DD_HH:MM:SS` under `~/tokenshare`.
- A task is not marked `Done` unless the agent returns successfully and the latest commit message in the worktree contains `/git-commit`.

## Operator workflow

1. Add well-formed `Pending` task blocks to the task-list GitLab repository.
2. Start `~/tokenshare/tokenshare.sh` on a worker machine with Git credentials configured for both repositories.
3. The runner claims the bottom-most pending task by committing and pushing `WIP`.
4. The configured `tokenshare-agent` runs from the generated target repo worktree.
5. The agent must finish development and run `/git-commit` so the latest commit proves the work was committed.
6. The runner commits and pushes the final `Done` state.
7. If no pending tasks exist, the runner sleeps for 5 minutes and polls again.
