"""Outcome metrics: what actually worked.

Pure aggregation over rows — dicts in, numbers out. No DB, no HTTP, so every figure here is
unit-testable against fixtures and reconcilable with hand-counted SQL.

**A CRM that generates but never counts is a very sophisticated mail merge.** The test for
including a number is whether it would change a decision. Anything that would not is noise.

Two honesty rules are built into the types rather than left to the UI:

  * every rate carries its `n`, and knows whether that `n` is enough to mean anything;
  * a **bounced** email is excluded from the denominator. It never arrived, so counting it as
    "emailed, no reply" understates the true rate — the live DB already has one Affirm address
    that bounced silently for two weeks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Below this, a percentage is arithmetic rather than evidence. With 33 emails the top-level
#: rate is becoming real; every per-slice cut is still far short.
MIN_MEANINGFUL_N = 10


@dataclass(frozen=True)
class Rate:
    """A proportion that knows how much to trust itself."""

    hits: int
    n: int
    label: str = ""

    @property
    def pct(self) -> float:
        return (100.0 * self.hits / self.n) if self.n else 0.0

    @property
    def meaningful(self) -> bool:
        """False when the sample is too small to read as a rate.

        Reported, never hidden: "1 of 3" is useful, "33%" from the same data is a lie with a
        decimal point.
        """
        return self.n >= MIN_MEANINGFUL_N

    def as_dict(self) -> dict:
        return {"hits": self.hits, "n": self.n, "pct": round(self.pct, 1),
                "meaningful": self.meaningful, "label": self.label}


@dataclass
class Funnel:
    discovered: int = 0
    applied: int = 0
    contacted: int = 0
    emailed: int = 0
    replied: int = 0
    bounced: int = 0
    steps: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"discovered": self.discovered, "applied": self.applied,
                "contacted": self.contacted, "emailed": self.emailed,
                "replied": self.replied, "bounced": self.bounced,
                "steps": self.steps}


def _emailed(c: dict) -> bool:
    return bool(c.get("sent_message_id") or c.get("submitted_at"))


def _bounced(c: dict) -> bool:
    return (c.get("email_status") or "") == "bounced"


def _replied(c: dict) -> bool:
    return bool(c.get("replied_at"))


def _deliverable(contacts: list[dict]) -> list[dict]:
    """Emailed contacts whose mail actually arrived.

    The denominator for every reply rate. A bounce is not a silent non-answer — it is a message
    that never existed from the recipient's point of view, and leaving it in makes outreach look
    worse than it is while hiding the real problem (a dead address).
    """
    return [c for c in contacts if _emailed(c) and not _bounced(c)]


def funnel(jobs: list[dict], contacts: list[dict]) -> Funnel:
    """discovered → applied → contacted → emailed → replied, with the bounce leak shown."""
    jobs = jobs or []
    contacts = contacts or []
    job_urls_with_contacts = {c.get("job_url") for c in contacts if c.get("job_url")}
    f = Funnel(
        discovered=len(jobs),
        applied=sum(1 for j in jobs if j.get("applied_at")),
        contacted=len(job_urls_with_contacts),
        emailed=sum(1 for c in contacts if _emailed(c)),
        replied=sum(1 for c in contacts if _replied(c)),
        bounced=sum(1 for c in contacts if _bounced(c)),
    )
    f.steps = [
        {"key": "discovered", "label": "Jobs", "n": f.discovered},
        {"key": "applied", "label": "Applied", "n": f.applied},
        {"key": "contacted", "label": "With contacts", "n": f.contacted},
        {"key": "emailed", "label": "Emailed", "n": f.emailed},
        {"key": "replied", "label": "Replied", "n": f.replied},
    ]
    return f


def reply_rate(contacts: list[dict], label: str = "overall") -> Rate:
    pool = _deliverable(contacts)
    return Rate(hits=sum(1 for c in pool if _replied(c)), n=len(pool), label=label)


def by_layer(contacts: list[dict]) -> list[Rate]:
    """Warm (a LinkedIn connection) vs cold (Apollo).

    `hunter` is a legacy provider, removed in favour of Apollo. Folded into COLD rather than
    dropped: those emails were really sent and really did or did not get answered, and silently
    excluding them would quietly shrink the only sample big enough to read.
    """
    warm = [c for c in contacts if (c.get("source") or "") == "connection"]
    cold = [c for c in contacts if (c.get("source") or "") != "connection"]
    return [reply_rate(warm, "warm (your connections)"), reply_rate(cold, "cold (Apollo)")]


def by_variant(contacts: list[dict]) -> list[Rate]:
    """Reply rate per DRAFT VARIANT — "cold+jd2k+deck" vs "cold+jd2k+noticed+deck".

    The metric the system was missing, and the reason every improvement to the copy was
    unfalsifiable: after 77 emails and 2 replies there was no way to ask whether the
    personalised ones did better. A single reply-rate number moves for reasons nobody can name.

    Untagged contacts are grouped as "(untagged)" rather than dropped. Every email sent before
    tagging existed is untagged, and silently excluding them would make the first tagged variant
    look like the whole history. They are shown, and they are honestly labelled.

    Every Rate carries its n, and `meaningful` is False below MIN_MEANINGFUL_N — which for a
    while will be all of them. That is the point: an unreadable number that says so beats a
    confident percentage drawn from three sends.
    """
    buckets: dict[str, list[dict]] = {}
    for c in contacts:
        key = (c.get("draft_variant") or "").strip() or "(untagged)"
        buckets.setdefault(key, []).append(c)
    # Most-sent first: the variants with enough n to read belong at the top.
    return [reply_rate(v, k) for k, v in
            sorted(buckets.items(), key=lambda kv: -len(_deliverable(kv[1])))]


def by_confidence(contacts: list[dict]) -> list[Rate]:
    """Does verification's confidence predict a reply?

    The one metric here that changes CODE rather than behaviour: if `unconfirmed` contacts never
    reply, verification should reject harder; if they reply as often as confirmed ones, it is
    rejecting too much and losing real people.
    """
    out = []
    for level in ("high", "medium", "low", ""):
        pool = [c for c in contacts if (c.get("confidence") or "") == level]
        if pool:
            out.append(reply_rate(pool, level or "unverified"))
    return out


def by_touch(contacts: list[dict], touches: list[dict]) -> list[Rate]:
    """Reply rate by how many follow-ups a contact had received.

    Answers "does the third message earn its place?" — the only way to justify the length of
    `FOLLOWUP_SCHEDULE` with something other than instinct.
    """
    sent_by_contact: dict[str, int] = {}
    for t in touches or []:
        if t.get("channel") == "email" and t.get("sent_at"):
            cid = t.get("contact_id")
            if cid:
                sent_by_contact[cid] = sent_by_contact.get(cid, 0) + 1

    buckets: dict[int, list[dict]] = {}
    for c in _deliverable(contacts):
        buckets.setdefault(sent_by_contact.get(c.get("id"), 0), []).append(c)

    return [Rate(hits=sum(1 for c in pool if _replied(c)), n=len(pool),
                 label=("first email only" if k == 0 else f"+{k} follow-up{'s' if k > 1 else ''}"))
            for k, pool in sorted(buckets.items())]


def by_company(contacts: list[dict], min_emails: int = 2) -> list[dict]:
    """Who never answers — where to stop spending Apollo credits.

    `min_emails` keeps a single unanswered email from branding a company as unresponsive.
    """
    out: dict[str, dict] = {}
    for c in _deliverable(contacts):
        name = (c.get("company") or "").strip() or "—"
        row = out.setdefault(name, {"company": name, "emailed": 0, "replied": 0, "bounced": 0})
        row["emailed"] += 1
        row["replied"] += 1 if _replied(c) else 0
    for c in contacts:
        if _bounced(c):
            name = (c.get("company") or "").strip() or "—"
            out.setdefault(name, {"company": name, "emailed": 0, "replied": 0, "bounced": 0})
            out[name]["bounced"] += 1
    rows = [r for r in out.values() if r["emailed"] >= min_emails or r["bounced"]]
    return sorted(rows, key=lambda r: (r["replied"], -r["emailed"]))


def time_to_reply_hours(contacts: list[dict], parse_ts) -> list[float]:
    """Hours between sending and the reply, for every answered contact.

    `parse_ts` is injected rather than imported so this module stays free of the naive/aware
    timestamp handling in `domain.timeutil` — and, more to the point, free of anything that
    could raise on the mixed-tz rows older parts of the DB still hold.
    """
    out = []
    for c in contacts:
        if not _replied(c) or not c.get("submitted_at"):
            continue
        try:
            sent = parse_ts(c["submitted_at"])
            got = parse_ts(c["replied_at"])
            if sent and got:
                delta = (got - sent).total_seconds() / 3600.0
                if delta >= 0:
                    out.append(round(delta, 1))
        except Exception:  # noqa: BLE001
            continue
    return sorted(out)


def median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else round((vals[mid - 1] + vals[mid]) / 2, 1)


def summary(jobs: list[dict], contacts: list[dict], touches: list[dict], parse_ts) -> dict:
    """Everything the dashboard panel and `stats --outreach` both render."""
    ttr = time_to_reply_hours(contacts, parse_ts)
    return {
        "funnel": funnel(jobs, contacts).as_dict(),
        "overall": reply_rate(contacts).as_dict(),
        "by_layer": [r.as_dict() for r in by_layer(contacts)],
        "by_variant": [r.as_dict() for r in by_variant(contacts)],
        "by_confidence": [r.as_dict() for r in by_confidence(contacts)],
        "by_touch": [r.as_dict() for r in by_touch(contacts, touches)],
        "by_company": by_company(contacts),
        "median_hours_to_reply": median(ttr),
        "min_meaningful_n": MIN_MEANINGFUL_N,
    }
