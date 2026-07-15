from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


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
                "pathlib.Path('agent-ran.txt').write_text('ok\\n', encoding='utf-8')\n"
                "pathlib.Path('agent-ran.txt').unlink()\n",
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


if __name__ == "__main__":
    unittest.main()
