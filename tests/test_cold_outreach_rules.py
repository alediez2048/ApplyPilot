"""The cold-outreach rules every first-contact voice must carry.

Six rules were written into `_PITCH_SYSTEM` and `_PREMISE_SYSTEM` and never into `_SYSTEM`,
which is §Lessons 49's shape with a THIRD copy: a rule implemented at two of its three sites is
not implemented. What made it invisible is that the missing one is the highest-volume voice, and
an email with no explicit out is a perfectly good email — nothing errors and nothing renders
wrong.

Measured on the 149 live sends before the fix, all but two of them written by `_SYSTEM`:

    61%   opened with "I" / "I'm" / "I just applied"   (the CV opening the other two voices ban)
    44%   carried the subject "quick q about the <role> role"
    18%   blew the 120-word cap the other two voices set
    2/149 gave the reader an explicit out
    9     people at Salesforce received a byte-identical subject line

This file is parametrised over the voices rather than written once per prompt, for the reason
§Lessons 102 gives: the fourth voice will be added by somebody who never reads this, and a
hand-written list is what falls behind.
"""
import re

import pytest

from applypilot.networking import outreach


def flat(s: str) -> str:
    """Collapse the prompt's own hand-wrapping before matching a sentence in it.

    Same helper, same reason, as `test_premise_voice.flat`: these are hand-wrapped paragraphs,
    so "I wanted to reach\n     out" does not contain "I wanted to reach out". An assertion
    that passes only while a line happens to break in the right place tests the wrapping.
    """
    return re.sub(r"\s+", " ", s)

#: The three first-contact voices. Follow-up and reply prompts are deliberately NOT here: they
#: are written to someone the sender has already reached, so "lead with them, not your CV" and
#: the subject rules do not apply in the same way.
VOICES = {
    "jobseeker": outreach._SYSTEM,
    "pitch": outreach._PITCH_SYSTEM,
    "premise": outreach._PREMISE_SYSTEM,
}

#: Each rule and the wording that carries it. The alternations are the three voices' ACTUAL
#: phrasings, not a loose catch-all: deleting the line from any one prompt has to fail this.
RULES = {
    "hard word cap": r"under 120 words",
    "exactly one question": r"exactly ONE question",
    "explicit out": r"explicit out",
    "ban the 'quick question' subject": r'"quick question"',
    "no urgency or flattery": r"no urgency, no scarcity",
    "no em dash": r"NEVER use an em dash",
    "several people at one company": r"SEVERAL PEOPLE AT THE SAME",
    # Each voice says this its own way because each has a different first sentence to protect:
    # the pitch has no posting, the premise has no posting, and the job seeker MUST name the
    # role (the user prompt orders it). Copying the pitch wording verbatim into `_SYSTEM` would
    # have contradicted that instruction, which is §Lessons 40 rather than a wording problem.
    "lead with them, not the CV": r"Lead with THEM|Aim it at THEM|Open on the ROLE and on THEM",
}


@pytest.mark.parametrize("voice", sorted(VOICES))
@pytest.mark.parametrize("rule", sorted(RULES))
def test_every_first_contact_voice_carries_every_cold_outreach_rule(voice, rule):
    assert re.search(RULES[rule], flat(VOICES[voice]), re.I), (
        f"{voice} is missing the rule: {rule}. Six of these were absent from the job-seeker "
        f"voice for the whole life of the product, and it wrote 147 of 149 sent emails."
    )


def test_the_abbreviation_is_banned_too_not_only_the_full_phrase():
    """`"quick question"` alone does not cover it, and the live data is the argument.

    Both older voices banned the phrase `quick question`. The job-seeker voice banned nothing
    and produced `quick q about the <role> role` on 67 of 149 sends. A ban on the long form is
    not a ban on the form the model actually writes.
    """
    assert '"quick q"' in flat(outreach._SYSTEM)


def _burned_openers_clause() -> str:
    """The burned-opener LIST, isolated from the rest of the prompt.

    Scoped deliberately rather than matched against the whole prompt. "I'm really excited
    about" also appears in the sentence EXPLAINING why it is banned, so `opener in prompt`
    passes with the ban list deleted outright — a mutation dropping the list survived exactly
    that way (§Lessons 1, and §Lessons 71's tell: an assertion that still passes when the thing
    under test is emptied).

    The marker is asserted before the split, so a reworded prompt fails here loudly instead of
    silently returning an empty window to match against (§Lessons 98).
    """
    text = flat(outreach._SYSTEM)
    marker = "These openers are burned"
    end = "- Then ONE real"
    assert marker in text, "the burned-opener list is gone, or its wording moved"
    assert end in text, "the clause after the burned list moved; the window would run on"
    return text.split(marker, 1)[1].split(end, 1)[0]


def test_the_burned_openers_are_named_rather_than_described():
    """§Lessons 42: naming the phrasing as burned is what worked; describing the shape was not.
    The prompt already does this for sign-offs, and these are the openers 61% of live sends
    used."""
    clause = _burned_openers_clause()
    for opener in ("I'm really excited about", "I wanted to reach out",
                   # Generated by the live model AFTER the first version of this ban, which
                   # said "or a near-synonym" and was not enough on its own (§Lessons 42).
                   "I applied for the role and wanted to reach out"):
        assert opener in clause, f"{opener!r} is no longer named as burned"


def test_the_subject_frame_is_banned_as_a_property_not_as_a_specimen():
    """A subject built by dropping the role into a fixed frame is anybody's email: 67 of 149
    live sends used one shape and the Affirm and Saronic clusters used another.

    The rule must be stated WITHOUT quoting either frame. The first version of this fix quoted
    both, and `test_burned_copy.test_the_subject_example_is_gone` failed it correctly: that
    exact string had already been deleted from this prompt once, after it produced ten
    identical subject lines at Google. §Lessons 42 — the prompt's own example comes back
    verbatim, including when the example is the thing being forbidden.
    """
    spec = flat(outreach._SYSTEM)
    assert "fixed frame with the role name dropped into a slot" in spec
    assert "any other candidate" in spec, "the discriminator that replaced the specimen is gone"
    assert "quick q about the" not in spec.lower(), "a banned specimen is still a specimen"


def test_the_job_seeker_voice_still_orders_the_role_named_in_the_first_sentence():
    """The guard on the fix: `_PITCH_SYSTEM` says the first sentence must be about their
    COMPANY, and the job-seeker user prompt orders the ROLE named in the first sentence. If a
    later edit pastes the pitch wording in verbatim, the two instructions disagree and the
    heading wins (§Lessons 40). The adapted wording has to keep the role in sentence one."""
    assert "Name the specific" in flat(outreach._SYSTEM)
    assert "first sentence" in flat(outreach._SYSTEM)
    assert "first sentence must be about their company" not in flat(outreach._SYSTEM)
