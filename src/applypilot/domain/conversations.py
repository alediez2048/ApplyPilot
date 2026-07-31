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
            # Carried through so a reply can chain References across the whole thread.
            "rfc_message_id": msg.get("rfc_message_id") or "",
        })
    # internalDate is ms-since-epoch as a STRING; compare numerically or "9999" sorts after
    # "10000" (the same trap as domain.replies._ts).
    return sorted(rows, key=lambda r: int(r["at"]) if str(r["at"]).isdigit() else 0)


#: Where a reply stops being theirs and becomes a quote of ours. Gmail's snippet runs straight
#: through the quote header, so ~a third of a short reply can be the beginning of our OWN email
#: quoted back — which then reaches the drafter as something they said.
_QUOTE_MARKERS = (
    r"\nOn .{0,80}? wrote:", r"\bOn \w{3}, \w{3} \d{1,2}, \d{4} at \d{1,2}:\d{2}",
    r"\n-{2,}\s*Original Message", r"\n_{5,}", r"\nFrom:\s", r"\nSent from my ",
)


def strip_quoted_tail(text: str | None) -> str:
    """Drop the quoted-original tail from a reply snippet.

    Conservative: it only cuts at a recognised quote header, and only when something is left
    before it. Cutting too eagerly would discard the actual reply, which is far worse than
    leaving a few words of quote in.
    """
    body = (text or "").strip()
    if not body:
        return ""
    cut = len(body)
    for pattern in _QUOTE_MARKERS:
        m = re.search(pattern, body, re.IGNORECASE)
        if m and m.start() > 0:
            cut = min(cut, m.start())
    trimmed = body[:cut].strip()
    return trimmed or body


def _strip_re(subject: str) -> str:
    """'Re: RE: Fwd: hi' -> 'hi'. Repeated prefixes accumulate on a long thread."""
    s = (subject or "").strip()
    while True:
        m = re.match(r"^\s*(re|fwd|fw)\s*(\[\d+\])?\s*:\s*", s, re.IGNORECASE)
        if not m:
            return s.strip()
        s = s[m.end():]


def reply_target(messages: list[dict], me: str | list[str]) -> dict | None:
    """Who a reply should go to, and who must stay Cc'd.

    **This is the whole point of CRM-4a.** Victoria answered by Cc'ing David — so a reply that
    goes only to Victoria drops the person now actually handling the application, silently, and
    looks perfectly normal on screen. The Cc list is carried forward rather than rebuilt.

    Answers the LAST INBOUND message, not the last message overall: if we already replied and
    are replying again, the addresses still belong to what THEY sent — ours are a copy of a copy.

    Returns None when nobody has written to us. That is deliberate rather than a fallback to the
    contact's address: a thread with no inbound message is a follow-up, which has its own ladder,
    its own per-touch prompts and its own stop conditions. Silently turning "reply" into
    "follow-up #4" would bypass all three.

    `me` accepts several addresses because it genuinely can be several: sending may authenticate
    as one account and set From to a verified alias (`OUTREACH_FROM_ADDRESS`). Both are us, and
    an address of ours left in the Cc means every reply copies us on our own mail.

    `messages` are STORED rows (`from_addr`, `to_addrs`/`cc_addrs` as raw fragment lists), i.e.
    what `messages.thread_for_contact()` returns.
    """
    mine = {addr(x) for x in ([me] if isinstance(me, str) else (me or [])) if addr(x)}
    # Robots are not correspondents. A bounce is an inbound message in our own thread, and
    # without this the newest "reply" is MAILER-DAEMON and the composer politely offers to
    # answer it. Offering to reply to a bounce notification is the point at which a CRM stops
    # being trustworthy about the one thing it claims to know.
    inbound = [m for m in (messages or [])
               if isinstance(m, dict) and (m.get("direction") or "") == "in"
               and not is_robot(addr(m.get("from_addr")))]
    if not inbound:
        return None
    last = inbound[-1]

    to_raw = _pick_from(last)
    to_addr = addr(to_raw)
    if not to_addr:
        return None

    # Everyone else who was on that message stays on the reply — minus us and minus the person
    # we are addressing, or they receive it twice.
    seen, cc = mine | {to_addr}, []
    for one in (last.get("to_addrs") or []) + (last.get("cc_addrs") or []):
        a = addr(one)
        if not a or a in seen or is_robot(a):
            continue
        seen.add(a)
        cc.append(one.strip())

    # References chains the WHOLE thread, not just the message being answered — that is what
    # keeps a mail client from splitting the conversation in two.
    refs = [m["rfc_message_id"] for m in (messages or []) if (m.get("rfc_message_id") or "").strip()]
    subject = _strip_re(last.get("subject") or "")
    return {
        "to": to_raw.strip(),
        "to_addr": to_addr,
        "cc": cc,
        "subject": f"Re: {subject}" if subject else "",
        "in_reply_to": (last.get("rfc_message_id") or "").strip(),
        "references": " ".join(refs),
        "thread_id": (last.get("thread_id") or "").strip(),
        "answering": last.get("from_name") or to_addr,
        "at": last.get("sent_at") or "",
    }


#: Whose turn it is. `awaiting_us` is the one that matters: somebody wrote to you and the
#: conversation has been sitting there ever since, which is the single worst outcome this
#: system can produce — it spent Apollo credits and an email to earn a reply, then dropped it.
AWAITING_US, AWAITING_THEM = "awaiting_us", "awaiting_them"


def _from_robot(msg: dict) -> bool:
    """True for an inbound message from a machine — a bounce daemon, an ATS, a scheduler.

    A bounce arrives INSIDE our own thread, so nothing about thread membership excludes it. Left
    in, it becomes the newest inbound message and the whole product agrees that MAILER-DAEMON
    replied and is owed an answer: the row says "your turn", the banner names the daemon, and
    the composer aims a reply at it. `is_robot()` was already applied to the Cc list and never
    to the sender.

    Outbound is never filtered — we are not a robot, and matching our own address against these
    prefixes would be a coincidence away from hiding our own messages.
    """
    if (msg.get("direction") or "") != "in":
        return False
    return is_robot(addr(msg.get("from_addr")))


def conversation_state(messages: list[dict], now=None) -> dict | None:
    """Whose turn it is in this conversation, and how long it has been that way.

    Returns None when nobody has written to us — a thread of our own messages is outreach with
    a follow-up ladder, not a conversation, and describing it as "awaiting them" would put every
    unanswered cold email into the same bucket as a real live thread.

    Structural only, and honestly so: on `gmail.metadata` we know a message arrived and who it
    was from, never what it said. "They replied and you have not answered" needs no body to be
    true, which is exactly why it is worth saying.
    """
    from datetime import datetime, timezone

    from applypilot.domain.timeutil import hours_since

    msgs = [m for m in (messages or []) if isinstance(m, dict) and not _from_robot(m)]
    if not any((m.get("direction") or "") == "in" for m in msgs):
        return None
    last = msgs[-1]                     # stored oldest-first (`ORDER BY sent_at`)
    inbound = (last.get("direction") or "") == "in"
    hours = hours_since((last.get("sent_at") or "").strip(), now or datetime.now(timezone.utc))
    return {
        "state": AWAITING_US if inbound else AWAITING_THEM,
        "hours": None if hours is None else round(hours),
        "days": None if hours is None else int(hours // 24),
        "at": last.get("sent_at") or "",
        "who": (last.get("from_name") or last.get("from_addr") or "") if inbound else "",
        "messages": len(msgs),
    }


def _pick_from(msg: dict) -> str:
    """The raw From fragment, display name intact where we have one."""
    name = (msg.get("from_name") or "").strip()
    address = (msg.get("from_addr") or "").strip()
    if name and address:
        return f"{name} <{address}>"
    return address or name


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
