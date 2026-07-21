#!/usr/bin/env python3
"""Install Tokenshare for the current user."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


DEFAULT_DEVELOPMENT_DIRECTORY = Path.home() / "tokenshare_dev"


def absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("development directory must be an absolute path")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-dd",
        "-development-directory",
        "--development-directory",
        type=absolute_path,
        metavar="PATH",
        help="absolute directory in which task repositories will be cloned",
    )
    return parser


def choose_development_directory(configured: Path | None) -> Path:
    if configured is not None:
        return configured
    prompt = f"Development directory [{DEFAULT_DEVELOPMENT_DIRECTORY}]: "
    try:
        response = input(prompt).strip()
    except EOFError:
        response = ""
    if not response:
        return DEFAULT_DEVELOPMENT_DIRECTORY
    path = Path(response).expanduser()
    if not path.is_absolute():
        raise ValueError("development directory must be an absolute path")
    return path


def install(development_directory: Path, *, home: Path | None = None) -> None:
    repo_dir = Path(__file__).resolve().parent
    home = home or Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
    skill_dir = codex_home / "skills" / "tokenshare"
    bin_dir = home / ".local" / "bin"
    metadata_path = home / ".config" / "tokenshare" / "install.json"

    try:
        import prompt_toolkit  # noqa: F401
    except ImportError:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "prompt_toolkit>=3.0,<4"],
            text=True,
        )
        if result.returncode:
            raise OSError(
                "Could not install prompt_toolkit; install it with the current Python "
                "interpreter and rerun install.py"
            )

    development_directory.mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(repo_dir / "skills" / "tokenshare", skill_dir, dirs_exist_ok=True)
    controller = bin_dir / "tokenshare-controller"
    shutil.copy2(repo_dir / "scripts" / "tokenshare-controller.py", controller)
    controller.chmod(controller.stat().st_mode | 0o111)
    metadata_path.write_text(
        json.dumps(
            {
                "install_directory": str(repo_dir),
                "development_directory": str(development_directory),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Installed Tokenshare skill to {skill_dir}")
    print(f"Installed controller to {controller}")
    print(f"Repository config: {repo_dir / 'config' / 'task_repos.md'}")
    print(f"Development directory: {development_directory}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        development_directory = choose_development_directory(
            args.development_directory
        ).resolve()
        install(development_directory)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
