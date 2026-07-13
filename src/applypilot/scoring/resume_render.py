"""React-PDF resume renderer bridge.

Renders tailored resumes to PDF via the bundled headless Node/React-PDF renderer
(``resume_renderer/``). This is the preferred renderer; ``scoring.pdf`` falls back
to the Chromium HTML template when Node is unavailable or rendering fails.

Responsibilities:
  - map ApplyPilot data (LLM tailor JSON + profile) into the renderer's RenderRequest
  - materialize a writable runtime copy of the renderer and ``npm install`` it once
  - invoke ``node render.mjs <request.json> <out.pdf>`` and report success

Nothing here raises on the unhappy path — failures return None/False so the caller
can fall back cleanly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

from applypilot import config

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# npm install / render time budgets (seconds)
_INSTALL_TIMEOUT = 180
_RENDER_TIMEOUT = 60

# Serialize runtime bootstrap across streaming-mode threads in this process.
_runtime_lock = threading.Lock()
_runtime_ready: Path | None = None


# ── Mapping: ApplyPilot data → RenderRequest.resume ────────────────────────

def _clean_link(url: str) -> str:
    """Trim scheme and www. and any trailing slash for a compact contact line."""
    s = str(url).strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    if s.lower().startswith("www."):
        s = s[4:]
    return s.rstrip("/")


def _contact_from_profile(profile: dict, title: str) -> dict:
    """Build the contactInfo block from the user profile (never from the LLM)."""
    personal = (profile or {}).get("personal", {})
    links: list[str] = []
    seen: set[str] = set()
    for key in ("github_url", "linkedin_url", "portfolio_url", "website_url"):
        val = personal.get(key)
        if not val:
            continue
        cleaned = _clean_link(val)
        dedup_key = cleaned.lower()
        if cleaned and dedup_key not in seen:
            seen.add(dedup_key)
            links.append(cleaned)
    return {
        "name": personal.get("full_name") or personal.get("preferred_name") or "",
        "title": title or "",
        "email": personal.get("email") or "",
        "phone": personal.get("phone") or "",
        "location": _location_from_profile(personal),
        "links": links,
    }


def _location_from_profile(personal: dict) -> str:
    parts = [personal.get("city"), personal.get("province_state")]
    return ", ".join(p for p in parts if p)


def _skills_to_list(skills) -> list[dict]:
    """Normalize skills (dict of category->value, or string) to ordered pairs."""
    out: list[dict] = []
    if isinstance(skills, dict):
        for cat, val in skills.items():
            out.append({"category": str(cat), "value": str(val)})
    elif isinstance(skills, str) and skills.strip():
        out.append({"category": None, "value": skills.strip()})
    elif isinstance(skills, list):
        for item in skills:
            if isinstance(item, dict) and ("value" in item or "category" in item):
                out.append({"category": item.get("category"), "value": str(item.get("value", ""))})
            elif item:
                out.append({"category": None, "value": str(item)})
    return out


def _entries(raw) -> list[dict]:
    """Normalize experience/projects entries."""
    out: list[dict] = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        out.append({
            "header": str(e.get("header", "")),
            "subtitle": str(e.get("subtitle", "")),
            "location": str(e.get("location", "")),
            "date": str(e.get("date", "")),
            "bullets": [str(b) for b in (e.get("bullets") or []) if b],
        })
    return out


def _education_to_list(education) -> list[dict]:
    """Normalize education (LLM returns a free string; may also be a list)."""
    if isinstance(education, list):
        out = []
        for ed in education:
            if isinstance(ed, dict):
                out.append({
                    "school": str(ed.get("school", "")),
                    "degree": str(ed.get("degree", "")),
                    "detail": str(ed.get("detail", "")),
                    "date": str(ed.get("date", "")),
                })
            elif ed:
                out.append({"school": str(ed), "degree": "", "detail": "", "date": ""})
        return out
    text = str(education or "").strip()
    if not text:
        return []
    # Collapse a multi-line education blob into school + detail lines.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    return [{"school": lines[0], "degree": "",
             "detail": " · ".join(lines[1:]), "date": ""}]


def resume_from_llm_data(data: dict, profile: dict) -> dict:
    """Map the LLM tailor JSON + profile into a RenderRequest ``resume`` block."""
    title = str(data.get("title") or "")
    return {
        "contactInfo": _contact_from_profile(profile, title),
        "summary": (str(data["summary"]).strip() if data.get("summary") else None),
        "skills": _skills_to_list(data.get("skills")),
        "experience": _entries(data.get("experience")),
        "projects": _entries(data.get("projects")),
        "education": _education_to_list(data.get("education")),
    }


# ── Node runtime bootstrap ─────────────────────────────────────────────────

def _source_hash(src: Path) -> str:
    """Hash the renderer source (.mjs + package.json) to detect changes."""
    h = hashlib.sha256()
    for f in sorted(src.glob("*.mjs")) + [src / "package.json"]:
        if f.exists():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def ensure_runtime() -> Path | None:
    """Materialize + install the renderer runtime; return its dir, or None.

    Copies the shipped source into a writable runtime dir under APP_DIR and runs
    ``npm install --omit=dev`` once (cached by a source hash). Returns None if
    Node/npm are missing or install fails, signaling the caller to fall back.
    """
    global _runtime_ready
    if _runtime_ready is not None:
        return _runtime_ready

    with _runtime_lock:
        if _runtime_ready is not None:
            return _runtime_ready

        src = config.RESUME_RENDERER_SRC
        runtime = config.RESUME_RENDERER_RUNTIME
        if not (src / "render.mjs").exists():
            log.debug("Resume renderer source missing at %s", src)
            return None
        if config.get_node_path() is None or config.get_npm_path() is None:
            log.debug("Node.js/npm not found; using HTML fallback renderer.")
            return None

        want_hash = _source_hash(src)
        hash_file = runtime / ".src_hash"
        deps_ok = (runtime / "node_modules" / "@react-pdf" / "renderer").exists()
        cur_hash = hash_file.read_text().strip() if hash_file.exists() else ""

        if deps_ok and cur_hash == want_hash:
            _runtime_ready = runtime
            return runtime

        # (Re)materialize source files, then install.
        runtime.mkdir(parents=True, exist_ok=True)
        for f in list(src.glob("*.mjs")) + [src / "package.json"]:
            if f.exists():
                (runtime / f.name).write_bytes(f.read_bytes())

        log.info("Preparing React-PDF resume renderer (npm install, one-time)...")
        try:
            proc = subprocess.run(
                [config.get_npm_path(), "install", "--omit=dev", "--no-audit", "--no-fund"],
                cwd=str(runtime), capture_output=True, text=True, timeout=_INSTALL_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 - any failure → fallback
            log.warning("Resume renderer npm install failed (%s); using HTML fallback.", e)
            return None

        if proc.returncode != 0 or not (runtime / "node_modules" / "@react-pdf" / "renderer").exists():
            log.warning("Resume renderer npm install did not complete; using HTML fallback.\n%s",
                        (proc.stderr or "")[-500:])
            return None

        hash_file.write_text(want_hash)
        _runtime_ready = runtime
        log.info("React-PDF resume renderer ready at %s", runtime)
        return runtime


def node_renderer_available() -> bool:
    """Best-effort check used by `doctor`; does not trigger an install."""
    src = config.RESUME_RENDERER_SRC
    return (src / "render.mjs").exists() and config.get_node_path() is not None \
        and config.get_npm_path() is not None


# ── Cover letter mapping ────────────────────────────────────────────────────

def cover_letter_from_text(text: str, profile: dict, date: str | None = None) -> dict:
    """Map an ApplyPilot cover-letter prose file + profile into a coverLetter block.

    The candidate block reuses the résumé's contactInfo shape so the renderer can
    share the exact same header (centered name, blue "–"-separated links, no rule).
    ApplyPilot letters are full prose (salutation … sign-off); the renderer styles
    the salutation/body/sign-off from the paragraph structure.
    """
    return {
        "candidate": _contact_from_profile(profile, ""),  # {name, email, phone, links, ...}
        "date": date or "",
        "body": text.strip(),
    }


# ── Render ─────────────────────────────────────────────────────────────────

def _run_node_render(request: dict, out_path: Path, what: str) -> bool:
    """Write the request to a temp file and invoke the Node renderer. Returns success."""
    runtime = ensure_runtime()
    if runtime is None:
        return False

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir=str(out_path.parent), encoding="utf-8"
        ) as fh:
            json.dump(request, fh)
            tmp = Path(fh.name)

        proc = subprocess.run(
            [config.get_node_path(), str(runtime / "render.mjs"), str(tmp), str(out_path)],
            capture_output=True, text=True, timeout=_RENDER_TIMEOUT,
        )
        if proc.returncode != 0:
            log.warning("Node %s render failed (%s); falling back.\n%s",
                        what, out_path.name, (proc.stderr or "").strip()[-500:])
            return False
        if not out_path.exists() or out_path.stat().st_size == 0:
            log.warning("Node %s render produced no output for %s; falling back.", what, out_path.name)
            return False
        return True
    except Exception as e:  # noqa: BLE001 - any failure → fallback
        log.warning("Node %s render error (%s); falling back: %s", what, out_path.name, e)
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def render_with_node(resume: dict, out_path: Path, fit: str = "auto") -> bool:
    """Render a resume block to PDF via the Node renderer. Returns success.

    Args:
        resume:   RenderRequest ``resume`` block (see resume_from_llm_data).
        out_path: Destination .pdf path.
        fit:      "auto" | "compact" | "comfortable".
    """
    if not resume.get("contactInfo", {}).get("name"):
        log.debug("Skipping Node renderer: resume has no name.")
        return False
    request = {
        "schemaVersion": SCHEMA_VERSION,
        "options": {"kind": "resume", "fit": fit, "theme": "classic"},
        "resume": resume,
    }
    return _run_node_render(request, out_path, "resume")


def render_cover_with_node(cover: dict, out_path: Path) -> bool:
    """Render a cover-letter block to PDF via the Node renderer. Returns success."""
    if not cover.get("candidate", {}).get("name") or not cover.get("body"):
        log.debug("Skipping Node cover renderer: missing name or body.")
        return False
    request = {
        "schemaVersion": SCHEMA_VERSION,
        "options": {"kind": "cover_letter"},
        "coverLetter": cover,
    }
    return _run_node_render(request, out_path, "cover letter")
