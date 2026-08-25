"""Moving a CARD from one Space to another. Pure rules, no sqlite and no http.

Asked for as *"I just added a card to my professional space for Ian, but it should live on my
business network because I'm actually trying to do business with him"* — with the reasonable
worry that Spaces have different templates and therefore different schemas.

**They do not, and that is what makes this cheap.** A Space is a manifest, not a fork
(SPACE-1a): one `jobs` table, one `contacts` table, and only those two carry `space_id` at all.
Touches, messages, sequences, interactions and transcripts have no notion of a Space — they hang
off the contact and the anchor, neither of which moves.

**The anchor is deliberately NOT rewritten.** A target card's key is `target:<space>:<slug>` and
`store.contact_id` hashes it, so re-keying would orphan every contact, ladder, message and
transcript on the card — the exact failure `repo.attach_posting` exists to avoid. After creation
the Space inside that string is decorative: `target.parse_anchor` is called by one function in
the whole source tree (`is_target`), which asks only whether a row is a target and discards the
Space. Membership is the `space_id` COLUMN (SPACE-1a D2). So the card keeps an anchor naming the
Space it came from, which is the same trade already accepted for `job_url` vs `anchor`.
"""
from __future__ import annotations

#: Fields that decide HOW an email reads. If any differ, an unsent draft on this card was
#: written for the Space it is leaving, and carrying it across means the operator is one click
#: from sending copy that argues the old campaign's case. CO-2 learned this the expensive way:
#: sixteen unsent drafts naming a dead role, each one click from Send.
VOICE_FIELDS = ("voice", "tone", "offer", "must_mention")

#: Fields worth telling the operator about, but which change nothing already sent.
COMPARED = ("voice", "tone", "offer", "must_mention", "terminal", "schedules", "channels",
            "tailor_docs", "offer_deck", "can_autosend", "identity_id")


def refusal(src, dst, *, has_sent: bool = False) -> str:
    """Why this move may not happen, or "" if it may.

    Refusing is preferred to a half-move that renders wrong. The same discipline CO-2 uses when
    it refuses a cross-Space CONTACT move: an operation that cannot be made correct is better
    named than approximated.
    """
    if src is None or dst is None:
        return "that Space does not exist"
    if src.id == dst.id:
        return f"this card is already in {dst.name}"
    if src.shape != dst.shape:
        # The genuinely hard case, and it is structural rather than schema. `jobs_shaped_ids`
        # gates the pipeline queues and `queue_for_apply` intersects with it, so a posting moved
        # into a targets Space silently leaves the apply queue; and `is_target` decides how a row
        # RENDERS from the anchor's kind, which a move cannot change. Deliberately out of scope
        # rather than approximated.
        return (f"{src.name} holds {_shape_word(src.shape)} and {dst.name} holds "
                f"{_shape_word(dst.shape)}. Moving between the two is not supported yet.")
    if has_sent and src.identity_id != dst.identity_id:
        # The documented freeze: any sent message freezes `identity_id`, because the recipient
        # has already met one sender. Every Space is `personal` today so this cannot fire, and
        # it is written now because retrofitting it after ID-1 ships is the expensive order.
        return (f"outreach has already gone out from this card as {src.identity_id}, and "
                f"{dst.name} sends as {dst.identity_id}")
    return ""


def _shape_word(shape: str) -> str:
    return "job postings" if (shape or "").endswith("jobs") else "company cards"


def differences(src, dst) -> list[dict]:
    """What actually changes for this card, as `{field, from, to}`, most consequential first.

    A move that silently alters the ladder or the voice is the shape of bug this codebase keeps
    paying for, so the plan states it and the operator decides.
    """
    out = []
    for f in COMPARED:
        a, b = getattr(src, f, None), getattr(dst, f, None)
        if a != b:
            out.append({"field": f, "from": _show(a), "to": _show(b)})
    return out


def _show(v) -> str:
    if isinstance(v, (tuple, list)):
        return ", ".join(str(x) for x in v) or "none"
    if isinstance(v, dict):
        return ", ".join(f"{k}: {v[k]}" for k in sorted(v)) or "none"
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v or "none")


def drafts_are_stale(src, dst) -> bool:
    """Would an UNSENT draft on this card argue the wrong campaign after the move?

    Only the voice fields count. A different `terminal` or a longer ladder changes what happens
    NEXT; it does not make a sentence already written wrong.
    """
    return any(getattr(src, f, None) != getattr(dst, f, None) for f in VOICE_FIELDS)


def ladder_warning(src, dst) -> str:
    """A ladder that gets SHORTER can finish a sequence on arrival.

    `ladder_states` counts sent touches against `len(schedule)`, so moving a contact three
    touches deep into a Space with a two-touch ladder reads `finished` immediately and reopening
    does not help (§Lessons 107, which is the assumption CO-2's own ticket got wrong).
    """
    for ch, before in (getattr(src, "schedules", {}) or {}).items():
        after = (getattr(dst, "schedules", {}) or {}).get(ch)
        if after is not None and len(after) < len(before):
            return (f"the {ch} ladder is shorter in {dst.name} "
                    f"({len(before)} touches to {len(after)}), so a sequence already past "
                    f"{len(after)} will read as finished")
    return ""
