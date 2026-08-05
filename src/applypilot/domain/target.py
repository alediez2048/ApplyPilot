"""A company you want to work with, as a row. Pure — no SQL, no HTTP.

A target is the `pipeline/targets` counterpart of a job posting, and the difference that
matters is where the identity comes from. A job's identity is its URL, scraped and then
reverse-engineered back into an employer by `networking/derive.py` — 2,000 lines of rules that
produced the employers "Ouryahoo", "Edu", "Ats", "Hr" and "Uploaded" (§Lessons 20, 49, 52).

**Here the operator states the company, so none of that machinery runs.** The anchor is built
FROM the name rather than the name being recovered from a URL. That is the single biggest
correctness difference between the two shapes, and it is worth being explicit that this path is
simpler rather than newer.

`target:<space_id>:<slug>` is hashed into every `contact_id` in the Space (`store.contact_id`),
which is why `spaces-prd.md` §13.2 freezes a Space's id and why the slug is never recomputed for
a row that already exists.
"""

from __future__ import annotations

import re
import unicodedata

PREFIX = "target"

#: Long enough for a real company name, short enough that the anchor stays readable in a log
#: line. "The Trinity House Lighthouse Service" is 36 characters slugified.
_MAX_SLUG = 60

#: Words that are punctuation in a company name rather than part of it. Stripped from the END
#: only — "Inc" is noise in "Ridgeline Logistics Inc" and load-bearing in "Inc Magazine".
_SUFFIXES = ("inc", "llc", "ltd", "limited", "corp", "corporation", "co", "plc", "gmbh",
             "sa", "sas", "bv", "ag", "pty", "pte", "srl", "ab", "oy", "as")


def slug(name: str | None) -> str:
    """A company name as a URL-safe segment: "Ridgeline Logistics, Inc." -> "ridgeline-logistics".

    Deliberately NOT `domain/deck.slugify`, which takes the FIRST WORD only because it is
    naming a person for a link they will read ("/intro/gina"). A company slugged that way is
    "ridgeline", which collides with every other Ridgeline the operator ever adds — and a
    collision here is not a cosmetic problem, it is two companies sharing one row and one set
    of contacts.
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    # `&` is a real word in company names ("Johnson & Johnson"); dropping it silently joins two
    # halves that read as one word.
    folded = folded.replace("&", " and ")
    parts = [p for p in re.split(r"[^a-z0-9]+", folded.lower()) if p]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return "-".join(parts)[:_MAX_SLUG].strip("-")


def anchor(space_id: str, name: str | None) -> str:
    """The row's primary key AND its contacts' anchor. '' when the name yields no slug.

    Space-scoped on purpose (`spaces-prd.md` §5): the same company pursued in two Spaces is
    deliberately two rows, because the pitch, the identity sending it and the conversation are
    all different. Deduping them is `crm-prd.md`'s person-as-root job, not this one.
    """
    s = slug(name)
    return f"{PREFIX}:{space_id}:{s}" if s and space_id else ""


def parse_anchor(value: str | None) -> tuple[str, str] | None:
    """`target:acme:ridgeline-logistics` -> ("acme", "ridgeline-logistics"). None if it is not one.

    Split from the LEFT with a bounded count, so a Space id containing a colon cannot silently
    eat the slug — and matched on the whole first segment rather than with `startswith`, because
    `"target" in ...` is the substring habit that cost this codebase four shipped bugs
    (§Lessons 1).
    """
    parts = (value or "").split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def is_target(value: str | None) -> bool:
    return parse_anchor(value) is not None


_DOMAIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+)(?:[/?#]|$)", re.I)


def parse_line(line: str) -> dict | None:
    """One typed line -> {"name", "domain"}. None when there is no company in it.

    Accepts what a person actually pastes out of a directory or a note:

        Ridgeline Logistics
        Ridgeline Logistics, ridgeline.com
        Ridgeline Logistics — https://www.ridgeline.com/about
        ridgeline.com

    The domain is taken as given rather than confirmed. That is the opposite of the jobs path,
    where `confirm_employer_domain()` makes Apollo corroborate a GUESS before it is trusted
    (§Lessons 34) — the difference is that a guess needs corroborating and an operator's
    statement does not.
    """
    raw = (line or "").strip().strip(",;")
    if not raw:
        return None

    domain = ""
    m = _DOMAIN_RE.search(raw)
    if m:
        domain = m.group(1).lower()
        raw = (raw[:m.start()] + " " + raw[m.end():]).strip()

    # Separators a human uses between a name and a note. Everything after the FIRST one is
    # dropped: the name is what identifies the row, and a trailing note would change the slug.
    name = re.split(r"\s+[—–|]\s+|,\s+", raw, maxsplit=1)[0].strip(" -—–|,")

    if not name and domain:
        # "ridgeline.com" on its own — the label before the TLD is the best name available, and
        # saying so beats refusing a line the operator clearly meant.
        name = domain.split(".")[0].replace("-", " ").title()
    if not slug(name):
        return None
    return {"name": name, "domain": domain}


def parse_input(text: str | None) -> tuple[list[dict], list[str]]:
    """A pasted block -> (targets, rejected lines).

    Rejects are RETURNED, never dropped. A paste of twelve lines that quietly imports nine is
    the failure §Lessons 15 is about — the operator has no way to see which three are missing,
    and "0 imported" and "9 of 12 imported" need to read differently.

    Deduplicated by slug within the paste, because "Acme" and "Acme, Inc." on two lines are one
    company and would otherwise be one row imported twice.
    """
    targets: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parsed = parse_line(line)
        if not parsed:
            rejected.append(line.strip())
            continue
        key = slug(parsed["name"])
        if key in seen:
            continue
        seen.add(key)
        targets.append(parsed)
    return targets, rejected
