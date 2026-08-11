"""The Text tab's phone box is open when there is no phone.

Reported as **"the text feature is not working"**, with a screenshot of the feature working
exactly as designed: composer rendered, disabled, captioned *"Add a phone number below and
Save — then this composer turns on"*, and the `📇 Phone & notes` block directly beneath it
**collapsed**.

    const open = (NOTES_OPEN.has(c.id) || c.phone || c.notes) ? ' open' : '';

The block auto-expanded for anyone who already had a number and stayed shut for everyone who
did not — so the one control the disabled composer points at was hidden in exactly the case
that needs it, and visible only once it no longer matters. Nothing was broken downstream:
`/api/contact/details` exists, `_save_contact_details` writes, and `phone` is on the payload.
The whole defect is which way that ternary runs.

§Lessons 43 / 88 / 89, ninth occurrence and a new variant. Not "a control nobody can find" and
not "a control that isn't a control" — a control whose ONLY signpost is a disabled sibling that
says *below*, pointing at something folded shut. The operator's report is the measurement, and
the DOM saying the block exists is not a rebuttal.

The fix needs a second Set. With one, "never touched" and "closed by hand" are indistinguishable,
so a block defaulting to open springs back open on the next 2.5s refresh and can never be shut.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from browser_stubs import BROWSER_GLOBALS

JS = Path(__file__).resolve().parents[1] / "src/applypilot/static/dashboard.js"


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def _run(body: str, tmp_path) -> dict:
    script = tmp_path / "sms.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),
  querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,
  activeElement:null };
globalThis.alert = () => {};
const SRC = """ + json.dumps(_js()) + """;
const F = (new Function(SRC +
  '; return { contactNotes, onNotesToggle, smsChannel, NOTES_OPEN, NOTES_CLOSED };'))();
""" + body, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


#: A contact in the state from the report: verified email, no phone, nothing typed.
_NOPHONE = "{ id:'c1', full_name:'Rae', phone:'', notes:'', apollo_url:'https://apollo.test/1' }"
_PHONE = "{ id:'c2', full_name:'Dana', phone:'+1 555 0100', notes:'', apollo_url:'' }"


def test_the_phone_box_is_open_when_there_is_no_phone(tmp_path):
    """The composer above it is disabled and says "below". If this is shut, the instruction
    points at nothing and the tab reads as broken — which is how it was reported."""
    out = _run("""
console.log(JSON.stringify({
  noPhone: /<details class="cnotes" open/.test(F.contactNotes(%s)),
  hasPhone: /<details class="cnotes" open/.test(F.contactNotes(%s)),
}));
""" % (_NOPHONE, _PHONE), tmp_path)
    assert out["noPhone"] is True, "the block the disabled composer points at is collapsed"
    # Guard the guard: always-open passes the line above and is a different bug — every contact
    # who already has a number gets a phone form they did not ask for, on every render.
    assert out["hasPhone"] is False, "a contact WITH a number should not have the box expanded"


def test_the_input_and_its_save_are_really_inside_that_block(tmp_path):
    """Open is only worth asserting if the thing revealed is the thing the caption promised.
    A `<details open>` containing no input would satisfy the test above."""
    out = _run("""
const h = F.contactNotes(%s);
console.log(JSON.stringify({
  input: h.includes('class="c-phone"'),
  save: h.includes('saveContactDetails'),
  paste: h.includes('paste from Apollo'),
}));
""" % _NOPHONE, tmp_path)
    assert out == {"input": True, "save": True, "paste": True}


def test_closing_it_by_hand_sticks_across_a_refresh(tmp_path):
    """`refresh()` replaces `#jobs` wholesale every 2.5s. With a single Set the default would
    re-apply on every one of those, so the operator could never shut it — a box that reopens
    itself twice a second is worse than one that stays closed."""
    out = _run("""
const c = %s;
const before = /<details class="cnotes" open/.test(F.contactNotes(c));
F.onNotesToggle({ open:false }, 'c1');            // the operator collapses it
const after = /<details class="cnotes" open/.test(F.contactNotes(c));
F.onNotesToggle({ open:true }, 'c1');             // and opens it again
const reopened = /<details class="cnotes" open/.test(F.contactNotes(c));
console.log(JSON.stringify({ before, after, reopened }));
""" % _NOPHONE, tmp_path)
    assert out["before"] is True
    assert out["after"] is False, "an explicit close is undone by the next render"
    assert out["reopened"] is True


def test_opening_it_by_hand_sticks_for_a_contact_who_has_a_number(tmp_path):
    """The other direction: NOTES_OPEN still has to win, or you cannot edit a stored number."""
    out = _run("""
const c = %s;
const before = /<details class="cnotes" open/.test(F.contactNotes(c));
F.onNotesToggle({ open:true }, 'c2');
console.log(JSON.stringify({ before, after: /<details class="cnotes" open/.test(F.contactNotes(c)) }));
""" % _PHONE, tmp_path)
    assert out == {"before": False, "after": True}


def test_notes_with_no_number_still_open(tmp_path):
    """Typed notes were already a reason to expand and must stay one."""
    out = _run("""
console.log(JSON.stringify({ open: /<details class="cnotes" open/.test(
  F.contactNotes({ id:'c3', full_name:'Ana', phone:'', notes:'left a voicemail', apollo_url:'' })) }));
""", tmp_path)
    assert out["open"] is True


def test_the_two_sets_are_disjoint_after_a_toggle(tmp_path):
    """A contact in both sets is a state with no defined answer. `onNotesToggle` must remove
    from one whenever it adds to the other."""
    out = _run("""
F.onNotesToggle({ open:true }, 'x');  F.onNotesToggle({ open:false }, 'x');
const bothAfterClose = F.NOTES_OPEN.has('x') && F.NOTES_CLOSED.has('x');
F.onNotesToggle({ open:true }, 'x');
const bothAfterOpen = F.NOTES_OPEN.has('x') && F.NOTES_CLOSED.has('x');
console.log(JSON.stringify({ bothAfterClose, bothAfterOpen }));
""", tmp_path)
    assert out == {"bothAfterClose": False, "bothAfterOpen": False}


def test_the_composer_is_still_disabled_without_a_number(tmp_path):
    """The half that was never broken, pinned so fixing the visibility does not switch on a
    composer that cannot send. §Lessons 41: it renders DISABLED rather than being described."""
    out = _run("""
const off = F.smsChannel(%s), on = F.smsChannel(%s);
console.log(JSON.stringify({
  offDisabled: off.includes('disabled'),
  offSaysWhy: off.includes('Add a phone number below'),
  onEnabled: !on.includes('<textarea class="d-sms" rows="3" oninput="updSmsCount(this)" disabled'),
}));
""" % (_NOPHONE, _PHONE), tmp_path)
    assert out["offDisabled"] is True and out["offSaysWhy"] is True
    assert out["onEnabled"] is True


def test_the_caption_and_the_block_cannot_disagree(tmp_path):
    """The caption says "below". If the block ever renders above it, or stops rendering at all,
    the sentence is a lie and this is the only place the two are checked together."""
    out = _run("""
const h = F.smsChannel(%s);
console.log(JSON.stringify({
  hasCaption: h.includes('Add a phone number below and Save'),
  hasBlock: h.includes('class="cnotes"'),
  blockIsBelow: h.indexOf('class="cnotes"') > h.indexOf('Add a phone number below and Save'),
  blockIsOpen: /<details class="cnotes" open/.test(h),
}));
""" % _NOPHONE, tmp_path)
    assert out == {"hasCaption": True, "hasBlock": True,
                   "blockIsBelow": True, "blockIsOpen": True}
