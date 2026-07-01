#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../tokenshare.sh
source "$ROOT/tokenshare.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_eq() {
  local expected=$1 actual=$2 label=$3
  [[ "$expected" == "$actual" ]] || fail "$label: expected [$expected], got [$actual]"
}

fixture=$(mktemp)
cp "$ROOT/test/fixtures/tasks.md" "$fixture"
trap 'rm -f "$fixture"' EXIT

parsed=$(parse_tasks "$fixture")
count=$(printf '%s\n' "$parsed" | sed '/^$/d' | wc -l | tr -d ' ')
assert_eq "3" "$count" "valid task count ignores malformed tasks"

selected=$(select_bottom_pending "$fixture")
IFS=$'\t' read -r start end state repo title <<< "$selected"
assert_eq "Pending" "$state" "bottom-most pending state"
assert_eq "https://gitlab.example/acme/last.git" "$repo" "bottom-most pending repo"
assert_eq "Bottom pending task" "$title" "bottom-most pending title"

replace_state "$fixture" "$start" "$end" "Pending" "WIP"
updated=$(sed -n "${start},${end}p" "$fixture" | awk '/State:/ {print $3; exit}')
assert_eq "WIP" "$updated" "Pending transitions to WIP"

replace_state "$fixture" "$start" "$end" "WIP" "Done"
updated=$(sed -n "${start},${end}p" "$fixture" | awk '/State:/ {print $3; exit}')
assert_eq "Done" "$updated" "WIP transitions to Done"

invalid=$(parse_tasks "$fixture" | awk -F '\t' '$3 !~ /^(Pending|WIP|Done)$/ { print }')
assert_eq "" "$invalid" "malformed states are absent"

echo "All token-share parser tests passed"
