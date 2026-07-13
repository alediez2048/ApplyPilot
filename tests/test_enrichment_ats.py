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
