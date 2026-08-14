"""A moved contact's ROW must remember what was already said to them.

Reported as: "make sure that if contacts get imported to a different conversation, these
touchpoints showing on the right of each contact row remember the state of the previous job."

It is a tension CO-2 introduced deliberately and then got half right. `outreach_job_url` scoping
makes `emailed` read False on the new card so the new role can run its OWN sequence — correct,
and it is what stops a moved contact arriving with a spent ladder. But the row PILLS read the
same flag, so somebody four emails deep who had just been moved rendered "✉ draft", as though
nobody had ever written to them.

Live, after the real WebAI move: three contacts, four outbound emails each, all showing draft.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import applypilot.database as database
from applypilot.networking import messages as _msg, migrate, store
from applypilot.repo import jobs as _jobs

DEAD = "https://jobs.example.com/webai/forward-deployed"
LIVE = "https://jobs.example.com/webai/software-engineer"
ME = "me@work.test"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _msg.init_messages(conn)
    _jobs.insert_imported(DEAD, "AI Forward Deployed Engineer", "Webai", "Webai", DEAD, conn)
    _jobs.insert_imported(LIVE, "AI Software Engineer", "Webai", "Webai", LIVE, conn)
    return conn


def _emailed(conn):
    cid = store.upsert_contact({"job_url": DEAD, "full_name": "Marcus Godin", "company": "Webai",
                                "email": "m@webai.com", "outreach_status": "submitted",
                                "sent_message_id": "gm1", "submitted_at": "2026-08-01T09:00",
                                "outreach_message": "I just applied for the Forward Deployed "
                                                    "Engineer role"}, conn)
    _msg.upsert_messages([{
        "message_id": f"m{i}", "thread_id": "t1", "contact_id": cid, "job_url": DEAD,
        "direction": "out", "from_addr": ME, "from_name": "", "to_addrs": ["m@webai.com"],
        "cc_addrs": [], "subject": "quick q about the forward deployed engineer role",
        "sent_at": f"2026-08-0{i}T09:00", "rfc_message_id": f"<m{i}>", "snippet": "hi"}
        for i in range(1, 5)], conn)
    return cid


def _payload(db, cid):
    from applypilot import web_dashboard as wd
    c = store.get_contact(cid, db)
    return wd._contact_payload(c, company="Webai", ladders={}, conn_matches={},
                               thread=_msg.thread_for_contact(cid, db),
                               job_titles={DEAD: "AI Forward Deployed Engineer"})


def test_the_row_remembers_the_emails_that_already_went_out(db):
    """The report, in one assertion."""
    cid = _emailed(db)
    migrate.apply(DEAD, LIVE, [cid], db)
    got = _payload(db, store.contact_id(LIVE, None, "Marcus Godin"))
    assert got["emailed"] is False, "the new role must still be able to run its own sequence"
    assert got["prior_outreach"]["emails"] == 4, "the row forgot four emails"


def test_it_NAMES_the_role_those_emails_were_about(db):
    """"4 already sent" is a number; "4 already sent for AI Forward Deployed Engineer" is what
    tells the operator whether writing again is a repeat or a new conversation."""
    cid = _emailed(db)
    migrate.apply(DEAD, LIVE, [cid], db)
    got = _payload(db, store.contact_id(LIVE, None, "Marcus Godin"))
    assert got["prior_outreach"]["job_title"] == "AI Forward Deployed Engineer"


def test_a_contact_that_never_moved_has_NO_prior_block(db):
    """The negative control. `prior_outreach` on an ordinary row would put "earlier role" on
    every contact in the database."""
    assert _payload(db, _emailed(db))["prior_outreach"] is None


# ── the pill ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def node():
    if not shutil.which("node"):
        pytest.skip("node not available")


def _row(tmp_path, c):
    import applypilot.web_dashboard as wd
    from browser_stubs import BROWSER_GLOBALS
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    f = tmp_path / "probe.mjs"
    f.write_text(
        BROWSER_GLOBALS
        + "const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},"
          " closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},"
          " getAttribute:()=>null, addEventListener(){}, appendChild(){},"
          " classList:{add(){},remove(){},toggle(){}}, dataset:{} });\n"
          "globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),"
          " querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,"
          " activeElement:null };\n"
        + f"const SRC = {json.dumps(src)};\nconst C = {json.dumps(c)};\n"
          "const F = (new Function(SRC + '; return { contactRow };'))();\n"
          "console.log(JSON.stringify({html: F.contactRow(C)}));\n", encoding="utf-8")
    proc = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])["html"]


def _c(**kw):
    return {"id": "c1", "full_name": "Marcus Godin", "email": "m@webai.com", "emailed": False,
            "dm_status": "none", "linkedin_url": "", "replied_at": "", "thread": [],
            "prior_outreach": None, "followup_state": "", "transcripts": [], **kw}


def test_a_moved_contact_does_NOT_render_as_a_fresh_draft(tmp_path):
    html = _row(tmp_path, _c(prior_outreach={"emails": 4, "job_title": "AI Forward Deployed"}))
    assert "✉ draft" not in html, "a contact four emails deep still reads as unwritten-to"
    assert "4 sent · earlier role" in html


def test_the_pill_is_MARKED_not_merged_with_an_ordinary_send(tmp_path):
    """"We have spoken" and "we have spoken about this job" are different facts, and acting on
    the wrong one is how the same person gets written to twice."""
    moved = _row(tmp_path, _c(prior_outreach={"emails": 4, "job_title": "X"}))
    here = _row(tmp_path, _c(emailed=True))
    assert "pill on prev" in moved
    assert "pill on prev" not in here and "✉ sent" in here


def test_an_ordinary_contact_is_unchanged(tmp_path):
    """373 rows carry no `prior_outreach`; none of them may gain a pill from this."""
    assert "earlier role" not in _row(tmp_path, _c())
    assert "✉ draft" in _row(tmp_path, _c())


def test_this_role_beats_the_earlier_one_when_both_are_true(tmp_path):
    """Once real outreach starts here, THIS role's state is the one that matters."""
    html = _row(tmp_path, _c(emailed=True, prior_outreach={"emails": 4, "job_title": "X"}))
    assert "✉ sent" in html and "earlier role" not in html
