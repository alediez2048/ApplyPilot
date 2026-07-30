"""Every outreach EMAIL offers the intro deck; no LinkedIn note does.

Asked for 2026-07-29: "for all future emails can you also add this url and refer to it as
'heres a good intro deck we could go over during the call'".

The prompt asks the model for it, but a prompt instruction is not a guarantee — this codebase
has three separate incidents of the model ignoring or mangling explicit prompt instructions
(§Lessons 9, 12). So `ensure_intro_deck()` makes it true after the fact, and these tests pin
the guarantee rather than the request.

LinkedIn notes are deliberately excluded: they are capped at 300 chars and LinkedIn
strips/penalizes links in connect notes — the same reasoning that already keeps the scheduling
link out of them.
"""

from __future__ import annotations

import pytest

from applypilot.networking import outreach

URL = "https://www.jorgealejandrodiez.com/intro/"


class _C:
    """Minimal LLM stub. `payload` is returned verbatim as the model's JSON."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.seen: list[str] = []

    def chat(self, msgs, **kw):
        self.seen.append(msgs[-1]["content"])
        return self.payload


JOB = {"title": "AI Engineer", "company": "Zello", "full_description": "Build AI systems."}
CONTACT = {"full_name": "PJ", "title": "Recruiting", "company": "Zello",
           "match_reason": "recruiter", "email": "pj@zello.com"}
PROFILE = {"personal": {"full_name": "Alejandro Diez", "preferred_name": "Alejandro",
                        "intro_deck_url": URL}}


# ── ensure_intro_deck(): the guarantee ───────────────────────────────────────────────────

def test_a_body_that_omits_the_link_gets_the_exact_sentence():
    body = "Hi PJ,\n\nI applied for the AI Engineer role.\n\nThanks,\nAlejandro"
    out = outreach.ensure_intro_deck(body, URL)
    assert URL in out
    assert "Here's a good intro deck we could go over during the call" in out


def test_the_appended_link_sits_above_the_signoff_not_below_it():
    """A URL under "Thanks, Alejandro" reads as a footer and gets skimmed past."""
    body = "Hi PJ,\n\nI applied for the AI Engineer role.\n\nThanks,\nAlejandro"
    out = outreach.ensure_intro_deck(body, URL)
    assert out.index(URL) < out.index("Thanks,"), f"link landed below the sign-off:\n{out}"
    assert out.rstrip().endswith("Alejandro"), f"the sign-off must stay last:\n{out}"


def test_a_body_that_already_has_the_link_is_left_alone():
    """Idempotent: the model's own natural phrasing is better than an appended sentence, and
    two copies of the same URL in one short email looks broken."""
    body = (f"Hi PJ,\n\nI put together a short deck: {URL} — happy to walk through it.\n\n"
            "Thanks,\nAlejandro")
    assert outreach.ensure_intro_deck(body, URL) == body
    assert outreach.ensure_intro_deck(body, URL).count(URL) == 1


def test_a_trailing_slash_difference_is_not_treated_as_a_missing_link():
    """The model may drop the trailing slash. Appending a second near-identical URL is worse
    than accepting the one it wrote."""
    body = "Hi PJ,\n\nDeck: https://www.jorgealejandrodiez.com/intro\n\nThanks,\nAlejandro"
    out = outreach.ensure_intro_deck(body, URL)
    assert out.count("jorgealejandrodiez.com/intro") == 1, out


def test_a_link_split_across_lines_still_counts_as_present():
    """Wrapped text is a formatting artifact, not a missing link."""
    body = f"Hi PJ,\n\nDeck:\n{URL}\n\nThanks,\nAlejandro"
    assert outreach.ensure_intro_deck(body, URL).count(URL) == 1


def test_no_url_configured_changes_nothing():
    body = "Hi PJ,\n\nShort note.\n\nThanks,\nAlejandro"
    assert outreach.ensure_intro_deck(body, "") == body


def test_a_body_with_no_signoff_gets_the_sentence_appended():
    body = "Hi PJ, quick question about the AI Engineer role."
    out = outreach.ensure_intro_deck(body, URL)
    assert out.endswith(URL), out


# ── draft_email / draft_followup: end to end ─────────────────────────────────────────────

def test_the_cold_email_carries_the_deck_even_when_the_model_omits_it(monkeypatch):
    """The failure this guards: the model ignores the instruction and the email ships without
    the one link the operator asked to be in every email."""
    c = _C('{"subject":"quick q","body":"Hi PJ,\\n\\nI applied.\\n\\nThanks,\\nAlejandro",'
           '"linkedin_note":"Hi PJ, just applied — would love to connect."}')
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: c)
    monkeypatch.setenv("INTRO_DECK_URL", URL)

    out = outreach.draft_email(PROFILE, JOB, CONTACT)
    assert URL in out["body"], out["body"]
    assert "intro deck" in out["body"].lower()


def test_the_linkedin_note_never_carries_the_deck(monkeypatch):
    """300-char cap, and LinkedIn strips/penalizes links in connect notes."""
    c = _C('{"subject":"quick q","body":"Hi PJ,\\n\\nI applied.\\n\\nThanks,\\nAlejandro",'
           '"linkedin_note":"Hi PJ, just applied — would love to connect."}')
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: c)
    monkeypatch.setenv("INTRO_DECK_URL", URL)

    out = outreach.draft_email(PROFILE, JOB, CONTACT)
    assert URL not in out["linkedin_note"], out["linkedin_note"]


def test_the_model_is_actually_told_about_the_deck(monkeypatch):
    """Enforcement is the safety net, not the mechanism — a model that writes the link in its
    own words produces better copy than an appended stock sentence, so it must be asked."""
    c = _C('{"subject":"s","body":"Hi PJ,\\n\\nBody.\\n\\nThanks,\\nAlejandro","linkedin_note":"n"}')
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: c)
    monkeypatch.setenv("INTRO_DECK_URL", URL)

    outreach.draft_email(PROFILE, JOB, CONTACT)
    assert URL in c.seen[0], "the prompt never mentioned the deck URL"
    assert "INTRO DECK" in c.seen[0]


@pytest.mark.parametrize("touch", [1, 2, 3])
def test_every_followup_touch_carries_the_deck(monkeypatch, touch):
    """"All future emails" includes follow-ups — they are the messages most in need of a
    concrete reason to reply."""
    c = _C('{"subject":"Re: quick q","body":"Hi PJ,\\n\\nCircling back.\\n\\nAlejandro"}')
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: c)
    monkeypatch.setenv("INTRO_DECK_URL", URL)

    out = outreach.draft_followup(PROFILE, JOB, {**CONTACT, "outreach_subject": "quick q",
                                                 "outreach_message": "first"}, touch=touch)
    assert URL in out["body"], f"touch {touch}: {out['body']}"


def test_env_beats_profile_and_profile_is_the_fallback(monkeypatch):
    """Same precedence as SCHEDULING_LINK, so `doctor --config` explains the value."""
    monkeypatch.setenv("INTRO_DECK_URL", "https://env.example/deck")
    assert outreach._intro_deck_url(PROFILE) == "https://env.example/deck"
    monkeypatch.delenv("INTRO_DECK_URL", raising=False)
    assert outreach._intro_deck_url(PROFILE) == URL
    assert outreach._intro_deck_url({}) == ""


def test_the_url_is_a_declared_setting_so_doctor_reports_it():
    from applypilot import settings
    names = {s.name for s in settings.SETTINGS}
    assert "INTRO_DECK_URL" in names, "an undeclared env var is invisible to doctor --config"


@pytest.mark.parametrize("signoff", [
    "Thanks,\nAlejandro",
    "Alejandro",
    "Best regards,\nAlejandro Diez",
    "Cheers,\nAlejandro",
])
def test_the_signoff_block_is_never_split_in_half(signoff):
    """The first implementation inserted between "Thanks," and "Alejandro", producing:

        Thanks,

        Here's a good intro deck ...

        Alejandro

    A sign-off is a BLOCK, not a line. Placement walks paragraphs, not lines, because of this.
    """
    body = f"Hi PJ,\n\nI applied for the role.\n\n{signoff}"
    out = outreach.ensure_intro_deck(body, URL)
    assert out.endswith(signoff), f"the sign-off was broken up:\n{out}"
    assert out.index(URL) < out.index(signoff.split("\n")[0]), out


# ── LinkedIn: DMs get the deck, connection-request notes do not ──────────────────────────
#
# Three different messages, and only one of them has LinkedIn's link problem:
#   cold linkedin_note   = a CONNECTION REQUEST -> 300-char hard cap, links penalised
#   warm linkedin_note   = a DM to someone already connected  -> link fine
#   linkedin follow-up   = a DM to someone who accepted       -> link fine

DM_JSON = ('{"subject":"s","body":"Hi,\\n\\nBody.\\n\\nThanks,\\nAlejandro",'
           '"linkedin_note":"Hey Gina, long time without connecting, hope all is well at Zello."}')


def test_a_warm_linkedin_dm_carries_the_deck(monkeypatch):
    """You are already connected, so it lands in a chat window where a link works."""
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C(DM_JSON))
    monkeypatch.setenv("INTRO_DECK_URL", URL)
    out = outreach.draft_email(PROFILE, JOB, CONTACT, warm=True)
    assert URL in out["linkedin_note"], out["linkedin_note"]


def test_a_cold_connection_note_still_does_not(monkeypatch):
    """A connect note is capped at 300 chars and LinkedIn penalises links in invites. Spending
    41 of those characters on a URL that may be stripped is a bad trade."""
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C(DM_JSON))
    monkeypatch.setenv("INTRO_DECK_URL", URL)
    out = outreach.draft_email(PROFILE, JOB, CONTACT, warm=False)
    assert URL not in out["linkedin_note"], out["linkedin_note"]
    assert len(out["linkedin_note"]) <= 300


@pytest.mark.parametrize("touch", [1, 2, 3])
def test_every_linkedin_followup_carries_the_deck(monkeypatch, touch):
    monkeypatch.setattr(outreach, "get_client",
                        lambda *a, **k: _C('{"message":"Hey Gina, circling back on the role."}'))
    monkeypatch.setenv("INTRO_DECK_URL", URL)
    out = outreach.draft_linkedin_followup(
        PROFILE, JOB, {**CONTACT, "linkedin_message": "first note"}, touch=touch)
    assert URL in out["message"], f"touch {touch}: {out['message']}"


def test_a_warm_dm_is_not_cut_at_the_connection_note_limit(monkeypatch):
    """Warm notes were capped at 300 despite the warm prompt telling the model the cap does not
    apply, so they arrived truncated with an ellipsis. A DM has no such limit."""
    long_note = "Hey Gina, " + ("it has genuinely been a while and I wanted to reconnect. " * 8)
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C(
        '{"subject":"s","body":"b","linkedin_note":"' + long_note + '"}'))
    monkeypatch.setenv("INTRO_DECK_URL", URL)
    out = outreach.draft_email(PROFILE, JOB, CONTACT, warm=True)
    assert len(out["linkedin_note"]) > 300, "warm DM was cut at the connect-note limit"
    assert "…" not in out["linkedin_note"].replace(URL, ""), out["linkedin_note"]


def test_the_link_is_never_the_thing_that_gets_truncated(monkeypatch):
    """Capping AFTER appending would leave a broken half-URL, which is worse than no link."""
    huge = "word " * 400  # well past the DM limit
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C(
        '{"message":"' + huge + '"}'))
    monkeypatch.setenv("INTRO_DECK_URL", URL)
    out = outreach.draft_linkedin_followup(
        PROFILE, JOB, {**CONTACT, "linkedin_message": "x"}, touch=1)
    assert out["message"].rstrip().endswith(URL), "the URL was cut by the cap"
    assert URL in out["message"]
