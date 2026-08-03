"""Two limits the audit found missing: how much one employer receives, and what worked.

Neither existed, and both are invisible failures — the first because nothing counted per
company, the second because reply rate was a single number that moved for unnameable reasons.

Live numbers that motivated them: 34 first emails and 43 follow-ups had gone out for 2 replies,
and Webai and Salesforce had already received **10 emails each** before anything objected.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.domain import metrics
from applypilot.networking import gmail_send, outreach, store, touches


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    conn.execute("INSERT INTO jobs (url, title, site) VALUES (?,?,?)",
                 ("http://j/1", "AI Engineer", "Greenhouse"))
    conn.execute("INSERT INTO jobs (url, title, site) VALUES (?,?,?)",
                 ("http://j/2", "Staff Engineer", "Greenhouse"))
    conn.commit()
    return conn


def _person(db, name, company="Acme", job="http://j/1", sent=True):
    cid = store.upsert_contact({
        "job_url": job, "full_name": name, "company": company,
        "email": f"{name.lower().replace(' ', '')}@acme.com",
        "outreach_status": "submitted" if sent else "drafted",
        "sent_message_id": "g1" if sent else "",
    }, db)
    return cid


# ── per-company cap ─────────────────────────────────────────────────────────

def test_it_counts_first_emails_and_follow_ups_together(db):
    """Follow-ups are the reason the cap is needed — 43 of them against 34 first contacts, so
    most of what reaches an employer is chasing, and a cap that ignored them would miss it."""
    a = _person(db, "Ann")
    _person(db, "Bob")
    touches.record_sent(a, "email", conn=db)
    touches.record_sent(a, "email", conn=db)
    assert store.emails_sent_to_company("Acme", db) == 4      # 2 first + 2 follow-ups


def test_it_counts_across_jobs(db):
    """Applying to two roles at one employer does not buy a second allowance. The inbox and the
    reputation are shared — this is the case the per-address cooldown cannot see at all."""
    _person(db, "Ann", job="http://j/1")
    _person(db, "Bob", job="http://j/2")
    assert store.emails_sent_to_company("Acme", db) == 2


def test_unsent_drafts_do_not_count(db):
    """A draft is not an email. Counting them would block sends because of work never done."""
    _person(db, "Ann", sent=True)
    _person(db, "Bob", sent=False)
    assert store.emails_sent_to_company("Acme", db) == 1


def test_a_different_company_has_its_own_budget(db):
    _person(db, "Ann", company="Acme")
    _person(db, "Bob", company="Globex")
    assert store.emails_sent_to_company("Acme", db) == 1
    assert store.emails_sent_to_company("Globex", db) == 1


def test_company_matching_is_case_and_space_insensitive(db):
    """A false negative here silently RAISES the cap, which is the dangerous direction —
    "acme " and "Acme" must not be two budgets."""
    _person(db, "Ann", company="Acme")
    _person(db, "Bob", company="  ACME ")
    assert store.emails_sent_to_company("acme", db) == 2


def test_a_contact_with_no_company_is_not_counted_against_everyone(db):
    """An empty company must return 0, not match every blank-company row — otherwise one
    unattributed contact caps every send in the system."""
    store.upsert_contact({"job_url": "http://j/1", "full_name": "Nobody", "company": "",
                          "email": "n@x.com", "outreach_status": "submitted"}, db)
    assert store.emails_sent_to_company("", db) == 0


def test_the_cap_blocks_and_says_why(db, monkeypatch):
    """§Lessons 15 — a refusal that does not name the reason is indistinguishable from a
    button that did nothing."""
    monkeypatch.setattr(gmail_send, "configured", lambda: True)   # no token in a temp APP_DIR
    monkeypatch.setattr(gmail_send, "_COMPANY_CAP", 2)
    for n in ("Ann", "Bob"):
        _person(db, n)
    fresh = {"id": "x", "job_url": "http://j/1", "full_name": "Cy", "company": "Acme",
             "email": "cy@acme.com", "email_status": "verified", "outreach_status": "drafted"}
    ok, why = gmail_send.can_send(fresh, confirm_unverified=True)
    assert ok is False
    assert "Acme" in why and "2" in why, f"the refusal does not name the company or count: {why}"


def test_a_cap_of_zero_disables_it(db, monkeypatch):
    monkeypatch.setattr(gmail_send, "configured", lambda: True)
    monkeypatch.setattr(gmail_send, "_COMPANY_CAP", 0)
    for n in ("Ann", "Bob", "Cy"):
        _person(db, n)
    fresh = {"id": "x", "job_url": "http://j/1", "full_name": "Dee", "company": "Acme",
             "email": "dee@acme.com", "email_status": "verified", "outreach_status": "drafted"}
    assert gmail_send.can_send(fresh, confirm_unverified=True)[0] is True


def test_it_is_a_declared_setting():
    from applypilot import settings
    s = settings._BY_NAME.get("OUTREACH_COMPANY_CAP")
    assert s is not None and s.kind == "int" and s.default == 8


# ── draft variant ───────────────────────────────────────────────────────────

def test_the_signature_records_inputs_not_a_version():
    """A version number goes stale the moment a prompt is edited and silently pools two
    different things under one label. A signature of the inputs stays true."""
    v = outreach.draft_variant(warm=False, jd_chars=2400, noticed=True, deck=True,
                               scheduling=True, style=False)
    assert v == "cold+jd2k+noticed+deck+cal"


def test_the_same_inputs_always_produce_the_same_signature():
    """Otherwise every draft is its own variant and nothing accumulates an n worth reading."""
    a = outreach.draft_variant(warm=True, jd_chars=2100, deck=True)
    b = outreach.draft_variant(warm=True, jd_chars=2450, deck=True)
    assert a == b == "warm+jd2k+deck", "jd length is not bucketed, so n never accumulates"


def test_differing_inputs_produce_differing_signatures():
    with_note = outreach.draft_variant(jd_chars=2000, noticed=True)
    without = outreach.draft_variant(jd_chars=2000, noticed=False)
    assert with_note != without, "the observation field is invisible to attribution"


def test_reply_rate_is_broken_out_by_variant():
    contacts = [
        {"email": "a@x.com", "sent_message_id": "g1", "draft_variant": "cold+jd2k",
         "replied_at": ""},
        {"email": "b@x.com", "sent_message_id": "g2", "draft_variant": "cold+jd2k",
         "replied_at": ""},
        {"email": "c@x.com", "sent_message_id": "g3",
         "draft_variant": "cold+jd2k+noticed", "replied_at": "2026-08-01T00:00:00+00:00"},
    ]
    rates = {r.label: r for r in metrics.by_variant(contacts)}
    assert rates["cold+jd2k"].hits == 0 and rates["cold+jd2k"].n == 2
    assert rates["cold+jd2k+noticed"].hits == 1


def test_untagged_history_is_shown_not_silently_dropped():
    """Every email sent before tagging existed is untagged. Excluding them would make the first
    tagged variant look like the entire history."""
    contacts = [{"email": "a@x.com", "sent_message_id": "g1", "replied_at": ""},
                {"email": "b@x.com", "sent_message_id": "g2",
                 "draft_variant": "cold+jd2k", "replied_at": ""}]
    labels = [r.label for r in metrics.by_variant(contacts)]
    assert "(untagged)" in labels, "pre-tagging sends vanished from the breakdown"


def test_a_thin_variant_reports_counts_not_a_percentage():
    """"100%" from one send is a lie with a decimal point. Every Rate carries its n and says
    when it is unreadable — which, for a while, all of these will be."""
    one = [{"email": "a@x.com", "sent_message_id": "g1", "draft_variant": "cold",
            "replied_at": "2026-08-01T00:00:00+00:00"}]
    assert metrics.by_variant(one)[0].meaningful is False


# ── zero means unlimited, in all three caps ─────────────────────────────────

def test_zero_disables_the_daily_limit_rather_than_blocking_everything(monkeypatch):
    """The trap this shipped with. `_COMPANY_CAP` was guarded by `> 0` and a zero-day cooldown
    window matches nothing, so 0 already meant "off" in two of the three caps — but the daily
    limit compared `sent_today() >= 0`, which is true before the first email of the day.

    So turning the limit off silently blocked EVERY send, and the refusal read "daily send limit
    reached (0)" — a message describing a cap that had been switched off. One word meaning
    "unlimited" here and "send nothing" there is not a setting, it is a coin flip.
    """
    from applypilot.networking import gmail_send, store
    monkeypatch.setattr(gmail_send, "_DAILY_LIMIT", 0)
    monkeypatch.setattr(gmail_send, "_COMPANY_CAP", 0)
    monkeypatch.setattr(gmail_send, "_COOLDOWN_DAYS", 0)
    monkeypatch.setattr(gmail_send, "configured", lambda: True)
    monkeypatch.setattr(store, "sent_today", lambda *a, **k: 500)
    monkeypatch.setattr(store, "already_contacted_email", lambda *a, **k: None)
    monkeypatch.setattr(store, "emails_sent_to_company", lambda *a, **k: 500)

    ok, why = gmail_send.can_send({"email": "a@b.com", "email_status": "verified",
                                   "company": "Acme"})
    assert ok, f"a disabled cap still blocked the send: {why}"


def test_a_positive_daily_limit_still_stops_at_the_cap(monkeypatch):
    """The other half — without this, deleting the check entirely would also pass."""
    from applypilot.networking import gmail_send, store
    monkeypatch.setattr(gmail_send, "_DAILY_LIMIT", 20)
    monkeypatch.setattr(gmail_send, "configured", lambda: True)
    monkeypatch.setattr(store, "sent_today", lambda *a, **k: 20)
    ok, why = gmail_send.can_send({"email": "a@b.com", "email_status": "verified"})
    assert not ok and "daily send limit" in why
