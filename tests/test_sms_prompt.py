"""The SMS prompt — the channel where getting it wrong costs the relationship, not the reply.

A text interrupts. It arrives on a lock screen, at whatever hour it is sent, next to messages
from their family, and in most cases the number came from a data tool rather than from the
recipient. Everything asserted here follows from that: the message has to identify the sender,
concede the channel, and be trivially ignorable — and it must NEVER read as escalation.

These drive the real prompt-assembly path with the LLM client stubbed, so what is checked is
what the model is actually told, not a paraphrase of it.
"""

from __future__ import annotations

import pytest

from applypilot.networking import outreach

PROFILE = {"personal": {"name": "Alejandro Diez"}}
JOB = {"url": "http://j/1", "title": "Applied AI Engineer", "company": "Saronic",
       "applied_at": "2026-07-20T10:00:00+00:00"}


@pytest.fixture()
def prompt(monkeypatch):
    """Capture the user prompt and return a canned draft, so no network is touched."""
    seen = {}

    class FakeClient:
        def chat(self, messages, **kw):
            seen["system"] = messages[0]["content"]
            seen["user"] = messages[1]["content"]
            return '{"message": "Hi there, this is a stub."}'

    monkeypatch.setattr(outreach, "get_client", lambda *_a, **_k: FakeClient())
    return seen


def _contact(**over):
    c = {"id": "c1", "full_name": "Blake Reed", "title": "Talent Partner",
         "company": "Saronic", "email": "blake@saronic.com", "phone": "+1 555 0100",
         "sent_message_id": "", "dm_status": "", "replied_at": "", "submitted_at": "",
         "outreach_subject": "", "outreach_message": ""}
    c.update(over)
    return c


# ── the rules that make a text defensible at all ────────────────────────────

def test_the_system_prompt_states_the_sender_lacks_permission():
    """The whole design rests on this being said out loud rather than implied.

    A prompt that only says "be respectful" produces a polite sales text. Naming the actual
    situation — the number came from a tool, not from them — is what changes the copy.
    """
    s = outreach._SMS_SYSTEM.lower()
    assert "not given permission" in s or "was not given permission" in s
    assert "lock screen" in s, "the prompt should say WHY a text is different, not just that it is"


def test_links_are_forbidden_and_the_deck_is_never_offered(prompt):
    """A URL from an unrecognised number is the strongest spam signal that exists, and unlike
    LinkedIn's link penalty this one costs the whole conversation rather than some reach.

    `_intro_deck_url` is never consulted on this path — every OTHER channel offers the deck,
    so the omission has to be deliberate and checked, not assumed.
    """
    outreach.draft_sms(PROFILE, JOB, _contact(), touch=0)
    assert "NO LINKS" in prompt["system"]
    assert "intro" not in prompt["user"].lower(), "the deck leaked into a text message"
    assert "http" not in prompt["user"], "a URL reached the SMS prompt"


def test_the_burned_phrasings_are_named_so_they_stop_being_parroted():
    """§Lessons 9, reproduced exactly: an illustrative phrase in the prompt came back verbatim
    in all five generated drafts. Several people at ONE company get texted, so a stock sentence
    across them proves a machine wrote it — worse than omitting the sentence entirely."""
    s = outreach._SMS_SYSTEM.lower()
    assert "hope a text is okay" in s and "burned" in s, \
        "the parroted phrasings must be named as forbidden, not merely removed"
    assert "phrase it differently every time" in s


# ── standing: the biggest lever on the copy ─────────────────────────────────

# The marker for each tier must be UNIQUE, not a prefix of another. `"WEAK" in "WEAKEST…"` is
# true, so grading the invited case down to WEAKEST left this test green — §Lessons 1, inside
# the test written to guard the grading. Each expectation below names something only its own
# tier says.
@pytest.mark.parametrize("over,expect", [
    ({}, "TREAT WITH CARE"),
    ({"dm_status": "sent"}, "Only a LinkedIn invite"),
    ({"sent_message_id": "g1"}, "An email went out"),
    ({"sent_message_id": "g1", "dm_status": "manual"}, "Two channels of silence"),
    ({"replied_at": "2026-07-29T00:00:00+00:00"}, "STRONGEST FOOTING"),
])
def test_standing_is_graded_not_a_boolean(over, expect):
    """It used to be "did we email them", which collapsed five different situations into two.

    The cold case — no email, no LinkedIn, nothing — is the one that most needs the copy to
    work hardest, and it was previously indistinguishable from having only sent an invite.
    """
    assert expect in outreach._sms_permission(_contact(**over))


def test_every_standing_tier_says_something_different():
    """Five tiers that render the same text are one tier with extra branches. Compares the
    whole strings, so downgrading a tier to reuse another's wording fails here."""
    tiers = [
        outreach._sms_permission(_contact()),
        outreach._sms_permission(_contact(dm_status="sent")),
        outreach._sms_permission(_contact(sent_message_id="g1")),
        outreach._sms_permission(_contact(sent_message_id="g1", dm_status="manual")),
        outreach._sms_permission(_contact(replied_at="2026-07-29T00:00:00+00:00")),
    ]
    assert len(set(tiers)) == 5, "two standing tiers produce identical guidance"
    # Severity must be graded too, not just worded differently. Exactly ONE tier is the
    # weakest — the fully cold one — and exactly one is the strongest. A mutation that relabels
    # the invited case "WEAKEST" while keeping its own wording survived every check above.
    assert sum("WEAKEST" in t for t in tiers) == 1, \
        "more than one tier claims to be the weakest; the grading is decorative"
    assert "WEAKEST" in tiers[0], "the fully cold case must be the weakest standing"
    assert sum("STRONGEST" in t for t in tiers) == 1


def test_a_cold_contact_is_told_to_be_the_shortest_and_least_charming():
    cold = outreach._sms_permission(_contact())
    assert "NO prior contact" in cold
    assert "do not sell" in cold.lower() and "do not be charming" in cold.lower()


# ── the replied case: a different message, not a variant ────────────────────

THREAD = [
    {"direction": "out", "from_addr": "me@x.com", "snippet": "Hi Victoria, I just applied…",
     "sent_at": "2026-07-28T09:00:00+00:00"},
    {"direction": "in", "from_addr": "v@writer.com", "from_name": "Victoria Shearer",
     "snippet": "Thank you for reaching out. Looping in David, who manages this role.",
     "sent_at": "2026-07-29T09:00:00+00:00"},
]


def test_a_contact_who_replied_gets_their_words_in_the_prompt(prompt):
    """The bug this pins, which shipped twice in one sitting and both times looked fine.

    `conversation_transcript` uses the thread ONLY for the replier's name and date — the words
    must be handed to it separately as `their_reply`. Passing the thread alone renders the
    sender's own email and nothing else, and the model answered "only Alejandro's initial email
    is shown" rather than inventing a continuation. It was right; the prompt was wrong.
    """
    c = _contact(full_name="Victoria Shearer", replied_at="2026-07-29T09:00:00+00:00",
                 outreach_message="Hi Victoria, I just applied for the role.")
    outreach.draft_sms(PROFILE, JOB, c, touch=0, thread=THREAD)
    assert "Looping in David" in prompt["user"], \
        "their reply never reached the prompt — the draft can only restate, not continue"


def test_a_contact_who_replied_is_never_told_to_earn_the_channel(prompt):
    """The touch ladder describes COLD outreach — earn the channel, give the touchpoint, ask a
    yes/no. Every one of those is wrong for someone who answered, and because the ladder arrives
    under the heading "THIS MESSAGE:" it BEAT the standing block: drafts for a contact who had
    replied still asked whether the email had arrived. A contradiction in a prompt is not fixed
    by saying the other side louder, so the ladder is replaced rather than appended to.
    """
    c = _contact(replied_at="2026-07-29T09:00:00+00:00")
    outreach.draft_sms(PROFILE, JOB, c, touch=0, thread=THREAD)
    user = prompt["user"]
    assert "CONTINUATION of a live conversation" in user
    assert "FIRST text" not in user, "cold-outreach instructions reached a live conversation"
    assert "never ask" in user.lower() and "arrived" in user.lower()


def test_no_reply_text_means_say_so_rather_than_guess(prompt):
    """`gmail.metadata` yields headers with no body, so a thread can exist with every snippet
    empty. That is indistinguishable from no thread for this purpose and must be treated as
    such — the same principle `_draft_reply` enforces by refusing outright."""
    c = _contact(replied_at="2026-07-29T09:00:00+00:00")
    headers_only = [{"direction": "in", "from_addr": "v@writer.com", "snippet": ""}]
    outreach.draft_sms(PROFILE, JOB, c, touch=0, thread=headers_only)
    assert "do NOT have the text of their reply" in prompt["user"]
    assert "THE CONVERSATION SO FAR" not in prompt["user"], \
        "offered an empty transcript, which reads as 'they said nothing'"


# ── the two dates that are easy to confuse ──────────────────────────────────

def test_the_application_date_is_the_jobs_not_the_emails(prompt):
    """`contact.submitted_at` is when the OUTREACH EMAIL went out (the email ladder anchors on
    it); the job was applied to on `job.applied_at`. Labelling the email date "applied" puts a
    checkable factual error in a message to the one person positioned to check it."""
    c = _contact(submitted_at="2026-07-30T00:00:00+00:00", sent_message_id="g1")
    outreach.draft_sms(PROFILE, JOB, c, touch=0)
    user = prompt["user"]
    assert "applied 2026-07-20" in user, "the job's own application date was not used"
    assert "applied 2026-07-30" not in user, "the email send date was labelled as the apply date"


def test_the_ladder_is_short_and_the_last_touch_says_it_is_last():
    """Three touches is normal in email and reads as harassment on a phone, because each one
    interrupts. Two, slower, and the final one announces itself so they feel no obligation."""
    from applypilot.domain.followup import SMS
    assert len(SMS.default_schedule) == 2
    assert SMS.default_schedule[0] >= 48, "a text at 24h is pressure, not a follow-up"
    last = outreach._SMS_TOUCH_INTENT[2].lower()
    assert "final" in last and "last one" in last, "the closing text must announce itself"
    assert "ask nothing" in last, "a final text that asks a question is not a close"
    assert SMS.can_autosend is False, "a text must never be sent by the machine"
