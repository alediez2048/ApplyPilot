"""CTX-2 — what the operator knows about ONE job, and what they want from its people.

Everything the drafting prompt knew about a job arrived from `role_essentials(full_description)`
— a scrape. There was nowhere to put the fact that the operator met the Head of Engineering at
a meetup, or that what they want here is an introduction rather than a call.

The targets shape has had this tier since SPACE-3 (`_pitch_user_prompt` reads `full_description`
as "what the operator pasted about the company"); on the jobs shape that column holds the
posting. The asymmetry was the bug.

Two fields, not four and not one. Three of the four things the operator named land in the same
paragraph; the fourth changes the ASK, which is decided by the scheduling and deck blocks — so
it needs its own slot to REPLACE them from (§Lessons 40).
"""

from __future__ import annotations

import json
import subprocess

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.domain import space as sp
from applypilot.networking import outreach, store
from applypilot.repo import jobs as repo

from test_targets import BROWSER_GLOBALS

PROFILE = {"personal": {"full_name": "Alejandro Diez", "intro_deck_url": "",
                        "scheduling_link": "https://cal.test/me"}}
BASE_JOB = {"url": "http://j/1", "title": "Applied AI Engineer", "company": "Acme",
            "site": "Greenhouse", "full_description": "You will build agent pipelines end to end.",
            "space_id": "job-search"}
CONTACT = {"id": "c1", "full_name": "Sarah Chen", "title": "Head of Engineering",
           "company": "Acme", "email": "s@acme.test", "match_reason": "works at the company"}

CONTEXT = "Their intake team is rebuilding on agents after the Q2 reorg; I met Dana at PyCon."
ASK = "An introduction to whoever owns the intake rebuild."

CTX_HEADING = "WHAT THE SENDER KNOWS ABOUT THIS COMPANY AND ROLE"
ASK_HEADING = "WHAT THE SENDER WANTS FROM THIS PERSON"
DEFAULT_CTA = "SCHEDULING LINK (include in the EMAIL CTA so they can book a call directly)"


def _prompt(space=None, **jobfields):
    """The user prompt `draft_email` would send, without calling an LLM."""
    import applypilot.networking.outreach as o
    job = dict(BASE_JOB, **jobfields)
    captured = {}

    class _Client:
        def chat(self, messages, **_):
            captured["user"] = messages[1]["content"]
            return '{"subject":"s","body":"b","linkedin_note":"n"}'

    real = o.get_client
    o.get_client = lambda *_a, **_k: _Client()
    try:
        o.draft_email(PROFILE, job, CONTACT, space=space)
    finally:
        o.get_client = real
    return captured["user"]


# ── the context reaches the prompt, and only when there is one ──────────────

def test_the_context_reaches_the_draft():
    assert CONTEXT.strip(), "an empty fixture makes every assertion below vacuous"
    got = _prompt(job_context=CONTEXT)
    assert CTX_HEADING in got
    assert CONTEXT in got


def test_no_context_adds_nothing():
    """The additive guarantee. Asserted on the HEADING — `"" in prompt` is True for every
    string, which is §Lessons 71 and shipped three times in one session."""
    assert CTX_HEADING not in _prompt()
    assert CTX_HEADING not in _prompt(job_context="")
    assert CTX_HEADING not in _prompt(job_context="  \n ")


def test_the_context_outranks_the_posting_in_the_prompt_order():
    """It is the operator's knowledge against a scrape, and it must be read as such."""
    got = _prompt(job_context=CONTEXT)
    assert got.index("WHAT THE ROLE ACTUALLY INVOLVES") < got.index(CTX_HEADING)
    assert CTX_HEADING in got and got.index(CTX_HEADING) < got.index("Write the outreach email")


def test_the_context_is_told_it_is_facts_and_not_phrasing():
    """The structural risk: one paragraph, every contact at the company. §Lessons 42 fired on a
    single quoted phrasing landing in 5 of 5 drafts, and this is shared BY CONSTRUCTION."""
    block = _prompt(job_context=CONTEXT)
    block = block[block.index(CTX_HEADING):block.index("Write the outreach email")]
    assert "facts" in block.lower() and "phrasing" in block.lower()
    assert "your own words" in block.lower()


def test_the_context_is_not_the_premise():
    """Per-ROW against per-SPACE. Rendering one from the other passes every other test here."""
    space = sp.from_template("job-search", "Job Search", "jobs", offer="Ten weeks of agents.")
    both = _prompt(space=space, job_context=CONTEXT)
    assert CTX_HEADING in both and "THE PREMISE OF THIS CAMPAIGN" in both
    assert both.index(CTX_HEADING) < both.index("THE PREMISE OF THIS CAMPAIGN")

    assert CTX_HEADING not in _prompt(space=space)
    assert "THE PREMISE OF THIS CAMPAIGN" not in _prompt(job_context=CONTEXT)


# ── the ask REPLACES the CTA ────────────────────────────────────────────────

def test_the_ask_replaces_the_default_call_booking():
    """Not appended to it. A prompt carrying both produces an email that does both, badly —
    §Lessons 40, where appending never resolved the contradiction and the fix was to replace."""
    got = _prompt(job_ask=ASK)
    assert ASK_HEADING in got and ASK in got
    assert DEFAULT_CTA not in got


def test_the_scheduling_link_survives_a_custom_ask():
    """Replacing the FRAMING must not throw away the calendar. What the operator overrides is
    what to ask for, not whether a link exists."""
    got = _prompt(job_ask=ASK)
    assert "https://cal.test/me" in got


def test_the_ask_forbids_asking_for_two_things():
    got = _prompt(job_ask=ASK)
    block = got[got.index(ASK_HEADING):]
    assert "two things" in block.lower() or "one request" in block.lower()


def test_no_ask_leaves_the_default_cta_exactly_as_it_was():
    """A row with no ask must behave precisely as it did before CTX-2."""
    assert DEFAULT_CTA in _prompt()
    assert ASK_HEADING not in _prompt()
    assert ASK_HEADING not in _prompt(job_ask="   ")


def test_context_and_ask_are_independent():
    assert CTX_HEADING in _prompt(job_context=CONTEXT) and ASK_HEADING not in _prompt(job_context=CONTEXT)
    assert ASK_HEADING in _prompt(job_ask=ASK) and CTX_HEADING not in _prompt(job_ask=ASK)


# ── measurability ───────────────────────────────────────────────────────────

def test_draft_variant_records_both():
    """`ctx` and `ask` VARY across rows of one Space, unlike `premise` — so these are the first
    inputs that can be compared inside a single campaign rather than only before/after."""
    assert "ctx" in outreach.draft_variant(ctx=True).split("+")
    assert "ask" in outreach.draft_variant(ask=True).split("+")
    assert "ctx" not in outreach.draft_variant().split("+")
    assert "ask" not in outreach.draft_variant().split("+")


def test_the_variant_on_a_real_draft_reports_what_went_in(db):
    import applypilot.networking.outreach as o
    repo.set_context("http://j/1", context=CONTEXT, ask=ASK, conn=db)
    job = dict(BASE_JOB, job_context=CONTEXT, job_ask=ASK)

    class _Client:
        def chat(self, *_a, **_k):
            return '{"subject":"s","body":"b","linkedin_note":"n"}'
    real = o.get_client
    o.get_client = lambda *_a, **_k: _Client()
    try:
        out = o.draft_email(PROFILE, job, CONTACT)
    finally:
        o.get_client = real
    bits = out["variant"].split("+")
    assert "ctx" in bits and "ask" in bits


# ── storage ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    conn.execute("INSERT INTO jobs (url, title, company, site, strategy, tailored_resume_path, "
                 "space_id) VALUES (?,?,?,?,?,?,?)",
                 ("http://j/1", "Applied AI Engineer", "Acme", "Greenhouse",
                  "dashboard_upload", "/tmp/r.pdf", "job-search"))
    conn.commit()
    return conn


def _col(db, name):
    return db.execute(f"SELECT {name} FROM jobs WHERE url='http://j/1'").fetchone()[0]


def test_both_columns_are_additive_not_a_migration():
    """They must be in the dicts. A migration touching a declared column RACES the additive
    pass — `get_connection()` does not call `init_db`, so which runs first depends on the entry
    point, and it is a duplicate-column error one way round."""
    assert "job_context" in database._ALL_COLUMNS
    assert "job_ask" in database._ALL_COLUMNS
    import pathlib
    for f in (pathlib.Path(__file__).parent.parent / "src/applypilot/migrations").glob("m*.py"):
        src = f.read_text()
        assert "job_context" not in src and "job_ask" not in src, f"{f.name} touches a declared column"


def test_a_field_the_caller_did_not_show_is_not_cleared(db):
    """A missing key means "this caller did not render that box", never "the operator cleared
    it". `_save_draft` already carries this bug's scar: defaulting an absent field to "" had the
    LinkedIn tab silently blanking the outreach email."""
    repo.set_context("http://j/1", context=CONTEXT, ask=ASK, conn=db)

    repo.set_context("http://j/1", context="new context", conn=db)
    assert _col(db, "job_ask") == ASK, "an unshown ask was cleared"

    # BOTH directions. Testing one side leaves the other's branch unexercised — a mutation
    # making the context branch fire on None survived until this line existed (§Lessons 49's
    # shape, inside the test written to guard the behaviour).
    repo.set_context("http://j/1", ask="new ask", conn=db)
    assert _col(db, "job_context") == "new context", "an unshown context was cleared"


def test_an_empty_string_does_clear(db):
    repo.set_context("http://j/1", context=CONTEXT, conn=db)
    repo.set_context("http://j/1", context="", conn=db)
    assert _col(db, "job_context") == ""


def test_both_are_capped_at_the_write(db):
    repo.set_context("http://j/1", context="c" * 5000, ask="a" * 5000, conn=db)
    assert len(_col(db, "job_context")) == repo.CONTEXT_MAX
    assert len(_col(db, "job_ask")) == repo.ASK_MAX


def test_the_columns_reach_the_browser(db):
    """No `if "col" in row.keys()` guard anywhere on the path. That defensive read is what hid
    `interview_at` from the payload for two rounds while the write worked perfectly
    (§Lessons 47) — a column the payload needs belongs in the SELECT, and its absence must crash.
    """
    repo.set_context("http://j/1", context=CONTEXT, ask=ASK, conn=db)
    row = [j for j in wd._status_payload("job-search")["jobs"] if j["url"] == "http://j/1"][0]
    assert row["job_context"] == CONTEXT
    assert row["job_ask"] == ASK


# ── the endpoint ────────────────────────────────────────────────────────────

def test_the_endpoint_stores_and_reports(db):
    out = wd._save_job_context({"job_url": "http://j/1", "context": CONTEXT, "ask": ASK})
    assert out["ok"], out
    assert out["context"] == CONTEXT and out["ask"] == ASK
    assert _col(db, "job_context") == CONTEXT


def test_an_unknown_job_is_refused(db):
    out = wd._save_job_context({"job_url": "http://nope/9", "context": CONTEXT})
    assert not out["ok"] and "job" in out["message"].lower()


def test_a_request_carrying_neither_field_is_refused(db):
    assert not wd._save_job_context({"job_url": "http://j/1"})["ok"]


def test_saving_does_not_redraft(db, monkeypatch):
    """Regenerating eight drafts the moment somebody stops typing spends real credits on a
    paragraph they may still be editing. The panel reports staleness; the operator decides."""
    called = []
    monkeypatch.setattr("applypilot.networking.service.draft_for_contact",
                        lambda *a, **k: called.append(a) or {})
    wd._save_job_context({"job_url": "http://j/1", "context": CONTEXT})
    assert not called


# ── what the panel reports about existing drafts ────────────────────────────

def _contacts(*variants, sent=()):
    out = []
    for i, v in enumerate(variants):
        out.append({"id": f"c{i}", "outreach_message": "body",
                    "draft_variant": v, "sent_message_id": "m" if i in sent else ""})
    return out


def test_it_counts_drafts_written_without_the_context():
    """Read off `draft_variant`, which records the INPUTS to a draft — no new column, and it
    cannot lie the way a timestamp could."""
    u = wd._context_use(_contacts("cold+jd2k+deck+cal", "cold+ctx+deck", "cold+jd2k"))
    assert u == {"drafts": 3, "used": 1, "stale": 2, "sent": 0}


def test_a_sent_draft_is_never_offered_for_regeneration():
    """It is the only record of what actually went out — the `deck-relink` rule."""
    u = wd._context_use(_contacts("cold+jd2k", "cold+jd2k", sent=(0,)))
    assert u["sent"] == 1
    assert u["stale"] == 1, "a sent draft was counted as regenerable"


def test_a_contact_with_no_draft_is_not_counted():
    u = wd._context_use([{"id": "c9", "outreach_message": "", "draft_variant": ""}])
    assert u["drafts"] == 0


def test_ctx_is_matched_as_a_whole_token():
    """§Lessons 1. A variant containing `ctxfoo` — or any future bit with `ctx` inside it — is
    not this one."""
    assert wd._context_use(_contacts("cold+ctxfoo+deck"))["used"] == 0
    assert wd._context_use(_contacts("cold+ctx+deck"))["used"] == 1


# ── the browser half ────────────────────────────────────────────────────────

_DRIVER = """
// `refresh` is neutralised INSIDE the scope: saveJobContext calls it unawaited, and letting the
// real one run would fetch /api/status through the stub below and throw where nothing catches.
const F = (new Function(SRC + `; refresh = async () => {};
  return { contextBox, CTX_FORM, saveJobContext };`))();
const out = {};
const J = (over) => Object.assign({url:'http://j/1', job_context:'', job_ask:'',
                                   context_use:{drafts:0,used:0,stale:0,sent:0}}, over||{});

out.emptyIsOpen = /<details class="ctx" [^>]*open/.test(F.contextBox(J()));
out.emptyHasBothBoxes = /class="ctx-what"/.test(F.contextBox(J()))
                     && /class="ctx-ask"/.test(F.contextBox(J()));

const filled = F.contextBox(J({job_context:'Met Dana at PyCon.',
                               context_use:{drafts:3,used:3,stale:0,sent:0}}));
out.filledCollapsed = !/<details class="ctx" [^>]*open/.test(filled);
out.filledSaysUsed = /used in 3 drafts/.test(filled);
out.filledCarriesValue = /Met Dana at PyCon\\./.test(filled);

const stale = F.contextBox(J({job_context:'x',
                              context_use:{drafts:3,used:0,stale:2,sent:1}}));
out.staleWarns = /ctx-stale/.test(stale) && /2 drafts/.test(stale);
out.staleMentionsSent = /1 already sent/.test(stale);

// The unsaved buffer must beat the server copy, or the 2.5s refresh silently reverts a
// paragraph mid-edit.
F.CTX_FORM.set('http://j/1', {context:'half a para', ask:''});
const dirty = F.contextBox(J({job_context:'server copy'}));
out.dirtyShowsTyped = /half a para/.test(dirty) && !/server copy/.test(dirty);
out.dirtyMarked = /unsaved/.test(dirty);
out.dirtyStaysOpen = /<details class="ctx" [^>]*open/.test(dirty);
F.CTX_FORM.delete('http://j/1');

out.escaped = F.contextBox(J({job_context:'</textarea><script>x</script>'}));

// A FAILED save must not drop the typed paragraph. Dropping it discards the work AND leaves
// the box rendering the stale server copy, so the operator loses it without being able to see
// that they did.
const btn = { disabled:false, closest: () => ({
  querySelector: (sel) => sel === '.ctx-status' ? {textContent:''}
                        : {value: sel === '.ctx-what' ? 'a whole paragraph' : ''} }) };
globalThis.alert = () => {};
globalThis.fetch = async () => ({ ok:false, json: async () => ({ok:false, message:'boom'}) });
// Seed it the way typing does — `onCtxField` fills the buffer, `saveJobContext` only reads the
// DOM. Without this the assertion passes against a buffer that was never populated.
F.CTX_FORM.set('http://j/1', {context:'a whole paragraph', ask:''});
await F.saveJobContext('http://j/1', btn);
out.keptAfterFailure = F.CTX_FORM.get('http://j/1')?.context || '';

globalThis.fetch = async () => ({ ok:true, json: async () => ({ok:true, message:'Context saved.'}) });
F.CTX_FORM.set('http://j/1', {context:'a whole paragraph', ask:''});
await F.saveJobContext('http://j/1', btn);
out.clearedAfterSuccess = !F.CTX_FORM.has('http://j/1');

console.log(JSON.stringify(out));
"""


def _run_js(tmp_path, driver: str) -> dict:
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "ctx.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', placeholder:'',
  style:{}, closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){},
  scrollIntoView(){}, classList:{toggle(){},add(){},remove(){}},
  addEventListener(){}, appendChild(){}, dataset:{} });
globalThis.document = { getElementById: () => el(), querySelectorAll: ()=>[],
  querySelector: el, addEventListener(){}, activeElement:null, body: el(),
  hasFocus: () => false };
const SRC = """ + json.dumps(src) + ";\n" + driver, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_an_empty_panel_shows_the_boxes_rather_than_describing_them(tmp_path):
    """§Lessons 41: the SMS tab rendered an accurate sentence about a control instead of the
    control, and was reported TWICE as missing by somebody looking straight at it. An empty
    context panel must be open with both textareas in it."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["emptyIsOpen"] is True
    assert out["emptyHasBothBoxes"] is True


def test_a_filled_panel_collapses_and_says_it_is_in_play(tmp_path):
    out = _run_js(tmp_path, _DRIVER)
    assert out["filledCollapsed"] is True
    assert out["filledSaysUsed"] is True
    assert out["filledCarriesValue"] is True


def test_drafts_that_predate_the_context_are_flagged(tmp_path):
    out = _run_js(tmp_path, _DRIVER)
    assert out["staleWarns"] is True
    assert out["staleMentionsSent"] is True


def test_the_refresh_cannot_revert_a_paragraph_being_typed(tmp_path):
    """`isEditingJobs()` only holds off while a field HAS focus, so clicking from the textarea
    to anything that is not an input hands the next tick a paragraph to destroy. The typed value
    lives in CTX_FORM for the same reason ADD_FORM exists."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["dirtyShowsTyped"] is True
    assert out["dirtyMarked"] is True
    assert out["dirtyStaysOpen"] is True


def test_the_context_is_escaped_into_the_textarea(tmp_path):
    out = _run_js(tmp_path, _DRIVER)
    assert "</textarea>" not in out["escaped"].split("ctx-what")[1][:200]
    assert "<script>" not in out["escaped"]


def test_a_failed_save_keeps_the_paragraph(tmp_path):
    """The buffer is cleared on SUCCESS only. Clearing it unconditionally loses the work and
    re-renders the stale server copy, which looks like the save worked."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["keptAfterFailure"] == "a whole paragraph"


def test_a_successful_save_lets_go_of_the_buffer(tmp_path):
    """Guard the guard: keeping it forever would pin the box to a stale copy of itself and the
    test above would still pass."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["clearedAfterSuccess"] is True


# ── the read path that made the whole feature a no-op ───────────────────────

def test_the_drafting_job_dict_carries_the_operator_boxes(db):
    """CTX-2 shipped with its columns missing from the SELECT the drafter is fed.

    `_network._run` — the Find-contacts button — builds its job dict from `find_by_any_url`,
    which listed seven columns and neither of these. So the operator could type context onto a
    row, click Find contacts, and every draft came back written without it, while REGENERATING a
    draft went through `get()`'s `SELECT *` and read it perfectly. The feature worked on the
    second path anybody takes and not the first.

    Two days live and invisible, for the reason §Lessons 47 gives: the WRITE side is correct, the
    read is silent, and what arrives is a plausible empty string rather than an error. Nobody hit
    it because no row has carried a context yet — which is the only thing that kept it cheap.
    """
    repo.set_context("http://j/1", context=CONTEXT, ask=ASK, conn=db)
    row = repo.find_by_any_url("http://j/1", db)
    assert row["job_context"] == CONTEXT
    assert row["job_ask"] == ASK


def test_that_dict_still_carries_everything_else_drafting_reads(db):
    """Guard the guard. The test above passes with the rest of the SELECT deleted, and every
    column here is one a prompt reads: `space_id` picks WHICH prompt, `url` and
    `application_url` become the posting link, `full_description` is the role."""
    row = repo.find_by_any_url("http://j/1", db)
    for column in ("url", "title", "company", "site", "application_url",
                   "full_description", "space_id"):
        assert column in row, f"{column} dropped out of the drafting SELECT"
