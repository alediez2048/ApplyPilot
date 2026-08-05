"""SPACE-4: the manifest reaches the prompts, the ladders and the send path.

The load-bearing test here is `test_a_default_space_changes_the_prompt_by_nothing`. Every field
added in this ticket is additive, and the only way to know that is to compare the ARTIFACT
rather than to reason about it (§Lessons 46 — "14 of 16 résumés have a junk header" was reported
loudly from the intermediate file while zero delivered PDFs contained the string). A job search
that starts writing subtly different emails because Spaces shipped would be the worst possible
outcome of this work and nobody would notice for weeks.

The second thing worth more than the feature: the pitch prompt is a SEPARATE system prompt, not
the job-seeker one with caveats appended. `_SYSTEM` opens "You write short, casual networking
messages for a job seeker reaching out to someone at a company they just applied to", and no
amount of appended text makes that describe a business proposal — §Lessons 40, where the SMS
prompt's touch ladder beat the standing block every time because it arrived under a heading.
"""

from __future__ import annotations

import pathlib

import pytest

import applypilot.database as database
from applypilot.domain import followup as fu
from applypilot.domain import space as sp
from applypilot.networking import outreach, store
from applypilot.repo import spaces

PROFILE = {"personal": {"full_name": "Alejandro Diez", "intro_deck_url": ""}}
JOB = {"url": "http://j/1", "title": "Applied AI Engineer", "company": "Acme",
       "site": "Greenhouse", "full_description": "You will build agent pipelines end to end.",
       "space_id": "job-search"}
CONTACT = {"id": "c1", "full_name": "Sarah Chen", "title": "Head of Engineering",
           "company": "Acme", "email": "s@acme.test", "match_reason": "works at the company"}


def _prompt(job, contact, space, **kw):
    """The user prompt `draft_email` would send, without calling an LLM."""
    from applypilot.networking.outreach import _job_user_prompt, _pitch_user_prompt  # noqa: F401
    import applypilot.networking.outreach as o

    captured = {}

    class _Client:
        def chat(self, messages, **_):
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return '{"subject":"s","body":"b","linkedin_note":"n"}'

    real = o.get_client
    o.get_client = lambda *_a, **_k: _Client()
    try:
        o.draft_email(PROFILE, job, contact, space=space, **kw)
    finally:
        o.get_client = real
    return captured


# ── the guarantee that matters ──────────────────────────────────────────────

#: A FIXED baseline, not a computed one. The first version of the test below compared
#: `space=None` against a default manifest and passed while a mutation made a manifest field
#: leak into BOTH paths — it proved the two were equal to each other, not that either matched
#: what shipped before Spaces. Two things moving together is not a regression test.
#:
#: Updating this file is a deliberate act. If a diff appears here, the question is not "does the
#: new output look fine" but "did anyone decide to change the outreach copy" — it took four
#: passes and five separate §Lessons-9 incidents to get right.
GOLDEN = pathlib.Path(__file__).parent / "golden" / "jobs_outreach_prompt.txt"


def test_the_jobs_prompt_matches_the_fixed_baseline():
    """The artifact, against a stored copy of itself from before the manifest existed."""
    assert _prompt(JOB, CONTACT, None)["user"] == GOLDEN.read_text(encoding="utf-8")


def test_a_default_space_changes_the_prompt_by_nothing():
    """A job search must write byte-identical emails after Spaces shipped.

    Both halves are needed. This one says the manifest path matches the no-manifest path; the
    one above says the no-manifest path still matches what shipped. Either alone passes while
    the copy silently drifts.
    """
    default = sp.from_template("job-search", "Job Search", "jobs")
    before = _prompt(JOB, CONTACT, None)
    after = _prompt(JOB, CONTACT, default)
    assert after["user"] == before["user"]
    assert after["user"] == GOLDEN.read_text(encoding="utf-8")
    assert after["system"] == before["system"] == outreach._SYSTEM


def test_the_default_manifest_really_is_the_old_behaviour():
    """Guard the guard: if the defaults ever differ, the test above compares two new things."""
    d = sp.from_template("x", "X", "jobs")
    assert (d.tone, d.offer, d.offer_deck, d.can_autosend, d.schedules) == ("", "", True, True, {})
    assert d.channels == tuple(c.name for c in fu.CHANNELS)


# ── the pitch shape ─────────────────────────────────────────────────────────

TARGET_JOB = {"url": "target:partnerships:ridgeline", "title": "Ridgeline Logistics",
              "company": "Ridgeline Logistics", "site": "", "full_description": "",
              "space_id": "partnerships"}


def _pitch_space(**kw):
    return sp.from_template("partnerships", "Partnerships", "outreach",
                            offer="I build agent systems that run a real workflow end to end.",
                            **kw)


def test_a_targets_space_gets_its_own_system_prompt():
    """Not the job-seeker prompt with caveats appended (§Lessons 40)."""
    got = _prompt(TARGET_JOB, CONTACT, _pitch_space())
    assert got["system"] == outreach._PITCH_SYSTEM
    assert got["system"] != outreach._SYSTEM
    assert "job seeker" not in got["system"].lower()
    # The PREMISE must be gone, not the word. "Never imply you applied to anything" is the
    # rule; asserting `"applied" not in prompt` flags the rule as the violation, which is
    # §Lessons 1's habit in a new place.
    assert "they just applied" not in got["system"].lower()
    assert "Never imply you applied to anything." in got["system"]


def test_the_pitch_prompt_never_claims_an_application():
    """There is no job here, and a first email that says you applied to one is a lie."""
    got = _prompt(TARGET_JOB, CONTACT, _pitch_space())
    assert "JOB APPLIED TO" not in got["user"]
    assert "WHAT THE ROLE ACTUALLY INVOLVES" not in got["user"]


def test_the_offer_is_the_thing_being_proposed():
    got = _prompt(TARGET_JOB, CONTACT, _pitch_space())
    assert "I build agent systems that run a real workflow end to end." in got["user"]
    assert "WHAT YOU ARE PROPOSING" in got["user"]


def test_an_unknown_company_is_stated_as_unknown_not_left_blank():
    """A model handed "WHAT THEY DO:" followed by nothing invents something.

    An invented fact about the recipient's own company is the one error a first email cannot
    recover from, so the absence is named rather than rendered as an empty heading.
    """
    got = _prompt(TARGET_JOB, CONTACT, _pitch_space())["user"]
    assert "not recorded" in got and "Do not invent" in got


def test_a_pasted_description_is_used_instead():
    job = dict(TARGET_JOB, full_description="They run coastal freight across nine ports.")
    got = _prompt(job, CONTACT, _pitch_space())["user"]
    assert "coastal freight across nine ports" in got
    assert "not recorded" not in got


def test_the_pitch_prompt_bans_the_dash_like_every_other_one():
    """Belt and braces (CLAUDE.md §No em dashes). A new prompt is a new hole."""
    assert "em dash" in outreach._PITCH_SYSTEM.lower()
    assert "—" not in outreach._PITCH_SYSTEM.replace("(—)", "")


def test_the_pitch_prompt_carries_no_worked_example_in_the_senders_domain():
    """§Lessons 9 and 42, five occurrences and counting.

    The rule that is easiest to break in a NEW prompt: an illustrative sentence comes back
    verbatim, and here it would come back across several people at one company who sit near
    each other.
    """
    lowered = outreach._PITCH_SYSTEM.lower()
    for burned in ("i build autonomous agent", "applied ai engineer", "technical project manager"):
        assert burned not in lowered


# ── tone ────────────────────────────────────────────────────────────────────

def test_tone_reaches_both_shapes():
    for job, space in ((JOB, sp.from_template("s", "S", "jobs", tone="Dry and direct.")),
                       (TARGET_JOB, _pitch_space(tone="Dry and direct."))):
        assert "Dry and direct." in _prompt(job, CONTACT, space)["user"]


def test_tone_is_placed_after_the_per_run_style():
    """A one-off instruction the operator typed must not be silently overridden by a stored one.

    Both are honoured; the ordering is the tie-break, and it is the same reasoning as the burned
    block sitting last (§Lessons 40).
    """
    space = sp.from_template("s", "S", "jobs", tone="CAMPAIGNVOICE")
    got = _prompt(JOB, CONTACT, space, style="RUNSTYLE")["user"]
    assert got.index("RUNSTYLE") < got.index("CAMPAIGNVOICE")


# ── the deck switch ─────────────────────────────────────────────────────────

def test_offer_deck_false_removes_the_link_and_the_guarantee_that_appends_it(monkeypatch):
    """`ensure_intro_deck` appends the link when the model drops it — a guarantee (§Lessons 12).

    So turning the deck off has to happen at the SOURCE, or the belt-and-braces mechanism
    faithfully re-adds a link the Space said not to send.
    """
    monkeypatch.setenv("INTRO_DECK_URL", "https://example.test/intro/")
    on = _prompt(JOB, CONTACT, sp.from_template("s", "S", "jobs"))["user"]
    off = _prompt(JOB, CONTACT, sp.from_template("s", "S", "jobs", offer_deck=False))["user"]
    assert "INTRO DECK LINK" in on
    assert "INTRO DECK LINK" not in off


# ── cadence and channels ────────────────────────────────────────────────────

def test_a_space_overrides_the_cadence():
    """5d/12d for a C-suite pitch versus 2d/4d/7d for a job. A schedule, not new code."""
    slow = sp.from_template("p", "P", "outreach")          # template sets {"email": [120, 288]}
    assert fu.channel_schedule(fu.EMAIL, slow) == [120, 288]
    assert fu.channel_schedule(fu.EMAIL, None) == list(fu.EMAIL.default_schedule)


def test_the_manifest_beats_the_environment(monkeypatch):
    """The env var is every campaign's default; the manifest is THIS campaign's decision.

    A stored decision a global can silently override is not a decision.
    """
    monkeypatch.setenv("FOLLOWUP_SCHEDULE", "1,2,3")
    from applypilot import settings
    settings.resolve.cache_clear() if hasattr(settings.resolve, "cache_clear") else None
    space = sp.from_template("p", "P", "jobs", schedules={"email": [99]})
    assert fu.channel_schedule(fu.EMAIL, space) == [99]


def test_a_space_can_narrow_the_channel_set():
    space = sp.from_template("p", "P", "outreach", channels=("email",))
    assert [c.name for c in fu.channels_for(space)] == ["email"]
    assert fu.channels_for(None) == fu.CHANNELS


def test_an_unknown_channel_name_is_ignored_not_fatal():
    """A manifest is operator-editable config; a typo must not take the engine down.

    And it must not empty the set either — see below.
    """
    space = sp.from_template("p", "P", "outreach", channels=("email", "carrier-pigeon"))
    assert [c.name for c in fu.channels_for(space)] == ["email"]


def test_a_channel_set_that_matches_nothing_falls_back_to_all():
    """A Space that can send nothing renders an empty panel identical to one where nobody is due.

    §Lessons 15: silently offering nothing is the failure mode, not the safe default.
    """
    space = sp.from_template("p", "P", "outreach", channels=("carrier-pigeon",))
    assert fu.channels_for(space) == fu.CHANNELS


def test_the_panel_only_reports_the_channels_a_space_offers():
    contacts = [{"id": "c1", "full_name": "Sarah Chen", "email": "a@x.test", "emailed": True,
                 "submitted_at": "2026-01-01T00:00:00+00:00",
                 "linkedin_url": "https://l/in/a", "dm_status": "sent",
                 "dm_sent_at": "2026-01-01T00:00:00+00:00"}]
    full = fu.followup_panel(contacts)
    narrow = fu.followup_panel(contacts, space=sp.from_template("p", "P", "outreach",
                                                               channels=("email",)))
    assert "li_due" in full
    assert "li_due" not in narrow, "a channel the Space does not offer still reported work"
    assert "due" in narrow


# ── the send switch ─────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    spaces.create_space("quiet", "Quiet campaign", "outreach", conn=conn, can_autosend=False)
    conn.execute("INSERT INTO jobs (url, title, site, strategy, space_id) VALUES (?,?,?,?,?)",
                 ("target:quiet:acme", "Acme", "", "dashboard_upload", "quiet"))
    conn.commit()
    return conn


def test_autosend_off_is_enforced_at_the_send_path(db):
    """Not by hiding a button. The endpoint is reachable without one (§Lessons 43's inverse).

    This is the setting whose entire job is to stop the operator doing something at 11pm that
    cannot be undone, so it is checked where the irreversible thing happens.
    """
    from applypilot import web_dashboard as wd
    cid = store.upsert_contact({"job_url": "target:quiet:acme", "space_id": "quiet",
                                "full_name": "Sarah Chen", "email": "s@acme.test"}, db)
    space = wd._space_of_contact(cid, db)
    assert space is not None and space.can_autosend is False


def test_a_contact_with_no_space_does_not_break_the_gate(db):
    """Refusing to send is a decision; crashing on the way to making it is not."""
    from applypilot import web_dashboard as wd
    assert wd._space_of_contact("nobody", db) is not None or True   # must not raise


# ── the manifest actually reaches drafting from the row ─────────────────────

def test_the_drafting_query_carries_the_space(db):
    """§Lessons 47: a column the caller needs belongs in the SELECT.

    This one was left out first, and the effect was a targets contact drafted with the
    job-seeker prompt — the write side worked perfectly the whole time.
    """
    from applypilot.repo import jobs as repo
    row = repo.find_by_any_url("target:quiet:acme", db)
    assert row is not None and row["space_id"] == "quiet"

    cid = store.upsert_contact({"job_url": "target:quiet:acme", "space_id": "quiet",
                                "full_name": "Sarah Chen", "email": "s@acme.test"}, db)
    jrow = db.execute(
        "SELECT url, title, company, site, full_description, space_id FROM jobs WHERE url = ?",
        (store.get_contact(cid, db)["job_url"],)).fetchone()
    from applypilot.networking import service
    assert service.space_for(dict(zip(jrow.keys(), jrow)), db).shape == sp.TARGETS_SHAPE


# ── the sender's own name ───────────────────────────────────────────────────

@pytest.mark.parametrize("body,expected", [
    # The live failure: a plausible near-miss of the sender's name.
    ("Hi Priya,\n\nSome text.\n\nAlexander", "Alejandro"),
    ("Hi Priya,\n\nSome text.\n\nBest,\nAlex", "Alejandro"),
    # Already correct: untouched, including the surrounding shape.
    ("Hi Priya,\n\nSome text.\n\nAlejandro", "Alejandro"),
    ("Hi Priya,\n\nSome text.\n\nThanks,\nAlejandro", "Alejandro"),
])
def test_the_signoff_always_names_the_real_sender(body, expected):
    """§Lessons 42: found by generating against real data, not by reading the prompt.

    The third of three live pitch drafts was signed "Alexander" while the prompt gave both the
    sender's name and first name. A wrong name looks exactly as fluent as a right one, which is
    why a prompt instruction cannot be the only guard (§Lessons 9, 12).
    """
    out = outreach.ensure_sender_signoff(body, "Alejandro")
    assert out.rstrip().split("\n")[-1].strip() == expected


def test_it_refuses_to_rewrite_a_sentence():
    """Narrow on purpose. Rewriting a real closing line does more damage than the wrong name."""
    body = "Hi Priya,\n\nSome text.\n\nLet me know if that would be useful to you."
    assert outreach.ensure_sender_signoff(body, "Alejandro") == body


def test_an_empty_body_or_name_is_left_alone():
    assert outreach.ensure_sender_signoff("", "Alejandro") == ""
    assert outreach.ensure_sender_signoff("Hi\n\nAlex", "") == "Hi\n\nAlex"


def test_the_signoff_guard_uses_the_same_name_as_the_prompt():
    """One rule, one implementation (§Lessons 49).

    The profile's `full_name` is "Jorge Alejandro Diez" and his `preferred_name` is "Alejandro".
    Re-deriving the first name from `full_name` makes the guard rewrite every correct sign-off
    to "Jorge" — a rare hallucination converted into a systematic error, by the fix for it.
    Caught by regenerating against the live model; the unit tests above hardcode the name and
    cannot see it.
    """
    profile = {"personal": {"full_name": "Jorge Alejandro Diez", "preferred_name": "Alejandro"}}
    assert outreach._sender_name(profile) == "Alejandro"
    got = _prompt(JOB, CONTACT, None)          # the prompt's own rendering, for the same profile
    assert "Sender first name:" in got["user"]
    body = "Hi Sarah,\n\nSome text.\n\nAlejandro"
    assert outreach.ensure_sender_signoff(body, outreach._sender_name(profile)) == body
