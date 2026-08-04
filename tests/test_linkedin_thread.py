"""Reading an open LinkedIn thread: who is this, and when did each message arrive.

Both answers are derived, and both are load-bearing:

* the wrong PERSON files somebody's reply against a stranger, and nothing downstream could
  tell — the contact exists, the message exists, the row is well-formed;
* a duplicated TIMESTAMP silently destroys messages, because `interactions` keys a row on
  `sha256(contact|kind|at)` and LinkedIn gives one displayed time per GROUP, not per message.

The parser itself is JavaScript running inside the page, so it is tested the same way
`dashboard.js` is: executed under Node against a stub DOM. A selector-driven reader that
nothing exercises goes dead the next time LinkedIn ships a redesign, and goes dead *quietly*.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from applypilot.domain.linkedin_thread import better_name, dedupe_times, match_contact

_EXT = pathlib.Path(__file__).resolve().parents[1] / "extension"


def _c(name: str, cid: str = "", company: str = "") -> dict:
    return {"id": cid or name.lower().replace(" ", "-"), "full_name": name, "company": company}


# ── who is this? ────────────────────────────────────────────────────────────

def test_a_plain_name_matches():
    assert [c["id"] for c in match_contact("Anna Ruiz", [_c("Anna Ruiz")])] == ["anna-ruiz"]


@pytest.mark.parametrize("shown", [
    "Anna Ruiz, PMP",
    "Anna Ruiz 🚀",
    "Anna Ruiz (she/her)",
    "Anna Ruiz | Hiring Applied AI Engineers",
    "Anna Ruiz · Talent",
    "anna ruiz",
    "Annâ Ruíz",
])
def test_linkedin_decoration_does_not_hide_the_person(shown):
    """Nobody's LinkedIn name is just their name. Every one of these is a real shape."""
    assert [c["id"] for c in match_contact(shown, [_c("Anna Ruiz")])] == ["anna-ruiz"], shown


def test_a_tagline_cannot_supply_the_match():
    """"Anna Ruiz | Hiring Marcus Webb" must not match a contact called Marcus Webb.

    Everything after the separator is somebody's marketing copy, and matching on it files a
    message against whoever happens to be named in a headline.
    """
    got = match_contact("Anna Ruiz | Hiring Marcus Webb", [_c("Marcus Webb")])
    assert got == [], got


def test_it_never_substring_matches():
    """§Lessons 1, four shipped bugs: `"arm" in "armanino"`, `"lever.co" in "careers.clever.com"`.

    "Ann Lee" is not "Anna Leeson", and a naive `in` test says it is.
    """
    assert match_contact("Anna Leeson", [_c("Ann Lee")]) == []
    assert match_contact("Ann Lee", [_c("Anna Leeson")]) == []


def test_a_one_word_contact_matches_but_is_flagged():
    """Refusing these was the first design and it was measured wrong: **162 of 185 live
    contacts have no surname**, because Apollo's people search redacts it. Refusing them sent
    88% of threads to a manual picker — the ten-step flow this feature exists to remove.

    Safe because this returns CANDIDATES, not a decision: the basis is carried to the popup,
    which flags it, and the operator confirms against the company shown beside the name.
    """
    got = match_contact("Anna Ruiz", [_c("Anna")])
    assert [c["id"] for c in got] == ["anna"]
    assert got[0]["match_basis"] == "first-name"


def test_a_one_word_contact_only_matches_the_FIRST_name():
    """"Anna" must not match via a word sitting elsewhere in a decorated headline."""
    assert match_contact("Ruiz Anna-Maria Consulting", [_c("Anna")]) == []


def test_a_full_name_match_outranks_a_first_name_one():
    got = match_contact("Anna Ruiz", [_c("Anna", "just-anna"), _c("Anna Ruiz", "full")])
    assert [c["id"] for c in got] == ["full", "just-anna"]
    assert [c["match_basis"] for c in got] == ["full", "first-name"]


# ── the surname Apollo already told us and we threw away ────────────────────

def test_enrichment_may_only_EXTEND_a_stored_name():
    """Apollo's search returns `name: "Sage"`; its enrichment response carries "Sage Soronen"
    and the code read three fields off it and dropped the rest. 162 of 185 contacts are stored
    first-name-only as a result, while their own email (sage.soronen@betterup.co) spells it."""
    assert better_name("Sage", "Sage Soronen") == "Sage Soronen"
    assert better_name("", "Sage Soronen") == "Sage Soronen"


def test_it_never_renames_a_contact_into_a_different_person():
    """The dangerous direction. A wrong FIRST name is obvious in a greeting; a wrong FULL name
    is not, and a mismatched enrichment row must not be able to install one."""
    assert better_name("Sage", "Marcus Stefanide") == "Sage"
    assert better_name("Sage Soronen", "Sage") == "Sage Soronen"
    assert better_name("Sage Soronen", "") == "Sage Soronen"
    assert better_name("Sage Soronen", "Sage Anderson") == "Sage Soronen"


def test_an_equal_length_name_is_not_an_upgrade():
    assert better_name("Anna Ruiz", "Anna Ruiz") == "Anna Ruiz"
    assert better_name("Anna Ruiz", "Ruiz Anna") == "Anna Ruiz"


def test_ambiguity_is_returned_not_resolved():
    """Two people really are called Anna Ruiz. Picking one silently is unrecoverable."""
    got = match_contact("Anna Ruiz", [_c("Anna Ruiz", "a", "Yahoo"), _c("Anna Ruiz", "b", "Visa")])
    assert {c["id"] for c in got} == {"a", "b"}


def test_the_fuller_name_ranks_first():
    got = match_contact("Anna Maria Ruiz", [_c("Anna Ruiz", "short"), _c("Anna Maria Ruiz", "full")])
    assert [c["id"] for c in got] == ["full", "short"]


def test_our_own_name_matches_nobody():
    """How the popup tells which sender is you: yours is simply not a contact. That needs no
    selector for "me" and no stored copy of your own name."""
    assert match_contact("Alejandro Diez", [_c("Anna Ruiz"), _c("Gina Johnson")]) == []


# ── when did it arrive? ─────────────────────────────────────────────────────

def test_messages_sharing_a_group_timestamp_get_distinct_times():
    """The whole reason this function exists.

    LinkedIn renders consecutive messages from one person as a GROUP with ONE displayed time.
    `interactions.record` hashes (contact, kind, at) into the row id, so three such messages
    are one row and two of them are gone — no error, no warning, two-thirds of a conversation.
    """
    same = "2026-08-04T14:38:00+00:00"
    out = dedupe_times([{"detail": "a", "at": same}, {"detail": "b", "at": same},
                        {"detail": "c", "at": same}])
    assert len({m["at"] for m in out}) == 3, out
    assert [m["detail"] for m in out] == ["a", "b", "c"], "order was not preserved"


def test_it_only_moves_within_the_displayed_minute():
    """The stored time has to stay truthful to what the operator saw on the page."""
    same = "2026-08-04T14:38:00+00:00"
    out = dedupe_times([{"at": same}] * 5)
    assert all(m["at"].startswith("2026-08-04T14:38:") for m in out), out


def test_it_is_deterministic_so_re_reading_a_thread_is_a_no_op():
    """Re-reading a conversation you have logged before is the NORMAL case — you go back for
    the new message at the bottom. If de-collision drifted, every re-read would append
    duplicates of the whole thread (§Lessons 22: eleven identical BOUNCED entries)."""
    batch = [{"detail": x, "at": "2026-08-04T14:38:00+00:00"} for x in "abc"]
    assert [m["at"] for m in dedupe_times(batch)] == [m["at"] for m in dedupe_times(batch)]


def test_an_unreadable_time_stays_empty():
    """Inventing one would put a message at a moment it demonstrably did not arrive."""
    assert dedupe_times([{"at": "sometime tuesday"}])[0]["at"] == ""
    assert dedupe_times([{"at": ""}])[0]["at"] == ""


def test_distinct_times_are_left_alone():
    ts = ["2026-08-04T14:38:00+00:00", "2026-08-04T15:10:00+00:00"]
    assert [m["at"] for m in dedupe_times([{"at": t} for t in ts])] == ts


# ── the parser, against a stub DOM ──────────────────────────────────────────

# jsdom, not a hand-rolled stub. A stub selector engine that is more forgiving than a browser
# lets the parser pass here and fail on the real page, which is §Lessons 13 exactly — five
# vacuous tests shipped in one session, every one green on the first run. jsdom is a
# devDependency alongside eslint; nothing ships it.
#
# The markup below is the shape read off the LIVE thread on 2026-08-04, including the parts
# that are easy to miss: the date heading rides INSIDE the first event of its date, only the
# first message of a GROUP carries a name and a time, and the list holds loader, typing and
# boundary rows that are not messages at all.
#
# The doctype is load-bearing. Without it jsdom parses in QUIRKS mode, where class selectors
# match case-insensitively — so a control that renamed a class only by case passed happily and
# proved the opposite of what it claimed. The real page is standards mode; the fixture has to be.
_THREAD_HTML = """<!doctype html>
<ul class="msg-s-message-list-content">
  <li class="msg-s-message-list__top-of-list"></li>
  <li class="msg-s-message-list__loader hidden"></li>
  <li class="msg-s-message-list__event clearfix">
    <time class="msg-s-message-list__time-heading">TODAY</time>
    <span class="msg-s-message-group__profile-link">Anna Ruiz</span>
    <time class="msg-s-message-group__timestamp">2:00 AM</time>
    <div class="msg-s-event-listitem msg-s-event-listitem--last-in-group">
      <div class="msg-s-event__content"><p>first from Anna</p></div>
    </div>
  </li>
  <li class="msg-s-message-list__event clearfix">
    <div class="msg-s-event-listitem">
      <div class="msg-s-event__content"><p>still Anna, same group</p></div>
    </div>
  </li>
  <li class="msg-s-message-list__event clearfix">
    <span class="msg-s-message-group__profile-link">Alejandro Diez</span>
    <time class="msg-s-message-group__timestamp">2:38 AM</time>
    <div class="msg-s-event-listitem msg-s-event-listitem--other">
      <div class="msg-s-event__content"><p>my answer</p></div>
    </div>
  </li>
  <li class="msg-s-message-list__typing-indicator-container"></li>
  <li class="msg-s-message-list__bottom-of-list"></li>
</ul>
"""


def _run_parser(body_html: str, url: str = "https://www.linkedin.com/messaging/thread/x/") -> dict:
    """Run the real parser against a real DOM."""
    src = (_EXT / "thread_parser.js").read_text(encoding="utf-8")
    script = f"""
const {{ JSDOM }} = require({json.dumps(str(_EXT.parent / "node_modules" / "jsdom"))});
const dom = new JSDOM({json.dumps(body_html)}, {{ url: {json.dumps(url)} }});
global.document = dom.window.document;
global.location = dom.window.location;
// jsdom does not implement innerText (it is layout-dependent). The parser only ever wants the
// rendered text of a node, so textContent is the honest stand-in here.
Object.defineProperty(dom.window.Element.prototype, 'innerText', {{
  get() {{ return this.textContent; }}, configurable: true }});
{src}
console.log(JSON.stringify(readLinkedInThread()));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60,
                          cwd=str(_EXT.parent))
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:3000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _have_jsdom() -> bool:
    return (_EXT.parent / "node_modules" / "jsdom").exists()


@pytest.mark.skipif(not shutil.which("node") or not _have_jsdom(),
                    reason="node + jsdom required (npm install)")
def test_the_parser_carries_the_sender_across_a_group():
    """The trap the live DOM revealed: a continuation message has NO name and NO timestamp.

    Reading per-message drops the sender on every one of them, and a message with no sender
    cannot be given a direction — so a two-message reply logs as one inbound and one outbound,
    which reads as you having answered when you have not.
    """
    out = _run_parser(_THREAD_HTML)
    assert out["ok"], out
    assert [m["text"] for m in out["messages"]] == [
        "first from Anna", "still Anna, same group", "my answer"], out["messages"]
    assert [m["sender"] for m in out["messages"]] == [
        "Anna Ruiz", "Anna Ruiz", "Alejandro Diez"], "the sender was not carried across the group"


@pytest.mark.skipif(not shutil.which("node") or not _have_jsdom(),
                    reason="node + jsdom required (npm install)")
def test_the_parser_skips_everything_that_is_not_a_message():
    """Loaders, typing indicators and the top/bottom sentinels are `li`s in the same list."""
    out = _run_parser(_THREAD_HTML)
    assert len(out["messages"]) == 3, [m["text"] for m in out["messages"]]


@pytest.mark.skipif(not shutil.which("node") or not _have_jsdom(),
                    reason="node + jsdom required (npm install)")
def test_a_group_hands_its_clock_to_its_continuations():
    """Which is exactly why `dedupe_times` exists, and this is the evidence for it."""
    out = _run_parser(_THREAD_HTML)
    at = [m["at"] for m in out["messages"]]
    assert at[0] and at[0] == at[1], f"the continuation did not inherit the group time: {at}"
    assert at[2] != at[0]
    assert dedupe_times(out["messages"])[1]["at"] != at[0], "the collision survived de-duplication"


@pytest.mark.skipif(not shutil.which("node") or not _have_jsdom(),
                    reason="node + jsdom required (npm install)")
def test_the_parser_says_so_when_it_finds_nothing():
    """LinkedIn will rename these classes. When it does, this must return an error the popup can
    show — never an empty success. "No messages" and "I could not read the page" are opposite
    findings, and §Lessons 44 is what happens when a failure takes the success branch."""
    out = _run_parser("<div class='msg-s-not-the-class'></div>")
    assert out["ok"] is False and "conversation" in out["error"], out


@pytest.mark.skipif(not shutil.which("node") or not _have_jsdom(),
                    reason="node + jsdom required (npm install)")
def test_renaming_one_class_is_visible_rather_than_silent():
    """Negative control. If the parser ever starts finding messages in markup it should not
    understand, the tests above are measuring the fixture and not the parser."""
    broken = _THREAD_HTML.replace("msg-s-message-list__event", "msg-s-message-list__entry")
    out = _run_parser(broken)
    assert out["ok"] and out["messages"] == [], out

