#!/usr/bin/env bash

# Codex unrestricted launcher
# Model: gpt-5.6-sol
# Reasoning: medium
# Permissions: bypass approvals + sandbox

exec codex \
    --model gpt-5.6-sol \
    --dangerously-bypass-approvals-and-sandbox \
    "$@"
