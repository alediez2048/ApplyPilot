"""LinkedIn people-search fallback — hardened, opt-in, off by default.

Runs ONLY when Apollo returns too few contacts AND the feature is explicitly enabled.
Reuses the apply Chrome primitives but spawns a NEW thin agent (copying only the
Popen/stream-json pattern) with an ENFORCED read-only Playwright tool allowlist and a
Playwright-only MCP config (no Gmail).

Safeguards, all enforced here:
  - off by default (NETWORKING_LINKEDIN=0); requires a one-time consent acknowledgement
  - read-only tools (see prompt.READONLY_TOOLS) — cannot click/message/connect
  - login precheck — aborts cleanly if not logged into LinkedIn
  - global daily cap (NETWORKING_LINKEDIN_DAILY_LIMIT), persisted across runs
  - isolated Chrome profile + CDP port (separate from apply workers)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import date

from applypilot import config
from applypilot.networking.prompt import READONLY_TOOLS, build_linkedin_prompt

log = logging.getLogger(__name__)

# Dedicated worker slot so networking never collides with apply workers (0..N).
_WORKER_ID = 90
_CDP_PORT = 9222 + _WORKER_ID
_AGENT_TIMEOUT = 180

_CONSENT_FILE = config.APP_DIR / ".networking_linkedin_consent"
_USAGE_FILE = config.APP_DIR / "networking_linkedin_usage.json"


# ── feature flags / consent ─────────────────────────────────────────────────

def enabled() -> bool:
    return os.environ.get("NETWORKING_LINKEDIN", "0").lower() in {"1", "true", "yes", "on"}


def has_consent() -> bool:
    return _CONSENT_FILE.exists()


def record_consent() -> None:
    _CONSENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONSENT_FILE.write_text("acknowledged\n", encoding="utf-8")


CONSENT_TEXT = (
    "LinkedIn automation drives YOUR primary LinkedIn account with a browser agent.\n"
    "This violates LinkedIn's no-bots Terms of Service and can result in PERMANENT\n"
    "restriction of your account (not just a temporary block), plus the monthly\n"
    "commercial-use search lock. It is read-only (never connects or messages) and\n"
    "capped, but the risk is real. Consider using a secondary account.\n"
)


# ── global daily cap (persisted) ─────────────────────────────────────────────

def _daily_limit() -> int:
    try:
        return int(os.environ.get("NETWORKING_LINKEDIN_DAILY_LIMIT", "5") or "5")
    except ValueError:
        return 5


def _load_usage() -> dict:
    try:
        return json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def companies_today() -> int:
    u = _load_usage()
    return int(u.get("count", 0)) if u.get("date") == date.today().isoformat() else 0


def _bump_usage() -> None:
    today = date.today().isoformat()
    u = _load_usage()
    count = int(u.get("count", 0)) + 1 if u.get("date") == today else 1
    _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USAGE_FILE.write_text(json.dumps({"date": today, "count": count}), encoding="utf-8")


def under_daily_cap() -> bool:
    return companies_today() < _daily_limit()


# ── MCP config (Playwright only — drops the Gmail server apply uses) ─────────

def _mcp_config(cdp_port: int) -> dict:
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            }
        }
    }


def _parse_people(stdout_text: str, limit: int) -> list[dict]:
    """Extract the JSON array of people from the agent's stream-json output."""
    # Find the last JSON array in the text (the agent's final answer).
    import re
    candidates = re.findall(r"\[\s*{.*?}\s*\]", stdout_text, re.DOTALL)
    for blob in reversed(candidates):
        try:
            arr = json.loads(blob)
        except json.JSONDecodeError:
            continue
        out = []
        for p in arr:
            if isinstance(p, dict) and p.get("name"):
                out.append({
                    "full_name": p.get("name"),
                    "title": p.get("title"),
                    "linkedin_url": p.get("profile_url"),
                })
            if len(out) >= limit:
                break
        return out
    return []


# ── login precheck ───────────────────────────────────────────────────────────

def login_state_ok(chrome_mod=None) -> bool:
    """Best-effort check that the worker profile is logged into LinkedIn.

    We look for LinkedIn auth cookies in the worker profile. Returns False (caller
    aborts cleanly) if we can't confirm a session, rather than spawning a doomed agent.
    """
    profile = config.CHROME_WORKER_DIR / f"worker-{_WORKER_ID}"
    cookies = profile / "Default" / "Cookies"
    if not cookies.exists():
        return False
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{cookies}?mode=ro", uri=True, timeout=2)
        row = con.execute(
            "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%linkedin.com' AND name='li_at'"
        ).fetchone()
        con.close()
        return bool(row and row[0] > 0)
    except Exception:  # noqa: BLE001 - locked/absent -> treat as unknown -> abort
        return False


def open_login_browser() -> None:
    """`applypilot network --linkedin-login`: open the worker profile to log in once."""
    from applypilot.apply import chrome
    chrome.setup_worker_profile(_WORKER_ID)
    proc = chrome.launch_chrome(_WORKER_ID, port=_CDP_PORT, headless=False)
    log.info("Opened Chrome (worker %d). Log into LinkedIn, then close the window.", _WORKER_ID)
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass


# ── main entry ───────────────────────────────────────────────────────────────

def find_people(company: str, role: str | None, n: int = 5) -> list[dict]:
    """Read-only LinkedIn People search. Returns [] on any gate/failure (never raises)."""
    if not enabled():
        log.debug("LinkedIn fallback disabled (NETWORKING_LINKEDIN=0)")
        return []
    if not has_consent():
        log.warning("LinkedIn fallback needs one-time consent — run `applypilot network --linkedin-login`.")
        return []
    if not under_daily_cap():
        log.warning("LinkedIn daily company cap reached (%d).", _daily_limit())
        return []
    import shutil
    if not shutil.which("claude"):
        return []
    if not login_state_ok():
        log.warning("Not logged into LinkedIn (worker profile) — run `applypilot network --linkedin-login`.")
        return []

    from applypilot.apply import chrome
    chrome.setup_worker_profile(_WORKER_ID)
    chrome_proc = None
    try:
        chrome_proc = chrome.launch_chrome(_WORKER_ID, port=_CDP_PORT, headless=True)
        time.sleep(2)

        mcp_path = config.APP_DIR / f".mcp-network-{_WORKER_ID}.json"
        mcp_path.write_text(json.dumps(_mcp_config(_CDP_PORT)), encoding="utf-8")

        cmd = [
            "claude", "--model", "haiku", "-p",
            "--mcp-config", str(mcp_path),
            "--permission-mode", "bypassPermissions",
            "--allowedTools", ",".join(READONLY_TOOLS),  # ENFORCED read-only
            "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "-",
        ]
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
            cwd=str(chrome.reset_worker_dir(_WORKER_ID)),
        )
        proc.stdin.write(build_linkedin_prompt(company, role, n))
        proc.stdin.close()

        try:
            out, _ = proc.communicate(timeout=_AGENT_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            log.warning("LinkedIn agent timed out for %s", company)
            return []

        _bump_usage()  # count the company view against the daily cap
        people = _parse_people(out or "", n)
        log.info("LinkedIn fallback: %d people for %s", len(people), company)
        return people
    except Exception as e:  # noqa: BLE001
        log.warning("LinkedIn fallback error for %s: %s", company, e)
        return []
    finally:
        if chrome_proc is not None:
            chrome.cleanup_worker(_WORKER_ID, chrome_proc)
