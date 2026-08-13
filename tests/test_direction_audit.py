"""`doctor --directions` — repairing mail attributed to the wrong side of a conversation.

The operator sends from more than one address. Mail from the one on their RESUME, rather than the
account the app authenticates as, was stored as INBOUND — so the app believed a stranger had
written to it. `cv.timeline` now takes every address, so the SYNC is fixed; these are the rows
written before that.

Measured on the live database: **30 rows across 3 contacts**, including a 19-message conversation
in which 14 of the operator's own emails were filed as the other side's. That contact's composer
offered to reply to the operator's own address, which is what makes this more than bookkeeping —
`direction` decides who owes whom a reply, whether a ladder halts on a "reply", whether the
handoff banner fires, what the temperature band reads, and who `reply_target` addresses.

It stays a command rather than a typed-out UPDATE because `MY_ADDRESSES` can GROW: adding a
fourth alias re-creates exactly this on everything synced under the old set.
"""

from __future__ import annotations

import pytest

from applypilot.database import get_connection, init_db
from applypilot.networking import messages as ms

MINE = "me@work.test"
ALSO_MINE = "me@resume.test"
THEM = "kevin@amsys.test"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_ADDRESS", MINE)
    monkeypatch.setenv("OUTREACH_FROM_ADDRESS", "")
    monkeypatch.setenv("MY_ADDRESSES", ALSO_MINE)
    import applypilot.config as cfg
    monkeypatch.setattr(cfg, "APP_DIR", tmp_path, raising=False)
    init_db()
    conn = get_connection()
    rows = [
        # The bug: our own mail, from the résumé address, filed inbound.
        {"message_id": "a1", "contact_id": "c1", "thread_id": "t1", "job_url": "u",
         "direction": "in", "from_addr": ALSO_MINE, "from_name": "Me", "to_addrs": [THEM],
         "cc_addrs": [], "subject": "Pitch", "sent_at": "2026-05-08T18:52",
         "rfc_message_id": "<a1>", "snippet": "hi"},
        # A genuine reply, in the middle. Must be left exactly alone.
        {"message_id": "b1", "contact_id": "c1", "thread_id": "t1", "job_url": "u",
         "direction": "in", "from_addr": THEM, "from_name": "Kevin", "to_addrs": [ALSO_MINE],
         "cc_addrs": [], "subject": "Re: Pitch", "sent_at": "2026-05-12T20:36",
         "rfc_message_id": "<b1>", "snippet": "sure"},
        # Already correct. Must not be counted as work.
        {"message_id": "c1m", "contact_id": "c1", "thread_id": "t1", "job_url": "u",
         "direction": "out", "from_addr": MINE, "from_name": "Me", "to_addrs": [THEM],
         "cc_addrs": [], "subject": "Re: Pitch", "sent_at": "2026-05-13T10:00",
         "rfc_message_id": "<c1m>", "snippet": "ok"},
        # NEWEST, and ours. This ordering is what the live thread looked like: the operator
        # answered last, and that answer is what `reply_target` then offered to reply to.
        {"message_id": "a2", "contact_id": "c1", "thread_id": "t1", "job_url": "u",
         "direction": "in", "from_addr": ALSO_MINE, "from_name": "Me", "to_addrs": [THEM],
         "cc_addrs": [], "subject": "Re: Pitch", "sent_at": "2026-07-09T15:54",
         "rfc_message_id": "<a2>", "snippet": "again"},
    ]
    ms.upsert_messages(rows, conn)
    return conn


def _dirs(conn):
    return {r[0]: r[1] for r in
            conn.execute("SELECT message_id, direction FROM messages").fetchall()}


# ── the audit reports before it writes ──────────────────────────────────────

def test_the_audit_alone_changes_nothing(db, capsys):
    from applypilot.cli import _audit_directions
    _audit_directions(apply_fix=False)
    out = capsys.readouterr().out
    assert "2 of 4" in out, out
    assert _dirs(db) == {"a1": "in", "a2": "in", "b1": "in", "c1m": "out"}, \
        "a dry run wrote to the database"


def test_the_audit_names_the_address_that_caused_it(db, capsys):
    """"30 rows are wrong" is a statistic. The address is what the operator acts on — it is how
    they find out which mailbox they had forgotten to declare."""
    from applypilot.cli import _audit_directions
    _audit_directions(apply_fix=False)
    out = capsys.readouterr().out
    assert ALSO_MINE in out
    assert "in" in out and "out" in out


# ── the fix ─────────────────────────────────────────────────────────────────

def test_it_corrects_only_our_own_mail(db, capsys):
    from applypilot.cli import _audit_directions
    _audit_directions(apply_fix=True)
    assert _dirs(db) == {"a1": "out", "a2": "out", "b1": "in", "c1m": "out"}


def test_a_real_reply_is_never_touched(db):
    """The expensive direction to get wrong. Flipping a genuine reply to outbound removes it
    from the counter, halts nothing, and loses the one signal the whole CRM is built around."""
    from applypilot.cli import _audit_directions
    _audit_directions(apply_fix=True)
    assert _dirs(db)["b1"] == "in"


def test_running_it_twice_is_a_no_op(db, capsys):
    """Idempotence tested by running it twice, not by reasoning (§Lessons 22)."""
    from applypilot.cli import _audit_directions
    _audit_directions(apply_fix=True)
    after = _dirs(db)
    capsys.readouterr()
    _audit_directions(apply_fix=True)
    out = capsys.readouterr().out
    assert _dirs(db) == after
    assert "All 4 stored messages carry the right direction" in out


def test_with_no_addresses_configured_it_refuses(db, monkeypatch, capsys):
    """Every message looks inbound with an empty set, so a fix would rewrite the whole table."""
    monkeypatch.setenv("GMAIL_ADDRESS", "")
    monkeypatch.setenv("MY_ADDRESSES", "")
    monkeypatch.setenv("OUTREACH_FROM_ADDRESS", "")
    from applypilot.cli import _audit_directions
    _audit_directions(apply_fix=True)
    assert "No addresses configured" in capsys.readouterr().out
    assert _dirs(db) == {"a1": "in", "a2": "in", "b1": "in", "c1m": "out"}


# ── what the repair is FOR ──────────────────────────────────────────────────

def test_after_the_fix_a_reply_no_longer_targets_the_operator(db):
    """The live symptom, in one assertion. Before the repair the newest inbound on this thread
    is the operator's own message, so the composer offers to email themselves."""
    from applypilot.cli import _audit_directions
    from applypilot.domain import conversations as cv

    before = cv.reply_target(ms.thread_for_contact("c1", db), [MINE, ALSO_MINE])
    assert before["to_addr"] == ALSO_MINE, "fixture no longer reproduces the bug"

    _audit_directions(apply_fix=True)
    after = cv.reply_target(ms.thread_for_contact("c1", db), [MINE, ALSO_MINE])
    assert after["to_addr"] == THEM


def test_one_message_shared_by_two_contacts_is_corrected_per_contact(db):
    """`messages` is keyed `(message_id, contact_id)` because one email legitimately belongs to
    several people — §Lessons 36, where `INSERT OR REPLACE` on `message_id` alone moved three
    Writer messages onto one contact and emptied another's conversation. Live: 10 message ids
    are shared by two contacts each.

    **Stated rather than overclaimed:** an UPDATE keyed on `message_id` alone would today write
    the SAME value, because `direction` is a pure function of `from_addr` and all 10 shared ids
    carry one sender (measured: 0 disagree). So the narrow key is defensive, not a live bug fix,
    and a mutation widening it is an equivalent mutant — there is no honest test that kills it,
    and contriving a corrupt fixture to manufacture one would be a test that proves nothing
    (§Lessons 71). What this asserts is the part that IS observable: a shared row is repaired on
    every contact that holds it, and a genuine reply beside it is left alone.
    """
    from applypilot.cli import _audit_directions

    # The SAME Gmail message, stored against a second contact — and correct there, because that
    # row was written after the address set was fixed.
    ms.upsert_messages([
        {"message_id": "a1", "contact_id": "c2", "thread_id": "t1", "job_url": "u",
         "direction": "out", "from_addr": ALSO_MINE, "from_name": "Me", "to_addrs": [THEM],
         "cc_addrs": [], "subject": "Pitch", "sent_at": "2026-05-08T18:52",
         "rfc_message_id": "<a1>", "snippet": "hi"},
        # And one that is genuinely theirs, so the row count cannot be satisfied by luck.
        {"message_id": "z9", "contact_id": "c2", "thread_id": "t1", "job_url": "u",
         "direction": "in", "from_addr": THEM, "from_name": "Kevin", "to_addrs": [ALSO_MINE],
         "cc_addrs": [], "subject": "Re: Pitch", "sent_at": "2026-05-14T10:00",
         "rfc_message_id": "<z9>", "snippet": "ok"},
    ], db)

    _audit_directions(apply_fix=True)
    got = {(r[0], r[1]): r[2] for r in db.execute(
        "SELECT message_id, contact_id, direction FROM messages").fetchall()}
    assert got[("a1", "c1")] == "out"      # repaired
    assert got[("a1", "c2")] == "out"      # was already right, and stays right
    assert got[("z9", "c2")] == "in"       # a real reply on the other contact, untouched


def test_it_is_reachable_from_the_cli(db):
    """A repair nobody can run is not a repair (§Lessons 31). The flags have to exist."""
    import inspect
    from applypilot.cli import doctor
    params = inspect.signature(doctor).parameters
    assert "directions" in params and "fix_directions" in params
