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
""" + ("F.CONV_EXPANDED.add('c1|t1');" if expanded else "") + """
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
    return [{"message_id": f"m{i}", "thread_id": "t1", "subject": "Re: the role",
             "direction": "in" if i % 2 else "out",
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


# ── separate conversations are separate ─────────────────────────────────────

def _multi(tmp_path, contact, opens=(), shuts=()):
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "multi.mjs"
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
const F = (new Function(SRC + `; return { conversationView, groupThreads, CONV_OPEN, CONV_SHUT,
                                          toggleThread, rerenderJobs };`))();
""" + "".join(f"F.CONV_OPEN.add({json.dumps(k)});" for k in opens) \
    + "".join(f"F.CONV_SHUT.add({json.dumps(k)});" for k in shuts) + """
const C = """ + json.dumps(contact) + """;
const html = F.conversationView(C);
console.log(JSON.stringify({
  seps: (html.match(/class="th-sep/g) || []).length,
  // Decoded, because the render escapes it correctly — "Ormus &lt;&gt; AMSYS" is the right
  // markup and the wrong thing to assert against.
  metas: [...html.matchAll(/class="th-meta">([^<]*)</g)].map(m => m[1]),
  subjects: [...html.matchAll(/class="th-subj">([^<]*)</g)].map(m => m[1]
    .replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&#39;/g,"'")),
  bodies: (html.match(/class="cm-body"/g) || []).length,
  shutSeps: (html.match(/class="th-sep shut/g) || []).length,
  groups: F.groupThreads(C.thread).map(g => ({id: g.id, n: g.msgs.length, subject: g.subject})),
}));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _m(tid, subj, day, direction="in", mid=None):
    at = f"2026-08-{day:02d}T00:00:00+00:00"
    return {"message_id": mid or f"{tid}-{day}", "thread_id": tid, "subject": subj,
            "direction": direction, "from_name": "Diego" if direction == "in" else "",
            "sent_at": at, "cc_addrs": [], "snippet": "hello"}


THREE = [_m("tA", "External Partner Chat", 1), _m("tA", "Re: External Partner Chat", 2),
         _m("tB", "AMSYS OS Follow Up", 3),
         _m("tC", "Ormus Solutions <> AMSYS AI", 4), _m("tC", "Re: Ormus Solutions <> AMSYS AI", 5)]


def test_each_gmail_thread_gets_its_own_separator(tmp_path):
    """One contact, seven Gmail threads, rendered as a single continuous stream — a calendar
    invite, an introduction and two deals with the same four people interleaved by date.
    Reported as "this just looks like one long conversation which is not"."""
    out = _multi(tmp_path, _contact(THREE))
    assert out["seps"] == 3, "the threads are still merged into one conversation"
    assert len(out["groups"]) == 3


def test_the_separator_names_the_conversation(tmp_path):
    """The subject is the only thing that says WHICH conversation this is, and the shortest form
    is the right one: "Re: Re: Ormus <> AMSYS" is the same thread as "Ormus <> AMSYS"."""
    out = _multi(tmp_path, _contact(THREE))
    assert "External Partner Chat" in out["subjects"]
    assert all(m.strip().endswith(("Aug 1", "Aug 3", "Aug 5", "Aug 7", "Aug 9")) or "Aug" in m
               for m in out["metas"]), f"a separator has no date: {out['metas']}"
    assert "Ormus Solutions <> AMSYS AI" in out["subjects"]
    assert not any(s.lower().startswith("re:") for s in out["subjects"]), \
        "a separator is titled with a reply prefix"


def test_only_the_newest_thread_opens_by_default(tmp_path):
    """Seven expanded threads is the wall this replaced, and the live conversation is the one
    you came for — so it sits at the bottom, open, next to the composer."""
    out = _multi(tmp_path, _contact(THREE))
    assert out["bodies"] == 2, "expected only the newest thread's messages"
    assert out["shutSeps"] == 2
    assert out["subjects"][-1] == "Ormus Solutions <> AMSYS AI", "newest thread is not last"


def test_threads_are_ordered_by_their_LAST_message(tmp_path):
    """The live conversation belongs at the bottom, beside the composer.

    The fixture is deliberately out of order — built newest-thread-first — because the earlier
    version happened to insert chronologically, so removing the sort entirely left every test
    green. A test whose input is already sorted cannot see a sort.

    Ordered by the LAST message rather than the first: a thread opened in June and answered
    yesterday is the current one, however old it started.
    """
    out = _multi(tmp_path, _contact([
        _m("tC", "Ormus Solutions <> AMSYS AI", 9),      # newest, listed first
        _m("tA", "External Partner Chat", 1),
        _m("tA", "Re: External Partner Chat", 7),        # started first, answered recently
        _m("tB", "AMSYS OS Follow Up", 3),               # oldest last message
    ]))
    assert [g["id"] for g in out["groups"]] == ["tB", "tA", "tC"], \
        "threads are not ordered by their most recent message"
    assert out["subjects"][-1] == "Ormus Solutions <> AMSYS AI"


def test_an_older_thread_opens_when_asked(tmp_path):
    out = _multi(tmp_path, _contact(THREE), opens=["c1|tA"])
    assert out["bodies"] == 4, "opening an older thread did not show it"
    assert out["shutSeps"] == 1


def test_the_newest_thread_can_be_shut(tmp_path):
    """Both directions. One set for "opened" would make shutting the default-open thread
    indistinguishable from never having touched it."""
    out = _multi(tmp_path, _contact(THREE), shuts=["c1|tC"])
    assert out["bodies"] == 0
    assert out["shutSeps"] == 3


def test_a_single_thread_still_renders_without_ceremony(tmp_path):
    """The common case must not regress: one conversation, open, one separator naming it."""
    out = _multi(tmp_path, _contact([_m("tA", "Re: the role", 1), _m("tA", "Re: the role", 2)]))
    assert out["seps"] == 1 and out["bodies"] == 2 and out["shutSeps"] == 0


def test_messages_with_no_thread_id_group_by_subject(tmp_path):
    """Pasted messages and anything synced before threading was stored carry no id. Bucketing
    them all under "" rebuilds the merge this exists to undo."""
    msgs = [dict(_m("", "Intro call", 1), thread_id=""),
            dict(_m("", "Re: Intro call", 2), thread_id=""),
            dict(_m("", "Contract", 3), thread_id="")]
    out = _multi(tmp_path, _contact(msgs))
    assert len(out["groups"]) == 2, "no-id messages were merged into one conversation"
