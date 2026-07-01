#!/usr/bin/env bash
set -euo pipefail

# token-share: poll a GitLab-hosted markdown task list and run an agent on tasks.

TOK_SHARE_HOME="${TOK_SHARE_HOME:-$HOME/tokenshare}"
TASK_LIST_REPO_URL="${TASK_LIST_REPO_URL:-}"
TASK_LIST_REPO_DIR="${TASK_LIST_REPO_DIR:-$TOK_SHARE_HOME/TASK_REPO}"
TASK_LIST_FILE="${TASK_LIST_FILE:-TASKS.md}"
TASK_TARGET_REPO_URL="${TASK_TARGET_REPO_URL:-}"
TASK_TARGET_REPO_DIR="${TASK_TARGET_REPO_DIR:-$TOK_SHARE_HOME/target-repo}"
TOK_SHARE_AGENT="${TOK_SHARE_AGENT:-tokenshare-agent}"
TOK_SHARE_SLEEP_SECONDS="${TOK_SHARE_SLEEP_SECONDS:-300}"
TOK_SHARE_REMOTE="${TOK_SHARE_REMOTE:-origin}"
TOK_SHARE_BRANCH="${TOK_SHARE_BRANCH:-}"
TOK_SHARE_ONCE="${TOK_SHARE_ONCE:-0}"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
fatal() { log "ERROR: $*"; exit 1; }

ensure_repo() {
  local url=$1 dir=$2
  mkdir -p "$(dirname "$dir")"
  if [[ -d "$dir/.git" ]]; then
    git -C "$dir" fetch --prune "$TOK_SHARE_REMOTE"
    if [[ -n "$TOK_SHARE_BRANCH" ]]; then
      git -C "$dir" checkout "$TOK_SHARE_BRANCH"
      git -C "$dir" pull --ff-only "$TOK_SHARE_REMOTE" "$TOK_SHARE_BRANCH"
    else
      git -C "$dir" pull --ff-only
    fi
  else
    git clone "$url" "$dir"
    if [[ -n "$TOK_SHARE_BRANCH" ]]; then
      git -C "$dir" checkout "$TOK_SHARE_BRANCH"
    fi
  fi
}

# Task format (blank lines allowed inside body):
# <!-- tokenshare-task:start -->
# - State: Pending|WIP|Done
# - Repo: https://gitlab.example/group/project.git   # optional if TASK_TARGET_REPO_URL is set
# - Title: Human-readable title
# ...free-form markdown instructions...
# <!-- tokenshare-task:end -->
#
# The parser prints records as: start_line<TAB>end_line<TAB>state<TAB>repo<TAB>title.
# Malformed blocks are skipped instead of failing the polling loop.
parse_tasks() {
  local file=$1
  awk '
    function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
    function emit_if_valid() {
      if (in_block && state ~ /^(Pending|WIP|Done)$/ && start > 0 && end > start) {
        gsub(/\t/, " ", repo); gsub(/\t/, " ", title)
        printf "%d\t%d\t%s\t%s\t%s\n", start, end, state, repo, title
      }
    }
    /<!--[[:space:]]*tokenshare-task:start[[:space:]]*-->/ {
      in_block=1; start=NR; end=0; state=""; repo=""; title=""; next
    }
    /<!--[[:space:]]*tokenshare-task:end[[:space:]]*-->/ {
      if (in_block) { end=NR; emit_if_valid() }
      in_block=0; next
    }
    in_block && /^[[:space:]]*-[[:space:]]*State:[[:space:]]*/ {
      value=$0; sub(/^[[:space:]]*-[[:space:]]*State:[[:space:]]*/, "", value); state=trim(value); next
    }
    in_block && /^[[:space:]]*-[[:space:]]*Repo:[[:space:]]*/ {
      value=$0; sub(/^[[:space:]]*-[[:space:]]*Repo:[[:space:]]*/, "", value); repo=trim(value); next
    }
    in_block && /^[[:space:]]*-[[:space:]]*Title:[[:space:]]*/ {
      value=$0; sub(/^[[:space:]]*-[[:space:]]*Title:[[:space:]]*/, "", value); title=trim(value); next
    }
  ' "$file"
}

select_bottom_pending() {
  parse_tasks "$1" | awk -F '\t' '$3 == "Pending" { line=$0 } END { if (line != "") print line }'
}

replace_state() {
  local file=$1 start=$2 end=$3 from=$4 to=$5 tmp
  tmp=$(mktemp)
  awk -v start="$start" -v end="$end" -v from="$from" -v to="$to" '
    NR >= start && NR <= end && !done && $0 ~ /^[[:space:]]*-[[:space:]]*State:[[:space:]]*/ {
      old=$0
      sub(/State:[[:space:]]*[^[:space:]]+/, "State: " to)
      if (old != $0 && old ~ ("State:[[:space:]]*" from "([[:space:]]*$|[[:space:]])")) done=1
    }
    { print }
    END { if (!done) exit 42 }
  ' "$file" > "$tmp" || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$file"
}

commit_and_push_task_list() {
  local dir=$1 msg=$2
  git -C "$dir" add "$TASK_LIST_FILE"
  if git -C "$dir" diff --cached --quiet; then
    log "No task-list state change to commit"
    return 0
  fi
  git -C "$dir" commit -m "$msg"
  git -C "$dir" push
}

create_worktree() {
  local repo_dir=$1 stamp wt
  stamp=$(date -u '+%Y-%m-%d_%H:%M:%S')
  wt="$TOK_SHARE_HOME/tokenshare_$stamp"
  git -C "$repo_dir" worktree add "$wt"
  printf '%s\n' "$wt"
}

run_agent_for_task() {
  local worktree=$1 task_file=$2 start=$3 end=$4 title=$5
  (
    cd "$worktree"
    TOK_SHARE_TASK_FILE="$task_file" \
    TOK_SHARE_TASK_START_LINE="$start" \
    TOK_SHARE_TASK_END_LINE="$end" \
    TOK_SHARE_TASK_TITLE="$title" \
    "$TOK_SHARE_AGENT"
  )
  if ! git -C "$worktree" log -1 --pretty=%B | grep -qE '(^|[[:space:]])/git-commit($|[[:space:]])'; then
    fatal "Agent completed without a latest commit message containing /git-commit; leaving task WIP"
  fi
}

process_once() {
  [[ -n "$TASK_LIST_REPO_URL" ]] || fatal "Set TASK_LIST_REPO_URL to the GitLab task-list repository URL"
  ensure_repo "$TASK_LIST_REPO_URL" "$TASK_LIST_REPO_DIR"
  local task_file="$TASK_LIST_REPO_DIR/$TASK_LIST_FILE"
  [[ -f "$task_file" ]] || fatal "Task file not found: $task_file"

  local selected start end state repo title target_repo worktree
  selected=$(select_bottom_pending "$task_file" || true)
  if [[ -z "$selected" ]]; then
    log "No Pending tasks found"
    return 1
  fi
  IFS=$'\t' read -r start end state repo title <<< "$selected"
  target_repo="${repo:-$TASK_TARGET_REPO_URL}"
  [[ -n "$target_repo" ]] || fatal "Selected task has no Repo and TASK_TARGET_REPO_URL is unset"

  log "Claiming task at lines $start-$end: ${title:-untitled}"
  replace_state "$task_file" "$start" "$end" "Pending" "WIP"
  commit_and_push_task_list "$TASK_LIST_REPO_DIR" "tokenshare: mark task WIP"

  ensure_repo "$target_repo" "$TASK_TARGET_REPO_DIR"
  worktree=$(create_worktree "$TASK_TARGET_REPO_DIR")
  log "Created worktree $worktree"
  run_agent_for_task "$worktree" "$task_file" "$start" "$end" "$title"

  ensure_repo "$TASK_LIST_REPO_URL" "$TASK_LIST_REPO_DIR"
  replace_state "$task_file" "$start" "$end" "WIP" "Done"
  commit_and_push_task_list "$TASK_LIST_REPO_DIR" "tokenshare: mark task Done"
  log "Completed task at original lines $start-$end"
  return 0
}

main() {
  mkdir -p "$TOK_SHARE_HOME"
  while true; do
    if ! process_once; then
      if [[ "$TOK_SHARE_ONCE" == "1" ]]; then exit 0; fi
      sleep "$TOK_SHARE_SLEEP_SECONDS"
    fi
    [[ "$TOK_SHARE_ONCE" == "1" ]] && exit 0
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
