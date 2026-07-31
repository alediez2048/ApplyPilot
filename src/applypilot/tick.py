"""`applypilot tick` — the unattended heartbeat.

Nothing in this product runs unless the dashboard is open, which caps the leverage permanently:
the system can never surface something you did not think to ask for. `tick` is one idempotent
command, safe to run repeatedly, that `launchd` can call on a timer.

Three hard rules, and they are the reason this is a small file rather than a scheduler:

  * **It never sends anything.** Every safeguard in `gmail_send.py` assumes a human initiated
    the action, and there is still no per-company cap — 5 contacts × 3 touches is 15 emails at
    one employer. `tick` drafts and queues; sending stays a click.
  * **It never starts an apply.** Co-pilot ends by handing a browser to a human, so an
    unattended apply would fill a form nobody is there to review — and launching one closes
    whatever review browser is already open (§Lessons 8).
  * **It never touches `apply.pause`.** That flag is consumed by a running agent and cleared at
    start-up by `main()`. Writing it would pause a live application; clearing it would un-pause
    one the operator paused deliberately.

Every step is isolated: a failure in one is recorded and the rest still run. A heartbeat that
aborts halfway is worse than none, because the parts that did work look like the parts that
did not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step_release_locks(conn, dry_run: bool) -> dict:
    """Free `in_progress` locks no living worker can still hold.

    An interrupted apply — a dashboard restart, a crash — leaves the row locked forever and
    `acquire_job` then silently skips that job, so a retry does nothing and says nothing.
    This is a mitigation of open debt (applies run inside the HTTP request thread), not a fix.
    """
    from applypilot.apply.launcher import STALE_LOCK_MINUTES, release_stale_locks
    if dry_run:
        from applypilot.repo import jobs as _jobs
        stuck = _jobs.in_progress(conn)
        return {"would_release": len(stuck),
                "detail": f"{len(stuck)} lock(s) older than {STALE_LOCK_MINUTES}m would be checked"}
    freed = release_stale_locks(conn=conn)
    return {"released": len(freed), "detail": f"released {len(freed)} stale lock(s)"}


def _step_poll_replies(conn, dry_run: bool) -> dict:
    """Ask Gmail whether anyone answered. The whole reason a heartbeat is worth having."""
    from applypilot.networking import gmail_read
    ok, why = gmail_read.available()
    if not ok:
        return {"skipped": True, "detail": why}
    if dry_run:
        from applypilot.networking import store
        n = len(store.contacts_awaiting_reply(conn))
        return {"would_check": n, "detail": f"{n} thread(s) would be checked"}
    from applypilot.networking import replies
    res = replies.poll(conn)
    return {"replied": res.get("replied", 0), "bounced": res.get("bounced", 0),
            "checked": res.get("checked", 0),
            "detail": f"checked {res.get('checked', 0)}, "
                      f"{res.get('replied', 0)} replied, {res.get('bounced', 0)} bounced"}


def _due_followups(conn) -> list[dict]:
    """Contacts whose next follow-up is due and has no draft waiting.

    Delegates to `domain.followup_panel` — the SAME computation the dashboard renders, so
    `tick` can never disagree with what the operator sees on screen. Reimplementing the rule
    here is how the two would drift.

    Skipping ladders that already hold a draft is what makes `tick` idempotent: running it
    hourly must not regenerate (and pay for) the same message over and over.
    """
    from applypilot.domain import followup_panel
    from applypilot.domain.followup import CHANNELS
    from applypilot.networking import store, touches

    contacts = [store.get_contact(c["id"], conn)
                for c in store.all_contacts_for_metrics(conn)]
    contacts = [c for c in contacts if c]
    if not contacts:
        return []
    ladders = touches.ladder_states([c["id"] for c in contacts], conn)
    panel = followup_panel(contacts, ladders=ladders)

    # The panel is FLAT and prefixed per channel — `due` for email, `li_due` for LinkedIn —
    # not nested under a "channels" key. Reading a key that does not exist would make this
    # silently find nothing and look like "no follow-ups are ever due".
    by_id = {c["id"]: c for c in contacts}
    out = []
    for channel in CHANNELS:
        for row in panel.get(f"{channel.prefix}due", []) or []:
            cid = row.get("id")
            ladder = ladders.get((cid, channel.name)) or {}
            # `draft_body`, NOT `body` — that is the key ladder_states actually returns. Reading
            # the wrong one made this check silently never fire, so an hourly tick would
            # regenerate (and pay for) the same follow-up forever, churning text the operator
            # may already have edited.
            if (ladder.get("draft_body") or "").strip():
                continue  # already queued for review — nothing to do
            contact = by_id.get(cid)
            if contact:
                out.append({"contact": contact, "channel": channel.name,
                            "touch": (ladder.get("count") or 0) + 1})
    return out


def _step_draft_followups(conn, dry_run: bool, limit: int = 10) -> dict:
    """Draft what came due, and QUEUE it. Never send.

    Bounded per run so one tick cannot spend an unbounded amount on LLM calls if a large
    batch comes due at once.
    """
    try:
        due = _due_followups(conn)
    except Exception as e:  # noqa: BLE001
        log.debug("Could not compute due follow-ups", exc_info=True)
        return {"error": str(e)[:120], "detail": f"could not compute due follow-ups: {e}"}

    if dry_run:
        return {"would_draft": len(due),
                "detail": f"{len(due)} follow-up(s) would be drafted (never sent)"}
    if not due:
        return {"drafted": 0, "detail": "nothing due"}

    from applypilot.config import load_profile
    from applypilot.networking import outreach, touches
    from applypilot.repo import jobs as _jobs
    try:
        profile = load_profile()
    except Exception:  # noqa: BLE001
        profile = {}

    drafted, failed = 0, 0
    for item in due[:limit]:
        c = item["contact"]
        job = _jobs.get(c["job_url"], conn) or {"url": c["job_url"]}
        try:
            d = outreach.draft_for_channel(item["channel"], profile, job, c, touch=item["touch"])
            touches.set_draft(c["id"], item["channel"], d.get("subject", ""), d["body"], conn)
            drafted += 1
        except Exception:  # noqa: BLE001
            # One bad draft must not stop the rest — and it must not be retried forever
            # either, which is why the failure is counted rather than raised.
            log.debug("Follow-up draft failed for %s", c.get("full_name"), exc_info=True)
            failed += 1
    held = max(0, len(due) - limit)
    return {"drafted": drafted, "failed": failed, "held_back": held,
            "detail": f"drafted {drafted} follow-up(s), queued for your review"
                      + (f", {failed} failed" if failed else "")
                      + (f", {held} left for the next tick" if held else "")}


def _step_unanswered(conn, dry_run: bool) -> dict:
    """Report conversations where THEY wrote last and nobody has answered.

    Reports only — drafting a reply would need to know what they said, and on `gmail.metadata`
    we cannot read a body (that trade is CRM-4b). Saying "Gina replied 2 days ago and you have
    not answered" needs no body to be true, and it is the highest-value sentence this command
    can produce: every other step chases people who said nothing.
    """
    from applypilot.domain import conversations as cv
    from applypilot.networking import messages as msg_store, store

    try:
        contacts = store.all_contacts_for_metrics(conn)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120], "detail": f"could not list contacts: {e}"}

    threads = msg_store.threads_by_contact(conn)   # one query, not one per contact
    waiting = []
    for c in contacts:
        cid = c.get("id")
        if not cid:
            continue
        state = cv.conversation_state(threads.get(cid) or [])
        if state and state["state"] == cv.AWAITING_US:
            # The name off the inbound HEADER, not off the contact row. `all_contacts_for_metrics`
            # is a deliberately narrow projection (no name), and the header is the better source
            # anyway — a contact stored as "David" is "David Loveless" in the message he sent.
            waiting.append({"name": state.get("who") or c.get("company") or cid,
                            "company": c.get("company") or "",
                            "days": state.get("days")})
    waiting.sort(key=lambda w: -(w["days"] or 0))
    if not waiting:
        return {"awaiting_us": 0, "detail": "no unanswered replies"}
    names = ", ".join(f"{w['name']} ({w['days']}d)" for w in waiting[:5])
    return {"awaiting_us": len(waiting), "names": [w["name"] for w in waiting],
            "detail": f"{len(waiting)} unanswered repl{'y' if len(waiting) == 1 else 'ies'}: {names}"}


def _step_deck_clicks(conn, dry_run: bool) -> dict:
    """Pull intro-deck clicks from the collector on the sender's own site.

    Reads only. A click is the strongest engagement signal short of a reply, and unlike an
    open-tracking pixel it means a human chose to look — so it belongs in the same heartbeat
    that notices replies.
    """
    from applypilot.networking import deck_hits
    ok, why = deck_hits.configured()
    if not ok:
        return {"skipped": True, "detail": why}
    if dry_run:
        hits, err = deck_hits.fetch()
        return ({"error": err, "detail": err} if err
                else {"would_record": len(hits), "detail": f"{len(hits)} click(s) waiting"})
    res = deck_hits.poll(conn)
    return {"recorded": res.get("recorded", 0), "new": res.get("new", 0),
            "detail": res.get("note", "")}


#: Order matters: release locks first so a stuck job is visible to everything after it, and
#: poll replies BEFORE drafting so a contact who just answered never gets a follow-up drafted.
#: `unanswered` runs after the poll so a reply that arrived this minute is already counted.
#: `deck` sits before drafting too — knowing somebody read the deck changes what a follow-up
#: should say, and a step that ran after the draft could not influence it.
def _step_bookings(conn, dry_run: bool) -> dict:
    """Notice when somebody actually booked a call.

    The strongest signal a contact can produce — time, not just words — and it was invisible:
    the scheduling link goes to cal.com, which we do not control. But cal.com emails the host
    on every booking, and that email is already in the mailbox we can search.
    """
    from applypilot.networking import bookings, gmail_read
    ok, why = gmail_read.can_read_content()
    if not ok:
        return {"skipped": True, "detail": f"booking detection needs reply content — {why}"}
    if dry_run:
        return {"detail": "would check the mailbox for booking confirmations"}
    res = bookings.poll(conn)
    return {"found": res.get("found", 0), "new": res.get("new", 0),
            "detail": res.get("note", "")}


STEPS = (
    ("locks", _step_release_locks),
    ("replies", _step_poll_replies),
    ("unanswered", _step_unanswered),
    ("deck", _step_deck_clicks),
    # Before drafting, like the others: a follow-up written to somebody who already booked a
    # call is the most obviously automated message this system could send.
    ("bookings", _step_bookings),
    ("followups", _step_draft_followups),
)


def run(dry_run: bool = False, conn=None) -> dict:
    """One heartbeat. Returns a per-step summary; never raises."""
    from applypilot import config
    from applypilot.database import get_connection, init_db

    config.load_env()
    config.ensure_dirs()
    init_db()
    conn = conn or get_connection()

    started = _now()
    results: dict[str, dict] = {}
    for name, fn in STEPS:
        try:
            results[name] = fn(conn, dry_run)
        except Exception as e:  # noqa: BLE001
            # Isolated on purpose. A heartbeat that aborts halfway is worse than none: the
            # steps that did run look identical to the steps that did not.
            log.warning("tick step %s failed: %s", name, e)
            results[name] = {"error": str(e)[:200], "detail": f"failed: {e}"}

    summary = {"started_at": started, "finished_at": _now(), "dry_run": dry_run,
               "steps": results}
    if not dry_run:
        _record(summary, conn)
    return summary


def _record(summary: dict, conn) -> None:
    """Leave a trace so an unattended run is auditable after the fact."""
    from applypilot.networking import gmail_read
    try:
        gmail_read.save_watermark(checked_at=summary["finished_at"])
    except Exception:  # noqa: BLE001
        log.debug("Could not stamp the tick time", exc_info=True)
