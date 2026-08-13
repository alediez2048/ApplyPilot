"""What has already happened comes FIRST on an email card, sent or received.

`hasConversation()` asks whether an INBOUND message exists, so a person we had emailed and who
had not answered opened on a compose box with their own draft in it. Measured on the live
database: **126 of 141 emailed contacts** rendered that way.

Reported after writing to somebody at SpaceX who had already been contacted: opening the card
showed a subject box and a body, which reads as "here is what to send" rather than "here is what
you sent" — so the record of the first email was the one thing not on screen.

§Lessons 31 recorded exactly this for the REPLIED case — *"a contact who has REPLIED gets a
conversation, not a form"* — and the fix was applied only there. The rule was always about
whether an email had GONE OUT, not about whether one came back (§Lessons 49, half a rule).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run(tmp_path, tail):
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


@pytest.fixture(autouse=True)
def node():
    if not shutil.which("node"):
        pytest.skip("node not available")


#: Joshua's card: emailed today, no answer yet. This is the 126-contact case.
EMAILED = {
    "id": "c1", "full_name": "Joshua Yi", "email": "joshua@rocket.test", "emailed": True,
    "outreach_subject": "Curious about the Operations Engineer role",
    "outreach_message": "Hey Joshua, I recently applied for the Operations Engineer role...",
    "thread": [
        {"thread_id": "t1", "subject": "Curious about the Operations Engineer role",
         "sent_at": "2026-08-13T20:03", "direction": "out", "from_addr": "me@work.test",
         "snippet": "Hey Joshua, I recently applied for the Operations Engineer role..."},
    ],
    "reply_targets": {}, "reply_to": None, "conversation": None,
}

#: Nobody has been written to at all. The compose box IS the right answer here.
FRESH = {
    "id": "c2", "full_name": "Rey Rodriguez", "email": "rey@rocket.test", "emailed": False,
    "outreach_subject": "Curious about the role", "outreach_message": "Hey Rey, ...",
    "thread": [], "reply_targets": {}, "reply_to": None, "conversation": None,
}


def _html(tmp_path, contact):
    return _run(tmp_path, f"const C = {json.dumps(contact)};\n"
                "const F = (new Function(SRC + '; return { emailChannel };'))();\n"
                "console.log(JSON.stringify({html: F.emailChannel(C)}));\n")["html"]


def test_an_already_emailed_contact_shows_what_was_SENT(tmp_path):
    """The report, in one assertion. Opening this card must show the email that went out."""
    html = _html(tmp_path, EMAILED)
    assert "conv-msgs" in html, "the card still opens on a form with no record of the send"
    assert "Curious about the Operations Engineer role" in html


def test_the_sent_email_is_not_ALSO_repeated_as_a_compose_box(tmp_path):
    """Showing it twice — once as history, once as a disabled form — is the same confusion in a
    longer page. The body belongs to the history now."""
    html = _html(tmp_path, EMAILED)
    assert 'class="d-body"' not in html, "the sent draft is still rendered as a compose box"
    assert 'class="d-subj"' not in html


def test_the_actions_of_a_sent_draft_SURVIVE(tmp_path):
    """Dropping the whole block would take the follow-up button and the sent tag with it —
    those are the parts of an already-sent draft still worth having."""
    html = _html(tmp_path, dict(EMAILED, followup_due=True))
    assert "Gmail sent" in html, "the card no longer says the email went out"
    assert "Mark followed up" in html, "the follow-up action was lost with the form"


def test_a_contact_nobody_has_written_to_still_gets_the_compose_box(tmp_path):
    """The negative case. Without it, "show history instead of a form" passes by rendering a
    form for nobody — and a fresh contact would have no way to send a first email."""
    html = _html(tmp_path, FRESH)
    assert 'class="d-body"' in html, "a brand-new contact lost its compose box"
    assert "conv-msgs" not in html, "an empty history rendered anyway"


def test_history_sits_ABOVE_the_action(tmp_path):
    """Asked for explicitly: "this maybe should go on top of the draft box". Order is the whole
    point — a record of the send underneath a compose box is read after the decision."""
    html = _html(tmp_path, dict(EMAILED, emailed=False, followup_state="due",
                                followup_message="Following up on my note..."))
    assert html.index("conv-msgs") < html.index("Following up on my note"), \
        "the follow-up draft renders above the conversation it is following up on"


def test_a_replied_contact_is_unchanged(tmp_path):
    """The path §Lessons 31 already fixed must not regress: with an inbound message the
    composers live inside the history, and no compose box is appended below it."""
    replied = dict(EMAILED)
    replied["thread"] = EMAILED["thread"] + [
        {"thread_id": "t1", "subject": "Re: Curious about the Operations Engineer role",
         "sent_at": "2026-08-14T09:00", "direction": "in", "from_addr": "joshua@rocket.test",
         "snippet": "happy to chat"}]
    replied["reply_targets"] = {"t1": {"to": "Joshua <joshua@rocket.test>",
                                       "to_addr": "joshua@rocket.test", "cc": [],
                                       "subject": "Re: Curious", "thread_key": "t1",
                                       "thread_subject": "Curious"}}
    replied["conversation"] = {"state": "awaiting_us", "who": "Joshua"}
    html = _html(tmp_path, replied)
    assert "conv-msgs" in html and "reply-box" in html
    assert 'class="d-body"' not in html, "a compose box was appended under a live conversation"


# ── the same person on a SECOND role's card ─────────────────────────────────

#: Patrick, live: `contact_id` hashes the job url, so being found for a second Google role made
#: a second row with an empty history — and opening it offered a compose box to somebody nine
#: messages into a conversation with three replies already in it.
BORROWED = {
    "id": "c3", "full_name": "Patrick Omalley", "email": "p@bigco.test", "emailed": False,
    "outreach_subject": "quick q about the AI Sales Specialist role",
    "outreach_message": "Hey Patrick, I recently applied...",
    "thread_from": "http://j/other-role",
    "thread": [
        {"thread_id": "t9", "subject": "quick q about the Startups Performance Lead",
         "sent_at": "2026-08-04T13:49", "direction": "out", "from_addr": "me@work.test",
         "snippet": "quick q", "from_other_role": "http://j/other-role"},
        {"thread_id": "t9", "subject": "Re: quick q about the Startups Performance Lead",
         "sent_at": "2026-08-07T08:32", "direction": "in", "from_addr": "p@bigco.test",
         "snippet": "happy to help", "from_other_role": "http://j/other-role"},
    ],
    "reply_targets": {}, "reply_to": None,
    "conversation": {"state": "awaiting_them", "who": "Patrick"},
}


def test_a_second_card_for_the_same_person_shows_the_conversation(tmp_path):
    """The report: opening the second Google card offered a fresh cold email to somebody
    already mid-conversation."""
    html = _html(tmp_path, BORROWED)
    assert "conv-msgs" in html, "the borrowed conversation is not shown"
    assert "Startups Performance Lead" in html


def test_it_says_the_conversation_belongs_ELSEWHERE(tmp_path):
    """Showing another role's correspondence as this card's own is a quieter version of the
    same confusion — the operator would reply from the wrong place."""
    html = _html(tmp_path, BORROWED)
    assert "borrowed" in html
    assert "already in touch" in html
    assert "1 from them" in html, "the banner does not say they have actually replied"


def test_no_compose_box_on_a_borrowed_conversation(tmp_path):
    """The compose box IS the bug. And a reply box would be worse than useless here: sending
    resolves recipients from this row's own messages, of which there are none, so it would
    render, look right and refuse on click."""
    html = _html(tmp_path, BORROWED)
    assert 'class="d-body"' not in html, "still offering a fresh cold email"
    assert "reply-box" not in html, "a composer that cannot send was rendered"


def test_the_banner_comes_FIRST(tmp_path):
    """Under the history it would be read after the decision, which is the whole failure."""
    html = _html(tmp_path, BORROWED)
    assert html.index("borrowed") < html.index("conv-msgs")


def test_a_card_that_OWNS_its_conversation_gets_no_banner(tmp_path):
    """The negative case. Without it, "warn about borrowed threads" passes by warning on
    every card — and a banner that is always there is one nobody reads."""
    html = _html(tmp_path, EMAILED)
    assert "borrowed" not in html
