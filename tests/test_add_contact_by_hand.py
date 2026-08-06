"""Adding a contact by hand — the referral Apollo never found.

Someone you emailed writes back and hands you to a colleague. Sometimes that shows up as a Cc
on the thread, which `introBanner` already detects. Often it does not: it is said on a call, in
a LinkedIn DM, or in a reply that names the person without copying them. That is the warmest
lead this system can produce, and until now the only way to record it was to go and look them
up in Apollo — the automatic door only opens onto a state that already contains the answer
(§Lessons 37).

Both doors write through one function, and the difference between them is kept deliberately:
`email_status='verified'` is a claim about the ADDRESS, true of a Cc off a live thread and not
of one typed from memory.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.networking import store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    conn.execute("INSERT INTO jobs (url, title, site, company, strategy, tailored_resume_path) "
                 "VALUES (?,?,?,?,?,?)",
                 ("http://j/1", "AI Engineer", "Greenhouse", "Acme", "dashboard_upload",
                  "/tmp/r.pdf"))
    conn.commit()
    # Drafting is an LLM call and is not what any of this is testing; the add must survive it
    # failing anyway, which is why the real code catches around it.
    monkeypatch.setattr(wd, "load_profile", lambda *a, **k: {}, raising=False)
    return conn


def _add(**over):
    data = {"job_url": "http://j/1", "name": "Priya Raman"}
    data.update(over)
    return wd._add_introduced_contact(data)


def _one(db):
    rows = [dict(r) for r in db.execute("SELECT * FROM contacts WHERE job_url='http://j/1'")]
    assert len(rows) == 1, rows
    return rows[0]


# ── what makes someone reachable ────────────────────────────────────────────

def test_a_linkedin_url_alone_is_enough(db):
    """The case that motivated this. Half of what an introduction hands you is a profile and no
    address — refusing it would send you to Apollo to look up somebody you were just given."""
    out = _add(linkedin_url="https://www.linkedin.com/in/priya-raman/")
    assert out["ok"], out
    c = _one(db)
    assert c["linkedin_url"] == "https://www.linkedin.com/in/priya-raman/"
    assert not (c["email"] or ""), "an email was invented for a contact who has none"
    assert c["email_status"] == "none", c["email_status"]


def test_an_email_alone_is_enough(db):
    assert _add(email="priya@acme.com")["ok"]
    assert _one(db)["email"] == "priya@acme.com"


def test_neither_is_refused_with_a_reason(db):
    """A contact with no address and no profile is a row nothing can ever do anything with —
    it would sit in the People list forever looking like progress."""
    out = _add()
    assert not out["ok"]
    assert "email" in out["message"].lower() and "linkedin" in out["message"].lower(), out
    assert not list(db.execute("SELECT 1 FROM contacts")), "an unreachable contact was stored"


def test_a_typo_is_refused_before_it_is_stored(db):
    """Not validation theatre: this address goes into a real outbound email, and the failure
    arrives days later as a bounce with no obvious cause."""
    assert not _add(email="priya.acme.com")["ok"]
    assert not _add(linkedin_url="https://twitter.com/priya")["ok"]
    assert not list(db.execute("SELECT 1 FROM contacts"))


# ── the two doors, and what separates them ──────────────────────────────────

def test_a_hand_typed_address_is_not_marked_verified(db):
    """`email_status` is a claim about the ADDRESS. Marking a remembered one 'verified' would
    put it in the same class as a Cc read off a live thread, and the reply-poller and the bounce
    handling both read that field."""
    assert _add(email="priya@acme.com")["ok"]
    assert _one(db)["email_status"] == "unverified"


def test_an_address_off_a_live_thread_still_is(db):
    """The original CRM-4a path must not regress: that address was Cc'd on a real thread, so it
    is real by construction."""
    assert _add(email="priya@acme.com", introduced_by="Victoria", on_thread=1)["ok"]
    assert _one(db)["email_status"] == "verified"


def test_a_referral_is_not_pooled_with_a_plain_manual_add(db):
    """`source='introduction'` exists so CRM-2's by_layer() can eventually prove that a warm
    handoff outperforms a cold list. Labelling every hand-added contact an introduction would
    quietly destroy that comparison with the operator's own address book."""
    assert _add(email="a@acme.com", introduced_by="Victoria")["ok"]
    assert _one(db)["source"] == "introduction"

    db.execute("DELETE FROM contacts")
    assert _add(email="b@acme.com")["ok"]
    assert _one(db)["source"] == "manual"


def test_the_referrer_is_recorded_where_it_can_be_read(db):
    """"Victoria sent me" is the strongest line the draft can open with, and it only exists if
    somebody wrote down who Victoria was."""
    assert _add(email="priya@acme.com", introduced_by="Victoria Shearer")["ok"]
    c = _one(db)
    assert "Victoria Shearer" in (c["match_reason"] or ""), c["match_reason"]
    assert "Victoria Shearer" in (c["verify_note"] or ""), c["verify_note"]


# ── the details that make it usable ─────────────────────────────────────────

def test_a_missing_name_is_derived_rather_than_refused(db):
    """You are given a link and no name more often than the reverse."""
    assert _add(name="", email="priya.raman@acme.com")["ok"]
    assert _one(db)["full_name"] == "Priya Raman"

    db.execute("DELETE FROM contacts")
    assert _add(name="", linkedin_url="https://linkedin.com/in/dev-patel")["ok"]
    assert _one(db)["full_name"] == "Dev Patel"


def test_it_inherits_the_job_company(db):
    """The draft names the employer, and this person works there — that is the whole reason
    they were introduced."""
    assert _add(email="priya@acme.com")["ok"]
    assert _one(db)["company"] == "Acme"


def test_a_hand_added_contact_is_not_flagged_as_unconfirmed(db):
    """Verification exists to catch people who work somewhere ELSE, which cannot happen to a
    name the operator chose (§Lessons 19 — they are the authority on who they were told to talk
    to). A `? unconfirmed` chip on a personal referral would read as a bug. The note says how it
    got here, so the claim stays checkable."""
    assert _add(email="priya@acme.com")["ok"]
    c = _one(db)
    assert c["confidence"] == "high", c["confidence"]
    assert (c["verify_note"] or "").strip(), "no record of where this contact came from"


def test_an_unknown_job_is_rejected(db):
    out = wd._add_introduced_contact({"job_url": "http://nope/9", "email": "a@b.com"})
    assert not out["ok"] and "job" in out["message"].lower()


def test_it_reaches_the_activity_log(db):
    """A contact that appears with no trace of where it came from is the state this exists to
    prevent — six weeks later nobody remembers who Priya was."""
    assert _add(email="priya@acme.com", introduced_by="Victoria")["ok"]
    rows = [dict(r) for r in db.execute(
        "SELECT detail FROM job_events WHERE job_url='http://j/1' AND stage='outreach'")]
    assert any("Priya" in (r["detail"] or "") and "Victoria" in (r["detail"] or "") for r in rows), rows
