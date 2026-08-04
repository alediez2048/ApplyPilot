"""Wording that has already gone to this employer, so the next message does not repeat it.

Measured on the live corpus before this existed — 189 drafts, and the repetition was not spread
evenly, it was concentrated in exactly the places a reader looks first:

    subject lines   Google: 16 people, 4 distinct subjects, ten of them identical
                    Saronic: 12 people, 4 subjects. Yahoo: 12 people, 4 subjects.
    the CTA         one sentence, 48 times across the corpus, 9 of 16 at Google
    the sign-off    "Looking forward to connecting!" 16 times
    openings        Salesforce: 6 of 14 people got the same first sentence

Email BODIES were otherwise fine (Google: 16 people, 16 distinct openings), which is the shape
of the whole problem. The model varies freely where it is writing, and converges wherever the
prompt handed it a form of words — §Lessons 9 and 42, for the fourth and fifth time. Two worked
examples were doing it: `(e.g. "if you're open to a quick call, grab a time that works here:
<link>")` and `Subject: … (e.g. "quick q about the <role> role")`.

Deleting those examples is necessary and not sufficient. A model given no example still lands on
the same phrasing repeatedly, because the same prompt and the same job produce the same most
likely sentence. The only thing that reliably breaks the tie is telling it what has ALREADY been
said to these people — which the codebase already does for the intro deck, and which is the same
move `skip_known` makes for a second round of contacts.

Why the sender never notices: they see one message at a time. The recipients are the ones who sit
near each other.
"""

from __future__ import annotations

import re

#: Enough to recognise, short enough that the block stays readable. A whole body would flood the
#: prompt and bury the instruction, which is how a rule ends up losing to the text around it
#: (§Lessons 40).
_OPEN_CHARS = 160
_CTA_CHARS = 140
#: Newest first, and bounded: twelve is more than any real company on this board has received,
#: and an unbounded block would put a full inbox in front of a 400-token generation.
_MAX = 12


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in re.split(r"\n+", (text or "").strip()) if ln.strip()]


def opening(body: str) -> str:
    """The first sentence that is not a bare greeting.

    "Hi Sarah," carries nothing and would make every message look identical; the sentence AFTER
    it is the one a reader compares.
    """
    for line in _lines(body):
        stripped = re.sub(r"^(hi|hey|hello|dear)\b[^,]{0,40},?\s*", "", line, flags=re.I).strip()
        if len(stripped) > 20:
            return stripped[:_OPEN_CHARS]
    return ""


def cta(body: str) -> list[str]:
    """EVERY sentence carrying a link, which is where the repetition concentrated.

    All of them, not the first. An outreach email carries TWO links — the scheduling link and
    the intro deck — and returning only the first meant the deck sentence masked the booking
    sentence, so the 48-times-repeated CTA was never burned at all. Found by generating against
    real data rather than by reading this function (§Lessons 42, and the reason the first live
    run of this fix still showed 6 of 8 sharing a deck line).

    Returned with the URL stripped: the link is identical by design and must appear verbatim in
    every message, so leaving it in would ask the model to vary the one part it cannot.
    """
    out = []
    # Lines FIRST, then sentences. A line ending in a URL has no terminal punctuation, so a
    # sentence split alone glues the deck line onto the booking line and yields one blob that
    # matches neither next time. The two links routinely sit in separate paragraphs.
    for line in _lines(body):
        for sentence in re.split(r"(?<=[.!?])\s+", re.sub(r"[ \t]+", " ", line)):
            if "http" not in sentence:
                continue
            bare = re.sub(r"https?://\S+", "", sentence).strip(" .,:;-")
            if len(bare) > 12 and bare not in out:
                out.append(bare[:_CTA_CHARS])
    return out


def burned_block(previous: list[dict]) -> str:
    """The prompt section listing what this employer has already been sent. '' when there is none.

    Three lists rather than one blob, because the model has to be told which SLOT is burned. A
    used subject line does not stop a body from opening the same way, and the live corpus had
    both failing independently.
    """
    subjects, openings, ctas = [], [], []
    for item in (previous or [])[:_MAX]:
        subject = (item.get("subject") or "").strip()
        if subject and subject not in subjects:
            subjects.append(subject)
        body = item.get("body") or ""
        first = opening(body)
        if first and first not in openings:
            openings.append(first)
        for call in cta(body):
            if call not in ctas:
                ctas.append(call)

    if not (subjects or openings or ctas):
        return ""

    out = [
        "ALREADY USED AT THIS COMPANY, DO NOT REPEAT OR PARAPHRASE.",
        "Other people at this same employer received the wording below. They sit near each",
        "other and may compare messages. Anything recognisably similar reads as a mail merge.",
        "Do not swap a synonym into the same sentence, use a different sentence.",
    ]
    if subjects:
        out.append("\nSubject lines already used (yours must be none of these):")
        out += [f"  - {s}" for s in subjects]
    if openings:
        out.append("\nOpening sentences already used (open a different way):")
        out += [f"  - {s}" for s in openings]
    if ctas:
        out.append("\nLink sentences already used (the LINKS stay, the sentences change):")
        out += [f"  - {s}" for s in ctas]
    return "\n".join(out) + "\n\n"
