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
    """Order too: the composer anchors under the LAST group, so a different order is a different
    default thread."""
    assert js["groups"] == [g["key"] for g in cv.group_threads(FIXTURE)]


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


#: A contact with TWO conversations and a reply target on the older one.
CONTACT = {
    "id": "c1", "full_name": "Victoria", "email": "v@writer.test",
    "thread": [
        {"thread_id": "deal", "subject": "Ormus <> AMSYS", "sent_at": "2026-08-01T10:00",
         "direction": "out", "from_addr": "me@work.test"},
        {"thread_id": "deal", "subject": "Re: Ormus <> AMSYS", "sent_at": "2026-08-02T10:00",
         "direction": "in", "from_addr": "v@writer.test"},
        {"thread_id": "inv", "subject": "Invoice", "sent_at": "2026-08-05T10:00",
         "direction": "in", "from_addr": "accounts@writer.test"},
    ],
    "reply_to": {"to": "Victoria <v@writer.test>", "to_addr": "v@writer.test", "cc": [],
                 "subject": "Re: Ormus <> AMSYS", "thread_key": "deal",
                 "thread_subject": "Ormus <> AMSYS"},
    "conversation": {"state": "awaiting_us", "who": "Victoria"},
}


def test_the_composer_carries_the_thread_key(js, tmp_path):
    """`sendReply` reads `card.dataset.thread`. If `replyBox` does not write it, the browser
    posts nothing, the server falls back to the newest inbound across every thread, and the
    misaddressing is back with every Python test still green."""
    out = _run_js(tmp_path, f"const C = {json.dumps(CONTACT)};\n"
                  "const F = (new Function(SRC + '; return { replyBox };'))();\n"
                  "const html = F.replyBox(C);\n"
                  "console.log(JSON.stringify({html}));\n")
    assert 'data-thread="deal"' in out["html"], \
        "the composer does not carry the thread it is pointed at"


def test_the_composer_names_the_conversation_when_there_is_more_than_one(js, tmp_path):
    """One reply box, several threads. Without this there is nothing on screen distinguishing
    'answering the deal' from 'answering an invoice' (§Lessons 29)."""
    one = dict(CONTACT, thread=CONTACT["thread"][:2])
    out = _run_js(tmp_path, f"const C = {json.dumps(CONTACT)}; const O = {json.dumps(one)};\n"
                  "const F = (new Function(SRC + '; return { replyBox };'))();\n"
                  "console.log(JSON.stringify({many: F.replyBox(C), one: F.replyBox(O)}));\n")
    assert "reply-in" in out["many"] and "Ormus &lt;&gt; AMSYS" in out["many"]
    # Silent in the ordinary case: with one conversation there is nothing to disambiguate.
    assert "reply-in" not in out["one"]


def test_send_reply_posts_the_thread(js, tmp_path):
    """The end of the wire. A key rendered into the DOM and never posted is the same as absent."""
    out = _run_js(tmp_path, """
const F = (new Function(SRC + '; return { sendReply, REPLY_DRAFT };'))();
let posted = null;
// `sendReply` also calls `refresh()`, which fetches with ONE argument. Recording only the call
// that carries a body keeps this probe about the POST rather than about the refresh beside it.
globalThis.fetch = async (url, opts) => {
  if (opts && opts.body) posted = {url, body: JSON.parse(opts.body)};
  return { json: async () => ({ok: true, message: 'Sent.'}) };
};
F.REPLY_DRAFT.set('c1', 'hello');
const btn = { disabled:false, textContent:'',
  closest: () => ({ dataset: { cc: '[]', to: 'v@writer.test', thread: 'deal' } }) };
await F.sendReply('c1', btn);
console.log(JSON.stringify({posted}));
""")
    assert out["posted"], "sendReply never reached the network"
    assert out["posted"]["body"].get("thread") == "deal", \
        "the browser did not tell the server which conversation it was answering"
