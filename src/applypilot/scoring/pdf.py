"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import html as _html
import json
import logging
from pathlib import Path

from applypilot.config import TAILORED_DIR

log = logging.getLogger(__name__)


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = sections["EDUCATION"].strip()
        edu_html = f'<div class="section"><div class="section-title">Education</div><div class="edu">{edu_text}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{sections["SUMMARY"].strip()}</div></div>'

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.35in 0.5in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.35;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 5px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.35;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10pt;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    <div class="title">{resume['title']}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{edu_html}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Cover letter rendering (prose, not resume sections) ──────────────────

def build_cover_letter_html(text: str, profile: dict | None = None) -> str:
    """Render a cover letter as a clean prose letter (optional letterhead).

    Cover letters are free prose ("Dear Hiring Manager," … name), so they must
    NOT go through the resume section parser. This produces a simple, professional
    single-column letter.
    """
    personal = (profile or {}).get("personal", {}) if profile else {}
    name = personal.get("full_name") or personal.get("preferred_name") or ""
    bits = []
    if personal.get("email"):
        bits.append(personal["email"])
    if personal.get("phone"):
        bits.append(personal["phone"])
    loc = ", ".join(p for p in (personal.get("city"), personal.get("province_state")) if p)
    if loc:
        bits.append(loc)
    contact_line = " &nbsp;|&nbsp; ".join(_html.escape(b) for b in bits)

    letterhead = ""
    if name:
        letterhead = (
            f'<div class="letterhead"><div class="lh-name">{_html.escape(name)}</div>'
            f'<div class="lh-contact">{contact_line}</div></div>'
        )

    paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    body = "".join(
        f"<p>{_html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
@page {{ size: letter; margin: 0.9in 1in; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Calibri', 'Segoe UI', Arial, sans-serif; font-size: 11pt;
        line-height: 1.5; color: #1a1a1a; }}
.letterhead {{ border-bottom: 1.5px solid #1a3a5c; padding-bottom: 8px; margin-bottom: 18px; }}
.lh-name {{ font-size: 18pt; font-weight: 700; color: #1a3a5c; }}
.lh-contact {{ font-size: 9.5pt; color: #555; margin-top: 2px; }}
p {{ margin-bottom: 11px; text-align: left; }}
p:last-child {{ margin-top: 4px; }}
</style></head><body>
{letterhead}
{body}
</body></html>"""


# ── React-PDF resume block builders ──────────────────────────────────────

def _split_contact(contact: str) -> tuple[str, str, list[str]]:
    """Split a "a | b | c" contact line into (email, phone, links)."""
    email, phone = "", ""
    links: list[str] = []
    for part in (p.strip() for p in (contact or "").split("|") if p.strip()):
        if "@" in part and " " not in part:
            email = part
        elif not phone and any(ch.isdigit() for ch in part) and part.count(" ") <= 2 and "/" not in part:
            phone = part
        else:
            links.append(part)
    return email, phone, links


def _resume_block_from_text(text: str) -> dict:
    """Build a RenderRequest ``resume`` block from structured resume text.

    Used for legacy files that lack a ``_DATA.json`` sidecar, so the nicer Node
    renderer can still be used instead of the HTML fallback.
    """
    parsed = parse_resume(text)
    sections = parsed["sections"]
    email, phone, links = _split_contact(parsed["contact"])

    def entries(name: str) -> list[dict]:
        out = []
        for e in parse_entries(sections.get(name, "")):
            out.append({"header": e["title"], "subtitle": e.get("subtitle", ""),
                        "location": "", "date": "", "bullets": e.get("bullets", [])})
        return out

    skills = [{"category": cat, "value": val}
              for cat, val in parse_skills(sections.get("TECHNICAL SKILLS", ""))]

    education = []
    if sections.get("EDUCATION"):
        edu_lines = [ln.strip() for ln in sections["EDUCATION"].splitlines() if ln.strip()]
        if edu_lines:
            education = [{"school": edu_lines[0], "degree": "",
                          "detail": " · ".join(edu_lines[1:]), "date": ""}]

    return {
        "contactInfo": {
            "name": parsed["name"], "title": parsed["title"],
            "email": email, "phone": phone,
            "location": parsed["location"], "links": links,
        },
        "summary": sections.get("SUMMARY") or None,
        "skills": skills,
        "experience": entries("EXPERIENCE"),
        "projects": entries("PROJECTS"),
        "education": education,
    }


def _resume_block_for(text_path: Path, text: str) -> dict | None:
    """Prefer the structured LLM ``_DATA.json`` sidecar; else parse the text."""
    data_path = text_path.parent / f"{text_path.stem}_DATA.json"
    if data_path.exists():
        try:
            from applypilot.config import load_profile
            from applypilot.scoring import resume_render
            data = json.loads(data_path.read_text(encoding="utf-8"))
            try:
                profile = load_profile()
            except Exception:  # noqa: BLE001 - header falls back to whatever's in data
                profile = {}
            return resume_render.resume_from_llm_data(data, profile)
        except Exception:  # noqa: BLE001
            log.debug("Failed to build resume block from %s", data_path, exc_info=True)
    try:
        return _resume_block_from_text(text)
    except Exception:  # noqa: BLE001
        log.debug("Failed to build resume block from text for %s", text_path, exc_info=True)
        return None


# ── Public API ───────────────────────────────────────────────────────────

def _load_profile_safe() -> dict:
    try:
        from applypilot.config import load_profile
        return load_profile()
    except Exception:  # noqa: BLE001
        return {}


def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False,
    kind: str = "resume",
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Resumes prefer the bundled React-PDF (Node) renderer for polished, one-page
    output, falling back to the Chromium HTML template when Node is unavailable.
    Cover letters (``kind="cover_letter"``) render as prose via a dedicated letter
    template — they must never go through the resume section parser.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.
        kind: "resume" (default) or "cover_letter".

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")

    # Cover letters: prose letter, never the resume section parser.
    # Prefer React-PDF (Node); fall back to the Chromium prose template.
    if kind == "cover_letter":
        profile = _load_profile_safe()
        if html_only:
            out = Path(output_path or text_path.with_suffix(".html"))
            out.write_text(build_cover_letter_html(text, profile), encoding="utf-8")
            log.info("HTML generated: %s", out)
            return out

        out = Path(output_path or text_path.with_suffix(".pdf"))
        try:
            from datetime import datetime
            from applypilot.scoring import resume_render
            now = datetime.now()
            date_str = f"{now:%B} {now.day}, {now.year}"
            cover = resume_render.cover_letter_from_text(text, profile, date=date_str)
            if resume_render.render_cover_with_node(cover, out):
                log.info("Cover letter PDF generated (react-pdf): %s", out)
                return out
        except Exception:  # noqa: BLE001 - fall through to HTML
            log.debug("React-PDF cover renderer errored; using HTML fallback", exc_info=True)

        render_pdf(build_cover_letter_html(text, profile), str(out))
        log.info("Cover letter PDF generated (html fallback): %s", out)
        return out

    if html_only:
        out = Path(output_path or text_path.with_suffix(".html"))
        out.write_text(build_html(parse_resume(text)), encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = Path(output_path or text_path.with_suffix(".pdf"))

    # Preferred: React-PDF via Node.
    resume_block = _resume_block_for(text_path, text)
    if resume_block is not None:
        try:
            from applypilot.scoring import resume_render
            if resume_render.render_with_node(resume_block, out):
                log.info("PDF generated (react-pdf): %s", out)
                return out
        except Exception:  # noqa: BLE001 - fall through to HTML
            log.debug("React-PDF renderer errored; using HTML fallback", exc_info=True)

    # Fallback: Chromium HTML template.
    render_pdf(build_html(parse_resume(text)), str(out))
    log.info("PDF generated (html fallback): %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Filter to those without a corresponding PDF
    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            convert_to_pdf(f)
            converted += 1
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
