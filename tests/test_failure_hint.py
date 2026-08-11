"""The line under a failed row, in English.

It read, verbatim, on a live Google row:

    copilot_violation_agent_submitted Regenerates materials, then re-applies.

Two unrelated sentences welded together. The first half is a machine token; the second is the
Restart button's own description, appended to the error message with nothing between them. It was
reported twice as "what does this mean?", which is the correct reaction.

Two fixes, and they are different in kind:

**The button's description moved onto the button.** §Lessons 88 — a control describes itself where
it is. A description parked in a neighbouring sentence does not describe anything, it just makes
the sentence wrong.

**The code became a sentence, and the code SURVIVED as hover text.** An English translation is
better to read and strictly worse to search for; `copilot_violation_agent_submitted` is the exact
string in the apply log. Replacing it outright would fix the reading and break the debugging.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from browser_stubs import BROWSER_GLOBALS

from applypilot import web_dashboard as wd


def _hint(job, tmp_path):
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "hint.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),
  querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,
  activeElement:null };
const SRC = """ + json.dumps(src) + """;
const F = (new Function(SRC + '; return { nextHint, failWhy, FAIL_WHY, nextAction };'))();
console.log(JSON.stringify({ hint: F.nextHint(""" + json.dumps(job) + """),
                             codes: Object.keys(F.FAIL_WHY) }));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── the two sentences are no longer one ─────────────────────────────────────

def test_the_button_description_is_gone_from_the_error_line(tmp_path):
    """The actual complaint. This sentence describes what Restart DOES and had nothing to do with
    what went wrong."""
    out = _hint({"status": "failed", "apply_error": "copilot_violation_agent_submitted"}, tmp_path)
    assert "Regenerates materials" not in out["hint"]
    assert "re-applies" not in out["hint"]


def test_the_description_moved_onto_the_button():
    """Deleted from one place and not added to the other is a regression, not a fix — the operator
    then has no way at all to learn what Restart does before clicking it."""
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    fn = js[js.index("function nextAction(j)"):js.index("function nextHint(j)")]
    restart = [ln for ln in fn.splitlines() if "restartJob(" in ln]
    assert restart, "the restart button left nextAction"
    assert any("title=" in ln and "Regenerates" in ln for ln in restart), (
        "the button does not say what it does")


# ── the code became a sentence ──────────────────────────────────────────────

def test_the_copilot_violation_reads_as_english(tmp_path):
    out = _hint({"status": "failed", "apply_error": "copilot_violation_agent_submitted"}, tmp_path)
    assert "agent submitted this itself" in out["hint"].lower()
    assert "co-pilot" in out["hint"].lower()


def test_it_does_not_claim_the_application_failed_to_arrive(tmp_path):
    """The whole ambiguity. `applied_at` is empty and the agent said it submitted; the app cannot
    tell which is true and must not pick one (§Lessons 19). Saying "this was not submitted" would
    be a confident answer to a question nobody can answer, on the row where being wrong means a
    real application is recorded as a failure forever."""
    out = _hint({"status": "failed", "apply_error": "copilot_violation_agent_submitted"}, tmp_path)
    assert "may still have reached the employer" in out["hint"]
    assert "Mark as applied" in out["hint"], "no route out of the wrong state is named"


def test_the_raw_code_survives_as_hover_text(tmp_path):
    """It is what you grep the apply log for. A sentence does not replace it."""
    out = _hint({"status": "failed", "apply_error": "copilot_violation_agent_submitted"}, tmp_path)
    assert 'title="copilot_violation_agent_submitted"' in out["hint"]


def test_an_unrecognised_code_is_still_shown(tmp_path):
    """The dangerous failure mode of a translation table: a code with no entry falling back to a
    generic "it failed", which is less informative than the raw token it replaced.

    Asserted against the VISIBLE text, not the markup. The first version searched the whole
    string, which contains the code in the `title` too — so it passed against a hint whose body
    read "The last attempt failed." and said nothing. §Lessons 71: an assertion that cannot fail
    when the thing under test is emptied. Mutation caught it; reading it did not.
    """
    out = _hint({"status": "failed", "apply_error": "some_new_reason_2027"}, tmp_path)
    visible = re.sub(r'<[^>]+>', '', out["hint"])
    assert "some_new_reason_2027" in visible


def test_no_code_at_all_still_says_something(tmp_path):
    out = _hint({"status": "failed", "apply_error": ""}, tmp_path)
    assert "last attempt failed" in out["hint"].lower()


def test_a_code_is_never_left_bare(tmp_path):
    """Guard the guard: returning the code unchanged satisfies "the code is still shown" while
    changing nothing about the thing that was reported."""
    out = _hint({"status": "failed", "apply_error": "copilot_violation_agent_submitted"}, tmp_path)
    text = re.sub(r'<[^>]+>', '', out["hint"])
    assert "copilot_violation_agent_submitted" not in text, "the raw token is still on screen"


# ── the other states are untouched ──────────────────────────────────────────

@pytest.mark.parametrize("code,needle", [
    ("captcha", "Solve the captcha"),
    ("login", "Sign up or log in"),
    ("paused", "Paused"),
])
def test_needs_human_still_uses_its_own_map(code, needle, tmp_path):
    out = _hint({"status": "needs_human", "apply_error": code}, tmp_path)
    assert needle in out["hint"]


def test_a_healthy_row_gets_no_hint(tmp_path):
    assert _hint({"status": "applied", "apply_error": ""}, tmp_path)["hint"] == ""


# ── the table cannot drift from the launcher ────────────────────────────────

def test_every_failure_the_launcher_can_produce_has_a_sentence(tmp_path):
    """The table lives in the frontend and the codes are chosen in `apply/launcher.py`. Nothing
    connects them, so a new failure reason ships as a raw token on the row and nobody notices —
    which is exactly how the reported line came to exist. Read the codes out of the source.
    """
    launcher = Path(wd.__file__).parent.joinpath("apply/launcher.py").read_text(encoding="utf-8")
    returned = set(re.findall(r'return "failed:([a-z_]+)"', launcher))
    permanent = set(re.findall(r'"([a-z_]+)"', launcher[
        launcher.index("PERMANENT_FAILURES"):launcher.index("PERMANENT_PREFIXES")]))
    codes = returned | permanent
    # Non-vacuity. Both regexes read source that could be reworded, and a pair that matched
    # nothing would assert `set() - anything == set()` and pass forever — §Lessons 71, an
    # assertion that cannot fail when the thing under test is emptied.
    assert len(codes) >= 15, f"the guard stopped finding failure codes: {sorted(codes)}"
    assert "copilot_violation_agent_submitted" in codes
    known = _hint({"status": "failed", "apply_error": "x"}, tmp_path)["codes"]
    missing = sorted(codes - set(known))
    assert not missing, f"no English sentence for: {missing}"
