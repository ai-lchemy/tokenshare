#!/usr/bin/env python3
"""Persistent unattended controller for Tokenshare task repositories."""

from __future__ import annotations

import argparse
from collections import deque
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Iterable, Sequence
from urllib.parse import urlparse


class TokenshareError(RuntimeError):
    """A user-actionable controller error."""


class AttachmentError(TokenshareError):
    """A non-retryable failure involving the requested attachment terminal."""


class ControllerStopped(TokenshareError):
    """Internal signal used to stop active agent monitoring cleanly."""


@dataclasses.dataclass(frozen=True)
class Task:
    state: str
    title: str
    body: str
    start: int
    end: int


@dataclasses.dataclass
class QueueTask:
    number: int | None
    task_id: str
    state: str
    approval: str
    title: str
    body: str
    repo_name: str
    repo_url: str
    author: str
    source_commit: str
    imported_at: str
    branch: str


@dataclasses.dataclass(frozen=True)
class TasklistConfig:
    allow_multiple_branches: bool = False


@dataclasses.dataclass(frozen=True)
class ManagedBranch:
    name: str
    task_id: str
    title: str
    state: str
    log_path: Path | None
    head: str


@dataclasses.dataclass(frozen=True)
class AttachTarget:
    path: Path
    is_controller_tty: bool


TASK_START = re.compile(
    r"^###\s+<task>\s+\[(Pending|WIP|Done)\]\s+(.+?)\s*$", re.MULTILINE
)
TASK_MARKER = re.compile(r"^###\s+<task>.*$", re.MULTILINE)
TASK_END = re.compile(r"^###\s+</task>\s*$", re.MULTILINE)
SECTION_FOR_STATE = {
    "Pending": "Pending Tasks",
    "WIP": "WIP Tasks",
    "Done": "Completed Tasks",
}
BRANCH_PREFIX = "tokenshare-dev-"
QUEUE_TASK_START = re.compile(
    r"^###\s+<task>\s+\[(Pending|WIP|Done)\]\s+"
    r"\[(Unapproved|Approved)\]\s+(.+?)\s*$", re.MULTILINE
)
QUEUE_METADATA = re.compile(r"^- ([A-Za-z-]+):\s*(.*?)\s*$", re.MULTILINE)
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
QUEUE_LOCK = threading.RLock()
CONTROLLER_LOG: Path | None = None
LOG_LISTENERS: list[Callable[[str], None]] = []
CONTROLLER_STOP_EVENT: threading.Event | None = None


class Dashboard:
    """A compact terminal feed with task information pinned to the bottom."""

    def __init__(self, stream=None, *, enabled: bool | None = None) -> None:
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self.started = time.monotonic()
        self.idle_since = self.started
        self.repo = "Idle"
        self.summary = "No active task"
        self.active = False
        self.suspended = False
        self._pending_messages: list[str] = []
        self._footer_lines = 0

    @staticmethod
    def _stamp() -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _erase_footer(self) -> None:
        if self.enabled and self._footer_lines:
            self.stream.write(f"\x1b[{self._footer_lines}A\x1b[J")

    def _draw_footer(self) -> None:
        if not self.enabled or self.suspended:
            return
        now = time.monotonic()
        lines = [
            f"Active task repo: {self.repo}",
            f"Active task summary: {self.summary}",
            f"Uptime: {self._duration(now - self.started)} | "
            f"Idle: {self._duration(0 if self.active else now - self.idle_since)}",
        ]
        self.stream.write("\n".join(lines) + "\n")
        self.stream.flush()
        self._footer_lines = len(lines)

    def message(self, message: str) -> None:
        rendered = f"[{self._stamp()}] {message}"
        if self.suspended:
            self._pending_messages.append(rendered)
            return
        self._erase_footer()
        print(rendered, file=self.stream, flush=True)
        self._draw_footer()

    def set_task(self, repo: Path | None, summary: str | None = None) -> None:
        self._erase_footer()
        if repo is None:
            self.active = False
            self.repo = "Idle"
            self.summary = "No active task"
            self.idle_since = time.monotonic()
        else:
            self.active = True
            self.repo = str(repo)
            self.summary = summary or ""
        self._draw_footer()

    def refresh(self) -> None:
        if self.suspended:
            return
        self._erase_footer()
        self._draw_footer()

    def suspend(self) -> None:
        if not self.suspended:
            self._erase_footer()
            self.suspended = True
            self._footer_lines = 0

    def resume(self) -> None:
        if not self.suspended:
            return
        self.suspended = False
        for message in self._pending_messages:
            print(message, file=self.stream)
        self._pending_messages.clear()
        self._draw_footer()


DASHBOARD = Dashboard()


def audit(message: str) -> None:
    """Append to the durable controller log without rendering terminal output."""
    rendered = f"[{dt.datetime.now(dt.timezone.utc).strftime(UTC_FORMAT)}] {message}"
    with QUEUE_LOCK:
        if CONTROLLER_LOG is not None:
            CONTROLLER_LOG.parent.mkdir(parents=True, exist_ok=True)
            with CONTROLLER_LOG.open("a", encoding="utf-8") as stream:
                stream.write(rendered + "\n")


def log(message: str) -> None:
    rendered = f"[{dt.datetime.now(dt.timezone.utc).strftime(UTC_FORMAT)}] {message}"
    with QUEUE_LOCK:
        if CONTROLLER_LOG is not None:
            CONTROLLER_LOG.parent.mkdir(parents=True, exist_ok=True)
            with CONTROLLER_LOG.open("a", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        for listener in list(LOG_LISTENERS):
            listener(rendered)
        if not LOG_LISTENERS:
            DASHBOARD.message(message)


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
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
        dirty = run(["git", "status", "--porcelain"], cwd=destination).stdout.strip()
        branch = run(["git", "branch", "--show-current"], cwd=destination).stdout.strip()
        if dirty and not branch.startswith(BRANCH_PREFIX):
            raise TokenshareError(f"Refusing to update dirty checkout {destination}:\n{dirty}")
        configured = run(["git", "remote", "get-url", "origin"], cwd=destination).stdout.strip()
        if configured != url:
            raise TokenshareError(
                f"Origin mismatch for {destination}: configured {configured!r}, expected {url!r}"
            )
        run(["git", "fetch", "--prune", "origin"], cwd=destination)
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
    for marker in TASK_MARKER.finditer(text):
        if TASK_START.match(text, marker.start()) is None:
            header = marker.group(0).strip()
            raise TokenshareError(
                f"Malformed task header {header!r}; expected "
                "'### <task> [Pending|WIP|Done] Task title'"
            )
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
    titles = [task.title for task in tasks]
    duplicates = sorted({title for title in titles if titles.count(title) > 1})
    if duplicates:
        raise TokenshareError(f"Duplicate task title(s): {', '.join(duplicates)}")
    return tasks


def parse_tasklist_config(text: str) -> TasklistConfig:
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    matches = [(index, heading) for index, heading in enumerate(headings)
               if heading.group(1) == "Configuration"]
    if not matches:
        return TasklistConfig()
    if len(matches) > 1:
        raise TokenshareError("Tasklist has duplicate ## Configuration sections")
    index, heading = matches[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
    values: dict[str, bool] = {}
    for raw_line in text[heading.end():end].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("<!--"):
            continue
        match = re.fullmatch(r"([a-z0-9-]+)\s*:\s*(true|false)", line)
        if not match:
            raise TokenshareError(f"Malformed Tokenshare configuration line: {line!r}")
        key, raw_value = match.groups()
        if key != "allow-multiple-branches":
            raise TokenshareError(f"Unknown Tokenshare configuration key: {key}")
        if key in values:
            raise TokenshareError(f"Duplicate Tokenshare configuration key: {key}")
        values[key] = raw_value == "true"
    return TasklistConfig(values.get("allow-multiple-branches", False))


def task_fingerprint(task: Task) -> str:
    body = re.sub(
        r"^(###\s+<task>\s+)\[(?:Pending|WIP|Done)\]",
        r"\1",
        task.body.replace("\r\n", "\n").strip(),
        count=1,
    )
    canonical = f"{task.title.strip()}\n{body}\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def task_branch_name(task: Task) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:48] or "task"
    return f"{BRANCH_PREFIX}{slug}-{task_fingerprint(task)[:8]}"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(UTC_FORMAT)


def task_log_path(logs_dir: Path, branch: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip(".-") or "task"
    return logs_dir / f"{safe}_log.md"


def append_task_log(path: Path, event: str, note: str = "") -> None:
    with QUEUE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = "# Tokenshare Task Log\n" if not path.exists() else ""
        entry = f"\n## {utc_now()}\n\n- Event: {event}\n"
        if note:
            entry += f"- Note: {note.replace(chr(10), ' ')}\n"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(heading + entry)


def queue_path(workspace: Path) -> Path:
    return workspace / "logs" / "agent" / "tokenshare_agent_tasklist.md"


def controller_log_path(workspace: Path) -> Path:
    return workspace / "logs" / "agent" / "tokenshare-controller.log"


def repository_logs_path(workspace: Path) -> Path:
    return workspace / "logs" / "repos"


def parse_queue(text: str) -> list[QueueTask]:
    tasks: list[QueueTask] = []
    cursor = 0
    while True:
        match = QUEUE_TASK_START.search(text, cursor)
        if match is None:
            break
        end = TASK_END.search(text, match.end())
        if end is None:
            raise TokenshareError(f"Local task {match.group(3)!r} has no closing marker")
        block_end = end.end()
        if text[block_end:block_end + 2] == "\r\n":
            block_end += 2
        elif text[block_end:block_end + 1] == "\n":
            block_end += 1
        block = text[match.start():block_end]
        metadata = dict(QUEUE_METADATA.findall(block))
        required = {
            "Task-ID", "Source-Repo", "Source-URL", "Author",
            "Source-Commit", "Imported-At", "Branch",
        }
        missing = sorted(required - metadata.keys())
        if missing:
            raise TokenshareError(
                f"Local task {match.group(3)!r} missing metadata: {', '.join(missing)}"
            )
        raw_number = metadata.get("Task-Number")
        try:
            number = int(raw_number) if raw_number is not None else None
        except ValueError as exc:
            raise TokenshareError("Task-Number must be an integer") from exc
        if (
            match.group(1) == "Pending"
            and match.group(2) == "Unapproved"
            and number is None
        ):
            raise TokenshareError("Pending Unapproved tasks require Task-Number")
        tasks.append(QueueTask(
            number, metadata["Task-ID"], match.group(1), match.group(2),
            match.group(3).strip(), block, metadata["Source-Repo"],
            metadata["Source-URL"], metadata["Author"], metadata["Source-Commit"],
            metadata["Imported-At"], metadata["Branch"],
        ))
        cursor = block_end
    numbers = [
        task.number for task in tasks
        if task.state == "Pending" and task.approval == "Unapproved"
        and task.number is not None
    ]
    if len(numbers) != len(set(numbers)):
        raise TokenshareError("Duplicate local task numbers")
    return tasks


def normalize_queue_numbers(tasks: Sequence[QueueTask]) -> bool:
    """Keep numbers temporary, contiguous, and exclusive to unapproved tasks."""
    changed = False
    next_number = 1
    for task in tasks:
        wanted = (
            next_number
            if task.state == "Pending" and task.approval == "Unapproved"
            else None
        )
        if wanted is not None:
            next_number += 1
        if task.number != wanted:
            task.number = wanted
            changed = True
    return changed


def render_queue(tasks: Sequence[QueueTask]) -> str:
    normalize_queue_numbers(tasks)
    lines = ["# Tokenshare Agent Tasklist", ""]
    for state, heading in (("Pending", "Pending Tasks"), ("WIP", "WIP Tasks"),
                           ("Done", "Completed Tasks")):
        lines.extend([f"## {heading}", ""])
        section_tasks = [item for item in tasks if item.state == state]
        if state == "Pending":
            section_tasks.sort(key=lambda item: (
                item.approval != "Unapproved",
                item.number if item.number is not None else 0,
                item.imported_at,
            ))
        for task in section_tasks:
            original_lines = task.body.splitlines()
            body_start = next(
                (index + 1 for index, line in enumerate(original_lines)
                 if line.startswith("- Branch:")),
                1,
            )
            payload = original_lines[body_start:]
            while payload and not payload[-1].strip():
                payload = payload[:-1]
            if payload and payload[-1].strip() == "### </task>":
                payload = payload[:-1]
            metadata_lines = [
                f"### <task> [{task.state}] [{task.approval}] {task.title}",
            ]
            if task.number is not None:
                metadata_lines.append(f"- Task-Number: {task.number}")
            metadata_lines.extend([
                f"- Task-ID: {task.task_id}",
                f"- Source-Repo: {task.repo_name}",
                f"- Source-URL: {task.repo_url}",
                f"- Author: {task.author}",
                f"- Source-Commit: {task.source_commit}",
                f"- Imported-At: {task.imported_at}",
                f"- Branch: {task.branch}",
            ])
            lines.extend([*metadata_lines, *payload, "### </task>", ""])
    return "\n".join(lines).rstrip() + "\n"


def save_queue(path: Path, tasks: Sequence[QueueTask]) -> None:
    with QUEUE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(render_queue(tasks), encoding="utf-8")
        temporary.replace(path)


def load_queue(path: Path) -> list[QueueTask]:
    if not path.exists():
        save_queue(path, [])
    return parse_queue(path.read_text(encoding="utf-8"))


def migrate_queue(path: Path) -> list[QueueTask]:
    tasks = load_queue(path)
    if normalize_queue_numbers(tasks):
        save_queue(path, tasks)
    return tasks


def queue_task_from_remote(
    number: int, task: Task, repo: Path, source_url: str, source_commit: str,
    author: str,
) -> QueueTask:
    payload = "\n".join(task.body.splitlines()[1:-1]).strip()
    branch = task_branch_name(task)
    metadata_body = "\n".join([
        f"### <task> [Pending] [Unapproved] {task.title}",
        f"- Task-Number: {number}", f"- Task-ID: {task_fingerprint(task)}",
        f"- Source-Repo: {repo.name}", f"- Source-URL: {source_url}",
        f"- Author: {author}", f"- Source-Commit: {source_commit}",
        f"- Imported-At: {utc_now()}", f"- Branch: {branch}",
        payload, "### </task>", "",
    ])
    return QueueTask(number, task_fingerprint(task), "Pending", "Unapproved",
                     task.title, metadata_body, repo.name, source_url, author,
                     source_commit, utc_now(), branch)


def parse_approval_selector(expression: str, eligible: set[int]) -> set[int]:
    value = expression.strip().lower()
    if not value:
        raise TokenshareError("approve requires task numbers or 'all'")
    excluded = False
    if value == "all":
        return set(eligible)
    if value.startswith("all not "):
        excluded = True
        value = value[8:].strip()
    elif value.startswith("all"):
        raise TokenshareError("expected 'approve all' or 'approve all not <numbers>'")
    selected: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            raise TokenshareError("empty task selector")
        if ":" in token:
            parts = token.split(":")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise TokenshareError(f"invalid task range: {token}")
            start, end = map(int, parts)
            if start > end:
                raise TokenshareError(f"descending task range: {token}")
            selected.update(range(start, end + 1))
        elif token.isdigit():
            selected.add(int(token))
        else:
            raise TokenshareError(f"invalid task number: {token}")
    return set(eligible) - selected if excluded else selected


def approve_tasks(
    path: Path,
    expression: str,
    logs_dir: Path,
    *,
    automatic: bool = False,
) -> list[int]:
    with QUEUE_LOCK:
        tasks = load_queue(path)
        normalize_queue_numbers(tasks)
        eligible = {task.number for task in tasks
                    if task.state == "Pending" and task.approval == "Unapproved"
                    and task.number is not None}
        selected = parse_approval_selector(expression, eligible)
        unknown = selected - eligible
        if unknown:
            raise TokenshareError(
                "not eligible for approval: " + ", ".join(map(str, sorted(unknown)))
            )
        for task in tasks:
            if task.number in selected:
                task.approval = "Approved"
                event = "auto-approved" if automatic else "approved"
                note = (
                    f"Task #{task.number} automatically approved by "
                    "--dangerously-skip-approvals"
                    if automatic
                    else f"Task #{task.number} approved by local operator"
                )
                append_task_log(task_log_path(logs_dir, task.branch), event, note)
                task.number = None
        normalize_queue_numbers(tasks)
        save_queue(path, tasks)
    return sorted(selected)


def clear_controller_history(workspace: Path, state_path: Path) -> list[Path]:
    """Remove local controller history while retaining controller audit logs."""
    removed: list[Path] = []
    protected = {
        controller_log_path(workspace).resolve(),
        (workspace / "logs" / "tokenshare-controller.log").resolve(),
    }
    for path in (
        state_path,
        queue_path(workspace),
        workspace / "logs" / "tokenshare_agent_tasklist.md",
    ):
        if path.resolve() in protected:
            continue
        if path.is_dir():
            raise TokenshareError(f"Refusing to clear history file because it is a directory: {path}")
        if path.exists() or path.is_symlink():
            path.unlink()
            removed.append(path)
    repository_logs = repository_logs_path(workspace)
    if repository_logs.is_dir():
        shutil.rmtree(repository_logs)
        removed.append(repository_logs)
    # Clear legacy per-task logs, but explicitly retain the legacy controller audit log.
    legacy_logs = workspace / "logs"
    if legacy_logs.is_dir():
        for path in legacy_logs.glob(f"{BRANCH_PREFIX}*_log.md"):
            path.unlink()
            removed.append(path)
    return removed


def update_queue_task(path: Path, task_id: str, *, state: str | None = None,
                      approval: str | None = None) -> None:
    with QUEUE_LOCK:
        tasks = load_queue(path)
        matching = next((task for task in tasks if task.task_id == task_id), None)
        if matching is None:
            return
        if state is not None:
            matching.state = state
        if approval is not None:
            matching.approval = approval
        save_queue(path, tasks)


def queued_task_author(path: Path | None, task_id: str) -> str:
    if path is None:
        return "Unknown"
    matching = next(
        (task for task in load_queue(path) if task.task_id == task_id),
        None,
    )
    return matching.author if matching is not None else "Unknown"


def format_queue_view(tasks: Sequence[QueueTask]) -> str:
    priority = {("Pending", "Unapproved"): 0, ("Pending", "Approved"): 1,
                ("WIP", "Approved"): 2, ("Done", "Approved"): 3}
    normalize_queue_numbers(tasks)
    ordered = sorted(tasks, key=lambda task: (
        priority.get((task.state, task.approval), 9),
        task.number if task.number is not None else 0,
        task.imported_at,
    ))
    header = "#  State    Approval    Repository          Author               Title"
    rows = [header, "-" * len(header)]
    for task in ordered:
        number = str(task.number) if task.number is not None else ""
        rows.append(
            f"{number:<3}{task.state:<9}{task.approval:<12}"
            f"{task.repo_name[:18]:<20}{task.author[:19]:<21}{task.title}"
        )
    return "\n".join(rows) if ordered else "No tasks have been imported."


def default_branch(repo: Path) -> str:
    result = run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo,
        check=False,
    )
    if result.returncode or not result.stdout.strip().startswith("origin/"):
        raise TokenshareError(f"Cannot determine the remote default branch for {repo}")
    return result.stdout.strip().removeprefix("origin/")


def switch_to_default(repo: Path) -> str:
    ensure_clean(repo)
    branch = default_branch(repo)
    local = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo, check=False)
    if local.returncode == 0:
        run(["git", "switch", branch], cwd=repo)
        run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=repo)
    else:
        run(["git", "switch", "-c", branch, "--track", f"origin/{branch}"], cwd=repo)
    return branch


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "repositories": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenshareError(f"Cannot read Tokenshare state {path}: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != 1:
        raise TokenshareError(f"Unsupported Tokenshare state format in {path}")
    state.setdefault("repositories", {})
    return state


def save_state(path: Path, state: dict) -> None:
    with QUEUE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)


def repository_state(state: dict, repo: Path) -> dict:
    origin = run(["git", "remote", "get-url", "origin"], cwd=repo).stdout.strip()
    return state.setdefault("repositories", {}).setdefault(origin, {})


def remember_branch(
    state: dict, state_path: Path, repo: Path, task: Task, branch: str, head: str
) -> None:
    repository_state(state, repo)[task_fingerprint(task)] = {
        "branch": branch,
        "head": head,
        "title": task.title,
        "status": "published",
    }
    save_state(state_path, state)


def remote_managed_branches(repo: Path) -> list[ManagedBranch]:
    refs = run(
        ["git", "for-each-ref", "--format=%(refname:short) %(objectname)",
         "refs/remotes/origin"],
        cwd=repo,
    ).stdout.splitlines()
    records: list[ManagedBranch] = []
    for line in refs:
        if not line.strip():
            continue
        ref, head = line.split(maxsplit=1)
        branch = ref.removeprefix("origin/")
        if not branch.startswith(BRANCH_PREFIX):
            continue
        paths = run(["git", "ls-tree", "-r", "--name-only", ref], cwd=repo).stdout.splitlines()
        raw_path = next((path for path in ("tokenshare_tasklist.md",
                                            "docs/tokenshare_tasklist.md")
                         if path in paths), None)
        if raw_path is None:
            continue
        content = run(["git", "show", f"{ref}:{raw_path}"], cwd=repo).stdout
        candidates = [task for task in parse_tasks(content) if task.state in {"WIP", "Done"}]
        matching = next((task for task in candidates if task_branch_name(task) == branch), None)
        if matching is not None:
            records.append(ManagedBranch(
                branch, task_fingerprint(matching), matching.title,
                "complete" if matching.state == "Done" else "implementing", None, head,
            ))
    return records


def local_managed_branch(
    repo: Path, branch: str, expected_task_id: str
) -> ManagedBranch | None:
    """Recover a controller-owned local branch that has not been found remotely."""
    exists = run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo, check=False,
    )
    if exists.returncode:
        return None
    relative = str(find_tasklist(repo).relative_to(repo))
    snapshot = run(["git", "show", f"{branch}:{relative}"], cwd=repo)
    matching = next(
        (task for task in parse_tasks(snapshot.stdout)
         if task_fingerprint(task) == expected_task_id),
        None,
    )
    if matching is None:
        raise TokenshareError(
            f"Local managed branch {branch} does not contain its expected task"
        )
    if matching.state == "Pending":
        raise TokenshareError(
            f"Local managed branch {branch} still contains a Pending task; "
            "rename or delete that stale branch before retrying"
        )
    head = run(["git", "rev-parse", branch], cwd=repo).stdout.strip()
    state = "complete" if matching.state == "Done" else "implementing"
    return ManagedBranch(branch, expected_task_id, matching.title, state, None, head)


def branch_is_merged(repo: Path, record: ManagedBranch, tasks: Sequence[Task]) -> bool:
    matching = next((task for task in tasks if task.title == record.title), None)
    if matching is not None and matching.state == "Done":
        return True
    base = default_branch(repo)
    result = run(
        ["git", "merge-base", "--is-ancestor", record.head, f"origin/{base}"],
        cwd=repo,
        check=False,
    )
    return result.returncode == 0


def reconcile_deleted_branches(
    state: dict,
    state_path: Path,
    repo: Path,
    remote: Sequence[ManagedBranch],
    tasks: Sequence[Task],
) -> None:
    current = {record.name for record in remote}
    changed = False
    for task_id, entry in repository_state(state, repo).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "published" or entry.get("branch") in current:
            continue
        matching = next((task for task in tasks if task.title == entry.get("title")), None)
        head = entry.get("head", "")
        merged = matching is not None and matching.state == "Done"
        if not merged and head:
            merged = run(
                ["git", "merge-base", "--is-ancestor", head,
                 f"origin/{default_branch(repo)}"], cwd=repo, check=False
            ).returncode == 0
        entry["status"] = "merged" if merged else "declined"
        changed = True
        log(f"Task branch {entry['branch']} was {'merged' if merged else 'deleted without merge'}")
    if changed:
        save_state(state_path, state)


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


def write_task_log(
    path: Path,
    task: Task,
    state: str,
    note: str = "",
    *,
    task_id: str | None = None,
    branch: str | None = None,
) -> None:
    if state not in {"implementing", "testing", "complete", "failed"}:
        raise ValueError(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = path.read_text(encoding="utf-8") if path.exists() else (
        f"# Tokenshare Task Log\n\n- Task: {task.title}\n"
        f"- Task-ID: {task_id or task_fingerprint(task)}\n"
        f"- Branch: {branch or task_branch_name(task)}\n"
    )
    entry = f"\n## {now}\n\n- State: {state}\n"
    if note:
        entry += f"- Note: {note.replace(chr(10), ' ')}\n"
    path.write_text(existing + entry, encoding="utf-8")
    detail = f"task log: {state}"
    if note:
        detail += f" — {note.replace(chr(10), ' ')}"
    log(detail)


def git_commit_push(repo: Path, paths: Iterable[Path], message: str) -> None:
    relative = [str(path.relative_to(repo)) for path in paths]
    run(["git", "add", "--", *relative], cwd=repo)
    staged = run(
        ["git", "diff", "--cached", "--quiet", "--", *relative],
        cwd=repo,
        check=False,
    )
    if staged.returncode == 0:
        return
    if staged.returncode != 1:
        raise TokenshareError(f"Unable to inspect staged changes in {repo}")
    run(["git", "commit", "--only", "-m", message, "--", *relative], cwd=repo)
    run(["git", "push", "-u", "origin", "HEAD"], cwd=repo)


def git_commit_all(repo: Path, message: str) -> bool:
    """Commit the complete successful task result."""
    run(["git", "add", "--all"], cwd=repo)
    staged = run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False)
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise TokenshareError(f"Unable to inspect staged changes in {repo}")
    run(["git", "commit", "-m", message], cwd=repo)
    return True


def agent_prompt(task: Task, status_path: Path, phase: str) -> str:
    if phase == "implementing":
        action = "Implement every requirement in the task. Add or update tests as appropriate."
    else:
        action = (
            "Review the implementation for this task, run the appropriate test suite, fix all "
            "failures or omissions, and verify the result."
        )
    completion_marker = phase_completion_marker(phase)
    return f"""You are the unattended Tokenshare coding agent in the target repository.
There is no human interface. Do not ask questions. Use the task alone, inspect repository
instructions, and make the smallest reasonable assumptions needed to finish.

Phase: {phase}
Task log: {status_path}

{task.body.rstrip()}

{action}
Leave all code and test changes uncommitted in the working tree. Do not commit or push; the
controller owns commits and publication after successful verification.
Do not edit tokenshare_tasklist.md; the controller owns task transitions.
Add concise timestamped progress notes to the supplied status file when meaningful milestones
are reached. Do not write progress updates to the controller terminal.
After every requirement for this phase is finished, append a final timestamped status entry and
then append this exact marker on its own line:
{completion_marker}
This marker is the controller handshake. Never write it until the phase is fully complete. The
TUI may remain open afterward; the controller will detect the marker and close the session.
"""


def phase_completion_marker(phase: str) -> str:
    return f"<!-- tokenshare-agent-phase:{phase}:complete -->"


def _agent_kind(executable: str) -> str | None:
    name = Path(executable).name.removesuffix(".sh")
    for kind in ("codex", "claude", "opencode"):
        if name == kind or name.startswith(f"{kind}-"):
            return kind
    return None


def _agent_args(
    command: str, prompt: str, repo: Path, *, resume: bool = False
) -> list[str]:
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
    kind = _agent_kind(args[0])
    is_native = Path(args[0]).name in {"codex", "claude", "opencode"}
    if kind == "codex":
        escaped_repo = str(repo.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        args.extend(["--config", f'projects."{escaped_repo}".trust_level="trusted"'])
        if is_native and "--dangerously-bypass-approvals-and-sandbox" not in args:
            args.append("--dangerously-bypass-approvals-and-sandbox")
    elif kind == "claude" and is_native and "--dangerously-skip-permissions" not in args:
        args.append("--dangerously-skip-permissions")
    elif kind == "opencode" and is_native and "--auto" not in args:
        args.append("--auto")
    if resume and kind == "codex":
        return [*args, "resume", "--last", prompt]
    if resume and kind == "claude":
        return [*args, "--continue", prompt]
    if kind == "opencode":
        continuation = ["--continue"] if resume else []
        return [*args, *continuation, "--prompt", prompt]
    return [*args, prompt]


def resolve_agent_command(agent: str | None, agent_command: str) -> str:
    """Resolve an agent stub name/path, falling back to the raw command."""
    if not agent:
        return agent_command
    requested = Path(agent).expanduser()
    candidates: list[Path]
    if requested.is_absolute() or requested.parent != Path("."):
        candidates = [requested]
    else:
        project_root = Path(__file__).resolve().parents[1]
        stub_dirs = [
            project_root / "skills" / "tokenshare" / "scripts" / "agent-stubs",
            Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            / "skills" / "tokenshare" / "scripts" / "agent-stubs",
        ]
        names = [requested] if requested.suffix else [requested, requested.with_suffix(".sh")]
        candidates = [directory / name for directory in stub_dirs for name in names]
    stub = next((path.resolve() for path in candidates if path.is_file()), None)
    if stub is None:
        searched = ", ".join(str(path) for path in candidates)
        raise TokenshareError(f"Agent stub {agent!r} not found (searched: {searched})")
    if not os.access(stub, os.X_OK):
        raise TokenshareError(f"Agent stub is not executable: {stub}")
    return shlex.quote(str(stub))


def _log_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for block in re.split(r"(?=^##\s+)", text, flags=re.MULTILINE):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if lines and lines[0].startswith("## "):
            normalized = "\n".join(lines)
            fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            rendered = " | ".join(line.removeprefix("- ") for line in lines)
            blocks.append((fingerprint, rendered))
    return blocks


def _new_log_entries(path: Path, seen: set[str]) -> tuple[str, set[str], list[str]]:
    """Return only unseen timestamp blocks, tolerating rewrites and reordering."""
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = set(seen)
    entries: list[str] = []
    for fingerprint, rendered in _log_blocks(current):
        if fingerprint in updated:
            continue
        updated.add(fingerprint)
        entries.append(rendered)
    return current, updated, entries


def _current_tty() -> Path | None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                return Path(os.ttyname(stream.fileno())).resolve()
        except (AttributeError, OSError, ValueError):
            continue
    return None


def resolve_attach_target(value: str | None) -> AttachTarget | None:
    if value is None or value.strip().lower() in {"", "0", "false", "no", "off"}:
        return None
    controller_tty = _current_tty()
    if value.strip().lower() in {"1", "true", "yes", "current"}:
        if controller_tty is None:
            raise AttachmentError("--auto-attach requires an interactive controller TTY")
        requested = controller_tty
    else:
        requested = Path(value).expanduser().resolve()
    try:
        descriptor = os.open(requested, os.O_RDWR | os.O_NOCTTY)
    except OSError as exc:
        raise AttachmentError(f"Cannot access auto-attach TTY {requested}: {exc}") from exc
    try:
        if not os.isatty(descriptor):
            raise AttachmentError(f"Auto-attach target is not a TTY: {requested}")
        resolved = Path(os.ttyname(descriptor)).resolve()
    finally:
        os.close(descriptor)
    return AttachTarget(
        path=resolved,
        is_controller_tty=controller_tty is not None and resolved == controller_tty,
    )


def validate_attach_target(target: AttachTarget | None) -> AttachTarget | None:
    if target is None:
        return None
    if target is not None and target.is_controller_tty:
        raise AttachmentError(
            "--auto-attach cannot target the controller terminal; omit it or provide "
            "a TTY from a separate terminal (for example /dev/pts/3)"
        )
    matched = _tmux_client_for_tty(target.path)
    if matched is None:
        raise AttachmentError(
            f"--auto-attach target {target.path} has no tmux client. In the separate "
            "terminal run 'tmux new-session -A -s tokenshare-viewer', then run 'tty' "
            "inside that session and pass the printed TTY path to the controller"
        )
    return AttachTarget(matched[2], False)


def _tmux_client_for_tty(tty: Path) -> tuple[str, str, Path] | None:
    clients = run(
        ["tmux", "list-clients", "-F",
         "#{client_name}\t#{client_tty}\t#{session_name}\t#{pane_tty}"],
        check=False,
    )
    if clients.returncode:
        return None
    for line in clients.stdout.splitlines():
        fields = line.split("\t", 3)
        if len(fields) != 4:
            continue
        name, client_tty, session, pane_tty = fields
        matches = False
        for candidate in (client_tty, pane_tty):
            try:
                matches = matches or Path(candidate).resolve() == tty
            except OSError:
                matches = matches or candidate == str(tty)
        if matches:
            return name, session, Path(client_tty).resolve()
    return None


class AgentAttachment:
    def __init__(self, session: str, target: AttachTarget) -> None:
        self.session = session
        self.target = target
        self.client_name: str | None = None
        self.original_session: str | None = None
        self.closed = False

    def start(self) -> None:
        matched = _tmux_client_for_tty(self.target.path)
        if matched:
            self.client_name, self.original_session = matched[:2]
            result = run(
                ["tmux", "switch-client", "-c", self.client_name, "-t", self.session],
                check=False,
            )
            if result.returncode:
                raise AttachmentError(
                    f"Could not switch tmux client on {self.target.path}: "
                    f"{(result.stderr or result.stdout).strip()}"
                )
        else:
            raise AttachmentError(
                f"tmux client on {self.target.path} disappeared before attachment; "
                "restart the viewer tmux session and retry"
            )
        if self.target.is_controller_tty:
            DASHBOARD.suspend()

    def check_target(self) -> None:
        if self.closed:
            return
        try:
            descriptor = os.open(self.target.path, os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            raise AttachmentError(f"Auto-attach TTY disappeared: {self.target.path}") from exc
        try:
            if not os.isatty(descriptor):
                raise AttachmentError(f"Auto-attach target is no longer a TTY: {self.target.path}")
        finally:
            os.close(descriptor)
        if self.client_name:
            current = _tmux_client_for_tty(self.target.path)
            if (
                current is None
                or current[0] != self.client_name
                or current[1] != self.session
            ):
                # Detaching or manually switching sessions is allowed. Do not pull the
                # user back later; the next phase/retry will auto-attach again.
                self.client_name = None
                self.original_session = None
                if self.target.is_controller_tty:
                    DASHBOARD.resume()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.client_name and self.original_session:
            run(
                ["tmux", "switch-client", "-c", self.client_name, "-t", self.original_session],
                check=False,
            )
        if self.target.is_controller_tty:
            DASHBOARD.resume()


def _run_agent_once(
    repo: Path,
    command: str,
    task: Task,
    status_path: Path,
    phase: str,
    *,
    use_tmux: bool = True,
    resume: bool = False,
    auto_attach: AttachTarget | None = None,
) -> int:
    prompt = (
        "Continue the current Tokenshare task from where the session stopped. "
        f"The current phase is {phase}; keep updating {status_path} and finish all remaining work. "
        "Only after the phase is fully finished, append this exact controller handshake on its "
        f"own line: {phase_completion_marker(phase)}"
        if resume
        else agent_prompt(task, status_path, phase)
    )
    args = _agent_args(command, prompt, repo, resume=resume)
    env = os.environ.copy()
    env.update(
        {
            "TOKENSHARE_TASK_TITLE": task.title,
            "TOKENSHARE_TASK_STATE": phase,
            "TOKENSHARE_TASK_LOG": str(status_path),
        }
    )
    if _agent_kind(args[0]) == "opencode":
        env["OPENCODE_PERMISSION"] = json.dumps(
            {"*": "allow", "external_directory": "allow"}, separators=(",", ":")
        )
    if not use_tmux:
        result = subprocess.run(args, cwd=repo, text=True, env=env)
        returncode = result.returncode
    else:
        if not run(["tmux", "-V"], check=False).returncode == 0:
            raise TokenshareError("tmux is required by default; install it or pass --no-tmux")
        safe_title = re.sub(r"[^A-Za-z0-9_-]+", "-", task.title).strip("-")[:30]
        session = f"tokenshare-{os.getpid()}-{safe_title or 'agent'}-{phase[:4]}"
        run(["tmux", "new-session", "-d", "-s", session, "-c", str(repo)])
        run(["tmux", "set-option", "-t", session, "remain-on-exit", "on"])
        run(["tmux", "set-option", "-t", session, "mouse", "on"])
        run(["tmux", "set-option", "-t", session, "history-limit", "50000"])
        tmux_args = [
            "tmux", "respawn-pane", "-k", "-t", session, "-c", str(repo),
            "-e", f"TOKENSHARE_TASK_TITLE={task.title}",
            "-e", f"TOKENSHARE_TASK_STATE={phase}",
            "-e", f"TOKENSHARE_TASK_LOG={status_path}",
        ]
        if "OPENCODE_PERMISSION" in env:
            tmux_args.extend(["-e", f"OPENCODE_PERMISSION={env['OPENCODE_PERMISSION']}"])
        tmux_args.extend(args)
        run(tmux_args)
        log(f"Agent TUI is available in tmux session {session!r} (attach: tmux attach -t {session})")
        attachment = AgentAttachment(session, auto_attach) if auto_attach else None
        if attachment:
            try:
                attachment.start()
            except Exception:
                run(["tmux", "kill-session", "-t", session], check=False)
                raise
        current_log = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
        seen_log_blocks = {fingerprint for fingerprint, _ in _log_blocks(current_log)}
        returncode = 1
        try:
            while True:
                if CONTROLLER_STOP_EVENT is not None and CONTROLLER_STOP_EVENT.is_set():
                    raise ControllerStopped("controller shutdown requested")
                if attachment:
                    attachment.check_target()
                state = run(
                    ["tmux", "display-message", "-p", "-t", session,
                     "#{pane_dead} #{pane_dead_status}"], check=False,
                )
                if state.returncode:
                    raise TokenshareError(f"tmux agent session {session!r} disappeared")
                values = state.stdout.strip().split()
                current_log, seen_log_blocks, entries = _new_log_entries(
                    status_path, seen_log_blocks
                )
                for entry in entries:
                    log(f"task log: {entry}")
                DASHBOARD.refresh()
                if phase_completion_marker(phase) in current_log:
                    log(f"Agent reported {phase} phase complete")
                    returncode = 0
                    break
                if values and values[0] == "1":
                    returncode = int(values[1]) if len(values) > 1 else 1
                    if returncode:
                        captured = run(
                            ["tmux", "capture-pane", "-p", "-S", "-120", "-t", session],
                            check=False,
                        ).stdout.strip()
                        diagnostic = captured[-4000:] if captured else "No pane output captured"
                        append_task_log(
                            status_path, "agent-exit",
                            f"Exit status {returncode}; pane output: {diagnostic}",
                        )
                        log(f"Agent pane exited with status {returncode}: {diagnostic}")
                    break
                time.sleep(1)
        finally:
            if attachment and attachment.client_name:
                attachment.close()
            run(["tmux", "kill-session", "-t", session], check=False)
            if attachment and not attachment.closed:
                attachment.close()
    return returncode


def _retry_wait(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if CONTROLLER_STOP_EVENT is not None and CONTROLLER_STOP_EVENT.is_set():
            raise ControllerStopped("controller shutdown requested")
        DASHBOARD.refresh()
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def run_agent(
    repo: Path,
    command: str,
    task: Task,
    status_path: Path,
    phase: str,
    *,
    use_tmux: bool = True,
    auto_attach: AttachTarget | None = None,
) -> None:
    attempt = 0
    while True:
        try:
            returncode = _run_agent_once(
                repo,
                command,
                task,
                status_path,
                phase,
                use_tmux=use_tmux,
                resume=attempt > 0,
                auto_attach=auto_attach,
            )
            failure = f"exited with status {returncode}"
        except (AttachmentError, ControllerStopped):
            raise
        except (TokenshareError, OSError, subprocess.SubprocessError) as exc:
            returncode = 1
            failure = str(exc)
        if returncode == 0:
            if attempt:
                log(f"Agent connection restored during {phase}")
            return
        attempt += 1
        delay = attempt * 5
        kind = _agent_kind(shlex.split(command, posix=os.name != "nt")[0])
        action = "continuing session" if kind in {"codex", "claude", "opencode"} else "restarting agent"
        log(
            f"Agent {failure} during {phase}; {action} in {delay} seconds "
            f"(retry {attempt})"
        )
        _retry_wait(delay)


def task_author(repo: Path, tasklist: Path, task: Task, ref: str) -> str:
    relative = str(tasklist.relative_to(repo))
    wanted = task_fingerprint(task)
    commits = run(["git", "log", "--reverse", "--format=%H", ref, "--", relative],
                  cwd=repo, check=False).stdout.splitlines()
    introducing = commits[-1] if commits else ref
    for commit in commits:
        snapshot = run(["git", "show", f"{commit}:{relative}"], cwd=repo, check=False)
        if snapshot.returncode:
            continue
        try:
            fingerprints = {task_fingerprint(item) for item in parse_tasks(snapshot.stdout)}
        except TokenshareError:
            continue
        if wanted in fingerprints:
            introducing = commit
            break
    result = run(["git", "show", "-s", "--format=%an <%ae>", introducing],
                 cwd=repo, check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "Unknown"


def intake_remote_tasks(
    repo: Path, path: Path, state: dict, state_path: Path, logs_dir: Path
) -> tuple[int, int]:
    """Import remote Pending tasks and revoke stale unstarted snapshots."""
    switch_to_default(repo)
    tasklist = find_tasklist(repo)
    tasks = parse_tasks(tasklist.read_text(encoding="utf-8"))
    pending = [task for task in tasks if task.state == "Pending"]
    current_ids = {task_fingerprint(task) for task in pending}
    source_url = run(["git", "remote", "get-url", "origin"], cwd=repo).stdout.strip()
    source_commit = run(["git", "rev-parse", f"origin/{default_branch(repo)}"],
                        cwd=repo).stdout.strip()
    with QUEUE_LOCK:
        local = load_queue(path)
        normalize_queue_numbers(local)
        source_state = state.setdefault("sources", {}).setdefault(source_url, {})
        previous_commit = source_state.get("last_inspected_commit")
        remote_changed = previous_commit != source_commit
        if previous_commit and remote_changed:
            relative = str(tasklist.relative_to(repo))
            diff = run(["git", "diff", "--quiet", previous_commit, source_commit,
                        "--", relative], cwd=repo, check=False)
            remote_changed = diff.returncode != 0
        retained: list[QueueTask] = []
        revoked = 0
        for item in local:
            if (item.repo_url == source_url and item.state == "Pending"
                    and item.task_id not in current_ids):
                revoked += 1
                append_task_log(task_log_path(logs_dir, item.branch), "approval-revoked",
                                "Remote Pending task was edited or removed")
                label = f"#{item.number}" if item.number is not None else item.task_id[:8]
                log(f"Revoked stale task {label}: {item.title}")
            else:
                retained.append(item)
        local = retained
        known = {item.task_id for item in local}
        repo_data = repository_state(state, repo)
        repo_data.pop("seen_task_ids", None)
        repo_data.pop("last_inspected_commit", None)
        seen = set(source_state.get("seen_task_ids", []))
        next_number = 1 + sum(
            item.state == "Pending" and item.approval == "Unapproved"
            for item in local
        )
        imported = 0
        for task in pending if remote_changed else []:
            task_id = task_fingerprint(task)
            if task_id in known or task_id in seen:
                continue
            item = queue_task_from_remote(
                next_number, task, repo, source_url, source_commit,
                task_author(repo, tasklist, task, f"origin/{default_branch(repo)}"),
            )
            local.append(item)
            next_number += 1
            imported += 1
            task_log = task_log_path(logs_dir, item.branch)
            append_task_log(task_log, "remote-push",
                            f"Commit {source_commit}; author {item.author}")
            append_task_log(task_log, "remote-diff-detected", source_url)
            append_task_log(task_log, "imported-unapproved",
                            f"Task #{item.number} copied to {path}")
            append_task_log(task_log, "review-notification", "Ready for code-editor review")
            log(f"New unapproved task #{item.number}: {repo.name} — {task.title}")
            seen.add(task_id)
        managed = {record.task_id: record for record in remote_managed_branches(repo)}
        for item in local:
            record = managed.get(item.task_id)
            if record is None:
                continue
            migrated_state = "Done" if record.state == "complete" else "WIP"
            if item.state != migrated_state or item.approval != "Approved":
                item.state = migrated_state
                item.approval = "Approved"
                append_task_log(task_log_path(logs_dir, item.branch), "managed-branch-recovered",
                                f"Recovered {migrated_state} from origin/{record.name}")
        state.pop("next_task_number", None)
        source_state["seen_task_ids"] = sorted(seen)
        source_state["last_inspected_commit"] = source_commit
        save_queue(path, local)
        save_state(state_path, state)
    return imported, revoked


def process_task(
    repo: Path,
    task: Task,
    agent_command: str,
    *,
    state: dict,
    state_path: Path,
    existing: ManagedBranch | None = None,
    use_tmux: bool = True,
    auto_attach: AttachTarget | None = None,
    logs_dir: Path | None = None,
    local_queue: Path | None = None,
) -> None:
    if existing is None:
        task_id = task_fingerprint(task)
        existing = local_managed_branch(repo, task_branch_name(task), task_id)
    branch = existing.name if existing else task_branch_name(task)
    task_id = existing.task_id if existing else task_fingerprint(task)
    author = queued_task_author(local_queue, task_id)
    mode = "resume" if existing else "claim"
    log(
        f"Starting task: repo={repo.name}; author={author}; title={task.title}; "
        f"branch={branch}; mode={mode}"
    )
    DASHBOARD.set_task(repo, task.title)
    if existing:
        status_path = task_log_path(logs_dir or repo.parent / "logs", branch)
        log(f"Resuming {repo.name}: {task.title} on {branch}")
        current = run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()
        dirty = run(["git", "status", "--porcelain"], cwd=repo).stdout.strip()
        if dirty and current != branch:
            raise TokenshareError(
                f"Refusing to switch away from dirty branch {current!r} in {repo}"
            )
        if current != branch:
            ensure_clean(repo)
            local = run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo,
                check=False,
            )
            if local.returncode == 0:
                run(["git", "switch", branch], cwd=repo)
            else:
                run(["git", "switch", "-c", branch, "--track", f"origin/{branch}"], cwd=repo)
        if dirty:
            remote_is_ancestor = run(
                ["git", "merge-base", "--is-ancestor", f"origin/{branch}", "HEAD"],
                cwd=repo,
                check=False,
            )
            if remote_is_ancestor.returncode != 0:
                raise TokenshareError(
                    f"Managed branch {branch} has remote changes and local uncommitted work"
                )
        else:
            remote = run(
                ["git", "show-ref", "--verify", "--quiet",
                 f"refs/remotes/origin/{branch}"],
                cwd=repo, check=False,
            )
            if remote.returncode == 0:
                run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=repo)
            else:
                log(f"Resuming unpublished local managed branch {branch}")
        tasklist = find_tasklist(repo)
        branch_tasks = parse_tasks(tasklist.read_text(encoding="utf-8"))
        wip_task = next(
            (item for item in branch_tasks if item.title == task.title and item.state == "WIP"),
            None,
        )
        if wip_task is None:
            if any(item.title == task.title and item.state == "Done" for item in branch_tasks):
                completed_task = next(item for item in branch_tasks if item.title == task.title)
                if run(["git", "status", "--porcelain"], cwd=repo).stdout.strip():
                    write_task_log(
                        status_path, completed_task, "complete",
                        task_id=task_id, branch=branch,
                    )
                    git_commit_all(repo, f"tokenshare: complete {task.title}")
                run(["git", "push", "-u", "origin", "HEAD"], cwd=repo)
                head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
                remember_branch(
                    state, state_path, repo, completed_task, branch, head
                )
                if local_queue is not None:
                    update_queue_task(local_queue, task_id, state="Done", approval="Approved")
                log(f"Task branch already complete: {branch}")
                DASHBOARD.set_task(None)
                return
            raise TokenshareError(f"Managed branch {branch} has no WIP task {task.title!r}")
    else:
        log(f"Claiming {repo.name}: {task.title} on {branch}")
        ensure_clean(repo)
        base = default_branch(repo)
        run(["git", "switch", "-c", branch, f"origin/{base}"], cwd=repo)
        tasklist = find_tasklist(repo)
        task = next(
            item for item in parse_tasks(tasklist.read_text(encoding="utf-8"))
            if item.title == task.title and item.state == "Pending"
        )
        status_path = task_log_path(logs_dir or repo.parent / "logs", branch)
        write_task_log(
            status_path, task, "implementing", task_id=task_id, branch=branch
        )
        text = tasklist.read_text(encoding="utf-8")
        tasklist.write_text(transition_task(text, task, "WIP"), encoding="utf-8")
        git_commit_push(repo, [tasklist], f"tokenshare: claim {task.title}")
        head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        remember_branch(state, state_path, repo, task, branch, head)
        if local_queue is not None:
            update_queue_task(local_queue, task_id, state="WIP", approval="Approved")
        wip_task = next(
            item for item in parse_tasks(tasklist.read_text(encoding="utf-8"))
            if item.state == "WIP" and item.title == task.title
        )

    try:
        status_text = status_path.read_text(encoding="utf-8")
        if phase_completion_marker("implementing") not in status_text:
            run_agent(
                repo, agent_command, wip_task, status_path, "implementing",
                use_tmux=use_tmux, auto_attach=auto_attach,
            )
            status_text = status_path.read_text(encoding="utf-8")
        if "- State: testing" not in status_text:
            write_task_log(status_path, wip_task, "testing", task_id=task_id, branch=branch)
            status_text = status_path.read_text(encoding="utf-8")
        if phase_completion_marker("testing") not in status_text:
            run_agent(
                repo, agent_command, wip_task, status_path, "testing",
                use_tmux=use_tmux, auto_attach=auto_attach,
            )
        write_task_log(
            status_path, wip_task, "complete", task_id=task_id, branch=branch
        )
        current = tasklist.read_text(encoding="utf-8")
        refreshed = next(
            item
            for item in parse_tasks(current)
            if item.state == "WIP" and item.title == task.title
        )
        tasklist.write_text(transition_task(current, refreshed, "Done"), encoding="utf-8")
        git_commit_all(repo, f"tokenshare: complete {task.title}")
        run(["git", "push", "-u", "origin", "HEAD"], cwd=repo)
        head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        remember_branch(state, state_path, repo, task, branch, head)
        append_task_log(status_path, "branch-published", f"origin/{branch} at {head}")
        if local_queue is not None:
            update_queue_task(local_queue, task_id, state="Done", approval="Approved")
    except ControllerStopped:
        log(f"Paused {repo.name}: {task.title} during controller shutdown")
        raise
    except Exception as exc:
        write_task_log(
            status_path, wip_task, "failed", str(exc), task_id=task_id, branch=branch
        )
        raise
    log(f"Completed {repo.name}: {task.title}; review branch {branch}")
    DASHBOARD.set_task(None)


def scan_repositories(
    repos: Sequence[Path],
    agent_command: str,
    *,
    state: dict,
    state_path: Path,
    use_tmux: bool = True,
    auto_attach: AttachTarget | None = None,
    logs_dir: Path | None = None,
    local_queue: Path | None = None,
) -> int:
    completed = 0
    while True:
        progressed = False
        for repo in repos:
            current_branch = run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()
            dirty = run(["git", "status", "--porcelain"], cwd=repo).stdout.strip()
            remote = remote_managed_branches(repo)
            if dirty and current_branch.startswith(BRANCH_PREFIX):
                record = next((item for item in remote if item.name == current_branch), None)
                if record is None:
                    raise TokenshareError(
                        f"Dirty managed branch {current_branch} has not been published"
                    )
                branch_tasklist = find_tasklist(repo)
                branch_task = next(
                    item for item in parse_tasks(branch_tasklist.read_text(encoding="utf-8"))
                    if item.title == record.title
                )
                process_task(
                    repo, branch_task, agent_command, state=state, state_path=state_path,
                    existing=record, use_tmux=use_tmux, auto_attach=auto_attach,
                    logs_dir=logs_dir, local_queue=local_queue,
                )
                completed += 1
                progressed = True
                break

            switch_to_default(repo)
            tasklist = find_tasklist(repo)
            text = tasklist.read_text(encoding="utf-8")
            tasks = parse_tasks(text)
            config = parse_tasklist_config(text)
            wip = [task for task in tasks if task.state == "WIP"]
            if wip:
                names = ", ".join(task.title for task in wip)
                raise TokenshareError(f"Default branch contains WIP task(s): {names}")

            for record in remote:
                repository_state(state, repo)[record.task_id] = {
                    "branch": record.name,
                    "head": record.head,
                    "title": record.title,
                    "status": "published",
                }
            if remote:
                save_state(state_path, state)
            reconcile_deleted_branches(state, state_path, repo, remote, tasks)
            active = [record for record in remote if not branch_is_merged(repo, record, tasks)]

            revised_titles = {
                record.title for record in active
                if any(task.title == record.title and task_fingerprint(task) != record.task_id
                       for task in tasks)
            }
            if revised_titles:
                log(
                    f"Revised task blocked in {repo.name} until its earlier branch is resolved: "
                    + ", ".join(sorted(revised_titles))
                )
                continue

            incomplete = next((record for record in active if record.state != "complete"), None)
            if incomplete:
                branch_task = next(
                    task for task in tasks
                    if task.title == incomplete.title
                )
                process_task(
                    repo, branch_task, agent_command, state=state, state_path=state_path,
                    existing=incomplete, use_tmux=use_tmux, auto_attach=auto_attach,
                    logs_dir=logs_dir, local_queue=local_queue,
                )
                completed += 1
                progressed = True
                break
            if active and not config.allow_multiple_branches:
                continue

            active_ids = {record.task_id for record in active}
            declined = {
                task_id for task_id, entry in repository_state(state, repo).items()
                if isinstance(entry, dict) and entry.get("status") == "declined"
            }
            approved_ids = None
            if local_queue is not None:
                approved_ids = {
                    item.task_id for item in load_queue(local_queue)
                    if item.state == "Pending" and item.approval == "Approved"
                }
            pending = next((task for task in tasks if task.state == "Pending"
                            and task_fingerprint(task) not in active_ids | declined
                            and (approved_ids is None or task_fingerprint(task) in approved_ids)),
                           None)
            if pending is None:
                continue
            process_task(
                repo, pending, agent_command, state=state, state_path=state_path,
                use_tmux=use_tmux, auto_attach=auto_attach,
                logs_dir=logs_dir, local_queue=local_queue,
            )
            completed += 1
            progressed = True
            break
        if not progressed:
            return completed


def build_parser() -> argparse.ArgumentParser:
    script = Path(__file__).resolve()
    project_root = script.parents[1]
    install_metadata = Path(
        os.environ.get(
            "TOKENSHARE_INSTALL_METADATA",
            Path.home() / ".config" / "tokenshare" / "install.json",
        )
    ).expanduser()
    metadata: dict[str, str] = {}
    if install_metadata.is_file():
        try:
            loaded = json.loads(install_metadata.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metadata = {
                    key: value
                    for key, value in loaded.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
        except (OSError, json.JSONDecodeError):
            pass
    checkout_root = Path(
        os.environ.get(
            "TOKENSHARE_ROOT",
            metadata.get("install_directory", project_root),
        )
    ).expanduser()
    checkout_config = checkout_root / "config" / "task_repos.md"
    development_directory = Path(
        metadata.get("development_directory", Path.home() / "tokenshare_dev")
    ).expanduser()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("TOKENSHARE_CONFIG", checkout_config)),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("TOKENSHARE_WORKSPACE", development_directory)),
    )
    agent_group = parser.add_mutually_exclusive_group()
    agent_group.add_argument(
        "-a", "--agent",
        default=os.environ.get("TOKENSHARE_AGENT"),
        metavar="STUB",
        help="Agent stub name or executable path (for example: codex-gpt-56-sol)",
    )
    agent_group.add_argument(
        "--agent-command",
        default=os.environ.get(
            "TOKENSHARE_AGENT_COMMAND", "codex --dangerously-bypass-approvals-and-sandbox"
        ),
        help="Raw native agent command (advanced override)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("TOKENSHARE_POLL_SECONDS", "60")),
    )
    parser.add_argument(
        "-ni", "--non-interactive-mode", action="store_true",
        help="Synchronize, drain already-approved tasks, and exit",
    )
    parser.add_argument(
        "--dangerously-skip-approvals", action="store_true",
        help=("DANGER: immediately approve every imported task without human review; "
              "use only for tasks authored by the agent owner"),
    )
    parser.add_argument(
        "-ch", "--clear-history", action="store_true",
        help=("Delete local controller state and task history, preserve the controller "
              "audit log, and exit"),
    )
    parser.add_argument(
        "--workers", type=int,
        default=int(os.environ.get("TOKENSHARE_WORKERS", "1")),
        help="Maximum tasks running across different repositories (default: 1)",
    )
    parser.add_argument(
        "--no-tmux", action="store_true",
        help="Run the agent directly (mainly useful for tests and noninteractive automation)",
    )
    parser.add_argument(
        "--auto-attach",
        default=os.environ.get("TOKENSHARE_AUTO_ATTACH"),
        metavar="TTY",
        help="Attach agent TUIs to a separate terminal TTY path",
    )
    return parser


class ControllerRuntime:
    def __init__(self, args: argparse.Namespace, agent_command: str,
                 auto_attach: AttachTarget | None) -> None:
        global CONTROLLER_STOP_EVENT
        from concurrent.futures import ThreadPoolExecutor

        self.args = args
        self.agent_command = agent_command
        self.auto_attach = auto_attach
        self.workspace = args.workspace.expanduser().resolve()
        self.logs_dir = self.workspace / "logs"
        self.agent_logs_dir = self.logs_dir / "agent"
        self.repository_logs_dir = repository_logs_path(self.workspace)
        self.queue_file = queue_path(self.workspace)
        self.state_path = Path(os.environ.get(
            "TOKENSHARE_STATE", Path.home() / ".config" / "tokenshare" / "state.json"
        )).expanduser()
        self.state = load_state(self.state_path)
        if self.state.pop("next_task_number", None) is not None:
            save_state(self.state_path, self.state)
        self.urls = read_repo_urls(args.config.expanduser().resolve())
        names = [repo_name(url) for url in self.urls]
        if len(names) != len(set(names)):
            raise TokenshareError("Configured repositories must have unique repository names")
        self.executor = ThreadPoolExecutor(max_workers=args.workers,
                                           thread_name_prefix="tokenshare-worker")
        self.repos: dict[str, Path] = {}
        self.active: dict[str, object] = {}
        self.blocked_until: dict[str, float] = {}
        self.started = time.monotonic()
        self.idle_since = self.started
        self.stop_event = threading.Event()
        CONTROLLER_STOP_EVENT = self.stop_event
        self.wake_event = threading.Event()
        self.last_error: Exception | None = None
        self.thread: threading.Thread | None = None
        self.stopped = False

    def _worker(self, repo: Path) -> int:
        return scan_repositories(
            [repo], self.agent_command, state=self.state, state_path=self.state_path,
            use_tmux=not self.args.no_tmux, auto_attach=self.auto_attach,
            logs_dir=self.repository_logs_dir, local_queue=self.queue_file,
        )

    def cycle(self) -> bool:
        progressed = False
        for name, future in list(self.active.items()):
            if not future.done():
                continue
            del self.active[name]
            try:
                count = future.result()
                progressed = bool(count) or progressed
                if not count:
                    self.blocked_until[name] = time.monotonic() + self.args.poll_seconds
            except Exception as exc:
                self.last_error = exc
                log(f"ERROR processing {name}: {exc}")
            if not self.active:
                self.idle_since = time.monotonic()
        for url in self.urls:
            name = repo_name(url)
            if name in self.active:
                continue
            try:
                log(f"Checking repository {name}")
                repo = sync_repo(url, self.workspace)
                self.repos[name] = repo
                imported, revoked = intake_remote_tasks(
                    repo, self.queue_file, self.state, self.state_path,
                    self.repository_logs_dir,
                )
                if self.args.dangerously_skip_approvals:
                    approved = approve_tasks(
                        self.queue_file, "all", self.repository_logs_dir,
                        automatic=True,
                    )
                    if approved:
                        log("DANGER: automatically approved task(s): "
                            + ", ".join(map(str, approved)))
                progressed = bool(imported or revoked) or progressed
                if imported or revoked:
                    self.blocked_until.pop(name, None)
            except Exception as exc:
                self.last_error = exc
                log(f"ERROR checking {name}: {exc}")
        available = self.args.workers - len(self.active)
        if available > 0:
            approved_repos = {
                task.repo_name for task in load_queue(self.queue_file)
                if task.state == "Pending" and task.approval == "Approved"
            }
            for name in sorted(approved_repos):
                if (available <= 0 or name in self.active or name not in self.repos
                        or self.blocked_until.get(name, 0) > time.monotonic()):
                    continue
                future = self.executor.submit(self._worker, self.repos[name])
                future.add_done_callback(lambda _future: self.wake_event.set())
                self.active[name] = future
                available -= 1
                progressed = True
        return progressed

    def run_background(self) -> None:
        while not self.stop_event.is_set():
            self.cycle()
            self.wake_event.wait(self.args.poll_seconds)
            self.wake_event.clear()

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run_background,
                                       name="tokenshare-controller", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        self.stop_event.set()
        self.wake_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, self.args.poll_seconds + 1))
        self.executor.shutdown(wait=True)

    def toolbar(self) -> str:
        now = time.monotonic()
        uptime = Dashboard._duration(now - self.started)
        idle = Dashboard._duration(0 if self.active else now - self.idle_since)
        active = ", ".join(sorted(self.active)) or "none"
        return f" active: {active} | workers: {len(self.active)}/{self.args.workers} | uptime: {uptime} | idle: {idle} "


def handle_controller_command(command: str, runtime: ControllerRuntime,
                              output: Callable[[str], None] = print) -> bool:
    value = command.strip()
    if not value:
        return True
    if value == "view":
        tasks = load_queue(runtime.queue_file)
        output(format_queue_view(tasks))
        for task in tasks:
            if task.state == "Pending" and task.approval == "Unapproved":
                append_task_log(
                    task_log_path(runtime.repository_logs_dir, task.branch),
                    "viewed-in-tui",
                )
        return True
    if value.startswith("approve "):
        approved = approve_tasks(
            runtime.queue_file, value[8:], runtime.repository_logs_dir
        )
        output("Approved: " + (", ".join(map(str, approved)) if approved else "none"))
        runtime.wake_event.set()
        return True
    if value == "help":
        output(
            "usage: COMMAND [ARGS]\n\n"
            "Commands:\n"
            "  view                         Show the current task queue.\n"
            "  approve SELECTOR             Approve matching unapproved tasks.\n"
            "  help                         Show this help message.\n"
            "  quit, exit                   Stop the controller.\n\n"
            "Approval selectors:\n"
            "  N[,N...]                     Task numbers, e.g. 1,3,7.\n"
            "  START:END[,START:END...]     Inclusive ranges, e.g. 1:9,11:15.\n"
            "  all                          Every eligible task.\n"
            "  all not SELECTOR             Every eligible task except SELECTOR."
        )
        return True
    if value in {"quit", "exit"}:
        return False
    raise TokenshareError(f"Unknown command: {value}")


class ControllerTUI:
    """Full-screen renderer that exclusively owns interactive terminal output."""

    MAX_LOG_EVENTS = 2000

    def __init__(self, runtime: ControllerRuntime, *, input=None, output=None) -> None:
        try:
            from prompt_toolkit import Application
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.formatted_text import FormattedText
            from prompt_toolkit.layout import HSplit, Layout, Window
            from prompt_toolkit.layout.controls import FormattedTextControl
            from prompt_toolkit.styles import Style
            from prompt_toolkit.widgets import Frame, Label, TextArea
        except ImportError as exc:
            raise TokenshareError(
                "prompt_toolkit is required; rerun install.py or install prompt_toolkit"
            ) from exc

        self.runtime = runtime
        self._events: deque[str] = deque(maxlen=self.MAX_LOG_EVENTS)
        self._pending_events: deque[str] = deque()
        self._events_lock = threading.Lock()
        self._queue_mtime_ns: int | None = None
        self.closed = False
        self.task_height = 10
        self._resize_dragging = False
        self._resize_start_y = 0
        self._resize_start_height = self.task_height

        self.task_area = TextArea(
            text="Loading tasks…", read_only=True, focusable=False,
            scrollbar=True, wrap_lines=False,
        )
        self.log_area = TextArea(
            text="", read_only=True, focusable=False,
            scrollbar=True, wrap_lines=True,
        )
        self.command_area = TextArea(
            height=1, multiline=False, prompt="tokenshare> ",
            accept_handler=self._accept_command,
        )
        self.status = Label(text=self.runtime.toolbar, style="class:status")
        self.task_frame = Frame(
            self.task_area,
            title="Tasks",
            height=lambda: self.task_height,
        )
        divider_control = FormattedTextControl(
            FormattedText([("class:divider", " drag ↕ Tasks / Activity ")]),
            focusable=False,
        )
        self.divider = Window(
            content=divider_control, height=1, char="─", style="class:divider"
        )

        original_divider_mouse = self.divider._mouse_handler

        def divider_mouse(mouse_event):
            if self._handle_resize_mouse(mouse_event, self.divider):
                return None
            return original_divider_mouse(mouse_event)

        self.divider._mouse_handler = divider_mouse

        for window in (self.task_area.window, self.log_area.window):
            original_control_mouse = window.content.mouse_handler

            def pane_mouse(mouse_event, *, target=window, original=original_control_mouse):
                if self._resize_dragging and self._handle_resize_mouse(mouse_event, target):
                    return None
                return original(mouse_event)

            window.content.mouse_handler = pane_mouse

        root = HSplit([
            Label(" Tokenshare Controller  |  help: commands  |  Ctrl-C: quit ",
                  style="class:header"),
            self.task_frame,
            self.divider,
            Frame(self.log_area, title="Activity"),
            self.status,
            self.command_area,
        ])
        bindings = KeyBindings()

        @bindings.add("c-c")
        @bindings.add("c-d")
        def _exit(event) -> None:
            event.app.exit()

        @bindings.add("pageup")
        def _page_up(event) -> None:
            self.log_area.window.vertical_scroll = max(
                0, self.log_area.window.vertical_scroll - 10
            )

        @bindings.add("pagedown")
        def _page_down(event) -> None:
            self.log_area.window.vertical_scroll += 10

        @bindings.add("c-up")
        def _grow_tasks(event) -> None:
            self._set_task_height(self.task_height + 1)
            event.app.invalidate()

        @bindings.add("c-down")
        def _shrink_tasks(event) -> None:
            self._set_task_height(self.task_height - 1)
            event.app.invalidate()

        style = Style.from_dict({
            "header": "bold bg:#005f87 #ffffff",
            "status": "bg:#303030 #ffffff",
            "frame.label": "bold #00afff",
            "divider": "bg:#444444 #ffffff",
        })
        self.application = Application(
            layout=Layout(root, focused_element=self.command_area),
            key_bindings=bindings,
            style=style,
            full_screen=True,
            refresh_interval=1.0,
            before_render=self._before_render,
            mouse_support=True,
            input=input,
            output=output,
        )
        LOG_LISTENERS.append(self.enqueue_log)
        self.refresh_tasks(force=True)

    def _set_task_height(self, height: int) -> None:
        rows = self.application.output.get_size().rows if hasattr(self, "application") else 24
        self.task_height = max(4, min(height, max(4, rows - 10)))

    @staticmethod
    def _screen_y(window, mouse_event) -> int:
        info = window.render_info
        if info is None:
            return mouse_event.position.y
        positions = [
            y for (row, _column), (y, _x) in info._rowcol_to_yx.items()
            if row == mouse_event.position.y
        ]
        return min(positions) if positions else info._y_offset + mouse_event.position.y

    def _handle_resize_mouse(self, mouse_event, window) -> bool:
        from prompt_toolkit.mouse_events import MouseEventType

        event_type = mouse_event.event_type
        if event_type == MouseEventType.SCROLL_UP:
            self._set_task_height(self.task_height - 1)
            return True
        if event_type == MouseEventType.SCROLL_DOWN:
            self._set_task_height(self.task_height + 1)
            return True
        screen_y = self._screen_y(window, mouse_event)
        if event_type == MouseEventType.MOUSE_DOWN:
            self._resize_dragging = True
            self._resize_start_y = screen_y
            self._resize_start_height = self.task_height
            return True
        if event_type == MouseEventType.MOUSE_MOVE and self._resize_dragging:
            self._set_task_height(
                self._resize_start_height + screen_y - self._resize_start_y
            )
            return True
        if event_type == MouseEventType.MOUSE_UP and self._resize_dragging:
            self._set_task_height(
                self._resize_start_height + screen_y - self._resize_start_y
            )
            self._resize_dragging = False
            return True
        return False

    def enqueue_log(self, rendered: str) -> None:
        with self._events_lock:
            self._pending_events.append(rendered.replace("\t", "    "))
        self.application.invalidate()

    def _drain_events(self) -> None:
        with self._events_lock:
            pending = list(self._pending_events)
            self._pending_events.clear()
        if not pending:
            return
        self._events.extend(pending)
        self.log_area.text = "\n".join(self._events)
        self.log_area.buffer.cursor_position = len(self.log_area.buffer.text)

    def refresh_tasks(self, *, force: bool = False) -> None:
        try:
            mtime = self.runtime.queue_file.stat().st_mtime_ns
        except OSError:
            mtime = None
        if not force and mtime == self._queue_mtime_ns:
            return
        self._queue_mtime_ns = mtime
        self.task_area.text = format_queue_view(load_queue(self.runtime.queue_file))

    def _before_render(self, _app) -> None:
        self._drain_events()
        self.refresh_tasks()
        self.status.text = self.runtime.toolbar

    def _command_output(self, message: str) -> None:
        if message.startswith("#  State") or message == "No tasks have been imported.":
            self.task_area.text = message
            return
        self.enqueue_log(f"[command] {message}")

    def _accept_command(self, buffer) -> bool:
        command = buffer.text
        buffer.reset()
        try:
            keep_running = handle_controller_command(
                command, self.runtime, output=self._command_output
            )
            self.refresh_tasks(force=True)
            if not keep_running:
                self.application.exit()
        except TokenshareError as exc:
            self.enqueue_log(f"[error] {exc}")
        return True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            LOG_LISTENERS.remove(self.enqueue_log)
        except ValueError:
            pass

    def run(self) -> int:
        self.runtime.start()
        try:
            self.application.run()
        finally:
            self.close()
            self.runtime.stop()
        return 0


def run_tui(runtime: ControllerRuntime) -> int:
    return ControllerTUI(runtime).run()


def main(argv: Sequence[str] | None = None) -> int:
    global CONTROLLER_LOG
    args = build_parser().parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    CONTROLLER_LOG = controller_log_path(workspace)
    state_path = Path(os.environ.get(
        "TOKENSHARE_STATE", Path.home() / ".config" / "tokenshare" / "state.json"
    )).expanduser()
    if args.clear_history:
        DASHBOARD.enabled = False
        audit(f"Clear-history requested for state {state_path}")
        removed = clear_controller_history(workspace, state_path)
        audit("Clear-history completed; controller audit log preserved; removed: "
              + (", ".join(map(str, removed)) if removed else "nothing"))
        return 0
    if args.poll_seconds <= 0:
        raise TokenshareError("--poll-seconds must be greater than zero")
    if args.workers <= 0:
        raise TokenshareError("--workers must be greater than zero")
    if args.no_tmux and args.auto_attach:
        raise TokenshareError("--auto-attach cannot be used with --no-tmux")
    agent_command = resolve_agent_command(args.agent, args.agent_command)
    auto_attach = resolve_attach_target(args.auto_attach)
    auto_attach = validate_attach_target(auto_attach)
    runtime = ControllerRuntime(args, agent_command, auto_attach)
    try:
        DASHBOARD.enabled = False
        CONTROLLER_LOG = controller_log_path(runtime.workspace)
        migrate_queue(runtime.queue_file)
        if args.dangerously_skip_approvals:
            warning = (
                "DANGER: --dangerously-skip-approvals is enabled. Every imported task "
                "will run without human review. Use this only for tasks authored by the "
                "owner of this agent."
            )
            log(warning)
            print(warning, file=sys.stderr, flush=True)
        if args.non_interactive_mode:
            while True:
                progressed = runtime.cycle()
                if runtime.last_error is not None:
                    raise TokenshareError(str(runtime.last_error))
                if runtime.active:
                    time.sleep(0.1)
                    continue
                if not progressed:
                    break
            runtime.stop()
            return 0
        return run_tui(runtime)
    except BaseException:
        runtime.stop()
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TokenshareError, OSError, subprocess.SubprocessError) as exc:
        log(f"ERROR: {exc}")
        raise SystemExit(1)
