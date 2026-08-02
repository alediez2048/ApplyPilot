"""The header aggregator: everything outstanding, in one number.

The dashboard already knew what was actionable in four separate places — the tab badge
(`needsYou`), the per-job Next button (`nextAction`), the Follow-ups tab count, and the default
open tab. Three of them summed email + LinkedIn and silently ignored SMS the moment that
channel shipped, so a job whose only outstanding work was a text read as "nothing to do".

That is §Lessons 21 exactly: a derived number computed in several places is several numbers.
All of them read `dueByChannel` now, and these tests exist to keep that true — a fourth channel
must light up every counter without touching any of them.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from applypilot import web_dashboard

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not available")

_STUBS = """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){}, scrollIntoView(){},
  classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, appendChild(){}, dataset:{} });
globalThis.document = { getElementById: el, querySelectorAll: ()=>[], querySelector: el,
  addEventListener(){}, activeElement:null, body: el(), hasFocus: () => false };
globalThis.window = { open(){}, location:{href:''} };
Object.defineProperty(globalThis, "navigator",
  { value:{ clipboard:{ writeText(){} } }, configurable:true });
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.fetch = async () => ({ json: async () => ({}) });
globalThis.alert = () => {};
globalThis.confirm = () => true;
"""

_EXPORTS = ("; return { pendingActions, dueByChannel, needsYou, nextAction, jobTabs, "
            "activeTab, FOLLOWUP_CHANNELS, PANEL_OPEN, TAB_OPEN };")


def _js() -> str:
    path = web_dashboard._STATIC_DIR / "dashboard.js"
    if not path.exists():
        pytest.skip("dashboard.js not found")
    return path.read_text(encoding="utf-8")


def _run(driver: str, tmp_path, **payload):
    script = tmp_path / "t.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {json.dumps(_js())};\n"
        + f"const F = (new Function(SRC + {json.dumps(_EXPORTS)}))();\n"
        + "".join(f"const {k} = {json.dumps(v)};\n" for k, v in payload.items())
        + driver
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _fu(**over):
    base = {"due_count": 0, "li_due_count": 0, "sms_due_count": 0}
    base.update(over)
    return base


def _job(**over):
    base = {"url": "http://j/1", "title": "PM", "company": "Acme", "status": "applied",
            "contacts": [{"id": "c1"}], "awaiting_reply": [], "followups": _fu(),
            "checklist": {"steps": []}, "location": "", "salary": "", "description": ""}
    base.update(over)
    return base


# ── the gap this was built to close ─────────────────────────────────────────

def test_every_channel_counts_toward_every_counter(tmp_path):
    """The bug: a job whose ONLY outstanding action was a text read as "nothing to do" in the
    tab badge, the Next button and the Follow-ups tab, because all three summed exactly two
    channels by name."""
    out = _run(
        """
        const r = {};
        for (const ch of ['due_count', 'li_due_count', 'sms_due_count']) {
          const j = JSON.parse(JSON.stringify(JOB));
          j.followups[ch] = 1;
          r[ch] = { due: F.dueByChannel(j).total, needs: F.needsYou(j),
                    pending: F.pendingActions([j]).total,
                    next: F.nextAction(j).includes("'followups'"),
                    tab: F.jobTabs(j).includes('Follow-ups <span class="n due">1</span>') };
        }
        console.log(JSON.stringify(r));
        """, tmp_path, JOB=_job())
    for ch, r in out.items():
        assert r["due"] == 1, f"{ch}: dueByChannel missed it"
        assert r["needs"] is True, f"{ch}: the tab badge would not raise"
        assert r["pending"] == 1, f"{ch}: the header aggregator missed it"
        assert r["next"] is True, f"{ch}: the Next button did not offer the follow-up"
        assert r["tab"] is True, f"{ch}: the Follow-ups tab count missed it"


def test_the_channel_list_drives_the_count_not_hardcoded_keys():
    """`followup_panel` builds its payload FROM CHANNELS, so a fourth channel arrives with
    `<prefix>due_count` already populated. Reading a hardcoded pair of names is what broke when
    SMS shipped — this asserts nothing re-introduces one."""
    src = _js()
    assert "li_due_count" not in src, (
        "a hardcoded per-channel key is back; use dueByChannel so a new channel counts itself")


# ── ordering is the whole value ─────────────────────────────────────────────

def test_a_human_who_replied_outranks_every_ladder(tmp_path):
    """§Lessons 27. A flat count of 31 tells you nothing, and a list that puts "3 LinkedIn
    invites left" above "someone replied 4 days ago" is worse than no list."""
    out = _run(
        """
        const j = JSON.parse(JSON.stringify(JOB));
        j.awaiting_reply = [{ id: 'c1', full_name: 'Victoria', days: 4 }];
        j.followups.due_count = 3;
        const p = F.pendingActions([j]);
        console.log(JSON.stringify({ order: p.groups.map(g => g.key), total: p.total,
                                     urgent: p.urgent }));
        """, tmp_path, JOB=_job())
    assert out["order"][0] == "replies", f"a ladder outranked a live reply: {out['order']}"
    assert out["total"] == 4, "the counts do not add up"
    assert out["urgent"] == 1, "only the reply should be flagged time-sensitive"


def test_a_rejected_application_owes_you_nothing(tmp_path):
    """It is closed. Counting its stale ladder keeps the badge permanently lit, which trains
    you to ignore it — the same reason the tab badge counts only what is NEW (CRM-3a)."""
    out = _run(
        """
        const j = JSON.parse(JSON.stringify(JOB));
        j.status = 'rejected'; j.followups.due_count = 5;
        j.awaiting_reply = [{ id: 'c1', full_name: 'X' }];
        console.log(JSON.stringify(F.pendingActions([j])));
        """, tmp_path, JOB=_job())
    assert out["total"] == 0, f"a rejected job still demanded work: {out}"


def test_nothing_outstanding_reports_zero_not_a_missing_key(tmp_path):
    out = _run("console.log(JSON.stringify(F.pendingActions([JOB])));", tmp_path, JOB=_job())
    assert out["total"] == 0 and out["groups"] == [] and out["urgent"] == 0


def test_an_empty_dashboard_does_not_throw(tmp_path):
    out = _run("console.log(JSON.stringify(F.pendingActions(null)));", tmp_path)
    assert out["total"] == 0


# ── the counts themselves ───────────────────────────────────────────────────

def test_it_aggregates_across_applications_not_within_one(tmp_path):
    """The point of the header counter: the per-job strip already tells you about ONE job."""
    out = _run(
        """
        const a = JSON.parse(JSON.stringify(JOB)), b = JSON.parse(JSON.stringify(JOB));
        b.url = 'http://j/2';
        a.followups.due_count = 2; b.followups.sms_due_count = 1;
        b.status = 'ready_to_submit';
        const p = F.pendingActions([a, b]);
        const fu = p.groups.find(g => g.key === 'followups');
        console.log(JSON.stringify({ total: p.total, fuN: fu.n, fuJobs: fu.jobs.length,
                                     channels: p.channels.map(c => [c.name, c.n]) }));
        """, tmp_path, JOB=_job())
    assert out["total"] == 4, out                 # 2 email + 1 sms + 1 ready_to_submit
    assert out["fuN"] == 3 and out["fuJobs"] == 2, "follow-ups did not aggregate across jobs"
    assert out["channels"] == [["email", 2], ["sms", 1]], out["channels"]


def test_contacts_found_but_never_emailed_is_outstanding_work(tmp_path):
    """Read off the checklist, which already owns the denominator. Recomputing it here is
    exactly how the header and the job row would come to disagree."""
    out = _run(
        """
        const j = JSON.parse(JSON.stringify(JOB));
        j.checklist = { steps: [{ key: 'emailed', done: 1, total: 4, state: 'partial' }] };
        const p = F.pendingActions([j]);
        console.log(JSON.stringify(p.groups.map(g => [g.key, g.n])));
        """, tmp_path, JOB=_job())
    assert out == [["outreach", 3]], out


def test_a_job_with_no_contacts_is_counted_once_not_per_missing_email(tmp_path):
    out = _run(
        """
        const j = JSON.parse(JSON.stringify(JOB));
        j.contacts = [];
        j.checklist = { steps: [{ key: 'emailed', done: 0, total: 5, state: 'todo' }] };
        console.log(JSON.stringify(F.pendingActions([j]).groups.map(g => [g.key, g.n])));
        """, tmp_path, JOB=_job())
    assert out == [["contacts", 1]], \
        f"a job with nobody found counted phantom emails: {out}"


def test_the_counter_ignores_the_search_filter(tmp_path):
    """A search that hides a job does not mean its follow-up stopped being due. A counter that
    drops as you type is worse than none — it reads as work disappearing."""
    src = _js()
    body = src[src.index("function rerenderJobs"):]
    body = body[:body.index("\n\n")]
    assert "renderTodo" not in body, (
        "the aggregator re-renders from the FILTERED view, so searching hides outstanding work")
    assert "renderTodo(allJobs)" in src, "the aggregator is not fed the whole set"
