"""SPACE-3: a target is a company you state, not an employer recovered from a URL.

The jobs pipeline spends 2,000 lines reverse-engineering an employer out of an ATS hostname and
still produced "Ouryahoo", "Edu", "Ats", "Hr" and "Uploaded" (§Lessons 20, 49, 52). None of that
runs here — the operator types the company — so the tests that matter are about the two places
this shape CAN still go wrong: the anchor (hashed into every `contact_id`, so it must be stable
and space-scoped) and the import reporting what it dropped.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import applypilot.database as database
from applypilot.domain import checklist as cl
from applypilot.domain import target as t
from applypilot import web_dashboard as wd
from applypilot.networking import store
from applypilot.repo import jobs as repo
from applypilot.repo import spaces

from browser_stubs import BROWSER_GLOBALS


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    spaces.create_space("partnerships", "Partnerships", "outreach", conn=conn)
    return conn


# ── the slug and the anchor ─────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Ridgeline Logistics", "ridgeline-logistics"),
    ("Ridgeline Logistics, Inc.", "ridgeline-logistics"),
    ("  Acme   Labs  ", "acme-labs"),
    ("Johnson & Johnson", "johnson-and-johnson"),
    ("37signals", "37signals"),
])
def test_a_company_slugs_to_something_readable(name, expected):
    assert t.slug(name) == expected


def test_the_slug_keeps_the_whole_name():
    """Deliberately not `domain/deck.slugify`, which takes the first word only.

    That function is naming a PERSON for a link they will read ("/intro/gina"). A company
    slugged that way is "ridgeline" for every Ridgeline the operator ever adds — and a collision
    here is not cosmetic, it is two companies sharing one row and one set of contacts.
    """
    from applypilot.domain import deck
    assert deck.slugify("Ridgeline Logistics") == "ridgeline"
    assert t.slug("Ridgeline Logistics") != deck.slugify("Ridgeline Logistics")


def test_a_dotted_legal_form_is_left_alone():
    """"Nestlé S.A." slugs to `nestle-s-a`, and that is the right answer.

    Collapsing it to `nestle` means stripping trailing single letters, which is trimming
    affixes blind — the move that turned OurCrowd into "Crowd" (§Lessons 52) inside the
    function written to respect §Lessons 1. The anchor is an internal key, not a link a
    recruiter reads: stable and unique beats pretty, and `_SUFFIXES` only ever removes a WHOLE
    word it recognises.
    """
    assert t.slug("Nestlé S.A.") == "nestle-s-a"
    assert t.slug("Nestlé S.A.") == t.slug("Nestle S.A."), "the fold must be deterministic"


def test_a_suffix_is_only_stripped_from_the_end():
    """"Inc" is noise in "Ridgeline Logistics Inc" and load-bearing in "Inc Magazine".

    §Lessons 52's rule, one shape over: trimming affixes blind is what turned OurCrowd into
    "Crowd" inside the function written to respect §Lessons 1.
    """
    assert t.slug("Inc Magazine") == "inc-magazine"
    assert t.slug("Co-operative Group") == "co-operative-group"
    assert t.slug("Ltd") == "ltd", "a name that is ONLY a suffix must not slug to nothing"


def test_the_anchor_is_space_scoped():
    """The same company in two Spaces is deliberately two rows (`spaces-prd.md` §5).

    The pitch, the identity sending it and the conversation all differ. Deduping them is
    `crm-prd.md`'s person-as-root job, not this one.
    """
    a = t.anchor("partnerships", "Acme Labs")
    b = t.anchor("acme", "Acme Labs")
    assert a == "target:partnerships:acme-labs" and a != b
    assert t.parse_anchor(a) == ("partnerships", "acme-labs")


@pytest.mark.parametrize("bad", [
    "https://boards.greenhouse.io/acme/jobs/123",   # a job URL is not an anchor
    "target:acme",                                  # no slug
    "target::acme-labs",                            # no space
    "targeting:acme:labs",                          # §Lessons 1: not a prefix match
    "", None,
])
def test_parse_anchor_refuses_what_is_not_one(bad):
    assert t.parse_anchor(bad) is None
    assert t.is_target(bad) is False


# ── parsing what a person actually pastes ───────────────────────────────────

@pytest.mark.parametrize("line,name,domain", [
    ("Ridgeline Logistics", "Ridgeline Logistics", ""),
    ("Ridgeline Logistics, ridgeline.com", "Ridgeline Logistics", "ridgeline.com"),
    ("Ridgeline Logistics — https://www.ridgeline.com/about", "Ridgeline Logistics", "ridgeline.com"),
    ("ridgeline.com", "Ridgeline", "ridgeline.com"),
])
def test_a_typed_line_becomes_a_target(line, name, domain):
    assert t.parse_line(line) == {"name": name, "domain": domain}


def test_a_paste_reports_what_it_could_not_read():
    """§Lessons 15. Twelve lines in, nine rows out, and no way to see which three are missing.

    "0 imported" and "9 of 12 imported" have to read differently, and the rejects come BACK
    rather than being dropped — the operator cannot retype what they can no longer see.
    """
    good, bad = t.parse_input("Acme Labs\n???\nRidgeline Logistics\n   \n***")
    assert [g["name"] for g in good] == ["Acme Labs", "Ridgeline Logistics"]
    assert bad == ["???", "***"]


def test_one_company_written_two_ways_is_one_target():
    good, _ = t.parse_input("Acme Labs\nAcme Labs, Inc.\nacme labs")
    assert len(good) == 1, "the same company imported three times would be one row, three times"


# ── the row ─────────────────────────────────────────────────────────────────

def test_adding_a_target_is_idempotent(db):
    first = repo.add_target("partnerships", "Ridgeline Logistics", "ridgeline.com", db)
    second = repo.add_target("partnerships", "Ridgeline Logistics, Inc.", "", db)
    assert first["added"] is True and second["added"] is False
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert second["url"] == first["url"]


def test_a_target_is_not_owed_a_scrape(db):
    """`detail_scraped_at` is stamped at creation.

    There is no page to fetch — the operator stated the company — and a NULL here would put the
    row in `queue_needing_detail` forever, so a target would render exactly like a job that
    failed to enrich.
    """
    out = repo.add_target("partnerships", "Ridgeline Logistics", conn=db)
    row = db.execute("SELECT detail_scraped_at, strategy, space_id FROM jobs WHERE url=?",
                     (out["url"],)).fetchone()
    assert row["detail_scraped_at"]
    assert row["strategy"] == "dashboard_upload", "a private strategy would hide it from delete"
    assert row["space_id"] == "partnerships"
    assert out["url"] not in [r["url"] for r in repo.queue_needing_detail(conn=db)]


def test_a_target_can_be_deleted(db):
    """The reason it keeps `dashboard_upload`.

    `delete_job` is `DELETE ... AND {QUEUE_SQL}`, so a target given its own strategy value
    would be a row you could create and never remove.
    """
    out = repo.add_target("partnerships", "Ridgeline Logistics", conn=db)
    assert repo.queued_for_delete(out["url"], db) is not None
    assert repo.delete(out["url"], db) == 1


def test_a_name_that_yields_no_slug_is_refused(db):
    with pytest.raises(ValueError):
        repo.add_target("partnerships", "???", conn=db)


# ── the checklist reads differently ─────────────────────────────────────────

def test_a_target_has_no_applied_step():
    """OMITTED, not marked `na`, and the distinction is the point.

    `na` means "this step has no work in it" and the strip still draws it, greyed, as something
    that could have happened. There is no application to submit to a company you are pitching.
    """
    keys = [s["key"] for s in cl.job_checklist("imported", "", [], shape="pipeline/targets")["steps"]]
    assert "applied" not in keys
    assert "contacts" in keys and "emailed" in keys and "followup" in keys


def test_a_job_still_has_one():
    assert "applied" in [s["key"] for s in cl.job_checklist("imported", "", [])["steps"]]


def test_a_target_with_nobody_emailed_is_not_already_complete():
    """The percentage must still be reachable AND not free.

    Dropping a step from the denominator is exactly how a checklist starts reporting 100% for
    work nobody did — §Lessons 35, where the Interactions tab counted our own LinkedIn invite
    as engagement and every job read "3/3 engaged".
    """
    c = [{"email": "a@x.com", "emailed": False}]
    out = cl.job_checklist("imported", "", c, shape="pipeline/targets")
    assert out["complete"] is False and out["pct"] < 100


# ── the endpoints ───────────────────────────────────────────────────────────

def test_the_import_endpoint_adds_and_reports(db):
    r = wd._add_targets({"space": "partnerships",
                         "text": "Acme Labs\nRidgeline Logistics, ridgeline.com\n???"})
    assert r["ok"] and r["added"] == 2
    assert r["rejected"] == ["???"]
    assert "not understood" in r["message"]
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_targets_are_refused_in_a_jobs_space(db):
    """Rather than writing a row into a panel that cannot show it.

    The Space's shape decides, and it decides at the WRITE — a row created here would be
    filtered out by `dashboard_rows` and look like the button did nothing.
    """
    r = wd._add_targets({"space": "job-search", "text": "Acme Labs"})
    assert r["ok"] is False and "outreach Space" in r["message"]
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_an_empty_paste_says_so_rather_than_succeeding(db):
    assert wd._add_targets({"space": "partnerships", "text": "   "})["ok"] is False


def test_the_offer_round_trips(db):
    r = wd._save_offer({"space": "partnerships",
                        "offer": "I build autonomous agent systems."})
    assert r["ok"]
    assert spaces.load("partnerships", db).offer == "I build autonomous agent systems."
    assert wd._status_payload("partnerships")["space_offer"] == "I build autonomous agent systems."


def test_saving_the_offer_cannot_move_the_shape(db):
    """`save()` leaves `id` and `shape` out of the UPDATE entirely (§13.2).

    This is the call site that would have re-keyed a Space by accident, so it is asserted here
    and not only against the repository.
    """
    wd._save_offer({"space": "partnerships", "offer": "x"})
    assert spaces.load("partnerships", db).shape == spaces.TARGETS_SHAPE


def test_the_payload_carries_the_shape_on_every_row(db):
    repo.add_target("partnerships", "Ridgeline Logistics", conn=db)
    p = wd._status_payload("partnerships")
    assert p["space_shape"] == spaces.TARGETS_SHAPE
    assert p["space_terminal"] == "booked"
    assert [j["shape"] for j in p["jobs"]] == [spaces.TARGETS_SHAPE]
    assert "applied" not in [s["key"] for s in p["jobs"][0]["checklist"]["steps"]]


def test_a_space_with_a_corrupt_shape_does_not_take_the_dashboard_down(db):
    """A hand-edited row must degrade, not 500.

    The opposite of §Lessons 47, deliberately: a MISSING column should crash, because the
    payload needs it and a default hides the bug for two rounds. A malformed VALUE in a
    registry the operator can edit is a different case — falling back to job-shaped is what
    every Space was before this feature.
    """
    db.execute("UPDATE spaces SET shape='pipeline/people' WHERE id='partnerships'")
    db.commit()
    p = wd._status_payload("partnerships")
    assert p["space_shape"] == spaces.JOBS_SHAPE


# ── the browser half ────────────────────────────────────────────────────────

_DRIVER = """
const F = (new Function(SRC + `; return { renderSpaceShape, isTargetRow, restartButton,
  signinButton, interviewButton, nextAction, stepStrip };`))();
const out = {};
const jobs = document.getElementById('jobControls');
const tgt = document.getElementById('targetControls');
const offer = document.getElementById('offerInput');

F.renderSpaceShape('pipeline/jobs', '');
out.jobsHiddenInJobs = jobs.hidden; out.tgtHiddenInJobs = tgt.hidden;

F.renderSpaceShape('pipeline/targets', 'We build agents.');
out.jobsHiddenInTargets = jobs.hidden; out.tgtHiddenInTargets = tgt.hidden;
out.offerValue = offer.value;

// The refresh must not overwrite the box while it is being typed in.
document.activeElement = offer;
offer.value = 'half a sentence';
F.renderSpaceShape('pipeline/targets', 'We build agents.');
out.offerWhileTyping = offer.value;
document.activeElement = null;

const target = {url:'target:p:acme', shape:'pipeline/targets', terminal:'booked',
                status:'imported', contacts:[], checklist:{steps:[]}};
const job = {url:'http://j/1', shape:'pipeline/jobs', terminal:'interview',
             status:'applied', contacts:[], checklist:{steps:[]}};
// A jobs-shaped Space that sets terminal deliberately. The proxy this replaced — inferring
// the word from the shape — gets this one wrong, which is the whole reason for the field.
out.jobsShapedBooked = F.interviewButton(
  {url:'http://j/2', shape:'pipeline/jobs', terminal:'booked', status:'imported',
   contacts:[], checklist:{steps:[]}});
out.targetRestart = F.restartButton(target);
out.jobRestart = F.restartButton(job);
out.targetSignin = F.signinButton(target);
out.targetWon = F.interviewButton(target);
out.jobWon = F.interviewButton(job);
out.targetStrip = F.stepStrip(target);
console.log(JSON.stringify(out));
"""


def _run_js(tmp_path, driver: str) -> dict:
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "targets.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){},
  scrollIntoView(){}, classList:{toggle(){},add(){},remove(){}},
  addEventListener(){}, appendChild(){}, dataset:{} });
// REAL nodes for the three this file asserts on, so `hidden` and `value` are observable.
// An el() that swallows every write passes with the toggling deleted (§Lessons 41).
const NODES = { jobControls: el(), targetControls: el(), offerInput: el() };
globalThis.document = { getElementById: (id) => NODES[id] || el(),
  querySelectorAll: ()=>[], querySelector: el, addEventListener(){},
  activeElement:null, body: el(), hasFocus: () => false };
const SRC = """ + json.dumps(src) + ";\n" + driver, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_console_swaps_wholesale_for_the_shape(tmp_path):
    """Not greyed-out buttons. "Prepare Materials" is not unavailable in a targets Space, it is
    meaningless there, and a disabled control asserts an action exists (§Lessons 43)."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["jobsHiddenInJobs"] is False and out["tgtHiddenInJobs"] is True
    assert out["jobsHiddenInTargets"] is True and out["tgtHiddenInTargets"] is False


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_offer_box_is_not_rewritten_mid_sentence(tmp_path):
    """The 2.5s refresh runs while you are typing a paragraph into it.

    The jobs table already skips its rewrite for exactly this reason; a textarea outside that
    subtree needs its own guard or the refresh eats the sentence.
    """
    out = _run_js(tmp_path, _DRIVER)
    assert out["offerValue"] == "We build agents."
    assert out["offerWhileTyping"] == "half a sentence", "the refresh overwrote what was typed"


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_apply_shaped_controls_are_absent_on_a_target(tmp_path):
    out = _run_js(tmp_path, _DRIVER)
    assert out["targetRestart"] == "", "a target offered 🔄 Re-apply"
    assert out["targetSignin"] == "", "a target offered 🔐 Sign in first"
    assert out["jobRestart"] != "", "the control vanished for jobs too — that is not the fix"
    assert "Re-apply" not in out["targetStrip"] and "Sign in" not in out["targetStrip"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_success_is_named_by_the_shape(tmp_path):
    """Same column, same write, same halting of every sequence — only the word changes."""
    out = _run_js(tmp_path, _DRIVER)
    assert "Call booked" in out["targetWon"] and "📞" in out["targetWon"]
    assert "Interview" in out["jobWon"] and "🎯" in out["jobWon"]
    assert "Call booked" in out["jobsShapedBooked"], \
        "the label is inferred from the shape, so a jobs Space with terminal='booked' is wrong"


def test_the_accounts_banner_belongs_to_the_jobs_shape(db):
    """"4 employers need an account before their jobs can run" is about jobs, not targets.

    Found by looking at the rendered panel, not by a test: the payload was perfectly correct and
    describing a different room. A sign-in wall is a fact about submitting an application.
    """
    assert wd._status_payload("partnerships")["accounts"] == {}
    assert isinstance(wd._status_payload("job-search")["accounts"], dict)


def test_hidden_actually_hides():
    """`hidden` is a USER-AGENT rule, so any author `display` beats it.

    `.controls{display:grid}` did exactly that, and the jobs console stayed on screen in a
    targets Space with `hidden` correctly set — the Node test asserted the PROPERTY and passed,
    because the property was set and did nothing. §Lessons 41's shape: assert the effect, not
    the assignment.

    Checked against the stylesheet because the render harness uses hand-built element stubs
    with no cascade at all; the one thing it structurally cannot see is a CSS override.
    """
    import re
    css = (wd._STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", css), \
        "nothing in the stylesheet makes [hidden] beat an author display rule"
