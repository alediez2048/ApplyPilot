"""A Gmail link beside LinkedIn and Apollo, on the contact meta row.

`gmailThreadUrl` already existed and read ONE source — `reply_to.thread_id`, which is computed
from a stored thread and therefore only exists once somebody has answered. Live numbers:

    contacts with a thread Gmail gave us at SEND time   141
    contacts who have replied                            11

So the link was missing for ~130 people we have a real thread with. `contacts.thread_id` was on
the row the whole time; `_contact_payload` shipped only `threaded`, the BOOLEAN derived from it —
the UI could say a thread existed and had no way to open it (§Lessons 47's shape: the payload
carrying a summary of a value instead of the value).

The fallback is not a consolation prize. A Gmail SEARCH by address is the only thing that finds
the conversations a stored id cannot — a thread they started, a reply from a different address,
one where we were only Cc'd. That is the same reason `replies.sync_all_with()` searches by
address rather than by id.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from browser_stubs import BROWSER_GLOBALS

from applypilot import web_dashboard as wd


def _link(contact, tmp_path):
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "gm.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),
  querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,
  activeElement:null };
const SRC = """ + json.dumps(src) + """;
const F = (new Function(SRC + '; return { gmailMetaLink, gmailThreadUrl, gmailSearchUrl };'))();
const c = """ + json.dumps(contact) + """;
console.log(JSON.stringify({ html: F.gmailMetaLink(c), thread: F.gmailThreadUrl(c),
                             search: F.gmailSearchUrl(c) }));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── which thread it opens ───────────────────────────────────────────────────

def test_a_reply_thread_is_used_when_there_is_one(tmp_path):
    out = _link({"email": "k@x.test", "reply_to": {"thread_id": "REPLIED"},
                 "thread_id": "SENT"}, tmp_path)
    assert out["thread"].endswith("#all/REPLIED")


def test_the_SEND_thread_is_used_when_nobody_has_replied(tmp_path):
    """The whole fix. 141 contacts have this and 11 have the other, so reading only `reply_to`
    left the link missing for most of the people it was built for."""
    out = _link({"email": "k@x.test", "reply_to": None, "thread_id": "SENT"}, tmp_path)
    assert out["thread"].endswith("#all/SENT")
    assert "gm-link" in out["html"]
    assert "Gmail ↗" in out["html"]


def test_no_thread_falls_back_to_a_search(tmp_path):
    out = _link({"email": "dana@example.test", "reply_to": None, "thread_id": ""}, tmp_path)
    assert out["thread"] == ""
    assert "#search/dana%40example.test" in out["html"]


def test_the_two_are_not_the_same_link(tmp_path):
    """They do different things: one opens the conversation, the other opens a search that may
    return nothing. A shared label would make a search look like a thread — which is the promise
    §Lessons 88 is about, in a link instead of a button."""
    threaded = _link({"email": "k@x.test", "thread_id": "T"}, tmp_path)["html"]
    searched = _link({"email": "k@x.test", "thread_id": ""}, tmp_path)["html"]
    assert "gm-link" in threaded and "gm-alt" not in threaded
    assert "gm-alt" in searched and "gm-link" not in searched
    assert "search" in searched.lower() and "search" not in threaded.lower()


def test_the_search_label_says_it_is_a_search(tmp_path):
    out = _link({"email": "k@x.test", "full_name": "Dana", "thread_id": ""}, tmp_path)
    assert "Gmail search ↗" in out["html"]
    assert "No thread was captured" in out["html"]


# ── it is always there when it can be ───────────────────────────────────────

def test_a_contact_with_no_address_gets_nothing(tmp_path):
    """The one case where there is genuinely nothing to open. Rendering a dead link would be
    worse than omitting it — there is no Gmail page for a person with no address."""
    out = _link({"email": "", "thread_id": ""}, tmp_path)
    assert out["html"] == ""


def test_a_contact_never_emailed_still_gets_the_search(tmp_path):
    """Guard the guard: gating on `emailed` would hide the link for exactly the people whose
    thread we could not have captured — the ones who wrote to US first."""
    out = _link({"email": "k@x.test", "emailed": False, "thread_id": ""}, tmp_path)
    assert "#search/" in out["html"]


@pytest.mark.parametrize("tid", ["a b", "a/b", "a#b", "a&b"])
def test_thread_ids_are_url_encoded(tid, tmp_path):
    out = _link({"email": "k@x.test", "thread_id": tid}, tmp_path)
    assert " " not in out["thread"]
    assert out["thread"].count("#") == 1, "an unescaped # would truncate the fragment"


def test_the_address_is_url_encoded_in_the_search(tmp_path):
    out = _link({"email": "a+b@x.test", "thread_id": ""}, tmp_path)
    assert "a%2Bb%40x.test" in out["search"]


# ── it is in the row the operator asked for ─────────────────────────────────

def test_it_renders_beside_linkedin_and_apollo():
    """Not in the conversation view two clicks down — §Lessons 89, where a working control in the
    wrong room was reported three times as not existing."""
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    meta = js[js.index('<div class="cmeta">'):]
    meta = meta[:meta.index("</div>")]
    assert "LinkedIn ↗" in meta and "Apollo ↗" in meta
    assert "${gmailMetaLink(c)}" in meta


def test_the_payload_carries_the_thread_id():
    """`threaded` — the boolean derived from it — was all that shipped, so the UI could say a
    thread existed and not link to it."""
    import inspect
    src = inspect.getsource(wd)
    assert '"thread_id": (c.get("thread_id") or "").strip(),' in src


def test_the_payload_still_carries_the_boolean():
    """Guard the guard: `threaded` drives the "won't thread" warning on the follow-up card, and
    replacing it rather than adding beside it would silently disable that."""
    import inspect
    assert '"threaded"' in inspect.getsource(wd)
