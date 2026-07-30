"""Who is in a conversation, and who just joined it.

Pure functions over header dicts. The `domain/` boundary matters here for the usual reason —
this is testable without a mailbox — and for one more: getting "is this address us?" wrong is
how a system starts emailing itself.

The motivating case, from the first reply CRM-1 ever detected:

    1. me        ->  victoria.shearer@writer.com
    2. Victoria  ->  me       CC: David Loveless <david@writer.com>   <- a handoff
    3. me        ->  Victoria CC: David

Victoria answered by introducing a colleague. Recording that as `replied=True` loses the only
thing that actually happened: the application moved to someone else.
"""

from __future__ import annotations

import re

#: Addresses that are infrastructure, never a person worth adding to a CRM. Matched as a
#: prefix on the local part, so `noreply@x.com` and `no-reply+tag@x.com` both hit.
_ROBOT_PREFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
                   "postmaster", "notifications", "notification", "bounce", "bounces",
                   "auto-confirm", "jobs-noreply", "calendar-notification")

#: Substrings that mark an ATS or scheduling robot rather than a human at the company.
_ROBOT_DOMAINS = ("greenhouse.io", "ashbyhq.com", "lever.co", "myworkday", "icims.com",
                  "smartrecruiters.com", "calendly.com", "google.com/calendar")


def addr(value: str | None) -> str:
    """Bare lowercase address from a header value: 'Jo <JO@x.com>' -> 'jo@x.com'."""
    raw = (value or "").strip()
    m = re.search(r"<([^>]+)>", raw)
    if m:
        raw = m.group(1)
    return raw.strip().strip("<>").lower()


def display_name(value: str | None) -> str:
    """'David Loveless <david@writer.com>' -> 'David Loveless'. Falls back to the local part."""
    raw = (value or "").strip()
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', raw)
    if m and m.group(1).strip():
        return m.group(1).strip()
    local = addr(raw).split("@")[0]
    return re.sub(r"[._-]+", " ", local).title() if local else ""


def split_parts(value: str | None) -> list[str]:
    """Split a To/Cc header into RAW fragments, display names intact.

    Kept separate from `split_addrs` because throwing the name away here loses it for good:
    "David Loveless <david@writer.com>" degrades to "David", since the only fallback left is
    the local part.
    """
    if not value:
        return []
    # A regex lookahead cannot do this. `"Loveless, David" <david@writer.com>, jo@y.com` has a
    # comma INSIDE a quoted display name, and `,(?![^<]*>)` splits it into `"Loveless` and
    # `David" <david@writer.com>` — one recipient becomes two, one of them a garbage address.
    # Track quotes and angle brackets instead; a comma only separates when outside both.
    parts, buf, in_quotes, in_angle = [], [], False, False
    for ch in value:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "<" and not in_quotes:
            in_angle = True
        elif ch == ">" and not in_quotes:
            in_angle = False
        elif ch == "," and not in_quotes and not in_angle:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def split_addrs(value: str | None) -> list[str]:
    """Split a To/Cc header into bare addresses. Commas inside a quoted name are ignored."""
    return [a for a in (addr(p) for p in split_parts(value)) if a]


def is_robot(address: str) -> bool:
    """True for automated senders that must never become contacts.

    Threads collect schedulers, ATS notifications and `noreply@` addresses. An auto-added
    contact is one an automated follow-up ladder will then email — so this filter is what
    stands between "we noticed a new participant" and "we emailed a no-reply mailbox".
    """
    a = (address or "").lower()
    if not a or "@" not in a:
        return True
    local, _, domain = a.partition("@")
    if any(local.startswith(p) for p in _ROBOT_PREFIXES):
        return True
    return any(d in domain for d in _ROBOT_DOMAINS)


def participants(messages: list[dict], me: str) -> list[dict]:
    """Everyone on the thread except us, newest display name wins.

    Reads From, To AND Cc — an introduction usually arrives as a Cc, which is invisible if you
    only look at senders.
    """
    mine = addr(me)
    seen: dict[str, dict] = {}
    for msg in messages or []:
        pairs = [(msg.get("from"), "from")]
        for field in ("to", "cc"):
            for one in split_parts(msg.get(field)):
                pairs.append((one, field))
        for raw, field in pairs:
            a = addr(raw)
            if not a or a == mine:
                continue
            entry = seen.setdefault(a, {"email": a, "name": "", "first_seen": msg.get("id"),
                                        "via": field})
            name = display_name(raw)
            if name and len(name) > len(entry["name"]):
                entry["name"] = name
    return list(seen.values())


def introductions(messages: list[dict], me: str, known: list[str]) -> list[dict]:
    """People who appeared on the thread that we did not put there — a handoff.

    `known` is the address(es) we already track for this conversation. Anything else that a
    THEM message introduces is somebody the other side added: the recruiter looping in a hiring
    manager, which is the single most valuable event in a job-search conversation and the one
    thing a boolean `replied` throws away.

    Deliberately ignores participants added by OUR OWN messages — we already know about anyone
    we chose to email.
    """
    mine = addr(me)
    knowns = {addr(k) for k in (known or []) if k}
    out: dict[str, dict] = {}
    for msg in messages or []:
        sender = addr(msg.get("from"))
        if not sender or sender == mine:
            continue  # our own message: anyone on it, we added
        for field in ("to", "cc"):
            for one in split_parts(msg.get(field)):
                a = addr(one)
                if not a or a == mine or a == sender or a in knowns or a in out:
                    continue
                if is_robot(a):
                    continue
                out[a] = {"email": a, "name": display_name(one),
                          "introduced_by": sender,
                          "introduced_by_name": display_name(msg.get("from")),
                          "message_id": msg.get("id"),
                          "at": msg.get("internalDate") or ""}
    return list(out.values())


def timeline(messages: list[dict], me: str) -> list[dict]:
    """The conversation as the dashboard should show it, oldest first."""
    mine = addr(me)
    rows = []
    for msg in messages or []:
        sender = addr(msg.get("from"))
        rows.append({
            "id": msg.get("id"),
            "direction": "out" if sender == mine else "in",
            "from_addr": sender,
            "from_name": display_name(msg.get("from")),
            # RAW fragments, not bare addresses. Storing "david@writer.com" loses the display
            # name for good — the only fallback left is the local part, so "David Loveless"
            # degrades to "David" the moment it is written. Consumers call addr() to compare.
            "to_addrs": split_parts(msg.get("to")),
            "cc_addrs": split_parts(msg.get("cc")),
            "subject": msg.get("subject") or "",
            "at": msg.get("internalDate") or "",
        })
    # internalDate is ms-since-epoch as a STRING; compare numerically or "9999" sorts after
    # "10000" (the same trap as domain.replies._ts).
    return sorted(rows, key=lambda r: int(r["at"]) if str(r["at"]).isdigit() else 0)


def pending_introductions(threads: dict, contact_emails: list[str], me: str) -> list[dict]:
    """People introduced on stored threads who are NOT yet contacts on this job.

    Works from the `messages` table rather than a live fetch, so the dashboard can show a
    pending handoff on every refresh without touching Gmail.

    `threads` is contact_id -> stored message rows (already normalised: `from_addr`, `cc_addrs`
    as lists), which is what `messages.threads_for_job()` returns.
    """
    known = {addr(e) for e in (contact_emails or []) if e}
    mine = addr(me)
    out: dict[str, dict] = {}
    for contact_id, msgs in (threads or {}).items():
        for msg in msgs or []:
            if msg.get("direction") != "in":
                continue  # only the other side can introduce someone
            sender = addr(msg.get("from_addr"))
            for one in (msg.get("cc_addrs") or []) + (msg.get("to_addrs") or []):
                a = addr(one)
                if not a or a == mine or a == sender or a in known or a in out:
                    continue
                if is_robot(a):
                    continue
                out[a] = {"email": a,
                          "name": display_name(one),
                          "introduced_by": msg.get("from_name") or sender,
                          "from_contact_id": contact_id,
                          "at": msg.get("sent_at") or ""}
    return list(out.values())
