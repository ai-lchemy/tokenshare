#!/usr/bin/env python3
"""Persistent unattended controller for Tokenshare task repositories."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
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


class AttachmentError(TokenshareError):
    """A non-retryable failure involving the requested attachment terminal."""


@dataclasses.dataclass(frozen=True)
class Task:
    state: str
    title: str
    body: str
    start: int
    end: int


@dataclasses.dataclass(frozen=True)
class TasklistConfig:
    allow_multiple_branches: bool = False


@dataclasses.dataclass(frozen=True)
class ManagedBranch:
    name: str
    task_id: str
    title: str
    state: str
    status_path: Path
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
STATUS_TASK_ID = re.compile(r"^- Task-ID:\s*([0-9a-f]{64})\s*$", re.MULTILINE)
STATUS_TASK = re.compile(r"^- Task:\s*(.+?)\s*$", re.MULTILINE)
STATUS_BRANCH = re.compile(r"^- Branch:\s*(.+?)\s*$", re.MULTILINE)
STATUS_STATE = re.compile(r"^- State:\s*(implementing|testing|complete|failed)\s*$", re.MULTILINE)


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


def log(message: str) -> None:
    DASHBOARD.message(message)


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
        paths = run(
            ["git", "ls-tree", "-r", "--name-only", ref, "--", "docs"], cwd=repo
        ).stdout.splitlines()
        for raw_path in reversed(sorted(paths)):
            if not re.fullmatch(r"docs/status_.*\.md", raw_path):
                continue
            content = run(["git", "show", f"{ref}:{raw_path}"], cwd=repo).stdout
            id_match = STATUS_TASK_ID.search(content)
            title_match = STATUS_TASK.search(content)
            branch_match = STATUS_BRANCH.search(content)
            states = STATUS_STATE.findall(content)
            if not (id_match and title_match and branch_match and states):
                continue
            if branch_match.group(1) != branch:
                continue
            records.append(
                ManagedBranch(
                    branch, id_match.group(1), title_match.group(1), states[-1],
                    repo / raw_path, head,
                )
            )
            break
    return records


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


def write_status(
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
        f"# Tokenshare Task Status\n\n- Task: {task.title}\n"
        f"- Task-ID: {task_id or task_fingerprint(task)}\n"
        f"- Branch: {branch or task_branch_name(task)}\n"
    )
    entry = f"\n## {now}\n\n- State: {state}\n"
    if note:
        entry += f"- Note: {note.replace(chr(10), ' ')}\n"
    path.write_text(existing + entry, encoding="utf-8")
    detail = f"status.md: {state}"
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
Status file: {status_path}

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
    if kind == "codex":
        escaped_repo = str(repo.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        args.extend(["--config", f'projects."{escaped_repo}".trust_level="trusted"'])
    elif kind == "claude" and "--dangerously-skip-permissions" not in args:
        args.append("--dangerously-skip-permissions")
    elif kind == "opencode" and "--auto" not in args:
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


def _new_status_entries(path: Path, previous: str) -> tuple[str, list[str]]:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == previous:
        return current, []
    addition = current[len(previous):] if current.startswith(previous) else current
    entries: list[str] = []
    for block in re.split(r"(?=^##\s+)", addition, flags=re.MULTILINE):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if lines and lines[0].startswith("## "):
            entries.append(" | ".join(line.removeprefix("- ") for line in lines))
    return current, entries


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


def _tmux_client_for_tty(tty: Path) -> tuple[str, str] | None:
    clients = run(
        ["tmux", "list-clients", "-F", "#{client_name}\t#{client_tty}\t#{session_name}"],
        check=False,
    )
    if clients.returncode:
        return None
    for line in clients.stdout.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        name, client_tty, session = fields
        try:
            matches = Path(client_tty).resolve() == tty
        except OSError:
            matches = client_tty == str(tty)
        if matches:
            return name, session
    return None


class AgentAttachment:
    def __init__(self, session: str, target: AttachTarget) -> None:
        self.session = session
        self.target = target
        self.client_name: str | None = None
        self.original_session: str | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.terminal = None
        self.closed = False

    def start(self) -> None:
        matched = _tmux_client_for_tty(self.target.path)
        if matched:
            self.client_name, self.original_session = matched
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
            try:
                self.terminal = open(self.target.path, "r+b", buffering=0)
                self.process = subprocess.Popen(
                    ["tmux", "attach-session", "-t", self.session],
                    stdin=self.terminal,
                    stdout=self.terminal,
                    stderr=self.terminal,
                    close_fds=True,
                )
            except OSError as exc:
                self.close()
                raise AttachmentError(
                    f"Could not attach tmux session to {self.target.path}: {exc}"
                ) from exc
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
        elif self.process is not None and self.process.poll() is not None:
            returncode = self.process.returncode
            if returncode:
                raise AttachmentError(
                    f"tmux attachment on {self.target.path} exited with status {returncode}"
                )
            # A normal return here means the user manually detached. Monitoring continues.
            self._release_terminal()
            if self.target.is_controller_tty:
                DASHBOARD.resume()

    def _release_terminal(self) -> None:
        if self.terminal is not None:
            self.terminal.close()
            self.terminal = None
        self.process = None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.client_name and self.original_session:
            run(
                ["tmux", "switch-client", "-c", self.client_name, "-t", self.original_session],
                check=False,
            )
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
        self._release_terminal()
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
            "TOKENSHARE_STATUS_FILE": str(status_path),
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
        tmux_args = [
            "tmux", "respawn-pane", "-k", "-t", session, "-c", str(repo),
            "-e", f"TOKENSHARE_TASK_TITLE={task.title}",
            "-e", f"TOKENSHARE_TASK_STATE={phase}",
            "-e", f"TOKENSHARE_STATUS_FILE={status_path}",
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
        previous = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
        returncode = 1
        try:
            while True:
                if attachment:
                    attachment.check_target()
                state = run(
                    ["tmux", "display-message", "-p", "-t", session,
                     "#{pane_dead} #{pane_dead_status}"], check=False,
                )
                if state.returncode:
                    raise TokenshareError(f"tmux agent session {session!r} disappeared")
                values = state.stdout.strip().split()
                previous, entries = _new_status_entries(status_path, previous)
                for entry in entries:
                    log(f"status.md: {entry}")
                DASHBOARD.refresh()
                if phase_completion_marker(phase) in previous:
                    log(f"Agent reported {phase} phase complete")
                    returncode = 0
                    break
                if values and values[0] == "1":
                    returncode = int(values[1]) if len(values) > 1 else 1
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
        except AttachmentError:
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


def status_filename(repo: Path) -> Path:
    timestamp = dt.datetime.now()
    candidate = repo / "docs" / f"status_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    while candidate.exists():
        timestamp += dt.timedelta(seconds=1)
        candidate = repo / "docs" / f"status_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    return candidate


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
) -> None:
    DASHBOARD.set_task(repo, task.title)
    branch = existing.name if existing else task_branch_name(task)
    task_id = existing.task_id if existing else task_fingerprint(task)
    if existing:
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
            run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=repo)
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
                    write_status(
                        status_path, completed_task, "complete",
                        task_id=task_id, branch=branch,
                    )
                    git_commit_all(repo, f"tokenshare: complete {task.title}")
                run(["git", "push", "-u", "origin", "HEAD"], cwd=repo)
                head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
                remember_branch(
                    state, state_path, repo, completed_task, branch, head
                )
                log(f"Task branch already complete: {branch}")
                DASHBOARD.set_task(None)
                return
            raise TokenshareError(f"Managed branch {branch} has no WIP task {task.title!r}")
        status_path = existing.status_path
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
        status_path = status_filename(repo)
        write_status(
            status_path, task, "implementing", task_id=task_id, branch=branch
        )
        text = tasklist.read_text(encoding="utf-8")
        tasklist.write_text(transition_task(text, task, "WIP"), encoding="utf-8")
        git_commit_push(repo, [tasklist, status_path], f"tokenshare: claim {task.title}")
        head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        remember_branch(state, state_path, repo, task, branch, head)
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
            write_status(status_path, wip_task, "testing", task_id=task_id, branch=branch)
            git_commit_push(repo, [status_path], f"tokenshare: test {task.title}")
            status_text = status_path.read_text(encoding="utf-8")
        if phase_completion_marker("testing") not in status_text:
            run_agent(
                repo, agent_command, wip_task, status_path, "testing",
                use_tmux=use_tmux, auto_attach=auto_attach,
            )
        write_status(
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
    except Exception as exc:
        write_status(
            status_path, wip_task, "failed", str(exc), task_id=task_id, branch=branch
        )
        try:
            git_commit_push(repo, [status_path], f"tokenshare: record failure for {task.title}")
        except Exception as record_error:
            log(f"Could not push failure status: {record_error}")
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
                )
                completed += 1
                progressed = True
                break
            if active and not config.allow_multiple_branches:
                continue

            active_ids = {record.task_id for record in active}
            declined = {
                task_id for task_id, entry in repository_state(state, repo).items()
                if entry.get("status") == "declined"
            }
            pending = next(
                (task for task in tasks if task.state == "Pending"
                 and task_fingerprint(task) not in active_ids | declined),
                None,
            )
            if pending is None:
                continue
            process_task(
                repo, pending, agent_command, state=state, state_path=state_path,
                use_tmux=use_tmux, auto_attach=auto_attach,
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
        default=os.environ.get("TOKENSHARE_AGENT_COMMAND", "codex --full-auto"),
        help="Raw native agent command (advanced override)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("TOKENSHARE_POLL_SECONDS", "60")),
    )
    parser.add_argument("--once", action="store_true", help="Drain current Pending tasks and exit")
    parser.add_argument(
        "--no-tmux", action="store_true",
        help="Run the agent directly (mainly useful for tests and noninteractive automation)",
    )
    parser.add_argument(
        "--auto-attach",
        nargs="?",
        const="current",
        default=os.environ.get("TOKENSHARE_AUTO_ATTACH"),
        metavar="TTY",
        help="Attach agent TUIs to the current terminal or an optional TTY path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_seconds <= 0:
        raise TokenshareError("--poll-seconds must be greater than zero")
    if args.no_tmux and args.auto_attach:
        raise TokenshareError("--auto-attach cannot be used with --no-tmux")
    auto_attach = resolve_attach_target(args.auto_attach)
    agent_command = resolve_agent_command(args.agent, args.agent_command)
    state_path = Path(
        os.environ.get(
            "TOKENSHARE_STATE",
            Path.home() / ".config" / "tokenshare" / "state.json",
        )
    ).expanduser()
    state = load_state(state_path)
    urls = read_repo_urls(args.config.expanduser().resolve())
    names = [repo_name(url) for url in urls]
    if len(names) != len(set(names)):
        raise TokenshareError("Configured repositories must have unique repository names")

    while True:
        repos = []
        for url in urls:
            log(f"Checking repository {repo_name(url)}")
            repos.append(sync_repo(url, args.workspace.expanduser().resolve()))
        completed = scan_repositories(
            repos,
            agent_command,
            state=state,
            state_path=state_path,
            use_tmux=not args.no_tmux,
            auto_attach=auto_attach,
        )
        if args.once:
            return 0
        if not completed:
            log(f"No Pending tasks; checking again in {args.poll_seconds:g} seconds")
        deadline = time.monotonic() + args.poll_seconds
        while time.monotonic() < deadline:
            DASHBOARD.refresh()
            time.sleep(min(1, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TokenshareError, OSError, subprocess.SubprocessError) as exc:
        log(f"ERROR: {exc}")
        raise SystemExit(1)
