"""ATS public-API enrichment (Greenhouse, Lever, Ashby).

Many postings are hosted on ATS platforms that expose clean public JSON APIs.
Fetching the description via the API is faster and far more reliable than
scraping the JS-rendered page (which often yields "no data extracted").

`fetch_ats_job()` is called as Tier 0 at the top of the detail cascade, before
any browser navigation. Returns None for non-ATS URLs or on any failure, so the
normal browser cascade still runs as a fallback.
"""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_TIMEOUT = 20
_UA = "Mozilla/5.0 (compatible; ApplyPilot/1.0; +https://github.com/Pickle-Pixel/ApplyPilot)"


def _html_to_text(raw: str | None) -> str:
    """Convert (possibly entity-encoded) HTML into readable plain text."""
    if not raw:
        return ""
    soup = BeautifulSoup(_html.unescape(raw), "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "ul", "ol"]):
        block.append("\n")
    text = soup.get_text()
    # collapse excess blank lines
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def detect_ats(url: str) -> str | None:
    """Return the ATS name for a URL, or None."""
    u = (url or "").lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    return None


def _get_json(api_url: str) -> dict | list | None:
    resp = httpx.get(api_url, headers={"User-Agent": _UA}, timeout=_TIMEOUT, follow_redirects=True)
    if resp.status_code != 200:
        log.debug("ATS API %s -> HTTP %s", api_url, resp.status_code)
        return None
    return resp.json()


def _greenhouse(url: str) -> dict | None:
    """Greenhouse: job-boards/boards.greenhouse.io/{company}/jobs/{id} (or embed form)."""
    p = urlparse(url)
    qs = parse_qs(p.query)
    company = job_id = None
    m = re.search(r"/([\w.-]+)/jobs/(\d+)", p.path)
    if m:
        company, job_id = m.group(1), m.group(2)
    elif qs.get("for") and qs.get("token"):
        company, job_id = qs["for"][0], qs["token"][0]
    if not (company and job_id):
        return None

    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}")
    if not isinstance(data, dict):
        return None
    desc = _html_to_text(data.get("content"))
    if not desc:
        return None
    return {"full_description": desc, "application_url": data.get("absolute_url") or url}


def _lever(url: str) -> dict | None:
    """Lever: jobs.lever.co/{company}/{postingId}."""
    p = urlparse(url)
    m = re.search(r"/([\w.-]+)/([\w-]+)", p.path)
    if not m:
        return None
    company, job_id = m.group(1), m.group(2)

    data = _get_json(f"https://api.lever.co/v0/postings/{company}/{job_id}")
    if not isinstance(data, dict):
        return None
    parts = [data.get("descriptionPlain") or _html_to_text(data.get("description"))]
    for lst in data.get("lists", []) or []:
        head = _html_to_text(lst.get("text", ""))
        body = _html_to_text(lst.get("content", ""))
        if head or body:
            parts.append(f"{head}\n{body}".strip())
    extra = data.get("additionalPlain") or _html_to_text(data.get("additional"))
    if extra:
        parts.append(extra)
    desc = "\n\n".join(p for p in parts if p).strip()
    if not desc:
        return None
    apply_url = data.get("applyUrl") or data.get("hostedUrl") or url
    return {"full_description": desc, "application_url": apply_url}


def _ashby(url: str) -> dict | None:
    """Ashby: jobs.ashbyhq.com/{orgSlug}/{jobId} — matched against the board API."""
    p = urlparse(url)
    parts = [seg for seg in p.path.split("/") if seg]
    if len(parts) < 2:
        return None
    org, job_id = parts[0], parts[1]

    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true")
    if not isinstance(data, dict):
        return None
    for job in data.get("jobs", []) or []:
        if job.get("id") == job_id or job_id in str(job.get("jobUrl", "")):
            desc = job.get("descriptionPlain") or _html_to_text(job.get("descriptionHtml"))
            if desc:
                return {
                    "full_description": desc,
                    "application_url": job.get("applyUrl") or job.get("jobUrl") or url,
                }
    return None


_EXTRACTORS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def fetch_ats_job(url: str) -> dict | None:
    """Fetch {full_description, application_url, ats} via an ATS public API, or None."""
    ats = detect_ats(url)
    if not ats:
        return None
    try:
        result = _EXTRACTORS[ats](url)
        if result and result.get("full_description"):
            result["ats"] = ats
            log.info("ATS enrich (%s): %d chars", ats, len(result["full_description"]))
            return result
    except Exception as e:  # noqa: BLE001 - any failure falls back to browser cascade
        log.debug("ATS %s fetch failed for %s: %s", ats, url, e)
    return None
