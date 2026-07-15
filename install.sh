#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
bin_dir="${HOME}/.local/bin"

mkdir -p "$codex_home/skills/tokenshare" "$bin_dir" "$HOME/.config/tokenshare"
cp -R "$repo_dir/skills/tokenshare/." "$codex_home/skills/tokenshare/"
cp "$repo_dir/scripts/tokenshare-controller.py" "$bin_dir/tokenshare-controller"
chmod +x "$bin_dir/tokenshare-controller"

if [[ ! -e "$HOME/.config/tokenshare/task_repos.md" ]]; then
  cp "$repo_dir/config/task_repos.md" "$HOME/.config/tokenshare/task_repos.md"
fi

printf 'Installed Tokenshare skill to %s\n' "$codex_home/skills/tokenshare"
printf 'Installed controller to %s\n' "$bin_dir/tokenshare-controller"
printf 'Configure repositories in %s\n' "$HOME/.config/tokenshare/task_repos.md"
