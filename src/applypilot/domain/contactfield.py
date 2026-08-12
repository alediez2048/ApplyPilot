"""The identifiers on a contact — email, LinkedIn, phone — cleaned once. Pure.

Two paths now write these fields: a pasted sheet, and the operator typing one into the contact
card. That is exactly the shape where one path enforces a rule and the other quietly does not
(§Lessons 49, which has fired at two call sites, four call sites, and once across two SHAPES of
the same Space). So the cleaning lives here and both import it.

**Nothing here rejects a person.** A malformed address loses the address, never the contact —
the same rule the sheet parser already applies, and for the same reason: a name and a title are
worth keeping when the email cell is a typo.
"""

from __future__ import annotations

import re

#: Longer than any real address; a bound, not a validation.
EMAIL_MAX = 254
LINKEDIN_MAX = 400
PHONE_MAX = 40

_URL_JUNK = re.compile(r"^https?://(www\.)?", re.I)

#: A bare LinkedIn handle: what you get from copying the last segment of a profile URL.
#: Deliberately narrow — anything with a dot, a slash or a space is a URL, a path or prose, and
#: must be left alone rather than guessed at.
_BARE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{2,99}$", re.I)


def clean_linkedin(value: str) -> str:
    """A full URL, a `/in/…` path, or a bare handle -> a URL. Anything else is kept as given.

    Not validated beyond looking like a profile: a sheet may carry a company page or a search
    link, and refusing over it would lose the person's name and title too.

    The bare-handle case is here because it is what the operator has in their hand — copying the
    last segment out of the address bar is the normal way to note somebody down, and stored raw
    it renders as a link that navigates nowhere.
    """
    v = (value or "").strip()[:LINKEDIN_MAX]
    if not v:
        return ""
    if v.startswith("/in/") or v.startswith("in/"):
        return "https://www.linkedin.com/" + v.lstrip("/")
    if _BARE_HANDLE.match(v) and "." not in v:
        return "https://www.linkedin.com/in/" + v
    return v


def clean_email(value: str) -> str:
    """Lowercased and trimmed, or "" when it could not be an address.

    An address with no `@` reaches nobody, so storing it buys a field that looks populated and a
    send that fails later, further from the typo that caused it.
    """
    v = (value or "").strip().lower()[:EMAIL_MAX]
    if not v or "@" not in v or v.startswith("@") or v.endswith("@") or " " in v:
        return ""
    return v


def email_problem(value: str) -> str:
    """Why an address was refused, or "" when it is fine (or absent).

    Separate from `clean_email` because the two callers need different things: the sheet parser
    reports it as one rejected ROW among many, and the contact card has to put a sentence next
    to the box the operator just typed into. A silent drop there is an edit they watched succeed
    and which never happened (§Lessons 75).
    """
    v = (value or "").strip()
    if not v:
        return ""
    return "" if clean_email(v) else f"{v!r} does not look like an email address"


def clean_phone(value: str) -> str:
    """Kept as typed. Formats vary by country and normalising is how a number stops dialling."""
    return (value or "").strip()[:PHONE_MAX]
