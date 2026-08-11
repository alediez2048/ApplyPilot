"""Places contact discovery will not keep people from.

The operator's own targeting rule: a US job search reaches a company's US recruiting org, and a
recruiter at the same employer's Bangalore office is the wrong desk for an Austin requisition.
This is who to COLD-EMAIL, decided by the person doing the emailing. It never touches a stored
contact, and it makes no judgement about anybody.

Two properties carry it, and both come from what the data actually looks like:

**A country is matched through its CITIES.** Apollo returns "Bengaluru, Karnataka, India" for
some people and "Bengaluru" for others — the same person, and only one of those strings contains
the word the operator typed. Matching the country alone silently keeps everybody whose row
happens to be city-only.

**Whole words, never substrings.** §Lessons 1 is four bugs in this repo already, and the failures
here are live ones: "India" is inside "Indiana" and "Indianapolis", so a substring rule aimed at
Bangalore quietly drops Indianapolis. `test_indiana_is_not_india` is the case that matters.

An UNKNOWN location is KEPT. Apollo does not always return one, and dropping a person because a
provider left a field blank would silently shrink every search on missing data rather than on
the rule — §Lessons 34's shape, where a guess was fed to a check that treated it as proof.
"""

from __future__ import annotations

import re

#: Cities that identify a country the operator may exclude. Only the ones big enough to appear
#: as a bare `city` in a provider response — this is a matching aid, not a gazetteer, and
#: anything missing simply falls back to the country name being present.
_CITIES: dict[str, tuple[str, ...]] = {
    "india": (
        "bangalore", "bengaluru", "mumbai", "bombay", "pune", "chennai",
        "new delhi", "noida", "gurgaon", "gurugram", "kolkata", "calcutta",
        "ahmedabad", "jaipur", "coimbatore", "kochi", "cochin", "thiruvananthapuram",
        "chandigarh", "indore", "nagpur", "vadodara", "bhubaneswar", "mysore", "mysuru",
        "visakhapatnam", "trivandrum",
    ),
}

#: Cities whose NAME is not proof of the country, so they are only accepted when the country is
#: named too. Every one of these is a real place somewhere else:
#:
#:     Delhi        Ohio, California, New York, Ontario
#:     Madras       Oregon
#:     Hyderabad    Sindh, Pakistan
#:
#: Found by a test, not by review: "Delhi Township, Ohio" excluded as India. That is §Lessons 1
#: wearing a different hat — the string really does contain the word, and the word really is not
#: about the place. A bare `hyderabad` staying in the plain list would also have made the filter
#: quietly reach a second country the operator never named.
_AMBIGUOUS: dict[str, tuple[str, ...]] = {
    "india": ("delhi", "madras", "hyderabad"),
}


def parse_exclusions(raw: str | None) -> tuple[str, ...]:
    """"India, Pakistan" -> ("india", "pakistan"). Empty means the filter is off."""
    return tuple(p.strip().lower() for p in (raw or "").split(",") if p.strip())


def query_terms(exclusions: tuple[str, ...]) -> list[str]:
    """What to hand a provider that filters server-side.

    The plain names, capitalised — Apollo matches its own place names, and sending our city list
    would be guessing at their taxonomy. The city list is for OUR check on what comes back.
    """
    return [e.title() for e in exclusions]


_WORD = re.compile(r"[a-z0-9]+")


def _names(words: list[str], term: str) -> bool:
    """Whole words, adjacent. "new delhi" must not be satisfied by "New York" plus a stray
    "delhi", and "India" must never be found inside "Indiana"."""
    parts = term.split()
    return any(words[i:i + len(parts)] == parts
               for i in range(len(words) - len(parts) + 1))


def is_excluded(location: str | None, exclusions: tuple[str, ...]) -> str:
    """The excluded place this location belongs to, or "" — a blank location is never excluded.

    Returns the PLACE rather than a bool so the caller can say which rule fired. "dropped 3" is
    an answer nobody can check; "dropped 3 in India" is one they can.
    """
    words = _WORD.findall((location or "").lower())
    if not words or not exclusions:
        return ""
    for place in exclusions:
        if _names(words, place):
            return place
        if any(_names(words, city) for city in _CITIES.get(place, ())):
            return place
        # An ambiguous city counts only with its country beside it — and the country was already
        # checked above, so reaching here means it is absent. It still matches when the string
        # is NOTHING BUT the city ("Delhi"), because a provider returning a bare city with no
        # qualifier is the shape the city list exists for at all.
        if any(_names(words, city) for city in _AMBIGUOUS.get(place, ())) and len(words) == 1:
            return place
    return ""


def split(candidates: list[dict], exclusions: tuple[str, ...],
          key: str = "location") -> tuple[list[dict], list[dict]]:
    """(kept, dropped). Order is preserved — ranking already decided it and this is a filter."""
    if not exclusions:
        return list(candidates), []
    kept, dropped = [], []
    for c in candidates:
        place = is_excluded((c or {}).get(key), exclusions)
        if place:
            dropped.append({**c, "excluded_place": place})
        else:
            kept.append(c)
    return kept, dropped
