"""Round two: "nobody replied, find me new people" — and the label that says when to press it.

Two halves that only work together. The label (`exhausted`) has to mean something precise or
the button appears at the wrong moment; the search has to actually EXCLUDE the people it
already found or pressing it spends Apollo credits to overwrite the rows you had.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import applypilot.database as database
from applypilot.domain.followup import EMPTY_LADDER, exhausted
from applypilot.networking import store, touches

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _ladder(count=0, last="", status=""):
    return {**EMPTY_LADDER, "count": count, "last_sent_at": last, "sequence_status": status}


def _emailed(**over):
    """Someone we emailed, whose email ladder is the only one that applies."""
    c = {"id": "c1", "full_name": "Jane", "email": "j@x.com", "emailed": True,
         "sent_message_id": "g1", "submitted_at": ago(days=30), "replied_at": "",
         "linkedin_url": "", "dm_status": "", "phone": "", "sms_sent_at": ""}
    c.update(over)
    return c


# ── what "no response" must and must not mean ───────────────────────────────

def test_a_finished_ladder_with_no_reply_is_no_response():
    done = {"email": _ladder(count=3, last=ago(days=1))}
    assert exhausted(_emailed(), done, NOW) is True


def test_somebody_never_written_to_is_not_unresponsive():
    """The distinction the whole feature rests on. An untouched contact needs an EMAIL, not a
    new round of contacts — labelling them "no response" would send you buying strangers while
    people you already found sit unwritten."""
    untouched = _emailed(emailed=False, sent_message_id="")
    assert exhausted(untouched, {}, NOW) is False


def test_a_running_ladder_is_not_exhausted():
    """The cheapest next move is finishing the sequence you started, not buying more contacts."""
    mid = {"email": _ladder(count=1, last=ago(hours=1))}
    assert exhausted(_emailed(), mid, NOW) is False


def test_a_reply_disqualifies_however_it_was_recorded():
    done = {"email": _ladder(count=3, last=ago(days=1))}
    assert exhausted(_emailed(replied_at=ago(days=2)), done, NOW) is False
    replied_seq = {"email": _ladder(count=3, last=ago(days=1), status="replied")}
    assert exhausted(_emailed(), replied_seq, NOW) is False


def test_one_channel_finishing_is_not_enough():
    """`finished` is per-CHANNEL. Someone whose email ladder is done but whose LinkedIn sequence
    is still running has not gone quiet — they have a channel left, and calling that "no
    response" sends you shopping while a live sequence is mid-flight."""
    both = _emailed(linkedin_url="https://l/in/j", dm_status="manual", dm_sent_at=ago(days=1))
    ladders = {"email": _ladder(count=3, last=ago(days=10)),
               "linkedin": _ladder(count=0)}       # invited yesterday, first touch not due
    assert exhausted(both, ladders, NOW) is False

    ladders["linkedin"] = _ladder(count=2, last=ago(days=20))   # now spent too
    assert exhausted(both, ladders, NOW) is True


def test_an_operator_stopped_sequence_still_counts_as_spent():
    """`stopped` is a decision, not an outcome — but the channel IS over, so it cannot hold the
    contact in limbo forever. What must never happen is a stop counting as a REPLY."""
    stopped = {"email": _ladder(count=1, last=ago(days=5), status="stopped")}
    assert exhausted(_emailed(), stopped, NOW) is True


def test_it_is_derived_and_never_stored():
    """A column would be stale between a touch being sent and the next recompute — §Lessons 21
    with a new name. This asserts nobody adds one."""
    assert "exhausted" not in store._CONTACT_COLUMNS, (
        "`exhausted` became a column; it must stay derived so it cannot drift from the ladder")


# ── the search actually has to find NEW people ──────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    conn.execute("INSERT INTO jobs (url, title, site) VALUES (?,?,?)",
                 ("http://j/1", "AI Engineer", "Greenhouse"))
    conn.commit()
    return conn


def test_round_two_excludes_exactly_what_is_already_stored(db, monkeypatch):
    """The failure this prevents is expensive and silent: `select()` scores title relevance and
    is deterministic, so a plain re-run returns the SAME top five, `upsert_contact` overwrites
    the rows you had, and the operator has spent Apollo credits to stand still.

    Exclusion uses `contact_id` — the same function that stores them. Deriving a second
    name/email match here would be a competing answer to "is this the same person", and the two
    would disagree; §Lessons 1 is a whole family of exactly that.
    """
    from applypilot.networking import service

    store.upsert_contact({"job_url": "http://j/1", "full_name": "Ada Known",
                          "linkedin_url": "https://l/in/ada", "email": "ada@x.com"}, db)

    pool = [{"full_name": "Ada Known", "linkedin_url": "https://l/in/ada", "title": "Recruiter"},
            {"full_name": "Bo New", "linkedin_url": "https://l/in/bo", "title": "Recruiter"}]

    monkeypatch.setattr(service.derive, "derive_company", lambda job: "Acme")
    monkeypatch.setattr(service.derive, "derive_domain", lambda job, company: "acme.com")
    monkeypatch.setattr(service.providers, "search", lambda *a, **kw: list(pool))
    monkeypatch.setattr(service.rank, "select", lambda cands, role, n=None: list(cands))
    seen = {}

    def fake_enrich(batch):
        seen["batch"] = [b["full_name"] for b in batch]
        return {}
    monkeypatch.setattr(service.providers, "enrich", fake_enrich)

    service.find_contacts_for_job({"url": "http://j/1", "title": "AI Engineer"},
                                  per_job=5, skip_known=True, draft=False)
    assert seen.get("batch") == ["Bo New"], (
        f"round two re-enriched somebody already stored: {seen.get('batch')}")


def test_round_two_finding_nobody_new_says_so_loudly(db, monkeypatch):
    """§Lessons 15. A search that ran, spent credits and kept nobody must not be
    byte-identical to a button that never fired."""
    from applypilot.networking import service

    store.upsert_contact({"job_url": "http://j/1", "full_name": "Ada Known",
                          "linkedin_url": "https://l/in/ada"}, db)
    monkeypatch.setattr(service.derive, "derive_company", lambda job: "Acme")
    monkeypatch.setattr(service.derive, "derive_domain", lambda job, company: "acme.com")
    monkeypatch.setattr(service.providers, "search",
                        lambda *a, **kw: [{"full_name": "Ada Known",
                                          "linkedin_url": "https://l/in/ada", "title": "R"}])
    monkeypatch.setattr(service.rank, "select", lambda cands, role, n=None: list(cands))

    def boom(batch):
        raise AssertionError("enriched a candidate that was already known — credits spent")
    monkeypatch.setattr(service.providers, "enrich", boom)

    res = service.find_contacts_for_job({"url": "http://j/1", "title": "AI Engineer"},
                                        per_job=5, skip_known=True, draft=False)
    assert res["found"] == 0
    assert "already on this job" in res["note"], f"a silent zero: {res['note']!r}"

    events = [r["detail"] for r in db.execute(
        "SELECT detail FROM job_events WHERE job_url = ?", ("http://j/1",)).fetchall()]
    assert any("already on this job" in e for e in events), \
        "nothing reached the activity log, so the run left no trace"


def test_a_normal_first_run_is_unchanged_by_the_flag(db, monkeypatch):
    """skip_known defaults OFF. The first round must still find everyone."""
    from applypilot.networking import service

    monkeypatch.setattr(service.derive, "derive_company", lambda job: "Acme")
    monkeypatch.setattr(service.derive, "derive_domain", lambda job, company: "acme.com")
    monkeypatch.setattr(service.providers, "search",
                        lambda *a, **kw: [{"full_name": "Ada", "linkedin_url": "https://l/in/a",
                                          "title": "R"}])
    monkeypatch.setattr(service.rank, "select", lambda cands, role, n=None: list(cands))
    seen = {}
    def _enrich(batch):
        seen["n"] = len(batch)
        return {}
    monkeypatch.setattr(service.providers, "enrich", _enrich)

    service.find_contacts_for_job({"url": "http://j/1", "title": "AI Engineer"},
                                  per_job=5, draft=False)
    assert seen.get("n") == 1, "the default path stopped considering candidates"


# ── the panel must EXPLAIN itself, not vanish ───────────────────────────────

import json as _json          # noqa: E402
import shutil as _shutil      # noqa: E402
import subprocess as _sp      # noqa: E402

from applypilot import web_dashboard as _wd   # noqa: E402

_STUBS = """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, focus(){}, scrollIntoView(){},
  classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, appendChild(){}, dataset:{} });
globalThis.document = { getElementById: el, querySelectorAll: ()=>[], querySelector: el,
  addEventListener(){}, activeElement:null, body: el(), hasFocus: () => false };
globalThis.window = { open(){}, location:{href:''} };
Object.defineProperty(globalThis,"navigator",{value:{clipboard:{writeText(){}}},configurable:true});
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0;
globalThis.fetch = async () => ({ json: async () => ({}) });
globalThis.alert = () => {}; globalThis.confirm = () => true;
"""


def _panel(contacts, tmp_path, network_running=False):
    src = (_wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "r2.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {_json.dumps(src)};\n"
        + "const F = (new Function(SRC + '; NET_AVAIL = true; return { anotherRoundPrompt };'))();\n"
        + f"const CS = {_json.dumps(contacts)};\n"
        + f"const J = {{ url: 'http://j/1', network_running: {str(network_running).lower()} }};\n"
        + "console.log(JSON.stringify(F.anotherRoundPrompt(J, CS)));"
    )
    proc = _sp.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:1500]
    return _json.loads(proc.stdout.strip().splitlines()[-1])


def _c(**over):
    base = {"id": "x", "full_name": "Ada Lovelace", "emailed": True, "exhausted": False,
            "replied_at": "", "dm_status": "", "conversation": {}}
    base.update(over)
    return base


@pytest.mark.skipif(not _shutil.which("node"), reason="node not available")
def test_the_panel_explains_why_instead_of_disappearing(tmp_path):
    """§Lessons 41, repeated two commits after it was written down. Returning '' when the round
    is not spent is correct behaviour and unusable feedback: reported as "I'm not seeing the
    button" on a job whose sequences were simply still running. The operator cannot tell
    "not yet" from "broken" if there is nothing on screen."""
    running = _panel([_c(), _c(id="y", exhausted=True)], tmp_path)
    assert running, "the panel vanished instead of saying why it is not available"
    assert "disabled" in running and "still running" in running

    replied = _panel([_c(replied_at="2026-08-01T00:00:00+00:00")], tmp_path)
    assert "disabled" in replied and "waiting on you" in replied, \
        "a live reply must be named as the reason, not silently disable the button"
    assert "Ada" in replied, "it does not say WHO is waiting"

    untouched = _panel([_c(emailed=False)], tmp_path)
    assert "disabled" in untouched and "never been written to" in untouched

    ready = _panel([_c(exhausted=True), _c(id="y", exhausted=True)], tmp_path)
    assert "disabled" not in ready, "every ladder is spent and the button is still disabled"
    assert "No response from any of the 2" in ready and "round2 ready" in ready


@pytest.mark.skipif(not _shutil.which("node"), reason="node not available")
def test_no_contacts_means_no_panel(tmp_path):
    """With nobody found, the FIRST-round prompt is the right control. Two competing
    find-contacts buttons on one empty tab is worse than either alone."""
    assert _panel([], tmp_path) == ""
