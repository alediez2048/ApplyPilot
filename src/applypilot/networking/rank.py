"""Pure ranking + title-synonym helpers for contact selection.

Ranks Apollo candidates (which are masked — no email/LinkedIn yet) purely on title/
seniority, and picks a useful mix: peers in the role plus at least one recruiter / hiring
manager. No I/O — trivially testable.
"""

from __future__ import annotations

import re

_RECRUITER_TITLES = [
    "Technical Recruiter", "Recruiter", "Talent Acquisition", "Talent Partner",
    "Sourcer", "People Operations", "Head of Talent",
]
#: Public: the recruiter side of the search is now its own query, so callers need these.
RECRUITER_TITLES = _RECRUITER_TITLES

# Words that mark a hiring-side contact.
_RECRUITER_RE = re.compile(
    r"\b(recruit|talent|sourc|people ops|people operations|hr\b|human resources)", re.I
)
_HIRING_MGR_RE = re.compile(r"\b(hiring manager|engineering manager|director|head of|vp|chief)\b", re.I)

_STOP = {"senior", "sr", "staff", "lead", "principal", "junior", "jr", "i", "ii", "iii",
         "the", "of", "and", "&", "a", "an", "at"}


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOP and len(w) > 1}


def role_to_person_titles(job_title: str | None) -> list[str]:
    """Map a job title to Apollo person_titles[] + synonyms + recruiter titles."""
    titles: list[str] = []
    if job_title:
        base = job_title.strip()
        titles.append(base)
        # de-seniored variant (drop leading Senior/Staff/Lead/Principal)
        stripped = re.sub(r"^(senior|sr\.?|staff|lead|principal|junior|jr\.?)\s+", "", base, flags=re.I).strip()
        if stripped and stripped != base:
            titles.append(stripped)
    # always include recruiter/talent so a hiring contact surfaces
    titles.extend(_RECRUITER_TITLES[:3])
    # de-dup preserving order
    seen: set[str] = set()
    out = []
    for t in titles:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def is_recruiter(title: str | None) -> bool:
    """True for a hiring-side title. Public because the peer query has to exclude them:
    "Talent Acquisition Strategist" matches a search for "Strategist"."""
    return bool(title and _RECRUITER_RE.search(title))


#: Words that carry no signal on their own. Broadening down to "Manager" or "Specialist" returns
#: an arbitrary slice of the company rather than anyone doing this job.
_WEAK_HEADS = {"manager", "specialist", "associate", "analyst", "coordinator", "consultant",
               "director", "officer", "executive", "administrator", "generalist", "professional"}

#: Words that are never any part of a real job title. The dashboard import writes
#: `title = f"{company} uploaded job"` for every pasted URL, so broadening once reached the bare
#: word "job" — and at Yahoo that returned four colleagues titled "Job", "Student Job", "No job"
#: and "Job Captain". The title is fixed at the source now (`detail._clean_role_title`); this is
#: the second layer, because a search term is the last place to discover the input was junk.
_NON_ROLE_WORDS = {"job", "jobs", "uploaded", "role", "roles", "position", "positions",
                   "opening", "openings", "career", "careers", "vacancy", "posting", "apply"}


def peer_titles(job_title: str | None) -> list[str]:
    """Title queries for people who would be COLLEAGUES, widest-useful first.

    Measured at Yahoo against the live API, which is the only reason this function has the shape
    it does:

        ["AI Operations Strategist"]  -> 0 people
        ["Operations Strategist"]     -> 0 people
        ["Strategist"]                -> 25 people

    A bespoke multi-word title matches nobody. Employers invent titles; Apollo indexes the ones
    people actually put on LinkedIn. So the exact title is tried first (when it hits, it is the
    best possible match) and then progressively shortened from the FRONT, because English job
    titles put the qualifier first and the function last: "Senior Technical Program Manager" is
    a Program Manager, not a Senior Technical.

    Never includes a recruiter title. This is one half of a two-query mix, and blending them is
    exactly what produced 25 recruiters and 0 peers on a real job.
    """
    if not job_title:
        return []
    base = re.sub(r"\s*[(\[].*?[)\]]", "", job_title).strip()
    base = re.sub(r"\s*[-–—,|/].*$", "", base).strip()   # "PM, Search" / "PM - Remote"
    if not base:
        return []

    out: list[str] = [base]
    stripped = re.sub(r"^(senior|sr\.?|staff|lead|principal|junior|jr\.?)\s+", "",
                      base, flags=re.I).strip()
    if stripped and stripped != base:
        out.append(stripped)

    words = stripped.split()
    # Shorten from the front: "AI Operations Strategist" -> "Operations Strategist" -> "Strategist"
    for i in range(1, len(words)):
        tail = " ".join(words[i:])
        # A bare weak head ("Manager") is not a role, it is a rank. Two words keep the function.
        if len(tail.split()) == 1 and tail.lower() in _WEAK_HEADS:
            continue
        out.append(tail)

    seen: set[str] = set()
    deduped = [t for t in out if not (t.lower() in seen or seen.add(t.lower()))]
    # Drop anything built out of non-role vocabulary. A query for "job" is not a narrow search
    # that happens to return the wrong people — it is a search with no subject.
    return [t for t in deduped
            if not (set(re.findall(r"[a-z]+", t.lower())) <= _NON_ROLE_WORDS)]


def _match_reason(title: str | None, role: str | None, overlap: int) -> str:
    if title and _RECRUITER_RE.search(title):
        return "recruiter"
    if title and _HIRING_MGR_RE.search(title):
        return "hiring manager"
    if overlap >= 2:
        return "same role"
    return "same team"


def _score(candidate: dict, role_tokens: set[str]) -> tuple[int, int]:
    """(title overlap with role, seniority weight) — higher is better."""
    title = candidate.get("title") or ""
    overlap = len(_tokens(title) & role_tokens)
    seniority = (candidate.get("seniority") or "").lower()
    weight = {"c_suite": 1, "vp": 2, "head": 3, "director": 4, "manager": 5,
              "senior": 6, "entry": 4, "intern": 1}.get(seniority, 5)
    return (overlap, weight)


def select(candidates: list[dict], role: str | None, n: int = 5) -> list[dict]:
    """Pick the best up-to-n candidates: relevant peers + ≥1 recruiter/hiring contact.

    Returns candidates annotated with `match_reason`, ordered best-first.
    """
    if not candidates:
        return []
    role_tokens = _tokens(role)

    scored = []
    for c in candidates:
        title = c.get("title") or ""
        overlap = len(_tokens(title) & role_tokens)
        is_hiring = bool(_RECRUITER_RE.search(title) or _HIRING_MGR_RE.search(title))
        scored.append((c, overlap, is_hiring, _score(c, role_tokens)))

    # peers (non-hiring), ranked by title overlap then seniority
    peers = sorted(
        [s for s in scored if not s[2]], key=lambda s: s[3], reverse=True
    )
    hiring = sorted(
        [s for s in scored if s[2]], key=lambda s: s[3], reverse=True
    )

    chosen: list[tuple] = []
    # guarantee at least one hiring contact if available
    if hiring:
        chosen.append(hiring[0])
    # fill the rest with top peers, then remaining hiring
    for pool in (peers, hiring[1:]):
        for s in pool:
            if len(chosen) >= n:
                break
            if s not in chosen:
                chosen.append(s)

    out = []
    for c, overlap, _is_hiring, _sc in chosen[:n]:
        annotated = dict(c)
        annotated["match_reason"] = _match_reason(c.get("title"), role, overlap)
        out.append(annotated)
    return out


def select_mix(peers: list[dict], recruiters: list[dict], role: str | None, *,
               min_peers: int = 4, min_recruiters: int = 4, n: int = 0) -> list[dict]:
    """Order two separately-sourced pools so both sides survive to the end of the list.

    `select()` guaranteed "at least one recruiter" and filled the rest with peers, which sounds
    balanced and is not: it can only choose from the pool it is given, and the blended Apollo
    query returned **25 recruiters and 0 peers** on a real Yahoo job. Ranking was working
    perfectly on a pool that had already lost the argument.

    So the mix is decided here, by INTERLEAVING rather than by scoring. The caller enriches down
    this list in batches and drops whoever fails verification, so the two sides have to stay
    interwoven the whole way — front-loading four peers means a company whose first four peers
    all fail verification ends up all-recruiter again, which is the bug restated.

    Quotas are minimums, not caps: if one side is short, the other fills the gap rather than
    leaving the operator with fewer people than the provider actually had.
    """
    role_tokens = _tokens(role)

    # Both pools are merged and then split by TITLE, never by which query produced someone. A
    # "Talent Acquisition Strategist" answers a search for "Strategist"; counting them as a
    # colleague rebuilds the imbalance, and discarding them throws away a real contact who
    # simply belongs on the other side.
    def _rank(recruiter_side: bool) -> list[dict]:
        wanted = [c for c in (*peers, *recruiters)
                  if is_recruiter(c.get("title")) is recruiter_side]
        return sorted(wanted, key=lambda c: _score(c, role_tokens), reverse=True)

    ranked_peers = _rank(False)
    ranked_recruiters = _rank(True)

    total = n or (len(ranked_peers) + len(ranked_recruiters))
    out: list[dict] = []
    seen: set = set()
    # Round-robin, weighted so each side reaches its minimum inside the first `min_*` picks.
    queues = [(ranked_peers, max(1, min_peers)), (ranked_recruiters, max(1, min_recruiters))]
    idx = {0: 0, 1: 0}
    while len(out) < total:
        progressed = False
        for q, (pool, share) in enumerate(queues):
            for _ in range(share):
                if idx[q] >= len(pool) or len(out) >= total:
                    break
                c = pool[idx[q]]
                idx[q] += 1
                key = c.get("key") or c.get("apollo_id") or id(c)
                if key in seen:
                    continue
                seen.add(key)
                overlap = len(_tokens(c.get("title")) & role_tokens)
                annotated = dict(c)
                annotated["match_reason"] = _match_reason(c.get("title"), role, overlap)
                annotated["side"] = "recruiter" if q else "peer"
                out.append(annotated)
                progressed = True
        if not progressed:
            break
    return out
