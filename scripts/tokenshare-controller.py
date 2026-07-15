#!/usr/bin/env python3
"""Persistent unattended controller for Tokenshare task repositories."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Iterable, Sequence
from urllib.parse import urlparse


class TokenshareError(RuntimeError):
    """A user-actionable controller error."""


@dataclasses.dataclass(frozen=True)
class Task:
    state: str
    title: str
    body: str
    start: int
    end: int


TASK_START = re.compile(
    r"^###\s+<task>\s+\[(Pending|WIP|Done)\]\s+(.+?)\s*$", re.MULTILINE
)
TASK_END = re.compile(r"^###\s+</task>\s*$", re.MULTILINE)
SECTION_FOR_STATE = {
    "Pending": "Pending Tasks",
    "WIP": "WIP Tasks",
    "Done": "Completed Tasks",
}


def log(message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise TokenshareError(f"Command failed ({' '.join(args)}): {detail}")
    return result


def read_repo_urls(config: Path) -> list[str]:
    if not config.is_file():
        raise TokenshareError(f"Repository config not found: {config}")
    urls: list[str] = []
    in_fence = False
    for raw_line in config.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not line or in_fence or line.startswith(("#", "<!--")):
            continue
        urls.append(line)
    if not urls:
        raise TokenshareError(f"No repository URLs configured in {config}")
    if len(urls) != len(set(urls)):
        raise TokenshareError(f"Duplicate repository URL in {config}")
    return urls


def repo_name(url: str) -> str:
    candidate = urlparse(url).path if "://" in url else url.rsplit(":", 1)[-1]
    name = Path(candidate.rstrip("/")).name
    if name.endswith(".git"):
        name = name[:-4]
    if not name or name in {".", ".."}:
        raise TokenshareError(f"Cannot determine repository name from {url!r}")
    return name


def ensure_clean(repo: Path) -> None:
    dirty = run(["git", "status", "--porcelain"], cwd=repo).stdout.strip()
    if dirty:
        raise TokenshareError(f"Refusing to update dirty checkout {repo}:\n{dirty}")


def sync_repo(url: str, workspace: Path) -> Path:
    destination = workspace / repo_name(url)
    workspace.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not (destination / ".git").exists():
            raise TokenshareError(f"Clone destination exists but is not a Git repo: {destination}")
        ensure_clean(destination)
        configured = run(["git", "remote", "get-url", "origin"], cwd=destination).stdout.strip()
        if configured != url:
            raise TokenshareError(
                f"Origin mismatch for {destination}: configured {configured!r}, expected {url!r}"
            )
        run(["git", "fetch", "--prune", "origin"], cwd=destination)
        run(["git", "pull", "--ff-only"], cwd=destination)
    else:
        result = run(["git", "clone", "--", url, str(destination)], check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise TokenshareError(
                f"Cannot access or clone {url!r} with the current user's Git credentials: {detail}"
            )
    return destination


def find_tasklist(repo: Path) -> Path:
    candidates = [repo / "tokenshare_tasklist.md", repo / "docs" / "tokenshare_tasklist.md"]
    found = [path for path in candidates if path.is_file()]
    if len(found) > 1:
        raise TokenshareError(
            f"Both root and docs tasklists exist in {repo}; keep exactly one"
        )
    if not found:
        raise TokenshareError(
            f"Missing tokenshare_tasklist.md in {repo} (expected repo root or docs/)"
        )
    return found[0]


def section_ranges(text: str) -> dict[str, tuple[int, int]]:
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    ranges: dict[str, tuple[int, int]] = {}
    for index, heading in enumerate(headings):
        name = heading.group(1)
        if name not in SECTION_FOR_STATE.values():
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        ranges[name] = (heading.end(), end)
    missing = [name for name in SECTION_FOR_STATE.values() if name not in ranges]
    if missing:
        raise TokenshareError(f"Tasklist missing section(s): {', '.join(missing)}")
    return ranges


def parse_tasks(text: str) -> list[Task]:
    ranges = section_ranges(text)
    tasks: list[Task] = []
    cursor = 0
    while True:
        start_match = TASK_START.search(text, cursor)
        if not start_match:
            break
        end_match = TASK_END.search(text, start_match.end())
        if not end_match:
            raise TokenshareError(
                f"Task {start_match.group(2)!r} has no closing '### </task>'"
            )
        nested = TASK_START.search(text, start_match.end(), end_match.start())
        if nested:
            raise TokenshareError(f"Nested task block near {start_match.group(2)!r}")
        block_end = end_match.end()
        if block_end < len(text) and text[block_end : block_end + 2] == "\r\n":
            block_end += 2
        elif block_end < len(text) and text[block_end] == "\n":
            block_end += 1
        state = start_match.group(1)
        expected_section = SECTION_FOR_STATE[state]
        section_start, section_end = ranges[expected_section]
        if not (section_start <= start_match.start() < section_end):
            raise TokenshareError(
                f"Task {start_match.group(2)!r} is [{state}] but is not under ## {expected_section}"
            )
        tasks.append(
            Task(
                state=state,
                title=start_match.group(2).strip(),
                body=text[start_match.start() : block_end],
                start=start_match.start(),
                end=block_end,
            )
        )
        cursor = block_end
    return tasks


def transition_task(text: str, task: Task, target_state: str) -> str:
    allowed = {("Pending", "WIP"), ("WIP", "Done")}
    if (task.state, target_state) not in allowed:
        raise TokenshareError(f"Invalid task transition: {task.state} -> {target_state}")
    current = text[task.start : task.end]
    if current != task.body:
        raise TokenshareError("Tasklist changed while a task transition was being prepared")
    updated = re.sub(
        rf"\[{re.escape(task.state)}\]",
        f"[{target_state}]",
        current,
        count=1,
    ).strip("\r\n") + "\n"
    remaining = text[: task.start] + text[task.end :]
    ranges = section_ranges(remaining)
    _, insertion = ranges[SECTION_FOR_STATE[target_state]]
    prefix = remaining[:insertion].rstrip("\r\n") + "\n"
    suffix = remaining[insertion:].lstrip("\r\n")
    return prefix + updated + ("\n" if suffix else "") + suffix


def write_status(path: Path, task: Task, state: str, note: str = "") -> None:
    if state not in {"implementing", "testing", "complete", "failed"}:
        raise ValueError(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = path.read_text(encoding="utf-8") if path.exists() else (
        f"# Tokenshare Task Status\n\n- Task: {task.title}\n"
    )
    entry = f"\n## {now}\n\n- State: {state}\n"
    if note:
        entry += f"- Note: {note.replace(chr(10), ' ')}\n"
    path.write_text(existing + entry, encoding="utf-8")


def git_commit_push(repo: Path, paths: Iterable[Path], message: str) -> None:
    relative = [str(path.relative_to(repo)) for path in paths]
    run(["git", "add", "--", *relative], cwd=repo)
    staged = run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False)
    if staged.returncode == 0:
        return
    if staged.returncode != 1:
        raise TokenshareError(f"Unable to inspect staged changes in {repo}")
    run(["git", "commit", "-m", message], cwd=repo)
    run(["git", "push"], cwd=repo)


def agent_prompt(task: Task, status_path: Path, phase: str) -> str:
    if phase == "implementing":
        action = "Implement every requirement in the task. Add or update tests as appropriate."
    else:
        action = (
            "Review the implementation for this task, run the appropriate test suite, fix all "
            "failures or omissions, and verify the result."
        )
    return f"""You are the unattended Tokenshare coding agent in the target repository.
There is no human interface. Do not ask questions. Use the task alone, inspect repository
instructions, and make the smallest reasonable assumptions needed to finish.

Phase: {phase}
Status file: {status_path}

{task.body.rstrip()}

{action}
Commit and push all code and test changes using the current Git configuration before exiting.
Do not edit tokenshare_tasklist.md; the controller owns task transitions.
"""


def run_agent(repo: Path, command: str, task: Task, status_path: Path, phase: str) -> None:
    args = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        args = [
            value[1:-1]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}
            else value
            for value in args
        ]
    if not args:
        raise TokenshareError("Agent command is empty")
    env = os.environ.copy()
    env.update(
        {
            "TOKENSHARE_TASK_TITLE": task.title,
            "TOKENSHARE_TASK_STATE": phase,
            "TOKENSHARE_STATUS_FILE": str(status_path),
        }
    )
    result = subprocess.run(
        args,
        cwd=repo,
        input=agent_prompt(task, status_path, phase),
        text=True,
        env=env,
    )
    if result.returncode:
        raise TokenshareError(
            f"Coding agent exited with status {result.returncode} during {phase}"
        )


def status_filename(repo: Path) -> Path:
    timestamp = dt.datetime.now()
    candidate = repo / "docs" / f"status_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    while candidate.exists():
        timestamp += dt.timedelta(seconds=1)
        candidate = repo / "docs" / f"status_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    return candidate


def process_task(repo: Path, tasklist: Path, task: Task, agent_command: str) -> None:
    log(f"Claiming {repo.name}: {task.title}")
    status_path = status_filename(repo)
    write_status(status_path, task, "implementing")
    text = tasklist.read_text(encoding="utf-8")
    tasklist.write_text(transition_task(text, task, "WIP"), encoding="utf-8")
    git_commit_push(repo, [tasklist, status_path], f"tokenshare: claim {task.title}")

    wip_task = next(
        item
        for item in parse_tasks(tasklist.read_text(encoding="utf-8"))
        if item.state == "WIP" and item.title == task.title
    )
    try:
        run_agent(repo, agent_command, wip_task, status_path, "implementing")
        ensure_clean(repo)
        write_status(status_path, wip_task, "testing")
        git_commit_push(repo, [status_path], f"tokenshare: test {task.title}")
        run_agent(repo, agent_command, wip_task, status_path, "testing")
        ensure_clean(repo)
        write_status(status_path, wip_task, "complete")
        current = tasklist.read_text(encoding="utf-8")
        refreshed = next(
            item
            for item in parse_tasks(current)
            if item.state == "WIP" and item.title == task.title
        )
        tasklist.write_text(transition_task(current, refreshed, "Done"), encoding="utf-8")
        git_commit_push(repo, [tasklist, status_path], f"tokenshare: complete {task.title}")
    except Exception as exc:
        write_status(status_path, wip_task, "failed", str(exc))
        try:
            git_commit_push(repo, [status_path], f"tokenshare: record failure for {task.title}")
        except Exception as record_error:
            log(f"Could not push failure status: {record_error}")
        raise
    log(f"Completed {repo.name}: {task.title}")


def scan_repositories(repos: Sequence[Path], agent_command: str) -> int:
    tasklists = [(repo, find_tasklist(repo)) for repo in repos]
    completed = 0
    while True:
        parsed = [
            (repo, tasklist, parse_tasks(tasklist.read_text(encoding="utf-8")))
            for repo, tasklist in tasklists
        ]
        wip = [
            (repo, task) for repo, _, tasks in parsed for task in tasks if task.state == "WIP"
        ]
        if wip:
            names = ", ".join(f"{repo.name}: {task.title}" for repo, task in wip)
            raise TokenshareError(
                f"Existing WIP task(s) require attention before continuing: {names}"
            )
        pending = next(
            (
                (repo, tasklist, task)
                for repo, tasklist, tasks in parsed
                for task in tasks
                if task.state == "Pending"
            ),
            None,
        )
        if pending is None:
            return completed
        process_task(*pending, agent_command)
        completed += 1


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[1]
    checkout_config = project_root / "config" / "task_repos.md"
    installed_config = Path.home() / ".config" / "tokenshare" / "task_repos.md"
    default_config = checkout_config if checkout_config.is_file() else installed_config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("TOKENSHARE_CONFIG", default_config)),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("TOKENSHARE_WORKSPACE", Path.home() / "tokenshare-dev")),
    )
    parser.add_argument(
        "--agent-command",
        default=os.environ.get("TOKENSHARE_AGENT_COMMAND", "codex exec --full-auto -"),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("TOKENSHARE_POLL_SECONDS", "60")),
    )
    parser.add_argument("--once", action="store_true", help="Drain current Pending tasks and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_seconds <= 0:
        raise TokenshareError("--poll-seconds must be greater than zero")
    urls = read_repo_urls(args.config.expanduser().resolve())
    names = [repo_name(url) for url in urls]
    if len(names) != len(set(names)):
        raise TokenshareError("Configured repositories must have unique repository names")

    while True:
        repos = [sync_repo(url, args.workspace.expanduser().resolve()) for url in urls]
        completed = scan_repositories(repos, args.agent_command)
        if args.once:
            return 0
        if not completed:
            log(f"No Pending tasks; checking again in {args.poll_seconds:g} seconds")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TokenshareError, OSError, subprocess.SubprocessError) as exc:
        log(f"ERROR: {exc}")
        raise SystemExit(1)
