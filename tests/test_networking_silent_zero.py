"""A contact search that finds nobody must SAY so.

2026-07-29, reported as "find contacts is not working" on a Zello application. It had in fact
run, and the run had done real work: Apollo returned 5 candidates, 4 emails were revealed
(credits spent), and then verification correctly dropped all 5 as people who work at a
different company named Zello — Apollo lists THREE orgs named Zello/ZELLO, none with a
primary_domain, so which one the search hits is close to a coin flip (§Lessons 5).

The bug is not the dropping. Verification was right. The bug is that the outcome was
invisible in every surface the operator looks at:

  * `log_event` was gated on `stored_contacts` being non-empty, so the Activity tab got
    nothing at all;
  * `network_note` ("5 found, 4 with email (ok; dropped 5 who work elsewhere)") was returned
    by /api/status and never rendered by any JS;
  * the People tab therefore still read "No contacts yet. [Find contacts]".

A run that did the work and found nobody was byte-for-byte indistinguishable from a button
that never fired. That is what cost the diagnosis, so it is what these tests pin.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.networking import service, store, verify


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return database.get_connection(path)


JOB = {
    "url": "https://jobs.ashbyhq.com/Zello/2fa8cd4a/application",
    "title": "AI Engineer",
    "company": "Zello",
}


def _candidates(n: int) -> list[dict]:
    """n people whose employer is a DIFFERENT company that merely shares the name."""
    return [{"key": f"k{i}", "full_name": f"Person {i}", "title": "Engineer",
             "company": "Zello Systems GmbH", "employer_domain": "zello-systems.de",
             "linkedin_url": f"http://linkedin.com/in/p{i}", "source": "apollo"}
            for i in range(n)]


@pytest.fixture()
def all_rejected(monkeypatch):
    """Wire the provider chain so 5 candidates are found, enriched, then all rejected."""
    from applypilot.networking import connections, providers

    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(providers, "search", lambda *a, **k: _candidates(5))
    monkeypatch.setattr(providers, "enrich",
                        lambda sel: {c["key"]: {"email": f"{c['key']}@zello-systems.de",
                                                "email_status": "verified"} for c in sel})
    monkeypatch.setattr(connections, "count_at_company", lambda *a, **k: 0)
    # The real verdict for these people, from the real function's own logic.
    monkeypatch.setattr(verify, "verify_contact",
                        lambda c, comp, dom: {"verdict": verify.REJECT,
                                              "reasons": ["works at Zello Systems GmbH"],
                                              "confidence": "low"})


def test_a_run_that_drops_everyone_still_logs_an_event(db, all_rejected):
    """The exact silence. Without an event the Activity tab shows nothing, so the operator
    cannot tell a completed search from a dead button."""
    res = service.find_contacts_for_job(JOB, per_job=5)

    assert res["contacts"] == [], "fixture should have rejected all 5"
    events = database.get_job_events(JOB["url"], conn=db)
    details = [e["detail"] or "" for e in events]
    assert details, "a search that found 5 people and dropped 5 logged NOTHING"


def test_the_event_explains_why_there_are_no_contacts(db, all_rejected):
    """"0 contacts" and "5 found, all working elsewhere" need different reactions from the
    operator: the first means try again, the second means the employer name is ambiguous."""
    service.find_contacts_for_job(JOB, per_job=5)
    blob = " ".join(e["detail"] or "" for e in database.get_job_events(JOB["url"], conn=db))

    assert "5" in blob, f"the number of people considered is missing: {blob!r}"
    assert "elsewhere" in blob.lower(), f"the REASON is missing: {blob!r}"


def test_a_search_with_no_candidates_at_all_also_logs(db, monkeypatch):
    """The other zero: the provider returned nobody, so there is nothing to drop. This path
    returned early, before any logging existed."""
    from applypilot.networking import connections, providers
    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(providers, "search", lambda *a, **k: [])
    monkeypatch.setattr(connections, "count_at_company", lambda *a, **k: 0)

    res = service.find_contacts_for_job(JOB, per_job=5)
    assert res["found"] == 0
    details = [e["detail"] or "" for e in database.get_job_events(JOB["url"], conn=db)]
    assert details, "a search that returned zero candidates logged NOTHING"


def test_a_successful_search_is_not_double_logged(db, monkeypatch):
    """Guard against the obvious fix going too far: adding a zero-case event must not also
    fire on the happy path, or every find writes two contradictory lines."""
    from applypilot.networking import connections, providers
    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(providers, "search", lambda *a, **k: _candidates(2))
    monkeypatch.setattr(providers, "enrich",
                        lambda sel: {c["key"]: {"email": f"{c['key']}@zello.com",
                                                "email_status": "verified"} for c in sel})
    monkeypatch.setattr(connections, "count_at_company", lambda *a, **k: 0)
    monkeypatch.setattr(verify, "verify_contact",
                        lambda c, comp, dom: {"verdict": verify.OK, "reasons": [],
                                              "confidence": "high"})
    monkeypatch.setattr(service, "_draft_for_contact", lambda *a, **k: None, raising=False)

    service.find_contacts_for_job(JOB, per_job=5, draft=False)

    # Count EVERY outreach event, not just ones containing the word "found". Filtering on
    # "found" made this test vacuous: the zero-case message says "No contacts kept", so an
    # over-fix that logged BOTH outcomes passed unnoticed. Mutation testing caught that.
    outreach = [e["detail"] or "" for e in database.get_job_events(JOB["url"], conn=db)
                if (e["stage"] or "") == "outreach"]
    assert len(outreach) == 1, f"expected exactly one outreach event, got {outreach}"
    assert "2 contact(s)" in outreach[0], outreach[0]
    assert "no contacts kept" not in outreach[0].lower(), \
        f"the happy path also logged the zero-case message: {outreach[0]!r}"


# ── The root cause: selection happens before verification ────────────────────────────────
#
# rank.select() scores TITLE relevance and knows nothing about which employer a person works
# for. The strongest verification signal — the work-email domain — only exists after
# enrichment, which runs on the selected few. So when an employer name is ambiguous, the
# best-titled people can all belong to the wrong company, get correctly dropped, and the real
# colleagues further down the pool are never even enriched.

def _mixed_pool() -> list[dict]:
    """The Zello shape: 5 best-titled people at the WRONG Zello, 2 real ones behind them.

    Every candidate's Apollo org name is literally "Zello" — all three orgs share the name —
    so nothing before enrichment can tell them apart. Only the revealed email domain can.
    """
    wrong = [{"key": f"w{i}", "full_name": f"Wrong {i}",
              # Exact title match => these rank top.
              "title": "AI Engineer", "company": "Zello",
              "linkedin_url": f"http://linkedin.com/in/w{i}", "source": "apollo"}
             for i in range(5)]
    right = [{"key": "r0", "full_name": "Real Recruiter", "title": "Recruiting",
              "company": "Zello", "linkedin_url": "http://linkedin.com/in/r0", "source": "apollo"},
             {"key": "r1", "full_name": "Real Tech Recruiter", "title": "Technical Recruiter",
              "company": "Zello", "linkedin_url": "http://linkedin.com/in/r1", "source": "apollo"}]
    return wrong + right


@pytest.fixture()
def ambiguous_employer(monkeypatch):
    from applypilot.networking import connections, providers

    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(providers, "search", lambda *a, **k: _mixed_pool())
    monkeypatch.setattr(connections, "count_at_company", lambda *a, **k: 0)
    monkeypatch.setattr(connections, "at_company", lambda *a, **k: [])

    def enrich(batch):
        # The wrong-Zello people work at a different domain; the real ones are @zello.com.
        return {c["key"]: {"email": f"{c['key']}@" + ("zello.com" if c["key"].startswith("r")
                                                      else "zello-systems.de"),
                           "email_status": "verified"} for c in batch}

    monkeypatch.setattr(providers, "enrich", enrich)

    def verify_contact(contact, employer, domain):
        email = contact.get("email") or ""
        if email.endswith("@zello-systems.de"):
            return {"verdict": verify.REJECT, "reasons": ["work email is zello-systems.de"],
                    "confidence": "low"}
        return {"verdict": verify.OK, "reasons": ["email domain matches"], "confidence": "high"}

    monkeypatch.setattr(verify, "verify_contact", verify_contact)
    monkeypatch.setattr(store, "upsert_contact", lambda c: c.get("full_name"))


def test_a_fully_rejected_batch_tops_up_from_the_rest_of_the_pool(db, ambiguous_employer):
    """The bug, exactly. per_job=5 selects the 5 best-titled people, all from the wrong Zello.
    Before the top-up the run ended there and stored ZERO, while the two genuine @zello.com
    recruiters — candidates 6 and 7 of 7 — were never enriched or examined."""
    res = service.find_contacts_for_job(JOB, per_job=5, draft=False)

    names = sorted(c["full_name"] for c in res["contacts"])
    assert names == ["Real Recruiter", "Real Tech Recruiter"], \
        f"the real contacts behind a rejected batch were never reached: {names}"
    assert len(res.get("rejected", [])) == 5, res.get("rejected")


def test_topping_up_is_bounded_so_it_cannot_burn_every_credit(db, monkeypatch):
    """Enrichment costs Apollo credits. A pool where NOBODY passes must not enrich all 25 —
    it stops after _TOPUP_ROUNDS batches."""
    from applypilot.networking import connections, providers
    enriched: list[str] = []

    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(providers, "search", lambda *a, **k: _candidates(25))
    monkeypatch.setattr(connections, "count_at_company", lambda *a, **k: 0)
    monkeypatch.setattr(connections, "at_company", lambda *a, **k: [])

    def enrich(batch):
        enriched.extend(c["key"] for c in batch)
        return {c["key"]: {"email": f"{c['key']}@elsewhere.de", "email_status": "verified"}
                for c in batch}

    monkeypatch.setattr(providers, "enrich", enrich)
    monkeypatch.setattr(verify, "verify_contact",
                        lambda c, comp, dom: {"verdict": verify.REJECT,
                                              "reasons": ["works elsewhere"],
                                              "confidence": "low"})

    service.find_contacts_for_job(JOB, per_job=5, draft=False)
    assert len(enriched) == 5 * service._TOPUP_ROUNDS, \
        f"enriched {len(enriched)} of 25 — the top-up is unbounded"
    assert len(enriched) == len(set(enriched)), "the same person was enriched twice"


def test_a_first_batch_that_passes_costs_no_extra_credits(db, monkeypatch):
    """The top-up must be a fallback, not a new baseline: when the first batch is clean, no
    second enrichment call may happen."""
    from applypilot.networking import connections, providers
    calls: list[int] = []

    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(providers, "search", lambda *a, **k: _candidates(25))
    monkeypatch.setattr(connections, "count_at_company", lambda *a, **k: 0)
    monkeypatch.setattr(connections, "at_company", lambda *a, **k: [])

    def enrich(batch):
        calls.append(len(batch))
        return {c["key"]: {"email": f"{c['key']}@zello.com", "email_status": "verified"}
                for c in batch}

    monkeypatch.setattr(providers, "enrich", enrich)
    monkeypatch.setattr(verify, "verify_contact",
                        lambda c, comp, dom: {"verdict": verify.OK, "reasons": [],
                                              "confidence": "high"})
    monkeypatch.setattr(store, "upsert_contact", lambda c: c.get("full_name"))

    res = service.find_contacts_for_job(JOB, per_job=5, draft=False)
    assert len(res["contacts"]) == 5
    assert calls == [5], f"expected ONE enrichment of 5, got {calls}"
