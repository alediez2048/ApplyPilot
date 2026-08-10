"""How to NAME a job in a message, without pasting a URL at somebody.

The first version of this sent the posting link. It worked and it read badly: a Workday URL is
141 characters, and inline in an opening sentence it turns a personal note into something
forwarded by a bot. What a human writes is the role's name, and a requisition number if the
person on the other end is the sort who would look one up.

Both halves needed measuring, and the surprise was which one carried the work.

**The requisition number is rare.** 8 of 33 live rows have one recoverable from the URL —
`JR349466`, `REQ-12289`, `REF085286W`, `R-108281`. It never appears in the posting TEXT on any
of them, so there is no corroboration available; it is taken from the job's own URL path, which
is the one place it is reliable.

**The title is common and frequently unusable — 11 of 33.** This is the real work:

    "Webai uploaded job"                     the scraper placeholder, on 5 rows
    "LegalZoom Careers"                      the page title of a careers index, not a role
    "Program Manager, Customer &amp; …"      an HTML entity that was never decoded
    "Salesforce - Forward Deployed Engineer" the employer, prefixed onto its own role
    "Google hiring AI Sales Specialist, …"   an aggregator's sentence, not a title

Quoting any of those to a recruiter is worse than naming no role at all: "I applied for the
Webai uploaded job role" is unrecoverable in a first sentence, and §Lessons 42 caught exactly
that shape reaching a live draft ("the Betterup uploaded job"). So an unusable title resolves to
`""`, and the caller must be able to say nothing rather than say that.
"""

from __future__ import annotations

import html
import re
from urllib.parse import unquote, urlsplit

#: Requisition shapes a human recognises on a posting and can paste into their own ATS. The
#: lookbehind/lookahead are character classes rather than `\b`, because `_` is a word character:
#: `..._REQ-12289` and `..._REF085286W` both failed a `\b` test and two live rows went missing.
_REQ = re.compile(r"(?<![A-Za-z0-9])((?:JR|REQ|REF|R)[-_ ]?\d{3,}[A-Z]?)(?![A-Za-z0-9])")

#: NOT included: a bare number (`446718602839`), a Greenhouse job id (`7778204003`) or an Ashby
#: uuid. Those are internal keys, not requisitions — a recruiter would not recognise one, and
#: quoting a machine id at somebody is the exact tell this change exists to remove.

#: What the scraper writes when it could not read a title. `collect_detail_intelligence`
#: backfilled 17 of 22, and the 5 that failed are expired postings and auth walls, so this is a
#: permanent state rather than a transitional one.
_PLACEHOLDER = re.compile(r"^\s*\S.*\buploaded job\s*$", re.I)

#: A careers INDEX page, whose <title> is the company's, not a role's.
_INDEX_PAGE = re.compile(r"^\s*(.*\b)?careers?\s*$", re.I)


def _strip_employer(title: str, company: str) -> str:
    """Remove the employer from its own job title, at either end.

    Boards write "Salesforce - Forward Deployed Engineer" and "Senior AI Engineer at Acme". The
    email already names the company in its own sentence, so leaving it here produces "the
    Salesforce - Forward Deployed Engineer role at Salesforce".

    Anchored and separator-bound, never a substring replace (§Lessons 1): a company called "AI"
    must not eat the "AI" out of "Senior AI Engineer".
    """
    name = (company or "").strip()
    if not name:
        return title
    esc = re.escape(name)
    title = re.sub(rf"^\s*{esc}\s*[-–—:|]\s*", "", title, flags=re.I)
    title = re.sub(rf"\s*[-–—:|]\s*{esc}\s*$", "", title, flags=re.I)
    title = re.sub(rf"\s+at\s+{esc}\s*$", "", title, flags=re.I)
    return title.strip()


#: "Google hiring AI Sales Specialist, Startups, Google Cloud in Austin, TX" — a board's sentence
#: ABOUT a posting, not the posting's own title. Deliberately independent of the resolved
#: employer: the live row carrying this shape has `company` stored as **"Uploaded"**, because its
#: URL is a `linkedin.com/jobs/view/…` from which no employer is recoverable at all. A rule that
#: needed the company name would have been unable to fire on the one row that has the problem.
_AGGREGATOR_SENTENCE = re.compile(r"^\s*[A-Z][\w.&' -]{1,40}?\s+hiring\s+", re.I)


def _strip_aggregator(title: str) -> str:
    return _AGGREGATOR_SENTENCE.sub("", title).strip()


def clean_title(title: str | None, company: str | None = None) -> str:
    """The role's name as it can be said out loud, or "" when there is nothing sayable.

    Returning "" is a real answer and the caller has to handle it. A third of the live rows carry
    a title that would embarrass the sender if quoted, and the honest move on those is to write
    an email that does not name a role rather than one that names the wrong thing.
    """
    text = html.unescape((title or "").strip())
    if not text:
        return ""
    # Aggregator sentence first: it is the only rule that can leave a usable title behind, and
    # the placeholder/index tests below would otherwise never see what is inside it.
    text = _strip_aggregator(text)
    text = _strip_employer(text, company or "")
    if _PLACEHOLDER.match(text) or _INDEX_PAGE.match(text):
        return ""
    text = re.sub(r"\s+", " ", text).strip(" -–—:|,")
    # A "title" that is only the company name says nothing a reader does not already know.
    if company and text.lower() == (company or "").strip().lower():
        return ""
    return text


def requisition(job: dict) -> str:
    """The posting's requisition id, or "".

    Read from the URL PATH only. A query string carries tracking and session junk, and a match
    there would be a coincidence rather than an identifier.
    """
    for key in ("url", "application_url"):
        raw = (job or {}).get(key) or ""
        if not raw or raw.startswith("target:"):
            continue
        try:
            path = urlsplit(unquote(raw)).path
        except ValueError:
            continue
        match = _REQ.search(path)
        if match:
            return match.group(1).upper()
    return ""


def posting_reference(job: dict, shape: str = "pipeline/jobs") -> dict:
    """{"title": …, "req": …} — how to refer to this job in a message. Either may be "".

    Jobs shape only: a targets Space pitches a company and there is no posting to name.
    """
    if shape == "pipeline/targets":
        return {"title": "", "req": ""}
    job = job or {}
    return {"title": clean_title(job.get("title"), job.get("company")),
            "req": requisition(job)}
