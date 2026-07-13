"""Local operator dashboard for ApplyPilot.

Runs a small localhost-only HTTP server with:
  - application tracker
  - URL import box
  - prepare/apply buttons
  - live command and apply logs
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
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


def _status_payload() -> dict:
    init_db()
    conn = get_connection()

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
        jobs.append({
            "url": row["url"],
            "title": row["title"] or "Untitled",
            "company": row["site"] or "",
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
                "INSERT INTO jobs (url, title, site, strategy, discovered_at, application_url) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (url, title, company, "dashboard_upload", now, url),
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


def _delete_job(url: str) -> dict:
    init_db()
    conn = get_connection()
    if not url:
        return {"ok": False, "message": "Missing job URL"}

    row = conn.execute(
        f"SELECT title, site FROM jobs WHERE url = ? AND {_URL_QUEUE_SQL}",
        (url,),
    ).fetchone()
    if not row:
        return {"ok": False, "message": "Application not found"}

    conn.execute(f"DELETE FROM jobs WHERE url = ? AND {_URL_QUEUE_SQL}", (url,))
    conn.commit()
    return {
        "ok": True,
        "message": f"Deleted {row['site'] or 'Unknown'} - {row['title'] or 'Untitled'}",
    }


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
        _json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = _read_json(self)
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

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    url = f"http://{host}:{port}/"
    console.print(f"[green]ApplyPilot dashboard running:[/green] {url}")
    console.print(f"[dim]Data directory:[/dim] {config.APP_DIR}")
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
    --bg:#f6f9fc;
    --surface:#ffffff;
    --surface2:#f8fbff;
    --surface3:#eef6ff;
    --line:#d9e5ef;
    --line2:#b9cfe1;
    --text:#13202e;
    --muted:#637587;
    --soft:#2f4052;
    --accent:#3ba7ff;
    --accent2:#0077d9;
    --green:#16845a;
    --red:#c93b34;
    --yellow:#9a6a00;
    --blue:#0077d9;
    --shadow:0 18px 45px rgba(54,86,115,.14);
  }
  * { box-sizing:border-box; }
  body {
    margin:0;
    font:14px/1.45 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:
      linear-gradient(180deg, rgba(59,167,255,.16), transparent 340px),
      repeating-linear-gradient(90deg, rgba(25,80,120,.045) 0 1px, transparent 1px 92px),
      var(--bg);
    color:var(--text);
  }
  body:before {
    content:"";
    position:fixed;
    inset:0;
    pointer-events:none;
    background:linear-gradient(rgba(25,80,120,.05) 1px, transparent 1px) 0 0 / 100% 32px;
    mask-image:linear-gradient(to bottom, rgba(0,0,0,.5), transparent 55%);
  }
  header {
    padding:24px 28px 18px;
    border-bottom:1px solid var(--line);
    display:flex;
    justify-content:space-between;
    align-items:flex-end;
    gap:18px;
    background:rgba(246,249,252,.86);
    backdrop-filter:blur(16px);
    position:sticky;
    top:0;
    z-index:5;
  }
  .brand { display:grid; gap:4px; }
  .eyebrow { color:var(--accent2); font:12px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; text-transform:uppercase; }
  h1 { font-size:26px; line-height:1.05; margin:0; letter-spacing:0; }
  .subtitle { color:var(--muted); max-width:720px; }
  main { padding:22px 28px 28px; display:grid; gap:18px; min-width:0; }
  .stats { display:grid; grid-template-columns:repeat(8, minmax(104px,1fr)); gap:10px; }
  .stat {
    background:linear-gradient(180deg, rgba(59,167,255,.08), rgba(255,255,255,.2)), var(--surface);
    border:1px solid var(--line);
    border-radius:8px;
    padding:12px;
    min-height:78px;
    box-shadow:0 1px 0 rgba(255,255,255,.8) inset;
  }
  .stat strong { display:block; font-size:28px; line-height:1; margin-bottom:9px; }
  .stat span { color:var(--muted); font:12px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; text-transform:uppercase; }
  .progress-panel {
    display:grid;
    gap:12px;
    background:rgba(255,255,255,.92);
    border:1px solid var(--line);
    border-radius:8px;
    padding:16px;
    box-shadow:var(--shadow);
  }
  .progress-head { display:flex; justify-content:space-between; align-items:center; gap:12px; }
  .progress-title { font-weight:700; color:var(--soft); }
  .progress-label { color:var(--muted); font:12px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .progress-track { height:12px; border-radius:999px; border:1px solid var(--line2); background:#eaf3fb; overflow:hidden; }
  .progress-fill { height:100%; width:0%; border-radius:999px; background:linear-gradient(90deg, #8ed0ff, var(--accent2)); transition:width .35s ease; }
  .progress-meta { display:flex; gap:10px; flex-wrap:wrap; color:var(--muted); font-size:12px; }
  .job-chip { display:inline-flex; gap:6px; align-items:center; border:1px solid var(--line); background:#f7fbff; border-radius:999px; padding:5px 9px; color:#34485a; }
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
  .controls { display:grid; grid-template-columns:minmax(0,1.2fr) minmax(340px,.8fr); gap:18px; }
  section {
    background:rgba(255,255,255,.9);
    border:1px solid var(--line);
    border-radius:8px;
    padding:16px;
    box-shadow:var(--shadow);
    min-width:0;
  }
  h2 { margin:0 0 14px; font-size:14px; font-weight:650; text-transform:uppercase; color:var(--soft); }
  textarea {
    width:100%;
    height:146px;
    resize:vertical;
    border:1px solid var(--line2);
    border-radius:8px;
    background:#fbfdff;
    color:var(--text);
    padding:12px;
    outline:none;
    box-shadow:0 0 0 1px rgba(255,255,255,.7) inset;
  }
  textarea:focus, input:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(59,167,255,.18); }
  input, select {
    background:#fbfdff;
    color:var(--text);
    border:1px solid var(--line2);
    border-radius:6px;
    padding:8px;
    outline:none;
  }
  button {
    border:1px solid var(--line2);
    border-radius:6px;
    padding:9px 12px;
    background:linear-gradient(180deg, #ffffff, #edf5fc);
    color:var(--text);
    cursor:pointer;
    font-weight:650;
  }
  button:hover { border-color:var(--accent); color:var(--accent2); }
  button.primary { background:linear-gradient(180deg, #62bdff, #1592ed); border-color:#1592ed; color:#ffffff; }
  button.primary:hover { color:#ffffff; filter:brightness(1.04); }
  button.danger { background:linear-gradient(180deg, #fff5f4, #ffe3e0); border-color:#f0aaa5; color:#9f2e29; }
  .row { display:flex; gap:9px; align-items:center; flex-wrap:wrap; }
  .hint { color:var(--muted); font-size:12px; margin-top:10px; }
  .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:8px; background:#ffffff; max-width:100%; }
  table { width:100%; border-collapse:collapse; min-width:1320px; }
  th, td { border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; }
  th {
    color:var(--muted);
    font:11px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
    text-transform:uppercase;
    background:#f2f7fb;
    position:sticky;
    top:0;
  }
  tr:hover td { background:rgba(59,167,255,.07); }
  td.desc { color:#44576a; max-width:370px; }
  .badge {
    display:inline-block;
    padding:3px 8px;
    border-radius:999px;
    font:12px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
    background:#edf4fb;
    color:#334456;
    border:1px solid rgba(25,80,120,.12);
  }
  .applied { background:#e6f7ef; color:var(--green); }
  .failed { background:#fff0ef; color:var(--red); }
  .ready { background:#e8f4ff; color:var(--blue); }
  .in_progress { background:#fff7e1; color:var(--yellow); }
  .logs { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
  pre {
    margin:0;
    min-height:240px;
    max-height:400px;
    overflow:auto;
    white-space:pre-wrap;
    background:#fbfdff;
    border:1px solid var(--line2);
    border-radius:8px;
    padding:12px;
    color:#34485a;
    font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  a { color:var(--accent2); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .path-pill {
    color:var(--muted);
    font:12px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
    border:1px solid var(--line);
    border-radius:999px;
    padding:6px 10px;
    max-width:520px;
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap;
  }
  @media (max-width: 1100px) {
    header { align-items:flex-start; flex-direction:column; }
    .stats { grid-template-columns:repeat(2,1fr); }
    .controls,.logs { grid-template-columns:1fr; }
    .path-pill { max-width:100%; }
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="eyebrow">Autonomous Application Console</div>
    <h1>ApplyPilot Operator</h1>
    <div class="subtitle">Paste job URLs, generate materials for those URLs only, and supervise live applications from one control surface.</div>
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
      <h2>Upload Job URLs</h2>
      <textarea id="urls" placeholder="Paste one or more job URLs here. Direct ATS/career URLs work best."></textarea>
      <div class="row" style="margin-top:10px">
        <button class="primary" onclick="importUrls()">Import URLs</button>
        <button onclick="prepareJobs()">Prepare Materials</button>
      </div>
      <div id="importStatus" class="hint"></div>
      <div class="hint">Prepare only works on URLs imported here. Fit scoring and broad job research are bypassed.</div>
    </section>
    <section>
      <h2>Application Control</h2>
      <div class="row">
        <label>Limit <input id="limit" type="number" value="10" min="1" max="100" style="width:72px"></label>
        <label><input id="dryRun" type="checkbox"> Dry run</label>
        <button class="primary" onclick="applyJobs()">Apply Ready Jobs</button>
        <button class="danger" onclick="stopCommand()">Stop</button>
      </div>
      <p id="command" class="hint"></p>
      <div class="hint">Apply launches the visible Chrome/Claude Code flow for prepared imported URLs only.</div>
    </section>
  </div>

  <section>
    <h2>Applications</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Status</th><th>Company</th><th>Title</th><th>Salary</th><th>Location</th><th>Description</th><th>Materials</th><th>Error</th><th>Links</th><th>Actions</th></tr></thead>
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
async function prepareJobs() { await post('/api/prepare', {}); refresh(); }
async function applyJobs() { await post('/api/apply', {limit: document.getElementById('limit').value, dry_run: document.getElementById('dryRun').checked}); refresh(); }
async function stopCommand() { await post('/api/stop', {}); refresh(); }
async function deleteJob(url, label) {
  if (!confirm(`Delete this application?\n\n${label}`)) return;
  const data = await post('/api/delete', {url});
  if (data.message) document.getElementById('command').textContent = data.message;
  await refresh();
}
function badge(status) { return `<span class="badge ${esc(status)}">${esc(status || 'new')}</span>`; }
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
  document.getElementById('jobs').innerHTML = (data.jobs || []).map(j => `
    <tr>
      <td>${badge(j.status)}</td>
      <td>${esc(j.company)}</td>
      <td>${esc(j.title)}</td>
      <td>${esc(j.salary)}</td>
      <td>${esc(j.location)}</td>
      <td class="desc">${esc(j.description)}</td>
      <td>${materialLinks(j.materials)}</td>
      <td>${esc(j.apply_error)}</td>
      <td><a href="${esc(j.url)}" target="_blank">job</a>${j.application_url ? ` · <a href="${esc(j.application_url)}" target="_blank">apply</a>` : ''}</td>
      <td><button class="danger" onclick="deleteJob(decodeURIComponent('${encodeURIComponent(j.url)}'), decodeURIComponent('${encodeURIComponent(`${j.company} - ${j.title}`)}'))">Delete</button></td>
    </tr>`).join('');
}
setInterval(refresh, 2500);
refresh();
</script>
</body>
</html>"""
