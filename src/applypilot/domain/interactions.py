"""Everything a contact has actually DONE — one timeline, several sources.

The point of the tab is a question the dashboard could not answer: *has this person engaged at
all?* The pieces existed but each lived in its own place — sends on `contacts`, replies in
`messages`, deck clicks in three columns — so answering it meant opening four panels and
holding the result in your head.

**What is and is not detectable was established by looking, not by guessing:**

| Signal | Source | Why |
|---|---|---|
| Opened the intro deck | first-party beacon on our own site | a click, not an open — see `domain/deck.py` |
| Booked a call | cal.com emails us on every booking | verified: `hello@cal.com`, "30 Min Meeting between …" |
| Replied / we emailed | `contacts` + `messages` | already tracked |
| **Viewed your LinkedIn profile** | **operator-logged only** | not in the LinkedIn data export (checked: no such file in a Basic export) and LinkedIn sends no notification email for it (checked: zero such threads). The only source is LinkedIn's own UI, and driving that from outside the browser was abandoned twice here (§Lessons 3). Recording what the operator saw is honest; inventing a detector is not. |

Derived facts are computed here rather than copied into the table, because a column that
duplicates a fact drifts from it — the `emailed` bug (§Lessons 21) was exactly that. Only
events with no other home are stored: a booking we detected, and something the operator saw.
"""

from __future__ import annotations

from applypilot.domain.timeutil import parse_ts

#: Ordered by how much each one tells you. A booking is someone spending time on you; a reply
#: is someone spending words; a deck view is someone spending attention. An email WE sent is
#: not engagement at all — it is included because a timeline with only their actions cannot be
#: read (you cannot tell a fast reply from a slow one), but it never counts as a signal.
BOOKED, REPLIED, DECK, PROFILE_VIEW, CONNECTED, SENT, NOTE = (
    "booked", "replied", "deck", "profile_view", "connected", "sent", "note")
#: A LinkedIn message, either direction, typed in by the operator (UX-2). There is nowhere else
#: for these: `messages` is keyed on Gmail's own message id and carries `thread_id`,
#: `rfc_message_id` and `from_addr`, none of which a DM has — inventing them to fit would
#: corrupt the join reply detection runs on. And `contacts.dm_status` is 'sent'|'manual', both
#: meaning WE sent an invite, so nothing recorded what they sent back.
LINKEDIN_IN, LINKEDIN_OUT = "linkedin_in", "linkedin_out"

#: A calendar invitation WE sent. Our own action, so it sits with CONNECTED and SENT below the
#: engagement line: proposing a time is not somebody agreeing to one. Them accepting arrives, if
#: at all, as a detected cal.com BOOKED — which is why the two are separate kinds rather than one
#: "meeting" that would read as a success the moment we asked for it (§Lessons 35).
INVITED = "invited"

#: `LINKEDIN_IN` sits with REPLIED: someone writing to you on LinkedIn is the same act as
#: someone writing to you by email, and it is the strongest signal short of booking time.
WEIGHT = {BOOKED: 5, REPLIED: 4, LINKEDIN_IN: 4, PROFILE_VIEW: 3, DECK: 2,
          CONNECTED: 0, SENT: 0, LINKEDIN_OUT: 0, NOTE: 0, INVITED: 0}

LABEL = {
    BOOKED: "Booked a call",
    REPLIED: "Replied",
    LINKEDIN_IN: "Messaged you on LinkedIn",
    LINKEDIN_OUT: "You replied on LinkedIn",
    DECK: "Opened the intro deck",
    PROFILE_VIEW: "Viewed your LinkedIn profile",
    CONNECTED: "You sent a LinkedIn invite",
    SENT: "You emailed them",
    INVITED: "You sent a calendar invite",
    NOTE: "Note",
}

ICON = {BOOKED: "📅", REPLIED: "💬", LINKEDIN_IN: "🔗", LINKEDIN_OUT: "↪", DECK: "👁",
        PROFILE_VIEW: "🔗", CONNECTED: "🤝", SENT: "✉", NOTE: "📝", INVITED: "📨"}

#: Signals that mean the PERSON did something. Our own actions are context, not engagement.
#:
#: `CONNECTED` is on this side of the line and it is the easy mistake: `dm_status` is 'sent' or
#: 'manual', both of which mean WE sent an invite — there is no 'accepted' state anywhere in the
#: schema, so nothing here knows whether they ever responded to it. Counting it made every
#: contact engaged the moment an invite went out: three live jobs read "3/3 engaged", "5/5
#: engaged" before anyone had done a thing, which is a tab that answers its own question with
#: yes and is therefore worth nothing.
ENGAGEMENT = (BOOKED, REPLIED, LINKEDIN_IN, DECK, PROFILE_VIEW)

#: They wrote to us, on any channel. Separate from `replied_at`, which means specifically a
#: DETECTED email reply and is what `metrics.by_variant` divides by — mixing a typed-in number
#: into a measured one makes the copy experiment unreadable. The 🔔 counter joins the two;
#: the reply RATE does not.
INBOUND = (REPLIED, LINKEDIN_IN)


def has_inbound(rows: list[dict] | None) -> bool:
    """True if this person has written to us on any channel. Reads a rendered timeline, so it
    needs no new column and cannot drift from one."""
    return any(r.get("kind") in INBOUND for r in (rows or []))


def deck_opened_since_we_wrote(contact: dict, touches: list[dict] | None = None) -> bool:
    """They clicked the intro deck AFTER the last thing we sent them, and have not replied.

    The one moment a follow-up has something real to be about. Everything else the ladder does is
    chasing silence; this is the only case where the recipient has done something and the next
    message can respond to it.

    Anchored on the LAST message we sent rather than on "have they ever opened it", and the live
    data is why. Of the only two recorded opens at the time this was written:

        contact A   open stamped 19:28:19, our email sent 19:29:47   <- ninety seconds EARLIER
        contact B   emailed on the 6th,    opened on the 9th         <- genuine

    The first is the operator previewing their own `/intro/<name>` link before hitting send,
    which §Lessons 64 records as the recipient having read it. A rule reading "has ever opened"
    calls that engagement and writes a follow-up asking what they thought of a deck they were
    never sent. Comparing against our last send rejects it for free.

    A reply outranks this entirely: once someone writes back the sequence is terminal and the
    conversation, not the ladder, decides what to say next.
    """
    if (contact.get("replied_at") or "").strip():
        return False
    # `deck_last_at` (most recent open), not `deck_viewed_at` (the first). Someone who opened it
    # once a fortnight ago and again this morning has just done something.
    opened = parse_ts((contact.get("deck_last_at") or contact.get("deck_viewed_at") or "").strip())
    if not opened:
        return False
    sent = [parse_ts((contact.get("submitted_at") or "").strip())]
    sent += [parse_ts((t.get("sent_at") or "").strip()) for t in (touches or [])]
    last_sent = max([s for s in sent if s], default=None)
    return bool(last_sent) and opened > last_sent


def _row(kind: str, at: str, detail: str = "", source: str = "detected") -> dict:
    return {"kind": kind, "at": at or "", "detail": detail,
            "source": source, "label": LABEL.get(kind, kind), "icon": ICON.get(kind, "·")}


def for_contact(contact: dict, stored: list[dict] | None = None) -> list[dict]:
    """Every interaction with one person, newest first.

    `stored` is rows from the `interactions` table — the events with nowhere else to live.
    Everything else is derived from the contact itself, so it cannot fall out of step with the
    columns it describes.
    """
    c = contact or {}
    out: list[dict] = []

    if (c.get("submitted_at") or "").strip():
        out.append(_row(SENT, c["submitted_at"], "outreach email sent"))
    if (c.get("replied_at") or "").strip():
        out.append(_row(REPLIED, c["replied_at"]))
    if (c.get("dm_sent_at") or "").strip() and c.get("dm_status") in ("sent", "manual"):
        out.append(_row(CONNECTED, c["dm_sent_at"], "invite sent — acceptance is not tracked"))

    if (c.get("deck_viewed_at") or "").strip():
        views = c.get("deck_views") or 1
        # The FIRST view is the event; later ones are recurrence. Reporting only the latest
        # would silently move the date every time they looked again.
        detail = f"first of {views} views" if views > 1 else ""
        out.append(_row(DECK, c["deck_viewed_at"], detail))
        if views > 1 and (c.get("deck_last_at") or "").strip() != c.get("deck_viewed_at"):
            out.append(_row(DECK, c["deck_last_at"], f"most recent of {views} views"))

    for s in (stored or []):
        out.append(_row(s.get("kind") or NOTE, s.get("at") or "", s.get("detail") or "",
                        s.get("source") or "detected"))

    return sorted(out, key=lambda r: r["at"], reverse=True)


def summarise(rows: list[dict]) -> dict:
    """The one-line verdict for a contact: did they engage, and with the strongest what?"""
    engaged = [r for r in (rows or []) if r["kind"] in ENGAGEMENT]
    if not engaged:
        return {"engaged": False, "top": "", "label": "", "icon": "", "count": 0}
    best = max(engaged, key=lambda r: WEIGHT.get(r["kind"], 0))
    return {"engaged": True, "top": best["kind"], "label": best["label"],
            "icon": best["icon"], "count": len(engaged)}


def for_job(contacts: list[dict], stored_by_contact: dict | None = None) -> dict:
    """The whole job's interactions, plus who has engaged at all.

    `people` keeps contacts with NO engagement too, and says so — a tab that lists only the
    people who did something cannot answer "has anyone?", which is the question being asked.
    """
    stored_by_contact = stored_by_contact or {}
    people, total = [], 0
    for c in (contacts or []):
        rows = for_contact(c, stored_by_contact.get(c.get("id")))
        summary = summarise(rows)
        total += summary["count"]
        people.append({"id": c.get("id"), "full_name": c.get("full_name") or "",
                       "title": c.get("title") or "", "rows": rows, **summary})
    # Engaged first, then by how strong the signal was; the rest keep their order.
    people.sort(key=lambda p: (-int(p["engaged"]), -WEIGHT.get(p["top"], 0)))
    return {"people": people, "total": total,
            "engaged": sum(1 for p in people if p["engaged"])}
