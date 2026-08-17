""""⤓ Fetch from Gmail" fetches the actual message, not Gmail's 200-character preview.

Reported as: "when I go to the actual gmail thread the message is not cut off at all."

`gmail_read.thread_messages` requests `format="metadata"`, which by design returns headers and
Gmail's own `snippet` and no body at all. That was the only option when the token carried
`gmail.metadata` — and `gmail.readonly` has been granted since 2026-07-31 with nothing changed to
use it. So the button that exists to read a conversation in full stored exactly the same preview
the automatic sync already had, and `PASTED_MAX = 2000` recorded an intent that was never met.

Measured before the fix: of **646 stored messages, none exceeded 200 characters** and half sat at
151-199 — the shape of Gmail's snippet rather than of anybody's writing.

The narrowing is unchanged and is the point: the poller and the card-open sync still store the
snippet. A BODY is read only when the operator asks for one conversation by name.
"""

from __future__ import annotations

import base64

import pytest

from applypilot.domain import conversations as cv
from applypilot.networking import gmail_read


def enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# ── pulling the body out of a MIME tree ─────────────────────────────────────

def test_it_finds_the_plain_text_body():
    payload = {"mimeType": "text/plain", "body": {"data": enc("the whole message")}}
    assert gmail_read._walk_parts(payload) == ("the whole message", "")


def test_it_recurses_into_NESTED_multiparts():
    """`multipart/mixed` wrapping `multipart/alternative` wrapping the two bodies is the
    ordinary shape. Reading only the top level finds neither."""
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": enc("real text")}},
            {"mimeType": "text/html", "body": {"data": enc("<p>real text</p>")}}]}]}
    plain, html = gmail_read._walk_parts(payload)
    assert plain == "real text" and html == "<p>real text</p>"


def test_plain_text_is_PREFERRED_over_html():
    """The HTML alternative carries markup that would reach a drafting prompt as content."""
    # The two parts must differ in CONTENT, not only in markup: with `<b>hi</b>` and `hi` the
    # stripped html equals the plain text, so preferring html passes and the mutation survives.
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": enc("<b>HTML VERSION</b>")}},
        {"mimeType": "text/plain", "body": {"data": enc("PLAIN VERSION")}}]}
    assert gmail_read.message_body_from(payload) == "PLAIN VERSION"


def test_html_is_a_FALLBACK_when_there_is_no_plain_part():
    """Some clients emit HTML only. Dropping the message entirely is worse than stripping it."""
    got = gmail_read._strip_html("<div>hello<br>there</div><script>evil()</script>")
    assert "hello" in got and "there" in got and "evil" not in got


def test_gmail_returns_base64URL_not_standard_base64():
    """'-' and '_' stand in for '+' and '/'. Decoding with the standard alphabet raises or
    produces mojibake on any body containing those bytes."""
    raw = "☕ résumé — attached"
    assert gmail_read._decode(enc(raw)) == raw


def test_a_malformed_body_returns_EMPTY_rather_than_raising():
    """This runs inside a request handler; one unusual message must not take the fetch down."""
    assert gmail_read._decode("!!! not base64 !!!") == ""


def test_no_scope_means_the_api_is_never_CALLED(monkeypatch):
    """The gate, asserted on the CALL rather than the return value.

    With no service configured `message_body` returns "" regardless, so checking the result
    passes whether the gate exists or not — and the mutation that deleted it survived.
    """
    called = []
    monkeypatch.setattr(gmail_read, "_service", lambda: called.append(1) or None)
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (False, "off"))
    assert gmail_read.message_body("m1") == ""
    assert called == [], "it tried to reach Gmail without the scope"
    # ...and WITH the scope it does get that far, so this cannot pass by never calling at all.
    monkeypatch.setattr(gmail_read, "can_read_content", lambda: (True, "ok"))
    gmail_read.message_body("m1")
    assert called == [1]


# ── what surrounds the message is not the message ──────────────────────────

def test_a_signature_delimiter_ends_the_message():
    got = cv.strip_footer("Happy to chat Tuesday.\n\n-- \nJane Doe\nVP Engineering\nAcme")
    assert got == "Happy to chat Tuesday."


@pytest.mark.parametrize("boiler", [
    "IMPORTANT NOTICE: The contents of this email are confidential",
    "CONFIDENTIALITY NOTICE: this message is intended only for",
    "This email and any files transmitted are confidential",
    "The contents of this email are privileged",
])
def test_corporate_disclaimers_are_dropped(boiler):
    """A real reply here is 2781 characters of which the last third is a disclaimer — all of it
    about to reach the drafting prompt as something the sender said."""
    got = cv.strip_footer(f"Yes, Tuesday works.\n{boiler} and must not be forwarded.")
    assert got == "Yes, Tuesday works."


def test_a_signature_service_tracking_pixel_is_dropped():
    got = cv.strip_footer("See you then.\n[https://tracy.srv.wisestamp.com/px/642316.png]")
    assert got == "See you then."


def test_inline_image_refs_are_REMOVED_not_cut_at():
    """`[cid:…]` appears mid-message as often as at the end, so cutting there would discard the
    rest of the reply."""
    got = cv.strip_footer("Here is the deck [cid:abc-123] — let me know what you think.")
    assert "let me know what you think" in got and "cid:" not in got


def test_a_message_that_IS_a_disclaimer_is_kept_whole():
    """Cutting to nothing is worse than leaving boilerplate in — the same conservatism
    `strip_quoted_tail` already follows."""
    only = "CONFIDENTIALITY NOTICE: this message is intended only for the addressee."
    assert cv.strip_footer(only) == only


def test_ordinary_text_is_untouched():
    """The negative control: an over-eager pattern would silently truncate real replies."""
    body = ("Thanks for reaching out! I appreciate your interest in the Program Manager role.\n\n"
            "This is a great fit and I would love to talk. Does Thursday work?")
    assert cv.strip_footer(body) == body


def test_quoting_and_footers_are_BOTH_stripped():
    """A full body carries the whole quoted chain AND a signature; the snippet rarely reached
    either, which is why this only matters now that bodies are fetched."""
    raw = ("Tuesday is perfect.\n\n-- \nJane\n\nOn Mon, Aug 4, 2026 at 9:00 AM Jorge wrote:\n"
           "> my original email")
    got = cv.strip_footer(cv.strip_quoted_tail(raw))
    assert got == "Tuesday is perfect."


# ── the two declared bounds ────────────────────────────────────────────────

def test_the_EXPLICIT_fetch_stores_more_than_the_automatic_one(tmp_path, monkeypatch):
    """`upsert_messages` caps at the write so no caller can bypass it — but it capped the
    deliberate fetch at the automatic bound too, so the button that exists to read a message in
    full stored the same 200-character preview. Measured before: of 646 stored messages, NONE
    exceeded 200 characters.
    """
    import applypilot.database as database
    from applypilot.networking import messages as _m, store
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _m.init_messages(conn)
    long_body = "x" * 1500

    def row(mid):
        return {"message_id": mid, "thread_id": "t1", "contact_id": "c1",
                "job_url": "http://j/1", "direction": "in", "from_addr": "a@b.test",
                "from_name": "A", "to_addrs": [], "cc_addrs": [], "subject": "s",
                "sent_at": "2026-08-01T09:00", "rfc_message_id": "<x>", "snippet": long_body}

    _m.upsert_messages([row("auto")], conn)                 # the poller / card-open sync
    _m.upsert_messages([row("explicit")], conn, full=True)  # ⤓ Fetch from Gmail
    got = {m["message_id"]: len(m["snippet"] or "")
           for m in _m.thread_for_contact("c1", conn)}
    assert got["auto"] == _m.SNIPPET_MAX
    assert got["explicit"] == 1500, "the deliberate fetch was capped at the automatic bound"
    assert got["explicit"] <= _m.PASTED_MAX


def test_the_auto_sync_never_DOWNGRADES_a_fetched_body(tmp_path, monkeypatch):
    """The fix was being undone within minutes of shipping.

    "Never downgrade what we already hold" was written for OUTBOUND only, and the inbound branch
    took Gmail's ~200-character snippet unconditionally. Harmless while that was the best text
    available; destructive the moment `fetch_thread_text` started storing real bodies, because
    opening the card auto-syncs. Measured 20 minutes after shipping the body fetch: a thread
    stored at 613, 486 and 262 characters was back to ZERO messages over 200.
    """
    from applypilot.networking import replies
    long_body = "the whole message, fetched on purpose. " * 12    # ~460 chars
    short = "the whole message, fetched on p"
    assert replies._keep_longer(short, long_body) == "", "the preview overwrote the full body"
    # `.strip()` — the helper normalises, so compare against the normalised form.
    assert replies._keep_longer(long_body, short) == long_body.strip(), \
        "a real improvement was dropped"
    assert replies._keep_longer("", long_body) == ""
    assert replies._keep_longer(long_body, "") == long_body.strip()


def test_the_rule_is_the_same_in_BOTH_directions(tmp_path):
    """It was applied to our own sent text and not to theirs — §Lessons 49's shape, and the half
    that was missing is the half that mattered once bodies existed."""
    import inspect

    from applypilot.networking import replies
    src = inspect.getsource(replies.sync_all_with)
    assert "_keep_longer" in src
    # The snippet expression must not branch on direction. Checked on the ROW BUILD rather than
    # the whole function, which mentions direction for other, legitimate reasons.
    row = src[src.index('"snippet"'):src.index('"snippet"') + 200]
    assert "inbound" not in row, "direction still decides whether a stored body survives"


def test_PRESERVED_text_is_not_re_capped(tmp_path, monkeypatch):
    """The second half of the same bug, and the one that made the first fix look ineffective.

    `sync_all_with` correctly declined to overwrite a fetched body — and `upsert_messages` then
    applied the SNIPPET bound to the text it was merely preserving. A thread fetched at
    613/486/262 characters came back 200/200/200/200 after one automatic sync that stored
    nothing new. The cap belongs to what is ARRIVING; stored text was already capped correctly
    when it was written.
    """
    import applypilot.database as database
    from applypilot.networking import messages as _m, store
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _m.init_messages(conn)
    body = "y" * 900

    def row(snippet):
        return {"message_id": "m1", "thread_id": "t1", "contact_id": "c1",
                "job_url": "http://j/1", "direction": "in", "from_addr": "a@b.test",
                "from_name": "A", "to_addrs": [], "cc_addrs": [], "subject": "s",
                "sent_at": "2026-08-01T09:00", "rfc_message_id": "<x>", "snippet": snippet}

    _m.upsert_messages([row(body)], conn, full=True)          # ⤓ Fetch from Gmail
    assert len(_m.thread_for_contact("c1", conn)[0]["snippet"]) == 900
    _m.upsert_messages([row("")], conn)                        # the automatic sync, declining
    assert len(_m.thread_for_contact("c1", conn)[0]["snippet"]) == 900, \
        "the preserved body was re-capped at the snippet bound"


def test_the_RFC_MESSAGE_ID_is_preserved_the_same_way_a_snippet_is(tmp_path, monkeypatch):
    """Same rule, and the more damaging column of the two to lose.

    `INSERT OR REPLACE` writes every column, so a caller that legitimately does not know the RFC
    header erases one the poller had already read off the message. The send paths are exactly
    that caller: they hold Gmail's own message id, not the `Message-ID` header it generated. And
    this column is what a later reply chains `References` from — blanking it tells the recipient's
    mail client that this conversation is a different conversation, which is the 87-ids-across-25-
    threads failure in reverse.
    """
    import applypilot.database as database
    from applypilot.networking import messages as _m, store
    path = tmp_path / "rfc.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _m.init_messages(conn)

    def row(rfc):
        return {"message_id": "m1", "thread_id": "t1", "contact_id": "c1",
                "job_url": "http://j/1", "direction": "out", "from_addr": "me@x.test",
                "from_name": "", "to_addrs": [], "cc_addrs": [], "subject": "s",
                "sent_at": "2026-08-01T09:00", "rfc_message_id": rfc, "snippet": "hello."}

    _m.upsert_messages([row("<real@header>")], conn)            # the poller, reading headers
    assert _m.thread_for_contact("c1", conn)[0]["rfc_message_id"] == "<real@header>"
    _m.upsert_messages([row("")], conn)                         # a caller that does not know it
    assert _m.thread_for_contact("c1", conn)[0]["rfc_message_id"] == "<real@header>", \
        "an empty rfc_message_id erased the header a reply chains References from"
    # ...and a caller that DOES know it can still correct one.
    _m.upsert_messages([row("<corrected@header>")], conn)
    assert _m.thread_for_contact("c1", conn)[0]["rfc_message_id"] == "<corrected@header>"
