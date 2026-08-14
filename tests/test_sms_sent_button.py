"""The "✓ I sent it" button on a text — reported as doing nothing.

It did. The server stamps `sms_sent_at` and five live contacts carry one, **two of them recorded
minutes before the report was filed**. Driving `_followup_action` directly confirms both verbs:
`sms_connected` returns "recorded — follow-up clock started" and `sms_sent` records touch #1.

What did nothing was the screen. The follow-up variant of the button was offered the instant the
first text was recorded, so the click produced: "Done ✓", a 2.5s refresh, and a button identical
to the one just pressed. §Lessons 43's fourth form — the feature works perfectly and the result
is imperceptible, which is exactly how the greyed `won` row was reported.

And it was not only cosmetic. Offered while the ladder is still WAITING, a second click records a
touch for a text nobody sent and moves the whole cadence forward on evidence that is wrong — and
the evidence is operator-asserted, so nothing downstream can ever contradict it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def node():
    if not shutil.which("node"):
        pytest.skip("node not available")


def _run(tmp_path, tail):
    import applypilot.web_dashboard as wd
    from browser_stubs import BROWSER_GLOBALS
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "probe.mjs"
    script.write_text(
        BROWSER_GLOBALS
        + "const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},"
          " closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},"
          " getAttribute:()=>null, addEventListener(){}, appendChild(){},"
          " classList:{add(){},remove(){},toggle(){}}, dataset:{} });\n"
          "globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),"
          " querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,"
          " activeElement:null };\n"
        + f"const SRC = {json.dumps(src)};\n" + tail, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


#: A real live row: Zoë, a phone number, first text recorded yesterday, nothing due yet.
def _c(**kw):
    return {"id": "c1", "full_name": "Zoe Coleman", "phone": "+1 916-204-9535",
            "sms_sent_at": "", "sms_followup_state": "", "sms_followup_count": 0,
            "sms_followup_total": 2, "sms_followup_due_in_h": None,
            "sms_followup_message": "hi", "notes": "", "noticed": "", **kw}


def _pane(tmp_path, c):
    """Through the whole phone pane, not through `smsSentButton` — a test that calls the
    button builder proves it can be built and nothing about whether it reaches the page."""
    return _run(tmp_path, f"const C = {json.dumps(c)};\n"
                "const F = (new Function(SRC + '; return { smsChannel };'))();\n"
                "console.log(JSON.stringify({html: F.smsChannel(C)}));\n")["html"]


def test_the_FIRST_text_is_offered_when_nothing_has_been_recorded(tmp_path):
    html = _pane(tmp_path, _c())
    assert "sms_connected" in html and "I sent it" in html


def test_the_button_DISAPPEARS_once_the_first_text_is_recorded(tmp_path):
    """The report, in one assertion: the click has to change the screen. Re-rendering the same
    button is indistinguishable from the click having been ignored."""
    html = _pane(tmp_path, _c(sms_sent_at="2026-08-12T21:21", sms_followup_state="waiting",
                              sms_followup_due_in_h=60))
    assert "I sent it" not in html, "the same button is offered again after being pressed"


def test_and_it_says_so_LOUDLY(tmp_path):
    """`.muted` grey is what made a working feature read as a dead one. The acknowledgement is
    the only evidence the operator gets, because the button that produced it is gone."""
    html = _pane(tmp_path, _c(sms_sent_at="2026-08-12T21:21", sms_followup_state="waiting",
                              sms_followup_due_in_h=60))
    assert "✓ texted 2026-08-12" in html
    i = html.index("✓ texted")
    assert "sent-tag" in html[max(0, i - 120):i], "the confirmation is not the affirmative style"


def test_a_follow_up_is_offered_ONLY_when_one_is_actually_due(tmp_path):
    """The data half. Clicking while the ladder is waiting records a touch for a text nobody
    sent — and it is operator-asserted, so nothing downstream can ever contradict it."""
    waiting = _pane(tmp_path, _c(sms_sent_at="2026-08-12T21:21", sms_followup_state="waiting",
                                 sms_followup_due_in_h=60))
    assert "sms_sent" not in waiting
    due = _pane(tmp_path, _c(sms_sent_at="2026-08-09T21:21", sms_followup_state="due",
                             sms_followup_count=0))
    assert "sms_sent" in due and "I sent it" in due


def test_a_finished_or_replied_ladder_offers_nothing(tmp_path):
    for state in ("replied", "stopped", "finished"):
        assert "I sent it" not in _pane(
            tmp_path, _c(sms_sent_at="2026-08-01T09:00", sms_followup_state=state)), state


def test_without_a_number_the_button_is_rendered_DISABLED_not_hidden(tmp_path):
    """§Lessons 41/99: the empty pane is where a number gets entered, so the composer stays and
    the control is disabled with the reason rather than described in a sentence."""
    html = _pane(tmp_path, _c(phone=""))
    i = html.index("I sent it")
    assert "disabled" in html[max(0, i - 200):i]


# ── the server half, which was never broken ─────────────────────────────────

def test_both_verbs_really_do_record(tmp_path, monkeypatch):
    """Pinned because the diagnosis depended on it: had this been the failure, hiding a button
    would have been the wrong fix entirely (§Lessons 104 — verify the reported thing end to end
    before believing the report's diagnosis)."""
    import applypilot.database as database
    from applypilot.networking import store, touches
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

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Blake",
                                "phone": "+15125550101"}, conn)
    assert wd._followup_action({"contact_id": cid, "action": "sms_connected"})["ok"]
    assert (store.get_contact(cid, conn)["sms_sent_at"] or "").strip()
    # Idempotent: the only evidence is somebody clicking, so a double-click must not move the
    # anchor forward and silently push every touch later.
    again = wd._followup_action({"contact_id": cid, "action": "sms_connected"})
    assert again["message"] == "already recorded"
    assert wd._followup_action({"contact_id": cid, "action": "sms_sent"})["touch"] == 1


def test_the_CALL_channel_records_the_same_way(tmp_path, monkeypatch):
    """The fourth channel through the same door. `_followup_action` maps a channel to its anchor
    setter rather than branching, so `call_connected` needed one entry in that dict — and a
    missing entry returns "call has no anchor to set", which is a button that reports failure
    rather than one that silently does nothing.
    """
    import applypilot.database as database
    from applypilot.networking import store, touches
    path = tmp_path / "c.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(wd, "init_db", lambda *a, **k: conn)

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Marcus",
                                "phone": "+15125550199"}, conn)
    got = wd._followup_action({"contact_id": cid, "action": "call_connected"})
    assert got["ok"], got.get("message")
    assert (store.get_contact(cid, conn)["call_made_at"] or "").strip()
    # Idempotent, and here it matters more than anywhere: a second stamp pushes the ONE
    # follow-up call three days further out on somebody already rung once who said nothing.
    assert wd._followup_action({"contact_id": cid, "action": "call_connected"})[
        "message"] == "already recorded"
    assert wd._followup_action({"contact_id": cid, "action": "call_sent"})["touch"] == 1
