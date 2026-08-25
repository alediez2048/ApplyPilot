"""A posting served inside an iframe is still the posting.

Reported as an application that did not go through end to end. The row stopped at ENRICH with
`no data extracted`, so there was no description, so scoring, tailoring and the cover letter
never ran, and `queue_for_apply` requires `tailored_resume_path` — the apply could not start.

Measured on the live URL before changing anything:

    career-schwab.icims.com/jobs/123453/job   200, 206KB, 31 iframes,
                                              title "Finance, Service, Engineering, & Developer
                                              Jobs | Schwab Jobs"          <- a careers SHELL
    ...the same URL with ?in_iframe=1          200, 42KB, 27k chars,
                                              title "Production Support Engineer in Austin,
                                              Texas"                       <- the actual posting

Every tier of the cascade reads the main document, and `extract_main_content` deletes `iframe`
outright, so the content was removed before extraction rather than missed by it.
"""
import pytest

from applypilot.enrichment import detail

PAGE = "https://career-schwab.icims.com/jobs/123453/job"
FRAME = "https://career-schwab.icims.com/jobs/123453/job?in_iframe=1"


@pytest.mark.parametrize("frames,expected,why", [
    ([FRAME], FRAME, "the same-host frame is the posting"),
    (["https://www.youtube.com/embed/x"], "", "a third party's page is not this employer's job"),
    (["https://ads.doubleclick.net/f"], "", "tracking frames are not content"),
    ([], "", "no frames, nothing to follow"),
    (["about:blank"], "", "about:blank is not a page"),
    (["javascript:void(0)"], "", "not a fetchable scheme"),
    ([PAGE], "", "the page itself would be a loop"),
    (["https://ads.example.com/x", FRAME], FRAME, "the same-host one is chosen from a mix"),
])
def test_only_a_same_host_frame_is_followed(frames, expected, why):
    assert detail.same_host_frame_url(PAGE, frames) == expected, why


def test_a_lookalike_host_is_not_followed():
    """Whole host strings, never a substring (§Lessons 1). `icims.com.evil.test` ends with the
    real host's name and is a different server entirely."""
    assert detail.same_host_frame_url(
        PAGE, ["https://career-schwab.icims.com.evil.test/jobs/1/job"]) == ""


def test_a_page_with_no_host_follows_nothing():
    assert detail.same_host_frame_url("", [FRAME]) == ""
    assert detail.same_host_frame_url("not a url", [FRAME]) == ""


# ── the cascade reads the frame IN PLACE, and only after failing ───────────

class _Ctx:
    """A page or a frame: the cascade uses the same API for both."""

    def __init__(self, url, text="", frames=()):
        self.url = url
        self._text = text
        self._frames = list(frames)
        self.goto_calls = []
        self.main_frame = None

    # navigation (a frame must never be asked to do this)
    def goto(self, url, **_):
        self.goto_calls.append(url)
        self.url = url
        return type("R", (), {"status": 200})()

    def wait_for_load_state(self, *a, **k): return None
    def query_selector(self, *a, **k): return None
    def query_selector_all(self, *a, **k): return []
    def evaluate(self, *a, **k): return self._text
    def title(self): return "t"
    def content(self): return self._text


class _Page(_Ctx):
    def __init__(self, frame_ctxs, url=PAGE):
        super().__init__(url)
        self._children = list(frame_ctxs)
        self.main_frame = self

    @property
    def frames(self):
        return [self] + self._children


def _no_llm(monkeypatch, desc=None):
    import applypilot.enrichment.ats as ats
    monkeypatch.setattr(ats, "fetch_ats_job", lambda url: None)
    monkeypatch.setattr(detail, "extract_with_llm",
                        lambda ctx, url: ({"full_description": desc} if desc else {}))


def test_the_frame_is_read_in_place_and_never_navigated(monkeypatch):
    """Navigating to the frame's URL does not work and this is the whole reason the fix is
    shaped this way: iCIMS strips `in_iframe=1` when `window.top === window`, so opening it
    top-level bounces to the shell (1,386 chars against 5,041 read in place)."""
    _no_llm(monkeypatch)
    frame = _Ctx(FRAME)
    page = _Page([frame])
    detail.scrape_detail_page(page, PAGE)
    assert frame.goto_calls == [], "the frame was navigated, which is what strips the parameter"


def test_the_frame_supplies_the_description(monkeypatch):
    """The posting is whatever the frame holds."""
    import applypilot.enrichment.ats as ats
    monkeypatch.setattr(ats, "fetch_ats_job", lambda url: None)
    frame = _Ctx(FRAME)
    page = _Page([frame])

    def llm(ctx, url):
        # Only the FRAME has the posting; the shell has nothing.
        return {"full_description": "Production Support Engineer, Austin."} if ctx is frame else {}

    monkeypatch.setattr(detail, "extract_with_llm", llm)
    out = detail.scrape_detail_page(page, PAGE)
    assert out["full_description"] == "Production Support Engineer, Austin."
    assert out.get("followed_frame") == FRAME


def test_a_third_party_frame_is_not_read(monkeypatch):
    """Reading one means storing somebody else's page as this employer's job description."""
    _no_llm(monkeypatch)
    frame = _Ctx("https://www.youtube.com/embed/x")
    page = _Page([frame])
    out = detail.scrape_detail_page(page, PAGE)
    assert out.get("followed_frame") is None


def test_a_frame_inside_a_frame_is_not_read(monkeypatch):
    """One hop. A frame that frames something is a loop."""
    _no_llm(monkeypatch)
    inner = _Ctx("https://career-schwab.icims.com/jobs/123453/job?in_iframe=1&depth=2")
    frame = _Ctx(FRAME)
    frame.frames = [frame, inner]        # the frame offers a frame of its own
    frame.main_frame = frame
    page = _Page([frame])
    out = detail.scrape_detail_page(page, PAGE)
    assert out.get("followed_frame") in (None, FRAME)
    assert inner.goto_calls == []


def test_a_successful_scrape_never_reaches_the_frame_path(monkeypatch):
    """The guard that makes this safe to add: a page yielding a description returns long before
    the frame logic, so no working scrape can change behaviour."""
    _no_llm(monkeypatch)
    monkeypatch.setattr(detail, "extract_description_deterministic",
                        lambda ctx: "A real description, long enough to be used.")
    monkeypatch.setattr(detail, "extract_apply_url_deterministic", lambda ctx: PAGE)
    frame = _Ctx(FRAME)
    out = detail.scrape_detail_page(_Page([frame]), PAGE)
    assert out["full_description"]
    assert out.get("followed_frame") is None


def test_a_description_found_by_the_LLM_tier_does_not_trigger_the_frame_hop(monkeypatch):
    """Tier 3 is the one success path reaching the end of the function rather than returning
    early, so `not result.get("full_description")` is load-bearing exactly there."""
    _no_llm(monkeypatch, desc="The role, recovered by the LLM.")
    out = detail.scrape_detail_page(_Page([_Ctx(FRAME)]), PAGE)
    assert out["full_description"]
    assert out.get("followed_frame") is None
