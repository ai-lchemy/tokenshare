from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "install.py"
SPEC = importlib.util.spec_from_file_location("tokenshare_install", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_development_directory_flags_require_absolute_paths(self):
        parser = installer.build_parser()
        for flag in ("-dd", "-development-directory", "--development-directory"):
            args = parser.parse_args([flag, "/work/repos"])
            self.assertEqual(args.development_directory, Path("/work/repos"))
        with self.assertRaises(SystemExit):
            parser.parse_args(["-dd", "relative/path"])

    def test_prompt_uses_default_or_absolute_response(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(
                installer.choose_development_directory(None),
                installer.DEFAULT_DEVELOPMENT_DIRECTORY,
            )
        with mock.patch("builtins.input", return_value="/srv/tasks"):
            self.assertEqual(
                installer.choose_development_directory(None), Path("/srv/tasks")
            )

    def test_install_creates_workspace_and_records_source_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            workspace = Path(directory) / "workspace"
            with mock.patch.dict("os.environ", {"CODEX_HOME": str(home / "codex")}):
                installer.install(workspace, home=home)
            self.assertTrue(workspace.is_dir())
            self.assertTrue((home / ".local/bin/tokenshare-controller").is_file())
            metadata = json.loads(
                (home / ".config/tokenshare/install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["install_directory"], str(SCRIPT.parent))
            self.assertEqual(metadata["development_directory"], str(workspace))


if __name__ == "__main__":
    unittest.main()
