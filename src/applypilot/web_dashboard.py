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

    slug = _derive.employer_slug_from_url(url)
    if slug:
        return _derive.titleize_slug(slug)

    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    domain = host.split(".")
    if len(domain) >= 2:
        company = domain[-2]
        if company in {"careers", "jobs"} and len(domain) >= 3:
            company = domain[-3]
        return _derive.titleize_slug(company)
    return "Uploaded"


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
            job = dict(zip(row.keys(), row))
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
    max_attempts = config.DEFAULTS["max_apply_attempts"]
    rows = _jobs.queue_for_apply(limit, max_attempts, conn)

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

    result = {"queued": len(rows), "applied": applied, "failed": failed, "needs_review": needs_review}
    print(f"Dashboard URL apply complete: {result}", flush=True)
    return result


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
    if not url:
        return {"ok": False, "message": "url required"}
    init_db()
    conn = get_connection()
    if not _jobs.exists(url, conn):
        return {"ok": False, "message": "job not found"}
    status = _jobs.apply_status(url, conn)
    # The whole point of this endpoint: only a job the co-pilot filled and handed back may
    # be marked submitted. Without this guard the button would rubber-stamp anything.
    if status != "ready_to_submit":
        return {"ok": False, "message": f"job is not awaiting review (status: {status or 'none'})"}
    _jobs.mark_applied(url, conn)
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
                     conn_matches: dict | None = None) -> dict:
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
        contacts = [_contact_payload(c, contact_company, job_ladders, job_matches)
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
            "activity": _job_activity(row["url"], conn),
            "network_running": bool(net_task.get("running")),
            "network_note": net_task.get("note") or "",
            "network_error": net_task.get("error") or "",
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
        "gmail_available": _gmail_available(),
        # Mutual shared token for the LinkedIn extension — operator pastes it into the popup once.
        "ext_token": _ext_token(),
    }


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

    # Save an edit
    fields = {
        "id": cid, "job_url": row["job_url"],
        "outreach_subject": data.get("subject", ""),
        "outreach_message": data.get("body", ""),
        "outreach_status": "drafted",
    }
    if "linkedin" in data:
        fields["linkedin_message"] = data.get("linkedin", "")
    upsert_contact(fields)
    return {"ok": True, "message": "saved"}


_PHONE_MAX_LEN = 40
_NOTES_MAX_LEN = 2000


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
