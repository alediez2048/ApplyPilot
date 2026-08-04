"""Turning a read LinkedIn thread into rows we can store.

The extension reads the thread the operator already has open and posts what it found. Two
things have to be decided before any of it can be written, and neither belongs in a browser:

**Who is this?** LinkedIn hands over a DISPLAY name, and display names are decorated —
"Anna Ruiz, PMP", "Anna Ruiz 🚀 | Hiring", "Anna Ruiz (she/her)". The stored contact is plain.
So the match is word-level containment of the STORED name inside the displayed one, never a
substring test (§Lessons 1: `"arm" in "armanino"`, four bugs, one root cause). Ambiguity is
returned, not resolved — two people really are called Anna Ruiz, and picking one silently is
how a message gets filed against a stranger.

**When did each message arrive?** There is no machine-readable timestamp anywhere in the
thread — verified against the live DOM, not assumed: `<time>` carries a class and nothing else,
and no element anywhere in the message list holds an ISO string or an epoch. What exists is a
date heading ("TODAY") and a per-GROUP time ("2:38 AM"). Consecutive messages from one person
share one group, so they share one displayed time.

That is not a display problem. `interactions` keys a row on `sha256(contact|kind|at)`, so three
messages sent in the same minute are ONE row — two of them silently overwritten. De-colliding
is what makes the batch storable at all, and it is done here rather than in the page because
the server owns that id.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from applypilot.domain.timeutil import parse_ts

#: Decorations LinkedIn users put in the name field. Stripped before comparison, never used to
#: MATCH — a credential is not evidence about which Anna Ruiz this is.
_SUFFIX_NOISE = {
    "phd", "mba", "pmp", "cpa", "cfa", "msc", "bsc", "ma", "ms", "md", "jd", "rn", "pe",
    "csm", "cissp", "sr", "jr", "ii", "iii", "iv", "he", "him", "she", "her", "they", "them",
}


def _words(name: str) -> list[str]:
    """Comparable words in a name: accent-folded, lowercased, decoration dropped.

    Everything after a separator LinkedIn uses for a tagline is discarded first — "Anna Ruiz |
    Hiring Engineers" must not contribute "hiring" and "engineers" to a name match.
    """
    raw = (name or "").strip()
    if not raw:
        return []
    raw = re.split(r"[|·•–—]|\s-\s", raw)[0]
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    parts = re.findall(r"[a-zA-Z']+", raw.lower())
    return [p for p in parts if p not in _SUFFIX_NOISE and len(p) > 1]


#: How a candidate was arrived at. Carried to the UI, because the two are not equally
#: trustworthy and a picker that shows them identically invites the wrong click.
FULL, FIRST_ONLY = "full", "first-name"


def match_contact(display_name: str, contacts: list[dict]) -> list[dict]:
    """Contacts this LinkedIn display name could be, best first, each with `match_basis`.

    A stored name matches when EVERY one of its words appears as a whole word in the displayed
    one. That direction is deliberate: the display name is the decorated superset, so testing
    the other way round would reject "Anna Ruiz" the moment she added ", PMP".

    **Single-word stored names match on the first name alone, and say so.** They were refused
    outright at first, on the reasoning that "Anna" matches every Anna on LinkedIn — correct in
    principle and useless here: measured on the live board, **162 of 185 contacts have no
    surname**, because Apollo's people search redacts it (see `better_name`). Refusing them sent
    88% of threads to a manual picker, which is the ten-step flow this exists to remove.

    The safety is that this returns CANDIDATES, never a decision. A first-name match is tagged
    `FIRST_ONLY`, the popup flags it, and the operator confirms against the company shown beside
    the name. As enrichment backfills surnames these become `FULL` on their own.
    """
    shown = _words(display_name)
    if not shown:
        return []
    seen = set(shown)
    out = []
    for c in contacts or []:
        stored = _words(c.get("full_name") or "")
        if not stored:
            continue
        if len(stored) >= 2:
            if not set(stored) <= seen:
                continue
            basis, rank = FULL, len(stored)
        else:
            # Only against the FIRST word shown. "Anna" must not match "Ruiz Anna-Maria" via a
            # word that happens to sit elsewhere in a decorated headline.
            if stored[0] != shown[0]:
                continue
            basis, rank = FIRST_ONLY, 0
        # An exact word-set match beats a partial one: given "Anna Ruiz" and "Anna Maria Ruiz"
        # stored, and "Anna Maria Ruiz" displayed, both match and the longer is the better read.
        out.append((basis == FULL, len(stored) == len(shown), rank, {**c, "match_basis": basis}))
    out.sort(key=lambda t: (-int(t[0]), -int(t[1]), -t[2]))
    return [c for _, _, _, c in out]


def better_name(stored: str, offered: str) -> str:
    """The better of two names for the same person — always `stored` unless `offered` wins.

    Apollo's people SEARCH redacts surnames and its enrichment response carries them, so a
    contact stored as "Sage" can be upgraded to "Sage Soronen" for free on the next enrichment.
    162 of 185 live contacts are in that state.

    An upgrade is accepted ONLY when the offered name is a strict extension of the stored one:
    more words, and every stored word still present. Anything looser lets a mismatched
    enrichment row rename a contact into a different human, which is worse than a first name —
    a wrong first name is obvious in a greeting, and a wrong full name is not.
    """
    old, new = _words(stored), _words(offered)
    if not new or len(new) <= len(old) or not set(old) <= set(new):
        return (stored or "").strip()
    return (offered or "").strip()


#: Messages LinkedIn renders in one group share one displayed time, so a batch routinely
#: arrives with duplicates. Bumping by whole seconds keeps the displayed MINUTE truthful.
_NUDGE = timedelta(seconds=1)


def dedupe_times(messages: list[dict]) -> list[dict]:
    """Give every message in a batch a distinct `at`, in the order they were read.

    Deterministic, which is what makes re-reading the same thread a no-op rather than a
    duplicate: identical input resolves to identical timestamps, so `interactions.record`
    upserts the same rows (§Lessons 22 — an afternoon of ticks once produced eleven identical
    BOUNCED entries because a terminal state stayed in the pool).

    A message whose timestamp cannot be parsed keeps `at` empty and is left for the caller;
    inventing one would put a message at a time it demonstrably did not arrive.
    """
    out, taken = [], set()
    for msg in messages or []:
        row = dict(msg)
        when = parse_ts(row.get("at") or "")
        if when is None:
            row["at"] = ""
            out.append(row)
            continue
        while when.isoformat() in taken:
            when = when + _NUDGE
        taken.add(when.isoformat())
        row["at"] = when.isoformat()
        out.append(row)
    return out
