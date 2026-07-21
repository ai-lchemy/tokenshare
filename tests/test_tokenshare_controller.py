from __future__ import annotations

import importlib.util
import io
import dataclasses
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "tokenshare-controller.py"
SPEC = importlib.util.spec_from_file_location("tokenshare_controller", SCRIPT)
controller = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = controller
SPEC.loader.exec_module(controller)


TASKLIST = """# Tokenshare Tasklist
## Pending Tasks
### <task> [Pending] Implement Awesome Feature
- Do the work.
### </task>
## WIP Tasks
## Completed Tasks
### <task> [Done] Existing Feature
- Already complete.
### </task>
"""

TWO_TASKS = TASKLIST.replace(
    "## WIP Tasks",
    "### <task> [Pending] Implement Another Feature\n- Do more work.\n### </task>\n"
    "## WIP Tasks",
)


class TaskParsingTests(unittest.TestCase):
    def test_checkout_defaults_use_root_config_and_dev_workspace(self):
        with mock.patch.dict(os.environ, {"TOKENSHARE_INSTALL_METADATA": "/missing"}):
            args = controller.build_parser().parse_args([])
        root = SCRIPT.parents[1]
        self.assertEqual(args.config, root / "config" / "task_repos.md")
        self.assertEqual(args.workspace, Path.home() / "tokenshare_dev")
        self.assertEqual(
            args.agent_command, "codex --dangerously-bypass-approvals-and-sandbox"
        )
        self.assertIsNone(args.agent)
        self.assertFalse(args.no_tmux)
        self.assertIsNone(args.auto_attach)
        self.assertEqual(args.workers, 1)
        self.assertFalse(args.non_interactive_mode)
        self.assertFalse(args.dangerously_skip_approvals)
        self.assertFalse(args.clear_history)

    def test_dangerous_approval_and_clear_history_flags_parse(self):
        args = controller.build_parser().parse_args([
            "--dangerously-skip-approvals", "-ch",
        ])
        self.assertTrue(args.dangerously_skip_approvals)
        self.assertTrue(args.clear_history)

    def test_installed_defaults_come_from_install_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "install.json"
            metadata.write_text(
                json.dumps({
                    "install_directory": "/opt/tokenshare",
                    "development_directory": "/work/repos",
                }),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"TOKENSHARE_INSTALL_METADATA": str(metadata)}, clear=False
            ):
                args = controller.build_parser().parse_args([])
            self.assertEqual(args.config, Path("/opt/tokenshare/config/task_repos.md"))
            self.assertEqual(args.workspace, Path("/work/repos"))

    def test_auto_attach_requires_an_explicit_tty(self):
        parser = controller.build_parser()
        self.assertEqual(
            parser.parse_args(["--auto-attach", "/dev/pts/9"]).auto_attach,
            "/dev/pts/9",
        )

    def test_auto_attach_rejects_no_tmux(self):
        with self.assertRaisesRegex(controller.TokenshareError, "cannot be used"):
            controller.main(["--no-tmux", "--auto-attach", "/dev/pts/9"])

    def test_resolves_and_validates_explicit_tty(self):
        master, slave = pty.openpty()
        try:
            tty = Path(os.ttyname(slave)).resolve()
            target = controller.resolve_attach_target(str(tty))
            self.assertIsNotNone(target)
            self.assertEqual(target.path, tty)
        finally:
            os.close(master)
            os.close(slave)
        with tempfile.NamedTemporaryFile() as regular_file:
            with self.assertRaisesRegex(controller.AttachmentError, "not a TTY"):
                controller.resolve_attach_target(regular_file.name)

    def test_controller_tty_cannot_be_used_for_auto_attach(self):
        with self.assertRaisesRegex(controller.AttachmentError, "controller terminal"):
            controller.validate_attach_target(
                controller.AttachTarget(Path("/dev/pts/0"), True)
            )
        target = controller.AttachTarget(Path("/dev/pts/1"), False)
        with mock.patch.object(
            controller, "_tmux_client_for_tty",
            return_value=("client", "viewer", Path("/dev/pts/4"))
        ):
            validated = controller.validate_attach_target(target)
            self.assertEqual(validated.path, Path("/dev/pts/4"))
        with mock.patch.object(controller, "_tmux_client_for_tty", return_value=None):
            with self.assertRaisesRegex(
                controller.AttachmentError, "tmux new-session -A -s tokenshare-viewer"
            ):
                controller.validate_attach_target(target)

    def test_tmux_client_lookup_accepts_outer_or_inner_tty(self):
        completed = subprocess.CompletedProcess(
            [], 0, "client-1\t/dev/pts/4\tviewer\t/dev/pts/9\n", ""
        )
        with mock.patch.object(controller, "run", return_value=completed):
            self.assertEqual(
                controller._tmux_client_for_tty(Path("/dev/pts/4")),
                ("client-1", "viewer", Path("/dev/pts/4")),
            )
            self.assertEqual(
                controller._tmux_client_for_tty(Path("/dev/pts/9")),
                ("client-1", "viewer", Path("/dev/pts/4")),
            )

    def test_tmux_client_attachment_switches_back_to_original_session(self):
        target = controller.AttachTarget(Path("/dev/pts/9"), False)
        attachment = controller.AgentAttachment("agent-session", target)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            controller, "_tmux_client_for_tty",
            return_value=("client-1", "controller", Path("/dev/pts/9"))
        ), mock.patch.object(controller, "run", return_value=completed) as run_mock:
            attachment.start()
            attachment.close()
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertIn(
            ["tmux", "switch-client", "-c", "client-1", "-t", "agent-session"],
            commands,
        )
        self.assertIn(
            ["tmux", "switch-client", "-c", "client-1", "-t", "controller"],
            commands,
        )

    def test_manual_tmux_client_switch_does_not_force_restore(self):
        target = controller.AttachTarget(Path("/dev/pts/9"), False)
        attachment = controller.AgentAttachment("agent-session", target)
        attachment.client_name = "client-1"
        attachment.original_session = "controller"
        with mock.patch.object(
            controller, "_tmux_client_for_tty",
            return_value=("client-1", "other", Path("/dev/pts/9"))
        ), mock.patch.object(controller.os, "open", return_value=99), mock.patch.object(
            controller.os, "isatty", return_value=True
        ), mock.patch.object(controller.os, "close"), mock.patch.object(
            controller, "run"
        ) as run_mock:
            attachment.check_target()
            attachment.close()
        self.assertIsNone(attachment.client_name)
        run_mock.assert_not_called()

    def test_agent_flag_resolves_named_and_path_stubs(self):
        args = controller.build_parser().parse_args(["-a", "codex-gpt-56-sol"])
        command = controller.resolve_agent_command(args.agent, args.agent_command)
        self.assertEqual(
            Path(command),
            SCRIPT.parents[1]
            / "skills" / "tokenshare" / "scripts" / "agent-stubs"
            / "codex-gpt-56-sol.sh",
        )
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "custom-agent"
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)
            self.assertEqual(
                Path(controller.resolve_agent_command(str(stub), "unused")), stub.resolve()
            )

    def test_agent_flag_rejects_missing_stub(self):
        with self.assertRaisesRegex(controller.TokenshareError, "not found"):
            controller.resolve_agent_command("definitely-missing-agent", "unused")

    def test_codex_command_trusts_the_task_repo_for_the_invocation(self):
        repo = Path("/tmp/task repo")
        args = controller._agent_args("codex --full-auto", "do work", repo)
        self.assertEqual(args[-1], "do work")
        self.assertIn("--config", args)
        self.assertIn(
            f'projects."{repo.resolve()}".trust_level="trusted"', args
        )

    def test_non_codex_command_does_not_receive_codex_config(self):
        args = controller._agent_args("claude", "do work", Path("/tmp/repo"))
        self.assertEqual(
            args, ["claude", "--dangerously-skip-permissions", "do work"]
        )

    def test_opencode_auto_approves_access(self):
        args = controller._agent_args("opencode", "do work", Path("/tmp/repo"))
        self.assertEqual(args, ["opencode", "--auto", "--prompt", "do work"])

    def test_provider_stubs_own_their_permission_flags(self):
        claude = controller._agent_args(
            "/tmp/claude-sonnet.sh", "do work", Path("/tmp/repo")
        )
        opencode = controller._agent_args(
            "/tmp/opencode-gpt.sh", "do work", Path("/tmp/repo")
        )
        codex = controller._agent_args(
            "/tmp/codex-gpt.sh", "do work", Path("/tmp/repo")
        )
        self.assertNotIn("--dangerously-skip-permissions", claude)
        self.assertNotIn("--auto", opencode)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", codex)
        self.assertIn("--config", codex)

    def test_provider_resume_arguments(self):
        repo = Path("/tmp/repo")
        codex = controller._agent_args("codex", "continue", repo, resume=True)
        claude = controller._agent_args("claude", "continue", repo, resume=True)
        opencode = controller._agent_args("opencode", "continue", repo, resume=True)
        self.assertEqual(codex[-3:], ["resume", "--last", "continue"])
        self.assertEqual(claude[-2:], ["--continue", "continue"])
        self.assertEqual(opencode[-3:], ["--continue", "--prompt", "continue"])

    def test_failed_agent_retries_with_incremental_backoff_and_resume(self):
        task = controller.parse_tasks(TASKLIST)[0]
        with mock.patch.object(
            controller, "_run_agent_once", side_effect=[1, 1, 0]
        ) as run_mock, mock.patch.object(controller, "_retry_wait") as wait_mock:
            controller.run_agent(
                Path("/tmp/repo"), "codex", task, Path("/tmp/status.md"),
                "implementing",
            )
        self.assertEqual(wait_mock.call_args_list, [mock.call(5), mock.call(10)])
        self.assertFalse(run_mock.call_args_list[0].kwargs["resume"])
        self.assertTrue(run_mock.call_args_list[1].kwargs["resume"])
        self.assertTrue(run_mock.call_args_list[2].kwargs["resume"])

    def test_controller_shutdown_interrupts_agent_retry_wait(self):
        previous = controller.CONTROLLER_STOP_EVENT
        stop_event = controller.threading.Event()
        stop_event.set()
        controller.CONTROLLER_STOP_EVENT = stop_event
        try:
            with self.assertRaises(controller.ControllerStopped):
                controller._retry_wait(30)
        finally:
            controller.CONTROLLER_STOP_EVENT = previous

    def test_opencode_child_gets_allow_all_permission_environment(self):
        task = controller.parse_tasks(TASKLIST)[0]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            with mock.patch.object(
                controller.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run_mock:
                controller.run_agent(
                    repo, "opencode", task, repo / "status.md", "implementing",
                    use_tmux=False,
                )
            environment = run_mock.call_args.kwargs["env"]
            self.assertEqual(
                environment["OPENCODE_PERMISSION"],
                '{"*":"allow","external_directory":"allow"}',
            )

    def test_agent_prompt_contains_phase_completion_handshake(self):
        task = controller.parse_tasks(TASKLIST)[0]
        prompt = controller.agent_prompt(task, Path("status.md"), "implementing")
        self.assertIn(controller.phase_completion_marker("implementing"), prompt)
        self.assertNotIn(controller.phase_completion_marker("testing"), prompt)

    def test_task_branch_name_is_readable_stable_and_collision_resistant(self):
        task = controller.parse_tasks(TASKLIST)[0]
        branch = controller.task_branch_name(task)
        self.assertRegex(branch, r"^tokenshare-dev-implement-awesome-feature-[0-9a-f]{8}$")
        moved = controller.transition_task(TASKLIST, task, "WIP")
        self.assertEqual(
            controller.task_fingerprint(task),
            controller.task_fingerprint(controller.parse_tasks(moved)[0]),
        )

    def test_tasklist_configuration_is_strict_and_defaults_to_one_branch(self):
        self.assertFalse(controller.parse_tasklist_config(TASKLIST).allow_multiple_branches)
        configured = TASKLIST.replace(
            "## Pending Tasks",
            "## Configuration\nallow-multiple-branches: true\n## Pending Tasks",
        )
        self.assertTrue(
            controller.parse_tasklist_config(configured).allow_multiple_branches
        )
        with self.assertRaisesRegex(controller.TokenshareError, "Unknown"):
            controller.parse_tasklist_config(
                configured.replace("allow-multiple-branches", "unknown-setting")
            )

    def test_rejects_duplicate_task_titles(self):
        duplicate = TWO_TASKS.replace("Implement Another Feature", "Implement Awesome Feature")
        with self.assertRaisesRegex(controller.TokenshareError, "Duplicate task title"):
            controller.parse_tasks(duplicate)

    def test_parses_and_moves_task_between_sections(self):
        tasks = controller.parse_tasks(TASKLIST)
        self.assertEqual([(task.state, task.title) for task in tasks], [
            ("Pending", "Implement Awesome Feature"),
            ("Done", "Existing Feature"),
        ])

        moved = controller.transition_task(TASKLIST, tasks[0], "WIP")
        parsed = controller.parse_tasks(moved)
        self.assertEqual([task.state for task in parsed], ["WIP", "Done"])
        self.assertIn("## WIP Tasks\n### <task> [WIP] Implement Awesome Feature", moved)

    def test_rejects_task_in_wrong_section(self):
        invalid = TASKLIST.replace("[Pending] Implement", "[WIP] Implement")
        with self.assertRaisesRegex(controller.TokenshareError, "not under"):
            controller.parse_tasks(invalid)

    def test_rejects_misspelled_task_state_instead_of_treating_queue_as_empty(self):
        invalid = TASKLIST.replace("[Pending]", "[Pendig]", 1)
        with self.assertRaisesRegex(controller.TokenshareError, "Malformed task header"):
            controller.parse_tasks(invalid)

    def test_finds_exactly_one_tasklist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "tokenshare_tasklist.md").write_text(TASKLIST, encoding="utf-8")
            self.assertEqual(controller.find_tasklist(root), root / "tokenshare_tasklist.md")
            (root / "docs" / "tokenshare_tasklist.md").write_text(TASKLIST, encoding="utf-8")
            with self.assertRaisesRegex(controller.TokenshareError, "Both root and docs"):
                controller.find_tasklist(root)

    def test_reads_markdown_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "task_repos.md"
            config.write_text(
                "# Tokenshare Task Repos\n<!-- note -->\nhttps://github.com/a/one\n"
                "git@github.com:b/two.git\n",
                encoding="utf-8",
            )
            self.assertEqual(
                controller.read_repo_urls(config),
                ["https://github.com/a/one", "git@github.com:b/two.git"],
            )
            self.assertEqual(controller.repo_name("git@github.com:b/two.git"), "two")

    def test_approval_selectors_support_lists_ranges_all_and_exclusions(self):
        eligible = {1, 2, 3, 7, 8, 9}
        self.assertEqual(controller.parse_approval_selector("1,3,7", eligible), {1, 3, 7})
        self.assertEqual(controller.parse_approval_selector("1:3,7:9", eligible), eligible)
        self.assertEqual(controller.parse_approval_selector("all", eligible), eligible)
        self.assertEqual(
            controller.parse_approval_selector("all not 2,7:8", eligible), {1, 3, 9}
        )
        with self.assertRaises(controller.TokenshareError):
            controller.parse_approval_selector("3:1", eligible)

    def test_local_queue_round_trip_and_view_order(self):
        remote = controller.parse_tasks(TASKLIST)[0]
        first = controller.queue_task_from_remote(
            1, remote, Path("/tmp/repo"), "https://example.invalid/repo.git",
            "a" * 40, "Task Author <author@example.invalid>",
        )
        second = dataclasses.replace(first, number=2, task_id="b" * 64,
                                     state="Pending", approval="Approved", title="Second")
        third = dataclasses.replace(first, number=3, task_id="c" * 64,
                                    state="Done", approval="Approved", title="Third")
        rendered = controller.render_queue([third, second, first])
        self.assertEqual(rendered.count("### </task>"), 3)
        parsed = controller.parse_queue(rendered)
        self.assertEqual([task.number for task in parsed], [1, 2, 3])
        view = controller.format_queue_view(parsed)
        self.assertLess(view.index("Implement Awesome Feature"), view.index("Second"))
        self.assertLess(view.index("Second"), view.index("Third"))

    def test_approve_tasks_updates_only_eligible_tasks_and_logs(self):
        remote = controller.parse_tasks(TASKLIST)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "logs" / "tokenshare_agent_tasklist.md"
            item = controller.queue_task_from_remote(
                4, remote, Path("/tmp/repo"), "remote", "a" * 40, "Author",
            )
            controller.save_queue(path, [item])
            self.assertEqual(controller.approve_tasks(path, "4", root / "logs"), [4])
            self.assertEqual(controller.load_queue(path)[0].approval, "Approved")
            self.assertTrue(controller.task_log_path(root / "logs", item.branch).is_file())

    def test_automatic_approval_is_distinctly_audited(self):
        remote = controller.parse_tasks(TASKLIST)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = controller.queue_path(root)
            item = controller.queue_task_from_remote(
                1, remote, Path("/tmp/repo"), "remote", "a" * 40, "Owner",
            )
            controller.save_queue(queue, [item])
            logs = controller.repository_logs_path(root)
            self.assertEqual(
                controller.approve_tasks(queue, "all", logs, automatic=True), [1]
            )
            audit = controller.task_log_path(logs, item.branch).read_text(encoding="utf-8")
            self.assertIn("Event: auto-approved", audit)
            self.assertIn("--dangerously-skip-approvals", audit)

    def test_clear_history_preserves_controller_audit_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "config" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}", encoding="utf-8")
            queue = controller.queue_path(root)
            queue.parent.mkdir(parents=True)
            queue.write_text("queue", encoding="utf-8")
            repo_log = controller.repository_logs_path(root) / "task_log.md"
            repo_log.parent.mkdir(parents=True)
            repo_log.write_text("task", encoding="utf-8")
            audit = controller.controller_log_path(root)
            audit.write_text("keep", encoding="utf-8")
            legacy_audit = root / "logs" / "tokenshare-controller.log"
            legacy_audit.write_text("keep legacy", encoding="utf-8")
            legacy_task = root / "logs" / "tokenshare-dev-old_log.md"
            legacy_task.write_text("remove", encoding="utf-8")

            controller.clear_controller_history(root, state)

            self.assertFalse(state.exists())
            self.assertFalse(queue.exists())
            self.assertFalse(repo_log.exists())
            self.assertFalse(legacy_task.exists())
            self.assertEqual(audit.read_text(encoding="utf-8"), "keep")
            self.assertEqual(legacy_audit.read_text(encoding="utf-8"), "keep legacy")

            controller.clear_controller_history(root, audit)
            self.assertEqual(audit.read_text(encoding="utf-8"), "keep")

    def test_clear_history_main_is_silent_and_never_initializes_an_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            state.write_text("{}", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            old_dashboard_enabled = controller.DASHBOARD.enabled
            try:
                with mock.patch.dict(
                    os.environ, {"TOKENSHARE_STATE": str(state)}, clear=False
                ), mock.patch.object(
                    controller, "resolve_agent_command"
                ) as resolve_agent, mock.patch.object(
                    controller, "ControllerRuntime"
                ) as runtime, mock.patch("sys.stdout", stdout), mock.patch(
                    "sys.stderr", stderr
                ):
                    self.assertEqual(
                        controller.main(["--workspace", str(root), "-ch"]), 0
                    )
            finally:
                controller.DASHBOARD.enabled = old_dashboard_enabled
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertFalse(state.exists())
            resolve_agent.assert_not_called()
            runtime.assert_not_called()
            self.assertIn(
                "Clear-history completed",
                controller.controller_log_path(root).read_text(encoding="utf-8"),
            )

    def test_task_start_audit_includes_repo_and_queued_author(self):
        task = controller.parse_tasks(TASKLIST)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = controller.queue_path(root)
            queued = controller.queue_task_from_remote(
                1, task, root / "sample-repo", "remote", "a" * 40,
                "Task Owner <owner@example.invalid>",
            )
            controller.save_queue(queue, [queued])
            with mock.patch.object(controller, "log") as log_mock, mock.patch.object(
                controller, "ensure_clean", side_effect=controller.TokenshareError("stop")
            ), mock.patch.object(
                controller, "local_managed_branch", return_value=None
            ), self.assertRaisesRegex(controller.TokenshareError, "stop"):
                controller.process_task(
                    root / "sample-repo", task, "agent", state={},
                    state_path=root / "state.json", use_tmux=False,
                    logs_dir=controller.repository_logs_path(root), local_queue=queue,
                )
            first = log_mock.call_args_list[0].args[0]
            self.assertIn("repo=sample-repo", first)
            self.assertIn("author=Task Owner <owner@example.invalid>", first)
            self.assertIn("mode=claim", first)

    def test_tui_help_uses_cli_style_command_summaries(self):
        output = []
        runtime = types.SimpleNamespace()
        self.assertTrue(controller.handle_controller_command("help", runtime, output.append))
        rendered = output[0]
        self.assertIn("usage: COMMAND [ARGS]", rendered)
        self.assertIn("approve SELECTOR", rendered)
        self.assertIn("all not SELECTOR", rendered)

    def test_log_entry_monitor_deduplicates_rewrites_and_reordering(self):
        first = "## 2026-01-01T00:00:00Z\n\n- Progress: first\n"
        second = "## 2026-01-01T00:01:00Z\n\n- Progress: second\n"
        third = "## 2026-01-01T00:02:00Z\n\n- Progress: third\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task_log.md"
            path.write_text("# Log\n\n" + first, encoding="utf-8")
            seen = {fingerprint for fingerprint, _ in controller._log_blocks(
                path.read_text(encoding="utf-8")
            )}
            path.write_text("# Log\n\n" + first + "\n" + second, encoding="utf-8")
            _current, seen, entries = controller._new_log_entries(path, seen)
            self.assertEqual(len(entries), 1)
            self.assertIn("second", entries[0])
            path.write_text("# Log\n\n" + second + "\n" + first + "\n" + third,
                            encoding="utf-8")
            _current, seen, entries = controller._new_log_entries(path, seen)
            self.assertEqual(len(entries), 1)
            self.assertIn("third", entries[0])

    def test_full_screen_tui_preserves_typed_command_on_log_update(self):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with tempfile.TemporaryDirectory() as directory, create_pipe_input() as pipe_input:
            root = Path(directory)
            queue = controller.queue_path(root)
            controller.save_queue(queue, [])
            runtime = types.SimpleNamespace(
                queue_file=queue,
                logs_dir=root / "logs",
                repository_logs_dir=controller.repository_logs_path(root),
                toolbar=lambda: " active: none | workers: 0/1 | uptime: 00:00:01 ",
            )
            tui = controller.ControllerTUI(
                runtime, input=pipe_input, output=DummyOutput()
            )
            try:
                tui.command_area.text = "approve 1"
                tui.enqueue_log("[2026-01-01T00:00:00Z] background update")
                tui._before_render(tui.application)
                self.assertEqual(tui.command_area.text, "approve 1")
                self.assertIn("background update", tui.log_area.text)
                self.assertTrue(tui.application.full_screen)
                tui._set_task_height(1)
                self.assertEqual(tui.task_height, 4)
                tui._set_task_height(10_000)
                self.assertGreaterEqual(tui.task_height, 4)
                from prompt_toolkit.data_structures import Point
                from prompt_toolkit.mouse_events import (
                    MouseButton, MouseEvent, MouseEventType,
                )
                window = types.SimpleNamespace(render_info=None)
                tui.task_height = 10
                tui._handle_resize_mouse(
                    MouseEvent(Point(x=0, y=5), MouseEventType.MOUSE_DOWN,
                               MouseButton.LEFT, frozenset()),
                    window,
                )
                tui._handle_resize_mouse(
                    MouseEvent(Point(x=0, y=8), MouseEventType.MOUSE_MOVE,
                               MouseButton.LEFT, frozenset()),
                    window,
                )
                self.assertEqual(tui.task_height, 13)
            finally:
                tui.close()

    def test_full_screen_tui_processes_commands_and_exits_cleanly(self):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with tempfile.TemporaryDirectory() as directory, create_pipe_input() as pipe_input:
            root = Path(directory)
            queue = controller.queue_path(root)
            controller.save_queue(queue, [])
            runtime = types.SimpleNamespace(
                queue_file=queue,
                logs_dir=root / "logs",
                repository_logs_dir=controller.repository_logs_path(root),
                toolbar=lambda: " active: none ",
                start=mock.Mock(),
                stop=mock.Mock(),
            )
            tui = controller.ControllerTUI(
                runtime, input=pipe_input, output=DummyOutput()
            )
            pipe_input.send_text("help\nquit\n")
            self.assertEqual(tui.run(), 0)
            runtime.start.assert_called_once_with()
            runtime.stop.assert_called_once_with()

    def test_interactive_log_listener_suppresses_legacy_terminal_print(self):
        listener = mock.Mock()
        controller.LOG_LISTENERS.append(listener)
        try:
            with mock.patch.object(controller.DASHBOARD, "message") as dashboard_message:
                controller.log("background event")
            dashboard_message.assert_not_called()
            listener.assert_called_once()
        finally:
            controller.LOG_LISTENERS.remove(listener)


class ControllerIntegrationTests(unittest.TestCase):
    def git(self, *args, cwd=None, env=None):
        subprocess.run(
            ["git", *args], cwd=cwd, env=env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_once_clones_runs_agent_and_pushes_done_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = root / "seed"
            remote = root / "task-repo.git"
            workspace = root / "workspace"
            seed.mkdir()
            self.git("init", cwd=seed)
            self.git("config", "user.name", "Tokenshare Test", cwd=seed)
            self.git("config", "user.email", "tokenshare@example.invalid", cwd=seed)
            multiple = TWO_TASKS.replace(
                "## Pending Tasks",
                "## Configuration\nallow-multiple-branches: true\n## Pending Tasks",
            )
            (seed / "tokenshare_tasklist.md").write_text(multiple, encoding="utf-8")
            self.git("add", ".", cwd=seed)
            self.git("commit", "-m", "seed", cwd=seed)
            self.git("init", "--bare", str(remote))
            self.git("remote", "add", "origin", str(remote), cwd=seed)
            self.git("push", "-u", "origin", "HEAD", cwd=seed)

            fake_agent = root / "fake_agent.py"
            fake_agent.write_text(
                "import pathlib, sys\n"
                "sys.stdin.read()\n"
                "pathlib.Path('agent-result.txt').write_text('ok\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            config = root / "task_repos.md"
            config.write_text(f"# Tokenshare Task Repos\n{remote}\n", encoding="utf-8")
            command = f'"{sys.executable}" "{fake_agent}"'
            env = os.environ.copy()
            env.update(
                {
                    "GIT_AUTHOR_NAME": "Tokenshare Test",
                    "GIT_AUTHOR_EMAIL": "tokenshare@example.invalid",
                    "GIT_COMMITTER_NAME": "Tokenshare Test",
                    "GIT_COMMITTER_EMAIL": "tokenshare@example.invalid",
                    "TOKENSHARE_STATE": str(root / "state.json"),
                }
            )
            previous = os.environ.copy()
            os.environ.update(env)
            try:
                result = controller.main(
                    [
                        "--config", str(config),
                        "--workspace", str(workspace),
                        "--agent-command", command,
                        "--no-tmux",
                        "--non-interactive-mode",
                        "--dangerously-skip-approvals",
                    ]
                )
            finally:
                os.environ.clear()
                os.environ.update(previous)
            self.assertEqual(result, 0)
            queue = controller.queue_path(workspace)
            checkout = workspace / "task-repo"
            default_text = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "HEAD:tokenshare_tasklist.md"],
                check=True, stdout=subprocess.PIPE, text=True,
            ).stdout
            self.assertEqual(default_text, multiple)
            branches = subprocess.run(
                ["git", f"--git-dir={remote}", "for-each-ref", "--format=%(refname:short)",
                 "refs/heads"],
                check=True, stdout=subprocess.PIPE, text=True,
            ).stdout.splitlines()
            branches = [branch for branch in branches if branch.startswith("tokenshare-dev-")]
            self.assertEqual(len(branches), 2)
            for branch in branches:
                completed_text = subprocess.run(
                    ["git", f"--git-dir={remote}", "show", f"{branch}:tokenshare_tasklist.md"],
                    check=True, stdout=subprocess.PIPE, text=True,
                ).stdout
                self.assertIn("[Done]", completed_text)
                published = subprocess.run(
                    ["git", f"--git-dir={remote}", "show", f"{branch}:agent-result.txt"],
                    check=True, stdout=subprocess.PIPE, text=True,
                )
                self.assertEqual(published.stdout, "ok\n")
                paths = subprocess.run(
                    ["git", f"--git-dir={remote}", "ls-tree", "-r", "--name-only", branch],
                    check=True, stdout=subprocess.PIPE, text=True,
                ).stdout.splitlines()
                self.assertFalse(any("status" in path or "/logs/" in f"/{path}/"
                                     for path in paths))
            local_logs = workspace / "logs"
            self.assertTrue(
                (local_logs / "agent" / "tokenshare_agent_tasklist.md").is_file()
            )
            self.assertEqual(
                len(list((local_logs / "repos").glob("tokenshare-dev-*_log.md"))), 2
            )

            # Clearing local history must not make deterministic local review branches
            # collide when the corresponding remote review branches were removed.
            for branch in branches:
                self.git("push", "origin", "--delete", branch, cwd=checkout)
            controller.clear_controller_history(workspace, root / "state.json")
            with mock.patch.dict(os.environ, env, clear=False):
                result = controller.main(
                    ["--config", str(config), "--workspace", str(workspace),
                     "--agent-command", command, "--no-tmux",
                     "--non-interactive-mode", "--dangerously-skip-approvals"]
                )
            self.assertEqual(result, 0)

    def test_default_stops_after_one_outstanding_task_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = root / "seed"
            remote = root / "task-repo.git"
            workspace = root / "workspace"
            seed.mkdir()
            self.git("init", cwd=seed)
            self.git("config", "user.name", "Tokenshare Test", cwd=seed)
            self.git("config", "user.email", "tokenshare@example.invalid", cwd=seed)
            (seed / "tokenshare_tasklist.md").write_text(TWO_TASKS, encoding="utf-8")
            self.git("add", ".", cwd=seed)
            self.git("commit", "-m", "seed", cwd=seed)
            self.git("init", "--bare", str(remote))
            self.git("remote", "add", "origin", str(remote), cwd=seed)
            self.git("push", "-u", "origin", "HEAD", cwd=seed)

            fake_agent = root / "fake_agent.py"
            fake_agent.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
            config = root / "task_repos.md"
            config.write_text(f"{remote}\n", encoding="utf-8")
            command = f'"{sys.executable}" "{fake_agent}"'
            environment = {
                "GIT_AUTHOR_NAME": "Tokenshare Test",
                "GIT_AUTHOR_EMAIL": "tokenshare@example.invalid",
                "GIT_COMMITTER_NAME": "Tokenshare Test",
                "GIT_COMMITTER_EMAIL": "tokenshare@example.invalid",
                "TOKENSHARE_STATE": str(root / "state.json"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                result = controller.main(
                    [
                        "--config", str(config),
                        "--workspace", str(workspace),
                        "--agent-command", command,
                        "--no-tmux",
                        "--non-interactive-mode",
                    ]
                )
            self.assertEqual(result, 0)
            controller.approve_tasks(
                controller.queue_path(workspace), "all", workspace / "logs"
            )
            with mock.patch.dict(os.environ, environment, clear=False):
                result = controller.main(
                    ["--config", str(config), "--workspace", str(workspace),
                     "--agent-command", command, "--no-tmux",
                     "--non-interactive-mode"]
                )
            self.assertEqual(result, 0)
            branches = subprocess.run(
                ["git", f"--git-dir={remote}", "for-each-ref", "--format=%(refname:short)",
                 "refs/heads"],
                check=True, stdout=subprocess.PIPE, text=True,
            ).stdout.splitlines()
            managed = [branch for branch in branches if branch.startswith("tokenshare-dev-")]
            self.assertEqual(len(managed), 1)
            default_text = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "HEAD:tokenshare_tasklist.md"],
                check=True, stdout=subprocess.PIPE, text=True,
            ).stdout
            self.assertEqual(default_text, TWO_TASKS)

            fresh_environment = dict(environment)
            fresh_environment["TOKENSHARE_STATE"] = str(root / "fresh-state.json")
            with mock.patch.dict(os.environ, fresh_environment, clear=False):
                fresh_result = controller.main(
                    [
                        "--config", str(config),
                        "--workspace", str(root / "fresh-workspace"),
                        "--agent-command", command,
                        "--no-tmux",
                        "--non-interactive-mode",
                    ]
                )
            self.assertEqual(fresh_result, 0)
            fresh_branches = subprocess.run(
                ["git", f"--git-dir={remote}", "for-each-ref", "--format=%(refname:short)",
                 "refs/heads"],
                check=True, stdout=subprocess.PIPE, text=True,
            ).stdout.splitlines()
            self.assertEqual(
                len([branch for branch in fresh_branches if branch.startswith("tokenshare-dev-")]),
                1,
            )

            self.git("update-ref", "-d", f"refs/heads/{managed[0]}", cwd=remote)
            with mock.patch.dict(os.environ, environment, clear=False):
                second = controller.main(
                    [
                        "--config", str(config),
                        "--workspace", str(workspace),
                        "--agent-command", command,
                        "--no-tmux",
                        "--non-interactive-mode",
                    ]
                )
            self.assertEqual(second, 0)
            remaining = subprocess.run(
                ["git", f"--git-dir={remote}", "for-each-ref", "--format=%(refname:short)",
                 "refs/heads"],
                check=True, stdout=subprocess.PIPE, text=True,
            ).stdout.splitlines()
            remaining_managed = [
                branch for branch in remaining if branch.startswith("tokenshare-dev-")
            ]
            self.assertEqual(len(remaining_managed), 1)
            self.assertNotEqual(remaining_managed[0], managed[0])
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            entries = next(iter(state["repositories"].values()))
            self.assertIn("declined", {entry["status"] for entry in entries.values()})


if __name__ == "__main__":
    unittest.main()
