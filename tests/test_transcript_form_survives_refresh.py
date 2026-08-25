"""What the operator pasted into the meeting form must survive the 2.5s refresh.

Reported as "when I save a transcript nothing gets saved on the meeting tab". The server was
never the problem: the endpoint, the store, the payload and the render all worked when driven
directly. What the database showed was the diagnosis — **two transcript saves have EVER reached
this server**, one of them a probe. Every real attempt died in the browser with no request, no
error and no row.

`#jobs` is replaced wholesale every 2.5 seconds. The refresh guards protect a field that has
FOCUS, which is not the same as a field with CONTENT: switching to Granola to copy the summary
takes focus off the page, the next tick rebuilds the form empty, and `saveTranscript` then
returns at its own `if (!body.trim())` guard without posting — leaving "Paste the transcript
first." beside a form that looks ready, and destroying the paste.

`REPLY_DRAFT` had solved exactly this for the reply composer; this form never got it.
"""
import json
import pathlib
import subprocess

from applypilot import web_dashboard as wd

from test_conversation_completeness import BROWSER_GLOBALS

SRC = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

CONTACT = {"id": "c1", "full_name": "Waheed Brown", "transcripts": []}


def _run(tmp_path, script_body):
    src = pathlib.Path(tmp_path) / "tr.mjs"
    src.write_text(BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),
  querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,
  activeElement:null };
const SRC = """ + json.dumps(SRC) + """;
const F = (new Function(SRC + '; return { transcriptSection, TR_OPEN, TR_DRAFT, trSet, trDraft };'))();
""" + script_body, encoding="utf-8")
    p = subprocess.run(["node", str(src)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[:2000]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_a_pasted_transcript_is_still_there_after_a_rerender(tmp_path):
    """The whole bug, executed: paste, let the table rebuild, and the text must survive."""
    out = _run(tmp_path, """
const c = """ + json.dumps(CONTACT) + """;
F.TR_OPEN.add('c1');
F.trSet('c1', 'body', 'Waheed and I talked about the hackathon.');
F.trSet('c1', 'title', 'ZZ-KEPT-TITLE');
F.trSet('c1', 'summary', 'ZZ-KEPT-SUMMARY');
F.trSet('c1', 'date', '2026-08-21');
const html = F.transcriptSection(c);          // <- the 2.5s rebuild
console.log(JSON.stringify({
  keepsBody: html.includes('Waheed and I talked about the hackathon.'),
  keepsTitle: html.includes('ZZ-KEPT-TITLE'),
  keepsSummary: html.includes('ZZ-KEPT-SUMMARY'),
  keepsDate: html.includes('2026-08-21'),
  writesBack: html.includes("trSet('c1','body'"),
}));
""")
    assert out["keepsBody"], "the pasted transcript was wiped by the rebuild"
    # Distinctive values on purpose: "Intro call" is also in the field's PLACEHOLDER, so the
    # first version of this assertion passed with the `value` attribute blanked entirely
    # (§Lessons 71 — an assertion that still passes when the thing under test is emptied).
    assert out["keepsTitle"], "the title was wiped by the rebuild"
    assert out["keepsSummary"], "the summary was wiped by the rebuild"
    assert out["keepsDate"], "the date was wiped by the rebuild"
    assert out["writesBack"], "typing is not recorded outside the DOM, so it will be lost again"


def test_an_untouched_form_renders_empty(tmp_path):
    """Guard the guard: the fields must not carry another contact's paste, or the store is worse
    than the bug it fixes."""
    out = _run(tmp_path, """
const c = """ + json.dumps(CONTACT) + """;
F.TR_OPEN.add('c1');
F.trSet('other', 'body', 'SOMEONE ELSES MEETING');
const html = F.transcriptSection(c);
console.log(JSON.stringify({ leaked: html.includes('SOMEONE ELSES MEETING') }));
""")
    assert out["leaked"] is False, "the form showed a different contact's transcript"


def test_the_draft_store_is_keyed_per_contact(tmp_path):
    out = _run(tmp_path, """
F.trSet('a', 'body', 'AAA');
F.trSet('b', 'body', 'BBB');
console.log(JSON.stringify({ a: F.trDraft('a').body, b: F.trDraft('b').body,
                             missing: F.trDraft('zz').body }));
""")
    assert out["a"] == "AAA" and out["b"] == "BBB"
    assert out["missing"] == "", "an unknown contact must start empty, never undefined"


def test_save_reads_the_store_when_the_dom_is_empty():
    """A rebuild landing between the paste and the click leaves an empty textarea. Reading only
    the DOM is precisely what dropped the save silently."""
    body = SRC[SRC.index("async function saveTranscript"):]
    body = body[:body.index("\n}")]
    assert "kept.body" in body, "saveTranscript still trusts the DOM alone"
    assert "trDraft(cid)" in body


def test_a_failed_save_keeps_the_paste():
    """It may be the only copy the operator has in hand. Cleared only on success."""
    body = SRC[SRC.index("async function saveTranscript"):]
    body = body[:body.index("\n}")]
    fail_branch = body[body.index("if (!r.ok)"):]
    assert "TR_DRAFT.delete" not in fail_branch.split("\n")[0], "the failure path clears the paste"
    assert "TR_DRAFT.delete(cid)" in body, "a successful save must clear the form"
