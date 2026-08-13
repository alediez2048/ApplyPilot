"""The operator has more than one email address, and six places assumed they had one.

Found on live data. A contact at Amsysis had 61 messages synced — **50 stored as `in`, of which
30 were the operator's own**, sent from the address on their résumé rather than the account the
app authenticates as. Every one of those was attributed to the other person.

That is not cosmetic. `direction` decides who owes whom a reply, whether the handoff banner
fires, whether a follow-up ladder halts, and what the temperature band reads. The visible symptom
was a wall of `👋 X added Y to the thread` banners — including one offering the operator's OWN
address as "+ Add as contact", and one claiming the operator had introduced somebody to their own
thread.

`reply_target` already took `str | list[str]`, for exactly this reason and with a comment saying
so. The other six kept taking a string — §Lessons 49 at six call sites, and the rule was not only
written down but IMPLEMENTED, once.
"""

from __future__ import annotations

import pytest

from applypilot.domain import conversations as cv
from applypilot.domain import replies as dr

MINE = "me@work.test"
ALSO_MINE = "me@resume.test"          # the address on the résumé, replied to by real people
THEM = "kevin@amsysis.test"

BOTH = [MINE, ALSO_MINE]


def msg(frm, to=THEM, cc="", at="1000", mid="m1"):
    return {"id": mid, "from": frm, "to": to, "cc": cc, "subject": "Re: intro",
            "internalDate": at, "snippet": "hello"}


# ── the shared answer ───────────────────────────────────────────────────────

def test_one_address_or_many_reach_the_same_answer():
    assert cv.me_set(MINE) == {MINE}
    assert cv.me_set(BOTH) == {MINE, ALSO_MINE}
    assert cv.me_set(None) == set() and cv.me_set([]) == set()
    # Display forms and case are normalised, or "Me <Me@Work.test>" is a stranger.
    assert cv.me_set(["Alejandro <Me@Work.TEST>"]) == {MINE}


# ── direction: the bug, in one assertion ────────────────────────────────────

def test_mail_from_the_operators_OTHER_address_is_not_inbound():
    """The 30 messages. Sent by the operator, stored as if the other side had written them."""
    rows = cv.timeline([msg(ALSO_MINE)], BOTH)
    assert rows[0]["direction"] == "out"


def test_and_with_only_one_address_it_is_wrong_the_old_way():
    """The negative case, so the test cannot pass by calling everything outbound."""
    assert cv.timeline([msg(ALSO_MINE)], MINE)[0]["direction"] == "in"
    assert cv.timeline([msg(THEM)], BOTH)[0]["direction"] == "in"


def test_a_real_reply_is_still_inbound():
    rows = cv.timeline([msg(MINE, at="1"), msg(THEM, at="2"), msg(ALSO_MINE, at="3")], BOTH)
    assert [r["direction"] for r in rows] == ["out", "in", "out"]


# ── the banner, which is what the operator actually saw ─────────────────────

def test_the_operators_own_address_is_never_offered_as_a_contact():
    """A "+ Add as contact" button pointing at yourself. It rendered, live."""
    threads = {"c1": [{"direction": "in", "from_addr": THEM,
                       "cc_addrs": [ALSO_MINE, "colleague@amsysis.test"],
                       "to_addrs": [MINE], "sent_at": "1"}]}
    got = {p["email"] for p in cv.pending_introductions(threads, [THEM], BOTH)}
    assert ALSO_MINE not in got, "offered the operator their own address"
    assert got == {"colleague@amsysis.test"}, "a genuine introduction was lost"


def test_the_operator_cannot_introduce_someone_to_their_own_thread():
    """`Alejandro Diez added Jess Daniel Weinstein` — the operator's own message, read as a
    handoff FROM a stranger, because the message was filed inbound."""
    threads = {"c1": [{"direction": "in", "from_addr": ALSO_MINE,
                       "cc_addrs": ["someone@utexas.test"], "to_addrs": [THEM], "sent_at": "1"}]}
    assert cv.pending_introductions(threads, [THEM], BOTH) == []


# ── reply detection: the expensive one ──────────────────────────────────────

def test_our_own_mail_is_not_counted_as_a_reply():
    """A false reply HALTS the follow-up ladder, lights the counter and moves the temperature
    band — on a message the operator sent themselves. Missing a real one costs a wasted
    follow-up; inventing one costs the sequence."""
    assert dr.is_inbound({"from": ALSO_MINE, "labelIds": []}, BOTH) is False
    assert dr.is_inbound({"from": ALSO_MINE, "labelIds": []}, MINE) is True   # the old behaviour
    assert dr.is_inbound({"from": THEM, "labelIds": []}, BOTH) is True


def test_a_single_string_still_works_everywhere():
    """Every one of these took a string yesterday, and the CLI still passes one."""
    assert dr.is_inbound({"from": THEM, "labelIds": []}, MINE) is True
    assert cv.timeline([msg(MINE)], MINE)[0]["direction"] == "out"
    assert cv.participants([msg(THEM)], MINE)
    assert cv.pending_introductions({}, [], MINE) == []


# ── the setting that supplies the extra addresses ───────────────────────────

def test_the_extra_addresses_reach_the_one_function_that_answers_this(monkeypatch):
    from applypilot.networking.gmail_send import _our_addresses
    monkeypatch.setenv("GMAIL_ADDRESS", MINE)
    monkeypatch.setenv("MY_ADDRESSES", f" {ALSO_MINE} , old@alias.test ")
    monkeypatch.setenv("OUTREACH_FROM_ADDRESS", "")
    got = _our_addresses()
    assert ALSO_MINE in got and "old@alias.test" in got and MINE in got


def test_an_empty_setting_changes_nothing(monkeypatch):
    from applypilot.networking.gmail_send import _our_addresses
    monkeypatch.setenv("GMAIL_ADDRESS", MINE)
    monkeypatch.setenv("OUTREACH_FROM_ADDRESS", "")
    monkeypatch.setenv("MY_ADDRESSES", "")
    assert MINE in _our_addresses()


def test_it_is_declared_so_doctor_can_show_it():
    from applypilot import settings
    s = next((x for x in settings.SETTINGS if x.name == "MY_ADDRESSES"), None)
    assert s is not None, "MY_ADDRESSES is not in the settings registry"


@pytest.mark.parametrize("fn", ["timeline", "participants", "introductions",
                                "reply_target", "pending_introductions"])
def test_every_is_this_us_question_accepts_several(fn):
    """The guard against the seventh. Each of these decides ours-vs-theirs, and a new one that
    takes a bare `str` is this bug again somewhere else."""
    import inspect
    sig = inspect.signature(getattr(cv, fn))
    ann = str(sig.parameters["me"].annotation)
    assert "list" in ann, f"cv.{fn} still takes a single address"


# ── and the wall of banners, which is what got reported ─────────────────────

def test_a_busy_thread_collapses_its_handoff_banners(tmp_path):
    """Eleven stacked banners pushed the conversation off screen on a real card. Two show; the
    rest are a list you open — the operator had just clicked "Fetch from Gmail" to READ the
    thread, and got a page of buttons instead."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest as _pytest

    import applypilot.web_dashboard as wd
    from browser_stubs import BROWSER_GLOBALS
    if not shutil.which("node"):
        _pytest.skip("node not available")
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    intros = [{"email": f"p{i}@amsysis.test", "name": f"P{i}", "introduced_by": "Kevin"}
              for i in range(11)]
    script = tmp_path / "intro.mjs"
    script.write_text(
        BROWSER_GLOBALS
        + "const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},"
          " closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},"
          " getAttribute:()=>null, addEventListener(){}, appendChild(){},"
          " classList:{add(){},remove(){},toggle(){}}, dataset:{} });\n"
          "globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),"
          " querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,"
          " activeElement:null };\n"
        + f"const SRC = {json.dumps(src)};\n"
        + f"const J = {{url:'u1', introductions: {json.dumps(intros)}}};\n"
        + """
const F = (new Function(SRC + '; return { introBanner, INTRO_OPEN, toggleIntros };'))();
const shut = F.introBanner(J);
F.INTRO_OPEN.add('u1');
const open = F.introBanner(J);
console.log(JSON.stringify({
  shutCount: (shut.match(/intro-bar/g) || []).length,
  openCount: (open.match(/intro-bar/g) || []).length,
  hasMore: shut.includes('9 more people were added'),
  hasFewer: open.includes('Show fewer'),
  none: F.introBanner({url:'u2', introductions: []}),
}));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["shutCount"] == 2, "the banners are not bounded"
    assert out["hasMore"], "no way to reach the other nine"
    assert out["openCount"] == 11, "expanding does not show them all"
    assert out["hasFewer"], "no way back"
    # Nothing to say -> say nothing. An empty heading above the People tab is worse than absent.
    assert out["none"] == ""
