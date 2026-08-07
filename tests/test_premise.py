"""CTX-1 — the campaign premise reaches a jobs draft.

`Space.offer` was declared, documented for exactly this case, and handed only to
`_pitch_user_prompt`. The jobs branch never received it, so `job-search` and `gauntlet` — both
`pipeline/jobs`, 30 rows and 1 — wrote the same email as each other. The only Space-level lever
that reached a jobs draft was `tone`, and tone is voice, not substance.

`UNAPPLIED` was empty the whole time and `test_unapplied_fields_are_really_unapplied` was
honest: it asks whether a field is read ANYWHERE, and the answer was yes, on one of two shapes.
§Lessons 49 — a rule implemented at one of its two call sites is not implemented.

The second half of the ticket is that there was nowhere to TYPE one: `#offerInput` lived inside
`#targetControls`, which is `hidden` on a jobs Space. Wiring the prompt without moving the
control ships a field nobody can fill (§Lessons 43, six occurrences, every one reported as "it
does nothing").
"""

from __future__ import annotations

import json
import subprocess

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.domain import space as sp
from applypilot.networking import outreach, store
from applypilot.repo import spaces

from test_targets import BROWSER_GLOBALS

PROFILE = {"personal": {"full_name": "Alejandro Diez", "intro_deck_url": ""}}
JOB = {"url": "http://j/1", "title": "Applied AI Engineer", "company": "Acme",
       "site": "Greenhouse", "full_description": "You will build agent pipelines end to end.",
       "space_id": "job-search"}
CONTACT = {"id": "c1", "full_name": "Sarah Chen", "title": "Head of Engineering",
           "company": "Acme", "email": "s@acme.test", "match_reason": "works at the company"}

#: The heading the block renders under. Asserted on rather than on the premise text itself,
#: because a premise the operator happens to word like the posting would make a text-only
#: assertion pass for the wrong reason.
HEADING = "THE PREMISE OF THIS CAMPAIGN"

GAUNTLET = "Ten weeks of building agents full time, and I am looking for where that goes next."
SEARCH = "Fifteen years shipping operations systems, now pointed at applied AI."


def _prompt(job, contact, space, **kw):
    """The user prompt `draft_email` would send, without calling an LLM."""
    import applypilot.networking.outreach as o

    captured = {}

    class _Client:
        def chat(self, messages, **_):
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return '{"subject":"s","body":"b","linkedin_note":"n"}'

    real = o.get_client
    o.get_client = lambda *_a, **_k: _Client()
    try:
        o.draft_email(PROFILE, job, contact, space=space, **kw)
    finally:
        o.get_client = real
    return captured


def _jobs_space(space_id="job-search", name="Job Search", **kw):
    return sp.from_template(space_id, name, "jobs", **kw)


# ── the objective ───────────────────────────────────────────────────────────

def test_two_jobs_spaces_produce_different_prompts():
    """The whole ticket, asserted on the ARTIFACT.

    Comparing the two prompts to each other would prove only that they differ — §Lessons 60,
    where the first version of `test_a_default_space_changes_the_prompt_by_nothing` compared
    two moving things and survived a mutation leaking a field into both paths. Each premise is
    asserted present in its own prompt and ABSENT from the other's.
    """
    assert GAUNTLET.strip() and SEARCH.strip(), "an empty premise makes every assertion below vacuous"
    assert GAUNTLET != SEARCH

    g = _prompt(JOB, CONTACT, _jobs_space("gauntlet", "Gauntlet", offer=GAUNTLET))["user"]
    s = _prompt(JOB, CONTACT, _jobs_space(offer=SEARCH))["user"]

    assert GAUNTLET in g and GAUNTLET not in s
    assert SEARCH in s and SEARCH not in g
    assert g != s


def test_the_premise_renders_under_its_own_heading():
    got = _prompt(JOB, CONTACT, _jobs_space(offer=GAUNTLET))["user"]
    assert HEADING in got
    assert got.index(HEADING) < got.index("Write the outreach email")


# ── it stays additive ───────────────────────────────────────────────────────

def test_an_empty_premise_adds_nothing():
    """The guarantee `tests/golden/jobs_outreach_prompt.txt` exists for.

    A Space with no premise must produce the byte-identical prompt it produced before CTX-1, so
    this ticket cannot have quietly changed 30 jobs' worth of outreach. Asserted on the HEADING,
    not on `offer in prompt` — `"" in prompt` is True for every string, which is §Lessons 71 and
    shipped three times in one session.
    """
    assert HEADING not in _prompt(JOB, CONTACT, _jobs_space())["user"]
    assert HEADING not in _prompt(JOB, CONTACT, None)["user"]


def test_whitespace_is_not_a_premise():
    assert HEADING not in _prompt(JOB, CONTACT, _jobs_space(offer="   \n  "))["user"]


def test_the_premise_is_not_the_tone():
    """Two different levers that both arrive as free text from the same manifest.

    Rendering one from the other would pass every test above and silently collapse voice into
    substance — the distinction the ticket exists to draw.
    """
    voice_only = _prompt(JOB, CONTACT, _jobs_space(tone="Keep it dry and short."))["user"]
    assert HEADING not in voice_only
    assert "Keep it dry and short." in voice_only

    premise_only = _prompt(JOB, CONTACT, _jobs_space(offer=GAUNTLET))["user"]
    assert "VOICE FOR THIS CAMPAIGN" not in premise_only
    assert GAUNTLET in premise_only


def test_the_premise_is_told_not_to_be_reused_verbatim():
    """The exposure here is two orders of magnitude worse than `noticed`.

    `noticed` is per PERSON, so a repeated sentence reaches one reader. This is per SPACE:
    `job-search` holds 30 jobs and 131 emailed contacts, so a premise quoted verbatim is one
    paragraph arriving in ~200 inboxes. §Lessons 42 fired on a single quoted phrasing appearing
    in 5 of 5 drafts.
    """
    got = _prompt(JOB, CONTACT, _jobs_space(offer=GAUNTLET))["user"]
    block = got[got.index(HEADING):got.index("Write the outreach email")]
    assert "never sentences to reuse" in block.lower() or "not sentences to reuse" in block.lower()
    assert "your own words" in block.lower()


def test_the_targets_offer_still_works():
    """The path that already read this field must not move."""
    target = {"url": "target:partnerships:acme", "title": "Acme", "company": "Acme",
              "site": "", "full_description": "", "space_id": "partnerships"}
    space = sp.from_template("partnerships", "Partnerships", "outreach", offer=SEARCH)
    got = _prompt(target, CONTACT, space)
    assert SEARCH in got["user"]
    assert got["system"] == outreach._PITCH_SYSTEM
    # The jobs heading must NOT leak into the pitch prompt — that shape calls it an offer.
    assert HEADING not in got["user"]


# ── measurability ───────────────────────────────────────────────────────────

def test_draft_variant_records_the_premise():
    """Constant within a Space, so it says little ACROSS a campaign — but it is the only way to
    separate drafts written before a premise existed from those written after, which is the one
    comparison that says whether writing it was worth anything."""
    assert "premise" in outreach.draft_variant(premise=True)
    assert "premise" not in outreach.draft_variant(premise=False)
    assert "premise" not in outreach.draft_variant()


# ── the label cannot drift from the field ───────────────────────────────────

def test_every_shape_has_copy_for_the_box():
    """A shape with no copy renders an unlabelled textarea, which is the §Lessons 41 failure:
    a control the operator cannot name is one they never use."""
    for shape in sp.SHAPES:
        c = sp.offer_copy(shape)
        assert c["title"].strip() and c["placeholder"].strip() and c["hint"].strip()


def test_the_two_shapes_do_not_call_it_the_same_thing():
    jobs = sp.offer_copy(sp.JOBS_SHAPE)
    targets = sp.offer_copy(sp.TARGETS_SHAPE)
    assert jobs["title"] != targets["title"]
    assert "premise" in jobs["title"].lower()
    assert "offer" in targets["title"].lower()


def test_an_unknown_shape_falls_back_to_a_label_rather_than_to_nothing():
    """The opposite of §Lessons 47's missing column: a corrupt shape already degrades to
    job-shaped elsewhere, so the copy must degrade the same way rather than render blank."""
    assert sp.offer_copy("pipeline/people") == sp.offer_copy(sp.JOBS_SHAPE)


def test_offer_copy_hands_back_a_copy():
    """It is shipped on the payload; a caller mutating it would rewrite the label for the
    process, and the next render would show whatever the last request did."""
    got = sp.offer_copy(sp.JOBS_SHAPE)
    got["title"] = "mutated"
    assert sp.offer_copy(sp.JOBS_SHAPE)["title"] != "mutated"


# ── the endpoint and the payload ────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    spaces.create_space("gauntlet", "Gauntlet", "jobs", conn=conn)
    spaces.create_space("partnerships", "Partnerships", "outreach", conn=conn)
    return conn


def test_a_jobs_space_can_be_given_a_premise(db):
    """The endpoint never had a shape guard; only the UI hid the box. Proven, not assumed —
    if this had refused, the ticket would have needed a backend half."""
    out = wd._save_offer({"space": "gauntlet", "offer": GAUNTLET})
    assert out["ok"], out
    assert spaces.load("gauntlet", db).offer == GAUNTLET


def test_the_confirmation_uses_the_label_above_the_box(db):
    """"Offer saved." is the wrong word on a jobs Space. One field, two names, and the
    confirmation has to match the heading the operator just typed under."""
    jobs = wd._save_offer({"space": "gauntlet", "offer": GAUNTLET})["message"]
    targets = wd._save_offer({"space": "partnerships", "offer": SEARCH})["message"]
    assert jobs != targets
    assert sp.offer_copy(sp.JOBS_SHAPE)["title"] in jobs
    assert sp.offer_copy(sp.TARGETS_SHAPE)["title"] in targets


def test_clearing_says_so(db):
    assert "cleared" in wd._save_offer({"space": "gauntlet", "offer": ""})["message"].lower()


def test_the_payload_carries_the_copy_for_the_space_on_screen(db):
    assert wd._status_payload("gauntlet")["space_offer_copy"] == sp.offer_copy(sp.JOBS_SHAPE)
    assert wd._status_payload("partnerships")["space_offer_copy"] == sp.offer_copy(sp.TARGETS_SHAPE)


# ── the browser half ────────────────────────────────────────────────────────

_DRIVER = """
const F = (new Function(SRC + `; return { renderSpaceShape };`))();
const out = {};
const box = document.getElementById('offerInput');
const title = document.getElementById('premiseTitle');
const hint = document.getElementById('premiseHint');
const panel = document.getElementById('premiseControls');
const COPY = {title:'The premise of this campaign', placeholder:'What you are looking for.',
              hint:'Used in <strong>every</strong> draft here.'};

F.renderSpaceShape('pipeline/jobs', 'Ten weeks of agents.', COPY);
out.panelHiddenInJobs = panel.hidden;
out.titleInJobs = title.textContent;
out.hintInJobs = hint.innerHTML;
out.placeholderInJobs = box.placeholder;
out.valueInJobs = box.value;

F.renderSpaceShape('pipeline/targets', 'We build agents.',
                   {title:'Your offer', placeholder:'What you propose.', hint:'x'});
out.panelHiddenInTargets = panel.hidden;
out.titleInTargets = title.textContent;

// The 2.5s refresh must not eat a paragraph mid-sentence.
document.activeElement = box;
box.value = 'half a sen';
F.renderSpaceShape('pipeline/jobs', 'Ten weeks of agents.', COPY);
out.valueWhileTyping = box.value;
document.activeElement = null;

// A payload without the copy must not blank the labels it cannot supply.
F.renderSpaceShape('pipeline/jobs', '', undefined);
out.titleAfterNoCopy = title.textContent;
console.log(JSON.stringify(out));
"""


def _run_js(tmp_path, driver: str) -> dict:
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "premise.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', placeholder:'',
  style:{}, closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){},
  scrollIntoView(){}, classList:{toggle(){},add(){},remove(){}},
  addEventListener(){}, appendChild(){}, dataset:{} });
// REAL nodes for everything this file asserts on. An el() that swallows every write passes
// with the whole function deleted (§Lessons 41).
const NODES = { jobControls: el(), targetControls: el(), premiseControls: el(),
                offerInput: el(), premiseTitle: el(), premiseHint: el() };
globalThis.document = { getElementById: (id) => NODES[id] || el(),
  querySelectorAll: ()=>[], querySelector: el, addEventListener(){},
  activeElement:null, body: el(), hasFocus: () => false };
const SRC = """ + json.dumps(src) + ";\n" + driver, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_premise_box_is_on_screen_in_both_shapes(tmp_path):
    """The defect that made the prompt work worthless: the box was inside `#targetControls`, so
    a jobs Space had nowhere to type one."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["panelHiddenInJobs"] is False
    assert out["panelHiddenInTargets"] is False


def test_the_box_is_labelled_by_the_shape(tmp_path):
    out = _run_js(tmp_path, _DRIVER)
    assert out["titleInJobs"] == "The premise of this campaign"
    assert out["titleInTargets"] == "Your offer"
    assert out["placeholderInJobs"] == "What you are looking for."
    assert "<strong>" in out["hintInJobs"], "the hint is rendered as markup, not escaped text"


def test_the_saved_premise_reaches_the_box(tmp_path):
    out = _run_js(tmp_path, _DRIVER)
    assert out["valueInJobs"] == "Ten weeks of agents."


def test_the_refresh_does_not_eat_a_paragraph_being_typed(tmp_path):
    out = _run_js(tmp_path, _DRIVER)
    assert out["valueWhileTyping"] == "half a sen"


def test_a_missing_copy_leaves_the_last_label_standing(tmp_path):
    """Blanking a heading because one payload arrived without it is worse than a stale word."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["titleAfterNoCopy"].strip()


def test_the_markup_no_longer_hides_the_box_behind_the_shape(tmp_path):
    """Reads the shipped HTML, not the JS. `hidden` is a user-agent rule that any author
    `display` beats (§Lessons 62), so the attribute's ABSENCE is the thing to assert."""
    html = (wd._STATIC_DIR / "index.html").read_text(encoding="utf-8")
    panel = html[html.index('id="premiseControls"'):]
    panel = panel[:panel.index("</div>")]
    assert "hidden" not in html[html.index('id="premiseControls"') - 40:html.index('id="premiseControls"') + 40]
    assert 'id="offerInput"' in panel, "the textarea left the panel that renders on both shapes"
    # And it must no longer be inside the block that disappears on a jobs Space.
    targets = html[html.index('id="targetControls"'):html.index('id="premiseControls"')]
    assert 'id="offerInput"' not in targets
