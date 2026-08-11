"""Contact discovery never keeps people from an excluded place.

Asked for as "auto delete contacts we find where the people are based in India". Built as a
FILTER at discovery rather than a delete, and the difference is the whole design:

    delete   store the person, then remove them — and `delete_contact` also wipes their rows in
             `touches`, `sequences`, `messages` and `interactions`. An automatic, irreversible
             delete driven by a fuzzy provider field is the one thing this codebase should not
             have running unattended.
    filter   never store them. Nothing to orphan, nothing to undo, and no stored contact is ever
             altered — including anyone already emailed or who has replied.

The measurement that shaped it: `contacts.location` was empty on **all 244 stored rows**, so the
filter as described could not have matched anybody. Apollo's SEARCH response carries
`has_city` / `has_state` / `has_country` — booleans about whether the data exists — and never the
values, so `city or state or country` was None every time. The real values come back from
ENRICHMENT, where the mapper read four fields and dropped the rest (the same bug that had left
162 of 185 contacts first-name-only).

So the exclusion runs in two places, and both are necessary:

    query-side   Apollo's own `person_not_locations`, verified live: `person_locations:[India]`
                 and `person_not_locations:[India]` return sets with ZERO overlap. An excluded
                 person is never enriched, so they never cost a credit.
    result-side  the enriched location, checked ourselves. A filter enforced only by the vendor
                 is not enforced, and the hot layer has no search to filter at all.
"""

from __future__ import annotations

import pytest

from applypilot.domain import geo


# ── the rule ────────────────────────────────────────────────────────────────

IN = geo.parse_exclusions("India")


@pytest.mark.parametrize("loc", [
    "India",
    "Bengaluru, Karnataka, India",
    "Bangalore",                       # city only — Apollo returns this shape too
    "Hyderabad, Telangana, India",
    "new delhi",
    "Gurugram, Haryana",
    "MUMBAI",
])
def test_india_is_matched_through_its_cities(loc):
    """A country matched by name alone silently keeps everyone whose row is city-only, which is
    a real shape in the same provider's output."""
    assert geo.is_excluded(loc, IN) == "india"


@pytest.mark.parametrize("loc", [
    "Indiana",
    "Indianapolis, Indiana",
    "Indianapolis",
    "West Lafayette, Indiana",
])
def test_indiana_is_not_india(loc):
    """§Lessons 1, and the exact trap here: "India" is a substring of "Indiana". A naive rule
    aimed at Bangalore quietly drops every candidate in Indianapolis."""
    assert geo.is_excluded(loc, IN) == ""


@pytest.mark.parametrize("loc", [
    "Austin, Texas, United States", "Dublin, Ireland", "London", "Singapore",
    "San Francisco Bay Area",
])
def test_everywhere_else_is_kept(loc):
    assert geo.is_excluded(loc, IN) == ""


@pytest.mark.parametrize("loc,expected", [
    ("Delhi Township, Ohio", ""),          # a real township in Hamilton County
    ("Delhi, California", ""),
    ("Delhi, New York", ""),
    ("Madras, Oregon", ""),                # a real city in Jefferson County
    ("Hyderabad, Sindh, Pakistan", ""),    # a real city in a country nobody excluded
    ("New Delhi, India", "india"),
    ("Hyderabad, Telangana, India", "india"),
    ("Delhi", "india"),                    # bare, unqualified — the shape the city list is for
])
def test_an_ambiguous_city_needs_its_country(loc, expected):
    """Caught by a test, not by review: "Delhi Township, Ohio" was excluded as India. Delhi,
    Madras and Hyderabad are all real places outside India, so the NAME is not proof of the
    country — §Lessons 1 with geography instead of company names. They match only when the
    country is named too, or when the string is nothing but the city."""
    assert geo.is_excluded(loc, IN) == expected


def test_a_multi_word_city_needs_its_words_adjacent():
    """"New Delhi" must not be matched by "New York" plus a stray "delhi" elsewhere in the
    string — and "Delhi Township, Ohio" is a real place that has to survive."""
    assert geo.is_excluded("New York, New York", IN) == ""
    assert geo.is_excluded("New Delhi", IN) == "india"


def test_an_unknown_location_is_KEPT():
    """Apollo does not always return one. Dropping on a blank field shrinks every search on
    missing data rather than on the rule — §Lessons 34, where a guess was handed to a check that
    treated it as proof."""
    for blank in ("", None, "   "):
        assert geo.is_excluded(blank, IN) == ""


def test_no_exclusions_means_the_filter_is_off():
    assert geo.parse_exclusions("") == ()
    assert geo.parse_exclusions(None) == ()
    assert geo.is_excluded("Bengaluru, India", ()) == ""


def test_several_places_can_be_excluded():
    two = geo.parse_exclusions("India, Ireland")
    assert geo.is_excluded("Dublin, Ireland", two) == "ireland"
    assert geo.is_excluded("Bengaluru", two) == "india"
    assert geo.is_excluded("Austin, Texas", two) == ""


def test_it_names_WHICH_place_matched():
    """"dropped 3" is an answer nobody can check. "dropped 3 in India" is one they can."""
    assert geo.is_excluded("Pune", geo.parse_exclusions("India, Ireland")) == "india"


def test_split_preserves_order_and_partitions_completely():
    cands = [{"location": "Austin, Texas"}, {"location": "Pune, India"},
             {"location": ""}, {"location": "Bengaluru"}]
    kept, dropped = geo.split(cands, IN)
    assert [c["location"] for c in kept] == ["Austin, Texas", ""]
    assert [c["location"] for c in dropped] == ["Pune, India", "Bengaluru"]
    assert len(kept) + len(dropped) == len(cands)
    assert all(d["excluded_place"] == "india" for d in dropped)


# ── the query-side filter ───────────────────────────────────────────────────

def test_the_provider_is_asked_to_exclude(monkeypatch):
    """Apollo filters server-side, so an excluded person is never enriched and never costs a
    credit. Doing it only on our side would mean paying to learn who to drop."""
    monkeypatch.setenv("OUTREACH_EXCLUDE_LOCATIONS", "India")
    from applypilot.networking import providers
    assert providers.excluded_query_terms() == ["India"]


def test_an_empty_setting_sends_no_filter(monkeypatch):
    monkeypatch.setenv("OUTREACH_EXCLUDE_LOCATIONS", "")
    from applypilot.networking import providers
    assert providers.excluded_query_terms() == []


def test_the_setting_is_read_per_call_not_at_import(monkeypatch):
    """`OUTREACH_ATTACH_DOCS` was read at import, which made it a deployment setting needing a
    restart — and then needed a file override bolted on. One env lookup is free next to an HTTP
    round trip."""
    from applypilot.networking import providers
    monkeypatch.setenv("OUTREACH_EXCLUDE_LOCATIONS", "India")
    assert providers.exclusions() == ("india",)
    monkeypatch.setenv("OUTREACH_EXCLUDE_LOCATIONS", "Ireland")
    assert providers.exclusions() == ("ireland",)


def test_every_search_path_carries_the_filter():
    """The widening path drops the TITLE filter when a narrow query finds nobody. Dropping the
    location one there too would make the exclusion stop applying to exactly the searches that
    return the most people (§Lessons 49, which is four bugs in this repo)."""
    import inspect

    from applypilot.networking import providers
    src = inspect.getsource(providers)
    calls = src.count("apollo.search_people(")
    carried = src.count("not_locations=excluded_query_terms()") + src.count(
        '"not_locations": excluded_query_terms()')
    # The domain-confirmation probe searches a GUESSED domain to ask "does anyone here say they
    # work at this company" — a question about the domain, not about who to contact.
    assert calls >= 4
    assert carried >= 3, f"{carried} of {calls} search paths carry the location filter"


def test_the_search_mapper_no_longer_invents_a_location():
    """It read `city or state or country` off a response that has none of them — only
    `has_city` / `has_state` / `has_country`. That is why all 244 rows were blank."""
    import inspect

    from applypilot.networking import apollo
    fn = inspect.getsource(apollo.search_people)
    assert 'p.get("city")' not in fn
    assert '"location": None' in fn


def test_enrichment_supplies_the_location():
    import inspect

    from applypilot.networking import apollo
    assert '"location": _location(m)' in inspect.getsource(apollo.bulk_enrich)
    assert '"location": _location(m)' in inspect.getsource(apollo.match_by_identity), (
        "the hot layer would keep every connection regardless of the setting")


def test_the_location_string_keeps_the_country():
    """`city or state or country` returns "Bengaluru" and throws the country away — the one word
    a country exclusion could match on."""
    from applypilot.networking import apollo
    assert apollo._location({"city": "Bengaluru", "state": "Karnataka", "country": "India"}) \
        == "Bengaluru, Karnataka, India"
    assert apollo._location({"country": "India"}) == "India"
    assert apollo._location({}) == ""


# ── it filters, it does not delete ──────────────────────────────────────────

def test_discovery_never_deletes_for_location():
    """The requested behaviour was "auto delete". `delete_contact` also wipes `touches`,
    `sequences`, `messages` and `interactions` — so an automatic delete on a fuzzy provider field
    would silently destroy a live follow-up ladder, and a replied-to thread, on a bad location
    string. Filtering before the write makes all of that unreachable."""
    import inspect

    from applypilot.networking import service
    src = inspect.getsource(service)
    # The exclusion appears before `upsert_contact` in the cold loop, and `continue`s.
    cold = src[src.index("place = geo.is_excluded"):]
    assert "continue" in cold[:400]
    # No delete is reachable from the geo check anywhere in the module.
    for block in src.split("geo.is_excluded")[1:]:
        assert "delete_contact" not in block[:600], "a location check leads to a delete"


def test_an_excluded_person_is_reported_separately_from_a_rejection():
    """They are different findings. A rejection means "does not work there"; an exclusion means
    "they do, and it is not the desk we write to". Merged, a targeting choice reads as a
    data-quality failure in the one place anyone looks (§Lessons 15)."""
    import inspect

    from applypilot.networking import service
    src = inspect.getsource(service)
    assert 'result["excluded"] = excluded' in src
    assert "skipped" in src and "who work elsewhere" in src


def test_an_empty_result_caused_by_the_filter_says_so():
    """"No contacts kept — the employer name may match more than one company" would send the
    operator to fix the employer when the fix is one setting."""
    import inspect

    from applypilot.networking import service
    src = inspect.getsource(service)
    assert "OUTREACH_EXCLUDE_LOCATIONS to keep them" in src


def test_the_setting_is_declared():
    from applypilot.settings import SETTINGS
    s = next(x for x in SETTINGS if x.name == "OUTREACH_EXCLUDE_LOCATIONS")
    assert s.default == "India"
    assert "never deletes" in s.help.lower() or "never delete" in s.help.lower()
