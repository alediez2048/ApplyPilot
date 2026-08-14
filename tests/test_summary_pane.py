"""The Summary tab's layout — reported as looking bare, and the diagnosis was INFORMATION.

The first version rendered four rows in five reading `waiting` and nothing else: no time on any
row, no sense of what was being waited for, a `✉ 1/4 💬 — 📞 —` counts cell that reads as missing
data rather than "no number", and a four-cell grid stretched across the full ~1100px panel so the
eye crossed an inch of nothing between a name and its status.

What is asserted here is the information, not the styling: every row carries a TIME, the status
names the actual next message, the track shows one pip per planned message, and the pane offers
something to click. Layout itself is checked in a browser — the suite cannot see it (§Lessons 101,
106), which is how the legend shipped with three filled "done" dots above every job.
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
    f = tmp_path / "probe.mjs"
    f.write_text(
        BROWSER_GLOBALS
        + "const el = () => ({ innerHTML:\'\', textContent:\'\', hidden:false, value:\'\', style:{},"
          " closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},"
          " getAttribute:()=>null, addEventListener(){}, appendChild(){},"
          " classList:{add(){},remove(){},toggle(){}}, dataset:{} });\n"
          "globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),"
          " querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,"
          " activeElement:null };\n"
        + f"const SRC = {json.dumps(src)};\n" + tail, encoding="utf-8")
    proc = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def ch(sent=0, planned=4, possible=True, state="waiting", due=False, due_in_h=28, last="",
       label="email"):
    return {"sent": sent, "planned": planned, "possible": possible, "state": state,
            "due": due, "due_in_h": due_in_h, "last_at": last, "channel": "x", "label": label,
            "started": bool(sent)}


def row(**kw):
    r = {"id": "c1", "full_name": "Taylor Crawford", "title": "Talent Acquisition",
         "email": "t@x.test", "phone": "", "replied": False, "replied_at": "",
         "last_at": "2026-08-13T20:00:00+00:00", "next_in_h": 28,
         "next_label": "email 2 of 4", "stage": {"key": "email", "label": "Emails", "index": 0},
         "next": None,
         "channels": {"email": ch(sent=1), "sms": ch(planned=3, possible=False, label="text"),
                      "call": ch(planned=2, possible=False), "linkedin": ch(planned=1)}}
    r.update(kw)
    return r


def _pane(tmp_path, rows, **cov):
    c = {"people": len(rows), "rows": rows, "emails": 5, "texts": 0, "calls": 0, "invites": 5,
         "replied": 0, "due": 0, "with_phone": 0,
         "unworked": {"texted": [], "called": [], "no_phone": []},
         "close_warning": {"warn": False, "lines": []}}
    c.update(cov)
    j = {"url": "http://j/1", "title": "Role", "company": "Acme", "status": "applied",
         "contacts": rows, "coverage": c}
    return _run(tmp_path, f"const J = {json.dumps(j)};\n"
                "const F = (new Function(SRC + \'; return { summaryPane };\'))();\n"
                "console.log(JSON.stringify({html: F.summaryPane(J)}));\n")["html"]


def test_every_row_carries_a_TIME(tmp_path):
    """The whole gap. Four rows in five said "waiting" and no row said when anything happened."""
    html = _pane(tmp_path, [row()])
    assert "last touch" in html
    assert "ago" in html, "no row says when we last spoke to this person"


def test_the_status_names_the_NEXT_MESSAGE_not_just_a_clock(tmp_path):
    """"next in 1d" is the same non-answer as "waiting": it says a timer is running and nothing
    about what it will do."""
    html = _pane(tmp_path, [row()])
    assert "email 2 of 4" in html


def test_the_track_is_one_pip_per_PLANNED_message(tmp_path):
    """4 email pips with 1 filled — legible as "1 of 4 sent" without reading a number."""
    html = _pane(tmp_path, [row()])
    seg = html[html.index("sq"):html.index("sum-st")]
    assert seg.count("<i") >= 4, f"the email track has no pips: {seg[:200]}"
    assert seg.count('class="on"') == 1


def test_a_channel_with_no_identifier_is_MARKED_not_shown_as_zero(tmp_path):
    """`💬 —` read as missing data. It is not missing — there is no number, which is a
    different fact and has a different fix."""
    html = _pane(tmp_path, [row()])
    assert "sq off" in html
    assert "no number" in html


def test_the_pane_offers_something_to_CLICK(tmp_path):
    """A summary whose job is to drive work and offers nothing to act on is a report
    (§Lessons 43's family)."""
    html = _pane(tmp_path, [row(next={"channel": "email", "what": "follow up by email"})])
    assert "sum-go" in html and "Write" in html


def test_the_button_opens_the_channel_it_NAMES(tmp_path):
    """"Text ↗" landing on the email composer is a promise the page does not keep."""
    html = _pane(tmp_path, [row(phone="+1 555 0100",
                                next={"channel": "sms", "what": "text and call them"})])
    assert "Text ↗" in html
    i = html.index("Text ↗")
    assert "\'phone\'" in html[max(0, i - 220):i], "the Text button does not open the Text tab"


def test_people_who_need_you_are_GROUPED_away_from_those_who_do_not(tmp_path):
    """A flat list makes the one person who needs you look exactly like the four who do not —
    the failure the 🔔 counter exists to prevent, one level down."""
    html = _pane(tmp_path, [row(id="a", replied=True, replied_at="2026-08-13T20:00:00+00:00"),
                            row(id="b")])
    assert "Needs you" in html and "In sequence" in html


def test_no_group_headers_when_there_is_only_one_group(tmp_path):
    """Furniture that says nothing. With everyone in the same state the headers are noise."""
    html = _pane(tmp_path, [row(id="a"), row(id="b")])
    assert "In sequence" not in html and "Needs you" not in html


def test_a_replied_row_is_accented_and_offers_a_REPLY(tmp_path):
    html = _pane(tmp_path, [row(replied=True, replied_at="2026-08-13T20:00:00+00:00")])
    assert "sum-row rep" in html
    assert "they replied" in html and "Reply" in html


def test_the_stage_bar_is_filled_from_the_ROWS_not_hardcoded(tmp_path):
    """It reports how many people cleared each stage. A static bar would look identical on a
    job nobody has been contacted for."""
    nobody = _pane(tmp_path, [row(id="a"), row(id="b")])
    assert "width:0%" in nobody, "the stage bar shows progress on a job with none"
    done = _pane(tmp_path, [row(id="a", stage={"key": "done", "label": "x", "index": 3}),
                            row(id="b", stage={"key": "done", "label": "x", "index": 3})])
    assert "width:100%" in done
