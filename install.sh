#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tokenshare_root="$repo_dir"
codex_home="${CODEX_HOME:-$HOME/.codex}"
bin_dir="${HOME}/.local/bin"

mkdir -p "$codex_home/skills/tokenshare" "$bin_dir"
cp -R "$repo_dir/skills/tokenshare/." "$codex_home/skills/tokenshare/"
cp "$repo_dir/scripts/tokenshare-controller.py" "$bin_dir/tokenshare-controller"
chmod +x "$bin_dir/tokenshare-controller"

printf 'Installed Tokenshare skill to %s\n' "$codex_home/skills/tokenshare"
printf 'Installed controller to %s\n' "$bin_dir/tokenshare-controller"
printf 'Primary checkout config: %s\n' "$tokenshare_root/config/task_repos.md"
