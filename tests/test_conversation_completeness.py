"""Seeing the whole conversation.

Reported as "I'm not getting the entire interaction with people I'm emailing". Measured against
the live Google thread first: all **8 messages were stored**, with text, on the wire. Nothing was
missing. Three separate things made it read as missing, and only one of them was a bug in the
usual sense:

**The collapse had no way out.** A thread over six messages rendered first + last two and printed
`· 5 earlier messages ·` as PLAIN TEXT. The middle of a live conversation — including the reply
that asked a question — was unreachable. §Lessons 43's family: the control that would fix the
state did not exist, so the data may as well not have been there.

**Every message rendered `I&#39;m`.** Gmail's API returns `snippet` HTML-escaped; storing it raw
put the entity through the dashboard's own `esc()` a second time. **70 of 99 stored messages**
were affected, which is every message containing an apostrophe.

**A message at the cap stopped mid-sentence with nothing saying so.** `SNIPPET_MAX` is a
deliberate CRM-4b bound, not data loss — but a sentence that just stops looks identical to one
that was lost, and the way to get more (⤓ Fetch from Gmail, at `PASTED_MAX`) was unsignposted.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from browser_stubs import BROWSER_GLOBALS

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.networking import messages as msgs


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    msgs.init_messages(conn)
    return conn


# ── the escaping ────────────────────────────────────────────────────────────

def test_gmail_entities_are_decoded_at_the_write(db):
    """Decoding at RENDER would fix the display and leave the table holding markup — which the
    prompts, the metrics and every future reader would then have to know about."""
    msgs.upsert_messages([{
        "message_id": "m1", "thread_id": "t1", "contact_id": "c1", "direction": "in",
        "snippet": "Hi Alejandro, I&#39;m glad you applied &amp; I&#39;ll pass it on",
        "sent_at": "2026-08-10T00:00:00+00:00"}], db)
    got = db.execute("SELECT snippet FROM messages WHERE message_id='m1'").fetchone()[0]
    assert got == "Hi Alejandro, I'm glad you applied & I'll pass it on"


def test_decoding_is_idempotent(db):
    """It runs over rows that were already clean, and over the 71 that were backfilled. A decode
    that mangles clean text cannot be applied twice, and this one is applied on every sync."""
    plain = "Costs 5 < 6 and that's fine"
    msgs.upsert_messages([{"message_id": "m1", "thread_id": "t1", "contact_id": "c1", "direction": "in",
                           "snippet": plain, "sent_at": "2026-08-10T00:00:00+00:00"}], db)
    msgs.upsert_messages([{"message_id": "m1", "thread_id": "t1", "contact_id": "c1", "direction": "in",
                           "snippet": plain, "sent_at": "2026-08-10T00:00:00+00:00"}], db)
    assert db.execute("SELECT snippet FROM messages WHERE message_id='m1'").fetchone()[0] == plain


def test_the_cap_counts_characters_a_person_reads(db):
    """Decoded BEFORE truncation. Capping the escaped form spends five bytes on an apostrophe,
    so a message full of them is cut far shorter than one without — the bound would depend on
    the punctuation."""
    body = "I&#39;m " * 60           # 8 escaped chars each, 4 decoded
    msgs.upsert_messages([{"message_id": "m1", "thread_id": "t1", "contact_id": "c1", "direction": "in",
                           "snippet": body, "sent_at": "2026-08-10T00:00:00+00:00"}], db)
    got = db.execute("SELECT snippet FROM messages WHERE message_id='m1'").fetchone()[0]
    assert "&#39;" not in got
    assert len(got) == msgs.SNIPPET_MAX


def test_the_fetch_path_decodes_too(db):
    """`set_reply_text` does its own UPDATE and does not go through `upsert_messages`, so the fix
    there does not reach it — and "⤓ Fetch from Gmail" pulls from the same escaping API
    (§Lessons 49: a rule at one of its call sites is not implemented)."""
    msgs.upsert_messages([{"message_id": "m1", "thread_id": "t1", "contact_id": "c1", "direction": "in",
                           "snippet": "short", "sent_at": "2026-08-10T00:00:00+00:00"}], db)
    msgs.set_reply_text("c1", "They said I&#39;m a good fit &amp; will follow up", conn=db)
    got = db.execute("SELECT snippet FROM messages WHERE message_id='m1'").fetchone()[0]
    assert got == "They said I'm a good fit & will follow up"


# ── the collapse ────────────────────────────────────────────────────────────

def _conv(contact, expanded, tmp_path):
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "conv.mjs"
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
const F = (new Function(SRC + `; return { conversationView, CONV_EXPANDED, expandConv,
                                          collapseConv, rerenderJobs };`))();
""" + ("F.CONV_EXPANDED.add('c1');" if expanded else "") + """
const html = F.conversationView(""" + json.dumps(contact) + """);
console.log(JSON.stringify({
  bodies: (html.match(/class="cm-body"/g) || []).length,
  gap: /show (\\d+) earlier message/.exec(html),
  gapIsAButton: /cm-gap"><button/.test(html),
  hasExpand: html.includes('expandConv'),
  hasCollapse: html.includes('collapseConv'),
  clipped: (html.match(/cm-clip/g) || []).length,
}));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _thread(n, snippet="hello"):
    return [{"message_id": f"m{i}", "direction": "in" if i % 2 else "out",
             "from_name": "Patrick" if i % 2 else "", "sent_at": f"2026-08-{i + 1:02d}T00:00:00+00:00",
             "cc_addrs": [], "snippet": snippet} for i in range(n)]


def _contact(thread):
    return {"id": "c1", "full_name": "Patrick", "email": "p@x.test", "thread": thread,
            "conversation": {}, "reply_to": {"to_addr": "p@x.test", "cc": []},
            "outreach_message": "", "outreach_subject": "Re: the role"}


def test_a_long_thread_still_collapses(tmp_path):
    """The collapse is not the bug — an unbounded list pushes the composer off screen, and it is
    re-rendered every 2.5s."""
    out = _conv(_contact(_thread(8)), False, tmp_path)
    assert out["bodies"] == 3
    assert out["gap"][1] == "5"


def test_the_gap_is_a_BUTTON(tmp_path):
    """It was plain text. Five stored messages the operator could see the count of and never
    read — which is what "I'm not getting the entire interaction" was describing."""
    out = _conv(_contact(_thread(8)), False, tmp_path)
    assert out["gapIsAButton"] is True
    assert out["gap"] is not None
    assert out["hasExpand"] is True, "the gap renders as a button that does nothing"


def test_expanding_shows_every_message(tmp_path):
    out = _conv(_contact(_thread(8)), True, tmp_path)
    assert out["bodies"] == 8, "expanding did not reveal the middle"
    assert out["gap"] is None


def test_an_expanded_thread_can_be_collapsed_again(tmp_path):
    out = _conv(_contact(_thread(8)), True, tmp_path)
    assert out["hasCollapse"] is True


def test_a_short_thread_has_no_gap_and_no_collapse(tmp_path):
    """Guard the guard: rendering the controls unconditionally puts a "show 0 earlier" button on
    every two-message conversation."""
    out = _conv(_contact(_thread(4)), False, tmp_path)
    assert out["bodies"] == 4
    assert out["gap"] is None and out["hasCollapse"] is False


def test_a_short_thread_expanded_still_offers_no_collapse(tmp_path):
    out = _conv(_contact(_thread(4)), True, tmp_path)
    assert out["hasCollapse"] is False, "a control for a state that cannot happen"


# ── the truncation mark ─────────────────────────────────────────────────────

def test_a_message_at_the_cap_says_it_was_cut(tmp_path):
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "clip.mjs"
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
const F = (new Function(SRC + '; return { conversationView, setMax(v){ CONV_SNIPPET_MAX = v; } };'))();
F.setMax(200);
const long  = F.conversationView(""" + json.dumps(_contact(_thread(2, "x" * 200))) + """);
const short = F.conversationView(""" + json.dumps(_contact(_thread(2, "x" * 40))) + """);
console.log(JSON.stringify({ atCap: (long.match(/cm-clip/g)||[]).length,
                             under: (short.match(/cm-clip/g)||[]).length }));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["atCap"] == 2
    # Guard the guard: marking everything makes the mark meaningless and calls a complete
    # message truncated.
    assert out["under"] == 0


def test_the_cap_is_SERVED_not_hardcoded():
    """A bound written down twice is two bounds. The intro-deck PDF rode along on 34 real emails
    while `doctor --config` reported it off, because a default lived in two places."""
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert "CONV_SNIPPET_MAX = data.snippet_max" in js
    assert "cm-clip" in js
    # The literal must not reappear beside the constant that replaced it.
    conv = js[js.index("function convMessage("):js.index("function convMessage(") + 2500]
    assert ".length >= CONV_SNIPPET_MAX" in conv
    assert ">= 200" not in conv


def test_the_payload_serves_it():
    import inspect
    src = inspect.getsource(wd)
    assert '"snippet_max": _snippet_max()' in src
    assert "return _m.SNIPPET_MAX" in src


def test_the_served_value_is_the_enforced_one():
    """Executed, not grepped: `_snippet_max()` must return what the store layer actually caps at,
    so the mark cannot say "truncated" at a length nothing truncates."""
    assert wd._snippet_max() == msgs.SNIPPET_MAX
