"""Per-job completion checklist — "did I actually work this job?"

Drives both the status strip (a left-to-right path with the current step highlighted) and
the single Next action on each row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot.domain.timeutil import parse_ts


def followup_after_days() -> int:
    """Days after the FIRST email before a follow-up counts as owed, for the checklist step.

    Derived from `FOLLOWUP_SCHEDULE`'s first entry (ARCH-6) — one knob per concept. The
    legacy `FOLLOWUP_AFTER_DAYS` still overrides it, with a deprecation warning at startup.

    NOTE: this is a DIFFERENT rule from the ladder's, deliberately. The checklist measures
    from the first email and ignores ladder position; the panel measures from the most
    recent touch. Sharing the config does not merge the rules — see followup.py's note
    about `followup_due`, which a byte-identical /api/status check once caught.
    """
    from applypilot import settings
    values, _ = settings.resolve()
    return max(0, int(values.get("FOLLOWUP_AFTER_DAYS") or 0))


def _step(key: str, label: str, done_n: int, total_n: int, hint: str = "") -> dict:
    if total_n == 0:
        return {"key": key, "label": label, "done": 0, "total": 0, "state": "na", "hint": hint}
    state = "done" if done_n >= total_n else ("partial" if done_n else "todo")
    return {"key": key, "label": label, "done": done_n, "total": total_n,
            "state": state, "hint": hint}


def job_checklist(job_status: str, applied_at: str, contacts: list[dict],
                  now: datetime | None = None, shape: str = "pipeline/jobs",
                  interview_at: str = "") -> dict:
    """Completion state for one row.

    Steps with a zero denominator come back as 'na' and are EXCLUDED from the percentage.
    A job with no emailable contacts must still be able to reach 100%, or the widget reads
    as permanently broken rather than as a goal.

    `shape` decides whether "Applied to the job" is one of the steps at all (SPACE-3). It is
    OMITTED for a targets row rather than marked `na`, and the distinction is the point: `na`
    means "this step has no work in it" — no emailable contacts, no LinkedIn profiles — and the
    strip still draws it, greyed, as a thing that could have happened. There is no application
    to submit to a company you are pitching, so drawing the step at all would describe work that
    does not exist. Compare §Lessons 35: a widget that answers its own question is worth
    nothing, and a permanently-grey step is that with extra pixels.

    `interview_at` ends the follow-up step. Marking an interview already HALTS every ladder —
    chasing somebody after they agreed to meet is the one follow-up guaranteed to cost
    something — but the checklist measures from `submitted_at` and knew nothing about it, so a
    won job kept counting follow-ups as owed. Live on WRITER: temperature `won`, Next slot
    empty, excluded from the 🔔 counter, and the strip still reading `↻ Follow up 1/2`. Three
    surfaces agreeing and one contradicting them is §Lessons 21 — a fact one layer has that
    the other cannot see.

    Nothing needs special-casing downstream: with no follow-up owed the step is either `done`
    (some were sent, none outstanding) or `na` (none were ever owed), and both are true.

    Side effect: stamps `followup_due` on each contact, so the per-contact button and this
    widget agree on who is owed one — one cutoff, computed once.
    """
    now = now or datetime.now(timezone.utc)

    emailable = [c for c in contacts if c.get("email")]
    emailed = [c for c in emailable if c.get("emailed")]
    linkedinable = [c for c in contacts if c.get("linkedin_url")]
    connected = [c for c in linkedinable if c.get("dm_status") in ("sent", "manual")]

    # An interview stops every sequence, so nothing here is owed any more. Computed as a flag
    # rather than by clearing `due` afterwards, because the loop below also stamps
    # `followup_due` on each contact — and that is what the per-contact button reads, so the
    # two have to go quiet together or the row and the person disagree.
    won = bool((interview_at or "").strip())
    cutoff = now - timedelta(days=followup_after_days())
    due, done = [], []
    for c in contacts:
        c["followup_due"] = False
    for c in emailed:
        if (c.get("followed_up_at") or "").strip():
            done.append(c)
            continue
        if won:
            continue
        sent_dt = parse_ts(c.get("submitted_at"))
        if sent_dt is not None and sent_dt <= cutoff:
            due.append(c)
            c["followup_due"] = True

    applied = bool((applied_at or "").strip()) or job_status == "applied"
    targets = shape == "pipeline/targets"
    steps = [
        _step("contacts", "Found people", 1 if contacts else 0, 1,
              "Find the decision-maker and whoever owns the work."
              if targets else "Run “Find contacts” to pull recruiters and peers."),
        *([] if targets else [
            _step("applied", "Applied to the job", 1 if applied else 0, 1,
                  "The application itself hasn’t been submitted yet."),
        ]),
        _step("emailed", "Emailed everyone", len(emailed), len(emailable),
              "Some reachable contacts haven’t been emailed."),
        _step("linkedin", "LinkedIn invites sent", len(connected), len(linkedinable),
              "Copy the note, send the invite, then mark it."),
        # `total` is done + still-owed, so this reads 100% until a follow-up actually comes due.
        _step("followup", f"Followed up (after {followup_after_days()}d)",
              len(done), len(done) + len(due),
              f"{len(due)} follow-up(s) now due."),
    ]
    counted = [s for s in steps if s["state"] != "na"]
    pct = round(100 * sum(s["done"] / s["total"] for s in counted) / len(counted)) if counted else 0
    return {"steps": steps, "pct": pct,
            "complete": bool(counted) and all(s["state"] == "done" for s in counted),
            "followups_due": len(due)}
