"""Opening a contact card pulls the latest Gmail for that person, once.

Asked for as: *"every time I click on a contact card it autofetches the latest gmail
conversation... I keep having to click fetch to make sure I got the latest email I sent, this is
important for context for the next email."*

The reason it is genuinely missing: the reply poller only covers contacts IN PLAY and runs every
five minutes, so an email sent by hand from Gmail two minutes ago is not on the card, and the
next draft is written without it.

The whole risk is WHERE it fires. `#jobs` is replaced wholesale every 2.5s, so a fetch on the
render path is a Gmail round-trip every 2.5 seconds per open card — §Lessons 26, where one HTTP
call per job took `/api/status` from 0.043s to 2.4s and `test_query_budget.py` stayed green
throughout because none of it was SQL.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def node():
    if not shutil.which("node"):
        pytest.skip("node not available")


CONTACT = {"id": "c1", "full_name": "Diego Bodart", "email": "d@bigco.test", "hot": False}
JOB = {"url": "http://j/1", "title": "Role", "company": "BigCo", "contacts": [CONTACT],
       "status": "applied"}


def _probe(tmp_path, script):
    """Counts real POSTs by replacing `post` after the source is evaluated, so the module's own
    call sites are the ones being measured."""
    import applypilot.web_dashboard as wd
    from browser_stubs import BROWSER_GLOBALS
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    f = tmp_path / "probe.mjs"
    f.write_text(
        BROWSER_GLOBALS
        + "const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},"
          " closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},"
          " getAttribute:()=>null, addEventListener(){}, appendChild(){},"
          " classList:{add(){},remove(){},toggle(){}}, dataset:{} });\n"
          "globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),"
          " querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,"
          " activeElement:null };\n"
        + f"const SRC = {json.dumps(src)};\n"
        + f"const JOB = {json.dumps(JOB)};\n" + script, encoding="utf-8")
    proc = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2500]
    return json.loads(proc.stdout.strip().splitlines()[-1])


#: Boot the module, hand it one job, stub `post`/`refresh`, and return the handles under test.
_BOOT = ("const F = (new Function(SRC + '; return { toggleContact, setChannel, autoSyncGmail,"
         " contactRow, peopleList, CONTACT_OPEN, SYNC_AT,"
         " boot: (jobs, p, cs) => { LAST_JOBS = jobs; post = p; CONTENT_SCOPE = cs;"
         "   refresh = () => {}; } };'))();\n"
         "const CALLS = [];\n"
         "F.boot([JOB], async (path, body) => { CALLS.push(path); "
         "  return {ok:true, messages:0, message:'nothing new'}; }, true);\n")


def test_opening_a_card_fetches_gmail(tmp_path):
    got = _probe(tmp_path, _BOOT + "await F.toggleContact('c1');\n"
                 "console.log(JSON.stringify({calls: CALLS}));\n")
    assert got["calls"] == ["/api/contact/sync-gmail"]


def test_RENDERING_the_card_fetches_NOTHING(tmp_path):
    """The §Lessons 26 guard. Rendering happens every 2.5s forever; clicking happens when the
    operator asks. A test that only proves the fetch HAPPENS cannot tell the two apart."""
    got = _probe(tmp_path, _BOOT
                 + "F.CONTACT_OPEN.add('c1');\n"
                   "for (let i = 0; i < 5; i++) F.peopleList(JOB);\n"
                   "console.log(JSON.stringify({calls: CALLS}));\n")
    assert got["calls"] == [], f"the render path is calling Gmail: {got['calls']}"


def test_re_opening_within_the_window_costs_nothing(tmp_path):
    got = _probe(tmp_path, _BOOT + "await F.toggleContact('c1');\n"
                 "await F.toggleContact('c1');\n"          # closed
                 "await F.toggleContact('c1');\n"          # re-opened straight away
                 "console.log(JSON.stringify({calls: CALLS}));\n")
    assert got["calls"] == ["/api/contact/sync-gmail"], "re-opening a card re-hit Gmail"


def test_it_fetches_again_once_the_window_has_passed(tmp_path):
    """The negative control: a cache with no expiry would pass the test above and never pick up
    the email the operator sent five minutes ago, which is the entire request."""
    got = _probe(tmp_path, _BOOT + "await F.toggleContact('c1');\n"
                 "F.SYNC_AT.set('c1', Date.now() - 120000);\n"
                 "F.CONTACT_OPEN.delete('c1');\n"
                 "await F.toggleContact('c1');\n"
                 "console.log(JSON.stringify({calls: CALLS}));\n")
    assert got["calls"] == ["/api/contact/sync-gmail"] * 2


def test_switching_CHANNEL_on_an_already_open_card_does_not_refetch(tmp_path):
    """`setChannel` also opens a card, so it has to sync — but clicking ✉ then 💬 on a card that
    is already open is not a new open, and three tabs would be three round-trips."""
    got = _probe(tmp_path, _BOOT + "await F.setChannel('c1', 'email');\n"
                 "await F.setChannel('c1', 'phone');\n"
                 "await F.setChannel('c1', 'meetings');\n"
                 "console.log(JSON.stringify({calls: CALLS}));\n")
    assert got["calls"] == ["/api/contact/sync-gmail"]


def test_no_readonly_scope_means_no_call(tmp_path):
    """`gmail.metadata` cannot run a `q=` search at all, so this would be a guaranteed 400 on
    every card open."""
    boot = _BOOT.replace("}; }, true);", "}; }, false);")
    got = _probe(tmp_path, boot + "await F.toggleContact('c1');\n"
                 "console.log(JSON.stringify({calls: CALLS}));\n")
    assert got["calls"] == []


def test_a_contact_with_no_ADDRESS_is_never_searched_for(tmp_path):
    """The search is by address. 85 of the first 105 imported people have none."""
    job = dict(JOB, contacts=[dict(CONTACT, email="")])
    got = _probe(tmp_path, _BOOT.replace("F.boot([JOB]", f"F.boot([{json.dumps(job)}]")
                 + "await F.toggleContact('c1');\n"
                   "console.log(JSON.stringify({calls: CALLS}));\n")
    assert got["calls"] == []
