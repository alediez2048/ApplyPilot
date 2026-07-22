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
from datetime import datetime, timezone
from hashlib import sha1
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from rich.console import Console

from applypilot import config
from applypilot.database import get_connection, init_db

console = Console()

_URL_RE = re.compile(r"https?://[^\s,<>\"']+")
_URL_QUEUE_STRATEGIES = ("dashboard_upload", "manual_url_batch")
_URL_QUEUE_SQL = "strategy IN ('dashboard_upload', 'manual_url_batch')"

# ── Extension local API (EXT-0) — frozen contract in extension/CONTRACTS.md §3.
# Paths / header / limits mirror extension/shared/constants.js (API.*, NOTE_MAX_LEN).
EXT_TOKEN_HEADER = "X-ApplyPilot-Token"
EXT_QUEUE_PATH = "/api/ext/queue"
EXT_STATUS_PATH = "/api/ext/status"
EXT_NOTE_PATH = "/api/ext/note"
EXT_NOTE_MAX_LEN = 300
# The only dm_status values the extension may POST to /api/ext/status.
_POSTABLE_DM_STATUSES = frozenset({"sent", "manual", "skipped"})


def _titleize_slug(value: str) -> str:
    value = re.sub(r"[-_]+", " ", value).strip()
    overrides = {
        "ai": "AI",
        "xai": "xAI",
        "openai": "OpenAI",
    }
    key = value.lower().replace(" ", "")
    if key in overrides:
        return overrides[key]
    return value.title()


def _infer_company(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path_parts = [p for p in parsed.path.split("/") if p]

    if "greenhouse.io" in host and len(path_parts) >= 1:
        return _titleize_slug(path_parts[0])
    if "ashbyhq.com" in host and len(path_parts) >= 1:
        return _titleize_slug(path_parts[0])
    if "lever.co" in host and len(path_parts) >= 1:
        return _titleize_slug(path_parts[0])
    if "workdayjobs.com" in host and len(path_parts) >= 1:
        return _titleize_slug(path_parts[0].split("_")[0])

    domain = host.split(".")
    if len(domain) >= 2:
        company = domain[-2]
        if company in {"careers", "jobs"} and len(domain) >= 3:
            company = domain[-3]
        return _titleize_slug(company)
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
            row = conn.execute(
                "SELECT url, title, company, site, application_url, full_description "
                "FROM jobs WHERE url = ? OR application_url = ? LIMIT 1", (job_url, job_url)
            ).fetchone()
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
            if c.get("outreach_status") == "submitted":
                continue
            if not confirm_unverified and (c.get("email_status") or "none") != "verified":
                continue  # skip unverified unless the caller opts in
        else:  # linkedin
            if not (c.get("linkedin_url") and c.get("linkedin_message")):
                continue
            if c.get("dm_status") in _DM_DONE_STATUSES:
                continue  # sent/manual/skipped are finished — don't re-offer them
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


def run_dashboard_prepare(limit: int = 0, validation_mode: str = "lenient") -> dict:
    """Prepare materials only for URLs imported through the dashboard.

    Imported URLs are treated as user-approved targets. We intentionally bypass
    broad discovery and fit scoring here so the dashboard cannot spend time or
    tokens on older researched jobs.
    """
    config.load_env()
    config.ensure_dirs()
    init_db()
    conn = get_connection()

    pending_detail = conn.execute(
        f"""
        SELECT url, title, site
        FROM jobs
        WHERE {_URL_QUEUE_SQL}
          AND detail_scraped_at IS NULL
        ORDER BY discovered_at DESC, rowid DESC
        """
    ).fetchall()
    if limit > 0:
        pending_detail = pending_detail[:limit]

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

    now = datetime.now(timezone.utc).isoformat()
    scored = conn.execute(
        f"""
        UPDATE jobs
        SET fit_score = 10,
            score_reasoning = 'User-imported URL. Fit scoring intentionally bypassed.',
            scored_at = ?
        WHERE {_URL_QUEUE_SQL}
          AND full_description IS NOT NULL
          AND fit_score IS NULL
        """,
        (now,),
    ).rowcount
    conn.commit()
    print(f"STAGE: score bypass - marked {scored} imported URL(s) as user-approved", flush=True)

    profile = None
    resume_text = None
    tailored = 0
    tailor_errors = 0

    tailor_rows = conn.execute(
        f"""
        SELECT *
        FROM jobs
        WHERE {_URL_QUEUE_SQL}
          AND full_description IS NOT NULL
          AND tailored_resume_path IS NULL
          AND COALESCE(tailor_attempts, 0) < 5
        ORDER BY discovered_at DESC, rowid DESC
        """
    ).fetchall()
    if limit > 0:
        tailor_rows = tailor_rows[:limit]
    tailor_jobs = _rows_to_dicts(tailor_rows)

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

                if report.get("status") in {"approved", "approved_with_judge_warning"}:
                    conn.execute(
                        "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                        (str(txt_path), now, job["url"]),
                    )
                    tailored += 1
                else:
                    conn.execute("UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?", (job["url"],))
                    tailor_errors += 1
                conn.commit()
            except Exception as exc:
                conn.execute("UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?", (job["url"],))
                conn.commit()
                tailor_errors += 1
                print(f"  tailor error: {exc}", flush=True)

    cover_rows = conn.execute(
        f"""
        SELECT *
        FROM jobs
        WHERE {_URL_QUEUE_SQL}
          AND full_description IS NOT NULL
          AND tailored_resume_path IS NOT NULL
          AND (cover_letter_path IS NULL OR cover_letter_path = '')
          AND COALESCE(cover_attempts, 0) < 5
        ORDER BY discovered_at DESC, rowid DESC
        """
    ).fetchall()
    if limit > 0:
        cover_rows = cover_rows[:limit]
    cover_jobs = _rows_to_dicts(cover_rows)

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
                conn.execute(
                    "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                    (str(cl_path), now, job["url"]),
                )
                conn.commit()
                covers += 1
            except Exception as exc:
                conn.execute("UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?", (job["url"],))
                conn.commit()
                cover_errors += 1
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


def run_dashboard_apply(limit: int = 10, dry_run: bool = False) -> dict:
    """Apply only to prepared jobs imported through the dashboard URL box."""
    config.load_env()
    config.ensure_dirs()
    init_db()
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT url, title, site
        FROM jobs
        WHERE strategy = 'dashboard_upload'
          AND tailored_resume_path IS NOT NULL
          AND applied_at IS NULL
          AND (apply_status IS NULL OR apply_status = '')
        ORDER BY discovered_at DESC, rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    print(f"Dashboard URL apply queue: {len(rows)} job(s)", flush=True)
    applied = 0
    failed = 0
    for index, row in enumerate(rows, 1):
        print(f"\n=== Applying {index}/{len(rows)}: {row['site']} / {row['title']} ===", flush=True)
        print(row["url"], flush=True)
        args = [sys.executable, "-m", "applypilot.cli", "apply", "--url", row["url"], "--min-score", "1"]
        if dry_run:
            args.append("--dry-run")
        completed = subprocess.run(args, check=False)
        status = conn.execute("SELECT apply_status, applied_at FROM jobs WHERE url = ?", (row["url"],)).fetchone()
        if status and status["applied_at"]:
            applied += 1
        else:
            failed += 1
        print(f"=== Finished {index}/{len(rows)} with exit code {completed.returncode} ===", flush=True)

    result = {"queued": len(rows), "applied": applied, "failed": failed}
    print(f"Dashboard URL apply complete: {result}", flush=True)
    return result


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
    handler.end_headers()
    handler.wfile.write(body)


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


def _networking_available() -> bool:
    from applypilot.networking import providers
    return providers.available()


def _gmail_available() -> bool:
    """True if any Gmail send transport (OAuth or SMTP app-password) is ready."""
    from applypilot.networking import gmail_send
    return gmail_send.transport() is not None


def _contact_payload(c: dict, company: str | None = None) -> dict:
    from applypilot.networking import connections
    conn_rec = connections.match(c.get("full_name"), company)
    return {
        "id": c.get("id") or "",
        "full_name": c.get("full_name") or "",
        "title": c.get("title") or "",
        "email": c.get("email") or "",
        "email_status": c.get("email_status") or "none",
        "linkedin_url": c.get("linkedin_url") or "",
        "match_reason": c.get("match_reason") or "",
        "outreach_subject": c.get("outreach_subject") or "",
        "outreach_message": c.get("outreach_message") or "",
        "linkedin_message": c.get("linkedin_message") or "",
        "outreach_status": c.get("outreach_status") or "none",
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
    }


def _status_payload() -> dict:
    init_db()
    conn = get_connection()
    from applypilot.networking.store import init_contacts, get_contacts_for_job
    from applypilot.networking import derive as _derive
    init_contacts(conn)
    _net_tasks = _network.statuses()

    stats = conn.execute(f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN full_description IS NOT NULL AND lower(trim(full_description)) != 'null' THEN 1 ELSE 0 END) AS enriched,
          SUM(CASE WHEN fit_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
          SUM(CASE WHEN tailored_resume_path IS NOT NULL THEN 1 ELSE 0 END) AS tailored,
          SUM(CASE WHEN cover_letter_path IS NOT NULL THEN 1 ELSE 0 END) AS covers,
          SUM(CASE WHEN tailored_resume_path IS NOT NULL AND applied_at IS NULL AND (apply_status IS NULL OR apply_status = '') THEN 1 ELSE 0 END) AS ready,
          SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS applied,
          SUM(CASE WHEN apply_error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
          SUM(CASE WHEN apply_status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress
        FROM jobs
        WHERE {_URL_QUEUE_SQL}
    """).fetchone()
    lifetime = conn.execute("""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS applied,
          SUM(CASE WHEN apply_error IS NOT NULL THEN 1 ELSE 0 END) AS errors
        FROM jobs
    """).fetchone()

    rows = conn.execute(f"""
        SELECT url, title, site, salary, location, full_description, application_url, detail_error,
               fit_score, score_reasoning, tailored_resume_path, cover_letter_path,
               apply_status, apply_error, apply_attempts, applied_at,
               last_attempted_at, apply_duration_ms
        FROM jobs
        WHERE {_URL_QUEUE_SQL}
        ORDER BY
          CASE
            WHEN applied_at IS NOT NULL THEN 0
            WHEN apply_status = 'in_progress' THEN 1
            WHEN tailored_resume_path IS NOT NULL THEN 2
            WHEN {_URL_QUEUE_SQL} AND (full_description IS NULL OR lower(trim(full_description)) = 'null') THEN 3
            WHEN fit_score IS NOT NULL THEN 4
            ELSE 5
          END,
          discovered_at DESC,
          fit_score DESC NULLS LAST
        LIMIT 500
    """).fetchall()

    jobs: list[dict] = []
    for row in rows:
        status = row["apply_status"] or ""
        if row["applied_at"]:
            status = "applied"
        elif row["apply_error"]:
            status = status or "failed"
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
        job_row = dict(zip(row.keys(), row))
        contact_company = _derive.derive_company(job_row) or row["site"] or ""
        contacts = [_contact_payload(c, contact_company)
                    for c in get_contacts_for_job(row["url"], conn)]
        from applypilot.networking import connections as _conns
        net_task = _net_tasks.get(row["url"], {})
        jobs.append({
            "url": row["url"],
            "title": row["title"] or "Untitled",
            "company": row["site"] or "",
            "contact_company": contact_company,
            "connections_at_company": _conns.count_at_company(contact_company),
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
            "last_attempted_at": row["last_attempted_at"] or "",
            "materials": materials,
            "contacts": contacts,
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

    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    duplicates = 0
    existing_applied = 0
    existing_failed = 0
    existing_ready = 0
    existing_pending = 0

    for url in urls:
        existing = conn.execute(
            "SELECT url, strategy, applied_at, apply_status, apply_error, tailored_resume_path FROM jobs WHERE url = ? OR application_url = ?",
            (url, url),
        ).fetchone()
        if existing:
            if existing["strategy"] != "dashboard_upload":
                conn.execute(
                    """
                    UPDATE jobs
                    SET strategy = 'dashboard_upload',
                        discovered_at = ?,
                        application_url = COALESCE(NULLIF(application_url, ''), ?)
                    WHERE url = ? OR application_url = ?
                    """,
                    (now, url, url, url),
                )
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
            conn.execute(
                "INSERT INTO jobs (url, title, company, site, strategy, discovered_at, application_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (url, title, company, company, "dashboard_upload", now, url),
            )
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
    row = conn.execute("SELECT id, job_url FROM contacts WHERE id = ?", (cid,)).fetchone()
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


def _delete_job(url: str) -> dict:
    init_db()
    conn = get_connection()
    from applypilot.networking.store import init_contacts
    init_contacts(conn)
    if not url:
        return {"ok": False, "message": "Missing job URL"}

    row = conn.execute(
        f"SELECT title, site FROM jobs WHERE url = ? AND {_URL_QUEUE_SQL}",
        (url,),
    ).fetchone()
    if not row:
        return {"ok": False, "message": "Application not found"}

    conn.execute(f"DELETE FROM jobs WHERE url = ? AND {_URL_QUEUE_SQL}", (url,))
    conn.execute("DELETE FROM contacts WHERE job_url = ?", (url,))  # no SQLite FK cascade
    conn.commit()
    return {
        "ok": True,
        "message": f"Deleted {row['site'] or 'Unknown'} - {row['title'] or 'Untitled'}",
    }


# ── Extension local API handlers (EXT-0) ─────────────────────────────────────
# Loopback + shared-token guarded; frozen contract in extension/CONTRACTS.md §3.

def _queue_contact_payload(c: dict) -> dict:
    """One /api/ext/queue row. `note` = contacts.linkedin_message (the verbatim invite note)."""
    return {
        "id": c.get("id") or "",
        "full_name": c.get("full_name") or "",
        "title": c.get("title") or "",
        "company": c.get("company") or "",
        "linkedin_url": c.get("linkedin_url") or "",
        "note": c.get("linkedin_message") or "",
    }


def _ext_queue(job_url: str | None) -> dict:
    """Ready LinkedIn contacts. Per-job (via _eligible_contact_ids) or all-jobs (deduped)."""
    from applypilot.networking.store import _norm_linkedin, get_contact, init_contacts
    init_db()
    conn = get_connection()
    init_contacts(conn)

    if job_url:
        # Per-job: reuse the shared eligibility helper (linkedin_url + note + not done-set).
        contacts = [get_contact(cid, conn) for cid in _eligible_contact_ids(job_url, "linkedin")]
    else:
        # All-jobs variant: single SELECT over contacts, then dedupe by normalized profile URL
        # so the same person surfaced under two jobs yields exactly one queue row.
        placeholders = ", ".join("?" for _ in _DM_DONE_STATUSES)
        rows = conn.execute(
            "SELECT * FROM contacts "
            "WHERE linkedin_url IS NOT NULL AND trim(linkedin_url) != '' "
            "AND linkedin_message IS NOT NULL AND trim(linkedin_message) != '' "
            f"AND (dm_status IS NULL OR dm_status NOT IN ({placeholders})) "
            "ORDER BY discovered_at ASC",
            tuple(_DM_DONE_STATUSES),
        ).fetchall()
        contacts = []
        seen: set[str] = set()
        for r in rows:
            c = dict(zip(r.keys(), r))
            norm = _norm_linkedin(c.get("linkedin_url"))
            if norm in seen:
                continue
            seen.add(norm)
            contacts.append(c)

    return {"ok": True, "contacts": [_queue_contact_payload(c) for c in contacts if c]}


def _ext_status(data: dict) -> tuple[dict, int]:
    """Map a reported send status to the store's dm_* helpers (sent/manual/skipped)."""
    from applypilot.networking import store
    cid = (data.get("contact_id") or "").strip()
    status = (data.get("status") or "").strip()
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
        "from applypilot.web_dashboard import run_dashboard_prepare; run_dashboard_prepare(validation_mode='lenient')",
    ]
    return _runner.start("prepare", args)


def _start_apply(limit: int, min_score: int, dry_run: bool) -> tuple[bool, str]:
    args = [
        sys.executable, "-c",
        (
            "from applypilot.web_dashboard import run_dashboard_apply; "
            f"run_dashboard_apply(limit={limit}, dry_run={dry_run!r})"
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
            _html_response(self, _INDEX_HTML)
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
                job_url = (parse_qs(parsed.query).get("job_url", [""])[0] or "").strip() or None
                _json_response(self, _ext_queue(job_url))
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
                ok, msg = _start_apply(limit, min_score, dry_run)
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


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ApplyPilot Operator</title>
<style>
  :root {
    color-scheme: light;
    --bg:#f4f2ee;
    --surface:#ffffff;
    --surface2:#f8f9fa;
    --surface3:#eef3f8;
    --line:#e0dfdc;
    --line2:#d0cfcb;
    --text:rgba(0,0,0,.9);
    --muted:rgba(0,0,0,.6);
    --faint:rgba(0,0,0,.45);
    --soft:rgba(0,0,0,.75);
    --accent:#0a66c2;
    --accent2:#0a66c2;
    --accent-hover:#004182;
    --accent-soft:#e8f0fa;
    --green:#057642;
    --green-soft:#ddf5e5;
    --red:#b91c1c;
    --red-soft:#fbeae8;
    --yellow:#915907;
    --yellow-soft:#fef3e0;
    --blue:#0a66c2;
    --shadow:0 0 0 1px rgba(0,0,0,.08), 0 2px 4px rgba(0,0,0,.04);
    --shadow-sm:0 0 0 1px rgba(0,0,0,.08);
    --font:-apple-system,system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  * { box-sizing:border-box; }
  body {
    margin:0;
    font:14px/1.45 var(--font);
    background:var(--bg);
    color:var(--text);
    -webkit-font-smoothing:antialiased;
  }
  header {
    padding:0 24px;
    height:56px;
    border-bottom:1px solid var(--line);
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:18px;
    background:var(--surface);
    position:sticky;
    top:0;
    z-index:20;
    box-shadow:0 1px 2px rgba(0,0,0,.04);
  }
  .brand { display:flex; align-items:center; gap:12px; }
  .logo { width:36px; height:36px; border-radius:8px; background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center; font-size:19px; box-shadow:0 1px 2px rgba(10,102,194,.35); flex-shrink:0; }
  .brand-text { display:flex; flex-direction:column; line-height:1.15; }
  .brand-name { font-size:19px; font-weight:700; letter-spacing:-.2px; color:var(--text); }
  .brand-sub { font-size:12px; color:var(--muted); }
  .eyebrow { display:none; }
  h1 { font-size:20px; line-height:1.1; margin:0; font-weight:600; }
  .subtitle { color:var(--muted); max-width:720px; }
  main { max-width:1280px; margin:0 auto; padding:20px 24px 40px; display:grid; gap:16px; min-width:0; }
  .stats { display:grid; grid-template-columns:repeat(8, minmax(96px,1fr)); gap:12px; }
  .stat {
    background:var(--surface);
    border:1px solid var(--line);
    border-radius:10px;
    padding:14px 16px;
    min-height:74px;
    transition:box-shadow .15s ease;
  }
  .stat:hover { box-shadow:var(--shadow); }
  .stat strong { display:block; font-size:26px; line-height:1; margin-bottom:6px; font-weight:600; color:var(--text); }
  .stat span { color:var(--muted); font-size:12px; font-weight:500; }
  .progress-panel {
    display:grid;
    gap:14px;
    background:var(--surface);
    border:1px solid var(--line);
    border-radius:10px;
    padding:20px;
  }
  .progress-head { display:flex; justify-content:space-between; align-items:center; gap:12px; }
  .progress-title { font-weight:600; font-size:16px; color:var(--text); }
  .progress-label { color:var(--muted); font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .progress-track { height:8px; border-radius:999px; background:#e9e5df; overflow:hidden; }
  .progress-fill { height:100%; width:0%; border-radius:999px; background:var(--accent); transition:width .35s ease; }
  .progress-meta { display:flex; gap:8px; flex-wrap:wrap; color:var(--muted); font-size:12px; }
  .job-chip { display:inline-flex; gap:6px; align-items:center; border:1px solid var(--line); background:var(--surface2); border-radius:999px; padding:4px 12px; color:var(--soft); font-weight:500; }
  .pulse {
    width:8px;
    height:8px;
    border-radius:50%;
    background:var(--accent);
    box-shadow:0 0 0 rgba(59,167,255,.55);
    animation:pulse 1.35s infinite;
  }
  @keyframes pulse {
    0% { box-shadow:0 0 0 0 rgba(59,167,255,.45); }
    70% { box-shadow:0 0 0 8px rgba(59,167,255,0); }
    100% { box-shadow:0 0 0 0 rgba(59,167,255,0); }
  }
  .controls { display:grid; grid-template-columns:1fr; gap:16px; }
  section {
    background:var(--surface);
    border:1px solid var(--line);
    border-radius:10px;
    padding:20px;
    min-width:0;
  }
  h2 { margin:0 0 16px; font-size:16px; font-weight:600; color:var(--text); }
  textarea {
    width:100%;
    height:120px;
    resize:vertical;
    border:1px solid var(--line2);
    border-radius:8px;
    background:var(--surface);
    color:var(--text);
    padding:12px;
    outline:none;
    font:14px/1.45 var(--font);
    transition:border-color .15s ease, box-shadow .15s ease;
  }
  textarea:focus, input:focus { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-soft); }
  input, select {
    background:var(--surface);
    color:var(--text);
    border:1px solid var(--line2);
    border-radius:6px;
    padding:8px 10px;
    outline:none;
    font:14px var(--font);
  }
  /* LinkedIn-style pill buttons: base = outlined, .primary = filled blue, .danger = red outline. */
  button {
    border:1px solid var(--line2);
    border-radius:999px;
    padding:6px 16px;
    min-height:34px;
    background:var(--surface);
    color:var(--soft);
    cursor:pointer;
    font-weight:600;
    font-size:14px;
    font-family:var(--font);
    transition:background .15s ease, border-color .15s ease, box-shadow .15s ease;
  }
  button:hover { background:rgba(0,0,0,.05); border-color:var(--line2); box-shadow:inset 0 0 0 1px var(--line2); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.primary:hover { background:var(--accent-hover); border-color:var(--accent-hover); box-shadow:none; }
  button.secondary { background:transparent; border:1px solid var(--accent); color:var(--accent); }
  button.secondary:hover { background:var(--accent-soft); box-shadow:inset 0 0 0 1px var(--accent); }
  button.danger { background:transparent; border:1px solid #d99; color:var(--red); }
  button.danger:hover { background:var(--red-soft); box-shadow:inset 0 0 0 1px #d99; }
  button:disabled { opacity:.55; cursor:not-allowed; box-shadow:none; }
  .row { display:flex; gap:9px; align-items:center; flex-wrap:wrap; }
  .hint { color:var(--muted); font-size:13px; margin-top:10px; }
  button.linklike { background:none; border:none; color:var(--accent); padding:0; min-height:0; font-size:13px; font-weight:600; cursor:pointer; }
  button.linklike:hover { background:none; box-shadow:none; text-decoration:underline; }
  .people-details > summary { list-style:none; cursor:pointer; padding:6px 4px; user-select:none; display:flex; align-items:center; gap:8px; border-radius:6px; }
  .people-details > summary::-webkit-details-marker { display:none; }
  .people-details > summary:hover { background:#f1f5f9; }
  .people-caret { display:inline-block; transition:transform .15s ease; color:var(--muted); font-size:11px; }
  .people-details[open] > summary .people-caret { transform:rotate(90deg); }
  .people-count { color:var(--muted); font-size:12px; font-weight:500; }
  .people-body { padding-top:6px; }
  /* --- Pipeline visualizer --- */
  .pipeline { margin-top:12px; border:1px solid var(--line); border-radius:10px; padding:14px; background:#fbfdff; }
  .pipe-steps { display:flex; align-items:flex-start; gap:0; flex-wrap:nowrap; overflow-x:auto; }
  .pipe-step { display:flex; flex-direction:column; align-items:center; text-align:center; min-width:88px; flex:1; position:relative; }
  .pipe-step .pnode { width:38px; height:38px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:16px; font-weight:700;
      border:2px solid #cbd5e1; background:#fff; color:#94a3b8; transition:all .2s ease; z-index:1; }
  .pipe-step .plabel { font-size:12px; font-weight:600; margin-top:6px; color:#475569; }
  .pipe-step .pcount { font-size:11px; color:var(--muted); margin-top:2px; min-height:14px; }
  /* connector line to the NEXT node */
  .pipe-step:not(:last-child)::after { content:''; position:absolute; top:19px; left:calc(50% + 19px); right:calc(-50% + 19px); height:2px; background:#cbd5e1; z-index:0; }
  .pipe-step.done::after { background:#22c55e; }
  .pipe-step.idle    .pnode { border-color:#d0cfcb; color:var(--faint); background:#fff; }
  .pipe-step.active  .pnode { border-color:var(--accent); color:var(--accent); background:var(--accent-soft); box-shadow:0 0 0 4px rgba(10,102,194,.12); }
  .pipe-step.done    .pnode { border-color:var(--green); color:#fff; background:var(--green); }
  .pipe-step.failed  .pnode { border-color:var(--red); color:#fff; background:var(--red); }
  .pipe-step.active  .plabel { color:var(--accent); }
  .pipe-step.done    .plabel { color:var(--green); }
  .pipe-step.failed  .plabel { color:var(--red); }
  .pipe-step.done::after { background:var(--green); }
  .pipe-spin { width:16px; height:16px; border:2px solid rgba(10,102,194,.3); border-top-color:var(--accent); border-radius:50%; animation:pipespin .7s linear infinite; }
  @keyframes pipespin { to { transform:rotate(360deg); } }
  .pipe-log-wrap { margin-top:12px; }
  .pipe-log { background:#0f172a; color:#cbd5e1; border-radius:8px; padding:10px 12px; margin:0; max-height:150px; overflow-y:auto;
      font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; word-break:break-word; }
  .pipe-log .lg-stage { color:#7dd3fc; font-weight:600; }
  .pipe-log .lg-ok { color:#4ade80; }
  .pipe-log .lg-err { color:#f87171; }
  .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:10px; background:var(--surface); max-width:100%; }
  table { width:100%; border-collapse:collapse; min-width:1320px; }
  th, td { border-bottom:1px solid var(--line); padding:12px; text-align:left; vertical-align:top; }
  th {
    color:var(--muted);
    font-size:12px;
    font-weight:600;
    background:var(--surface2);
    position:sticky;
    top:0;
    z-index:1;
  }
  tbody tr:hover td { background:#f8f9fa; }
  td.desc { color:var(--muted); max-width:370px; }
  td.people button { white-space:nowrap; }
  td.people .neterr { color:var(--red); font-size:11px; margin-top:3px; max-width:150px; }
  tr.contacts-row td { background:var(--surface2); padding:0; }
  .contacts-wrap { padding:8px 12px 12px; }
  .contact { margin-top:8px; display:flex; gap:12px; align-items:flex-start; padding:10px 12px; background:var(--surface); border:1px solid var(--line); border-radius:10px; }
  .cavatar { width:44px; height:44px; border-radius:50%; flex-shrink:0; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; font-size:15px; }
  .cbody { flex:1; min-width:0; }
  .cname { font-weight:600; color:var(--text); font-size:15px; }
  .cname .ctitle { font-weight:400; color:var(--muted); font-size:14px; }
  .cmeta { font-size:13px; color:var(--muted); margin-top:2px; }
  .chip { display:inline-block; margin-left:6px; padding:1px 9px; border-radius:999px; background:var(--surface3); color:var(--accent); font-size:11px; font-weight:500; vertical-align:middle; }
  .chip.conn { background:var(--green-soft); color:var(--green); font-weight:600; }
  .contact.is-conn { border-color:#a9e0c2; background:#f3fbf6; }
  .conn-hint { margin-left:10px; font-size:12px; color:var(--green); font-weight:600; }
  .bulkbar { display:flex; gap:8px; align-items:center; margin:8px 0 10px; flex-wrap:wrap; }
  .bulkbar .bulk { font-size:12px; }
  .bulknote { font-size:11px; color:#555; }
  .ebadge { display:inline-block; padding:0 6px; border-radius:8px; font-size:10px; }
  .ebadge.ok { background:#e6f7ef; color:#137a4b; }
  .ebadge.warn { background:#fff5e6; color:#9a6b00; }
  .ebadge.none { background:#eef0f2; color:#68727c; }
  .draft { margin-top:5px; max-width:560px; }
  .draft .d-subj { width:100%; font-size:12px; padding:4px 6px; border:1px solid #d7e2ec; border-radius:5px; margin-bottom:3px; }
  .draft .d-body { width:100%; font-size:12px; padding:5px 6px; border:1px solid #d7e2ec; border-radius:5px; font-family:inherit; resize:vertical; }
  .draft .d-style { width:100%; font-size:12px; padding:5px 8px; border:1px dashed var(--accent); border-radius:6px; margin:5px 0 3px; background:var(--accent-soft); color:var(--text); }
  .draft .d-style::placeholder { color:var(--accent); opacity:.8; }
  .draft .dbtns { margin-top:3px; display:flex; gap:6px; flex-wrap:wrap; }
  .draft .dbtns button { font-size:11px; padding:2px 9px; }
  .draft .dbtns button.send { background:linear-gradient(180deg,#eef7ff,#dcefff); border-color:#a9cdf0; color:#1a5aa0; font-weight:600; }
  .draft .sent-tag { font-size:11px; color:#137a4b; font-weight:600; align-self:center; }
  .draft .d-label { font-size:11px; font-weight:600; color:#55707f; margin:6px 0 2px; text-transform:uppercase; letter-spacing:.4px; }
  .draft .d-count { font-weight:400; color:#8a97a2; text-transform:none; letter-spacing:0; }
  .draft .d-count.over { color:#c0392b; font-weight:700; }
  .draft .d-linkedin { width:100%; font-size:12px; padding:5px 6px; border:1px solid #d7e2ec; border-radius:5px; font-family:inherit; resize:vertical; }
  .badge {
    display:inline-block;
    padding:3px 10px;
    border-radius:999px;
    font-size:12px;
    font-weight:600;
    background:var(--surface3);
    color:var(--soft);
  }
  .applied { background:var(--green-soft); color:var(--green); }
  .failed { background:var(--red-soft); color:var(--red); }
  .ready { background:var(--accent-soft); color:var(--accent); }
  .in_progress { background:var(--yellow-soft); color:var(--yellow); }
  .logs { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  pre {
    margin:0;
    min-height:200px;
    max-height:380px;
    overflow:auto;
    white-space:pre-wrap;
    background:#1d2226;
    border:1px solid #2b3236;
    border-radius:10px;
    padding:14px;
    color:#c9d1d3;
    font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  a { color:var(--accent); text-decoration:none; font-weight:500; }
  a:hover { text-decoration:underline; color:var(--accent-hover); }
  .path-pill {
    color:var(--muted);
    font-size:12px;
    background:var(--surface2);
    border:1px solid var(--line);
    border-radius:999px;
    padding:6px 12px;
    max-width:520px;
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap;
  }
  @media (max-width: 1100px) {
    .stats { grid-template-columns:repeat(2,1fr); }
    .logs { grid-template-columns:1fr; }
    .path-pill { max-width:200px; }
    .brand-sub { display:none; }
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="logo">✈</div>
    <div class="brand-text">
      <div class="brand-name">ApplyPilot</div>
      <div class="brand-sub">Autonomous Application Console</div>
    </div>
  </div>
  <div id="appDir" class="path-pill"></div>
</header>
<main>
  <div class="stats" id="stats"></div>

  <div class="progress-panel">
    <div class="progress-head">
      <div>
        <div class="progress-title">Pipeline Progress</div>
        <div id="progressLabel" class="progress-label">Idle</div>
      </div>
      <div id="progressPercent" class="path-pill">0%</div>
    </div>
    <div class="progress-track"><div id="progressFill" class="progress-fill"></div></div>
    <div id="progressMeta" class="progress-meta"></div>
  </div>

  <div class="controls">
    <section>
      <h2>Apply to Jobs</h2>
      <textarea id="urls" placeholder="Paste one or more job URLs here, then click Run. Direct ATS/career URLs work best."></textarea>
      <div class="row" style="margin-top:10px; align-items:center">
        <button id="runBtn" class="primary" style="font-size:15px; padding:10px 20px" onclick="runEverything()">🚀 Import, Prepare &amp; Apply</button>
        <button class="danger" onclick="stopCommand()">Stop</button>
        <label style="margin-left:12px"><input id="dryRun" type="checkbox"> Dry run (fill, don't submit)</label>
      </div>
      <p id="command" class="hint" style="margin-top:10px; font-weight:600; color:#374151"></p>
      <div id="pipeline" class="pipeline" style="display:none">
        <div id="pipeSteps" class="pipe-steps"></div>
        <div class="pipe-log-wrap"><pre id="pipeLog" class="pipe-log"></pre></div>
      </div>
      <div id="importStatus" class="hint"></div>
      <div class="hint">One click runs the whole chain: import the URLs → prepare tailored résumé + cover letter → apply via the visible Chrome flow. Advanced: <button class="linklike" onclick="toggleAdvanced()">show step controls</button></div>
      <div id="advancedControls" style="display:none; margin-top:8px" class="row">
        <label>Limit <input id="limit" type="number" value="10" min="1" max="100" style="width:72px"></label>
        <button id="prepareBtn" onclick="prepareJobs()">Prepare only</button>
        <button id="applyBtn" onclick="applyJobs()">Apply only</button>
      </div>
    </section>
  </div>

  <section>
    <h2>Applications</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Status</th><th>Company</th><th>Title</th><th>Salary</th><th>Location</th><th>Description</th><th>Materials</th><th>People</th><th>Error</th><th>Links</th><th>Actions</th></tr></thead>
        <tbody id="jobs"></tbody>
      </table>
    </div>
  </section>

  <div class="logs">
    <section><h2>Command Log</h2><pre id="cmdLog"></pre></section>
    <section><h2>Apply Log</h2><pre id="applyLog"></pre></section>
  </div>
</main>

<script>
async function post(path, payload) {
  const res = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload || {})});
  const data = await res.json();
  if (!res.ok) alert(data.error || data.message || 'Request failed');
  return data;
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function importUrls() {
  const status = document.getElementById('importStatus');
  status.textContent = 'Importing URLs...';
  const data = await post('/api/import', {urls: document.getElementById('urls').value});
  const existingParts = [
    `${data.existing_applied || 0} applied`,
    `${data.existing_failed || 0} failed`,
    `${data.existing_ready || 0} ready`,
    `${data.existing_pending || 0} pending`
  ].join(', ');
  status.textContent = `Found ${data.found || 0} URL(s). Imported ${data.inserted || 0}; existing ${data.duplicates || 0} (${existingParts}).`;
  await refresh();
}
// ---- Pipeline visualizer: a live stepper (Import → Enrich → Tailor → Cover → Apply) ----
const PIPE_STAGES = [
  { key: 'import', label: 'Import', icon: '1', stat: 'total' },
  { key: 'enrich', label: 'Enrich', icon: '2', stat: 'enriched' },
  { key: 'tailor', label: 'Tailor', icon: '3', stat: 'tailored' },
  { key: 'cover',  label: 'Cover',  icon: '4', stat: 'covers' },
  { key: 'apply',  label: 'Apply',  icon: '5', stat: 'applied' },
];
const _PIPE_ORDER = { idle: 0, active: 1, done: 2, failed: 3 };
let PIPE_STATUS = {};
let PIPE_STATS = {};

function pipeShow(on) { document.getElementById('pipeline').style.display = on ? 'block' : 'none'; }
function pipeReset() {
  PIPE_STATUS = {}; PIPE_STAGES.forEach(s => PIPE_STATUS[s.key] = 'idle');
  document.getElementById('pipeLog').innerHTML = ''; pipeShow(true); pipeRender();
}
function pipeSet(key, status) { PIPE_STATUS[key] = status; pipeRender(); }
// Monotonic upgrade — never downgrade a stage (log for a later command won't reset earlier ones).
function pipeUp(key, status) {
  if ((_PIPE_ORDER[status] || 0) > (_PIPE_ORDER[PIPE_STATUS[key]] || 0)) PIPE_STATUS[key] = status;
}
function pipeRender() {
  document.getElementById('pipeSteps').innerHTML = PIPE_STAGES.map(s => {
    const st = PIPE_STATUS[s.key] || 'idle';
    const inner = st === 'active' ? '<span class="pipe-spin"></span>' : st === 'done' ? '✓' : st === 'failed' ? '✗' : s.icon;
    const cnt = PIPE_STATS[s.stat] != null ? `${PIPE_STATS[s.stat]}` : '';
    return `<div class="pipe-step ${st}"><div class="pnode">${inner}</div><div class="plabel">${s.label}</div><div class="pcount">${cnt}</div></div>`;
  }).join('');
}
function pipeRenderLog(lines) {
  const box = document.getElementById('pipeLog');
  box.innerHTML = (lines || []).map(l => {
    const e = esc(l);
    if (/^STAGE:/.test(l)) return `<span class="lg-stage">${e}</span>`;
    if (/RESULT:APPLIED|complete ✓|success|✓/i.test(l)) return `<span class="lg-ok">${e}</span>`;
    if (/error|fail|429|400|denied|not found/i.test(l)) return `<span class="lg-err">${e}</span>`;
    return e;
  }).join('\n');
  box.scrollTop = box.scrollHeight;
}
// Derive enrich/tailor/cover sub-progress from the backend's "STAGE:" log lines (monotonic).
function advanceStagesFromLog(lines) {
  const txt = (lines || []).join('\n');
  if (/STAGE:\s*enrich/i.test(txt)) pipeUp('enrich', 'active');
  if (/STAGE:\s*(tailor|score bypass)/i.test(txt)) { pipeUp('enrich', 'done'); pipeUp('tailor', 'active'); }
  if (/STAGE:\s*cover/i.test(txt)) { pipeUp('enrich', 'done'); pipeUp('tailor', 'done'); pipeUp('cover', 'active'); }
  if (/prepare complete/i.test(txt)) { pipeUp('enrich', 'done'); pipeUp('tailor', 'done'); pipeUp('cover', 'done'); }
}

// Poll /api/status until the background command (prepare/apply) finishes, keeping the status
// line + pipeline visualizer live the whole time and refreshing the table so materials appear
// the moment they're ready. Resolves with the final command object.
async function pollCommandUntilDone(label) {
  const cmdEl = document.getElementById('command');
  for (let i = 0; i < 600; i++) { // ~20 min ceiling (2s * 600)
    const data = await (await fetch('/api/status')).json();
    const c = data.command || {};
    PIPE_STATS = data.stats || {};
    pipeRenderLog(c.log || []);
    advanceStagesFromLog(c.log || []);
    pipeRender();
    await refresh();
    if (c.running) {
      cmdEl.textContent = `${label}… running (${i * 2}s)`;
    } else {
      const rc = c.returncode;
      cmdEl.textContent = rc === 0 || rc == null ? `${label} complete ✓` : `${label} failed (exit ${rc}) — see log below`;
      return c;
    }
    await new Promise(r => setTimeout(r, 2000));
  }
  cmdEl.textContent = `${label} still running — check the log below`;
  return null;
}

async function prepareJobs() {
  const btn = document.getElementById('prepareBtn');
  const cmdEl = document.getElementById('command');
  const data = await post('/api/prepare', {});
  if (!data.ok) { cmdEl.textContent = data.message || 'Could not start prepare'; return; }
  if (btn) btn.disabled = true;
  cmdEl.textContent = 'Preparing materials… (enrich → tailor → cover, ~30–60s)';
  await pollCommandUntilDone('Prepare materials');
  if (btn) btn.disabled = false;
}

async function applyJobs() {
  const cmdEl = document.getElementById('command');
  // Guard: apply only works on jobs that are already prepared (tailored + cover). If none are
  // Ready, launching apply just silently does nothing — so tell the user instead of no-op'ing.
  const status = await (await fetch('/api/status')).json();
  const ready = (status.stats || {}).ready || 0;
  const dryRun = document.getElementById('dryRun').checked;
  if (ready < 1) {
    cmdEl.textContent = 'No prepared materials to apply with. Click "Prepare Materials" first and wait for it to finish.';
    alert('Nothing is ready to apply yet.\n\nClick "Prepare Materials" first and wait for "Prepare materials complete ✓", then Apply.');
    return;
  }
  if (!dryRun && !confirm(`Submit real application(s) for ${ready} prepared job(s)?\n\nThis drives Chrome and actually submits. Use the Dry-run checkbox to fill without submitting.`)) return;
  const btn = document.getElementById('applyBtn');
  const data = await post('/api/apply', {limit: document.getElementById('limit').value, dry_run: dryRun});
  if (!data.ok) { cmdEl.textContent = data.message || 'Could not start apply'; return; }
  if (btn) btn.disabled = true;
  cmdEl.textContent = dryRun ? 'Applying (DRY RUN — no submit)…' : 'Applying — Chrome is submitting…';
  await pollCommandUntilDone(dryRun ? 'Dry-run apply' : 'Apply');
  if (btn) btn.disabled = false;
}
// The one button: import (if URLs pasted) -> prepare -> apply, streaming live status through
// each phase. Stops early with a clear message if a phase fails or nothing ends up Ready.
async function runEverything() {
  const btn = document.getElementById('runBtn');
  const cmdEl = document.getElementById('command');
  const urls = document.getElementById('urls').value.trim();
  btn.disabled = true;
  pipeReset(); // show the visualizer, all stages idle
  try {
    // 1) Import any pasted URLs (skip if the box is empty — re-runs work on already-imported jobs).
    if (urls) {
      pipeSet('import', 'active');
      cmdEl.textContent = 'Importing URLs…';
      const imp = await post('/api/import', {urls});
      document.getElementById('importStatus').textContent =
        `Imported ${imp.inserted || 0} new URL(s); ${imp.duplicates || 0} already known.`;
      pipeSet('import', 'done');
      await refresh();
    } else {
      pipeSet('import', 'done'); // working on already-imported jobs
    }

    // 2) Prepare materials (enrich -> tailor -> cover), poll to completion. Sub-stages advance
    //    from the backend's STAGE: log lines inside pollCommandUntilDone.
    const prep = await post('/api/prepare', {});
    if (!prep.ok) { cmdEl.textContent = prep.message || 'Could not start prepare.'; pipeSet('enrich', 'failed'); return; }
    pipeSet('enrich', 'active');
    cmdEl.textContent = 'Preparing materials… (enrich → tailor → cover, ~30–60s)';
    const pc = await pollCommandUntilDone('Prepare materials');
    if (pc && pc.returncode && pc.returncode !== 0) { // prepare failed — mark the last active stage failed
      ['cover','tailor','enrich'].some(k => { if (PIPE_STATUS[k] === 'active') { pipeSet(k, 'failed'); return true; } return false; });
      return;
    }
    pipeSet('enrich', 'done'); pipeSet('tailor', 'done'); pipeSet('cover', 'done');

    // 3) Apply — only if something is actually Ready (else say so, don't launch a no-op).
    const status = await (await fetch('/api/status')).json();
    const ready = (status.stats || {}).ready || 0;
    if (ready < 1) { cmdEl.textContent = 'Materials prepared, but no jobs are Ready to apply.'; return; }
    const dryRun = document.getElementById('dryRun').checked;
    if (!dryRun && !confirm(`Submit real application(s) for ${ready} prepared job(s)?\n\nThis drives Chrome and actually submits. Check "Dry run" to fill without submitting.`)) {
      cmdEl.textContent = `Prepared ${ready} job(s). Apply cancelled.`;
      return;
    }
    const ap = await post('/api/apply', {limit: document.getElementById('limit').value, dry_run: dryRun});
    if (!ap.ok) { cmdEl.textContent = ap.message || 'Could not start apply.'; return; }
    pipeSet('apply', 'active');
    cmdEl.textContent = dryRun ? 'Applying (DRY RUN — no submit)…' : 'Applying — Chrome is submitting…';
    const ac = await pollCommandUntilDone(dryRun ? 'Dry-run apply' : 'Apply');
    pipeSet('apply', ac && ac.returncode && ac.returncode !== 0 ? 'failed' : 'done');
  } finally {
    btn.disabled = false;
  }
}
function toggleAdvanced() {
  const el = document.getElementById('advancedControls');
  el.style.display = el.style.display === 'none' ? 'flex' : 'none';
}
async function stopCommand() { await post('/api/stop', {}); refresh(); }
async function deleteJob(url, label) {
  if (!confirm(`Delete this application?\n\n${label}`)) return;
  const data = await post('/api/delete', {url});
  if (data.message) document.getElementById('command').textContent = data.message;
  await refresh();
}
function badge(status) { return `<span class="badge ${esc(status)}">${esc(status || 'new')}</span>`; }
let NET_AVAIL = false;
async function findContacts(url) {
  const r = await post('/api/network', {url, per_job: 5});
  if (!r.ok) alert(r.message || 'Could not start');
  refresh();
}
function emailBadge(s) {
  if (s === 'verified') return '<span class="ebadge ok">verified</span>';
  if (s === 'unverified') return '<span class="ebadge warn">unverified</span>';
  return '<span class="ebadge none">no email</span>';
}
function peopleCell(j) {
  const n = (j.contacts || []).length;
  const running = j.network_running;
  const label = running ? 'finding…' : (n ? `${n} contact${n>1?'s':''}` : 'Find contacts');
  const dis = (running || !NET_AVAIL) ? 'disabled' : '';
  const title = NET_AVAIL ? '' : 'Set APOLLO_API_KEY (paid plan) to enable';
  let out = `<button ${dis} title="${title}" onclick="findContacts(decodeURIComponent('${encodeURIComponent(j.url)}'))">${label}</button>`;
  if (j.network_error) out += `<div class="neterr">${esc(j.network_error)}</div>`;
  return out;
}
let GMAIL_AVAIL = false;
function draftBlock(c) {
  if (!c.email) return '';  // nothing to draft/send without an address
  const has = c.outreach_message || c.outreach_subject;
  const subj = esc(c.outreach_subject);
  const body = esc(c.outreach_message);
  const sent = c.outreach_status === 'submitted';
  let sendBtn;
  if (sent) sendBtn = `<span class="sent-tag">✓ submitted</span>`;
  else if (!GMAIL_AVAIL) sendBtn = `<button disabled title="Set GMAIL_ADDRESS + GMAIL_APP_PASSWORD">Send email</button>`;
  else sendBtn = `<button class="send" onclick="sendEmail('${esc(c.id)}', ${c.email_status==='verified'}, this)">Send email</button>`;
  const note = esc(c.linkedin_message);
  const noteLen = (c.linkedin_message || '').length;
  const overClass = noteLen > 300 ? 'over' : '';
  return `<div class="draft" data-cid="${esc(c.id)}">
      <div class="d-label">Email</div>
      <input class="d-subj" value="${subj}" placeholder="Subject…" ${sent?'disabled':''} />
      <textarea class="d-body" rows="4" ${sent?'disabled':''} placeholder="${has ? '' : 'No draft yet — click Regenerate'}">${body}</textarea>
      ${sent?'':`<input class="d-style" placeholder="✨ Tweak the vibe, then Regenerate — e.g. 'more casual', 'add a joke', 'mention I'm a Longhorn'">`}
      <div class="dbtns">
        ${sent?'':`<button onclick="saveDraft('${esc(c.id)}', this)">Save</button>
        <button class="secondary" onclick="regenDraft('${esc(c.id)}', this)">Regenerate</button>`}
        <button onclick="copyDraft(this)">Copy email</button>
        ${sendBtn}
      </div>
      <div class="d-label">LinkedIn note <span class="d-count ${overClass}"><span class="lcount">${noteLen}</span>/300</span></div>
      <textarea class="d-linkedin" rows="3" oninput="updCount(this)" placeholder="Short connection note (≤300 chars)">${note}</textarea>
      <div class="dbtns">
        <button onclick="saveLinkedin('${esc(c.id)}', this)">Save note</button>
        <button onclick="copyLinkedin(this)">Copy note</button>
        ${dmButton(c)}
      </div>
    </div>`;
}
function dmButton(c) {
  if (!c.linkedin_url || !c.linkedin_message)
    return `<button disabled title="Needs a LinkedIn URL and a drafted note">Copy note + open LinkedIn</button>`;
  const url = encodeURIComponent(c.linkedin_url);
  return `<button class="send" onclick="copyAndOpenLinkedin('${url}', this)" title="Copies your note and opens their profile — then Connect ▸ Add a note ▸ paste ▸ Send">Copy note + open LinkedIn</button>`;
}
function copyAndOpenLinkedin(encUrl, btn) {
  // Reliable + zero-risk: copy the (possibly edited) note, open the profile in a new tab.
  // You then do Connect ▸ Add a note ▸ paste (Cmd+V) ▸ Send yourself.
  const d = btn.closest('.draft');
  const note = d ? d.querySelector('.d-linkedin').value : '';
  if (note) { try { navigator.clipboard.writeText(note); } catch(e) {} }
  window.open(decodeURIComponent(encUrl), '_blank', 'noopener');
  btn.textContent = 'Copied ✓ — Connect ▸ Add a note ▸ paste';
  setTimeout(()=>btn.textContent='Copy note + open LinkedIn', 3500);
}
function updCount(ta) {
  const wrap = ta.closest('.draft');
  const el = wrap.querySelector('.lcount');
  const badge = wrap.querySelector('.d-count');
  if (el) { el.textContent = ta.value.length; badge.classList.toggle('over', ta.value.length > 300); }
}
async function saveLinkedin(cid, btn) {
  const d = btn.closest('.draft');
  await post('/api/outreach', {contact_id: cid,
    subject: d.querySelector('.d-subj').value, body: d.querySelector('.d-body').value,
    linkedin: d.querySelector('.d-linkedin').value});
  btn.textContent = 'Saved ✓'; setTimeout(()=>btn.textContent='Save note', 1200);
}
function copyLinkedin(btn) {
  const d = btn.closest('.draft');
  navigator.clipboard.writeText(d.querySelector('.d-linkedin').value);
  btn.textContent = 'Copied ✓'; setTimeout(()=>btn.textContent='Copy note', 1200);
}
async function sendEmail(cid, verified, btn) {
  const first = verified
    ? 'Send this outreach email now?'
    : '⚠ This email address is UNVERIFIED — it may bounce. Send anyway?';
  if (!confirm(first)) return;
  btn.disabled = true; btn.textContent = 'Sending…';
  const r = await post('/api/outreach/send', {contact_id: cid, confirm_unverified: !verified});
  if (r.ok) { refresh(); }
  else { btn.disabled = false; btn.textContent = 'Send email'; alert(r.message || 'Send failed'); }
}
function contactsRow(j, ncols) {
  if (!(j.contacts && j.contacts.length)) return '';
  const rows = j.contacts.map(c => `
    <div class="contact ${c.is_connection ? 'is-conn' : ''}">
      <div class="cavatar" style="background:${avatarColor(c.full_name)}">${initials(c.full_name)}</div>
      <div class="cbody">
        <div class="cname">${esc(c.full_name)} <span class="ctitle">— ${esc(c.title)}</span>
          ${c.match_reason ? `<span class="chip">${esc(c.match_reason)}</span>` : ''}
          ${c.is_connection ? `<span class="chip conn" title="${c.connection_at_company ? 'A 1st-degree connection currently at this company' : 'You are already connected to this person'}">🤝 ${c.connection_at_company ? 'Connection here' : 'Connection'}</span>` : ''}</div>
        <div class="cmeta">
          ${c.email ? `✉ <a href="mailto:${esc(c.email)}">${esc(c.email)}</a>` : '✉ —'} ${emailBadge(c.email_status)}
          ${c.linkedin_url ? ` · 🔗 <a href="${esc(c.linkedin_url)}" target="_blank">LinkedIn</a>` : ''}
        </div>
        ${draftBlock(c)}
      </div>
    </div>`).join('');
  const n = j.contacts.length;
  // Persist open/closed across the 2.5s auto-refresh (which re-renders this whole table and
  // would otherwise reset every <details> to closed). PEOPLE_OPEN holds the expanded job URLs;
  // ontoggle keeps it in sync, and we re-emit `open` from it on each render.
  const key = encodeURIComponent(j.url);
  const isOpen = PEOPLE_OPEN.has(j.url) ? 'open' : '';
  return `<tr class="contacts-row"><td colspan="${ncols}"><div class="contacts-wrap">
    <details class="people-details" ${isOpen} ontoggle="onPeopleToggle(this, decodeURIComponent('${key}'))">
      <summary><span class="people-caret">▸</span> <strong>People at ${esc(j.contact_company)}</strong>
        <span class="people-count">${n} contact${n>1?'s':''}</span>${j.connections_at_company ? `<span class="conn-hint">🤝 you have ${j.connections_at_company} connection${j.connections_at_company>1?'s':''} here</span>` : ''}</summary>
      <div class="people-body">${bulkBar(j)}${rows}</div>
    </details></div></td></tr>`;
}
// LinkedIn-style initials avatar: 1–2 initials + a stable color derived from the name.
function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  const first = parts[0][0] || '';
  const last = parts.length > 1 ? parts[parts.length - 1][0] : '';
  return (first + last).toUpperCase();
}
const _AVATAR_COLORS = ['#0a66c2','#057642','#915907','#7a3e9d','#0e7490','#b45309','#9f1239','#3730a3'];
function avatarColor(name) {
  let h = 0; const s = String(name || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return _AVATAR_COLORS[h % _AVATAR_COLORS.length];
}
const PEOPLE_OPEN = new Set(); // job URLs whose "People at" panel is expanded (survives refresh)
function onPeopleToggle(el, url) { if (el.open) PEOPLE_OPEN.add(url); else PEOPLE_OPEN.delete(url); }
function bulkBar(j) {
  const cs = j.contacts || [];
  const emailN = cs.filter(c => c.email && c.outreach_message && c.outreach_status !== 'submitted' && c.email_status === 'verified').length;
  const emailBtn = (GMAIL_AVAIL && emailN)
    ? `<button class="bulk send" onclick="sendAllEmails(decodeURIComponent('${encodeURIComponent(j.url)}'), this)">Send all emails (${emailN})</button>`
    : `<button class="bulk" disabled title="${GMAIL_AVAIL ? 'No verified emails ready' : 'Connect Gmail first'}">Send all emails (${emailN})</button>`;
  // LinkedIn is per-contact "Compose" (you click Send) — no bulk, since each compose
  // navigates the one browser away from the previous unsent invite.
  return `<div class="bulkbar">${emailBtn}<span class="li-hint">LinkedIn: use “Compose on LinkedIn” per contact →</span><span class="bulknote" data-bulk="${esc(j.url)}"></span></div>`;
}
async function sendAllEmails(url, btn) {
  if (!confirm('Send ALL verified-email drafts for this company now?')) return;
  btn.disabled = true; btn.textContent = 'Sending…';
  const r = await post('/api/outreach/send-all-emails', {job_url: url});
  const note = document.querySelector(`.bulknote[data-bulk="${cssEsc(url)}"]`);
  if (note) note.textContent = r.message || '';
  if (r.ok) setTimeout(refresh, 2500); else { btn.disabled = false; btn.textContent = 'Send all emails'; alert(r.message||'Failed'); }
}
function cssEsc(s){ return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/["\\\]]/g,'\\$&'); }
async function saveDraft(cid, btn) {
  const d = btn.closest('.draft');
  await post('/api/outreach', {contact_id: cid, subject: d.querySelector('.d-subj').value,
    body: d.querySelector('.d-body').value, linkedin: d.querySelector('.d-linkedin').value});
  btn.textContent = 'Saved ✓'; setTimeout(()=>btn.textContent='Save', 1200);
}
async function regenDraft(cid, btn) {
  const d = btn.closest('.draft');
  const style = d && d.querySelector('.d-style') ? d.querySelector('.d-style').value.trim() : '';
  btn.disabled = true; btn.textContent = 'Drafting…';
  const r = await post('/api/outreach', {contact_id: cid, regenerate: true, style});
  btn.disabled = false; btn.textContent = 'Regenerate';
  if (r.ok) {
    d.querySelector('.d-subj').value = r.subject;
    d.querySelector('.d-body').value = r.body;
    const ln = d.querySelector('.d-linkedin');
    if (ln && r.linkedin != null) { ln.value = r.linkedin; updCount(ln); }
  } else alert(r.message || 'Failed');
}
function copyDraft(btn) {
  const d = btn.closest('.draft');
  const text = `Subject: ${d.querySelector('.d-subj').value}\n\n${d.querySelector('.d-body').value}`;
  navigator.clipboard.writeText(text); btn.textContent = 'Copied ✓'; setTimeout(()=>btn.textContent='Copy', 1200);
}
function materialLinks(materials) {
  if (!materials || !materials.length) return '';
  return materials.map(m => `<a href="${esc(m.url)}" target="_blank">${esc(m.label)}</a>`).join(' · ');
}
function renderProgress(progress, stats) {
  const p = progress || {};
  const pct = Math.max(0, Math.min(100, Number(p.percent || 0)));
  document.getElementById('progressLabel').textContent = p.label || 'Idle';
  document.getElementById('progressPercent').textContent = `${pct}%`;
  document.getElementById('progressFill').style.width = `${pct}%`;
  const jobs = p.in_progress_jobs || [];
  const active = p.running ? `<span class="job-chip"><span class="pulse"></span>${esc(p.in_progress || 0)} in progress</span>` : `<span class="job-chip">Idle</span>`;
  const ready = `<span class="job-chip">${esc(stats.ready || 0)} ready</span>`;
  const applied = `<span class="job-chip">${esc(stats.applied || 0)} applied</span>`;
  const jobChips = jobs.map(j => `<span class="job-chip">${esc(j.company)} · ${esc(j.title)}</span>`).join('');
  document.getElementById('progressMeta').innerHTML = [active, ready, applied, jobChips].filter(Boolean).join('');
}
async function refresh() {
  const data = await (await fetch('/api/status')).json();
  document.getElementById('appDir').textContent = data.app_dir;
  const s = data.stats || {};
  const stats = [['URL Jobs',s.total],['URL Applied',s.applied],['Lifetime Applied',s.lifetime_applied],['Enriched',s.enriched],['User-approved',s.scored],['Tailored',s.tailored],['Covers',s.covers],['Ready',s.ready],['Errors',s.errors]];
  document.getElementById('stats').innerHTML = stats.map(([k,v]) => `<div class="stat"><strong>${v||0}</strong><span>${k}</span></div>`).join('');
  renderProgress(data.progress, s);
  const c = data.command || {};
  document.getElementById('command').textContent = c.running ? `Running: ${c.name}` : (c.name ? `Last: ${c.name}, exit ${c.returncode}` : 'Idle');
  document.getElementById('cmdLog').textContent = (c.log || []).join('\n');
  document.getElementById('applyLog').textContent = [...(data.worker_log || []), '', ...(data.claude_log || [])].join('\n');
  NET_AVAIL = !!data.networking_available;
  GMAIL_AVAIL = !!data.gmail_available;
  document.getElementById('jobs').innerHTML = (data.jobs || []).map(j => `
    <tr>
      <td>${badge(j.status)}</td>
      <td>${esc(j.company)}</td>
      <td>${esc(j.title)}</td>
      <td>${esc(j.salary)}</td>
      <td>${esc(j.location)}</td>
      <td class="desc">${esc(j.description)}</td>
      <td>${materialLinks(j.materials)}</td>
      <td class="people">${peopleCell(j)}</td>
      <td>${esc(j.apply_error)}</td>
      <td><a href="${esc(j.url)}" target="_blank">job</a>${j.application_url ? ` · <a href="${esc(j.application_url)}" target="_blank">apply</a>` : ''}</td>
      <td><button class="danger" onclick="deleteJob(decodeURIComponent('${encodeURIComponent(j.url)}'), decodeURIComponent('${encodeURIComponent(`${j.company} - ${j.title}`)}'))">Delete</button></td>
    </tr>${contactsRow(j, 11)}`).join('');
}
setInterval(refresh, 2500);
refresh();
</script>
</body>
</html>"""
