"""Networking orchestrator: job → contacts.

find_contacts_for_job derives the employer/domain, searches Apollo (masked), ranks,
reveals contact info for the selected few, and persists them. LinkedIn fallback (NET-5)
is a no-op here (use_linkedin is accepted but not yet wired).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter

from applypilot.domain import geo
from applypilot.domain import linkedin_thread as _lt
from applypilot.networking import derive, providers, rank, store, verify


def _exclusions() -> tuple[str, ...]:
    """One reader, shared with the query-side filter — two would drift into disagreeing."""
    return providers.exclusions()

log = logging.getLogger(__name__)

# How many batches of `per_job` people we are willing to enrich before giving up. Verification
# runs after enrichment, so a whole batch can be rejected; without a top-up an ambiguous
# employer name returns nobody while real colleagues sit unexamined further down the pool.
# 3 is a credits/coverage tradeoff: per_job=5 enriches at most 15 of the ~25 candidates.
_TOPUP_ROUNDS = 3

_RECRUITER_TITLE_WORDS = (
    "recruiter", "recruiting", "talent acquisition", "sourcer", "sourcing",
    "people", "staffing", "university recruiting", "executive recruiting",
    "talent aquisition",
)


def _already_ours(contact: dict, by_email: dict, by_li: dict, by_name: dict) -> dict | None:
    """The person we already hold at this employer, or None.

    ONE predicate for both passes — before selection (cheap, incomplete) and after enrichment
    (complete). Two spellings of "is this the same person" is how the two passes would come to
    disagree, which is the family §Lessons 1 keeps recording.

    Email first, then LinkedIn, then name: the same order the contact card already uses to put
    one person's two rows together (SHEET-1b). Never `contact_id`, which hashes `job_url` and so
    differs for exactly the rows this exists to catch.
    """
    return (by_email.get(store._norm_email(contact.get("email")))
            or by_li.get(store._norm_linkedin(contact.get("linkedin_url")))
            or by_name.get((contact.get("full_name") or "").strip().lower()))


def _draft_and_store(profile: dict, job: dict, contact: dict, warm: bool = False) -> None:
    """Best-effort outreach draft for one contact; failures are non-fatal.

    warm=True → the hot layer (existing connection): warmer email + a DM to a known connection.
    """
    from applypilot.networking import outreach
    try:
        # What this employer has ALREADY been sent, so the next one differs. Passed at BOTH
        # call sites — a rule implemented at one of its two is not implemented (§Lessons 49),
        # and this is the batch path, where a whole company gets drafted minutes apart.
        previous = store.copy_already_sent_to_company(
            contact.get("company") or job.get("company") or job.get("site") or "",
            exclude_id=contact.get("id"))
        draft = outreach.draft_email(profile, job, contact, warm=warm, previous=previous,
                                     space=space_for(job))
        store.upsert_contact({
            "id": contact.get("id"),
            "job_url": contact["job_url"],
            "linkedin_url": contact.get("linkedin_url"),
            "full_name": contact.get("full_name"),
            "outreach_subject": draft["subject"],
            "outreach_message": draft["body"],
            "linkedin_message": draft.get("linkedin_note", ""),
            "outreach_status": "drafted",
            "outreach_channel": "email",
            "draft_variant": draft.get("variant", ""),
        })
    except Exception as e:  # noqa: BLE001
        log.debug("Outreach draft failed for %s: %s", contact.get("full_name"), e)


def space_for(job: dict, conn=None):
    """The manifest of the Space a row belongs to. None when Spaces are not set up.

    Resolved from the ROW rather than passed down from the dashboard, because both drafting
    paths are also reachable from the CLI and from `tick`, and a manifest that only arrives via
    the web layer is a rule implemented at one of its call sites (§Lessons 49). Never raises:
    drafting must degrade to the pre-Spaces behaviour rather than fail.
    """
    try:
        from applypilot.repo import spaces as _sp
        return _sp.load((job or {}).get("space_id") or _sp.DEFAULT_SPACE_ID, conn)
    except Exception:  # noqa: BLE001
        return None


def draft_for_contact(contact_id: str, style: str = "") -> dict | None:
    """Regenerate the outreach draft for a stored contact. Returns the new draft or None.

    `style` is an optional free-text tone directive passed through to outreach.draft_email.
    """
    from applypilot.config import load_profile
    from applypilot.database import get_connection
    from applypilot.networking import outreach

    conn = get_connection()
    store.init_contacts(conn)
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        return None
    contact = dict(zip(row.keys(), row))
    jrow = conn.execute(
        # space_id: the Space decides which prompt writes this email (SPACE-4).
        "SELECT url, title, company, site, full_description, space_id FROM jobs WHERE url = ?",
        (contact["job_url"],),
    ).fetchone()
    job = dict(zip(jrow.keys(), jrow)) if jrow else {"title": contact.get("title")}
    try:
        profile = load_profile()
    except Exception:  # noqa: BLE001
        profile = {}
    try:
        previous = store.copy_already_sent_to_company(
            contact.get("company") or job.get("company") or job.get("site") or "",
            exclude_id=contact_id, conn=conn)
        draft = outreach.draft_email(profile, job, contact, style=style, previous=previous,
                                     space=space_for(job, conn))
    except Exception as e:  # noqa: BLE001
        log.warning("Regenerate draft failed for %s: %s", contact_id, e)
        return None
    store.upsert_contact({
        "id": contact_id, "job_url": contact["job_url"],
        "linkedin_url": contact.get("linkedin_url"), "full_name": contact.get("full_name"),
        "outreach_subject": draft["subject"], "outreach_message": draft["body"],
        "linkedin_message": draft.get("linkedin_note", ""),
        "outreach_status": "drafted", "outreach_channel": "email",
        "draft_variant": draft.get("variant", ""),
    })
    return draft


def _augment_with_linkedin(selected: list[dict], company: str | None,
                           role: str | None, per_job: int, result: dict) -> list[dict]:
    """Fill the gap with LinkedIn-found people (read-only), Apollo-enriched by URL."""
    from applypilot.networking import linkedin_agent
    need = per_job - len(selected)
    people = linkedin_agent.find_people(company or "", role, n=need)
    if not people:
        return selected
    have_urls = {(c.get("linkedin_url") or "").lower() for c in selected}
    added = 0
    for p in people:
        url = (p.get("linkedin_url") or "").lower()
        if not url or url in have_urls:
            continue
        p = dict(p)
        p["key"] = url  # linkedin_url as the dedupe/enrich key
        p["match_reason"] = "same team"
        p["source"] = "linkedin"
        p.setdefault("company", company)
        selected.append(p)
        have_urls.add(url)
        added += 1
        if len(selected) >= per_job:
            break
    if added:
        result["note"] = f"{added} via LinkedIn fallback"
    return selected


def _is_recruiter_title(title: str | None) -> bool:
    t = (title or "").strip().lower()
    return bool(t and any(word in t for word in _RECRUITER_TITLE_WORDS))


def _linkedin_key(url: str | None) -> str:
    return store._norm_linkedin(url)


_LINKEDIN_URL_RE = re.compile(r"https?://(?:[\w.-]+\.)?linkedin\.com/in/[^\s,;]+", re.I)
_LINKEDIN_DEGREE_RE = re.compile(r"\s*[•·]\s*(?:1st|2nd|3rd\+?).*$", re.I)
_LINKEDIN_ACTION_WORDS = {
    "connect", "message", "follow", "following", "pending", "view profile",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ACTIVITY_MAX_CHARS = 12000
_ACTIVITY_SUMMARY_MAX_CHARS = 900


def _clean_linkedin_url(url: str | None) -> str:
    raw = (url or "").strip().rstrip(").,;")
    return raw if "linkedin.com/in/" in raw.lower() else ""


def _manual_linkedin_key(person: dict) -> str:
    return _linkedin_key(person.get("linkedin_url")) or (person.get("full_name") or "").strip().lower()


def parse_manual_linkedin_contacts(text: str) -> list[dict]:
    """Parse names/titles/profile URLs pasted from a manual LinkedIn People search.

    Accepts forgiving line formats:
      Name - Title - https://www.linkedin.com/in/name
      Name | Title | https://www.linkedin.com/in/name
      https://www.linkedin.com/in/name
    """
    lines = [raw.strip() for raw in (text or "").splitlines() if raw.strip()]
    people: list[dict] = []
    consumed: set[int] = set()

    # LinkedIn result-card copies are usually multiline:
    #   Name • 2nd
    #   Title at Company
    #   Location
    #   Connect
    for idx, line in enumerate(lines):
        if not re.search(r"[•·]\s*(?:1st|2nd|3rd\+?)\b", line, re.I):
            continue
        name = _LINKEDIN_DEGREE_RE.sub("", line).strip()
        if not name or name.lower().endswith(" is open to work"):
            continue
        title = ""
        url = ""
        stop = min(idx + 6, len(lines))
        for j in range(idx + 1, min(idx + 6, len(lines))):
            nxt = lines[j].strip()
            low = nxt.lower()
            if re.search(r"[•·]\s*(?:1st|2nd|3rd\+?)\b", nxt, re.I):
                stop = j
                break
            if low in _LINKEDIN_ACTION_WORDS or low.startswith(("current:", "past:")):
                continue
            match = _LINKEDIN_URL_RE.search(nxt)
            if match:
                url = _clean_linkedin_url(match.group(0))
                continue
            if not title:
                title = nxt
        people.append({"full_name": name, "title": title, "linkedin_url": url})
        consumed.update(range(idx, stop))

    for idx, raw in enumerate(lines):
        if idx in consumed:
            continue
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low in _LINKEDIN_ACTION_WORDS or low.startswith(("current:", "past:")):
            continue
        if re.search(r"[•·]\s*(?:1st|2nd|3rd\+?)\b", line, re.I):
            continue
        url_match = _LINKEDIN_URL_RE.search(line)
        url = _clean_linkedin_url(url_match.group(0) if url_match else "")
        without_url = _LINKEDIN_URL_RE.sub("", line).strip(" -|,\t")
        parts = [p.strip() for p in re.split(r"\s+[|-]\s+|\t+", without_url) if p.strip()]
        name = parts[0] if parts else ""
        title = parts[1] if len(parts) > 1 else ""
        if not name and url:
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            name = slug.replace("-", " ").replace("_", " ").title()
        if url or len(parts) > 1:
            people.append({"full_name": name, "title": title, "linkedin_url": url})
    return people


def parse_manual_linkedin_recruiters(text: str) -> list[dict]:
    """Backward-compatible alias for the first NET-7 endpoint name."""
    return parse_manual_linkedin_contacts(text)


def import_linkedin_contacts_for_job(
    job: dict,
    people: list[dict],
    draft: bool = True,
) -> dict:
    """Apollo-enrich manually chosen LinkedIn people and persist them.

    NET-7 pivot: LinkedIn ranking stays human-operated. ApplyPilot opens the search, the
    operator pastes chosen contact identities, and this path handles dedupe, Apollo
    enrichment, storage, and drafting.
    """
    from applypilot.networking import apollo

    job_url = job.get("url")
    company = derive.derive_company(job)
    result = {"company": company, "found": 0, "revealed": 0, "contacts": [], "note": ""}

    def _log(detail: str, status: str = "ok") -> None:
        from applypilot.database import log_event
        log_event(job_url, "outreach", status, detail)

    if not job_url:
        result["note"] = "job url missing"
        return result
    if not company:
        result["note"] = "could not determine employer"
        _log("Could not import LinkedIn contacts: the employer name could not be derived "
             "from this job.", "error")
        return result

    raw = [p for p in (people or []) if _manual_linkedin_key(p)]
    if not raw:
        result["note"] = "paste at least one contact name or LinkedIn profile URL"
        return result

    candidates = raw

    known_job = store.get_contacts_for_job(job_url)
    known_li = {_linkedin_key(c.get("linkedin_url")) for c in known_job if c.get("linkedin_url")}
    known_names = {(c.get("full_name") or "").strip().lower() for c in known_job if c.get("full_name")}
    known_elsewhere = store.known_at_company(company, exclude_job_url=job_url,
                                             space_id=(job or {}).get("space_id") or "")
    elsewhere_li = {c["linkedin_url"] for c in known_elsewhere if c.get("linkedin_url")}
    elsewhere_names = {(c.get("full_name") or "").strip().lower()
                       for c in known_elsewhere if c.get("full_name")}

    fresh = []
    skipped_dup = 0
    seen = set()
    for p in candidates:
        li = _linkedin_key(_clean_linkedin_url(p.get("linkedin_url")))
        name = (p.get("full_name") or "").strip().lower()
        key = li or name
        if not key or key in seen:
            skipped_dup += 1
            continue
        seen.add(key)
        if (li and (li in known_li or li in elsewhere_li)) or (name and (name in known_names or name in elsewhere_names)):
            skipped_dup += 1
            continue
        p = dict(p)
        p["key"] = li or name
        p["linkedin_url"] = _clean_linkedin_url(p.get("linkedin_url"))
        p.setdefault("company", company)
        fresh.append(p)

    if not fresh:
        result["found"] = len(candidates)
        result["note"] = f"all {len(candidates)} LinkedIn contact candidate(s) were already known"
        _log(f"No new LinkedIn contacts at {company}: all {len(candidates)} candidate(s) "
             "were already stored on this job or another role here.", "warn")
        return result

    result["found"] = len(fresh)

    enrichment_input = [{
        "key": p["key"],
        "full_name": p.get("full_name"),
        "company": company,
        "linkedin_url": p.get("linkedin_url"),
    } for p in fresh]
    revealed = apollo.match_by_identity(enrichment_input)
    result["revealed"] = sum(1 for r in revealed.values() if r.get("email"))

    stored_contacts = []
    rejected = []
    excluded = []
    profile_cache: dict = {}

    def _profile_for_drafting() -> dict:
        if "p" not in profile_cache:
            from applypilot.config import load_profile
            try:
                profile_cache["p"] = load_profile()
            except Exception:  # noqa: BLE001
                profile_cache["p"] = {}
        return profile_cache["p"]

    for p in fresh:
        rev = revealed.get(p["key"], {})
        query = f"{company} recruiter"
        contact = {
            "job_url": job_url,
            "full_name": _lt.better_name(p.get("full_name") or "", rev.get("full_name") or ""),
            "title": p.get("title"),
            "company": company,
            "space_id": (job or {}).get("space_id") or "",
            "linkedin_url": rev.get("linkedin_url") or p.get("linkedin_url"),
            "email": rev.get("email"),
            "email_status": rev.get("email_status", "none"),
            "location": rev.get("location") or "",
            "match_reason": ("manual LinkedIn recruiter import"
                             if _is_recruiter_title(p.get("title")) else "manual LinkedIn import"),
            "source": "linkedin_manual_recruiter" if _is_recruiter_title(p.get("title"))
            else "linkedin_manual",
            "apollo_id": rev.get("apollo_id"),
        }
        place = geo.is_excluded(contact.get("location"), _exclusions())
        if place:
            excluded.append(contact.get("full_name") or "?")
            continue
        v = verify.verify_contact({**contact, "company": company}, company, "")
        if v["verdict"] == verify.REJECT:
            rejected.append(contact.get("full_name") or "?")
            continue
        contact["verify_note"] = "; ".join([f"LinkedIn query {query!r}", *v["reasons"]])
        contact["confidence"] = v["confidence"]
        cid = store.upsert_contact(contact)
        contact["id"] = cid
        if draft and (contact.get("email") or contact.get("linkedin_url")):
            _draft_and_store(_profile_for_drafting(), job, contact)
        stored_contacts.append(contact)

    result["contacts"] = stored_contacts
    recruiter_like = sum(1 for p in fresh if _is_recruiter_title(p.get("title")))
    parts = [f"{len(stored_contacts)} stored", f"{result['revealed']} with email",
             f"{recruiter_like} recruiter-like"]
    if skipped_dup:
        parts.append(f"{skipped_dup} duplicate")
    if rejected:
        parts.append(f"{len(rejected)} work elsewhere")
    if excluded:
        parts.append(f"{len(excluded)} excluded by location")
    result["note"] = ", ".join(parts)
    status = "ok" if stored_contacts else "warn"
    _log(f"Manual LinkedIn import for {company}: {result['note']}.", status)
    return result


def import_linkedin_recruiters_for_job(
    job: dict,
    people: list[dict],
    draft: bool = True,
) -> dict:
    """Backward-compatible alias for the first NET-7 endpoint name."""
    return import_linkedin_contacts_for_job(job, people, draft=draft)


def _clean_activity_summary(text: str | None) -> str:
    out = (text or "").strip()
    out = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", out).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out[:_ACTIVITY_SUMMARY_MAX_CHARS].strip()


def summarize_contact_activity(contact: dict, activity_text: str) -> str:
    """Compress operator-pasted public activity into draft-ready person context."""
    from applypilot.llm import get_client

    text = (activity_text or "").strip()[:_ACTIVITY_MAX_CHARS]
    system = (
        "You summarize public professional activity for outreach context. "
        "Return only the summary text, no JSON, no preamble."
    )
    user = f"""CONTACT
Name: {contact.get('full_name') or ''}
Title: {contact.get('title') or ''}
Company: {contact.get('company') or ''}

PASTED PUBLIC ACTIVITY
{text}

Write 2-4 concise bullets or one 40-90 word paragraph about what this person appears to be
working on, posting about, hiring for, building, speaking about, or repeatedly emphasizing.

Rules:
- Use only facts supported by the pasted text.
- Use careful language like "appears to", "recently posted about", or "seems focused on" when
  certainty is limited.
- Do not infer private traits or protected-class information.
- Do not mention LinkedIn, scraping, profiles, feeds, or that the sender noticed/saw the post.
- Do not quote or mimic the pasted wording.
- Drop boilerplate such as reactions, comments, followers, Connect, Message, mutual connections,
  and navigation text.
- Keep it useful as hidden context for a human-sounding email, LinkedIn message, or text.
"""
    raw = get_client("light").chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=220,
        temperature=0.2,
    )
    summary = _clean_activity_summary(raw)
    if not summary:
        raise ValueError("empty activity summary")
    return summary


def enrich_contact_activity(contact_id: str, activity_text: str, replace: bool = False) -> dict:
    """Summarize pasted contact activity and store it in the existing noticed field."""
    from applypilot.database import get_connection, log_event

    cid = (contact_id or "").strip()
    text = (activity_text or "").strip()
    if not cid:
        return {"ok": False, "message": "contact_id required"}
    if not text:
        return {"ok": False, "message": "Paste recent activity first."}

    conn = get_connection()
    store.init_contacts(conn)
    contact = store.get_contact(cid, conn)
    if not contact:
        return {"ok": False, "message": "contact not found"}
    existing = (contact.get("noticed") or "").strip()
    if existing and not replace:
        return {"ok": False, "message": "This contact already has context. Confirm replace first.",
                "needs_replace": True, "summary": existing}

    try:
        summary = summarize_contact_activity(contact, text)
    except Exception as e:  # noqa: BLE001
        log.warning("Contact activity enrichment failed for %s: %s", cid, e)
        return {"ok": False, "message": f"enrichment failed: {e}"}

    store.upsert_contact({
        "id": cid,
        "job_url": contact["job_url"],
        "linkedin_url": contact.get("linkedin_url"),
        "full_name": contact.get("full_name"),
        "noticed": summary,
    }, conn)
    log_event(contact.get("job_url") or "", "outreach", "ok",
              f"Enriched {contact.get('full_name') or 'a contact'} with pasted activity.", conn)
    return {"ok": True, "contact_id": cid, "summary": summary,
            "message": "Contact enriched. Regenerate drafts to use it."}


def _email_tokens(name: str | None) -> list[str]:
    raw = unicodedata.normalize("NFKD", name or "")
    ascii_name = raw.encode("ascii", "ignore").decode("ascii")
    return [p.lower() for p in re.findall(r"[a-zA-Z0-9]+", ascii_name)]


def _email_pattern_candidates(name: str | None) -> dict[str, str]:
    parts = _email_tokens(name)
    if len(parts) < 2:
        return {}
    first = parts[0]
    last = parts[-1]
    return {
        "first.last": f"{first}.{last}",
        "first_last": f"{first}_{last}",
        "firstlast": f"{first}{last}",
        "flast": f"{first[:1]}{last}",
        "firstl": f"{first}{last[:1]}",
        "first": first,
    }


def _infer_company_email_pattern(contacts: list[dict]) -> dict:
    seeds = []
    verified = [c for c in contacts if (c.get("email_status") or "").lower() == "verified"]
    source = verified or contacts
    for c in source:
        email = (c.get("email") or "").strip().lower()
        if not _EMAIL_RE.match(email):
            continue
        local, domain = email.rsplit("@", 1)
        for pattern, candidate in _email_pattern_candidates(c.get("full_name")).items():
            if local == candidate:
                seeds.append({
                    "pattern": pattern,
                    "domain": domain,
                    "name": c.get("full_name") or "",
                    "email": email,
                    "verified": (c.get("email_status") or "").lower() == "verified",
                })
                break
    if not seeds:
        return {"ok": False, "message": "No usable company email pattern found."}

    domains = Counter(s["domain"] for s in seeds)
    patterns = Counter(s["pattern"] for s in seeds)
    domain, domain_count = domains.most_common(1)[0]
    pattern, pattern_count = patterns.most_common(1)[0]
    if len(domains) > 1 and domains.most_common(2)[1][1] == domain_count:
        return {"ok": False, "message": "Company email domain is ambiguous."}
    if len(patterns) > 1 and patterns.most_common(2)[1][1] == pattern_count:
        return {"ok": False, "message": "Company email pattern is ambiguous."}
    example = next(s for s in seeds if s["domain"] == domain and s["pattern"] == pattern)
    return {
        "ok": True,
        "domain": domain,
        "pattern": pattern,
        "example_name": example["name"],
        "example_email": example["email"],
        "verified_seed": bool(verified),
    }


def guess_missing_emails_for_job(job: dict, draft: bool = True) -> dict:
    """Infer missing emails from the dominant same-company email format.

    Guesses are reachability hints, not verification. They are stored as unverified, never
    overwrite an existing address, and only run when one clear pattern wins.
    """
    from applypilot.database import get_connection, log_event

    job_url = job.get("url")
    company = derive.derive_company(job)
    result = {"company": company, "guessed": 0, "skipped": 0, "contacts": [], "note": ""}
    if not job_url:
        result["note"] = "job url missing"
        return result
    if not company:
        result["note"] = "could not determine employer"
        return result

    conn = get_connection()
    store.init_contacts(conn)
    space_id = (job or {}).get("space_id") or ""
    scope = " AND (c.job_url = ? OR c.space_id = ?)" if space_id else ""
    args = [(company or "").strip().lower()] + ([job_url, space_id] if scope else [])
    rows = conn.execute(
        "SELECT c.* FROM contacts c WHERE LOWER(TRIM(COALESCE(c.company,''))) = ?"
        + scope + " ORDER BY c.discovered_at ASC",
        args,
    ).fetchall()
    company_contacts = [dict(zip(r.keys(), r)) for r in rows]
    pattern = _infer_company_email_pattern(company_contacts)
    if not pattern.get("ok"):
        result["note"] = pattern.get("message") or "No usable company email pattern found."
        log_event(job_url, "network", "warn",
                  f"Could not guess emails at {company}: {result['note']}", conn)
        return result

    job_contacts = store.get_contacts_for_job(job_url, conn)
    existing_emails = {
        (c.get("email") or "").strip().lower() for c in company_contacts if c.get("email")
    }
    profile_cache: dict = {}

    def _profile_for_drafting() -> dict:
        if "p" not in profile_cache:
            from applypilot.config import load_profile
            try:
                profile_cache["p"] = load_profile()
            except Exception:  # noqa: BLE001
                profile_cache["p"] = {}
        return profile_cache["p"]

    guessed = []
    skipped = 0
    for contact in job_contacts:
        if (contact.get("email") or "").strip():
            skipped += 1
            continue
        locals_by_pattern = _email_pattern_candidates(contact.get("full_name"))
        local = locals_by_pattern.get(pattern["pattern"])
        if not local:
            skipped += 1
            continue
        email = f"{local}@{pattern['domain']}".lower()
        if email in existing_emails:
            skipped += 1
            continue
        note = (f"Guessed {pattern['pattern']}@{pattern['domain']} from "
                f"{pattern['example_name']} <{pattern['example_email']}>.")
        update = {
            "id": contact["id"],
            "job_url": contact["job_url"],
            "linkedin_url": contact.get("linkedin_url"),
            "full_name": contact.get("full_name"),
            "email": email,
            "email_status": "unverified",
            "verify_note": note,
        }
        store.upsert_contact(update, conn)
        saved = store.get_contact(contact["id"], conn) or {**contact, **update}
        guessed.append(saved)
        existing_emails.add(email)
        if draft:
            _draft_and_store(_profile_for_drafting(), job, saved)

    result["guessed"] = len(guessed)
    result["skipped"] = skipped
    result["contacts"] = guessed
    result["note"] = (f"{len(guessed)} guessed with {pattern['pattern']}@{pattern['domain']} "
                      f"from {pattern['example_email']}")
    status = "ok" if guessed else "warn"
    log_event(job_url, "network", status, f"Email guessing for {company}: {result['note']}.", conn)
    return result


def find_contacts_for_job(
    job: dict,
    per_job: int = 5,
    use_linkedin: bool = False,
    dry_run: bool = False,
    draft: bool = True,
    skip_known: bool = False,
) -> dict:
    """Find + persist up to `per_job` contacts for a job.

    Args:
        job: job row dict (needs url; ideally title, company, application_url, full_description).
        per_job: how many contacts to find/reveal.
        use_linkedin: reserved for NET-5 (fallback); no-op in NET-1.
        dry_run: search + rank only — no reveal (no Apollo credits), no persistence of email.
        skip_known: drop anyone already stored for this job BEFORE selection, so a second round
            reaches deeper into the ranked pool instead of re-picking the same five. Without it
            a re-run is a no-op that spends credits: `select()` scores title relevance and is
            deterministic, so it returns the same top N, and `upsert_contact` then overwrites
            the rows you already had. This is the "nobody replied, find me new people" path.

    Returns:
        {"company": str|None, "found": int, "revealed": int, "contacts": [dict], "note": str}
    """
    job_url = job.get("url")
    role = job.get("title")
    # `derive_company` now does the whole chain — URL rules, then the tenant-slug repair
    # (Ouryahoo -> Yahoo), then the wrong-entity challenge (Jobvite -> LegalZoom). It used to do
    # only the first, with the repair bolted on HERE and nowhere else, so the import path wrote
    # an uncorrected name into `jobs.company` and every later read trusted it. One function, one
    # answer, four call sites that can no longer disagree (§Lessons 49).
    company = derive.derive_company(job)
    domain = derive.derive_domain(job, company)
    # WHERE the domain came from decides how much it may be trusted later. A domain read off the
    # careers-site host is an inference: avathongov.com hosts Avathon Government's postings, but
    # its people email from @sparkcognition.com (the company was SparkCognition before the
    # rename). Rejecting real employees for contradicting a GUESS is how that job found nobody.
    # A domain Apollo corroborated is evidence and keeps its full weight.
    domain_source = "url" if domain else ""

    from applypilot.networking import connections
    conns_at_company = connections.count_at_company(company)
    result = {"company": company, "found": 0, "revealed": 0, "contacts": [],
              "connections_at_company": conns_at_company, "note": ""}

    def _log(detail: str, status: str = "ok") -> None:
        """Record the outcome on the job. EVERY exit from this function logs.

        A search that finds nobody used to log nothing at all, which made a completed run
        indistinguishable from a button that never fired — see tests/test_networking_silent_zero.
        """
        if dry_run:
            return
        from applypilot.database import log_event
        log_event(job_url, "outreach", status, detail)

    if not company and not domain:
        result["note"] = "could not determine employer/domain"
        _log("Could not find contacts: the employer name and domain could not be derived "
             "from this job's URL.", "error")
        return result

    # An ATS-hosted posting carries no employer domain (ats.rippling.com is the vendor's), and
    # without one Apollo falls back to a fuzzy NAME search. For a common word that finds the
    # wrong company entirely: "Wander" returned four unrelated Wanders, every candidate came
    # from "Wander AG", verification correctly dropped all 15 — and the real employer's CEO and
    # CMO sat at wander.com untouched. Recover the domain first; the guess is only accepted if
    # Apollo's own people there report a matching employer name.
    if not domain and company:
        slug = derive.employer_slug_from_url(job.get("url") or job.get("application_url"))
        domain = providers.confirm_employer_domain(company, slug) or None
        if domain:
            result["employer_domain"] = domain
            domain_source = "apollo"      # corroborated: people there report this employer

    # Colleagues and recruiters are searched SEPARATELY. One blended query cannot produce a mix
    # because the provider decides the composition — measured at 25 recruiters and 0 peers on a
    # real job, after which the ranking stage was choosing five recruiters out of five.
    # Defaults come from the settings registry, never repeated here: `OUTREACH_ATTACH_DECK`
    # defaulted to "1" at the call site while settings.py declared False, so `doctor --config`
    # reported the feature off while 3.1 MB rode along on 34 real emails. A default in two
    # places is two defaults.
    from applypilot import settings as _settings
    _cfg, _ = _settings.resolve()
    min_peers = _cfg["OUTREACH_MIN_PEERS"]
    min_recruiters = _cfg["OUTREACH_MIN_RECRUITERS"]

    pools = providers.search_mix(company, domain, role, per_page=25)
    candidates = pools["peers"] + pools["recruiters"]
    if not candidates:
        # "coverage or plan/key" named three unrelated problems at once and pointed at none of
        # them. It cost a wrong diagnosis on the Yahoo job, where the real answer was that the
        # employer name was a Workday tenant slug ("Ouryahoo") that no provider has ever heard
        # of — and where a missing API key would have printed exactly the same sentence.
        # §Lessons 15: a zero result has to say WHICH zero it is.
        name = providers.active() or "the provider"
        reachable, why = providers.probe()
        if not reachable:
            result["note"] = f"{name} is not usable: {why}"
            _log(f"Could not search for contacts: {name} is not usable ({why}). "
                 f"No credits were spent.", "error")
        elif not providers.company_known(company, domain):
            result["note"] = f"{name} has no company called {company!r}"
            _log(f"No contacts found: {name} has no company named {company!r}. If that is not "
                 f"how the employer is actually known, correct the company on the Job tab — an "
                 f"ATS tenant slug is often not the employer's name.", "warn")
        else:
            result["note"] = f"{name} knows {company!r} but returned nobody matching this role"
            _log(f"No contacts found at {company} — {name} knows the company but returned "
                 f"nobody for these titles. Try “find contacts” again for a wider search.", "warn")
        return result

    # Rank the WHOLE pool, then work down it in batches. Ranking scores title relevance and
    # knows nothing about which employer a person actually works for, while the strongest
    # verification signal (the work-email domain) only exists after enrichment. So a batch can
    # come back 100% rejected while genuine colleagues sit further down the same pool.
    #
    # That is exactly what "find contacts is not working" was on a Zello job: Apollo lists
    # THREE orgs named Zello/ZELLO, none with a primary_domain to tell them apart. The five
    # best-titled people were all from the wrong one, were all correctly dropped, and the two
    # real @zello.com recruiters were never looked at — they were candidates 6..25. Stopping
    # after batch one turned an ambiguous employer into a silent zero.
    # Interleaved, not scored into one list: the loop below enriches down this order and drops
    # whoever fails verification, so the two sides must stay interwoven the whole way. Putting
    # four peers at the front means a company whose first four peers all fail verification ends
    # up all-recruiter again — the original bug, one step later.
    ranked = rank.select_mix(pools["peers"], pools["recruiters"], role,
                             min_peers=min_peers, min_recruiters=min_recruiters)

    # Round two. Excluded by the SAME identity function that stores them — computing a fresh
    # name/email match here would be a second answer to "is this the same person", and the two
    # would disagree (§Lessons 1 is a whole family of exactly that).
    # ── Anyone we already have at this EMPLOYER is not a new contact ────────────
    #
    # Unconditional, and that is the change: `skip_known` below is opt-in and per JOB, so a
    # FIRST search on a second role at a company we already work had no exclusion at all. That
    # is how the WebAI pair happened — two people four emails deep on a cancelled role, re-found
    # for the live one, each given a fresh cold email draft and a text opening "I applied for
    # the AI Software Engineer role" as though the conversation did not exist.
    #
    # Runs BEFORE enrichment, so a person we already hold costs no Apollo credit either.
    #
    # Matched on EMAIL then LinkedIn then NAME — never on `contact_id`, which hashes `job_url`
    # and therefore differs for exactly the rows this exists to catch. Same order the card
    # already uses to put one person's two rows together (SHEET-1b).
    known_elsewhere: list[dict] = []
    #: The same three keys, kept for a SECOND pass after enrichment — see `_already_ours` and
    #: the loop below. Empty dicts when there is nobody at this employer yet.
    by_email: dict = {}
    by_li: dict = {}
    by_name: dict = {}
    if company:
        mine = store.known_at_company(company, exclude_job_url=job_url or "",
                                      space_id=(job or {}).get("space_id") or "")
        if mine:
            by_email = {m["email"]: m for m in mine if m["email"]}
            by_li = {m["linkedin_url"]: m for m in mine if m["linkedin_url"]}
            by_name = {(m["full_name"] or "").strip().lower(): m for m in mine if m["full_name"]}
            fresh = []
            for c in ranked:
                hit = _already_ours(c, by_email, by_li, by_name)
                (known_elsewhere.append(hit) if hit else fresh.append(c))
            if known_elsewhere:
                log.info("%d of %d candidates at %s are already stored on another role",
                         len(known_elsewhere), len(ranked), company)
            ranked = fresh

    already = 0
    if skip_known and job_url:
        known = {c["id"] for c in store.get_contacts_for_job(job_url)}
        before = len(ranked)
        ranked = [c for c in ranked
                  if store.contact_id(job_url, c.get("linkedin_url"), c.get("full_name"))
                  not in known]
        already = before - len(ranked)
        log.info("Second round: %d of %d candidates already known, %d left",
                 already, before, len(ranked))
        if not ranked:
            # Loud, not silent. A search that spent credits and found nobody NEW must not look
            # identical to a button that never fired (§Lessons 15).
            note = (f"No new people at {company or 'this company'} — all "
                    f"{already} candidate(s) the provider returned are already on this job.")
            from applypilot.database import log_event
            log_event(job_url, "network", "warn", note)
            return {"company": company, "found": 0, "revealed": 0, "contacts": [],
                    "note": note}

    # LinkedIn fallback (opt-in): when the provider under-covers this company, read
    # the company People page and merge the found profiles.
    if use_linkedin and len(ranked) < per_job:
        ranked = _augment_with_linkedin(ranked, company, role, per_job, result)

    # Enrichment costs Apollo credits, so topping up is bounded rather than "walk all 25".
    max_enriched = min(len(ranked), max(per_job, per_job * _TOPUP_ROUNDS))

    _profile_cache: dict = {}

    def _profile_for_drafting() -> dict:
        if "p" not in _profile_cache:
            from applypilot.config import load_profile
            try:
                _profile_cache["p"] = load_profile()
            except Exception:  # noqa: BLE001
                _profile_cache["p"] = {}
        return _profile_cache["p"]

    stored_contacts: list[dict] = []
    rejected: list[str] = []
    #: Dropped for LOCATION, kept separate from `rejected`. They are different findings: a
    #: rejection means "this person does not work there", an exclusion means "they do, and they
    #: are not the desk we are writing to". Folding them together makes a targeting choice look
    #: like a data-quality failure in the one place anyone reads (§Lessons 15).
    excluded: list[str] = []
    considered: list[dict] = []
    cursor = 0

    while len(stored_contacts) < per_job and cursor < max_enriched:
        batch = ranked[cursor:cursor + (per_job - len(stored_contacts))]
        if not batch:
            break
        cursor += len(batch)
        considered.extend(batch)

        # Reveal contact info for this batch only (Apollo bulk enrichment; consumes credits).
        revealed: dict[str, dict] = {}
        if not dry_run:
            revealed = providers.enrich(batch)
            result["revealed"] += sum(1 for r in revealed.values() if r.get("email"))

        for c in batch:
            rev = revealed.get(c.get("key"), {})
            contact = {
                "job_url": job_url,
                # Enrichment knows the surname that search redacted. `better_name` takes it only
                # when it is a strict EXTENSION of what we have — a looser rule lets a
                # mismatched enrichment row rename a contact into a different person, which is
                # worse than a missing surname: a wrong first name shows up in the greeting and
                # a wrong full name does not.
                "full_name": _lt.better_name(c.get("full_name") or "", rev.get("full_name") or ""),
                "title": c.get("title"),
                "company": company or c.get("company"),
                "linkedin_url": rev.get("linkedin_url") or c.get("linkedin_url"),
                "email": rev.get("email"),
                "email_status": rev.get("email_status", "none"),
                # ENRICHMENT first. The search response has no location at all — it carries
                # `has_city`/`has_state`/`has_country`, booleans about whether the data exists —
                # which is why `contacts.location` was empty on all 244 stored rows.
                "location": rev.get("location") or c.get("location") or "",
                "seniority": c.get("seniority"),
                "match_reason": c.get("match_reason"),
                "source": c.get("source") or providers.active() or "apollo",
                "apollo_id": c.get("apollo_id"),
            }
            # Self-check before this reaches the dashboard. Catches the contacts an org-name
            # filter alone misses — Apollo returns people with no email whose employer is
            # plainly someone else (a freelance resume writer on a "Writer" job).
            # Excluded location. Apollo's own `person_not_locations` already kept these out of
            # the search, so this fires only when their filter and their record disagree — but
            # it runs on BOTH layers and on any future provider, and a filter enforced solely by
            # the vendor is not enforced (§Lessons 49).
            #
            # It comes BEFORE verification deliberately: the two answer different questions
            # ("do they work here" vs "is this the right desk"), and a person dropped for
            # location should not also be logged as failing an employer check they never took.
            place = geo.is_excluded(contact.get("location"), _exclusions())
            if place:
                log.info("Skipping %s — located in %s (%s)",
                         contact.get("full_name"), place.title(), contact.get("location"))
                excluded.append(contact.get("full_name") or "?")
                continue
            # ── the SAME exclusion, run again now that enrichment has revealed who this is ──
            #
            # The pass before selection is the cheap one and it cannot be complete: Apollo's
            # SEARCH response carries no email and a REDACTED surname (the `better_name` comment
            # below says so), so the only key available there is a truncated first name. It
            # catches a stored row that is ALSO first-name-only — 62% of them are — and misses
            # everyone whose surname a later enrichment filled in.
            #
            # That is exactly how Emilia Pavlovic landed on two webAI roles while Marcus,
            # Michael and Marcus were correctly skipped in the same search: those three are
            # stored as bare first names, and she is not. Same email, same LinkedIn URL, same
            # full name on both rows — every key matched, and none of them existed yet at the
            # only point the check ran.
            #
            # Costs no credit: enrichment has already happened. What it saves is the duplicate
            # row, its fresh cold draft, and a second unrelated email to somebody there is
            # already a conversation with.
            dup = _already_ours(contact, by_email, by_li, by_name)
            if dup:
                log.info("Skipping %s — already stored on %s",
                         contact.get("full_name"), dup.get("job_title") or "another role")
                known_elsewhere.append(dup)
                continue
            # Self-check before this reaches the dashboard.
            v = verify.verify_contact({**contact, "company": c.get("company"),
                                       "from_domain_search": c.get("from_domain_search"),
                                       "domain_source": domain_source},
                                      company, c.get("employer_domain") or "")
            if v["verdict"] == verify.REJECT:
                log.info("Dropping %s — %s", contact.get("full_name"), "; ".join(v["reasons"]))
                rejected.append(contact.get("full_name") or "?")
                continue
            contact["confidence"] = v["confidence"]
            contact["verify_note"] = "; ".join(v["reasons"])
            if not dry_run:
                cid = store.upsert_contact(contact)
                contact["id"] = cid
                # Draft outreach for anyone reachable — an EMAIL or a LINKEDIN profile. A
                # no-email contact still has a LinkedIn note (Copy note + open LinkedIn); only
                # truly unreachable contacts (no email AND no LinkedIn) are skipped.
                if draft and (contact.get("email") or contact.get("linkedin_url")):
                    _draft_and_store(_profile_for_drafting(), job, contact)
            stored_contacts.append(contact)

        # A dry run reveals nothing, so verification has no email domain to judge and every
        # further batch would be decided on identical evidence. One pass is all it can learn.
        if dry_run:
            break

    selected = considered
    result["found"] = len(considered)

    # ── HOT layer: your existing 1st-degree connections at this company (the warm approach). ──
    # Cold (Apollo, above) = strangers. Hot = people you already know there. Enrich their email
    # via Apollo (name+company+LinkedIn → email) and draft WARM outreach (reconnect email + a DM).
    if not dry_run:
        try:
            hot = _find_hot_contacts(job, company, selected, per_job=per_job,
                                     profile_fn=_profile_for_drafting, draft=draft,
                                     known=(by_email, by_li, by_name))
            stored_contacts = hot + stored_contacts  # warm contacts first
            result["hot"] = len(hot)
        except Exception as e:  # noqa: BLE001
            log.debug("Hot (connections) layer failed: %s", e)
        # Self-heal rows a previous (buggier) company match attached to this job.
        try:
            pruned = _prune_stale_connection_contacts(job_url, company)
            if pruned:
                result["pruned"] = pruned
                stored_contacts = [c for c in stored_contacts
                                   if (c.get("full_name") or "") not in set(pruned)]
                log.info("Dropped %d stale connection contact(s) at %s: %s",
                         len(pruned), company, ", ".join(pruned))
                from applypilot.database import log_event
                log_event(job_url, "outreach", "info",
                          f"Removed {len(pruned)} contact(s) who no longer match "
                          f"{company}: {', '.join(pruned)}.")
        except Exception as e:  # noqa: BLE001
            log.debug("Stale-connection prune failed: %s", e)

    result["contacts"] = stored_contacts
    result["note"] = "dry-run (no reveal)" if dry_run else "ok"
    log.info("Networking: %s → %d cold + %d hot contacts (%d with email)%s",
             company, result["found"], result.get("hot", 0), result["revealed"],
             " [dry-run]" if dry_run else "")
    if rejected:
        result["rejected"] = rejected
        result["note"] = (result["note"] + "; " if result["note"] else "") + \
            f"dropped {len(rejected)} who work elsewhere"
        log.info("Verification dropped %d contact(s) at %s: %s",
                 len(rejected), company, ", ".join(rejected))
    # Said out loud, and said SEPARATELY. A search that quietly kept 2 of 5 because three people
    # were in an excluded place looks identical to a company Apollo barely covers — §Lessons 15,
    # which is the whole reason every exit from this function logs.
    if excluded:
        result["excluded"] = excluded
        places = ", ".join(sorted({p.title() for p in _exclusions()})) or "an excluded place"
        result["note"] = (result["note"] + "; " if result["note"] else "") + \
            f"skipped {len(excluded)} in {places}"
        log.info("Location filter skipped %d contact(s) at %s: %s",
                 len(excluded), company, ", ".join(excluded))
    # People we already hold on another role at this employer. Reported SEPARATELY from every
    # other skip, because it is the only one that is not a loss: they are not gone, they are one
    # card over with a live conversation on them, and the fix is to MOVE them rather than to
    # widen a setting or correct an employer name (§Lessons 91's rule — "does not work there"
    # and "works there, already ours" are different findings and merging them makes a
    # deduplication read as a data-quality failure).
    if known_elsewhere:
        result["known_elsewhere"] = [
            {"id": m["id"], "full_name": m["full_name"], "email": m["email"],
             "job_url": m["job_url"], "job_title": m["job_title"],
             "emailed": m["emailed"], "replied": m["replied"]}
            for m in known_elsewhere]
        names = ", ".join(m["full_name"] or m["email"] for m in known_elsewhere[:4])
        result["note"] = (result["note"] + "; " if result["note"] else "") + \
            f"{len(known_elsewhere)} already on another role here ({names})"
        log.info("Skipped %d already-known contact(s) at %s: %s",
                 len(known_elsewhere), company, names)

    hot_n = result.get("hot", 0)
    dupes = (f" {len(known_elsewhere)} more are already on another role at this company — "
             f"move them across instead of writing to them twice."
             if known_elsewhere else "")
    dropped = f" Dropped {len(rejected)} who work elsewhere." if rejected else ""
    skipped = (f" Skipped {len(excluded)} based in "
               f"{', '.join(sorted({p.title() for p in _exclusions()}))}.") if excluded else ""
    if stored_contacts:
        warm = f", {hot_n} you already know" if hot_n else ""
        _log(f"Found {len(stored_contacts)} contact(s) at {company or 'the employer'} — "
             f"{result['revealed']} with a verified email{warm}.{dropped}{skipped}{dupes}")
    else:
        # Nobody survived. This is the case that used to be silent, and it is the one the
        # operator most needs explained: the search DID run and DID spend credits. Naming the
        # people who were dropped is what makes an ambiguous employer diagnosable — Apollo
        # lists three orgs called "Zello", none with a domain to disambiguate them.
        who = f" ({', '.join(rejected[:4])})" if rejected else ""
        # An empty result caused by the location filter has a DIFFERENT fix — widen the setting,
        # not the employer name — so it must not be reported as an ambiguous company.
        if known_elsewhere and not rejected and not excluded:
            # NOT a failed search. Every candidate the provider returned is somebody we already
            # have on another role here, which is the correct outcome and the whole point of the
            # exclusion — reporting it as "the employer name may match more than one company"
            # would send the operator to fix a name that is right.
            _log(f"Nobody new at {company or 'the employer'} — all "
                 f"{len(known_elsewhere)} are already on another role here. Move them to this "
                 f"application rather than starting a second conversation with them.", "warn")
        elif excluded and not rejected:
            _log(f"No contacts kept at {company or 'the employer'} — all {len(excluded)} "
                 f"considered are based in "
                 f"{', '.join(sorted({p.title() for p in _exclusions()}))}. "
                 f"Change OUTREACH_EXCLUDE_LOCATIONS to keep them.", "warn")
        else:
            _log(f"No contacts kept at {company or 'the employer'} — considered "
                 f"{result['found']} and dropped {len(rejected)} who work elsewhere{who}"
                 f"{skipped} "
                 f"The employer name may match more than one company.", "warn")
    return result


def _prune_stale_connection_contacts(job_url: str, company: str | None) -> list[str]:
    """Drop stored hot-layer contacts that no longer match a connection at `company`.

    Contact discovery only ever upserts, so a row written by a buggy matcher survives the
    fix forever — a substring bug once attached Armanino and State Farm people to an "Arm"
    job, and they stayed after the matcher was corrected. Re-running discovery now
    self-heals instead of needing a manual DELETE.

    Deliberately conservative. A row is kept if ANY of these hold, because the cost of
    deleting something real is far higher than leaving a stale row visible:
      - it was emailed or a LinkedIn invite went out (there is history on it)
      - you typed a phone number or notes on it (you invested in it by hand)
      - we cannot tell who the employer is (no company -> no basis to judge)
    """
    from applypilot.networking import connections, store
    if not company or not job_url:
        return []
    removed = []
    for c in store.get_contacts_for_job(job_url):
        if (c.get("source") or "") != "connection":
            continue
        if (c.get("sent_message_id") or "").strip() or (c.get("dm_status") or "") in ("sent", "manual"):
            continue
        if (c.get("phone") or "").strip() or (c.get("notes") or "").strip():
            continue
        rec = connections.match(c.get("full_name"), company)
        if rec and rec.get("company_match"):
            continue
        if store.delete_contact(c["id"]):
            removed.append(c.get("full_name") or c["id"])
    return removed


def _find_hot_contacts(job: dict, company: str | None, cold_selected: list[dict],
                       per_job: int, profile_fn, draft: bool,
                       known: tuple[dict, dict, dict] = ({}, {}, {})) -> list[dict]:
    """Surface + enrich + draft outreach for your existing connections at `company`.

    Skips anyone already covered by the cold Apollo layer (dedupe by normalized name). Enriches
    email via Apollo identity match (name/company/LinkedIn), stores as source='connection', and
    drafts WARM outreach (reconnect email + a DM to a known connection).

    `known` is the same three lookups the cold layer uses for "already ours at this employer, on
    another role". Without it this layer had NO cross-role check at all — it deduped only against
    the current search's own cold results — so a connection at a company you already work could
    be stored again for a second role, with a fresh warm draft. The cold path was fixed first and
    this is its other call site (§Lessons 49, the pattern this codebase keeps paying for).
    """
    from applypilot.networking import apollo, connections
    job_url = job.get("url")
    conns = connections.at_company(company, limit=per_job)
    if not conns:
        return []

    # Don't double-list someone the cold layer already found.
    cold_names = {(c.get("full_name") or "").strip().lower() for c in cold_selected}
    conns = [c for c in conns if (c.get("full_name") or "").strip().lower() not in cold_names][:per_job]
    if not conns:
        return []

    # Enrich emails via Apollo (name + company + LinkedIn URL → verified email). One credit each.
    people = [{"key": c.get("url") or c.get("full_name"), "full_name": c.get("full_name"),
               "company": company or c.get("company"), "linkedin_url": c.get("url")} for c in conns]
    enriched = apollo.match_by_identity(people)

    out = []
    for c, p in zip(conns, people):
        rev = enriched.get(p["key"], {})
        contact = {
            "job_url": job_url,
            "full_name": c.get("full_name"),
            "title": c.get("position"),
            "company": company or c.get("company"),
            "linkedin_url": rev.get("linkedin_url") or c.get("url"),
            "email": rev.get("email"),
            "email_status": rev.get("email_status", "none"),
            "match_reason": "🤝 connection — you already know them",
            "source": "connection",  # marks the HOT layer
            "apollo_id": rev.get("apollo_id"),
            "location": rev.get("location") or "",
        }
        # The hot layer has no Apollo SEARCH to filter, so the query-side exclusion cannot reach
        # it — this is the only place it applies. Same rule, one shared function.
        place = geo.is_excluded(contact.get("location"), _exclusions())
        if place:
            log.info("Skipping connection %s — located in %s (%s)",
                     contact.get("full_name"), place.title(), contact.get("location"))
            continue
        # Already ours on another role at this employer. Checked HERE, after the identity match
        # has filled in the email, for the same reason the cold layer checks after enrichment:
        # before it, a connection row is a name and a LinkedIn URL.
        dup = _already_ours(contact, *known)
        if dup:
            log.info("Skipping connection %s — already stored on %s",
                     contact.get("full_name"), dup.get("job_title") or "another role")
            continue
        cid = store.upsert_contact(contact)
        contact["id"] = cid
        if draft:  # warm draft even without an email (the DM path works for connections)
            _draft_and_store(profile_fn(), job, contact, warm=True)
        out.append(contact)
    return out
