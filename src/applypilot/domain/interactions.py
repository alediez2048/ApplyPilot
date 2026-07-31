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

#: Ordered by how much each one tells you. A booking is someone spending time on you; a reply
#: is someone spending words; a deck view is someone spending attention. An email WE sent is
#: not engagement at all — it is included because a timeline with only their actions cannot be
#: read (you cannot tell a fast reply from a slow one), but it never counts as a signal.
BOOKED, REPLIED, DECK, PROFILE_VIEW, CONNECTED, SENT, NOTE = (
    "booked", "replied", "deck", "profile_view", "connected", "sent", "note")

WEIGHT = {BOOKED: 5, REPLIED: 4, PROFILE_VIEW: 3, DECK: 2, CONNECTED: 0, SENT: 0, NOTE: 0}

LABEL = {
    BOOKED: "Booked a call",
    REPLIED: "Replied",
    DECK: "Opened the intro deck",
    PROFILE_VIEW: "Viewed your LinkedIn profile",
    CONNECTED: "You sent a LinkedIn invite",
    SENT: "You emailed them",
    NOTE: "Note",
}

ICON = {BOOKED: "📅", REPLIED: "💬", DECK: "👁", PROFILE_VIEW: "🔗", CONNECTED: "🤝",
        SENT: "✉", NOTE: "📝"}

#: Signals that mean the PERSON did something. Our own actions are context, not engagement.
#:
#: `CONNECTED` is on this side of the line and it is the easy mistake: `dm_status` is 'sent' or
#: 'manual', both of which mean WE sent an invite — there is no 'accepted' state anywhere in the
#: schema, so nothing here knows whether they ever responded to it. Counting it made every
#: contact engaged the moment an invite went out: three live jobs read "3/3 engaged", "5/5
#: engaged" before anyone had done a thing, which is a tab that answers its own question with
#: yes and is therefore worth nothing.
ENGAGEMENT = (BOOKED, REPLIED, DECK, PROFILE_VIEW)


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
