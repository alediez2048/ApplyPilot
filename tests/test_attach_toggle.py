"""A global toggle for whether outreach carries the résumé and cover letter.

Some emails are not applications, and attaching two PDFs to them is wrong in a way the recipient
notices. `OUTREACH_ATTACH_DOCS` already existed and is read at STARTUP, which makes it a
deployment setting rather than a control you can flip between two sends.

Three properties carry it, and each one is a bug this repo has already paid for:

**It persists.** A toggle that reverts on the 2.5s refresh, or on a dashboard restart, sends
documents somebody had decided not to send — and the only place they would find out is their
Sent folder. So it is a file in `APP_DIR`, the same reason `apply/pause.py` is.

**One reader.** The dashboard shows what `gmail_send` will actually do, by calling the same
function. The intro-deck PDF rode along on all 34 sent emails while `doctor --config` reported it
OFF, because a default lived in two places (§Lessons 12's shape, in configuration).

**The env var stays the DEFAULT, not the authority.** It is what applies until the operator says
otherwise, and it must not silently win afterwards.
"""

from __future__ import annotations

import pytest

from applypilot.networking import gmail_send as gs


@pytest.fixture()
def app_dir(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.delenv("OUTREACH_ATTACH_DOCS", raising=False)
    return tmp_path


# ── the default ─────────────────────────────────────────────────────────────

def test_it_is_on_by_default(app_dir):
    """Unchanged behaviour for anyone who never touches it: an application email carries the
    documents it is about."""
    assert gs.attachments_enabled() is True


def test_the_env_var_is_still_the_default(app_dir, monkeypatch):
    monkeypatch.setenv("OUTREACH_ATTACH_DOCS", "0")
    assert gs.attachments_enabled() is False


def test_the_operator_override_beats_the_env_var(app_dir, monkeypatch):
    """The env var is what applies UNTIL somebody says otherwise. If it kept winning, the
    checkbox would be a control that does nothing, which is worse than no checkbox."""
    monkeypatch.setenv("OUTREACH_ATTACH_DOCS", "1")
    gs.set_attachments_enabled(False)
    assert gs.attachments_enabled() is False


def test_the_override_can_turn_it_back_on(app_dir, monkeypatch):
    monkeypatch.setenv("OUTREACH_ATTACH_DOCS", "0")
    gs.set_attachments_enabled(True)
    assert gs.attachments_enabled() is True


# ── it persists ─────────────────────────────────────────────────────────────

def test_it_survives_a_restart(app_dir):
    """The whole reason it is a file. In-memory state would silently revert to "attach" the next
    time the dashboard came up, and nothing would say so."""
    gs.set_attachments_enabled(False)
    assert (app_dir / gs._ATTACH_FLAG).exists()
    assert gs._read_attach_flag() is False        # what a fresh process would read
    assert gs.attachments_enabled() is False


def test_setting_it_returns_what_is_actually_in_force(app_dir):
    """Read back rather than echoed. A setter that returns its own argument reports success for
    a write that failed, and the UI would show a state nothing honoured."""
    assert gs.set_attachments_enabled(False) is False
    assert gs.set_attachments_enabled(True) is True


def test_an_unwritable_flag_does_not_raise(app_dir, monkeypatch):
    """Persisting is best-effort. Losing the toggle is recoverable; a 500 on the send path is
    not, and this runs next to sending."""
    monkeypatch.setattr(gs, "_attach_flag_path", lambda: app_dir / "no" / "such" / "dir" / "f")
    gs.set_attachments_enabled(False)             # must not raise


def test_a_corrupt_flag_falls_back_to_the_default(app_dir):
    """Someone hand-edits the file. Refusing to answer would break sending over a preference."""
    (app_dir / gs._ATTACH_FLAG).write_text("banana", encoding="utf-8")
    assert gs._read_attach_flag() is None
    assert gs.attachments_enabled() is True


# ── it reaches the send path ────────────────────────────────────────────────

def test_no_attachments_are_resolved_when_it_is_off(app_dir):
    """The point. `job_attachments` is the only thing that builds the PDF list, and it is
    consulted by `send_outreach` alone — follow-ups never attached and are unaffected."""
    gs.set_attachments_enabled(False)
    assert gs.job_attachments("http://any/job") == []


def test_the_dashboard_reads_the_SAME_function(app_dir):
    """Not a second copy of the rule. The intro-deck PDF was attached to all 34 sent emails
    while `doctor --config` reported it off, because `_intro_deck_path` defaulted it "1" while
    `settings.py` declared False — a default in two places is two defaults."""
    from applypilot import web_dashboard as wd
    gs.set_attachments_enabled(False)
    assert wd._attach_docs_state() is False
    gs.set_attachments_enabled(True)
    assert wd._attach_docs_state() is True


def test_the_payload_carries_it():
    """The checkbox and the card both render from `attach_docs`. A checkbox holding its own idea
    of the state is one that disagrees with what goes out."""
    import inspect

    from applypilot import web_dashboard as wd
    assert '"attach_docs"' in inspect.getsource(wd)


def test_the_endpoint_is_wired():
    import inspect

    from applypilot import web_dashboard as wd
    src = inspect.getsource(wd)
    assert '"/api/attach-docs"' in src
    assert "set_attachments_enabled" in src


def _dashboard_js() -> str:
    from pathlib import Path
    return Path(gs.__file__).parent.parent.joinpath("static/dashboard.js").read_text()


def test_the_toggle_is_a_real_button_in_the_action_row():
    """It shipped as a `<span>` beside the EMAIL label: button-shaped, not a button, and the
    first thing it got was a click and a bug report. Rendering something that looks like a
    control and is not one is a worse §Lessons 43 than hiding it — a hidden control is missing,
    a fake one is a promise the page does not keep."""
    js = _dashboard_js()
    assert "<button class=\"attach-btn" in js
    assert "toggleAttachDocs(this)" in js
    assert "attach-tag" not in js, "the fake badge is back"
    # In the row with Save / Regenerate / Copy / Send, not floating beside a label.
    row = js[js.index('<div class="dbtns">'):]
    assert "${attachBtn}" in row[:400]


def test_the_button_says_it_is_global():
    """It lives on one person's card and changes every email. A per-contact-looking control with
    global effect is the shape that gets clicked by mistake."""
    js = _dashboard_js()
    assert "all emails" in js
    assert "not per contact" in js.lower()


def test_the_button_renders_from_the_served_state():
    js = _dashboard_js()
    assert "ATTACH_DOCS ? 'Docs ON' : 'Docs OFF'" in js


def test_there_is_exactly_one_control():
    """The console checkbox was removed when this moved. Two controls for one global state drift
    the moment one of them stops being re-rendered."""
    from pathlib import Path
    html = Path(gs.__file__).parent.parent.joinpath("static/index.html").read_text()
    assert "attachDocs" not in html
    assert _dashboard_js().count("toggleAttachDocs") == 2   # the onclick and the definition
