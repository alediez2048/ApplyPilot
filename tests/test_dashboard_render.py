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
import re
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
        # CRM-4a: /api/status always sends these, so the fixture must too — a contact with no
        # conversation has an empty thread and a null reply target, never a missing key.
        "thread": [], "reply_to": None, "introduced_by": "", "conversation": None,
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
            # A live conversation with a handoff on it — the ONLY contact that exercises the
            # thread view and the reply composer. Without one, a ReferenceError in either would
            # blank the whole jobs table and no test would notice (§Lessons 7).
            _contact(id="c4", full_name="Victoria Shearer", email="victoria@writer.com",
                     followup_state="", introduced_by="Victoria Shearer",
                     thread=[
                         {"direction": "out", "from_addr": "me@x.com", "from_name": "",
                          "to_addrs": ["Victoria Shearer <victoria@writer.com>"],
                          "cc_addrs": [], "subject": "AI Engineer",
                          "sent_at": "2026-07-28T09:00:00+00:00"},
                         {"direction": "in", "from_addr": "victoria@writer.com",
                          "from_name": "Victoria Shearer", "to_addrs": ["me@x.com"],
                          "cc_addrs": ["David Loveless <david@writer.com>"],
                          "subject": "Re: AI Engineer", "sent_at": "2026-07-29T09:00:00+00:00"},
                     ],
                     reply_to={"to": "Victoria Shearer <victoria@writer.com>",
                               "to_addr": "victoria@writer.com",
                               "cc": ["David Loveless <david@writer.com>"],
                               "subject": "Re: AI Engineer", "in_reply_to": "<b@writer>",
                               "references": "<a@us> <b@writer>", "thread_id": "t1",
                               "answering": "Victoria Shearer",
                               "at": "2026-07-29T09:00:00+00:00"},
                     conversation={"state": "awaiting_us", "days": 2, "hours": 50,
                                   "at": "2026-07-29T09:00:00+00:00",
                                   "who": "Victoria Shearer", "messages": 2}),
        ],
        "awaiting_reply": [{"id": "c4", "full_name": "Victoria Shearer",
                            "days": 2, "hours": 50}],
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


_REPLY_DRIVER = """
const F = (new Function(SRC + `; return { contactPanel, CONTACT_OPEN, CHANNEL_TAB };`))();
const out = {};
for (const [name, c] of Object.entries(CASES)) {
  F.CONTACT_OPEN.add(c.id);
  F.CHANNEL_TAB.set(c.id, 'email');
  out[name] = F.contactPanel(c);
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_reply_composer_shows_who_it_will_reach(tmp_path):
    """"Renders without errors" would pass just as happily if the composer rendered NOTHING.

    What has to be on screen is the recipient list, because the Cc IS the feature: Victoria
    answered by adding David, and a reply that quietly goes only to Victoria looks identical to
    a correct one. If the operator cannot see David's name before clicking Send, the system is
    asking them to trust a decision it never showed them.
    """
    answered = _job()["contacts"][3]
    unanswered = _contact(id="c9", full_name="Nobody Answered", thread=[], reply_to=None)

    script = tmp_path / "reply.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {json.dumps(_page_js())};\n"
        + f"const CASES = {json.dumps({'answered': answered, 'unanswered': unanswered})};\n"
        + _REPLY_DRIVER
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    html = out["answered"]
    assert "reply-box" in html, "a contact who replied got no reply composer"
    assert "Victoria Shearer" in html, "the composer does not say who it is answering"
    # Deliberately NOT a bare `"david@writer.com" in html`: the address also travels in the
    # hidden data-cc attribute, so that assertion passes with the chips removed entirely and
    # nothing on screen naming him. Match the VISIBLE chip.
    chip = re.search(r'<button class="cc-chip[^"]*"[^>]*>([^<]*)</button>', html)
    assert chip and "david@writer.com" in chip.group(1), (
        "the introduced colleague is not VISIBLE on the reply — this is the silent drop CRM-4a "
        "exists to prevent, and the operator would click Send without ever seeing him")
    assert "sendReply(" in html and "Send reply" in html

    # The AI half. Both must be present on a DEFAULT install — no `last_reply` because the
    # content scope is off, so the operator pastes what they said and still gets a draft.
    assert "said-box" in html, "no way to tell it what they actually wrote"
    assert "draftReply(" in html and "Draft an answer" in html, "no way to draft an answer"
    # `class="r-style"` in full, not the bare substring: `"r-style" in html` also matches
    # `r-style-anything`, which is §Lessons 1 committed inside its own test.
    assert 'class="r-style"' in html, (
        "no vibe control — cold outreach has one and a reply should too")

    assert "reply-box" not in out["unanswered"], (
        "offered a 'reply' on a thread nobody answered — that is a follow-up, and it has its "
        "own ladder, schedule and stop conditions")


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_a_contact_who_replied_gets_a_conversation_not_an_outreach_form(tmp_path):
    """The reported bug, pinned.

    "if i open gina's contact the first thing it shows is the email box we already sent."
    It did: an EDITABLE form holding a message delivered five days earlier, with Copy and
    Regenerate controls, expanded and dominating — while the live exchange sat collapsed to one
    line. A sent email cannot be edited; presenting it as a form is offering an action that does
    not exist, and it pushed the only actionable thing below the fold.

    Once somebody replies the tab is a conversation: timeline first, composer anchored under it.
    """
    answered = _job()["contacts"][3]          # has an inbound message
    never = _contact(id="c9", full_name="No Reply Yet", thread=[], reply_to=None)

    script = tmp_path / "conv.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {json.dumps(_page_js())};\n"
        + f"const CASES = {json.dumps({'answered': answered, 'never': never})};\n"
        + _REPLY_DRIVER
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    html = out["answered"]

    assert "conv-msgs" in html, "no conversation timeline for a contact who replied"
    assert "d-subj" not in html and "Regenerate" not in html, (
        "the already-sent outreach still renders as an EDITABLE form — it is delivered and "
        "cannot be changed, and it buries the reply")
    # Order is the whole point: the exchange, then the composer.
    assert html.index("conv-msgs") < html.index("reply-box"), "composer above the conversation"
    assert "replied" in html and "haven" in html, "nothing says they are waiting on you"

    # A contact nobody has replied to keeps the outreach draft flow — chasing silence is the
    # right action there, and this must not have been broken in the process.
    assert "conv-msgs" not in out["never"]
    assert "reply-box" not in out["never"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_a_message_with_no_stored_text_says_so_instead_of_rendering_blank(tmp_path):
    """We hold headers for every message and TEXT only where it was pasted or the content scope
    supplied it. An empty bubble leaves the operator unable to tell a blank message from an
    unread one — the difference between "they sent nothing" and "we cannot see it"."""
    c = _job()["contacts"][3]
    script = tmp_path / "nobody.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {json.dumps(_page_js())};\n"
        + f"const CASES = {json.dumps({'x': c})};\n"
        + _REPLY_DRIVER
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    html = json.loads(proc.stdout.strip().splitlines()[-1])["x"]
    assert "cm-nobody" in html and "not stored" in html


_SAVE_DRIVER = """
const F = (new Function(SRC + `; return { draftBlock, fieldVal };`))();
// A minimal DOM stand-in: only the fields the tab actually rendered exist, which is the whole
// point — querySelector returns null for the rest, exactly as in the browser.
function fakeCard(html) {
  return { querySelector: sel => html.includes(sel.slice(1)) ? {value: 'typed-' + sel.slice(1)} : null };
}
const c = {id:'c1', full_name:'X', email:'a@b.com', linkedin_url:'https://l/in/x', emailed:false,
           outreach_subject:'S', outreach_message:'B', linkedin_message:'N', email_status:'verified'};
const emailTab = F.draftBlock(c, true), liTab = F.draftBlock(c, false, true);
const payload = (html, keys) => {
  const d = fakeCard(html), out = {};
  for (const [k, sel] of keys) out[k] = F.fieldVal(d, sel);
  return JSON.parse(JSON.stringify(out));   // drops undefined, exactly like fetch's JSON.stringify
};
console.log(JSON.stringify({
  emailTabHasLinkedinField: emailTab.includes('d-linkedin'),
  liTabHasSubjectField: liTab.includes('d-subj'),
  emailSave: payload(emailTab, [['subject','.d-subj'],['body','.d-body'],['linkedin','.d-linkedin']]),
  liSave:    payload(liTab,    [['subject','.d-subj'],['body','.d-body'],['linkedin','.d-linkedin']]),
}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_save_reads_only_the_fields_its_own_tab_rendered(tmp_path):
    """Both Save buttons were DEAD, silently, for every contact.

    The channel tabs each render half the form: `draftBlock(c, true)` emits no `.d-linkedin`,
    `draftBlock(c, false, true)` emits no `.d-subj`/`.d-body`. Both handlers read all three with
    a bare `.value`, so each threw a TypeError on its own tab. An exception inside an `onclick`
    is swallowed by the browser — no POST, no error, the label never even reached "Saved ✓".
    `regenDraft` was hardened against exactly this and these two were missed.

    The payload assertions matter as much as the null-safety: a missing field must be ABSENT
    from the JSON, not "". `_save_or_regen_draft` writes what it is given, so sending
    `subject: ""` from the LinkedIn tab would blank the outreach email — turning a dead button
    into a destructive one.
    """
    script = tmp_path / "save.mjs"
    script.write_text(_STUBS + f"const SRC = {json.dumps(_page_js())};\n" + _SAVE_DRIVER)
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # The precondition. If these ever become true the bug is gone and this test is vacuous.
    assert out["emailTabHasLinkedinField"] is False
    assert out["liTabHasSubjectField"] is False

    assert set(out["emailSave"]) == {"subject", "body"}, (
        "the Email tab's Save sent a linkedin key it never rendered")
    assert set(out["liSave"]) == {"linkedin"}, (
        "the LinkedIn tab's Save sent subject/body it never rendered — those would overwrite "
        "the outreach email with empty strings")


def test_the_server_only_writes_fields_the_client_actually_sent(tmp_path, monkeypatch):
    """The other half of the same bug, and the dangerous half.

    `subject`/`body` used to default to "", so a Save from the LinkedIn tab (which sends
    neither) would blank the outreach subject and body. It was masked only because the client
    threw before it could POST.
    """
    import applypilot.database as database
    from applypilot import web_dashboard as wd
    from applypilot.networking import store

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: conn)

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "X", "email": "a@b.com",
                                "outreach_subject": "KEEP-SUBJECT",
                                "outreach_message": "KEEP-BODY",
                                "linkedin_message": "KEEP-NOTE"}, conn)

    wd._save_or_regen_draft({"contact_id": cid, "linkedin": "NEW-NOTE"})
    c = store.get_contact(cid, conn)
    assert c["outreach_subject"] == "KEEP-SUBJECT", "saving a LinkedIn note blanked the email"
    assert c["outreach_message"] == "KEEP-BODY"
    assert c["linkedin_message"] == "NEW-NOTE"

    wd._save_or_regen_draft({"contact_id": cid, "subject": "NEW-SUBJECT", "body": "NEW-BODY"})
    c = store.get_contact(cid, conn)
    assert c["linkedin_message"] == "NEW-NOTE", "saving the email blanked the LinkedIn note"
    assert c["outreach_subject"] == "NEW-SUBJECT"

    # An explicit empty string IS a clear — that is a real edit, not an absent field.
    wd._save_or_regen_draft({"contact_id": cid, "body": ""})
    assert store.get_contact(cid, conn)["outreach_message"] == ""


_NEXT_DRIVER = """
const F = (new Function(SRC + `; return { nextAction, contactRow, CONTACT_OPEN };`))();
const out = {};
for (const [name, j] of Object.entries(CASES)) {
  out[name] = { next: F.nextAction(j), rows: (j.contacts || []).map(c => F.contactRow(c)).join('') };
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_an_unanswered_reply_outranks_every_follow_up(tmp_path):
    """Follow-up ladders chase people who said NOTHING. A reply that nobody answered is the
    opposite case and a far worse one — the system spent Apollo credits and an email to earn
    it, then dropped it. It was live in the database when this was written: Gina Johnson at
    Salesforce replied and the dashboard's Next action still said "1 follow-up due".

    The fixture has a genuinely due follow-up, so this fails if the ranking ever flips back.
    """
    waiting = _job()
    assert waiting["followups"]["due_count"] == 1, "fixture must have a competing follow-up"

    answered = _job(url="http://j/answered", awaiting_reply=[])

    script = tmp_path / "next.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {json.dumps(_page_js())};\n"
        + f"const CASES = {json.dumps({'waiting': waiting, 'answered': answered})};\n"
        + _NEXT_DRIVER
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    nxt = out["waiting"]["next"]
    assert "Answer Victoria" in nxt, f"a waiting reply did not become the Next action: {nxt}"
    assert "follow-up" not in nxt, "a follow-up outranked a human who actually replied"
    assert "openReply(" in nxt, "the action does not open the composer"

    assert "follow-up" in out["answered"]["next"], (
        "with nothing awaiting an answer, the due follow-up must come back as Next")

    # And it must be visible on the COLLAPSED row — a state you have to expand a contact to
    # find is a state nobody sees for days, which is the failure this ticket is about.
    assert "your turn" in out["waiting"]["rows"], (
        "the collapsed contact row does not show that they are waiting on you")


_NOTE_DRIVER = """
const F = (new Function(SRC + `; NET_AVAIL = true; return { findContactsPrompt };`))();
const out = {};
for (const [name, j] of Object.entries(CASES)) out[name] = F.findContactsPrompt(j);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_a_finished_empty_search_shows_its_outcome(tmp_path):
    """/api/status has always sent `network_note`; for months no JS read it.

    So a search that ran, spent Apollo credits on 5 people and then dropped all 5 as working
    at a different company with the same name rendered the identical "No contacts yet. [Find
    contacts]" as a job nobody had ever searched. That is what made "find contacts is not
    working" undiagnosable from the UI.
    """
    note = "No contacts kept at Zello — considered 5 and dropped 5 who work elsewhere."
    cases = {
        "never_run": _job(network_note="", network_error="", network_running=False),
        "finished_empty": _job(network_note=note, network_error="", network_running=False),
        "running": _job(network_note="searching…", network_error="", network_running=True),
        "errored": _job(network_note="error", network_error="No usable provider",
                        network_running=False),
    }
    script = tmp_path / "note.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {json.dumps(_page_js())};\n"
        + f"const CASES = {json.dumps(cases)};\n"
        + _NOTE_DRIVER
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # The whole point: a finished-but-empty run must not look like an un-run one.
    assert out["never_run"] != out["finished_empty"], \
        "a completed search that kept nobody renders identically to one that never ran"
    assert "considered 5" in out["finished_empty"], out["finished_empty"]
    assert "netnote" in out["finished_empty"]

    # A job nobody searched stays clean — no empty note div.
    assert "netnote" not in out["never_run"], out["never_run"]
    # Mid-flight, the spinner is the status; a stale note next to it contradicts it.
    assert "netnote" not in out["running"], out["running"]
    # A hard error already has its own red line; showing both says the same thing twice.
    assert "neterr" in out["errored"] and "netnote" not in out["errored"], out["errored"]


_MENU_DRIVER = """
const F = (new Function(SRC + `; return { positionRowMenu };`))();

// Minimal geometry model: a wrapper that CLIPS (overflow:hidden, like .table-wrap) and a menu
// whose rect we control, so the flip decision is exercised for real rather than assumed.
function harness(menuBottom, clipBottom, open) {
  const cls = new Set();
  const body = {
    classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c),
                 contains: (c) => cls.has(c) },
    getBoundingClientRect: () => ({ bottom: menuBottom }),
  };
  const wrap = { getBoundingClientRect: () => ({ bottom: clipBottom }) };
  const el = { open, querySelector: () => body, closest: (s) => s === '.table-wrap' ? wrap : null };
  F.positionRowMenu(el);
  return cls;
}
const out = {
  spills:      [...harness(900, 700, true)],   // menu bottom past the wrapper -> flip
  fits:        [...harness(500, 700, true)],   // room below -> stay
  exactly:     [...harness(700, 700, true)],   // flush -> stay
  closed:      [...harness(900, 700, false)],  // closed menus are never positioned
  noWrapper:   (() => { const cls = new Set();
      const body = { classList: { add: c => cls.add(c), remove: c => cls.delete(c) },
                     getBoundingClientRect: () => ({ bottom: 9999 }) };
      F.positionRowMenu({ open: true, querySelector: () => body, closest: () => null });
      return [...cls]; })(),
};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_row_menu_flips_up_when_it_would_be_clipped(tmp_path):
    """The ⋯ menu was rendered cut in half ("✕ Ma", "🗑 De").

    `.table-wrap` uses overflow:hidden to round the table's corners, so an absolutely
    positioned menu inside it is CLIPPED, never scrolled to. CSS fixes the horizontal side by
    anchoring the panel right instead of left; the bottom edge cannot be expressed in CSS
    (whether a row is the last one is runtime geometry), so it is measured. This drives that
    measurement with a real clipping wrapper.
    """
    script = tmp_path / "menu.mjs"
    script.write_text(_STUBS + f"const SRC = {json.dumps(_page_js())};\n" + _MENU_DRIVER)
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["spills"] == ["flip-up"], "a menu past the clip edge was left cut off"
    assert out["fits"] == [], "flipped a menu that had room below"
    assert out["exactly"] == [], "flipped on an exact fit"
    assert out["closed"] == [], "positioned a closed menu"
    assert out["noWrapper"] == [], "flipped with no clipping ancestor to flip inside"


def test_the_row_menu_opens_inward_not_off_the_edge():
    """Pins the CSS half of the fix: `left:0` is what pushed the panel past the clip edge.

    A geometry test cannot catch this — the browser clips it silently — so the anchor itself is
    the assertion.
    """
    css = (web_dashboard._STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    body = [ln for ln in css.splitlines() if ".rowmenu-body {" in ln]
    assert body, ".rowmenu-body rule not found"
    assert "right:0" in body[0].replace(" ", ""), f"menu is not right-anchored: {body[0]}"
    assert "left:0" not in body[0].replace(" ", ""), f"still opens rightward: {body[0]}"
    assert any("flip-up" in ln and "bottom:24px" in ln.replace(" ", "")
               for ln in css.splitlines()), "no .flip-up rule to flip the menu upward"
