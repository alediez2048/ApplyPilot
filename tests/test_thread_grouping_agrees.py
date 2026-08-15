"""The browser and the server must group a person's messages into the SAME threads.

`groupThreads()` in `dashboard.js` decides which conversation the composer sits under and posts
its key; `domain/conversations.py:thread_key` resolves that key to the rows a reply is addressed
from. If the two disagree about where one message belongs, the browser names a thread the server
either cannot find (a visible refusal) or resolves differently (a silent misaddressing).

They already disagreed when this was written: the JS stripped ONE leading `Re:` while `_strip_re`
strips repeated `Re:`/`Fwd:`/`Fw:` prefixes, so a forwarded message landed in its own group in the
browser and in the main thread on the server. Nothing rendered wrong — §Lessons 49 with the second
implementation deciding who gets emailed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from applypilot.domain import conversations as cv

#: Deliberately awkward. Every row here is a shape that appeared on the live database or that the
#: two implementations used to answer differently.
FIXTURE = [
    {"thread_id": "t1", "subject": "Ormus <> AMSYS", "sent_at": "2026-08-01T10:00"},
    {"thread_id": "t1", "subject": "Re: Ormus <> AMSYS", "sent_at": "2026-08-02T10:00"},
    {"thread_id": "t2", "subject": "Accepted: Meeting", "sent_at": "2026-08-03T10:00"},
    # No thread id: pasted, or synced before threading was stored. These fall back to the subject.
    {"thread_id": "", "subject": "Info for invoicing", "sent_at": "2026-08-04T10:00"},
    {"thread_id": "", "subject": "Re: Info for invoicing", "sent_at": "2026-08-05T10:00"},
    # The one that used to split: repeated prefixes.
    {"thread_id": "", "subject": "Fwd: Re: Info for invoicing", "sent_at": "2026-08-06T10:00"},
    {"thread_id": "", "subject": "RE: RE: Info for invoicing", "sent_at": "2026-08-07T10:00"},
    # Case and whitespace are not a new conversation.
    {"thread_id": "", "subject": "  INFO FOR INVOICING  ", "sent_at": "2026-08-08T10:00"},
    # A genuinely different subject with no id is genuinely a different thread.
    {"thread_id": "", "subject": "Friday Co", "sent_at": "2026-08-09T10:00"},
    # No subject and no id at all. Must not vanish, and must not join anything named.
    {"thread_id": "", "subject": "", "sent_at": "2026-08-10T10:00"},
    # An OVERLAPPING thread: starts before everything and ends after everything. Without a row
    # like this every thread here is contiguous, so ordering by the first message and ordering by
    # the last give the identical answer — and the ordering assertion below cannot fail however
    # either side is written. That is the same trap as a sort test whose input is pre-sorted, one
    # level up, and it let a mutation reverting the server's sort survive.
    {"thread_id": "t3", "subject": "Long running deal", "sent_at": "2026-07-30T10:00"},
    {"thread_id": "t3", "subject": "Re: Long running deal", "sent_at": "2026-08-11T10:00"},
]


def _python_keys():
    return [cv.thread_key(m) for m in FIXTURE]


def _js_keys(tmp_path):
    import applypilot.web_dashboard as wd
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "keys.mjs"
    from browser_stubs import BROWSER_GLOBALS
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
        f"const ROWS = {json.dumps(FIXTURE)};\n"
        "const F = (new Function(SRC + '; return { threadKey, groupThreads, stripRe };'))();\n"
        "console.log(JSON.stringify({\n"
        "  keys: ROWS.map(F.threadKey),\n"
        "  groups: F.groupThreads(ROWS).map(g => g.id),\n"
        "}));\n", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def js(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not available")
    return _js_keys(tmp_path)


def test_every_message_gets_the_same_thread_key_in_both(js):
    """The assertion that matters. Key by key, not a count — two implementations can produce the
    same NUMBER of groups while putting a message in the wrong one."""
    assert js["keys"] == _python_keys()


def test_the_groups_themselves_match_in_content_and_order(js):
    """Order too. Both sides sort by each thread's FIRST message so a card reads oldest to
    newest, and the two must agree — the browser posts the key it is showing and the server
    resolves it."""
    assert js["groups"] == [g["key"] for g in cv.group_threads(FIXTURE)]


def test_the_fixture_can_tell_the_two_orderings_APART(js):
    """Guards the assertion above from being vacuous.

    With no overlapping thread, sorting by the first message and sorting by the last produce the
    identical list, so both sides could sort either way and still agree. `t3` runs from before
    the earliest message to after the latest, which puts it first under one rule and last under
    the other.
    """
    groups = cv.group_threads(FIXTURE)
    by_first = [g["key"] for g in groups]
    by_last = [g["key"] for g in sorted(
        groups, key=lambda g: str(g["msgs"][-1].get("sent_at") or ""))]
    assert by_first != by_last, "the fixture cannot distinguish the two orderings"
    assert by_first[0] == "t3", "the overlapping thread starts earliest, so it comes first"
    assert by_last[-1] == "t3"


def test_the_fixture_would_actually_catch_a_disagreement(js):
    """A guard against this whole file being vacuous. If every row had a `thread_id` the two
    implementations would agree no matter how the subject fallback was written."""
    keys = _python_keys()
    assert sum(1 for k in keys if k.startswith("subj:")) >= 4, \
        "the fixture no longer exercises the subject fallback, so it proves nothing"
    assert len(set(keys)) < len(keys), "no two rows share a thread — grouping is untested"


def test_repeated_prefixes_land_in_ONE_thread():
    """The disagreement that was live. 'Fwd: Re: X', 'RE: RE: X' and 'X' are one conversation."""
    same = [m for m in FIXTURE if "invoicing" in (m["subject"] or "").lower()]
    assert len({cv.thread_key(m) for m in same}) == 1, \
        "a forwarded message split off into its own thread"
    assert len(same) == 5


def test_an_empty_subject_is_its_own_bucket_and_not_a_catch_all():
    """`subj:` with nothing after it must not swallow rows that DO have a subject — bucketing
    everything unnamed together is the merge this exists to undo."""
    empty = cv.thread_key({"thread_id": "", "subject": ""})
    named = cv.thread_key({"thread_id": "", "subject": "Friday Co"})
    assert empty != named
    assert sum(1 for m in FIXTURE if cv.thread_key(m) == empty) == 1


# ── the composer must actually CARRY the key ────────────────────────────────

def _run_js(tmp_path, tail):
    """Evaluate `dashboard.js` under DOM stubs and run `tail`, which prints one JSON line."""
    import applypilot.web_dashboard as wd
    from browser_stubs import BROWSER_GLOBALS
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "probe.mjs"
    script.write_text(
        BROWSER_GLOBALS
        + "const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},"
          " closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},"
          " getAttribute:()=>null, addEventListener(){}, appendChild(){},"
          " classList:{add(){},remove(){},toggle(){}}, dataset:{} });\n"
          "globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),"
          " querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,"
          " activeElement:null };\n"
        + f"const SRC = {json.dumps(src)};\n" + tail, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


#: A contact with THREE conversations: two answerable, one where only we have written.
#: The newest answerable one is from somebody else entirely, which is why a single composer
#: pinned to the newest thread was the bug.
CONTACT = {
    "id": "c1", "full_name": "Victoria", "email": "v@writer.test",
    "thread": [
        {"thread_id": "deal", "subject": "Ormus <> AMSYS", "sent_at": "2026-08-01T10:00",
         "direction": "out", "from_addr": "me@work.test"},
        {"thread_id": "deal", "subject": "Re: Ormus <> AMSYS", "sent_at": "2026-08-02T10:00",
         "direction": "in", "from_addr": "v@writer.test"},
        {"thread_id": "inv", "subject": "Invoice", "sent_at": "2026-08-05T10:00",
         "direction": "in", "from_addr": "accounts@writer.test"},
        # Nobody has answered this one. It must get NO composer: replying to a thread with no
        # inbound message is a FOLLOW-UP, with its own ladder, schedule and stop conditions.
        {"thread_id": "solo", "subject": "Intro deck", "sent_at": "2026-08-06T10:00",
         "direction": "out", "from_addr": "me@work.test"},
    ],
    "reply_to": {"to": "Accounts <accounts@writer.test>", "to_addr": "accounts@writer.test",
                 "cc": [], "subject": "Re: Invoice", "thread_key": "inv",
                 "thread_subject": "Invoice"},
    "reply_targets": {
        "deal": {"to": "Victoria <v@writer.test>", "to_addr": "v@writer.test", "cc": [],
                 "subject": "Re: Ormus <> AMSYS", "thread_key": "deal",
                 "thread_subject": "Ormus <> AMSYS"},
        "inv": {"to": "Accounts <accounts@writer.test>", "to_addr": "accounts@writer.test",
                "cc": [], "subject": "Re: Invoice", "thread_key": "inv",
                "thread_subject": "Invoice"},
    },
    "conversation": {"state": "awaiting_us", "who": "Victoria"},
}

#: Pull each composer out as DATA rather than slicing the page around a match.
#:
#: Two earlier versions of these assertions did `html.split(marker)[1].split('reply-box')[0]`,
#: which runs straight past the composer into the NEXT thread's messages — so "this box does not
#: mention the other person" was true of a window containing both of them. §Lessons 98: do not
#: slice a guess around a match.
_EXTRACT = r"""
const html = F.conversationView(C);
const boxes = html.split('<div class="reply-box"').slice(1).map(p => {
  // Cut at the REPLY BODY's own closing tag. `lastReplyCard` renders a `said-box` textarea
  // first, so stopping at the first `</textarea>` cuts the segment before the draft even
  // starts -- and every body then reads as empty, which looks exactly like a real bug.
  const at = p.indexOf('class="reply-body"');
  const end = at > -1 ? p.indexOf('</textarea>', at) : p.indexOf('</textarea>');
  const seg = p.slice(0, end > -1 ? end : p.length);
  const th = /data-thread="([^"]*)"/.exec(seg);
  const hd = /class="reply-hdr">([\s\S]*?)<\/div>/.exec(seg);
  const bd = /class="reply-body"[^>]*>([\s\S]*)$/.exec(seg);
  return {thread: th ? th[1] : null, hdr: hd ? hd[1] : '', body: bd ? bd[1] : ''};
});
console.log(JSON.stringify({boxes, marks: (html.match(/th-can/g) || []).length}));
"""


def _composers(tmp_path, open_all=True, draft=None):
    """Every composer on the card. `open_all` expands each thread, which is what a click does —
    composers live INSIDE a thread, so a collapsed one correctly has none."""
    expand = ("F.groupThreads(C.thread).forEach(g => F.CONV_OPEN.add(`${C.id}|${g.id}`));\n"
              if open_all else "")
    setd = f"F.REPLY_DRAFT.set(F.rkey('c1', '{draft}'), 'ONLY THIS ONE');\n" if draft else ""
    return _run_js(tmp_path, f"const C = {json.dumps(CONTACT)};\n"
                   "const F = (new Function(SRC + '; return { conversationView, groupThreads,"
                   " CONV_OPEN, REPLY_DRAFT, rkey };'))();\n" + expand + setd + _EXTRACT)


def test_every_answerable_thread_gets_its_own_composer(js, tmp_path):
    """The report: seven threads, one reply box, six conversations that could be read and not
    answered. Expanded, every answerable thread must now have one of its own."""
    boxes = _composers(tmp_path)["boxes"]
    assert {b["thread"] for b in boxes} == {"deal", "inv"}, \
        f"expected a composer per answerable thread, got {[b['thread'] for b in boxes]}"


def test_a_thread_nobody_answered_gets_NO_composer(js, tmp_path):
    """Replying to a conversation with no inbound message is a FOLLOW-UP — its own ladder, its
    own schedule, its own stop conditions. A reply box there blurs the two.

    Counts as well as names: falling back to the default target renders a THIRD box that is
    labelled with another thread's key, so `"solo" not in threads` stays true while the extra
    composer is right there. A set of names cannot see a duplicate."""
    boxes = _composers(tmp_path)["boxes"]
    assert len(boxes) == 2, f"an unanswerable thread was given a composer: {boxes}"
    assert "solo" not in {b["thread"] for b in boxes}


def test_a_COLLAPSED_answerable_thread_says_so(js, tmp_path):
    """Only one thread is open by default, so without a marker the card still reads as "several
    closed rows and no way to reply" — the exact report this answers. Shipping the composers
    without this would have reproduced it (§Lessons 43)."""
    out = _composers(tmp_path, open_all=False)
    assert len(out["boxes"]) == 1, "only the open thread should hold a composer"
    assert out["marks"] == 2, \
        f"a collapsed thread you can answer gives no sign of it (marks={out['marks']})"


def test_the_mark_is_not_on_every_thread(js, tmp_path):
    """A marker that appears on all three means nothing. Three threads, two answerable."""
    assert len(cv.group_threads(CONTACT["thread"])) == 3
    assert _composers(tmp_path, open_all=False)["marks"] == 2


def test_each_composer_addresses_ITS_OWN_thread(js, tmp_path):
    """The dangerous half. Two boxes that both said "Reply to Victoria" would look right and
    send one of them to the wrong person (§Lessons 29)."""
    boxes = {b["thread"]: b["hdr"] for b in _composers(tmp_path)["boxes"]}
    assert "v@writer.test" in boxes["deal"] and "accounts@writer.test" not in boxes["deal"]
    assert "accounts@writer.test" in boxes["inv"] and "v@writer.test" not in boxes["inv"]


def test_the_composers_do_not_share_a_draft(js, tmp_path):
    """Every piece of composer state was keyed by contact alone. With one box per thread that
    makes them all the same box: type into the deal and the invoice fills in too."""
    boxes = {b["thread"]: b["body"] for b in _composers(tmp_path, draft="deal")["boxes"]}
    assert "ONLY THIS ONE" in boxes["deal"], "the draft did not reach the thread it was for"
    assert "ONLY THIS ONE" not in boxes["inv"], "one draft leaked into every other box"


def test_send_reply_posts_the_thread_it_was_clicked_in(js, tmp_path):
    """The end of the wire. A key rendered into the DOM and never posted is the same as absent."""
    out = _run_js(tmp_path, """
const F = (new Function(SRC + '; return { sendReply, REPLY_DRAFT, rkey };'))();
let posted = null;
// `sendReply` also calls `refresh()`, which fetches with ONE argument. Recording only the call
// that carries a body keeps this probe about the POST rather than the refresh beside it.
globalThis.fetch = async (url, opts) => {
  if (opts && opts.body) posted = {url, body: JSON.parse(opts.body)};
  return { json: async () => ({ok: true, message: 'Sent.'}) };
};
F.REPLY_DRAFT.set(F.rkey('c1', 'deal'), 'hello');
const btn = { disabled:false, textContent:'',
  closest: () => ({ dataset: { cc: '[]', to: 'v@writer.test', thread: 'deal' },
                    querySelector: () => ({ textContent: 'Re: Ormus <> AMSYS' }) }) };
await F.sendReply('c1', 'deal', btn);
console.log(JSON.stringify({posted}));
""")
    assert out["posted"], "sendReply never reached the network"
    assert out["posted"]["body"].get("thread") == "deal", \
        "the browser did not tell the server which conversation it was answering"


def test_draft_reply_carries_the_thread_too(js, tmp_path):
    """`_draft_reply` reads the stored conversation to know what it is answering. Unscoped that
    is every thread merged, so a draft written under one subject answers another."""
    out = _run_js(tmp_path, """
const F = (new Function(SRC + '; return { draftReply };'))();
let posted = null;
globalThis.fetch = async (url, opts) => {
  if (opts && opts.body) posted = {url, body: JSON.parse(opts.body)};
  return { json: async () => ({ok: true, body: 'drafted'}) };
};
const btn = { disabled:false, textContent:'', closest: () => ({ querySelector: () => null }) };
await F.draftReply('c1', 'deal', btn);
console.log(JSON.stringify({posted}));
""")
    assert out["posted"]["body"].get("thread") == "deal"


def test_the_thread_that_OPENS_is_one_you_can_answer(js, tmp_path):
    """The newest thread on a card is often our own unanswered email or a calendar acceptance.
    Opening that one puts the composer the operator was sent to behind a click, under a banner
    reading "your turn". The default is the newest ANSWERABLE thread instead."""
    out = _composers(tmp_path, open_all=False)
    assert [b["thread"] for b in out["boxes"]] == ["inv"], \
        f"the card opened on a thread with no composer: {out['boxes']}"
