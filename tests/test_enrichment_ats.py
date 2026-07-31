"""Tests for ATS public-API enrichment (Greenhouse / Lever / Ashby)."""

from __future__ import annotations

from applypilot.enrichment import ats


def test_detect_ats_recognizes_known_boards():
    assert ats.detect_ats("https://job-boards.greenhouse.io/affirm/jobs/7778204003") == "greenhouse"
    assert ats.detect_ats("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert ats.detect_ats("https://jobs.lever.co/acme/abc-123") == "lever"
    assert ats.detect_ats("https://jobs.ashbyhq.com/acme/uuid-x") == "ashby"


def test_detect_ats_ignores_others():
    assert ats.detect_ats("https://example.com/careers/1") is None
    assert ats.detect_ats("") is None


def test_html_to_text_strips_tags_and_entities():
    html = "&lt;p&gt;Hello &amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;"
    text = ats._html_to_text(html)
    assert "Hello & welcome" in text
    assert "One" in text and "Two" in text
    assert "<" not in text and "&lt;" not in text


def test_fetch_ats_job_returns_none_for_non_ats():
    assert ats.fetch_ats_job("https://example.com/careers/1") is None


# ── embedded Greenhouse boards ───────────────────────────────────────────────────────────
#
# 2026-07-30. A pasted job did nothing: enrichment recorded "no data extracted", the
# description stayed empty, and every downstream stage then correctly had nothing to do — so
# score, tailor, cover and apply all silently skipped it and the job looked ignored.
#
#     https://avathongov.com/careers-job-listings/?gh_jid=4683241005
#
# It is a Greenhouse board EMBEDDED on the employer's own site. `detect_ats` matched only
# "greenhouse.io" in the hostname, so it was never recognised as an ATS at all and fell through
# to the generic scrape cascade — which hits a 403 bot wall on that domain.

EMBED_URL = "https://avathongov.com/careers-job-listings/?gh_jid=4683241005&gh_src=75e3867c5us"


def test_an_embedded_board_is_detected_by_its_query_string():
    """The hostname is the EMPLOYER's, not Greenhouse's. `gh_jid` is the only signal."""
    from applypilot.enrichment.ats import detect_ats

    assert detect_ats(EMBED_URL) == "greenhouse"


def test_a_plain_employer_url_is_still_not_an_ats():
    """The check must not turn every careers page into a Greenhouse lookup."""
    from applypilot.enrichment.ats import detect_ats

    assert detect_ats("https://avathongov.com/careers-job-listings/") is None
    assert detect_ats("https://acme.com/jobs/123") is None


def test_the_hosted_greenhouse_forms_still_work():
    from applypilot.enrichment.ats import detect_ats

    assert detect_ats("https://job-boards.greenhouse.io/affirm/jobs/7778204003") == "greenhouse"
    assert detect_ats("https://boards.greenhouse.io/embed/job_app?for=ag&token=1") == "greenhouse"


def test_the_board_token_is_resolved_from_the_redirect(monkeypatch):
    """`?gh_jid=` carries the job id but NOT the board, and the token is rarely guessable —
    "Avathon Government" is board `ag`. Greenhouse's embed endpoint resolves it via the URL it
    redirects to."""
    from applypilot.enrichment import ats

    class _Resp:
        status_code = 200
        url = "https://job-boards.greenhouse.io/embed/job_app?for=ag&token=4683241005"

    monkeypatch.setattr(ats.httpx, "get", lambda *a, **k: _Resp())
    assert ats._greenhouse_board_token("4683241005") == "ag"


def test_a_failed_token_lookup_returns_none_rather_than_raising(monkeypatch):
    """Enrichment must degrade to "could not read this page", never take the run down."""
    from applypilot.enrichment import ats

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ats.httpx, "get", boom)
    assert ats._greenhouse_board_token("123") is None


def test_an_embedded_url_fetches_through_the_api_not_the_blocked_page(monkeypatch):
    """The employer's page 403s. The API does not — and returns clean content rather than HTML
    that has to be scraped out of a rendered board."""
    from applypilot.enrichment import ats

    seen = {}

    def fake_json(api_url):
        seen["api"] = api_url
        return {"content": "<p>Build AI systems.</p>",
                "absolute_url": "https://avathongov.com/careers-job-listings/?gh_jid=4683241005"}

    monkeypatch.setattr(ats, "_greenhouse_board_token", lambda jid: "ag")
    monkeypatch.setattr(ats, "_get_json", fake_json)

    out = ats._greenhouse(EMBED_URL)
    assert out and "Build AI systems." in out["full_description"]
    assert "boards-api.greenhouse.io/v1/boards/ag/jobs/4683241005" in seen["api"]


def test_an_unresolvable_board_gives_up_cleanly(monkeypatch):
    from applypilot.enrichment import ats

    monkeypatch.setattr(ats, "_greenhouse_board_token", lambda jid: None)
    assert ats._greenhouse(EMBED_URL) is None
