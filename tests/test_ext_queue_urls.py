"""Extension queue contract tests — catch the class of bug that kept breaking Start.

The Chrome extension refuses to Start unless every queued contact's `linkedin_url` matches
its client-side validator (extension/shared/constants.js: LINKEDIN_PROFILE_RE). Providers like
Apollo return `http://www.linkedin.com/in/...`, which that regex rejects — producing
"No valid LinkedIn profile URLs in the queue" and a dead Start button.

These tests mirror the extension's EXACT regex here and assert that the queue payload the
dashboard serves always passes it. If someone tightens the regex, changes the normalizer, or a
new provider introduces a new URL shape, this fails in CI instead of silently on the user.
"""

from __future__ import annotations

import re

import pytest

from applypilot.web_dashboard import _normalize_linkedin_url, _queue_contact_payload

# MUST stay identical to extension/shared/constants.js `LINKEDIN_PROFILE_RE`.
EXT_LINKEDIN_PROFILE_RE = re.compile(r"^https://([a-z]+\.)?linkedin\.com/in/")


def _ext_accepts(url: str) -> bool:
    """True iff the extension's client-side validator would accept this URL."""
    return bool(EXT_LINKEDIN_PROFILE_RE.match(url or ""))


# Real-world shapes we've actually seen (Apollo, Hunter, hand-entered, already-clean).
@pytest.mark.parametrize(
    "raw",
    [
        "http://www.linkedin.com/in/sage-soronen-01716b32",   # Apollo (the bug)
        "http://linkedin.com/in/blumerica",                    # http, no www
        "https://www.linkedin.com/in/samerzaben",              # already clean
        "https://linkedin.com/in/foo",                          # clean, no www
        "www.linkedin.com/in/bar",                              # bare, no protocol
        "linkedin.com/in/baz",                                  # bare, no www/protocol
        "//www.linkedin.com/in/qux",                            # protocol-relative
    ],
)
def test_normalized_urls_pass_the_extension_regex(raw):
    """Every known URL shape, once normalized, must satisfy the extension's validator."""
    normalized = _normalize_linkedin_url(raw)
    assert _ext_accepts(normalized), f"{raw!r} -> {normalized!r} still rejected by the extension"


def test_queue_payload_url_is_extension_valid():
    """The actual payload row the dashboard serves must carry an extension-valid URL."""
    contact = {
        "id": "c1",
        "full_name": "Erica Blum",
        "title": "Recruiter",
        "company": "BetterUp",
        "linkedin_url": "http://www.linkedin.com/in/blumerica",  # Apollo http form
        "linkedin_message": "hi",
    }
    row = _queue_contact_payload(contact)
    assert _ext_accepts(row["linkedin_url"]), f"payload URL rejected: {row['linkedin_url']!r}"


def test_empty_url_stays_empty():
    assert _normalize_linkedin_url("") == ""
    assert _normalize_linkedin_url(None) == ""


def test_non_linkedin_bare_string_not_falsely_prefixed():
    # A non-LinkedIn bare string shouldn't be coerced into a fake https LinkedIn URL.
    out = _normalize_linkedin_url("example.com/in/nope")
    assert not _ext_accepts(out)
