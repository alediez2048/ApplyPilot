"""Local operator dashboard for ApplyPilot.

Runs a small localhost-only HTTP server with:
  - application tracker
  - URL import box
  - prepare/apply buttons
  - live command and apply logs
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from hashlib import sha1
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from rich.console import Console

from applypilot import __version__, config
from applypilot.database import get_connection, init_db
from applypilot.networking import store as _store
from applypilot.networking import touches as _touches
from applypilot.repo import jobs as _jobs

console = Console()

_URL_RE = re.compile(r"https?://[^\s,<>\"']+")
# Owned by repo.jobs now — the definition of "a job the operator pasted in" is a data
# rule, not a view concern. Re-exported because tests and the extension import them.
_URL_QUEUE_STRATEGIES = _jobs.QUEUE_STRATEGIES
_URL_QUEUE_SQL = _jobs.QUEUE_SQL

# ── Extension local API (EXT-0) — frozen contract in extension/CONTRACTS.md §3.
# Paths / header / limits mirror extension/shared/constants.js (API.*, NOTE_MAX_LEN).
EXT_TOKEN_HEADER = "X-ApplyPilot-Token"
EXT_QUEUE_PATH = "/api/ext/queue"
EXT_STATUS_PATH = "/api/ext/status"
EXT_NOTE_PATH = "/api/ext/note"
EXT_NOTE_MAX_LEN = 300
# The only dm_status values the extension may POST to /api/ext/status.
_POSTABLE_DM_STATUSES = frozenset({"sent", "manual", "skipped"})


def _infer_company(url: str) -> str:
    """Employer name for a pasted job URL.

    The board/ATS path-slug rules live in networking.derive so contact discovery and
    URL import agree on who the employer is (a YC listing is the startup, not YC).
    """
    from applypilot.networking import derive as _derive

    # Delegate wholesale. This used to re-implement the host handling as `domain[-2]` with a
    # 'careers'/'jobs' special case, which is the REGISTRABLE label — so an ATS tenant URL like
    # salesforce.wd12.myworkdayjobs.com imported as "Myworkdayjobs". The job was then titled,
    # tailored and cover-lettered against the ATS instead of Salesforce. derive already walks
    # ATS path slugs and host labels correctly; two implementations of "who is the employer"
    # is exactly what the docstring above promises not to have.
    return _derive.derive_company({"url": url, "application_url": url}) or "Uploaded"


class CommandRunner:
    """Tracks one active background ApplyPilot command."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.name: str = ""
        self.started_at: float = 0.0
        self.finished_at: float | None = None
        self.returncode: int | None = None
        self.lines: list[str] = []
        self.max_lines = 500

    def status(self) -> dict:
        with self._lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "running": running,
                "name": self.name,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "returncode": self.returncode,
                "log": self.lines[-200:],
            }

    def start(self, name: str, args: list[str]) -> tuple[bool, str]:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return False, f"Command already running: {self.name}"

            self.name = name
            self.started_at = time.time()
            self.finished_at = None
            self.returncode = None
            self.lines = [f"$ {' '.join(args)}"]

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"

            self.process = subprocess.Popen(
                args,
                cwd=str(Path.cwd()),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=(os.name != "nt"),
            )

            threading.Thread(target=self._read_output, daemon=True).start()
            return True, "started"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            proc = self.process
            if proc is None or proc.poll() is not None:
                return False, "No command is running"
            self.lines.append("Stopping command...")

        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        return True, "stopping"

    def _read_output(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                with self._lock:
                    self.lines.append(line.rstrip())
                    if len(self.lines) > self.max_lines:
                        self.lines = self.lines[-self.max_lines:]
        finally:
            rc = proc.wait()
            with self._lock:
                self.returncode = rc
                self.finished_at = time.time()
                self.lines.append(f"Command exited with code {rc}")


_runner = CommandRunner()


class NetworkRunner:
    """Keyed in-process registry for 'Find contacts' runs (one task per job_url).

    Networking is in-process Python (no subprocess), so it runs concurrently with
    prepare/apply and with other jobs' finds — unlike the single CommandRunner.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}  # job_url -> {running, note, error, finished_at}

    def is_running(self, job_url: str) -> bool:
        with self._lock:
            t = self._tasks.get(job_url)
            return bool(t and t.get("running"))

    def statuses(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._tasks.items()}

    def start(self, job_url: str, per_job: int, use_linkedin: bool) -> tuple[bool, str]:
        with self._lock:
            if self._tasks.get(job_url, {}).get("running"):
                return False, "already finding contacts for this job"
            self._tasks[job_url] = {"running": True, "note": "searching…", "error": "",
                                    "finished_at": None}
        threading.Thread(
            target=self._run, args=(job_url, per_job, use_linkedin), daemon=True
        ).start()
        return True, "started"

    def _run(self, job_url: str, per_job: int, use_linkedin: bool) -> None:
        note, error = "done", ""
        try:
            from applypilot.config import require_contacts_provider
            from applypilot.database import get_connection
            from applypilot.networking import service
            from applypilot.networking.store import init_contacts

            # Provider gate (raises SystemExit if unusable) — convert to a task error.
            try:
                require_contacts_provider("networking")
            except SystemExit:
                raise RuntimeError("No usable contact provider (set APOLLO_API_KEY, paid plan)")

            conn = get_connection()
            init_contacts(conn)
            row = _jobs.find_by_any_url(job_url, conn)
            if not row:
                raise RuntimeError("job not found")
            # `dict(row)`, NOT dict(zip(row.keys(), row)) — find_by_any_url already returns a
            # dict, and zipping a dict against its own keys maps every key to ITSELF. This
            # search was running on {"company": "company", "url": "url", ...}, so the dashboard
            # button searched Apollo for a company literally named "company" and verification
            # dropped everything it found. The CLI path passed a real row and worked, which is
            # why it looked like a coverage problem.
            job = dict(row)
            res = service.find_contacts_for_job(job, per_job=per_job, use_linkedin=use_linkedin)
            note = f"{res['found']} found, {res['revealed']} with email ({res['note']})"
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            note = "error"
        with self._lock:
            self._tasks[job_url] = {"running": False, "note": note, "error": error,
                                    "finished_at": time.time()}


_network = NetworkRunner()




class BulkEmailRunner:
    """Background sender for 'Send all emails' (Gmail, no browser). Keyed by job_url."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}  # job_url -> {running, total, sent, skipped, note}

    def status(self, job_url: str) -> dict:
        with self._lock:
            return dict(self._jobs.get(job_url, {}))

    def start(self, job_url: str, contact_ids: list[str], confirm_unverified: bool) -> tuple[bool, str]:
        ids = [c for c in contact_ids if c]
        if not ids:
            return False, "no emails ready to send"
        with self._lock:
            if self._jobs.get(job_url, {}).get("running"):
                return False, "a bulk email send is already running for this job"
            self._jobs[job_url] = {"running": True, "total": len(ids), "sent": 0,
                                   "skipped": 0, "note": "sending…"}
        threading.Thread(target=self._run, args=(job_url, ids, confirm_unverified),
                         daemon=True).start()
        return True, f"sending {len(ids)} email{'s' if len(ids) != 1 else ''}"

    def _run(self, job_url: str, contact_ids: list[str], confirm_unverified: bool) -> None:
        from applypilot.networking.gmail_send import send_outreach
        sent = skipped = 0
        for cid in contact_ids:
            try:
                res = send_outreach(cid, confirm_unverified=confirm_unverified)
                if res.get("ok"):
                    sent += 1
                else:
                    skipped += 1
            except Exception:  # noqa: BLE001
                skipped += 1
            with self._lock:
                self._jobs[job_url].update(sent=sent, skipped=skipped,
                                           note=f"{sent} sent, {skipped} skipped")
        with self._lock:
            self._jobs[job_url].update(running=False,
                                       note=f"done — {sent} sent, {skipped} skipped")


_bulk_email = BulkEmailRunner()



def _eligible_contact_ids(job_url: str, channel: str, confirm_unverified: bool = False) -> list[str]:
    """Contact ids for a job that are ready to send on the given channel ('email'|'linkedin')."""
    from applypilot.networking.store import get_contacts_for_job
    ids = []
    for c in get_contacts_for_job(job_url):
        if channel == "email":
            if not (c.get("email") and c.get("outreach_message")):
                continue
            # Already emailed = Gmail returned a message id (survives a draft regenerate that
            # resets outreach_status), or the status is explicitly submitted.
            if (c.get("sent_message_id") or "").strip() or c.get("outreach_status") == "submitted":
                continue
            if not confirm_unverified and (c.get("email_status") or "none") != "verified":
                continue  # skip unverified unless the caller opts in
        else:  # linkedin
            if not (c.get("linkedin_url") and c.get("linkedin_message")):
                continue
            if c.get("dm_status") in _EXT_QUEUE_EXCLUDE:
                continue  # only sent/manual are finished — skipped keeps re-appearing
        ids.append(c.get("id"))
    return [i for i in ids if i]


def _host_is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    """True if the request's Host header is a loopback address (DNS-rebinding guard)."""
    hosthdr = (handler.headers.get("Host") or "").split(":")[0]
    return hosthdr in ("127.0.0.1", "localhost", "::1", "")


def _origin_ok(handler: BaseHTTPRequestHandler) -> bool:
    """Reject cross-origin state-changing POSTs (DNS-rebinding guard on localhost)."""
    origin = handler.headers.get("Origin")
    if origin:
        host = urlparse(origin).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
    # Host header must also be a loopback address:port
    return _host_is_loopback(handler)


def _ext_origin_ok(handler: BaseHTTPRequestHandler) -> bool:
    """Origin guard for extension POSTs: loopback OR the chrome-extension scheme.

    Extension identity is proven by the shared token (verified separately), not by a
    hardcoded chrome-extension://<id> (unstable for load-unpacked). We accept the scheme
    so the extension's own Origin passes; a browser page on a non-loopback site is still
    rejected. A missing Origin (non-browser client) is allowed — the token still gates it.
    """
    origin = handler.headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme == "chrome-extension":
            return True
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
    return True


def _ext_token() -> str:
    """Read (or first-run generate) the mutual shared token at ~/.applypilot/ext_token.

    The extension sends it on every /api/ext/* request; the server rejects a wrong/missing
    token. Written 0600. Referenced via config.APP_DIR at call time (respects APPLYPILOT_DIR).
    """
    path = config.APP_DIR / "ext_token"
    try:
        if path.exists():
            tok = path.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    path.write_text(tok, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


def _ext_token_ok(handler: BaseHTTPRequestHandler) -> bool:
    """Constant-time compare of the request's token header against the stored token."""
    provided = handler.headers.get(EXT_TOKEN_HEADER, "") or ""
    return bool(provided) and hmac.compare_digest(provided, _ext_token())


def _rows_to_dicts(rows: list) -> list[dict]:
    if rows and not isinstance(rows[0], dict):
        return [dict(zip(row.keys(), row)) for row in rows]
    return rows


def _safe_material_prefix(job: dict) -> str:
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title") or "uploaded_job")[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job.get("site") or "Uploaded")[:20].strip().replace(" ", "_")
    digest = sha1((job.get("url") or "").encode("utf-8")).hexdigest()[:8]
    return f"{safe_site}_{safe_title}_{digest}"


def run_dashboard_prepare(limit: int = 0, validation_mode: str = "normal") -> dict:
    """Prepare materials only for URLs imported through the dashboard.

    Imported URLs are treated as user-approved targets. We intentionally bypass
    broad discovery and fit scoring here so the dashboard cannot spend time or
    tokens on older researched jobs.
    """
    config.load_env()
    config.ensure_dirs()
    init_db()
    conn = get_connection()
    from applypilot.database import log_event

    pending_detail = _jobs.queue_needing_detail(limit, conn)

    enriched = 0
    detail_errors = 0
    if pending_detail:
        from applypilot.enrichment.detail import scrape_site_batch

        by_site: dict[str, list[tuple[str, str]]] = {}
        for row in pending_detail:
            by_site.setdefault(row["site"] or "Uploaded", []).append((row["url"], row["title"] or "Uploaded job"))

        for site, jobs in by_site.items():
            print(f"STAGE: enrich dashboard URLs - {site} ({len(jobs)})", flush=True)
            stats = scrape_site_batch(conn, site, jobs, delay=1.0)
            enriched += int(stats.get("ok", 0)) + int(stats.get("partial", 0))
            detail_errors += int(stats.get("error", 0))
            # Per-job enrich outcome from the row's detail_error/full_description after the batch.
            for (jurl, _t) in jobs:
                r = _jobs.detail_outcome(jurl, conn)
                if r and r["detail_error"]:
                    log_event(jurl, "enrich", "failed", f"Could not read the job page: {r['detail_error']}", conn)
                elif r and r["full_description"]:
                    log_event(jurl, "enrich", "ok", "Read the full job description.", conn)

    scored = _jobs.bypass_scoring(conn)
    print(f"STAGE: score bypass - marked {scored} imported URL(s) as user-approved", flush=True)

    profile = None
    resume_text = None
    tailored = 0
    tailor_errors = 0

    tailor_jobs = _jobs.queue_for_tailor(limit, conn=conn)

    if tailor_jobs:
        from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
        from applypilot.scoring.tailor import tailor_resume

        profile = load_profile()
        resume_text = RESUME_PATH.read_text(encoding="utf-8")
        TAILORED_DIR.mkdir(parents=True, exist_ok=True)
        print(f"STAGE: tailor dashboard URLs ({len(tailor_jobs)})", flush=True)

        for index, job in enumerate(tailor_jobs, 1):
            print(f"[{index}/{len(tailor_jobs)}] tailoring {job.get('site')} - {job.get('title')}", flush=True)
            try:
                tailored_text, report = tailor_resume(resume_text, job, profile, validation_mode=validation_mode)
                prefix = _safe_material_prefix(job)
                txt_path = TAILORED_DIR / f"{prefix}.txt"
                txt_path.write_text(tailored_text, encoding="utf-8")

                # Structured JSON sidecar so the React-PDF renderer uses the
                # clean structured path (matches `applypilot run tailor`).
                resume_data = report.pop("resume_data", None)
                if resume_data is not None:
                    (TAILORED_DIR / f"{prefix}_DATA.json").write_text(
                        json.dumps(resume_data, indent=2), encoding="utf-8")
                (TAILORED_DIR / f"{prefix}_JOB.txt").write_text(
                    (
                        f"Title: {job.get('title')}\n"
                        f"Company: {job.get('site')}\n"
                        f"Location: {job.get('location') or 'N/A'}\n"
                        f"URL: {job.get('url')}\n\n"
                        f"{job.get('full_description') or ''}"
                    ),
                    encoding="utf-8",
                )
                (TAILORED_DIR / f"{prefix}_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    convert_to_pdf(txt_path)
                except Exception as exc:
                    print(f"  PDF warning: {exc}", flush=True)

                # Accept the resume if it was approved OR we're in lenient/aggressive mode (the
                # user's explicit choice). A non-blocking validation note (e.g. an old role
                # dropped to fit one page) must NOT discard a usable, already-rendered resume —
                # that's what left "the full app didn't go through" with no materials.
                accepted = (
                    report.get("status") in {"approved", "approved_with_judge_warning"}
                    or (validation_mode == "lenient" and tailored_text.strip())
                )
                if accepted:
                    _jobs.set_tailored(job["url"], str(txt_path), conn)
                    tailored += 1
                    note = "" if report.get("status") in {"approved", "approved_with_judge_warning"} else f" ({report.get('status')})"
                    log_event(job["url"], "tailor", "ok", f"Tailored résumé generated{note}.", conn)
                    # Validator warnings used to land ONLY in {prefix}_REPORT.json, which
                    # nothing reads (CLAUDE.md §Dev workflow). A dropped tool or a banned
                    # word is worth seeing before the résumé is submitted, not after.
                    warnings = (report.get("validator") or {}).get("warnings") or []
                    for w in warnings[:4]:
                        log_event(job["url"], "tailor", "info", f"Résumé note: {w}", conn)
                        print(f"  résumé note: {w}", flush=True)
                    if report.get("status") not in {"approved", "approved_with_judge_warning"}:
                        print(f"  tailor accepted with note (lenient): {report.get('status')}", flush=True)
                else:
                    _jobs.bump_tailor_attempts(job["url"], conn)
                    tailor_errors += 1
                    log_event(job["url"], "tailor", "failed", f"Résumé failed validation ({report.get('status')}).", conn)
                conn.commit()
            except Exception as exc:
                _jobs.bump_tailor_attempts(job["url"], conn)
                tailor_errors += 1
                log_event(job["url"], "tailor", "failed", f"Error tailoring résumé: {str(exc)[:200]}", conn)
                print(f"  tailor error: {exc}", flush=True)

    cover_jobs = _jobs.queue_for_cover(limit, conn=conn)

    covers = 0
    cover_errors = 0
    if cover_jobs:
        from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
        from applypilot.scoring.cover_letter import generate_cover_letter

        profile = profile or load_profile()
        resume_text = resume_text or RESUME_PATH.read_text(encoding="utf-8")
        COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
        print(f"STAGE: cover letters for dashboard URLs ({len(cover_jobs)})", flush=True)

        for index, job in enumerate(cover_jobs, 1):
            print(f"[{index}/{len(cover_jobs)}] cover letter {job.get('site')} - {job.get('title')}", flush=True)
            try:
                letter = generate_cover_letter(resume_text, job, profile, validation_mode=validation_mode)
                cl_path = COVER_LETTER_DIR / f"{_safe_material_prefix(job)}_CL.txt"
                cl_path.write_text(letter, encoding="utf-8")
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    convert_to_pdf(cl_path, kind="cover_letter")
                except Exception as exc:
                    print(f"  PDF warning: {exc}", flush=True)
                _jobs.set_cover(job["url"], str(cl_path), conn)
                covers += 1
                log_event(job["url"], "cover", "ok", "Cover letter generated.", conn)
                # `generate_cover_letter` returns only the text — its report never reaches
                # here, so a banned word or an unnamed employer was invisible even though
                # the validator had found it. Re-check the finished letter to surface notes.
                try:
                    from applypilot.scoring.validator import validate_cover_letter
                    notes = validate_cover_letter(letter, mode="normal",
                                                  company=job.get("site", ""))
                    for w in (notes.get("warnings") or [])[:3] + (notes.get("errors") or [])[:3]:
                        log_event(job["url"], "cover", "info", f"Cover letter note: {w}", conn)
                        print(f"  cover letter note: {w}", flush=True)
                except Exception:  # noqa: BLE001 - a reporting nicety must never fail prep
                    pass
            except Exception as exc:
                _jobs.bump_cover_attempts(job["url"], conn)
                cover_errors += 1
                log_event(job["url"], "cover", "failed", f"Error generating cover letter: {str(exc)[:200]}", conn)
                print(f"  cover error: {exc}", flush=True)

    result = {
        "enriched": enriched,
        "detail_errors": detail_errors,
        "score_bypassed": scored,
        "tailored": tailored,
        "tailor_errors": tailor_errors,
        "covers": covers,
        "cover_errors": cover_errors,
    }
    print(f"Dashboard URL prepare complete: {result}", flush=True)
    return result


def run_dashboard_apply(limit: int = 10, dry_run: bool = False, copilot: bool = True) -> dict:
    """Apply only to prepared jobs imported through the dashboard URL box.

    copilot=True (default): the agent fills each application but STOPS before submit and leaves
    the browser open for the human to review + submit. dry_run takes precedence if both set.
    """
    config.load_env()
    config.ensure_dirs()
    init_db()
    conn = get_connection()
    # Eligible = prepared, not yet applied, and either never-attempted OR a prior FAILURE that's
    # still under the retry cap. Including 'failed' (under-cap) means a botched/interrupted apply
    # (e.g. the user closed the tab, or a blocking field) can just be re-run from the dashboard —
    # no manual DB reset. 'in_progress' and cap-exhausted jobs are left alone.
    from applypilot.database import log_event
    max_attempts = config.DEFAULTS["max_apply_attempts"]
    rows = _jobs.queue_for_apply(limit, max_attempts, conn)

    # Refuse to start at all while a co-pilot review is already open. The queue below is
    # blind to it (`queue_for_apply` filters on the JOB, not on whether a browser is being
    # used), so without this a fresh apply silently closes the form you were mid-review on.
    stale_note = ""
    if copilot and not dry_run:
        awaiting = _jobs.awaiting_human(conn)
        # ...but only while the browser is REALLY still open. `apply_status` outlives the
        # process that set it: an apply runs as a synchronous child of this server, so
        # restarting the dashboard kills the browser and leaves the row saying a form is
        # waiting. Those fossils blocked a brand-new application with three reviews that no
        # longer existed, one of them from the previous day. Liveness — not a timeout — is the
        # discriminator, because it IS the question the guard cares about: would starting an
        # apply close a window someone needs?
        if awaiting and not _review_browser_alive():
            names = ", ".join((r["title"] or "?")[:28] for r in awaiting[:3])
            stale_note = (f"Ignored {len(awaiting)} abandoned review(s) ({names}) — the browser "
                          f"is gone, so nothing was waiting. Re-apply to fill them again.")
            print(f"NOTE: {stale_note}", flush=True)
            for r in awaiting:
                log_event(r["url"], "apply", "info",
                          "Review browser is gone (dashboard restarted) — this application was "
                          "never submitted. Re-apply to fill it again.", conn)
            awaiting = []
        if awaiting:
            names = ", ".join((r["title"] or "?")[:28] for r in awaiting[:3])
            msg = (f"{len(awaiting)} application(s) are filled and waiting for you ({names}). "
                   f"Starting another would close the browser you need. Submit or dismiss "
                   f"them first.")
            print(f"BLOCKED: {msg}", flush=True)
            return {"queued": 0, "applied": 0, "failed": 0, "needs_review": len(awaiting),
                    "held_back": len(rows), "blocked": msg}

    print(f"Dashboard URL apply queue: {len(rows)} job(s)", flush=True)
    applied = 0
    failed = 0
    needs_review = 0
    for index, row in enumerate(rows, 1):
        print(f"\n=== Applying {index}/{len(rows)}: {row['site']} / {row['title']} ===", flush=True)
        print(row["url"], flush=True)
        args = [sys.executable, "-m", "applypilot.cli", "apply", "--url", row["url"], "--min-score", "1"]
        if dry_run:
            args.append("--dry-run")
        elif copilot:
            args.append("--copilot")
        completed = subprocess.run(args, check=False)
        status = _jobs.apply_state(row["url"], conn)
        if status and status["applied_at"]:
            applied += 1
        elif status and status["apply_status"] == "ready_to_submit":
            needs_review += 1  # co-pilot handoff: filled + waiting for the human to submit
        else:
            failed += 1
        print(f"=== Finished {index}/{len(rows)} with exit code {completed.returncode} ===", flush=True)

        # STOP the batch the moment a job needs the human. Co-pilot mode ends by asking you
        # to review and submit in an open browser — and starting the next apply KILLS that
        # browser, because launch clears whatever holds the CDP port.
        #
        # 2026-07-29: a Zello application was filled correctly in 78s and handed over for
        # review; the next queued job (Deloitte) started 428ms later and destroyed the
        # browser. The row still read `ready_to_submit`, so the status claimed a form was
        # waiting that no longer existed. Batching N jobs in co-pilot mode leaves every one
        # of them un-reviewable except the last.
        #
        # Real fix is sequencing, not a bigger warning: one pending review at a time.
        pending = status and status["apply_status"] in ("ready_to_submit", "needs_human")
        if copilot and not dry_run and pending and index < len(rows):
            remaining = len(rows) - index
            print(f"=== PAUSED: {row['site']} is filled and waiting for your review. "
                  f"{remaining} job(s) left in the queue — they will NOT start until you "
                  f"submit or dismiss this one, because starting one would close the browser "
                  f"you need. ===", flush=True)
            log_event(row["url"], "apply", "info",
                      f"Queue paused here: {remaining} job(s) held back so this review "
                      f"stays open. Submit or dismiss, then run apply again.", conn)
            break

    result = {"queued": len(rows), "applied": applied, "failed": failed,
              "needs_review": needs_review,
              "held_back": max(0, len(rows) - index) if rows else 0}
    if stale_note:
        result["stale_reviews_note"] = stale_note
    print(f"Dashboard URL apply complete: {result}", flush=True)
    return result


#: job_url -> when its "Sign in first" window was opened. In-process only: a browser that
#: outlives this dashboard is detected by probing the CDP port, never by trusting this dict.
_SIGNIN_OPEN: dict[str, float] = {}


def _signin_state(job_url: str) -> bool:
    """Is a sign-in window open for this job RIGHT NOW?

    Both halves matter. The dict alone would keep claiming a window the operator closed; the
    port alone cannot tell a sign-in window from an agent's review browser.
    """
    return bool(_SIGNIN_OPEN.get(job_url)) and _review_browser_alive()


def _start_signin(job_url: str) -> dict:
    """Open the apply profile's Chrome on this job, with NO agent attached.

    Registration walls (Deloitte, Workday, Salesforce) need a human: an account, a password, a
    verification code. The agent cannot and should not do that. But the profile it drives is
    persistent — 830 cookie hosts and counting — so signing in ONCE makes every later
    application to that employer already authenticated.

    The value is in the timing. Doing it here costs a minute before anything starts; doing it
    mid-run means an agent burning time on a wall it can never pass.
    """
    from applypilot.apply.chrome import BASE_CDP_PORT, launch_chrome
    from applypilot.database import log_event

    init_db()
    conn = get_connection()
    row = _jobs.find_by_any_url(job_url, conn)
    if not row:
        return {"ok": False, "message": "job not found"}
    job = dict(row)

    # Chrome refuses to run two instances on one user-data-dir, and the apply profile is that
    # dir — so a sign-in window and a running apply cannot coexist. Refuse rather than let
    # launch_chrome's _kill_on_port silently reap whatever is there.
    running = _jobs.in_progress(conn)
    if running:
        names = ", ".join((r["title"] or "?")[:28] for r in running[:3])
        return {"ok": False, "message": f"{names} is being filled right now. Pause it first — "
                                        f"sign-in uses the same Chrome profile."}
    if _review_browser_alive() and not _SIGNIN_OPEN.get(job_url):
        return {"ok": False, "message": "A browser is already open for a review. Finish or "
                                        "dismiss it first — sign-in uses the same profile."}

    target = job.get("application_url") or job.get("url")
    try:
        launch_chrome(0, port=BASE_CDP_PORT, url=target, human=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Could not open Chrome: {exc}"}

    _SIGNIN_OPEN[job_url] = time.time()
    log_event(job_url, "apply", "info",
              "Opened the application in Chrome for you to register / sign in. The session is "
              "saved in the apply profile, so later applications to this employer skip it.",
              conn)
    return {"ok": True, "message": "Chrome is open. Register or sign in, then choose "
                                   "“Fill it now” or “Done for now”."}


def _finish_signin(job_url: str, fill: bool = False) -> dict:
    """Close the sign-in window, or hand it straight to the agent.

    "Fill it now" deliberately does NOT close and relaunch: the apply's resume path reconnects
    to a live browser on the same port (`resume_now = resume and chrome_alive_on_port(port)`),
    so the agent inherits the session that was just created instead of meeting the wall again.
    """
    from applypilot.apply.chrome import BASE_CDP_PORT, _kill_on_port
    from applypilot.database import log_event

    _SIGNIN_OPEN.pop(job_url, None)
    if fill:
        ok, msg = _start_continue(job_url)
        if ok:
            log_event(job_url, "apply", "info", "Signed in — handing the open browser to the agent.")
        return {"ok": ok, "message": msg if not ok else
                "Filling in the browser you just signed in to."}
    _kill_on_port(BASE_CDP_PORT)
    log_event(job_url, "apply", "info",
              "Sign-in window closed. The session is saved for later applications.")
    return {"ok": True, "message": "Closed. You are signed in for next time."}


log = logging.getLogger(__name__)

#: Background reply polling. Gmail is the slow part, so it never runs inside a request — a
#: 2.5s dashboard refresh cannot wait on a mailbox round-trip.
class ReplyPoller:
    """Polls Gmail for replies on a timer while the dashboard is up.

    Unattended polling with the dashboard CLOSED is CRM-3b (`applypilot tick`), deliberately not
    this. The point of keeping it in-process here is that it costs nothing when idle: with a
    watermark, a poll that finds no new mail is one `history.list` call and reads no threads.
    """

    def __init__(self, interval_s: int = 300) -> None:
        self._lock = threading.Lock()
        self._interval = interval_s
        self.interval_s = interval_s
        self._last: dict = {}
        self._running = False

    def status(self) -> dict:
        with self._lock:
            return dict(self._last)

    def poll_now(self, force_full: bool = False) -> dict:
        from applypilot.networking import replies as reply_svc
        with self._lock:
            if self._running:
                return {"ok": False, "note": "a poll is already running"}
            self._running = True
        try:
            res = reply_svc.poll(force_full=force_full)
        except Exception as exc:  # noqa: BLE001
            # Reply detection must never take the dashboard down with it.
            log.debug("Reply poll failed", exc_info=True)
            res = {"ok": False, "note": f"poll failed: {exc}", "checked": 0, "replied": 0}
        # Bookings and deck clicks ride the SAME timer. They were built for `applypilot tick`,
        # which is not installed on this machine (`schedule.installed()` is False) — so the two
        # "automatic" signals were, in practice, never running at all, and the manual buttons
        # beside them were the only thing that worked. A feature that only fires from a
        # scheduler nobody installed is a feature that does not exist.
        #
        # Each is isolated: a dead cal.com search must not stop reply detection, which is the
        # one people notice.
        for name, fn in (("bookings", self._poll_bookings), ("deck", self._poll_deck)):
            try:
                res[name] = fn()
            except Exception:  # noqa: BLE001
                log.debug("%s poll failed", name, exc_info=True)
        with self._lock:
            self._running = False
            self._last = {**res, "at": time.time()}
        return res

    @staticmethod
    def _poll_bookings() -> dict:
        """Detected from the scheduler's confirmation email — cal.com mails the host."""
        from applypilot.networking import bookings, gmail_read
        ok, _ = gmail_read.can_read_content()
        if not ok:
            return {"skipped": True}
        r = bookings.poll()
        return {"found": r.get("found", 0), "new": r.get("new", 0)}

    @staticmethod
    def _poll_deck() -> dict:
        """Pulled from the collector on the sender's own site, when one is configured."""
        from applypilot.networking import deck_hits
        ok, _ = deck_hits.configured()
        if not ok:
            return {"skipped": True}
        r = deck_hits.poll()
        return {"recorded": r.get("recorded", 0), "new": r.get("new", 0)}

    def start(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.poll_now()
                except Exception:  # noqa: BLE001
                    log.debug("Reply poll loop error", exc_info=True)
                time.sleep(self._interval)
        threading.Thread(target=loop, name="signal-poller", daemon=True).start()


_replies = ReplyPoller()


def _pause_apply() -> dict:
    """Ask the running apply to stop and hand its browser over.

    Distinct from `/api/stop`, which `killpg`s the process group — that signal reaches Chrome
    too and destroys the half-filled form. Pause stops only the agent and leaves the browser on
    screen as `needs_human:paused`, so the operator finishes the application themselves.
    """
    from applypilot.apply import pause

    init_db()
    conn = get_connection()
    running = _jobs.in_progress(conn)
    if not running:
        return {"ok": False, "message": "No application is being filled right now."}
    pause.request_pause()
    names = ", ".join((r["title"] or "?")[:28] for r in running[:3])
    return {"ok": True, "message": f"Pausing {names} — the browser stays open for you to finish. "
                                   f"It stops at the agent's next step."}


def _review_browser_alive(max_workers: int = 4) -> bool:
    """Is a co-pilot review browser actually still open on any worker's CDP port?

    `apply_status` says a form is waiting; only this says the window still exists. Probed
    rather than remembered: `chrome._keep_alive_ports` is per-process state and is empty in
    every new process, which is precisely why a restart turned pending reviews into fossils.

    Any live port blocks — the port is not recorded per job, so a live browser could belong to
    any pending row and closing the wrong one is the failure being prevented.
    """
    from applypilot.apply.chrome import BASE_CDP_PORT, chrome_alive_on_port
    return any(chrome_alive_on_port(BASE_CDP_PORT + w, timeout=0.5)
               for w in range(max(1, max_workers)))


def run_dashboard_fill_one(url: str) -> dict:
    """Co-pilot fill ONE specific job (the per-row "Fill application" action).

    Runs the co-pilot apply for a single prepared job: opens Chrome, fills the whole
    application, and stops for the human to review + submit. Same as the bulk fill but scoped
    to one URL so a row's own button drives it.
    """
    config.load_env()
    config.ensure_dirs()
    print(f"Dashboard fill-one (co-pilot) for: {url}", flush=True)
    args = [sys.executable, "-m", "applypilot.cli", "apply", "--url", url,
            "--min-score", "1", "--copilot"]
    completed = subprocess.run(args, check=False)
    init_db()
    conn = get_connection()
    result = {"url": url, "status": _jobs.apply_status(url, conn),
              "exit_code": completed.returncode}
    print(f"Dashboard fill-one complete: {result}", flush=True)
    return result


def run_dashboard_restart(url: str) -> dict:
    """Restart a job end-to-end: fix any missing materials, then co-pilot apply.

    For applications that didn't go through (failed, stuck, or partially prepared — e.g. a résumé
    but no cover letter). Unlike Fill-application (apply only), this first ensures the full
    material set exists, then applies. Steps:
      1. Clear the apply state (status/error/attempts) so it's a clean retry.
      2. Run prepare — regenerates only what's MISSING (enrich → tailor → cover) for this job.
      3. Co-pilot apply the job (fill in Chrome, hand off for review + submit).
    """
    config.load_env()
    config.ensure_dirs()
    init_db()
    conn = get_connection()
    from applypilot.database import log_event

    print(f"Dashboard RESTART (end-to-end) for: {url}", flush=True)
    log_event(url, "system", "info", "Restarted end-to-end (fix materials → apply).", conn)

    # 1) Clean apply slate so nothing blocks a fresh attempt. Restart is an explicit, confirmed
    #    user action (the UI double-confirms for already-applied jobs), so we DO clear applied_at
    #    here — that's the whole point: re-apply something that didn't truly go through.
    _jobs.reset_apply_state(url, conn)

    # 2) Ensure materials — prepare regenerates only what's missing (idempotent for complete jobs).
    row = _jobs.materials_present(url, conn)
    if row and not (row["enr"] and row["res"] and row["cov"]):
        print("STAGE: restart — regenerating missing materials", flush=True)
        run_dashboard_prepare(validation_mode="normal")

    # 3) Co-pilot apply this job.
    args = [sys.executable, "-m", "applypilot.cli", "apply", "--url", url,
            "--min-score", "1", "--copilot"]
    completed = subprocess.run(args, check=False)
    conn = get_connection()
    result = {"url": url, "status": _jobs.apply_status(url, conn), "exit_code": completed.returncode}
    print(f"Dashboard restart complete: {result}", flush=True)
    return result


def run_dashboard_continue(url: str) -> dict:
    """Resume a co-pilot job that paused on a hard blocker (captcha/login/field).

    Spawns a fresh agent with --resume: it reconnects to the still-open review browser (same CDP
    port) and continues from the current on-page state now that the human has resolved the blocker.
    """
    config.load_env()
    config.ensure_dirs()
    print(f"Dashboard continue (resume) for: {url}", flush=True)
    args = [sys.executable, "-m", "applypilot.cli", "apply", "--url", url,
            "--min-score", "1", "--copilot", "--resume"]
    completed = subprocess.run(args, check=False)
    init_db()
    conn = get_connection()
    result = {"url": url, "status": _jobs.apply_status(url, conn),
              "exit_code": completed.returncode}
    print(f"Dashboard continue complete: {result}", flush=True)
    return result


def _mark_submitted(url: str) -> dict:
    """Confirm a co-pilot 'ready_to_submit' job as applied after the human submitted it by hand.

    Only transitions a job that's actually in the ready_to_submit state (guards against
    marking something applied that never went through review).
    """
    from applypilot.database import log_event
    if not url:
        return {"ok": False, "message": "url required"}
    init_db()
    conn = get_connection()
    if not _jobs.exists(url, conn):
        return {"ok": False, "message": "job not found"}
    status = _jobs.apply_status(url, conn)
    # The guard exists so the button cannot rubber-stamp a job that was never filled. It used
    # to require exactly `ready_to_submit`, which was too narrow: a real Salesforce application
    # — signed in and submitted by hand — was classified `failed` by the agent, and this
    # endpoint then refused the operator's correction, leaving a successful application
    # recorded as a failure with no way to fix it from the UI.
    #
    # The operator saying "I submitted this" is a statement of fact about the outside world,
    # and they are the authority on it. What they must NOT be able to do is bless a job the
    # app never even attempted, so the gate is now "has this been attempted", not "is it in
    # one exact state".
    if status == "applied":
        return {"ok": False, "message": "already marked as submitted"}
    # Checked for EVERY status, not just the empty ones. Gating on the status name let a job
    # sitting at 'ready' — prepared but never opened by an agent — be marked applied, because
    # 'ready' is not in any "suspicious" list. What matters is whether it was ever attempted.
    if not _jobs.was_attempted(url, conn):
        return {"ok": False, "message": "not awaiting review — this application was never "
                                        "filled, so there is nothing to confirm"}
    _jobs.mark_applied(url, conn)
    note = "" if status == "ready_to_submit" else f" (was: {status or 'not attempted'})"
    log_event(url, "apply", "ok", f"You confirmed this was submitted{note}.", conn)
    return {"ok": True, "message": "Marked as submitted ✓"}


def _mark_rejected(url: str) -> dict:
    """Move a job to the rejected pile (apply_status='rejected' + rejected_at). Keeps applied_at
    so the record that you DID apply survives. Logs a rejection to the activity timeline."""
    from applypilot.database import log_event
    if not url:
        return {"ok": False, "message": "url required"}
    init_db()
    conn = get_connection()
    if not _jobs.exists(url, conn):
        return {"ok": False, "message": "job not found"}
    _jobs.mark_rejected(url, conn)
    log_event(url, "apply", "info", "Marked rejected — moved to the rejected pile.", conn)
    return {"ok": True, "message": "Moved to rejected pile"}


def _unmark_rejected(url: str) -> dict:
    """Undo a rejection: restore the job to its prior state (applied if it had applied_at, else
    cleared) and remove rejected_at."""
    from applypilot.database import log_event
    if not url:
        return {"ok": False, "message": "url required"}
    init_db()
    conn = get_connection()
    if not _jobs.exists(url, conn):
        return {"ok": False, "message": "job not found"}
    _jobs.unmark_rejected(url, "applied" if _jobs.applied_at(url, conn) else None, conn)
    log_event(url, "apply", "info", "Rejection undone — restored from the rejected pile.", conn)
    return {"ok": True, "message": "Restored from rejected pile"}


def _json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    # Never cached: it carries the ?v= that invalidates everything else.
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


# ── static assets (ARCH-2) ──────────────────────────────────────────────────
# The page lives in src/applypilot/static/, not in a Python string. Only these three
# names are servable — the path is looked up in a dict, never joined onto user input,
# so there is no traversal surface to get wrong.

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_ASSETS = {
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "application/javascript; charset=utf-8"),
}


def _asset_version() -> str:
    """Cache-buster for the <link>/<script> tags in index.html.

    Package version alone is useless in development — the version doesn't move but the
    file does, and you spend the afternoon debugging a bundle the browser cached an hour
    ago. Mixing in the newest asset mtime makes every edit a new URL.
    """
    newest = 0.0
    for name, _ in _STATIC_ASSETS.values():
        try:
            newest = max(newest, (_STATIC_DIR / name).stat().st_mtime)
        except OSError:
            pass
    return f"{__version__}-{int(newest)}"


def _index_html() -> str:
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__ASSET_V__", _asset_version())


def _serve_static(handler: BaseHTTPRequestHandler, path: str) -> bool:
    entry = _STATIC_ASSETS.get(path)
    if entry is None:
        return False
    name, content_type = entry
    try:
        body = (_STATIC_DIR / name).read_bytes()
    except OSError:
        _json_response(handler, {"error": "asset missing"}, HTTPStatus.NOT_FOUND)
        return True
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    # Safe to cache hard: index.html is never cached and always carries a fresh ?v=.
    handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
    handler.end_headers()
    handler.wfile.write(body)
    return True


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length).decode("utf-8") if length else "{}"
    if handler.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
        return {k: v[-1] if v else "" for k, v in parse_qs(raw).items()}
    return json.loads(raw or "{}")


def _tail_file(path: Path, max_lines: int = 120) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _material_url(path: str | None) -> str:
    if not path:
        return ""
    return f"/api/material?path={quote(path, safe='')}"


def _material_entries(label: str, path_value: str | None) -> list[dict]:
    if not path_value:
        return []

    path = Path(path_value)
    entries: list[dict] = []
    if path.exists():
        entries.append({"label": label, "url": _material_url(str(path)), "path": str(path)})

    pdf_path = path.with_suffix(".pdf")
    if pdf_path.exists():
        entries.append({"label": f"{label} PDF", "url": _material_url(str(pdf_path)), "path": str(pdf_path)})

    return entries


def _serve_material(handler: BaseHTTPRequestHandler, raw_path: str) -> None:
    try:
        requested = Path(unquote(raw_path)).expanduser().resolve()
        app_dir = config.APP_DIR.resolve()
        requested.relative_to(app_dir)
    except Exception:
        _json_response(handler, {"error": "material not found"}, HTTPStatus.NOT_FOUND)
        return

    if not requested.is_file():
        _json_response(handler, {"error": "material not found"}, HTTPStatus.NOT_FOUND)
        return

    mime_type = mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
    body = requested.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", mime_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Content-Disposition", f'inline; filename="{requested.name}"')
    handler.end_headers()
    handler.wfile.write(body)


# LinkedIn contacts in any of these states are "done" — a note was sent, or the user
# handled/skipped them manually — so they must not re-surface in the outreach queue
# (email `submitted` is handled separately in the email branch). `composed` is NOT here:
# the note was filled but the human hasn't sent yet.
_DM_DONE_STATUSES = frozenset({"sent", "manual", "skipped"})
# The extension LinkedIn queue excludes ONLY contacts actually invited (sent/manual) — NOT
# `skipped`. Auto-skip has been a false-positive trap; skipped contacts must keep re-appearing
# so the queue never silently empties. Use "Mark sent" to retire a genuinely-invited contact.
_EXT_QUEUE_EXCLUDE = frozenset({"sent", "manual"})


def _networking_available() -> bool:
    from applypilot.networking import providers
    return providers.available()


def _gmail_available() -> bool:
    """True if any Gmail send transport (OAuth or SMTP app-password) is ready."""
    from applypilot.networking import gmail_send
    return gmail_send.transport() is not None


def _apollo_profile_url(apollo_id: str | None) -> str:
    """Deep link to a person in the Apollo web app.

    Apollo's API will not release a direct dial to a local tool — verified three ways:
    people-search returns only `has_direct_phone: "Yes"`; `people/match` with
    `reveal_phone_number` 400s without a PUBLIC webhook_url (loopback is rejected); and
    saving the person as a contact yields only the org switchboard, even after polling.
    The UI reveal is the only local route, so we link there and the number is pasted back.
    """
    aid = (apollo_id or "").strip()
    return f"https://app.apollo.io/#/people/{aid}" if aid else ""


def _job_checklist(job_status: str, applied_at: str, contacts: list[dict]) -> dict:
    """Thin delegate — the rule lives in applypilot.domain.checklist."""
    from applypilot.domain import job_checklist
    return job_checklist(job_status, applied_at, contacts)


def _followup_panel(contacts: list[dict], ladders: dict | None = None) -> dict:
    """Thin delegate — the rule lives in applypilot.domain.followup.

    Ladder state comes from `touches` (ARCH-3), loaded in ONE bulk query rather than per
    contact per channel: this runs on every 2.5s dashboard refresh.
    """
    from applypilot.domain import followup_panel
    if ladders is None:
        ladders = _ladder_states([c.get("id") for c in contacts if c.get("id")])
    return followup_panel(contacts, ladders=ladders)


def _ladder_states(contact_ids: list[str]) -> dict:
    """Bulk-load follow-up ladders. Degrades to empty rather than 500-ing the dashboard.

    Deliberately NOT gated on `_networking_available()` — that means "Apollo key present",
    and follow-up history for contacts you already have must not disappear because a
    provider key went missing.
    """
    if not contact_ids:
        return {}
    try:
        from applypilot.networking import touches
        conn = get_connection()
        touches.init_touches(conn)
        return touches.ladder_states(contact_ids, conn)
    except Exception as exc:  # noqa: BLE001
        console.log(f"[dim]ladder load failed: {exc}[/dim]")
        return {}


def _apollo_search_url(full_name: str | None, company: str | None) -> str:
    """Fallback: Apollo people search prefilled with the person's name.

    The per-person route above is Apollo's SPA and undocumented; if it ever changes, a
    search page with their name already typed still gets you there in one more click.
    """
    q = " ".join(x for x in [(full_name or "").strip(), (company or "").strip()] if x)
    return f"https://app.apollo.io/#/people?qKeywords={quote(q)}" if q.strip() else ""


def _legacy_followup_status(ladder: dict) -> str:
    """Flatten the two ARCH-3 lifecycles back into the single field the payload exposes.

    Precedence is the old column's: a halted sequence wins, then an in-flight/staged touch,
    then 'sent' once at least one touch has gone out. Without the last clause a fully-sent
    ladder reports '' where it used to report 'sent' — caught by diffing /api/status against
    the pre-migration payload, not by any unit test.
    """
    if ladder["sequence_status"]:
        return ladder["sequence_status"]
    if ladder["touch_status"]:
        return ladder["touch_status"]
    return "sent" if ladder["count"] else ""


def _contact_payload(c: dict, company: str | None = None, ladders: dict | None = None,
                     conn_matches: dict | None = None, thread: list | None = None) -> dict:
    from applypilot.domain.followup import EMPTY_LADDER
    from applypilot.networking import connections
    # Prebuilt by the caller in one query when rendering a whole job; falls back to a
    # single lookup so this stays usable on its own.
    conn_rec = (conn_matches or {}).get(c.get("full_name") or "") if conn_matches is not None \
        else connections.match(c.get("full_name"), company)
    ladders = ladders or {}
    cid = c.get("id") or ""
    # ARCH-3: ladder state is per (contact, channel) in `touches`, not ten columns here.
    # The payload keys stay as they were so the frontend is untouched by the storage move.
    email_l = ladders.get((cid, "email")) or EMPTY_LADDER
    li_l = ladders.get((cid, "linkedin")) or EMPTY_LADDER
    return {
        "id": c.get("id") or "",
        "full_name": c.get("full_name") or "",
        "title": c.get("title") or "",
        "email": c.get("email") or "",
        "email_status": c.get("email_status") or "none",
        "linkedin_url": c.get("linkedin_url") or "",
        "match_reason": c.get("match_reason") or "",
        # Operator-entered (Apollo won't hand a direct dial to a local tool — see store.py).
        "phone": c.get("phone") or "",
        "notes": c.get("notes") or "",
        "apollo_url": _apollo_profile_url(c.get("apollo_id")),
        "apollo_search_url": _apollo_search_url(c.get("full_name"), company or c.get("company")),
        "outreach_subject": c.get("outreach_subject") or "",
        "outreach_message": c.get("outreach_message") or "",
        "linkedin_message": c.get("linkedin_message") or "",
        "outreach_status": c.get("outreach_status") or "none",
        # Ground-truth "an email actually went out": Gmail returned a message id. This survives a
        # later draft edit/regenerate (which resets outreach_status to 'drafted') — so the UI and
        # send-gate rely on THIS, not just outreach_status, to know a contact was already emailed.
        "emailed": bool((c.get("sent_message_id") or "").strip())
                   or c.get("outreach_status") == "submitted",
        # Checklist + follow-up inputs.
        "submitted_at": c.get("submitted_at") or "",
        "followed_up_at": email_l["last_sent_at"],
        "followup_count": email_l["count"],
        "followup_status": _legacy_followup_status(email_l),
        "followup_subject": email_l["draft_subject"],
        "followup_message": email_l["draft_body"],
        "followup_error": email_l["error"],
        "threaded": bool((c.get("thread_id") or "").strip()
                         or (c.get("rfc_message_id") or "").strip()),
        "li_followup_count": li_l["count"],
        "li_followup_status": _legacy_followup_status(li_l),
        "li_followup_message": li_l["draft_body"],
        "dm_sent_at": c.get("dm_sent_at") or "",
        # LinkedIn DM channel state + per-contact readiness (has note + profile, not sent).
        "dm_status": c.get("dm_status") or "none",
        "dm_error": c.get("dm_error") or "",
        "dm_ready": bool((c.get("linkedin_url") or "").strip()
                         and (c.get("linkedin_message") or "").strip()
                         and c.get("dm_status") not in _DM_DONE_STATUSES),
        # Live connection signal (recomputed each load so re-imports reflect instantly).
        "is_connection": bool(conn_rec),
        "connection_at_company": bool(conn_rec and conn_rec.get("company_match")),
        "connection_url": (conn_rec or {}).get("url", ""),
        # The employer LinkedIn actually lists for them. Surfaced so a bad company match is
        # visible instead of silent — "🤝 Connection here" once hid Armanino behind "Arm".
        "connection_company": (conn_rec or {}).get("company", "") or "",
        "confidence": c.get("confidence") or "",
        "verify_note": c.get("verify_note") or "",
        "replied_at": c.get("replied_at") or "",
        # Intro-deck engagement. A click, not an open — the difference between "a person read
        # your deck" and "a spam filter fetched an image".
        "deck_viewed_at": c.get("deck_viewed_at") or "",
        "deck_last_at": c.get("deck_last_at") or "",
        "deck_views": c.get("deck_views") or 0,
        # CRM-4: the stored conversation (headers only) and, when the other side added this
        # person to a thread, who did it — the handoff a boolean `replied` used to discard.
        "thread": thread or [],
        "introduced_by": _introduced_by(c, thread or []),
        # Who a reply would go to, computed from the stored thread — no extra query and no
        # extra round-trip, so the composer can open prefilled. None when nobody has written
        # to us, which is how the UI knows to offer a follow-up instead of a reply.
        "reply_to": _reply_target(thread or []),
        # Whose turn it is. `awaiting_us` means they wrote and nobody answered — the worst
        # outcome the system can produce, since it paid for the reply and then dropped it.
        "conversation": _conversation_state(thread or []),
        # CRM-4b. Empty on every metadata-only install, which is the default.
        "last_reply": _last_reply(thread or []),
        # HOT layer marker: found via your connections (vs cold Apollo). Either the stored source
        # or a live connection match makes it "hot".
        "hot": c.get("source") == "connection" or bool(conn_rec),
        "source": c.get("source") or "",
    }


def _status_payload() -> dict:
    init_db()
    conn = get_connection()
    from applypilot.networking.store import init_contacts, get_contacts_for_job
    from applypilot.networking import derive as _derive
    init_contacts(conn)
    _net_tasks = _network.statuses()

    stats = _jobs.queue_stats(conn)
    lifetime = _jobs.lifetime_stats(conn)

    rows = _jobs.dashboard_rows(conn=conn)

    jobs: list[dict] = []
    # Connection counts for every company up front: one scan of the 899-row connections
    # table instead of one per job. Derived here rather than inside the loop so the
    # expensive part happens once — see connections.company_counts.
    from applypilot.networking import connections as _conns
    _job_companies = {r["url"]: (_derive.derive_company(dict(zip(r.keys(), r)))
                                 or r["site"] or "") for r in rows}
    _conn_counts = _conns.company_counts(list(set(_job_companies.values())), conn)

    for row in rows:
        # Status precedence (each maps to a UI indicator):
        #   applied > active apply states (needs_human / ready_to_submit / in_progress / dryrun) >
        #   failed > ready (materials done) > scored > enrich-failed > enriched > imported.
        # The apply states MUST take precedence over 'ready' — a co-pilot-filled job has a resume
        # too, so checking tailored_resume_path first used to clobber 'ready_to_submit' -> 'ready'.
        apply_status = row["apply_status"] or ""
        _APPLY_STATES = {"needs_human", "ready_to_submit", "in_progress", "dryrun"}
        if apply_status == "rejected":
            status = "rejected"          # rejected pile — top precedence (even over applied)
        elif row["applied_at"]:
            status = "applied"
        elif apply_status in _APPLY_STATES:
            status = apply_status
        elif apply_status == "failed" or row["apply_error"]:
            status = "failed"
        elif row["tailored_resume_path"]:
            status = "ready"
        elif row["fit_score"] is not None:
            status = "scored"
        elif row["detail_error"]:
            status = "detail_failed"
        elif row["full_description"] and row["full_description"].strip().lower() != "null":
            status = "enriched"
        else:
            status = "imported"

        desc = row["full_description"] or ""
        if desc.strip().lower() == "null":
            desc = ""
        materials = [
            *_material_entries("Resume", row["tailored_resume_path"]),
            *_material_entries("Cover Letter", row["cover_letter_path"]),
        ]
        contact_company = _job_companies[row["url"]]
        raw_contacts = get_contacts_for_job(row["url"], conn)
        # One bulk ladder load per job, shared by the payloads and the follow-up panel —
        # not one query per contact per channel on a 2.5s refresh.
        job_ladders = _ladder_states([c.get("id") for c in raw_contacts if c.get("id")])
        job_matches = _conns.match_many([c.get("full_name") for c in raw_contacts],
                                        contact_company, conn)
        job_threads = _conversations_for_job(row["url"], conn)
        contacts = [_contact_payload(c, contact_company, job_ladders, job_matches,
                                     thread=job_threads.get(c.get("id")) or [])
                    for c in raw_contacts]
        net_task = _net_tasks.get(row["url"], {})
        jobs.append({
            "url": row["url"],
            "title": row["title"] or "Untitled",
            "company": row["site"] or "",
            "contact_company": contact_company,
            "connections_at_company": _conn_counts.get(contact_company, 0),
            "salary": row["salary"] or "",
            "location": row["location"] or "",
            "description": desc[:900],
            "application_url": row["application_url"] or "",
            "fit_score": row["fit_score"],
            "reasoning": row["score_reasoning"] or "",
            "status": status,
            "apply_error": row["apply_error"] or row["detail_error"] or "",
            "apply_attempts": row["apply_attempts"] or 0,
            "applied_at": row["applied_at"] or "",
            "rejected_at": row["rejected_at"] or "",
            "last_attempted_at": row["last_attempted_at"] or "",
            "materials": materials,
            "contacts": contacts,
            "checklist": _job_checklist(status, row["applied_at"] or "", contacts),
            "followups": _followup_panel(contacts, job_ladders),
            "conversations": job_threads,
            "awaiting_reply": _awaiting_us(contacts),
            "introductions": _pending_introductions(job_threads, raw_contacts),
            "interactions": _interactions_for_job(row["url"], contacts, conn),
            "activity": _job_activity(row["url"], conn),
            "network_running": bool(net_task.get("running")),
            "network_note": net_task.get("note") or "",
            "network_error": net_task.get("error") or "",
            "signin_open": _signin_state(row["url"]),
        })

    worker_log = _tail_file(config.LOG_DIR / "worker-0.log")
    latest_claude = sorted(config.LOG_DIR.glob("claude_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    claude_log = _tail_file(latest_claude[0]) if latest_claude else []
    stats_dict = dict(stats)
    stats_dict["lifetime_total"] = lifetime["total"] or 0
    stats_dict["lifetime_applied"] = lifetime["applied"] or 0
    stats_dict["lifetime_errors"] = lifetime["errors"] or 0
    command_status = _runner.status()

    return {
        "stats": stats_dict,
        "jobs": jobs,
        "command": command_status,
        "progress": _progress_payload(stats_dict, jobs, command_status),
        "worker_log": worker_log,
        "claude_log": claude_log,
        "app_dir": str(config.APP_DIR),
        "networking_available": _networking_available(),
        "metrics": _metrics_payload(rows, conn),
        "replies": _replies.status(),
        "gmail_available": _gmail_available(),
        # Whether the token carries gmail.readonly, so the UI can offer "Fetch from Gmail"
        # instead of only a paste box. Cached inside gmail_oauth (keyed on the token file's
        # mtime), so this costs nothing on a 2.5s refresh.
        "content_scope": _content_scope(),
        # The real poller cadence, so the UI states it rather than hardcoding a guess
        # that silently becomes wrong the moment the interval changes.
        "poll_every_s": _replies.interval_s,
        # Mutual shared token for the LinkedIn extension — operator pastes it into the popup once.
        "ext_token": _ext_token(),
    }


def _pending_introductions(job_threads: dict, raw_contacts: list) -> list[dict]:
    """People the other side added to a thread who are not contacts yet (CRM-4).

    Computed from STORED messages, so it costs no Gmail call on a 2.5s refresh.
    """
    try:
        from applypilot.domain import conversations as cv
        from applypilot.networking import gmail_oauth
        emails = [c.get("email") for c in raw_contacts if c.get("email")]
        return cv.pending_introductions(job_threads, emails, gmail_oauth.connected_email())
    except Exception:  # noqa: BLE001
        log.debug("Pending-introduction scan failed", exc_info=True)
        return []


def _add_introduced_contact(data: dict) -> dict:
    """Add someone who was introduced on a thread, as a real contact.

    Kept behind an explicit click rather than created automatically: threads collect
    schedulers, assistants and ATS robots, and a contact created here is one an automated
    follow-up ladder would then EMAIL.

    `source='introduction'` is deliberately distinct from apollo/connection — this is the
    warmest lead the system can produce (a human at the company handed you to them), and
    CRM-2's by_layer() should eventually be able to prove that.
    """
    from applypilot.database import log_event
    from applypilot.networking.store import init_contacts, upsert_contact

    job_url = (data.get("job_url") or "").strip()
    email = (data.get("email") or "").strip().lower()
    if not job_url or not email:
        return {"ok": False, "message": "job_url and email required"}

    init_db()
    conn = get_connection()
    init_contacts(conn)
    row = _jobs.find_by_any_url(job_url, conn)
    if not row:
        return {"ok": False, "message": "job not found"}
    job = dict(row)

    name = (data.get("name") or "").strip() or email.split("@")[0].replace(".", " ").title()
    by = (data.get("introduced_by") or "").strip()
    cid = upsert_contact({
        "job_url": job["url"],
        "full_name": name,
        "email": email,
        "email_status": "verified",   # it came off a real thread they were Cc'd on
        "company": job.get("company"),
        "source": "introduction",
        "match_reason": f"introduced by {by}" if by else "introduced on the thread",
        "confidence": "high",
        "verify_note": f"{by} added them to a live thread" if by else "added to a live thread",
        "outreach_status": "none",
    }, conn)

    log_event(job["url"], "outreach", "ok",
              f"Added {name} ({email}) as a contact — introduced by {by or 'the thread'}.", conn)

    drafted = False
    try:
        from applypilot.config import load_profile
        from applypilot.networking import service
        contact = _store.get_contact(cid, conn)
        service._draft_and_store(load_profile(), job, contact, warm=False)
        drafted = True
    except Exception:  # noqa: BLE001
        # A missing draft is recoverable from the UI; a failed add is not.
        log.debug("Draft for introduced contact failed", exc_info=True)

    return {"ok": True, "contact_id": cid,
            "message": f"Added {name}." + (" Draft ready." if drafted else
                                           " Use “Regenerate” to draft an email.")}


def _last_reply(thread: list) -> dict | None:
    """The newest inbound message's stored snippet, and what it looks like they want (CRM-4b).

    Returns None when there is no snippet — which is every install that never granted
    `gmail.readonly`, and the reason 4b can ship without changing anything for them.
    """
    try:
        inbound = [m for m in thread if isinstance(m, dict) and m.get("direction") == "in"]
        if not inbound:
            return None
        last = inbound[-1]
        text = (last.get("snippet") or "").strip()
        if not text:
            return None
        from applypilot.domain import intent as _intent
        return {"text": text, "at": last.get("sent_at") or "",
                "from": last.get("from_name") or last.get("from_addr") or "",
                **_intent.suggestion(_intent.classify(text))}
    except Exception:  # noqa: BLE001
        log.debug("Could not summarise the last reply", exc_info=True)
        return None


def _awaiting_us(contacts: list[dict]) -> list[dict]:
    """Contacts who wrote to us and are still waiting, newest silence last.

    Rolled up per job so the row can rank answering a real human above every follow-up:
    somebody who replied outranks somebody who did not, and no ladder should be able to
    outshout them.
    """
    out = []
    for c in contacts:
        conv = c.get("conversation") or {}
        if conv.get("state") == "awaiting_us":
            out.append({"id": c.get("id"), "full_name": c.get("full_name", ""),
                        "days": conv.get("days"), "hours": conv.get("hours")})
    return sorted(out, key=lambda r: -(r["hours"] or 0))


def _conversation_state(thread: list) -> dict | None:
    """Whose turn it is on this thread. Never raises — see `_reply_target`."""
    if not thread:
        return None
    try:
        from applypilot.domain import conversations as cv
        return cv.conversation_state(thread)
    except Exception:  # noqa: BLE001
        log.debug("Could not compute a conversation state", exc_info=True)
        return None


def _reply_target(thread: list) -> dict | None:
    """The recipients a reply would use, for the composer. Never raises.

    Rendered on every 2.5s refresh, so a thread with an odd header must not be able to 500 the
    whole dashboard — the same reason `_parse_ts` exists (§Lessons 6).
    """
    if not thread:
        return None
    try:
        from applypilot.domain import conversations as cv
        from applypilot.networking.gmail_send import _our_addresses
        return cv.reply_target(thread, _our_addresses())
    except Exception:  # noqa: BLE001
        log.debug("Could not compute a reply target", exc_info=True)
        return None


def _sync_all_gmail(data: dict) -> dict:
    """Pull every Gmail conversation with this contact, however it started.

    Distinct from `_fetch_reply_text`, which reads the ONE thread ApplyPilot sent. This searches
    by their address, so a thread they began, an email sent straight from Gmail, or one where
    they merely CC'd you all arrive — the conversations the CRM's memory previously stopped
    short of because it only knew thread ids it had captured at send time.
    """
    from applypilot.database import log_event
    from applypilot.networking import replies as _replies, store as _store

    cid = (data.get("contact_id") or "").strip()
    if not cid:
        return {"ok": False, "message": "contact_id required"}
    conn = get_connection()
    _store.init_contacts(conn)
    contact = _store.get_contact(cid, conn)
    if not contact:
        return {"ok": False, "message": "contact not found"}

    res = _replies.sync_all_with(contact, conn)
    if res.get("ok") and res.get("messages"):
        log_event(contact.get("job_url", ""), "outreach", "ok",
                  f"Pulled {res['messages']} message(s) across {res['threads']} Gmail "
                  f"conversation(s) with {contact.get('full_name') or cid}.", conn)
    return res


def _interactions_for_job(job_url: str, contacts: list, conn) -> dict:
    """Everything these people have DONE, as one timeline per person.

    Derived from the contacts already in hand plus ONE query for the stored events, so this
    costs a single statement per job rather than one per contact — /api/status is held to a
    50-statement budget and re-renders every 2.5 seconds.
    """
    try:
        from applypilot.domain import interactions as _ix
        from applypilot.networking import interactions_store
        return _ix.for_job(contacts, interactions_store.for_job(job_url, conn))
    except Exception:  # noqa: BLE001
        log.debug("Could not build interactions", exc_info=True)
        return {"people": [], "total": 0, "engaged": 0}


def _log_interaction(data: dict) -> dict:
    """Record something the operator SAW. Never dressed up as a detection.

    LinkedIn profile views are the motivating case and the reason `source` exists: they are not
    in the LinkedIn data export and generate no notification email, so the only source is
    LinkedIn's own UI — which this project abandoned automating twice (Lessons 3). An operator
    note is honest; a fake detector would not be.
    """
    from applypilot.database import log_event
    from applypilot.domain import interactions as _ix
    from applypilot.networking import interactions_store, store as _store

    cid = (data.get("contact_id") or "").strip()
    kind = (data.get("kind") or "").strip()
    if not cid:
        return {"ok": False, "message": "contact_id required"}
    if kind not in (_ix.PROFILE_VIEW, _ix.NOTE, _ix.BOOKED):
        return {"ok": False, "message": f"cannot log {kind!r} by hand"}

    conn = get_connection()
    _store.init_contacts(conn)
    contact = _store.get_contact(cid, conn)
    if not contact:
        return {"ok": False, "message": "contact not found"}

    at = (data.get("at") or "").strip()
    detail = (data.get("detail") or "").strip()
    is_new = interactions_store.record(cid, kind, at=at, detail=detail, source="manual",
                                       job_url=contact.get("job_url") or "", conn=conn)
    who = contact.get("full_name") or cid
    if is_new:
        log_event(contact.get("job_url", ""), "outreach", "ok",
                  f"Noted: {who} — {_ix.LABEL.get(kind, kind)}"
                  + (f" ({detail})" if detail else "") + ".", conn)
    return {"ok": True, "message": f"Noted for {who}." if is_new else "Already recorded."}


def _job_description(data: dict) -> dict:
    """The full posting text for ONE job, on demand.

    `/api/status` carries a 900-char excerpt. Descriptions run 4–8KB, so shipping them for every
    job on a 2.5s refresh — to fill a pane that is usually closed — would multiply the payload
    for nothing. Fetched once per job when the operator expands it.
    """
    url = (data.get("url") or "").strip()
    if not url:
        return {"ok": False, "message": "url required"}
    init_db()
    row = _jobs.find_by_any_url(url, get_connection())
    if not row:
        return {"ok": False, "message": "job not found"}
    job = dict(row)
    desc = (job.get("full_description") or "").strip()
    if desc.lower() == "null":          # scrapers write the string "null" (§enrichment)
        desc = ""
    return {"ok": True, "description": desc}


def _content_scope() -> bool:
    """Does the stored token allow reading message text? Never raises."""
    try:
        from applypilot.networking import gmail_oauth
        return bool(gmail_oauth.can_read_content())
    except Exception:  # noqa: BLE001
        return False


def _fetch_reply_text(data: dict) -> dict:
    """Read ONE conversation's text from Gmail, on an explicit click. Never automatic.

    The scope is all-or-nothing, so this does not narrow what ApplyPilot is permitted to read.
    It narrows what it ever does read — one named thread when asked, rather than every open
    thread on every poll. The refusal path says which is missing, because "nothing happened" is
    the one answer that leaves the operator unable to act (§Lessons 15).
    """
    from applypilot.database import log_event
    from applypilot.networking import replies as _replies, store as _store

    cid = (data.get("contact_id") or "").strip()
    if not cid:
        return {"ok": False, "message": "contact_id required"}
    conn = get_connection()
    _store.init_contacts(conn)
    contact = _store.get_contact(cid, conn)
    if not contact:
        return {"ok": False, "message": "contact not found"}

    res = _replies.fetch_thread_text(contact, conn)
    if res.get("ok"):
        log_event(contact.get("job_url", ""), "outreach", "ok",
                  f"Read this conversation's text from Gmail on request "
                  f"({res.get('stored', 0)} message(s)) — {contact.get('full_name') or cid}.",
                  conn)
    return res


def _draft_reply(data: dict) -> dict:
    """Draft an answer to a live conversation, from the whole sequence. Never sends.

    Two ways the reply text arrives and this does not prefer either: `their_reply` pasted by
    the operator, or the stored snippet if `gmail.readonly` was granted. A paste is PERSISTED —
    otherwise the sequence the CRM claims to remember has a hole exactly where the interesting
    part is, and the operator re-pastes it on every redraft.

    Refuses when there is no text at all, rather than producing something. A "contextual" reply
    written with no context is a generic follow-up wearing a Re: subject line, and it would be
    indistinguishable from a working feature until somebody read it.
    """
    from applypilot.database import log_event
    from applypilot.networking import messages as _msgs, store as _store, touches as _touches

    cid = (data.get("contact_id") or "").strip()
    if not cid:
        return {"ok": False, "message": "contact_id required"}

    conn = get_connection()
    _store.init_contacts(conn)
    contact = _store.get_contact(cid, conn)
    if not contact:
        return {"ok": False, "message": "contact not found"}

    pasted = (data.get("their_reply") or "").strip()
    if pasted:
        _msgs.set_reply_text(cid, pasted, conn)

    thread = _msgs.thread_for_contact(cid, conn)
    said = pasted or next((m.get("snippet") or "" for m in reversed(thread)
                           if m.get("direction") == "in" and (m.get("snippet") or "").strip()), "")
    if not said.strip():
        return {"ok": False,
                "message": "Paste what they wrote above and I'll answer it — or turn on reply "
                           "content (`network --gmail-connect --with-content`) to read it "
                           "automatically."}

    job = _jobs.get(contact.get("job_url", ""), conn) or {"url": contact.get("job_url", "")}
    try:
        from applypilot.config import load_profile
        from applypilot.networking import outreach
        try:
            profile = load_profile()
        except Exception:  # noqa: BLE001
            # Same fallback as `tick`: the profile supplies the sender's name and scheduling
            # link, both optional. Refusing to answer a live human because a config file is
            # missing is the wrong trade.
            profile = {}
        d = outreach.draft_reply(profile, job, contact, thread=thread,
                                 style=(data.get("style") or ""), their_reply=said,
                                 touches=_touches.sent_touches(cid, "email", conn))
    except Exception as e:  # noqa: BLE001
        log.debug("Reply draft failed", exc_info=True)
        return {"ok": False, "message": f"Draft failed: {e}"}

    log_event(contact.get("job_url", ""), "outreach", "ok",
              f"Drafted a reply to {contact.get('full_name') or 'them'}"
              + (f" ({d.get('intent')})" if d.get("intent") not in (None, "unknown") else "")
              + ". Not sent.", conn)
    return {"ok": True, "subject": d["subject"], "body": d["body"], "intent": d.get("intent"),
            "message": "Draft ready — read it before you send it."}


def _send_reply(data: dict) -> dict:
    """Answer a live conversation, in-thread, from the dashboard.

    Thin on purpose: recipients are decided by `domain.conversations.reply_target()` from the
    stored thread, NOT by anything the browser posts. The composer shows them and lets the
    operator drop a Cc, but it cannot invent a recipient — an endpoint that accepted a `to`
    would be an open relay pointed at whatever the page happened to hold.
    """
    from applypilot.database import log_event
    from applypilot.networking import gmail_send, store as _store

    cid = (data.get("contact_id") or "").strip()
    body = (data.get("body") or "").strip()
    if not cid:
        return {"ok": False, "message": "contact_id required"}
    if not body:
        return {"ok": False, "message": "write a reply first"}

    conn = get_connection()
    _store.init_contacts(conn)
    contact = _store.get_contact(cid, conn)
    if not contact:
        return {"ok": False, "message": "contact not found"}

    # `cc` absent means "keep whatever the thread had"; an explicitly empty list means the
    # operator removed everyone, which is a different instruction and must survive the trip.
    cc = data.get("cc")
    cc = None if cc is None else [str(c) for c in cc if str(c).strip()]

    res = gmail_send.send_reply(cid, body, subject=(data.get("subject") or ""),
                                cc=cc, conn=conn)
    if res.get("ok"):
        who = contact.get("full_name") or res.get("to", "")
        also = res.get("cc") or []
        log_event(contact.get("job_url", ""), "outreach", "ok",
                  f"Replied to {who}" + (f" (cc {', '.join(also)})" if also else "")
                  + " from the dashboard.", conn)
    return res


def _introduced_by(contact: dict, thread: list) -> str:
    """Who added this contact to the conversation, if anybody did.

    Only meaningful for someone who first appears as a Cc on a message WE did not send — that
    is the shape of a handoff ("Victoria introduced David"). A contact we emailed directly was
    introduced by nobody.
    """
    if not thread:
        return ""
    email = (contact.get("email") or "").strip().lower()
    if not email:
        return ""
    for msg in thread:
        if msg.get("direction") != "in":
            continue
        # cc_addrs holds RAW fragments ("David Loveless <david@writer.com>"), so compare on
        # the extracted address rather than the whole string.
        from applypilot.domain.conversations import addr as _addr
        if email in [_addr(a) for a in (msg.get("cc_addrs") or [])]:
            return msg.get("from_name") or msg.get("from_addr") or ""
    return ""


def _conversations_for_job(job_url: str, conn) -> dict:
    """contact_id -> stored thread (CRM-4). One query per JOB, never one per contact.

    Degrades to {} rather than raising: conversation memory is additive, and a job row must
    still render if the messages table is missing or unreadable.
    """
    try:
        from applypilot.networking import messages as _messages
        return _messages.threads_for_job(job_url, conn)
    except Exception:  # noqa: BLE001
        log.debug("Conversation load failed for %s", job_url, exc_info=True)
        return {}


def _metrics_payload(job_rows: list, conn) -> dict:
    """CRM-2 aggregates for the dashboard panel.

    Three narrow reads and one pass of pure aggregation — `domain.metrics` does the arithmetic,
    so the same numbers back `applypilot stats --outreach` and are unit-testable against
    fixtures. Kept cheap deliberately: this runs on every /api/status, which the query budget
    holds at 50 statements.
    """
    from applypilot.domain import metrics as metrics_mod
    from applypilot.domain.timeutil import parse_ts

    try:
        contacts = _store.all_contacts_for_metrics(conn)
        touch_rows = _touches.all_sent_touches(conn)
        jobs = [dict(zip(r.keys(), r)) if not isinstance(r, dict) else r for r in job_rows]
        return metrics_mod.summary(jobs, contacts, touch_rows, parse_ts)
    except Exception:  # noqa: BLE001
        # A metrics panel must never be able to take the dashboard down with it.
        log.debug("Metrics payload failed", exc_info=True)
        return {}


def _progress_payload(stats: dict, jobs: list[dict], command_status: dict) -> dict:
    running = bool(command_status.get("running"))
    name = command_status.get("name") or ""
    lines = command_status.get("log") or []
    last_lines = [line for line in lines[-40:] if line]
    current = "Idle"
    percent = 0

    if running:
        current = f"Running {name}"
        for line in reversed(last_lines):
            if "STAGE:" in line:
                current = line.strip("= ").replace("STAGE:", "").strip()
                break
            if re.search(r"\[\d+/\d+\]", line) or re.search(r"\d+/\d+", line):
                current = line.strip()
                break

        combined = "\n".join(last_lines)
        match = re.findall(r"(?:\[|\b)(\d+)/(\d+)(?:\]|\b)", combined)
        if match:
            done, total = match[-1]
            total_i = max(int(total), 1)
            percent = min(99, max(1, round(int(done) * 100 / total_i)))
        elif name == "prepare":
            total = max(int(stats.get("total") or 0), 1)
            prepared = int(stats.get("enriched") or 0) + int(stats.get("scored") or 0) + int(stats.get("tailored") or 0) + int(stats.get("covers") or 0)
            percent = min(99, round(prepared * 100 / (total * 4)))
        elif name == "apply":
            ready = int(stats.get("ready") or 0)
            applied = int(stats.get("applied") or 0)
            errors = int(stats.get("errors") or 0)
            denom = max(ready + applied + errors, 1)
            percent = min(99, round((applied + errors) * 100 / denom))
    elif name:
        rc = command_status.get("returncode")
        current = f"Last run: {name} exited {rc}"
        percent = 100 if rc == 0 else 0

    in_progress_jobs = [
        {"title": job["title"], "company": job["company"], "status": job["status"]}
        for job in jobs if job["status"] == "in_progress"
    ][:8]

    return {
        "running": running,
        "label": current,
        "percent": percent,
        "in_progress": int(stats.get("in_progress") or 0),
        "in_progress_jobs": in_progress_jobs,
    }


def _import_urls(text: str) -> dict:
    init_db()
    config.ensure_dirs()
    conn = get_connection()
    urls = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,;]")
        if url not in urls:
            urls.append(url)

    inserted = 0
    duplicates = 0
    existing_applied = 0
    existing_failed = 0
    existing_ready = 0
    existing_pending = 0

    for url in urls:
        existing = _jobs.import_state(url, conn)
        if existing:
            if existing["strategy"] != "dashboard_upload":
                _jobs.touch_import(url, url, conn)
            if existing["applied_at"]:
                existing_applied += 1
            elif existing["apply_error"] or existing["apply_status"] == "failed":
                existing_failed += 1
            elif existing["tailored_resume_path"]:
                existing_ready += 1
            else:
                existing_pending += 1
            duplicates += 1
            continue
        company = _infer_company(url)
        title = f"{company} uploaded job"
        try:
            _jobs.insert_imported(url, title, company, company, url, conn)
            inserted += 1
        except Exception:
            duplicates += 1

    conn.commit()
    return {
        "found": len(urls),
        "inserted": inserted,
        "duplicates": duplicates,
        "existing_applied": existing_applied,
        "existing_failed": existing_failed,
        "existing_ready": existing_ready,
        "existing_pending": existing_pending,
    }


def _save_or_regen_draft(data: dict) -> dict:
    """Save an edited outreach draft, or regenerate it via the LLM."""
    init_db()
    conn = get_connection()
    from applypilot.networking.store import init_contacts, upsert_contact
    init_contacts(conn)

    cid = data.get("contact_id", "")
    if not cid:
        return {"ok": False, "message": "contact_id required"}
    row = _store.contact_ref(cid, conn)
    if not row:
        return {"ok": False, "message": "contact not found"}

    if data.get("regenerate"):
        from applypilot.networking import service
        draft = service.draft_for_contact(cid, style=(data.get("style") or "").strip())
        if not draft:
            return {"ok": False, "message": "regeneration failed (LLM/provider)"}
        return {"ok": True, "subject": draft["subject"], "body": draft["body"],
                "linkedin": draft.get("linkedin_note", "")}

    # Save an edit. Only fields the client actually SENT are written — the channel tabs each
    # render half the form, so a Save from the LinkedIn tab carries no subject/body and a
    # Save from the Email tab carries no note. Defaulting a missing field to "" would have the
    # LinkedIn tab silently blank the outreach email (and vice versa): the absent key means
    # "this tab never showed it", not "the user cleared it".
    fields = {"id": cid, "job_url": row["job_url"], "outreach_status": "drafted"}
    for key, column in (("subject", "outreach_subject"), ("body", "outreach_message"),
                        ("linkedin", "linkedin_message")):
        if key in data:
            fields[column] = data.get(key) or ""
    if len(fields) == 3:
        return {"ok": False, "message": "nothing to save"}
    upsert_contact(fields)
    return {"ok": True, "message": "saved"}


_PHONE_MAX_LEN = 40
_NOTES_MAX_LEN = 2000


def _delete_contact(contact_id: str) -> dict:
    """Remove a contact discovery got wrong — someone who does not work at this employer.

    Verification catches most of them, but it deliberately errs towards keeping an unconfirmed
    person (dropping a real contact is worse than showing a doubtful one), so wrong people do
    reach the list. Until now there was no way to remove one: `store.delete_contact` existed
    with no endpoint and no button.

    Deleting is allowed even after an email was sent. The row is the only record of that send,
    so the activity log keeps the name and the fact — otherwise the outreach simply vanishes
    from the job's history and it looks like it never happened.
    """
    from applypilot.database import log_event
    from applypilot.networking.store import delete_contact, init_contacts

    if not contact_id:
        return {"ok": False, "message": "contact_id required"}
    init_db()
    conn = get_connection()
    init_contacts(conn)
    row = _store.contact_for_delete(contact_id, conn)
    if not row:
        return {"ok": False, "message": "contact not found"}

    name = row.get("full_name") or "?"
    job_url = row.get("job_url")
    emailed = bool(row.get("sent_message_id"))
    if not delete_contact(contact_id, conn):
        return {"ok": False, "message": "contact not found"}

    sent = " (an email had already been sent to them)" if emailed else ""
    log_event(job_url, "outreach", "info", f"Removed contact: {name}{sent}.", conn)
    return {"ok": True, "message": f"Removed {name}"}


def _save_contact_details(data: dict) -> dict:
    """Persist the operator-entered phone / notes for one contact.

    Separate from _save_or_regen_draft on purpose: that handler stamps
    outreach_status='drafted', which would wrongly re-open an already-sent contact
    just because a phone number got typed in.
    """
    init_db()
    conn = get_connection()
    from applypilot.networking.store import init_contacts, upsert_contact
    init_contacts(conn)

    cid = data.get("contact_id", "")
    if not cid:
        return {"ok": False, "message": "contact_id required"}
    row = _store.contact_ref(cid, conn)
    if not row:
        return {"ok": False, "message": "contact not found"}

    fields = {"id": cid, "job_url": row["job_url"]}
    if "phone" in data:
        fields["phone"] = str(data.get("phone") or "").strip()[:_PHONE_MAX_LEN]
    if "notes" in data:
        fields["notes"] = str(data.get("notes") or "").strip()[:_NOTES_MAX_LEN]
    if len(fields) == 2:
        return {"ok": False, "message": "nothing to save"}
    before = _store.contact_name_and_phone(cid, conn)
    upsert_contact(fields)
    # Only log a phone that actually changed — re-saving a note shouldn't spam the timeline.
    new_phone = fields.get("phone")
    if new_phone and new_phone != (before["phone"] or ""):
        from applypilot.networking.store import log_contact_event
        who = (before["full_name"] if before else None) or "contact"
        log_contact_event(cid, "info", f"Added a phone number for {who}: {new_phone}.", conn)
    return {"ok": True, "message": "saved"}


_SEQUENCE_VERBS = {"stop": "stopped", "replied": "replied", "reopen": ""}


def _split_followup_action(action: str):
    """`li_draft` -> (LINKEDIN, 'draft'); `draft` -> (EMAIL, 'draft').

    Channel lives in the action name, so the handler below never learns which channel it
    is serving. Email's prefix is '' and must therefore be the fallback, not a match.
    """
    from applypilot.domain.followup import CHANNELS, EMAIL
    for ch in CHANNELS:
        if ch.prefix and action.startswith(ch.prefix):
            return ch, action[len(ch.prefix):]
    return EMAIL, action


def _followup_action(data: dict) -> dict:
    """draft | save | send | sent | stop | replied | reopen — for ANY channel.

    Before ARCH-3 this was two mirrored blocks: stop/li_stop, save/li_save, draft/li_draft,
    each with its own store function. One code path now serves both, which is the property
    the ticket is actually buying — deleting the LinkedIn half is no longer possible,
    because there is no LinkedIn half.
    """
    init_db()
    conn = get_connection()
    from applypilot.networking import store, touches
    store.init_contacts(conn)
    touches.init_touches(conn)

    cid = (data.get("contact_id") or "").strip()
    raw_action = (data.get("action") or "").strip()
    contact = store.get_contact(cid) if cid else None
    if not contact:
        return {"ok": False, "message": "contact not found"}

    channel, verb = _split_followup_action(raw_action)
    name = "LinkedIn " if channel.name == "linkedin" else ""

    if verb in _SEQUENCE_VERBS:
        status = _SEQUENCE_VERBS[verb]
        touches.set_sequence_status(cid, channel.name, status)
        store.log_contact_event(
            cid, "info",
            f"{contact.get('full_name') or 'contact'}: {name}sequence "
            + ("reopened" if verb == "reopen" else
               "stopped" if verb == "stop" else "replied — sequence stopped") + ".", conn)
        return {"ok": True, "message": f"{name}sequence "
                + ("reopened" if verb == "reopen" else "stopped")}

    # Recording the invite is not a ladder action — it sets the anchor the clock runs from.
    if verb == "connected":
        store.mark_connected_now(cid)
        return {"ok": True, "message": "recorded — follow-up clock started"}

    if verb == "save":
        touches.set_draft(cid, channel.name, data.get("subject", ""), data.get("body", ""))
        return {"ok": True, "message": "saved"}

    if verb == "sent":
        # YOU sent it (LinkedIn is copy-paste only — CLAUDE.md §Lessons 3).
        n = touches.record_sent(cid, channel.name, conn=conn)
        store.log_contact_event(cid, "ok", f"{name}follow-up #{n} sent to "
                                f"{contact.get('full_name') or 'contact'}.", conn)
        return {"ok": True, "touch": n, "message": f"{name}follow-up #{n} recorded"}

    if verb == "draft":
        job = _jobs.get(contact["job_url"], conn) or {"url": contact["job_url"]}
        from applypilot.config import load_profile
        from applypilot.networking import outreach
        try:
            profile = load_profile()
        except Exception:  # noqa: BLE001
            profile = {}
        state = touches.ladder_state(cid, channel.name, conn)
        touch = (state["count"] or 0) + 1
        try:
            d = outreach.draft_for_channel(channel.name, profile, job, contact,
                                           touch=touch, style=(data.get("style") or "").strip())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"draft failed: {e}"}
        touches.set_draft(cid, channel.name, d["subject"], d["body"])
        return {"ok": True, "subject": d["subject"], "body": d["body"], "touch": touch}

    if verb == "send":
        if not channel.can_autosend:
            # Structural, not a policy string: driving LinkedIn from outside the browser
            # was abandoned twice (CLAUDE.md §Lessons 3).
            return {"ok": False, "message": f"{channel.name} cannot be auto-sent — "
                                            "copy it and send it yourself"}
        from applypilot.networking.gmail_send import send_followup
        return send_followup(cid)

    return {"ok": False, "message": f"unknown action: {raw_action!r}"}


def _delete_job(url: str) -> dict:
    init_db()
    conn = get_connection()
    from applypilot.networking.store import init_contacts
    init_contacts(conn)
    if not url:
        return {"ok": False, "message": "Missing job URL"}

    row = _jobs.queued_for_delete(url, conn)
    if not row:
        return {"ok": False, "message": "Application not found"}
    _jobs.delete(url, conn)
    return {
        "ok": True,
        "message": f"Deleted {row['site'] or 'Unknown'} - {row['title'] or 'Untitled'}",
    }


# ── Extension local API handlers (EXT-0) ─────────────────────────────────────
# Loopback + shared-token guarded; frozen contract in extension/CONTRACTS.md §3.

def _normalize_linkedin_url(url: str) -> str:
    """Coerce a LinkedIn profile URL into the canonical https form the extension accepts.

    Apollo (and other providers) return `http://www.linkedin.com/in/...`; the extension's
    validator requires `https://[sub.]linkedin.com/in/...`. Without this every contact is
    rejected as "No valid LinkedIn profile URLs in the queue". Handles: http→https, missing
    protocol, and a bare `linkedin.com/in/...` or `www.` prefix.
    """
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    elif u.startswith("//"):
        u = "https:" + u
    elif not u.startswith("https://"):
        # bare "www.linkedin.com/in/..." or "linkedin.com/in/..."
        if u.startswith(("www.linkedin.com/", "linkedin.com/")):
            u = "https://" + u
    return u


def _job_activity(url: str, conn) -> list[dict]:
    """The job's activity log (most-recent-last), lightly shaped for the UI."""
    from applypilot.database import get_job_events
    out = []
    for e in get_job_events(url, limit=40, conn=conn):
        out.append({
            "ts": e.get("ts") or "",
            "stage": e.get("stage") or "",
            "status": e.get("status") or "",
            "detail": e.get("detail") or "",
        })
    return out


def _queue_contact_payload(c: dict) -> dict:
    """One /api/ext/queue row. `note` = contacts.linkedin_message (the verbatim invite note)."""
    return {
        "id": c.get("id") or "",
        "full_name": c.get("full_name") or "",
        "title": c.get("title") or "",
        "company": c.get("company") or "",
        "linkedin_url": _normalize_linkedin_url(c.get("linkedin_url") or ""),
        "note": c.get("linkedin_message") or "",
    }


def _ext_queue(job_url: str | None, include_skipped: bool = False) -> dict:
    """Ready LinkedIn contacts. Per-job (via _eligible_contact_ids) or all-jobs (deduped).

    include_skipped=True resurrects contacts that were previously `skipped` (almost always an
    auto-skip false positive) by resetting them to `none` so every generated contact re-appears
    in the queue. `sent`/`manual` (genuinely done) are left untouched.
    """
    from applypilot.networking.store import _norm_linkedin, get_contact, init_contacts
    init_db()
    conn = get_connection()
    init_contacts(conn)

    if include_skipped:
        _store.unskip_dms(conn)

    if job_url:
        # Per-job: reuse the shared eligibility helper (linkedin_url + note + not done-set).
        contacts = [get_contact(cid, conn) for cid in _eligible_contact_ids(job_url, "linkedin")]
    else:
        # All-jobs variant: single SELECT over contacts, then dedupe by normalized profile URL
        # so the same person surfaced under two jobs yields exactly one queue row.
        contacts = []
        seen: set[str] = set()
        for c in _store.dm_queue(tuple(_EXT_QUEUE_EXCLUDE), conn):
            norm = _norm_linkedin(c.get("linkedin_url"))
            if norm in seen:
                continue
            seen.add(norm)
            contacts.append(c)

    return {"ok": True, "contacts": [_queue_contact_payload(c) for c in contacts if c]}


def _apply_dm_status(cid: str, status: str) -> tuple[dict, int]:
    """Map a reported LinkedIn status to the store's dm_* helpers (sent/manual/skipped).

    Shared by the extension API and the dashboard's own "sent the invite" confirm, so both
    routes stamp dedupe state and append the same activity-log line.
    """
    from applypilot.networking import store
    cid = (cid or "").strip()
    status = (status or "").strip()
    if not cid:
        return {"ok": False, "error": "contact_id required"}, 400
    if status not in _POSTABLE_DM_STATUSES:
        return {"ok": False, "error": f"invalid status: {status!r}"}, 400
    store.init_contacts()
    if not store.get_contact(cid):
        return {"ok": False, "error": "contact not found"}, 404
    if status == "sent":
        store.mark_dm_sent(cid)        # stamps dm_sent_at (COALESCE) — counts toward dedupe/cap
    elif status == "manual":
        store.mark_dm_manual(cid)      # real invite via fallback — stamps dm_sent_at too
    else:
        store.mark_dm_skipped(cid)     # no stamp; just excluded from the queue
    return {"ok": True}, 200


def _ext_status(data: dict) -> tuple[dict, int]:
    """Extension-reported send status (EXT-0 contract)."""
    return _apply_dm_status(data.get("contact_id") or "", data.get("status") or "")


def _ext_note(data: dict) -> tuple[dict, int]:
    """Persist an inline note edit (contacts.linkedin_message), capped server-side to 300.

    Writes linkedin_message DIRECTLY via upsert_contact — NOT _save_or_regen_draft, which
    would clobber the separate email/outreach state and has no cap.
    """
    from applypilot.networking import store
    cid = (data.get("contact_id") or "").strip()
    if not cid:
        return {"ok": False, "error": "contact_id required"}, 400
    note = str(data.get("note") or "")[:EXT_NOTE_MAX_LEN]
    store.init_contacts()
    if not store.get_contact(cid):
        return {"ok": False, "error": "contact not found"}, 404
    store.upsert_contact({"id": cid, "linkedin_message": note})
    return {"ok": True, "note": note}, 200


def _start_prepare(min_score: int) -> tuple[bool, str]:
    args = [
        sys.executable, "-c",
        "from applypilot.web_dashboard import run_dashboard_prepare; run_dashboard_prepare(validation_mode='normal')",
    ]
    return _runner.start("prepare", args)


def _start_continue(url: str) -> tuple[bool, str]:
    args = [
        sys.executable, "-c",
        f"from applypilot.web_dashboard import run_dashboard_continue; run_dashboard_continue({url!r})",
    ]
    return _runner.start("continue", args)


def _start_fill_one(url: str) -> tuple[bool, str]:
    args = [
        sys.executable, "-c",
        f"from applypilot.web_dashboard import run_dashboard_fill_one; run_dashboard_fill_one({url!r})",
    ]
    return _runner.start("fill", args)


def _start_restart(url: str) -> tuple[bool, str]:
    args = [
        sys.executable, "-c",
        f"from applypilot.web_dashboard import run_dashboard_restart; run_dashboard_restart({url!r})",
    ]
    return _runner.start("restart", args)


def _start_apply(limit: int, min_score: int, dry_run: bool, copilot: bool = True) -> tuple[bool, str]:
    args = [
        sys.executable, "-c",
        (
            "from applypilot.web_dashboard import run_dashboard_apply; "
            f"run_dashboard_apply(limit={limit}, dry_run={dry_run!r}, copilot={copilot!r})"
        ),
    ]
    return _runner.start("apply", args)


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the local dashboard."""

    server_version = "ApplyPilotDashboard/0.1"

    def log_message(self, fmt: str, *args) -> None:
        console.log(fmt % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            _html_response(self, _index_html())
            return
        if _serve_static(self, path):
            return
        if path == "/api/status":
            _json_response(self, _status_payload())
            return
        if path == "/api/material":
            query = parse_qs(parsed.query)
            _serve_material(self, query.get("path", [""])[0])
            return
        if path == EXT_QUEUE_PATH:
            # Host-loopback + shared token only. NO Origin half (the extension's
            # chrome-extension:// Origin would fail it) and NO CORS headers.
            if not _host_is_loopback(self):
                _json_response(self, {"ok": False, "error": "loopback required"}, HTTPStatus.FORBIDDEN)
                return
            if not _ext_token_ok(self):
                _json_response(self, {"ok": False, "error": "invalid or missing token"},
                               HTTPStatus.UNAUTHORIZED)
                return
            try:
                q = parse_qs(parsed.query)
                job_url = (q.get("job_url", [""])[0] or "").strip() or None
                include_skipped = (q.get("include_skipped", [""])[0] or "").lower() in {"1", "true", "yes"}
                _json_response(self, _ext_queue(job_url, include_skipped=include_skipped))
            except Exception as exc:  # noqa: BLE001
                _json_response(self, {"ok": False, "error": str(exc)},
                               HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        _json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_ext_post(self, path: str) -> None:
        """Auth + dispatch for /api/ext/status and /api/ext/note (EXT-0 frozen contract)."""
        if not _host_is_loopback(self):
            _json_response(self, {"ok": False, "error": "loopback required"}, HTTPStatus.FORBIDDEN)
            return
        if not _ext_origin_ok(self):
            _json_response(self, {"ok": False, "error": "cross-origin request rejected"},
                           HTTPStatus.FORBIDDEN)
            return
        if not _ext_token_ok(self):
            _json_response(self, {"ok": False, "error": "invalid or missing token"},
                           HTTPStatus.UNAUTHORIZED)
            return
        try:
            data = _read_json(self)
            if path == EXT_STATUS_PATH:
                payload, code = _ext_status(data)
                _json_response(self, payload, code)
                return
            if path == EXT_NOTE_PATH:
                payload, code = _ext_note(data)
                _json_response(self, payload, code)
                return
        except Exception as exc:  # noqa: BLE001
            _json_response(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        _json_response(self, {"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        # Extension API POSTs have their own auth: Host-loopback + (loopback OR chrome-extension
        # Origin) + shared token. Handled before the dashboard's Origin-only guard because the
        # extension's chrome-extension:// Origin would fail _origin_ok.
        if path.startswith("/api/ext/"):
            self._handle_ext_post(path)
            return
        # Reject cross-origin state-changing requests (guards irreversible actions).
        if not _origin_ok(self):
            _json_response(self, {"error": "cross-origin request rejected"}, HTTPStatus.FORBIDDEN)
            return
        try:
            data = _read_json(self)
            if path == "/api/network":
                url = data.get("url", "")
                per_job = int(data.get("per_job") or 5)
                use_linkedin = str(data.get("use_linkedin", "")).lower() in {"1", "true", "yes", "on"}
                if not url:
                    _json_response(self, {"ok": False, "message": "url required"}, 400)
                    return
                if not _networking_available():
                    _json_response(self, {"ok": False,
                                          "message": "Set APOLLO_API_KEY (paid plan) to find contacts"}, 409)
                    return
                ok, msg = _network.start(url, per_job, use_linkedin)
                _json_response(self, {"ok": ok, "message": msg}, 200 if ok else 409)
                return
            if path == "/api/outreach":
                _json_response(self, _save_or_regen_draft(data))
                return
            if path == "/api/contact/details":
                _json_response(self, _save_contact_details(data))
                return
            if path == "/api/contact/add-introduced":
                _json_response(self, _add_introduced_contact(data))
                return
            if path == "/api/contact/delete":
                _json_response(self, _delete_contact(data.get("contact_id", "")))
                return
            if path == "/api/contact/reply":
                _json_response(self, _send_reply(data))
                return
            if path == "/api/contact/draft-reply":
                _json_response(self, _draft_reply(data))
                return
            if path == "/api/contact/fetch-reply":
                _json_response(self, _fetch_reply_text(data))
                return
            if path == "/api/contact/interaction":
                _json_response(self, _log_interaction(data))
                return
            if path == "/api/contact/sync-gmail":
                _json_response(self, _sync_all_gmail(data))
                return
            if path == "/api/job-description":
                _json_response(self, _job_description(data))
                return
            if path == "/api/followup":
                _json_response(self, _followup_action(data))
                return
            if path == "/api/contact/followup":
                from applypilot.networking import store as _store
                cid = (data.get("contact_id") or "").strip()
                _store.init_contacts()
                if not cid or not _store.get_contact(cid):
                    _json_response(self, {"ok": False, "message": "contact not found"}, 404)
                    return
                first = _store.mark_followed_up(cid)
                _json_response(self, {"ok": True, "message": "recorded" if first
                                      else "already recorded"})
                return
            if path == "/api/contact/dm-status":
                body, code = _apply_dm_status(data.get("contact_id") or "",
                                              data.get("status") or "")
                _json_response(self, body, code)
                return
            if path == "/api/outreach/send":
                cid = data.get("contact_id", "")
                confirm = str(data.get("confirm_unverified", "")).lower() in {"1", "true", "yes", "on"}
                if not cid:
                    _json_response(self, {"ok": False, "message": "contact_id required"}, 400)
                    return
                from applypilot.networking.gmail_send import send_outreach
                res = send_outreach(cid, confirm_unverified=confirm)
                _json_response(self, res, 200 if res["ok"] else 409)
                return
            if path == "/api/outreach/send-all-emails":
                job_url = data.get("job_url", "")
                confirm = str(data.get("confirm_unverified", "")).lower() in {"1", "true", "yes", "on"}
                if not job_url:
                    _json_response(self, {"ok": False, "message": "job_url required"}, 400)
                    return
                if not _gmail_available():
                    _json_response(self, {"ok": False, "message": "Gmail not connected"}, 409)
                    return
                ids = _eligible_contact_ids(job_url, "email", confirm)
                ok, msg = _bulk_email.start(job_url, ids, confirm)
                _json_response(self, {"ok": ok, "message": msg}, 200 if ok else 409)
                return
            if path == "/api/import":
                _json_response(self, _import_urls(data.get("urls", "")))
                return
            if path == "/api/prepare":
                min_score = int(data.get("min_score") or 1)
                ok, msg = _start_prepare(min_score)
                _json_response(self, {"ok": ok, "message": msg}, 200 if ok else 409)
                return
            if path == "/api/apply":
                limit = int(data.get("limit") or 10)
                min_score = int(data.get("min_score") or 1)
                dry_run = str(data.get("dry_run", "")).lower() in {"1", "true", "yes", "on"}
                # Co-pilot (review before submit) is the default; the client can opt out for full auto.
                copilot = str(data.get("copilot", "1")).lower() in {"1", "true", "yes", "on"}
                ok, msg = _start_apply(limit, min_score, dry_run, copilot)
                _json_response(self, {"ok": ok, "message": msg}, 200 if ok else 409)
                return
            if path == "/api/mark-submitted":
                _json_response(self, _mark_submitted(data.get("url", "")))
                return
            if path == "/api/mark-rejected":
                _json_response(self, _mark_rejected(data.get("url", "")))
                return
            if path == "/api/unmark-rejected":
                _json_response(self, _unmark_rejected(data.get("url", "")))
                return
            if path == "/api/continue":
                url = (data.get("url") or "").strip()
                if not url:
                    _json_response(self, {"ok": False, "message": "url required"}, 400)
                    return
                ok, msg = _start_continue(url)
                _json_response(self, {"ok": ok, "message": msg}, 200 if ok else 409)
                return
            if path == "/api/fill-one":
                url = (data.get("url") or "").strip()
                if not url:
                    _json_response(self, {"ok": False, "message": "url required"}, 400)
                    return
                ok, msg = _start_fill_one(url)
                _json_response(self, {"ok": ok, "message": msg}, 200 if ok else 409)
                return
            if path == "/api/restart":
                url = (data.get("url") or "").strip()
                if not url:
                    _json_response(self, {"ok": False, "message": "url required"}, 400)
                    return
                ok, msg = _start_restart(url)
                _json_response(self, {"ok": ok, "message": msg}, 200 if ok else 409)
                return
            if path == "/api/delete":
                result = _delete_job(data.get("url", ""))
                _json_response(self, result, 200 if result["ok"] else 404)
                return
            if path == "/api/stop":
                ok, msg = _runner.stop()
                _json_response(self, {"ok": ok, "message": msg}, 200 if ok else 409)
                return
            if path == "/api/pause-apply":
                _json_response(self, _pause_apply())
                return
            if path == "/api/check-replies":
                res = _replies.poll_now(force_full=bool(data.get("full")))
                msg = (f"Checked {res.get('checked', 0)} thread(s) — "
                       f"{res.get('replied', 0)} new repl"
                       f"{'y' if res.get('replied') == 1 else 'ies'}."
                       if res.get("ok") else res.get("note", "could not check"))
                _json_response(self, {"ok": bool(res.get("ok")), "message": msg, **res})
                return
            if path == "/api/signin":
                url = (data.get("url") or "").strip()
                if not url:
                    _json_response(self, {"ok": False, "message": "url required"}, 400)
                    return
                _json_response(self, _start_signin(url))
                return
            if path == "/api/signin-done":
                url = (data.get("url") or "").strip()
                if not url:
                    _json_response(self, {"ok": False, "message": "url required"}, 400)
                    return
                fill = str(data.get("fill", "")).lower() in {"1", "true", "yes", "on"}
                _json_response(self, _finish_signin(url, fill=fill))
                return
        except Exception as exc:
            _json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        _json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)


def serve_dashboard(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Run the local dashboard server."""
    config.load_env()
    config.ensure_dirs()
    init_db()
    # An apply launched from here is a synchronous CHILD of this process, so anything still
    # marked in-progress from a previous run died with it and will never clear itself. Left
    # alone, the job reads "in progress" forever and acquire_job skips it — a silent retry.
    try:
        from applypilot.apply.launcher import release_stale_locks
        stale = release_stale_locks()
        if stale:
            console.print(f"[yellow]Released {len(stale)} stale in-progress apply lock(s)[/yellow] "
                          "[dim](an earlier apply was interrupted — those jobs can be re-applied)[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.log(f"[dim]stale-lock sweep skipped: {exc}[/dim]")
    # Generate the extension token up front so the operator can read it before any request
    # (the guard short-circuits on a missing header, so it would never be created lazily).
    ext_token = _ext_token()

    # Reply polling runs on its own thread from start-up. Never inside a request: a 2.5s
    # dashboard refresh cannot wait on a Gmail round-trip. Unattended polling with the
    # dashboard CLOSED is CRM-3b (`applypilot tick`), not this.
    _ok, _why = (False, "")
    try:
        from applypilot.networking import gmail_read
        _ok, _why = gmail_read.available()
    except Exception:  # noqa: BLE001
        _why = "reply detection unavailable"
    if _ok:
        _replies.start()
        console.print("[dim]Reply detection:[/dim] on (polling every 5 min)")
    else:
        console.print(f"[dim]Reply detection:[/dim] off — {_why}")

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    url = f"http://{host}:{port}/"
    console.print(f"[green]ApplyPilot dashboard running:[/green] {url}")
    console.print(f"[dim]Data directory:[/dim] {config.APP_DIR}")
    console.print(f"[dim]Extension token:[/dim] {ext_token}  [dim](paste into the extension popup)[/dim]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")
    if open_browser:
        webbrowser.open(url)

    def _shutdown(signum, frame) -> None:
        _runner.stop()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _runner.stop()
    finally:
        server.server_close()
