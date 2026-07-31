#!/bin/sh
# One-time: point git at .githooks/ so the secret guard runs on every commit.
# Tracked in the repo (unlike .git/hooks/), so it survives a fresh clone.
cd "$(dirname "$0")/.." || exit 1
git config core.hooksPath .githooks
echo "Hooks enabled: $(git config core.hooksPath)"
echo "The pre-commit secret guard is now active. Bypass once with: git commit --no-verify"
