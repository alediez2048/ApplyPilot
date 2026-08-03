"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection, log_event
from applypilot.apply import accounts
from applypilot.apply import pause
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome, keep_chrome_alive,
    chrome_alive_on_port, reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
def _reap_agents_on_exit() -> None:
    """Kill any agent still running when this process exits.

    Needed because the agent now runs in its OWN process group (see the Popen call in
    `run_job`): that stops a kill of the agent from taking this process down with it, but it
    also means the agent no longer dies automatically when we are signalled. Without this an
    interrupted run would leave a Claude session driving a browser with nobody watching.
    """
    with _claude_lock:
        procs = list(_claude_procs.values())
        _claude_procs.clear()
    for p in procs:
        try:
            if p.poll() is None:
                _kill_process_tree(p.pid)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to reap an agent process on exit", exc_info=True)


atexit.register(cleanup_on_exit)
atexit.register(_reap_agents_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Agent tool policy (security)
# ---------------------------------------------------------------------------
#
# The apply agent runs with --permission-mode bypassPermissions and navigates
# arbitrary, attacker-controllable careers pages. A prompt-injection on such a
# page must NOT be able to reach the machine or exfiltrate secrets. We therefore
# hard-deny every built-in tool that could run code, touch the filesystem, or
# make outbound requests — leaving the agent only the browser (Playwright) tools
# it needs plus Gmail *send* (for email-only applications).
#
# Blast radius after this: a successful injection can drive the browser and send
# an email, but cannot run shell commands, read ~/.applypilot secrets, write
# files, or fetch attacker URLs.
_DANGEROUS_BUILTINS = [
    "Bash", "BashOutput", "KillBash", "KillShell",           # code execution
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",    # filesystem
    "Glob", "Grep",                                          # filesystem search
    "WebFetch", "WebSearch",                                 # network exfil
    "Task", "Agent",                                         # spawning sub-agents
]

# Gmail management tools stay blocked (agent may only *send*, never read/modify).
# read_email / search_emails are the important ones: the agent browses ATTACKER-CONTROLLED
# careers pages, so a prompt injection that could read the inbox is a mailbox-exfiltration
# path. The allowlist already excludes them; this is the second lock on the same door.
_GMAIL_DENY = [
    "mcp__gmail__read_email", "mcp__gmail__search_emails",
    "mcp__gmail__draft_email", "mcp__gmail__modify_email", "mcp__gmail__delete_email",
    "mcp__gmail__download_attachment", "mcp__gmail__batch_modify_emails",
    "mcp__gmail__batch_delete_emails", "mcp__gmail__create_label",
    "mcp__gmail__update_label", "mcp__gmail__delete_label",
    "mcp__gmail__get_or_create_label", "mcp__gmail__list_email_labels",
    "mcp__gmail__create_filter", "mcp__gmail__list_filters",
    "mcp__gmail__get_filter", "mcp__gmail__delete_filter",
]

# What the agent IS allowed to do: browser automation + Gmail send only.
_ALLOWED_TOOLS = "mcp__playwright,mcp__gmail__send_email"
_DISALLOWED_TOOLS = ",".join(_DANGEROUS_BUILTINS + _GMAIL_DENY)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND applied_at IS NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    elif status == "needs_review":
        # Co-pilot: filled + waiting for the human to review + submit. NOT applied, NOT a failure,
        # and it does NOT burn an attempt (the human hasn't decided yet). last_attempted_at is
        # stamped so the UI can show when it was prepared.
        conn.execute("""
            UPDATE jobs SET apply_status = 'ready_to_submit', apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?, last_attempted_at = ?
            WHERE url = ?
        """, (duration_ms, task_id, now, url))
    elif status == "needs_human":
        # Co-pilot hard-blocker: paused on a captcha/login/field, browser left open. The human
        # resolves it and clicks Continue (resume). apply_error carries the blocker reason for the
        # UI. Does NOT burn an attempt — the human hasn't failed anything.
        conn.execute("""
            UPDATE jobs SET apply_status = 'needs_human', apply_error = ?, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?, last_attempted_at = ?
            WHERE url = ?
        """, (error or "blocker", duration_ms, task_id, now, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()

    # Activity log — a human-readable summary of what the apply agent did.
    try:
        from applypilot.database import log_event
        secs = f" ({duration_ms // 1000}s)" if duration_ms else ""
        # NOTE: match the status VALUE passed in (not the resulting apply_status). The co-pilot
        # review handoff comes in as "needs_review" (the DB branch maps it to 'ready_to_submit').
        if status == "applied":
            log_event(url, "apply", "ok", f"Application submitted successfully{secs}.")
        elif status == "needs_human":
            # A login wall stops the agent BEFORE it fills anything, so the old wording —
            # "Filled, then paused for you" — described work that had not happened and sent the
            # operator to review a form that did not exist.
            if (error or "") == "account":
                log_event(url, "apply", "info",
                          "Skipped before launching: this employer needs an account you do not "
                          "have yet. Create it once in the Accounts panel.")
            elif (error or "") in ("login", "sso_required", "login_issue"):
                log_event(url, "apply", "info",
                          f"Stopped at a sign-in wall{secs}. Nothing was filled. Sign in in the "
                          f"open browser, then Continue.")
            else:
                log_event(url, "apply", "info",
                          f"Filled, then paused for you: {error or 'blocker'}{secs}. "
                          f"Resolve in the browser + Continue.")
        elif status in ("needs_review", "ready_to_submit"):
            log_event(url, "apply", "ok", f"Application fully filled — waiting for you to review + submit{secs}.")
        else:
            log_event(url, "apply", "failed", f"Apply failed: {error or 'unknown'}{secs}.")
    except Exception:  # noqa: BLE001
        pass


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# Generous by default: an apply that is genuinely working can take a while, and stealing a
# live lock is worse than showing a stale one for another ten minutes.
STALE_LOCK_MINUTES = int(os.environ.get("APPLY_STALE_MINUTES", "30") or 30)

# How long the agent gets before we give up on it. Was a bare `timeout=300` — but a real
# Deloitte fill already took 208s, so a long application (Google's form is many screens) blew
# past five minutes and the TimeoutExpired path killed Chrome with the form fully filled. The
# cost of waiting is a slow run; the cost of cutting it short is destroying finished work.
AGENT_TIMEOUT_SECONDS = int(os.environ.get("APPLY_AGENT_TIMEOUT", "900") or 900)

#: Co-pilot outcomes where the browser may still hold a COMPLETE, filled application.
#:
#: The result is classified by regex over the agent's final text. "No RESULT line" means the
#: agent stopped without announcing an outcome (turn limit, crash, or just different wording)
#: and "timeout" means we stopped waiting — in BOTH cases the form on screen may be finished.
#: Killing the browser there is what "it fills everything, then closes Chrome" actually was.
#: Co-pilot's whole contract is that a human finishes the job, so an unknown outcome hands over
#: rather than throwing the work away.
_COPILOT_KEEP_OPEN_REASONS = ("no_result_line", "timeout")


def copilot_should_keep_browser(result: str) -> bool:
    """True when a co-pilot run ended ambiguously and the browser may hold real work.

    Deliberately NOT true for the permanent, diagnosed failures (expired, captcha,
    login_issue, not_eligible_*): those are answers, not uncertainty, and leaving a browser
    open for each would block the queue on jobs nobody can finish.
    """
    if not result or not result.startswith("failed:"):
        return False
    return result.split(":", 1)[1].strip().lower() in _COPILOT_KEEP_OPEN_REASONS


def release_stale_locks(max_age_minutes: int | None = None,
                        conn: sqlite3.Connection | None = None) -> list[str]:
    """Release `in_progress` locks that no living worker can still hold.

    `run_dashboard_restart` runs the apply as a SYNCHRONOUS CHILD of the dashboard
    (`subprocess.run`), so killing or restarting the server kills the apply with it — and
    the lock it took at acquisition is never released. The job then reads "in progress"
    forever, and `acquire_job` skips it, so a retry silently does nothing.

    That is exactly what happened on 2026-07-29: two jobs were left locked for 31 and 18
    minutes by a dashboard restart, with no error anywhere and no way to tell from the UI.

    Age-gated rather than "release everything at startup", because `applypilot apply` can
    legitimately be running in a terminal while the dashboard restarts, and stealing that
    job's lock mid-flight would let a second agent start on the same application.
    """
    cutoff_minutes = STALE_LOCK_MINUTES if max_age_minutes is None else max_age_minutes
    if conn is None:
        conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cutoff_minutes)).isoformat()
    rows = conn.execute(
        "SELECT url, title, last_attempted_at FROM jobs "
        "WHERE apply_status = 'in_progress' AND COALESCE(last_attempted_at, '') < ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return []
    urls = [r["url"] for r in rows]
    conn.execute(
        f"UPDATE jobs SET apply_status = NULL, agent_id = NULL "
        f"WHERE url IN ({','.join('?' for _ in urls)})", urls,
    )
    conn.commit()
    for row in rows:
        # Visible in the job's Activity tab — a lock that vanishes with no trace is how
        # this went unnoticed in the first place.
        log_event(row["url"], "apply", "info",
                  "Released a stale in-progress lock (the apply did not finish — most likely "
                  "the dashboard was restarted while it was running). Safe to re-apply.")
    return urls


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            copilot: bool = False, resume: bool = False) -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Reset the worker dir FIRST — build_prompt stages the resume PDF into it, and the
    # agent runs with cwd=worker_dir (Playwright MCP only uploads files from under cwd).
    # Resetting after staging would wipe the resume (file_access_denied on upload).
    worker_dir = reset_worker_dir(worker_id)

    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt (stages the resume PDF into worker_dir)
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        copilot=copilot,
        resume=resume,
        worker_id=worker_id,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    # Build claude command
    cmd = [
        "claude",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        # Use ONLY ApplyPilot's MCP servers (Playwright→real Chrome + Gmail-send).
        # Without this, Claude Code merges the user's GLOBAL MCP servers — e.g. a
        # globally-registered agent-browser (bundled Chromium) — which hijacks the
        # browser and gets fingerprint-blocked (AMD's 403). Also tightens security.
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        # Security: restrict the agent to browser + Gmail-send only. The allow-list
        # scopes capability; the deny-list is a hard backstop (takes precedence even
        # under bypassPermissions) against code exec / filesystem / network tools.
        "--allowedTools", _ALLOWED_TOOLS,
        "--disallowedTools", _DISALLOWED_TOOLS,
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
            # Its OWN process group. `_kill_process_tree` does os.killpg, which without this
            # walks up to the group the AGENT SHARES WITH US and SIGKILLs this process too.
            # Pause hit exactly that: the flag was consumed, the agent was killed, and the CLI
            # died at the same instant (exit -9) before it could mark the job needs_human — so
            # the browser was left open with the row still reading "in progress" and no
            # Continue button. The Ctrl+C skip paths call the same helper and had the same bug.
            start_new_session=(platform.system() != "Windows"),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                # Checked per agent action (each tool call emits a message), so a pause lands
                # within a second or two without polling a timer. Kill only the AGENT — Chrome
                # stays up so the operator inherits everything already typed in.
                if pause.pause_requested():
                    pause.clear_pause()
                    add_event(f"[W{worker_id}] PAUSED by you — browser left open")
                    _kill_process_tree(proc.pid)
                    proc = None
                    return "paused", int((time.time() - start) * 1000)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=AGENT_TIMEOUT_SECONDS)
        returncode = proc.returncode
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        # Co-pilot: the agent fills everything and hands off for human review — it must NEVER
        # submit. Recognize the handoff, and treat an agent self-submit as a violation.
        if copilot:
            # Hard-blocker handoff: agent stopped on a captcha/login/field and left the browser
            # open for the human to resolve + Continue. Reason travels in the result string.
            m_human = re.search(r"RESULT:\s*NEEDS_HUMAN(?::\s*(\w+))?", output, re.IGNORECASE)
            if m_human:
                reason = (m_human.group(1) or "blocker").lower()
                add_event(f"[W{worker_id}] NEEDS YOU ({reason}) ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status="needs_human",
                             last_action=f"needs you: {reason} ({elapsed}s)")
                return f"needs_human:{reason}", duration_ms
            if re.search(r"RESULT:\s*NEEDS_REVIEW\b", output, re.IGNORECASE):
                add_event(f"[W{worker_id}] READY TO REVIEW ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status="needs_review",
                             last_action=f"ready to review ({elapsed}s)")
                return "needs_review", duration_ms
            if re.search(r"RESULT:\s*APPLIED\b", output, re.IGNORECASE):
                if resume:
                    # RESUME means the operator was AT the keyboard: they had just resolved a
                    # blocker (signed in, solved a captcha) and clicked Continue. Very often
                    # they finish and submit it themselves, and the agent then truthfully
                    # reports the application as submitted. Reading that as "the agent
                    # submitted" recorded a real Salesforce application — signed in and
                    # submitted by hand — as `failed:copilot_violation_agent_submitted`.
                    #
                    # Not marked applied outright either, because we genuinely cannot tell WHO
                    # clicked submit. Hand it back for the one-click confirmation that already
                    # exists, which is the human's call to make.
                    add_event(f"[W{worker_id}] Submitted during your session ({elapsed}s) — "
                              f"confirm with 'Mark submitted ✓'")
                    update_state(worker_id, status="needs_review",
                                 last_action=f"submitted in your session ({elapsed}s)")
                    return "needs_review", duration_ms
                # A FRESH co-pilot run submitting on its own is a real safety breach: nobody
                # reviewed it. That stays loud and stays a failure.
                add_event(f"[W{worker_id}] ⚠ CO-PILOT VIOLATION: agent submitted! ({elapsed}s)")
                update_state(worker_id, status="failed",
                             last_action=f"CO-PILOT VIOLATION: submitted ({elapsed}s)")
                return "failed:copilot_violation_agent_submitted", duration_ms

        # Dry-run accounting comes first. A dry run must NEVER be recorded as 'applied'.
        if dry_run:
            if re.search(r"RESULT:\s*DRYRUN\b", output, re.IGNORECASE):
                add_event(f"[W{worker_id}] DRY-RUN OK ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status="dryrun", last_action=f"DRY-RUN ok ({elapsed}s)")
                return "dryrun", duration_ms
            if re.search(r"RESULT:\s*APPLIED\b", output, re.IGNORECASE):
                # The agent SUBMITTED during a dry run — a safety violation. Surface it
                # loudly and do NOT mark the job applied on our side.
                add_event(f"[W{worker_id}] ⚠ DRY-RUN VIOLATION: agent submitted! ({elapsed}s)")
                update_state(worker_id, status="failed",
                             last_action=f"DRY-RUN VIOLATION: submitted ({elapsed}s)")
                return "failed:dryrun_violation_agent_submitted", duration_ms

        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if re.search(rf"RESULT:\s*{result_status}\b", output, re.IGNORECASE):
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms

        if re.search(r"RESULT:\s*FAILED", output, re.IGNORECASE):
            for out_line in output.split("\n"):
                match = re.search(r"RESULT:\s*FAILED(?::\s*(.*))?", out_line, re.IGNORECASE)
                if match:
                    reason = (match.group(1) or "unknown").strip()
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms
            return "failed:unknown", duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                copilot: bool = False, resume: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        # Don't spend a Chrome launch and a full agent run rediscovering a wall we already
        # know about. Salesforce's Workday cost 59 seconds and a `needs_human:login` EVERY
        # time, because the finding was recorded on the job and the next job at the same
        # employer could not read it. `resume` skips this deliberately: the human has just
        # signed in, so the stored answer is the stale one.
        if not resume:
            allowed, realm_id, why = accounts.preflight(job)
            if not allowed:
                add_event(f"[W{worker_id}] {job.get('company') or '?'}: no account for "
                          f"{realm_id} — skipped before launching")
                mark_result(job["url"], "needs_human", error="account")
                log_event(job["url"], "apply", "info", why)
                update_state(worker_id, status="idle", last_action="needs an account")
                jobs_done += 1
                continue

        chrome_proc = None
        try:
            # Resume reconnects to the STILL-OPEN review browser (same CDP port) so a fresh agent
            # can continue from the current on-page state. If the human closed it, fall back to a
            # fresh launch (the application starts over).
            resume_now = resume and chrome_alive_on_port(port)
            if resume_now:
                add_event(f"[W{worker_id}] Resuming in the open browser (port {port})...")
            else:
                if resume:
                    add_event(f"[W{worker_id}] Review browser gone — starting fresh")
                add_event(f"[W{worker_id}] Launching Chrome...")
                chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run, copilot=copilot,
                                            resume=resume_now)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "needs_review":
                # Co-pilot handoff: form is filled, waiting for the human to review + submit.
                # Record the state and KEEP the browser open (skip cleanup) so they can act.
                mark_result(job["url"], "needs_review", duration_ms=duration_ms)
                keep_chrome_alive(worker_id)
                chrome_proc = None  # ensure the finally's cleanup_worker doesn't reap it
                add_event(f"[W{worker_id}] Ready to review: {job['title'][:30]} — submit in the open tab")
                update_state(worker_id, jobs_done=applied + failed)
            elif result.startswith("needs_human"):
                # Hard-blocker handoff: agent stopped on a captcha/login/field. KEEP the browser
                # open on the blocking page so the human resolves it and clicks Continue (resume).
                reason = result.split(":", 1)[-1] if ":" in result else "blocker"
                mark_result(job["url"], "needs_human", error=reason, duration_ms=duration_ms)
                # Teach the realm, so the next job at this employer is decided for free rather
                # than rediscovered at the cost of another full run.
                accounts.note_wall(job, reason)
                keep_chrome_alive(worker_id)
                chrome_proc = None
                add_event(f"[W{worker_id}] Needs you ({reason}): {job['title'][:30]} — resolve in the open tab, then Continue")
                update_state(worker_id, jobs_done=applied + failed)
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            elif result == "paused":
                # The operator asked to take over. Same handover as a co-pilot review, so the
                # dashboard's Continue / Mark-submitted paths work on it unchanged. Applies in
                # non-copilot mode too — an autonomous run is exactly when you most want a
                # stop button that does not throw the work away.
                mark_result(job["url"], "needs_human", error="paused", duration_ms=duration_ms)
                keep_chrome_alive(worker_id)
                chrome_proc = None  # ensure the finally's cleanup_worker doesn't reap it
                add_event(f"[W{worker_id}] Paused — take over in the open tab: {job['title'][:30]}")
                update_state(worker_id, status="needs_human", last_action="paused by you")
                break
            elif copilot and copilot_should_keep_browser(result):
                # The agent went quiet or ran long, but the form on screen may be COMPLETE.
                # Hand it to the human instead of reaping the browser and losing the work.
                reason = result.split(":", 1)[-1]
                mark_result(job["url"], "needs_human", error=reason, duration_ms=duration_ms)
                keep_chrome_alive(worker_id)
                chrome_proc = None  # ensure the finally's cleanup_worker doesn't reap it
                add_event(f"[W{worker_id}] Agent stopped without a verdict ({reason}) — browser "
                          f"LEFT OPEN, check the form: {job['title'][:30]}")
                update_state(worker_id, jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, copilot: bool = False, resume: bool = False,
         continuous: bool = False, poll_interval: int = 60, workers: int = 1) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()
    # A flag left behind by a crash would pause every future apply the instant it
    # started, which is indistinguishable from the apply being broken.
    pause.clear_pause()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    copilot=copilot,
                    resume=resume,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            copilot=copilot,
                            resume=resume,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
