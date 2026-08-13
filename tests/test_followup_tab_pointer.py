"""An empty Follow-ups tab says where the Space's follow-ups actually ARE.

Reported as the follow-ups feature not working end to end: the counter said follow-ups were
pending, the tab had none to draft or send. Both numbers were right and neither pointed at the
other — the counter is GLOBAL and the tab is PER JOB.

Measured live on `job-search`: **19 due across 6 jobs, and 24 jobs showing an empty Follow-ups
tab.** So four times out of five, opening the tab the badge sent you looking for lands on one
with nothing in it, saying "Nothing due right now" — true of that job, useless beside a counter
reporting the whole Space.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


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


@pytest.fixture(autouse=True)
def node():
    if not shutil.which("node"):
        pytest.skip("node not available")


def _panel(due_ids):
    return {"schedule": [48, 96, 168], "total_touches": 3, "due_count": len(due_ids),
            "due": [{"id": i, "full_name": f"P{i}", "touch": 1, "due_in_h": 0, "state": "due"}
                    for i in due_ids],
            "waiting": [], "finished": [], "stopped": [],
            "sms_due": [], "sms_due_count": 0, "sms_waiting": [], "sms_finished": [],
            "sms_stopped": [], "sms_schedule": [72, 168], "sms_total_touches": 2}


#: The job the operator happens to open — it has a Follow-ups tab and nothing in it.
EMPTY = {"url": "http://j/empty", "title": "Quiet Role", "company": "Acme",
         "contacts": [], "followups": _panel([]), "status": "applied"}
#: Where the work actually is.
BUSY = {"url": "http://j/busy", "title": "Forward Deployed AI Engineer", "company": "EY",
        "contacts": [{"id": "x1", "full_name": "Arun"}], "followups": _panel(["x1"]),
        "status": "applied"}


def _html(tmp_path, job, all_jobs):
    return _run(tmp_path, f"const J = {json.dumps(job)};\n"
                f"const ALL = {json.dumps(all_jobs)};\n"
                "const F = (new Function(SRC + '; return { followupBody, LAST_JOBS,"
                " setJobs: (v) => { LAST_JOBS = v; }, jobPane, TAB_OPEN };'))();\n"
                "F.setJobs(ALL);\n"
                "console.log(JSON.stringify({html: F.followupBody(J, J.followups)}));\n")["html"]


def test_an_empty_tab_says_how_many_are_due_elsewhere(tmp_path):
    """The report, in one assertion."""
    html = _html(tmp_path, EMPTY, [EMPTY, BUSY])
    assert "Nothing due right now" in html
    assert "1 due on 1 other application" in html, \
        f"the empty tab still points nowhere: {html[:300]}"


def test_it_offers_a_way_to_GET_there(tmp_path):
    """A count with no jump is the same dead end one sentence longer."""
    html = _html(tmp_path, EMPTY, [EMPTY, BUSY])
    assert "gotoTodo" in html
    assert "Forward Deployed AI Engineer" in html, "it does not say WHICH application"


def test_it_says_nothing_when_there_is_nothing_anywhere(tmp_path):
    """The ordinary case is genuinely "nothing to do". A pointer on every empty tab is noise,
    and a message that is always there is one nobody reads."""
    html = _html(tmp_path, EMPTY, [EMPTY])
    assert "Nothing due right now" in html
    assert "other application" not in html


def test_the_job_you_are_ON_is_never_counted_as_elsewhere(tmp_path):
    """Otherwise a job with work would tell you to go to itself."""
    assert "other application" not in _html(tmp_path, BUSY, [EMPTY, BUSY])
    # ...and the pointer really does fire when it should, so this cannot pass on a harness
    # where `LAST_JOBS` was never set (§Lessons 71: an assertion that cannot fail).
    assert "other application" in _html(tmp_path, EMPTY, [EMPTY, BUSY])


def test_closed_and_interviewing_jobs_are_not_offered(tmp_path):
    """Both have stopped asking for work — the counter excludes them, and sending the operator
    to one would be the same mismatch pointing the other way."""
    closed = dict(BUSY, url="http://j/closed", status="rejected", apply_status="rejected")
    won = dict(BUSY, url="http://j/won", interview_at="2026-08-01T10:00")
    assert "other application" not in _html(tmp_path, EMPTY, [EMPTY, closed, won]), \
        "offered a job that has left the pipeline"
    # The same two jobs, open, DO produce a pointer — so this is about `isClosed`, not about
    # the harness quietly rendering nothing.
    live = dict(BUSY, url="http://j/live")
    assert "other application" in _html(tmp_path, EMPTY, [EMPTY, live])


def test_it_sums_across_several_jobs(tmp_path):
    """The number has to match the counter's, or this replaces one mismatch with another."""
    b2 = dict(BUSY, url="http://j/busy2", title="Second Role",
              followups=_panel(["y1", "y2"]))
    html = _html(tmp_path, EMPTY, [EMPTY, BUSY, b2])
    assert "3 due on 2 other applications" in html
