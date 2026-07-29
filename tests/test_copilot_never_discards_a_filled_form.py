"""A co-pilot run that ends without a verdict must hand the browser over, not reap it.

Reported repeatedly: "the browser agent starts filling out the application and once it's almost
done it closes Chrome completely."

The mechanism. `run_job` classifies the outcome by regex over the agent's final text —
`RESULT: NEEDS_REVIEW`, `RESULT: NEEDS_HUMAN`, `RESULT: APPLIED`. `keep_chrome_alive()` was
called for exactly two of those. Everything else fell to the worker's generic failure branch,
and the `finally` ran `cleanup_worker(chrome_proc)`, which kills Chrome.

Two outcomes land there with a form that may be COMPLETE on screen:

  failed:no_result_line  the agent stopped without announcing anything — turn limit, crash,
                         or simply different wording at the end of a long fill
  failed:timeout         we stopped waiting. `proc.wait(timeout=300)` was a bare literal, and
                         a real Deloitte fill already took 208s, so a longer form (Google's
                         runs many screens) blew the 5-minute cap right as it finished

Neither is a diagnosis. Both destroyed finished work. Co-pilot's whole contract is that a human
submits, so an unknown outcome hands over.

What must NOT change: a permanent, diagnosed failure is an ANSWER, not uncertainty. Leaving a
browser open for every expired posting or captcha would block the queue on jobs nobody can
finish — and the queue guard only allows one open review at a time.
"""

from __future__ import annotations

import pytest

from applypilot.apply.launcher import AGENT_TIMEOUT_SECONDS, copilot_should_keep_browser


@pytest.mark.parametrize("result", ["failed:no_result_line", "failed:timeout"])
def test_an_ambiguous_outcome_keeps_the_browser(result):
    """The exact loss. The form may be finished; nothing here says it isn't."""
    assert copilot_should_keep_browser(result) is True


@pytest.mark.parametrize("result", [
    "failed:expired",
    "failed:captcha",
    "failed:login_issue",
    "failed:not_eligible_location",
    "failed:not_a_job_application",
    "failed:already_applied",
])
def test_a_diagnosed_failure_does_not_keep_the_browser(result):
    """These are answers. Holding a browser open for each would block the queue — only one
    pending review is allowed at a time — on applications nobody can complete."""
    assert copilot_should_keep_browser(result) is False


@pytest.mark.parametrize("result", ["applied", "needs_review", "needs_human:captcha",
                                    "skipped", "dryrun", "expired", "", None])
def test_non_failure_outcomes_are_handled_elsewhere(result):
    """needs_review / needs_human already keep the browser through their own branches; this
    helper must not claim them too, or the disposition is decided twice."""
    assert copilot_should_keep_browser(result) is False


def test_case_and_whitespace_do_not_change_the_verdict():
    """The reason is built by string surgery on the agent's own text, so it arrives dirty."""
    assert copilot_should_keep_browser("failed: TIMEOUT ") is True
    assert copilot_should_keep_browser("failed:No_Result_Line") is True


def test_the_agent_timeout_is_well_clear_of_a_real_long_fill():
    """208s was a REAL measured Deloitte fill against a 300s cap — 92 seconds of headroom on a
    form that was not even the longest. This is the number that was cutting runs off."""
    assert AGENT_TIMEOUT_SECONDS >= 600, (
        f"{AGENT_TIMEOUT_SECONDS}s is too close to a measured 208s fill; long applications "
        f"will be killed mid-form again")


def test_the_timeout_is_configurable_and_declared():
    """It was a bare `timeout=300` literal with no way to raise it from config."""
    from applypilot import settings
    names = {s.name for s in settings.SETTINGS}
    assert "APPLY_AGENT_TIMEOUT" in names, "undeclared: invisible to `doctor --config`"


def test_the_timeout_is_not_hardcoded_at_the_call_site():
    """Guards the actual regression: re-introducing `proc.wait(timeout=300)`."""
    import inspect

    from applypilot.apply import launcher
    src = inspect.getsource(launcher)
    assert "timeout=300)" not in src, "a hardcoded 300s wait is back in the launcher"
    assert "proc.wait(timeout=AGENT_TIMEOUT_SECONDS)" in src
