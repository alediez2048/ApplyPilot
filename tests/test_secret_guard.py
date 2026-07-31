"""The pre-commit secret guard — because this repository is PUBLIC and cannot be made private.

GitHub refuses to change a fork's visibility (it would let private history escape a public
network), so `alediez2048/ApplyPilot` is public and staying that way unless it is detached or
migrated. The history is clean today — verified across all branches — and every real secret
lives in `~/.applypilot/`, outside this directory.

So the risk is not the current state. It is one `git add -f`, one debug print of a token into a
committed log, one `cp ~/.applypilot/.env .` while chasing a bug. On a public repo that is
public instantly and permanent, because a push cannot be un-published.

A guard nobody tests is a shell script with good intentions. These run the real hook.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".githooks" / "pre-commit"


def _run(tmp_path: Path, filename: str, content: str) -> bool:
    """Stage `content` as `filename` in a throwaway repo and run the hook. True = blocked."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    target = tmp_path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-f", filename], cwd=tmp_path, check=True)
    r = subprocess.run(["sh", str(HOOK)], cwd=tmp_path, capture_output=True, text=True)
    return r.returncode != 0


def test_the_hook_is_installed_and_executable():
    assert HOOK.exists(), "the guard is missing — `sh scripts/install-hooks.sh`"
    import os
    assert os.access(HOOK, os.X_OK), "the guard is not executable, so git silently skips it"


#: Every one of these has a plausible route into a commit: a stray copy, a debug print, an
#: LLM-generated example filled in with a real value.
LEAKS = [
    (".env", "APOLLO_API_KEY=abc123realkeyvalue999", "a real .env"),
    ("leak.py", 'TOKEN = "ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxxx"', "Google access token"),
    ("leak.py", 'R = "1//09abcdefghijklmnopqrstuvwxyz012345"', "Google refresh token"),
    ("leak.py", 'K = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"', "Anthropic key"),
    ("leak.py", 'K = "sk-abcdefghijklmnopqrstuvwxyz1234"', "OpenAI key"),
    ("leak.py", 'K = "AIzaSyA1234567890abcdefghijklmnopqrstu"', "Google API key"),
    ("leak.py", 'K = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"', "GitHub token"),
    ("c.json", '{"client_secret": "GOCSPX-abcdef123456ghijkl"}', "OAuth client_secret"),
    ("k.pem", "-----BEGIN RSA PRIVATE KEY-----\nabc\n", "private key"),
    ("gmail_token.json", '{"token": "x"}', "a credential FILENAME"),
    ("applypilot.db", "sqlite", "the database — it holds correspondence"),
    ("profile.json", '{"a": 1}', "personal data"),
    ("resume.txt", "name", "personal data"),
]


@pytest.mark.parametrize("filename,content,label", LEAKS,
                         ids=[f"{lbl}" for _, _, lbl in LEAKS])
def test_a_secret_cannot_be_committed(tmp_path, filename, content, label):
    assert _run(tmp_path, filename, content) is True, f"{label} was allowed through"


#: A noisy guard gets `--no-verify`d out of habit, and a guard nobody runs protects nothing.
#: These must all pass cleanly.
BENIGN = [
    ("ok.py", 'API_KEY = os.environ.get("APOLLO_API_KEY", "")', "reading a key from env"),
    ("README.md", "Set APOLLO_API_KEY=your-key-here in .env", "documentation"),
    (".env.example", "APOLLO_API_KEY=sk-replace-me-with-your-real-key", "the template"),
    ("t.example", "client_secret: CHANGE-ME-BEFORE-USE", "an example file"),
    ("code.py", '"""Docs mention ya29 tokens conceptually."""', "prose about tokens"),
]


@pytest.mark.parametrize("filename,content,label", BENIGN,
                         ids=[f"{lbl}" for _, _, lbl in BENIGN])
def test_ordinary_work_is_not_blocked(tmp_path, filename, content, label):
    assert _run(tmp_path, filename, content) is False, (
        f"{label} was blocked — false positives are how a guard gets disabled")
