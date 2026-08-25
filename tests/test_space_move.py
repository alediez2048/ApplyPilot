"""Moving a CARD from one Space to another.

Asked for with the reasonable worry that "spaces have different templates and thus different
schema". They do not — SPACE-1a's whole claim is that a Space is a manifest, not a fork — and
that is what makes this two columns instead of a migration. Only `jobs` and `contacts` carry
`space_id`; touches, messages, sequences, interactions and transcripts hang off the contact and
the ANCHOR, and the anchor deliberately does not move.
"""
import pytest

from applypilot.domain import space as sp
from applypilot.domain import spacemove as mv


def S(sid, name, shape="pipeline/targets", **kw):
    return sp.from_template(sid, name, "outreach" if shape.endswith("targets") else "jobs",
                            shape=shape, **kw)


# ── what may move ───────────────────────────────────────────────────────────

def test_a_same_shape_move_is_allowed():
    assert mv.refusal(S("a", "A"), S("b", "B")) == ""


def test_a_card_cannot_move_to_the_space_it_is_already_in():
    assert "already in" in mv.refusal(S("a", "A"), S("a", "A"))


def test_cross_shape_is_refused_with_the_reason():
    """Genuinely unsupported rather than merely hidden: `jobs_shaped_ids` gates the apply queue
    and `is_target` decides rendering from the anchor's kind, which a move cannot change."""
    why = mv.refusal(S("t", "Targets"), S("j", "Jobs", shape="pipeline/jobs"))
    assert "not supported" in why
    assert "company cards" in why and "job postings" in why


def test_an_unknown_space_is_refused():
    assert mv.refusal(S("a", "A"), None) == "that Space does not exist"
    assert mv.refusal(None, S("a", "A")) == "that Space does not exist"


def test_a_sent_card_may_not_change_identity():
    """The documented freeze. Every Space is `personal` today so this cannot fire in practice —
    it is written now because retrofitting it after ID-1 ships is the expensive order."""
    src, dst = S("a", "A"), S("b", "B", identity_id="business")
    assert mv.refusal(src, dst, has_sent=True) != ""
    assert mv.refusal(src, dst, has_sent=False) == "", "nothing sent, so nobody has met a sender"


# ── what changes ────────────────────────────────────────────────────────────

def test_identical_manifests_report_no_changes():
    """The operator's real case: `professional-network` and `partnerships` are byte-identical,
    so the move is a relabel and the dialog should say nothing is changing."""
    assert mv.differences(S("a", "A", tone="x"), S("b", "B", tone="x")) == []


def test_a_different_voice_is_reported():
    got = mv.differences(S("a", "A"), S("b", "B", voice="premise"))
    assert {"field": "voice", "from": "none", "to": "premise"} in got


# Values chosen to DIFFER from the `outreach` template's own defaults (terminal='booked',
# tailor_docs=False, can_autosend=True). The first version reused the defaults, so the field
# "changed" to what it already was and the test failed against correct code — a fixture that
# cannot express the thing it is testing.
@pytest.mark.parametrize("field,value", [
    ("terminal", "interview"), ("tone", "a tone"), ("offer", "an offer"),
    ("must_mention", ("GauntletAI",)), ("tailor_docs", True), ("can_autosend", False),
    ("offer_deck", False), ("voice", "premise"), ("identity_id", "business"),
])
def test_every_compared_field_is_actually_compared(field, value):
    """Guard the guard: a field listed in COMPARED but never surfaced is a silent change."""
    got = mv.differences(S("a", "A"), S("b", "B", **{field: value}))
    assert any(c["field"] == field for c in got), f"{field} changed and nothing said so"


# ── unsent drafts ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("voice", "premise"), ("tone", "different"), ("offer", "different"),
    ("must_mention", ("X",)),
])
def test_a_voice_change_makes_unsent_drafts_stale(field, value):
    """CO-2 learned this the expensive way: sixteen unsent drafts naming a dead role, each one
    click from Send."""
    assert mv.drafts_are_stale(S("a", "A"), S("b", "B", **{field: value})) is True


@pytest.mark.parametrize("field,value", [("terminal", "booked"), ("tailor_docs", False),
                                         ("schedules", {"email": [1, 2]})])
def test_a_non_voice_change_leaves_drafts_alone(field, value):
    """A different terminal or ladder changes what happens NEXT; it does not make a sentence
    already written wrong. Discarding on those would destroy work for nothing."""
    assert mv.drafts_are_stale(S("a", "A"), S("b", "B", **{field: value})) is False


# ── the ladder ──────────────────────────────────────────────────────────────

def test_a_shorter_ladder_is_warned_about():
    """`ladder_states` counts sent touches against `len(schedule)`, so a contact three touches
    deep lands in a two-touch Space reading `finished` (§Lessons 107)."""
    w = mv.ladder_warning(S("a", "A", schedules={"email": [1, 2, 3]}),
                          S("b", "B", schedules={"email": [1, 2]}))
    assert "shorter" in w and "3 touches to 2" in w


def test_a_longer_ladder_is_not_a_warning():
    assert mv.ladder_warning(S("a", "A", schedules={"email": [1, 2]}),
                             S("b", "B", schedules={"email": [1, 2, 3]})) == ""


def test_an_absent_schedule_is_a_default_not_a_shorter_ladder():
    """A Space with no entry for a channel inherits the registry default. Reading that as zero
    would warn on every move into a Space that simply does not override it."""
    assert mv.ladder_warning(S("a", "A", schedules={"email": [1, 2, 3]}),
                             S("b", "B", schedules={})) == ""


# ── the control has to be FINDABLE, not merely present ─────────────────────

def test_the_move_is_offered_outside_the_overflow_menu():
    """It shipped only in `⋯` and was reported missing within minutes.

    This codebase carries a comment directly above the interview button saying "burying it made
    it unfindable", and the same mistake has now been made ten times (§Lessons 43, 88, 89, 97).
    The move must be reachable from the card's own surface, not only from the overflow menu.
    """

    from applypilot import web_dashboard as wd
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert "function spaceRow(" in js, "the Job tab no longer offers the move"
    # It is rendered by the Job tab, not just defined.
    detail = js[js.index("function jobDetail("):]
    detail = detail[:detail.index("\n}")]
    assert "spaceRow(j)" in detail, "spaceRow is defined but never rendered"
    # And still in the row menu, because two doors to one action is right here: the menu is
    # where the other row-level state changes live.
    menu = js[js.index("function rowMenu("):]
    menu = menu[:menu.index("\n}")]
    assert "moveCardToSpace" in menu


def test_the_space_row_states_where_the_card_currently_is():
    """"Move to X" alone does not say where you are. The row is also the only place the card's
    Space is written down."""

    from applypilot import web_dashboard as wd
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    fn = js[js.index("function spaceRow("):]
    fn = fn[:fn.index("\n}")]
    assert "SPACE_RESOLVED" in fn and "jd-k" in fn
