#!/usr/bin/env python3
"""Persistent unattended controller for Tokenshare task repositories."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
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


class PushNotApproved(TokenshareError):
    """A successful local result which was intentionally not published."""


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
    run(["git", "push"], cwd=repo)


def git_commit_all(repo: Path, message: str) -> bool:
    """Commit the complete successful task result without publishing it."""
    run(["git", "add", "--all"], cwd=repo)
    staged = run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False)
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise TokenshareError(f"Unable to inspect staged changes in {repo}")
    run(["git", "commit", "-m", message], cwd=repo)
    return True


def approve_push(repo: Path, task: Task, *, auto_push: bool) -> bool:
    if auto_push:
        log(f"Auto-push approved for {repo.name}: {task.title}")
        return True
    prompt = f"Push successful task changes for {repo.name} ({task.title})? [y/N] "
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as terminal:
            terminal.write(prompt)
            terminal.flush()
            answer = terminal.readline()
    except OSError:
        log("Push approval required, but no interactive terminal is available")
        return False
    return answer.strip().lower() in {"y", "yes"}


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
    tasklist: Path,
    task: Task,
    agent_command: str,
    *,
    use_tmux: bool = True,
    auto_push: bool = False,
    auto_attach: AttachTarget | None = None,
) -> None:
    DASHBOARD.set_task(repo, task.title)
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
        run_agent(
            repo, agent_command, wip_task, status_path, "implementing",
            use_tmux=use_tmux, auto_attach=auto_attach,
        )
        write_status(status_path, wip_task, "testing")
        git_commit_push(repo, [status_path], f"tokenshare: test {task.title}")
        run_agent(
            repo, agent_command, wip_task, status_path, "testing",
            use_tmux=use_tmux, auto_attach=auto_attach,
        )
        write_status(status_path, wip_task, "complete")
        current = tasklist.read_text(encoding="utf-8")
        refreshed = next(
            item
            for item in parse_tasks(current)
            if item.state == "WIP" and item.title == task.title
        )
        tasklist.write_text(transition_task(current, refreshed, "Done"), encoding="utf-8")
        git_commit_all(repo, f"tokenshare: complete {task.title}")
        if not approve_push(repo, task, auto_push=auto_push):
            raise PushNotApproved(
                f"Push not approved; successful changes remain committed locally in {repo}. "
                f"Publish them with: git -C {shlex.quote(str(repo))} push"
            )
        run(["git", "push"], cwd=repo)
    except PushNotApproved:
        raise
    except Exception as exc:
        write_status(status_path, wip_task, "failed", str(exc))
        try:
            git_commit_push(repo, [status_path], f"tokenshare: record failure for {task.title}")
        except Exception as record_error:
            log(f"Could not push failure status: {record_error}")
        raise
    log(f"Completed {repo.name}: {task.title}")
    DASHBOARD.set_task(None)


def scan_repositories(
    repos: Sequence[Path],
    agent_command: str,
    *,
    use_tmux: bool = True,
    auto_push: bool = False,
    auto_attach: AttachTarget | None = None,
) -> int:
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
        process_task(
            *pending, agent_command, use_tmux=use_tmux, auto_push=auto_push,
            auto_attach=auto_attach,
        )
        completed += 1


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
    parser.add_argument(
        "--auto-push",
        action="store_true",
        default=os.environ.get("TOKENSHARE_AUTO_PUSH", "").lower() in {"1", "true", "yes"},
        help="Pre-approve publishing coding changes after successful testing",
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
            use_tmux=not args.no_tmux,
            auto_push=args.auto_push,
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
