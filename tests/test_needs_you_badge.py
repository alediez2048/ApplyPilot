"""The tab title tells you an application is waiting.

CRM-3a. Co-pilot apply ENDS by handing an open browser to the operator, and the queue stays
paused until they act — but nothing pulled them back to the tab. No sound, no desktop
notification, no badge; only a 2.5s refresh that helps if you are already looking. A filled
application sat until someone happened to look, and a dashboard restart eventually closed it,
losing the filled form.

The design decision worth pinning: the badge counts what is NEW since you last looked, not
every actionable row. Two permanently-stale needs_human rows would otherwise show "(2)"
forever, which trains you to ignore the badge — the exact opposite of the point.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from applypilot import web_dashboard

from browser_stubs import BROWSER_GLOBALS

_STUBS = """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){},
  scrollIntoView(){},
  classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, appendChild(){}, dataset:{} });
globalThis.document = { title: '', hasFocus: () => FOCUSED.value,
  getElementById: el, querySelectorAll: ()=>[], querySelector: el,
  addEventListener(){}, activeElement:null, body: el() };
""" + BROWSER_GLOBALS

_DRIVER = """
const F = (new Function(SRC + `; return { updateNeedsYouBadge, needsYou };`))();
const out = [];
// Focus varies PER STEP: "seen" only happens while the tab is focused, so a test that never
// focuses can never exercise the seen-set at all — which is how the prune test passed against
// a build with the prune deleted.
for (const step of STEPS) {
  FOCUSED.value = !!step.focused;
  out.push({ unseen: F.updateNeedsYouBadge(step.jobs), title: document.title });
}
console.log(JSON.stringify(out));
"""


def _job(url, status="ready", due=0):
    return {"url": url, "status": status,
            "followups": {"due_count": due, "li_due_count": 0}}


def _run(steps, focused=False):
    """steps: list of job-lists, or list of {"jobs": [...], "focused": bool} for per-step focus."""
    steps = [s if isinstance(s, dict) else {"jobs": s, "focused": focused} for s in steps]
    if not shutil.which("node"):
        pytest.skip("node not available")
    src = (web_dashboard._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = f"const FOCUSED = {{value: {json.dumps(focused)}}};\n{_STUBS}\n" \
             f"const SRC = {json.dumps(src)};\nconst STEPS = {json.dumps(steps)};\n{_DRIVER}"
    import tempfile
    import pathlib
    path = pathlib.Path(tempfile.mkdtemp()) / "badge.mjs"
    path.write_text(script)
    proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:1500]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_waiting_application_raises_the_badge():
    """The whole point: you are in another tab and the agent just handed a form back."""
    out = _run([[_job("a", "ready_to_submit")]])
    assert out[0]["unseen"] == 1
    assert out[0]["title"].startswith("(1)"), out[0]["title"]


@pytest.mark.parametrize("status", ["ready_to_submit", "needs_human"])
def test_both_handover_states_count(status):
    assert _run([[_job("a", status)]])[0]["unseen"] == 1


def test_a_due_followup_counts_too():
    assert _run([[_job("a", "applied", due=2)]])[0]["unseen"] == 1


def test_nothing_to_do_means_a_clean_title():
    out = _run([[_job("a", "applied"), _job("b", "ready")]])
    assert out[0]["unseen"] == 0
    assert out[0]["title"] == "ApplyPilot Operator"


def test_looking_at_the_tab_clears_it():
    """Focused = seen. Otherwise the badge would persist while you are staring at the row."""
    out = _run([[_job("a", "ready_to_submit")]], focused=True)
    assert out[0]["unseen"] == 0
    assert out[0]["title"] == "ApplyPilot Operator"


def test_a_row_you_already_saw_does_not_keep_badging():
    """The anti-noise rule. Two stale needs_human rows must not show (2) forever."""
    steps = [[_job("a", "needs_human")], [_job("a", "needs_human")]]
    out = _run(steps, focused=True)
    assert [o["unseen"] for o in out] == [0, 0]


def test_a_NEW_job_still_raises_the_badge_after_an_earlier_one_was_seen():
    """The failure mode of a naive 'seen' flag: acknowledging one job silences the next."""
    steps = [
        [_job("a", "needs_human")],                              # seen while focused
        [_job("a", "needs_human"), _job("b", "ready_to_submit")],
    ]
    out = _run(steps, focused=False)
    # Unfocused throughout, so nothing is ever marked seen — both are unseen by step 2.
    assert out[1]["unseen"] == 2, out


def test_resolving_a_job_forgets_it_so_it_can_badge_again_later():
    """Re-applying a job that was submitted should be able to raise the badge a second time.
    Without pruning, the url would stay in the seen set forever."""
    steps = [
        {"jobs": [_job("a", "ready_to_submit")], "focused": True},   # SEEN
        {"jobs": [_job("a", "applied")], "focused": False},          # resolved -> prune
        {"jobs": [_job("a", "ready_to_submit")], "focused": False},  # actionable again
    ]
    out = _run(steps)
    assert out[0]["unseen"] == 0, "focused step should have marked it seen"
    assert out[2]["unseen"] == 1, "the resolved job was never pruned, so it can never badge again"
