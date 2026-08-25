"""The same person must not be found again for a second role at the same employer.

Reported with two webAI cards side by side showing the same contact. The exclusion already
existed and half-worked — its own log line read "3 more are already on another role at this
company" while a fourth walked straight through.

The cause is WHERE it ran. Apollo's SEARCH response carries no email and a REDACTED surname, so
the pre-selection pass can only match on a truncated first name:

    stored 'Marcus'          search 'Marcus'          -> caught
    stored 'Michael'         search 'Michael'         -> caught
    stored 'Emilia Pavlovic' search 'Emilia'          -> MISSED

62% of stored Apollo contacts are first-name-only, so the check worked for exactly those and
failed for everyone an enrichment had since completed. Emilia's two rows carry the same email,
the same LinkedIn URL and the same full name — every key matched, and none of them existed at
the only point the check ran.
"""
import pytest

from applypilot.networking import service


def keys(mine):
    """The three lookups exactly as `find_contacts_for_job` builds them.

    `known_at_company` returns the email and LinkedIn URL ALREADY NORMALISED (store.py), so the
    keys are normalised here too. The first version of this helper used the raw stored strings
    and failed against correct code — a fixture that does not match what the caller actually
    produces (§Lessons 103).
    """
    from applypilot.networking import store
    by_email = {store._norm_email(m["email"]): m for m in mine if m["email"]}
    by_li = {store._norm_linkedin(m["linkedin_url"]): m for m in mine if m["linkedin_url"]}
    by_name = {(m["full_name"] or "").strip().lower(): m for m in mine if m["full_name"]}
    return by_email, by_li, by_name


STORED = [{"email": "emilia.pavlovic@webai.com", "full_name": "Emilia Pavlovic",
           "linkedin_url": "http://www.linkedin.com/in/emilia-pavlovic-779311329",
           "job_title": "AI Software Engineer"}]


def test_the_search_time_record_does_not_match_which_is_the_bug():
    """What Apollo returns BEFORE enrichment: a first name and nothing else."""
    at_search = {"full_name": "Emilia", "email": "", "linkedin_url": ""}
    assert service._already_ours(at_search, *keys(STORED)) is None


def test_the_enriched_record_matches_which_is_the_fix():
    """The same person after enrichment, which is where the second pass now runs."""
    enriched = {"full_name": "Emilia Pavlovic", "email": "emilia.pavlovic@webai.com",
                "linkedin_url": "http://www.linkedin.com/in/emilia-pavlovic-779311329"}
    assert service._already_ours(enriched, *keys(STORED)) is not None


@pytest.mark.parametrize("contact,why", [
    ({"email": "EMILIA.PAVLOVIC@WEBAI.COM", "full_name": "", "linkedin_url": ""},
     "email, differently cased"),
    ({"email": "", "full_name": "",
      "linkedin_url": "https://www.linkedin.com/in/emilia-pavlovic-779311329"},
     "linkedin, https against a stored http"),
    ({"email": "", "full_name": "  emilia pavlovic  ", "linkedin_url": ""},
     "name, padded and lowercased"),
])
def test_any_one_key_is_enough(contact, why):
    assert service._already_ours(contact, *keys(STORED)) is not None, why


def test_a_different_person_is_not_matched():
    """The guard on the guard: an over-eager match silently drops real colleagues, which is the
    §Lessons 14 failure — being right producing nothing."""
    other = {"email": "frank.lange@webai.com", "full_name": "Frank Lange",
             "linkedin_url": "http://www.linkedin.com/in/frank-lange-1"}
    assert service._already_ours(other, *keys(STORED)) is None


def test_nobody_stored_yet_matches_nothing():
    assert service._already_ours({"email": "a@b.test", "full_name": "A B"}, {}, {}, {}) is None


def test_both_passes_use_the_same_predicate():
    """Two spellings of "is this the same person" is how the passes come to disagree."""
    import inspect
    src = inspect.getsource(service.find_contacts_for_job)
    assert src.count("_already_ours(") == 2, (
        "the pre-selection and post-enrichment passes must share one predicate")


def test_the_second_pass_runs_before_the_contact_is_stored():
    """After enrichment (so the keys exist) and before `upsert_contact` (so no row is written).
    Ordering is the whole fix: the same call one step later stores the duplicate first."""
    import inspect
    src = inspect.getsource(service.find_contacts_for_job)
    dup = src.index("dup = _already_ours(")
    assert src.index("revealed = providers.enrich(") < dup, "it runs before enrichment again"
    assert dup < src.index("cid = store.upsert_contact("), "it runs after the row is written"


# ── every layer that can store a contact, not just the one that was reported ──

def test_the_warm_layer_also_checks_across_roles():
    """The cold path was fixed first and the connections path had no cross-role check at all —
    it deduped only against the current search's own cold results, so a connection at a company
    you already work could be stored again for a second role with a fresh warm draft.

    §Lessons 49: a rule implemented at one of its call sites is not implemented.
    """
    import inspect
    src = inspect.getsource(service._find_hot_contacts)
    assert "_already_ours(" in src, "the warm layer stores without the cross-role check"
    # And it must run BEFORE the row is written, or it stores the duplicate and then notices.
    assert src.index("_already_ours(") < src.index("store.upsert_contact(")


def test_the_warm_layer_is_given_the_lookups():
    """A parameter that is accepted and never passed is the same as absent (§Lessons 39/73)."""
    import inspect
    caller = inspect.getsource(service.find_contacts_for_job)
    assert "known=(by_email, by_li, by_name)" in caller, (
        "the warm layer is called without the cross-role lookups")


def test_the_warm_layer_defaults_to_letting_everyone_through():
    """Callers that have no lookups must not silently drop every connection."""
    assert service._already_ours({"full_name": "Anyone", "email": "a@b.test"}, {}, {}, {}) is None
