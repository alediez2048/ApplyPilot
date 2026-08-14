""""✓ I emailed them" — the marker every other channel had and email did not.

Reported as: "this contact shows as if I have not sent an email but I HAVE."

Email was left out of the operator-asserted markers on the reasoning that Gmail's send response
proves itself. It proves the sends that went THROUGH ApplyPilot, and is silent about an email
typed in Gmail, sent from a phone, or picked up by the address search afterwards. Measured live:
**9 contacts with outbound mail on record and no send state at all** — Waheed Brown (3 outbound),
Sydney Hardwick (4), David Loveless (2), Lindsay (2) — so each card offered a cold first contact
to somebody already several emails deep.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import applypilot.database as database
from applypilot.domain import followup as fu
from applypilot.networking import store, touches


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(wd, "init_db", lambda *a, **k: conn)
    return conn


def _c(conn, **kw):
    row = {"job_url": "http://j/1", "full_name": "Waheed Brown", "email": "w@arm.com",
           "company": "Arm", "outreach_status": "drafted"}
    row.update(kw)
    return store.upsert_contact(row, conn)


def _act(cid, action="connected"):
    from applypilot import web_dashboard as wd
    return wd._followup_action({"contact_id": cid, "action": action})


def test_marking_it_records_a_send(db):
    cid = _c(db)
    assert _act(cid)["ok"]
    c = store.get_contact(cid, db)
    assert (c["submitted_at"] or "").strip()
    assert c["outreach_status"] == "submitted"


def test_it_does_NOT_fake_a_gmail_message_id(db):
    """`sent_message_id` is Gmail's own id and THREADING reads it. Inventing one would put a
    follow-up `In-Reply-To` a thread that does not exist."""
    cid = _c(db)
    _act(cid)
    assert not (store.get_contact(cid, db)["sent_message_id"] or "").strip()


def test_it_is_idempotent(db):
    """A click is the only evidence, which makes the double-click the obvious failure — and a
    second stamp would move the ladder's anchor forward and push every follow-up later."""
    cid = _c(db)
    assert _act(cid)["message"] == "recorded — follow-up clock started"
    assert _act(cid)["message"] == "already recorded"
    before = store.get_contact(cid, db)["submitted_at"]
    _act(cid)
    assert store.get_contact(cid, db)["submitted_at"] == before


def test_a_REAL_send_is_never_overwritten(db):
    """Somebody ApplyPilot already emailed must not have their send date reset by a stray
    click — that restarts a ladder mid-flight."""
    cid = _c(db, outreach_status="submitted", sent_message_id="gm1",
             submitted_at="2026-08-01T09:00")
    assert _act(cid)["message"] == "already recorded"
    assert store.get_contact(cid, db)["submitted_at"] == "2026-08-01T09:00"


# ── the ladder actually starts, which is the point ─────────────────────────

def _state(c, hours):
    return fu.touch_state(fu.normalize_for_ladder(c), fu.EMAIL, [48, 96, 168],
                          datetime.now(timezone.utc) + timedelta(hours=hours),
                          fu.EMPTY_LADDER)[0]


def test_the_follow_up_clock_starts(db):
    """The half that made this more than cosmetic. `normalize_for_ladder` checked ONLY
    `sent_message_id`, so a hand-marked send left the dashboard reading "emailed" while the
    ladder read the channel as never used and scheduled nothing — §Lessons 21's exact shape,
    one derived field computed two ways."""
    cid = _c(db)
    assert _state(store.get_contact(cid, db), 50) == "", "the fixture was already laddered"
    _act(cid)
    c = store.get_contact(cid, db)
    assert _state(c, 10) == "waiting"
    assert _state(c, 50) == "due"


def test_the_ladder_and_the_PAYLOAD_agree(db):
    """The two readers of `emailed` must not disagree. They did: the payload counted
    `outreach_status == 'submitted'` and the ladder did not."""
    from applypilot import web_dashboard as wd
    cid = _c(db)
    _act(cid)
    c = store.get_contact(cid, db)
    assert wd._contact_payload(c, ladders={}, conn_matches={})["emailed"] is True
    assert fu.normalize_for_ladder(c)["emailed"] is True


def test_a_MOVED_contact_can_still_assert_a_send_here(db):
    """Their stored send belongs to the other role (CO-2), so it is not "already recorded" —
    and asserting one here is how the new role's ladder starts. It also clears the stamp,
    because the outreach on this row is now about the job it sits on."""
    cid = _c(db, outreach_status="submitted", sent_message_id="gm1",
             submitted_at="2026-08-01T09:00", outreach_job_url="http://j/other")
    assert _act(cid)["ok"] and _act(cid)["message"] == "already recorded"
    c = store.get_contact(cid, db)
    assert (c["outreach_job_url"] or "") == ""
    assert c["submitted_at"] != "2026-08-01T09:00", "kept the other role's send date"


# ── the button ──────────────────────────────────────────────────────────────

@pytest.fixture()
def node():
    if not shutil.which("node"):
        pytest.skip("node not available")


def _draft(tmp_path, c):
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
          "const F = (new Function(SRC + '; return { emailSentButton };'))();\n"
          "console.log(JSON.stringify({html: F.emailSentButton(C)}));\n", encoding="utf-8")
    proc = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])["html"]


def _row(**kw):
    return {"id": "c1", "full_name": "Waheed Brown", "email": "w@arm.com", "emailed": False, **kw}


def test_the_button_is_offered_when_nothing_is_recorded(tmp_path, node):
    html = _draft(tmp_path, _row())
    assert "I emailed them" in html and "<button" in html


def test_it_uses_the_BARE_verb_because_email_has_no_prefix(tmp_path, node):
    """Email is the unmarked channel: `_split_followup_action` falls back to it, so
    `email_connected` parses as the VERB "email_connected" and is rejected as unknown. The
    first version of this button shipped that and did nothing when clicked."""
    assert "'connected'" in _draft(tmp_path, _row())


def test_it_DISAPPEARS_once_a_send_is_recorded(tmp_path, node):
    """§Lessons 43's fourth form, which the text marker already needed: re-rendering the same
    button after a click is indistinguishable from the click being ignored."""
    assert _draft(tmp_path, _row(emailed=True)) == ""


def test_no_address_means_no_button(tmp_path, node):
    assert _draft(tmp_path, _row(email="")) == ""


def test_an_existing_send_DATE_survives_even_when_the_status_was_reset(db):
    """The branch the idempotence test cannot reach, and it survived a mutation.

    `submitted_at` set, status back to `drafted`, no message id — the shape a contact takes when
    a draft is REGENERATED after an email went out. "Already recorded" is False, so the marker
    proceeds; overwriting the anchor there would silently restart a ladder that is already three
    touches in.
    """
    cid = _c(db, submitted_at="2026-08-01T09:00", outreach_status="drafted")
    assert _act(cid)["ok"]
    assert store.get_contact(db and cid, db)["submitted_at"] == "2026-08-01T09:00"


# ── reachable on a card that shows a CONVERSATION, not just a draft form ────

def _email_pane(tmp_path, c):
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
          "const F = (new Function(SRC + '; return { emailChannel };'))();\n"
          "console.log(JSON.stringify({html: F.emailChannel(C)}));\n", encoding="utf-8")
    proc = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])["html"]


def _msg(direction="out", i=1):
    return {"message_id": f"m{i}", "thread_id": "t1", "direction": direction,
            "from_addr": "me@x.test" if direction == "out" else "w@arm.com",
            "from_name": "", "to_addrs": ["w@arm.com"], "cc_addrs": [],
            "subject": "hello", "sent_at": f"2026-08-0{i}T09:00", "snippet": "hi"}


def test_it_is_reachable_on_a_card_that_shows_a_CONVERSATION(tmp_path, node):
    """Where the first version was NOT. The marker rode in `draftBlock`'s button row, which
    never renders once a reply exists — so Patrick Omalley, six outbound emails and
    `emailed=false`, had no way to correct it. That was the contact being pointed at."""
    c = _row(thread=[_msg("out", 1), _msg("in", 2)], replied_at="2026-08-02T10:00")
    html = _email_pane(tmp_path, c)
    assert "I emailed them" in html, "no way to record the send on a replied contact"
    assert "nothing is recorded" in " ".join(html.split())


def test_it_SAYS_what_is_wrong_before_offering_the_fix(tmp_path, node):
    """On a card already displaying six sent emails, a bare "✓ I emailed them" reads as a
    duplicate of what you are looking at rather than a correction to invisible state."""
    c = _row(thread=[_msg("out", i) for i in range(1, 4)] + [_msg("in", 4)])
    html = _email_pane(tmp_path, c).replace("\n", " ")
    assert "3 emails here went out" in " ".join(html.split())


def test_a_correctly_recorded_contact_gets_no_bar(tmp_path, node):
    """It is a correction, so it must vanish the moment there is nothing to correct — otherwise
    it is on every card forever and stops being read."""
    c = _row(emailed=True, thread=[_msg("out", 1), _msg("in", 2)])
    assert "I emailed them" not in _email_pane(tmp_path, c)
    assert "marksent" not in _email_pane(tmp_path, c)


def test_a_BORROWED_card_is_not_offered_it(tmp_path, node):
    """Those messages belong to another role's row. Asserting a send here would record one
    against a job nothing was sent about."""
    c = _row(thread=[_msg("out", 1)], thread_from="http://j/other")
    assert "I emailed them" not in _email_pane(tmp_path, c)
