"""What a reply is asking for — CRM-4b.

Pure, rule-based, and deliberately quick to say **unknown**. This exists to change which action
the dashboard offers: a rejection should surface *Mark rejected*, not *draft a follow-up*, and
a question should surface *answer it* rather than a nudge three days later.

Two design choices worth defending:

**Rules, not an LLM.** The input is a ~200-character Gmail snippet, the output picks a button,
and this runs inside a loop that may execute hourly forever. A wrong label here is worse than no
label — "interested" on a rejection would have the operator write an eager reply to someone who
already said no — so the cost of a confident mistake far exceeds the value of a confident guess.

**A snippet is the OPENING of a message, not a summary.** Gmail gives us the first ~200 chars,
which on a real reply is usually the greeting and the first sentence. That is enough for "Thanks
for reaching out, unfortunately..." and nowhere near enough for a nuanced answer, so anything
not clearly signposted stays `unknown` and the UI just shows the text.
"""

from __future__ import annotations

import re

INTRODUCTION = "introduction"
REJECTION = "rejection"
NOT_NOW = "not_now"
INTERESTED = "interested"
QUESTION = "question"
OUT_OF_OFFICE = "out_of_office"
UNKNOWN = "unknown"

#: Ordered most-specific first. The first pattern that matches wins, so a message that both
#: rejects and asks a question reads as a rejection — the stronger fact about what to do next.
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Auto-replies first: they can contain almost any other phrase and mean none of it.
    (OUT_OF_OFFICE, (
        r"\bout of (the )?office\b", r"\bon (annual |parental |maternity |paternity )?leave\b",
        r"\bauto(matic)?[- ]repl(y|ies)\b", r"\blimited access to (my )?email\b",
        r"\bi am currently away\b", r"\bback in the office\b",
    )),
    (INTRODUCTION, (
        r"\b(loop(ing|ed)?|cc|copy(ing)?|connect(ing)?|introduc(e|ing))\b[^.]{0,40}\b(in|you|here)\b",
        r"\bwill be (your|the) (main )?(point of )?contact\b",
        r"\b(handing|passing) (you |this )?(over|off) to\b",
        r"\bbest person (to|for)\b", r"\bmy colleague\b", r"\bover to\b",
    )),
    (REJECTION, (
        r"\bunfortunately\b", r"\bnot (moving|going) forward\b", r"\bwe('| ha)?ve decided\b",
        r"\bdecided to (move|go|proceed) (forward |ahead )?with (another|other|a different)\b",
        r"\bno longer (open|available|hiring|accepting)\b", r"\b(position|role|req) (has been )?(closed|filled)\b",
        r"\bnot a (good )?(fit|match)\b", r"\bwe are not\b", r"\bpursu(e|ing) other candidates\b",
    )),
    (NOT_NOW, (
        r"\b(circle|touch|check) back\b", r"\bin a (few|couple)? ?(weeks|months|quarters)\b",
        r"\bnot (hiring|recruiting) (right now|at the moment|currently)\b",
        r"\bkeep (you|your) (resume|cv|details|profile) on file\b",
        r"\brevisit\b", r"\bnext (quarter|year|cycle)\b", r"\bon hold\b", r"\bpaused?\b",
    )),
    (INTERESTED, (
        r"\b(happy|glad|would love|keen) to (chat|talk|connect|meet|speak)\b",
        r"\bset (up|something up)\b", r"\bschedul(e|ing)\b", r"\bcalendar\b", r"\bcalendly\b",
        r"\b(are you )?(free|available)\b", r"\bbook (a|some) time\b",
        r"\bnext steps?\b", r"\binterview\b", r"\blet'?s (chat|talk|connect|set)\b",
        r"\bsounds (great|good|interesting)\b",
    )),
)

#: A question mark alone is not a question — "Interested?" in a signature, or a rhetorical
#: "Sound good?" at the end of an acceptance. Require an interrogative opener near it.
_ASKS = re.compile(
    r"\b(what|when|where|which|who|how|why|are you|do you|would you|could you|can you|"
    r"have you|any chance|is there)\b[^?]{0,120}\?", re.IGNORECASE)


def classify(text: str | None) -> str:
    """Best-effort intent of an inbound message. `unknown` whenever it is not clear-cut.

    Returns one of the module constants. An empty or missing snippet — every metadata-only
    install — is always `unknown`, which is what keeps this from silently changing behaviour
    for anyone who never granted the content scope.
    """
    body = (text or "").strip()
    if not body:
        return UNKNOWN
    low = body.lower()
    for label, patterns in _PATTERNS:
        if any(re.search(p, low) for p in patterns):
            return label
    return QUESTION if _ASKS.search(body) else UNKNOWN


#: What to DO about each intent. The point of classifying at all — a label that does not change
#: the offered action is decoration.
_ACTIONS: dict[str, tuple[str, str]] = {
    INTRODUCTION: ("Someone new was handed this", "Add them as a contact and email them."),
    REJECTION: ("They said no", "Mark the job rejected — a follow-up here reads badly."),
    NOT_NOW: ("Not right now", "Stop the ladder and note when to revisit."),
    INTERESTED: ("They want to talk", "Reply with times — this is the one that matters."),
    QUESTION: ("They asked you something", "Answer it before anything else."),
    OUT_OF_OFFICE: ("Auto-reply", "Nobody has actually read it yet — leave the ladder running."),
    UNKNOWN: ("", ""),
}


def suggestion(intent: str) -> dict:
    """(label, action) for an intent, both empty for `unknown`."""
    label, action = _ACTIONS.get(intent, ("", ""))
    return {"intent": intent, "label": label, "action": action}
