"""Guard: the dashboard's render path must not throw at RUNTIME.

Parsing the script catches stray quotes but NOT a call to a function that no longer exists —
exactly the failure mode of restructuring the job panel. A ReferenceError inside refresh()
blanks the whole jobs table just as silently as a SyntaxError does. (ARCH-2 retired the
separate `node --check` test: constructing the Function below already throws on a syntax
error, so parsing is covered here and by ESLint.)

This evaluates the served script under minimal DOM stubs and actually calls the row renderers
against a synthetic job covering every branch: each tab, each contact channel, hot/cold
contacts, missing email, missing LinkedIn, due/waiting follow-ups.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from applypilot import web_dashboard

_STUBS = """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, appendChild(){}, dataset:{} });
globalThis.document = { getElementById: el, querySelectorAll: ()=>[], querySelector: el,
  addEventListener(){}, activeElement:null, body: el() };
globalThis.window = { open(){}, location:{href:''} };
Object.defineProperty(globalThis, "navigator",
  { value:{ clipboard:{ writeText(){} } }, configurable:true });
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.fetch = async () => ({ json: async () => ({}) });
globalThis.alert = () => {};
globalThis.confirm = () => true;
"""

_DRIVER = """
const F = (new Function(SRC + `; return { stepStrip, jobTabs, jobPane, peopleList,
  contactRow, contactPanel, nextAction, PANEL_OPEN, CONTACT_OPEN, TAB_OPEN };`))();
let n = 0; const errors = [];
for (const j of JOBS) {
  for (const [name, fn] of [['stepStrip',F.stepStrip], ['nextAction',F.nextAction]]) {
    try { if (typeof fn(j) !== 'string') throw new Error('did not return a string'); n++; }
    catch (e) { errors.push(name + ': ' + e.message); }
  }
  F.PANEL_OPEN.add(j.url);
  for (const tab of ['people','followups','materials','activity']) {
    F.TAB_OPEN.set(j.url, tab);
    try { F.jobTabs(j); F.jobPane(j); n += 2; }
    catch (e) { errors.push('jobPane[' + tab + ']: ' + e.message); }
  }
  for (const c of (j.contacts || [])) {
    try { F.contactRow(c); n++; } catch (e) { errors.push('contactRow: ' + e.message); }
    F.CONTACT_OPEN.add(c.id);
    for (const ch of ['email','linkedin','phone']) {
      F.contactPanel.CH = ch;
      try { F.contactPanel(c); n++; } catch (e) { errors.push('contactPanel: ' + e.message); }
    }
    F.CONTACT_OPEN.delete(c.id);
  }
}
console.log(JSON.stringify({ checked: n, errors }));
"""


def _page_js() -> str:
    path = web_dashboard._STATIC_DIR / "dashboard.js"
    if not path.exists():
        pytest.skip("dashboard.js not found")
    return path.read_text(encoding="utf-8")


def _contact(**over):
    base = {
        "id": "c1", "full_name": "Jane Smith", "title": "Recruiter", "email": "jane@x.com",
        "email_status": "verified", "linkedin_url": "https://l/in/jane", "match_reason": "recruiter",
        "outreach_subject": "Hi", "outreach_message": "Body", "linkedin_message": "note",
        "outreach_status": "submitted", "emailed": True, "submitted_at": "2026-07-20T10:00:00+00:00",
        "followed_up_at": "", "followup_count": 0, "followup_status": "", "followup_subject": "",
        "followup_message": "", "followup_error": "", "threaded": True,
        "followup_state": "due", "followup_due_in_h": 0, "followup_touch": 1,
        "dm_status": "none", "dm_error": "", "dm_ready": True, "phone": "", "notes": "",
        "apollo_url": "https://app.apollo.io/#/people/x", "apollo_search_url": "https://app.apollo.io/#/people?qKeywords=Jane",
        "is_connection": False, "connection_at_company": False, "connection_url": "",
        "connection_company": "", "hot": False,
    }
    base.update(over)
    return base


def _job(**over):
    base = {
        "url": "http://j/1", "title": "PM", "company": "Acme", "contact_company": "Acme",
        "connections_at_company": 1, "salary": "", "location": "", "description": "d",
        "application_url": "http://a/1", "fit_score": 9, "reasoning": "", "status": "applied",
        "apply_error": "", "apply_attempts": 0, "applied_at": "2026-07-20T10:00:00+00:00",
        "rejected_at": "", "last_attempted_at": "", "materials": [],
        "network_running": False, "network_note": "", "network_error": "",
        "activity": [{"ts": "2026-07-20T10:00:00+00:00", "stage": "apply",
                      "status": "ok", "detail": "Applied."}],
        "contacts": [
            _contact(),
            _contact(id="c2", full_name="No Email", email="", email_status="none",
                     emailed=False, followup_state="", hot=True, connection_company="Acme",
                     dm_status="manual"),
            _contact(id="c3", full_name="No LinkedIn", linkedin_url="", phone="+1 555 000 1111",
                     followup_state="waiting", followup_due_in_h=30,
                     followup_message="Re: hi", followup_subject="Re: Hi"),
        ],
        "checklist": {"steps": [
            {"key": "contacts", "label": "Found people", "done": 1, "total": 1, "state": "done", "hint": ""},
            {"key": "applied", "label": "Applied", "done": 1, "total": 1, "state": "done", "hint": ""},
            {"key": "emailed", "label": "Emailed", "done": 2, "total": 3, "state": "partial", "hint": "h"},
            {"key": "linkedin", "label": "LinkedIn", "done": 0, "total": 2, "state": "todo", "hint": "h"},
            {"key": "followup", "label": "Followed up", "done": 0, "total": 0, "state": "na", "hint": ""},
        ], "pct": 62, "complete": False, "followups_due": 1},
        "followups": {"due": [{"id": "c1", "full_name": "Jane Smith", "title": "Recruiter",
                               "touch": 1, "due_in_h": 0, "state": "due"}],
                      "waiting": [{"id": "c3", "full_name": "No LinkedIn", "title": "R",
                                   "touch": 1, "due_in_h": 30, "state": "waiting"}],
                      "finished": [], "stopped": [], "due_count": 1,
                      "total_touches": 3, "schedule": [48, 96, 168]},
    }
    base.update(over)
    return base


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_job_panel_renders_without_runtime_errors(tmp_path):
    jobs = [
        _job(),
        # every status the strip's next-action switch branches on
        _job(url="http://j/2", status="ready", applied_at="", contacts=[], checklist=None,
             followups=None, activity=[]),
        _job(url="http://j/3", status="needs_human", apply_error="captcha"),
        _job(url="http://j/4", status="failed"),
        _job(url="http://j/5", status="rejected", rejected_at="2026-07-21T10:00:00+00:00"),
        _job(url="http://j/6", status="ready_to_submit"),
    ]
    script = tmp_path / "smoke.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {json.dumps(_page_js())};\n"
        + f"const JOBS = {json.dumps(jobs)};\n"
        + _DRIVER
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not result["errors"], "render errors:\n  " + "\n  ".join(result["errors"][:10])
    assert result["checked"] > 50, f"suspiciously few render calls: {result['checked']}"
