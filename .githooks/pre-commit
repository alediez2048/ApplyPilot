#!/bin/sh
# Block a commit that would publish a secret.
#
# Why this exists: the repo is a PUBLIC fork, and a fork cannot be made private (GitHub
# refuses, to stop private history leaking out of a public network). Today the history is
# clean — verified across all branches with --diff-filter=A — and every real secret lives in
# ~/.applypilot/, outside this directory. So the danger is not the current state. It is one
# future `git add -f`, one debug print of a token into a committed log, one `cp ~/.applypilot/
# .env .` while chasing a bug. On a public repo that is instantly public and permanently in
# the history, because a push cannot be un-published.
#
# Install:  git config core.hooksPath .githooks   (see scripts/install-hooks.sh)
# Bypass:   git commit --no-verify   (deliberate, and it says so)

fail=0
say() { printf '%s\n' "$*" >&2; }

# 1. Files that must never be tracked, even with -f. Matched on PATH, so a copy under a new
#    name in the repo root is caught too.
staged=$(git diff --cached --name-only --diff-filter=ACM)
for f in $staged; do
  case "$f" in
    .env|.env.*|*/.env|*/.env.*)
      [ "$f" = ".env.example" ] || { say "BLOCKED  $f  — env file"; fail=1; } ;;
    *gmail_token*|*oauth_client*|*_token|*token.json|*.pem|*id_rsa*|*.p12|*.pfx)
      say "BLOCKED  $f  — credential file"; fail=1 ;;
    *.db|*.sqlite|*.sqlite3)
      say "BLOCKED  $f  — database (holds contacts and correspondence)"; fail=1 ;;
    profile.json|resume.txt|resume.pdf|*/profile.json|*/resume.txt)
      say "BLOCKED  $f  — personal data"; fail=1 ;;
  esac
done

# 2. Secret-SHAPED content in the staged diff. Deliberately narrow: a noisy guard gets
#    --no-verify'd habitually, and a guard nobody runs protects nothing.
#    .example files are skipped — they exist to carry placeholders.
#    tests/test_secret_guard.py is excluded by exact path: it exists to feed this guard
#    fake secrets, so scanning it blocks the very test that proves the guard works. The
#    FILENAME rules above still apply to it, and the exclusion is one named file, not a glob.
content=$(git diff --cached --diff-filter=ACM -U0 -- . ':(exclude)*.example' ':(exclude)*.md' \
          ':(exclude)tests/test_secret_guard.py' ':(exclude)scripts/pre-commit-secret-guard.sh' \
          | grep '^+' | grep -v '^+++')

check() {
  # -e is required: a pattern starting with "-" (the PRIVATE KEY block) is otherwise parsed as
  # a grep option, and that check silently never ran. Caught by the hook rejecting its own
  # test file and printing the usage message.
  hits=$(printf '%s\n' "$content" | grep -nEi -e "$1" | head -3)
  [ -n "$hits" ] && { say "BLOCKED  $2"; printf '%s\n' "$hits" | sed 's/^/         /' >&2; fail=1; }
}

check 'ya29\.[A-Za-z0-9_-]{20,}'                  'Google OAuth access token (ya29.…)'
check '1//[A-Za-z0-9_-]{30,}'                     'Google refresh token (1//…)'
check 'sk-[A-Za-z0-9_-]{20,}'                     'OpenAI-style API key (sk-…)'
check 'sk-ant-[A-Za-z0-9_-]{20,}'                 'Anthropic API key (sk-ant-…)'
check 'AIza[A-Za-z0-9_-]{30,}'                    'Google API key (AIza…)'
check 'ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}' 'GitHub token'
check '"client_secret"[[:space:]]*:[[:space:]]*"[^"]{10,}"' 'OAuth client_secret'
check '-----BEGIN [A-Z ]*PRIVATE KEY-----'        'private key block'
# An assignment with a long opaque value. Placeholders (<, your-, xxx, CHANGE) are allowed.
check '(APOLLO_API_KEY|GMAIL_APP_PASSWORD|OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY)[[:space:]]*=[[:space:]]*["'"'"']?[A-Za-z0-9_/+.-]{16,}' \
      'API key assigned a real-looking value'

if [ "$fail" -ne 0 ]; then
  say ""
  say "Commit blocked — this repository is PUBLIC and a push cannot be un-published."
  say "If this is genuinely a placeholder:  git commit --no-verify"
  exit 1
fi
exit 0
