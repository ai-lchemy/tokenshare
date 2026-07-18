from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
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
        args = controller.build_parser().parse_args([])
        root = SCRIPT.parents[1]
        self.assertEqual(args.config, root / "config" / "task_repos.md")
        self.assertEqual(args.workspace, root / "dev")
        self.assertEqual(args.agent_command, "codex --full-auto")
        self.assertIsNone(args.agent)
        self.assertFalse(args.no_tmux)
        self.assertFalse(args.auto_push)
        self.assertIsNone(args.auto_attach)

    def test_auto_attach_accepts_current_or_explicit_tty(self):
        parser = controller.build_parser()
        self.assertEqual(parser.parse_args(["--auto-attach"]).auto_attach, "current")
        self.assertEqual(
            parser.parse_args(["--auto-attach", "/dev/pts/9"]).auto_attach,
            "/dev/pts/9",
        )

    def test_auto_attach_rejects_no_tmux(self):
        with self.assertRaisesRegex(controller.TokenshareError, "cannot be used"):
            controller.main(["--no-tmux", "--auto-attach"])

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

    def test_tmux_client_attachment_switches_back_to_original_session(self):
        target = controller.AttachTarget(Path("/dev/pts/9"), False)
        attachment = controller.AgentAttachment("agent-session", target)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            controller, "_tmux_client_for_tty", return_value=("client-1", "controller")
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
            controller, "_tmux_client_for_tty", return_value=("client-1", "other")
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

    def test_provider_prefixed_stubs_receive_their_trust_flags(self):
        claude = controller._agent_args(
            "/tmp/claude-sonnet.sh", "do work", Path("/tmp/repo")
        )
        opencode = controller._agent_args(
            "/tmp/opencode-gpt.sh", "do work", Path("/tmp/repo")
        )
        self.assertIn("--dangerously-skip-permissions", claude)
        self.assertIn("--auto", opencode)

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

    def test_push_requires_approval_unless_preapproved(self):
        task = controller.parse_tasks(TASKLIST)[0]
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertFalse(controller.approve_push(Path("repo"), task, auto_push=False))
        self.assertTrue(controller.approve_push(Path("repo"), task, auto_push=True))

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
            (seed / "tokenshare_tasklist.md").write_text(TWO_TASKS, encoding="utf-8")
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
                        "--auto-push",
                        "--once",
                    ]
                )
            finally:
                os.environ.clear()
                os.environ.update(previous)
            self.assertEqual(result, 0)
            checkout = workspace / "task-repo"
            updated = (checkout / "tokenshare_tasklist.md").read_text(encoding="utf-8")
            self.assertIn("[Done] Implement Awesome Feature", updated)
            self.assertIn("[Done] Implement Another Feature", updated)
            statuses = list((checkout / "docs").glob("status_*.md"))
            self.assertEqual(len(statuses), 2)
            for status in statuses:
                status_text = status.read_text(encoding="utf-8")
                self.assertIn("- State: implementing", status_text)
                self.assertIn("- State: testing", status_text)
                self.assertIn("- State: complete", status_text)
            published = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "HEAD:agent-result.txt"],
                check=True, stdout=subprocess.PIPE, text=True,
            )
            self.assertEqual(published.stdout, "ok\n")


if __name__ == "__main__":
    unittest.main()
