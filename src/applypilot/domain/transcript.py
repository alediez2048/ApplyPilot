"""A meeting transcript — what it IS, how it is keyed, and what may reach a prompt. Pure.

GRAN-1. Three nouns that must not collapse into each other: a transcript is about a MEETING, a
meeting has ATTENDEES, and an attendee maps to CONTACTS. Storing the text on a contact duplicates
a 40 KB blob per person and lets the copies drift; storing it on a job is wrong twice, because a
call can span two roles at one employer and a `pipeline/targets` Space has no posting at all.

**Only the SUMMARY may reach a prompt.** The body runs 20–50 KB on a real 45-minute call, which
is most of a context window — and §Lessons 40 is that the loudest block in a prompt wins, so a
transcript dropped in whole would swamp the posting, the premise and the voice at once.

**Speakers cannot be attributed.** Granola diarizes by AUDIO SOURCE (microphone vs system), not
by person, so a transcript says which lines are the operator and which are "everyone else on the
call". Nothing here may claim to know which of three attendees said something — that would be
inventing it, and the invention would be quoted back to the person it was invented about.
"""

from __future__ import annotations

import hashlib
import re

#: The stored body cap, enforced at the WRITE and SERVED to the frontend rather than written
#: twice — a bound in two places is two bounds, which is how the intro-deck PDF rode along on 34
#: real emails while `doctor --config` reported it off (§Lessons 90).
#:
#: 200 KB is about four hours of speech. Past that it is a recording session, not a meeting.
TRANSCRIPT_MAX = 200_000

#: What may reach a drafting prompt. Deliberately close to the length of a paragraph a human
#: would write about a call, because that is what it stands in for.
SUMMARY_MAX = 1_200

#: Sources this may arrive from. `paste` is the one that needs nothing; the rest are named so a
#: stored row says where it came from without anyone parsing an id.
SOURCES = ("paste", "granola", "otter", "zoom", "fathom")


class TranscriptError(ValueError):
    """The transcript cannot be stored — empty, or too large to be a meeting."""


def transcript_id(source: str, external_id: str, *, body: str = "",
                  started_at: str = "") -> str:
    """A deterministic id, so re-importing the same meeting is an UPDATE and never a duplicate.

    Keyed on `(source, external_id)` when the source supplies one — that is what makes a polled
    API idempotent, and re-reading a rolling window is the normal way those work (§Lessons 65,
    where `deck_views` counted POLLS and read 99 from one click).

    A paste has no external id, so it falls back to the CONTENT plus the start time. Pasting the
    same note twice is then a no-op rather than two rows, which matters because re-pasting after
    an edit is the obvious way to correct a typo.
    """
    src = (source or "paste").strip().lower()
    ext = (external_id or "").strip()
    seed = f"{src}|{ext}" if ext else f"{src}|{started_at}|{hashlib.sha256(body.encode()).hexdigest()}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


def clean_body(text: str) -> str:
    """Trim and normalise, without touching the words.

    Deliberately NOT reformatted: every tool lays a transcript out differently and a parser that
    "tidies" one shape mangles the next. The body is evidence — it is stored the way it arrived.
    """
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    body = _WS.sub(" ", body)
    return _BLANKS.sub("\n\n", body)


def validate(body: str) -> str:
    """The cleaned body, or raise. Empty and oversized are different refusals."""
    out = clean_body(body)
    if not out:
        raise TranscriptError("nothing pasted")
    if len(out) > TRANSCRIPT_MAX:
        raise TranscriptError(
            f"that is {len(out):,} characters — the cap is {TRANSCRIPT_MAX:,}. "
            "Paste the part of the call that matters, or attach it in pieces.")
    return out


#: A line that is structure rather than speech: a timestamp, a speaker label, a source marker.
#: Used ONLY to skip such lines when falling back to an excerpt — never to attribute one.
_NOT_SPEECH = re.compile(
    r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?|speaker\s*\d+|microphone|system(?:\s*audio)?|"
    r"me|you|transcript|recording)\s*[:\-–]?\s*$", re.I)


def excerpt(body: str, limit: int = SUMMARY_MAX) -> str:
    """The opening of the meeting, for when no summary was supplied.

    A FALLBACK, and labelled as one by the caller. It is not a summary and must never be
    presented as one: the first 1,200 characters of a call are the greetings.

    Skips timestamp and speaker-label lines so the excerpt is words rather than scaffolding.
    """
    lines = [ln.strip() for ln in clean_body(body).split("\n")]
    keep = [ln for ln in lines if ln and not _NOT_SPEECH.match(ln)]
    out = " ".join(keep)[:limit].strip()
    # Stop on a word boundary. A summary that ends mid-word is indistinguishable from one that
    # was lost (§Lessons 90), and this one is shown beside real summaries.
    if len(out) == limit and " " in out:
        out = out[:out.rindex(" ")]
    return out


def clamp_summary(text: str) -> str:
    """A supplied summary, bounded. Whatever the source wrote is kept verbatim up to the cap."""
    out = clean_body(text)
    if len(out) <= SUMMARY_MAX:
        return out
    out = out[:SUMMARY_MAX]
    return out[:out.rindex(" ")] if " " in out else out


def prompt_block(rows: list[dict] | None, limit: int = 2) -> str:
    """What a drafter is told about meetings with this person, or "".

    **Summaries only, newest first, and at most two.** The body never appears here — see the
    module docstring.

    The instruction is the load-bearing part, and it is §Lessons 83 with higher stakes than the
    deck beacon: a sentence that only makes sense to somebody who was on the call tells them you
    are working from a recording of them. The deck version of this rule proved out — the live
    draft asked a substantive question and offered to "walk through any of it if something stood
    out", which is true whether or not they opened anything.
    """
    have = [r for r in (rows or []) if (r.get("summary") or "").strip()][:limit]
    if not have:
        return ""
    bits = []
    for r in have:
        when = (r.get("started_at") or "")[:10]
        title = (r.get("title") or "a call").strip()
        bits.append(f"- {when} — {title}: {(r.get('summary') or '').strip()}")
    return (
        "YOU HAVE ALREADY SPOKEN WITH THIS PERSON. Notes from that conversation:\n"
        + "\n".join(bits)
        + "\n\nUse this to know what has ALREADY been covered so you do not repeat it, and to "
        "pick up where it left off. These are FACTS, never phrasing to reuse.\n"
        "NEVER QUOTE THE CALL AND NEVER REFER TO A RECORDING, A TRANSCRIPT OR NOTES. Write as "
        "somebody who was present and remembers it. If a sentence would not make sense to "
        "someone who had NOT been on that call, do not write it.\n")
