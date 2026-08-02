"""The Tags column and the job search box.

Tags replaced the Links column. Those links were already redundant — the `job` one was
truncated and uncopyable, which is the entire reason the Job TAB exists and carries both URLs
in full — and the width is better spent on what distinguishes one row from another when you are
scanning sixteen of them.

Every tag is DERIVED from fields already on the wire. Nothing is stored, so a tag cannot drift
from the job it describes.

These run the REAL functions under node rather than asserting on source text, because the
failure mode that matters here is a handler that throws at runtime — which blanks the whole
jobs table exactly as silently as a syntax error (§Lessons 7).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from applypilot import web_dashboard

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not available")

_STUBS = """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){},
  scrollIntoView(){},
  classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, appendChild(){}, dataset:{} });
globalThis.document = { getElementById: el, querySelectorAll: ()=>[], querySelector: el,
  addEventListener(){}, activeElement:null, body: el() };
globalThis.window = { open(){}, location:{href:''} };
Object.defineProperty(globalThis, "navigator",
  { value:{ clipboard:{ writeText(){} } }, configurable:true });
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.fetch = async () => ({ json: async () => ({}) });
globalThis.alert = () => {};
globalThis.confirm = () => true;
"""

_EXPORTS = """; return { jobTags, salaryTag, locationTag, tagArg, TAG_FILTER,
  toggleTag, clearTags, jobMatchesTags, jobMatchesQuery, onJobSearch, renderJobsTable };"""


def _js() -> str:
    path = web_dashboard._STATIC_DIR / "dashboard.js"
    if not path.exists():
        pytest.skip("dashboard.js not found")
    return path.read_text(encoding="utf-8")


def _run(driver: str, tmp_path, **payload):
    script = tmp_path / "t.mjs"
    script.write_text(
        _STUBS
        + f"const SRC = {json.dumps(_js())};\n"
        + f"const F = (new Function(SRC + {json.dumps(_EXPORTS)}))();\n"
        + "".join(f"const {k} = {json.dumps(v)};\n" for k, v in payload.items())
        + driver
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _job(**over):
    base = {"url": "http://j/1", "title": "Applied AI Engineer", "company": "Saronic",
            "location": "Austin, Texas, United States", "salary": "$180,000 - $220,000",
            "description": "drone autonomy", "status": "applied", "fit_score": 9,
            "applied_at": "2026-07-20T10:00:00+00:00", "rejected_at": "",
            "application_url": "http://a/1"}
    base.update(over)
    return base


# ── the chips themselves ────────────────────────────────────────────────────

def test_tags_are_derived_from_fields_already_on_the_row(tmp_path):
    out = _run("console.log(JSON.stringify(F.jobTags(JOB)));", tmp_path, JOB=_job())
    kinds = [t["kind"] for t in out]
    labels = " ".join(t["label"] for t in out)
    assert kinds == ["loc", "pay", "fit", "src", "when"], kinds
    assert "Austin, TX" in labels, "the long location was not shortened"
    assert "$180–220k" in labels, "the salary range was not compacted"
    assert "9/10" in labels and "Saronic" in labels


@pytest.mark.parametrize("raw,expect", [
    ("$180,000 - $220,000", "$180–220k"),
    ("$150,000", "$150k"),
    ("$45/hr", "$45/hr"),                       # hourly must not become "$0k"
    ("", ""),
    ("competitive", "competitive"),             # unparseable is still worth showing
])
def test_salary_compacts_without_lying(raw, expect, tmp_path):
    got = _run("console.log(JSON.stringify(F.salaryTag(RAW)));", tmp_path, RAW=raw)
    assert got == expect


@pytest.mark.parametrize("raw,expect", [
    ("Austin, Texas, United States", "Austin, TX"),
    ("Remote - US", "Remote"),
    ("London, United Kingdom", "London, United Kingdom"),
    ("", ""),
])
def test_location_shortens_without_dropping_meaning(raw, expect, tmp_path):
    got = _run("console.log(JSON.stringify(F.locationTag(RAW)));", tmp_path, RAW=raw)
    assert got == expect


def test_a_job_with_no_facets_renders_no_chips(tmp_path):
    bare = _job(location="", salary="", fit_score="", company="", applied_at="", rejected_at="")
    out = _run("console.log(JSON.stringify(F.jobTags(JOB)));", tmp_path, JOB=bare)
    assert out == [], "empty fields produced chips with nothing in them"


# ── the thing that breaks silently ──────────────────────────────────────────

def test_an_apostrophe_in_a_tag_does_not_break_its_click_handler(tmp_path):
    """`esc()` is NOT enough here and this is the whole reason `tagArg` exists.

    esc() turns ' into &#39;, and the HTML parser turns that BACK into ' before JS sees the
    attribute — so `onclick="toggleTag('loc:o'fallon, mo')"` is a syntax error. A broken
    onclick throws silently: the chip renders perfectly and simply does nothing when clicked.
    """
    out = _run(
        """
        const tags = F.jobTags(JOB);
        const arg = F.tagArg(tags[0].k);
        // The rendered attribute must contain no bare quote that could close the string…
        const inner = arg.slice(arg.indexOf("('") + 2, arg.lastIndexOf("')"));
        // …and it must round-trip back to the exact key, or the filter silently never matches.
        const back = decodeURIComponent(inner);
        console.log(JSON.stringify({ raw: tags[0].k, inner, back, safe: !inner.includes("'") }));
        """,
        tmp_path, JOB=_job(location="O'Fallon, Missouri"))
    assert out["safe"], f"an apostrophe survived into the onclick attribute: {out['inner']}"
    assert out["back"] == out["raw"], "the encoded key does not decode back to itself"


# ── filtering ───────────────────────────────────────────────────────────────

def test_two_tags_narrow_rather_than_widen(tmp_path):
    """AND, not OR. With OR a second click returns MORE rows, which reads as the filter being
    broken — the user clicked to narrow and the table grew."""
    out = _run(
        """
        const austin = F.jobTags(A)[0].k, denver = F.jobTags(B)[0].k;
        const one = [A, B].filter(F.jobMatchesTags).length;   // no filter yet
        F.toggleTag(austin);
        const two = [A, B].filter(F.jobMatchesTags).length;
        F.toggleTag(denver);
        const three = [A, B].filter(F.jobMatchesTags).length;
        console.log(JSON.stringify({ one, two, three }));
        """,
        tmp_path, A=_job(location="Austin, TX"), B=_job(url="http://j/2", location="Denver, CO"))
    assert out == {"one": 2, "two": 1, "three": 0}, \
        f"adding a tag did not narrow the result: {out}"


def test_clicking_the_same_tag_twice_removes_it(tmp_path):
    out = _run(
        """
        const k = F.jobTags(JOB)[0].k;
        F.toggleTag(k); const on = F.TAG_FILTER.size;
        F.toggleTag(k); const off = F.TAG_FILTER.size;
        console.log(JSON.stringify({ on, off }));
        """, tmp_path, JOB=_job())
    assert out == {"on": 1, "off": 0}


def test_search_terms_are_ANDed_across_every_visible_field(tmp_path):
    """Two words must narrow. Matching ANY term means typing more makes the table grow."""
    out = _run(
        """
        const r = {};
        for (const q of ['saronic', 'austin', 'saronic austin', 'saronic denver',
                         'DRONE', '  ', 'nonsense']) {
          F.onJobSearch(q);
          r[q.trim() || '(blank)'] = [JOB].filter(F.jobMatchesQuery).length;
        }
        console.log(JSON.stringify(r));
        """, tmp_path, JOB=_job())
    assert out["saronic"] == 1
    assert out["austin"] == 1, "search does not reach the derived location tag"
    assert out["saronic austin"] == 1, "two matching terms excluded the row"
    assert out["saronic denver"] == 0, "terms are ORed — adding a word widened the result"
    assert out["DRONE"] == 1, "search is case-sensitive"
    assert out["(blank)"] == 1, "whitespace-only search hid everything"
    assert out["nonsense"] == 0


def test_search_reaches_the_description_not_just_the_row(tmp_path):
    """"the one about the drone startup" is how a job is actually remembered, and the
    description is truncated to a 900-char excerpt in the row — but it IS on the wire."""
    out = _run(
        """
        F.onJobSearch('autonomy');
        console.log(JSON.stringify([JOB].filter(F.jobMatchesQuery).length));
        """, tmp_path, JOB=_job(title="X", company="Y", location="", salary=""))
    assert out == 1


# ── the hot path ────────────────────────────────────────────────────────────

def test_typing_does_not_refetch_the_status_endpoint():
    """/api/status costs 50 SQL statements. Putting it behind a keystroke is §Lessons 11 and 26
    with a new trigger — the search box would have made the dashboard slower the more precisely
    you searched. Both the search and the tag handlers re-render from LAST_JOBS instead."""
    src = _js()
    for fn in ("function onJobSearch", "function toggleTag", "function clearTags"):
        body = src[src.index(fn):]
        body = body[:body.index("\n}") + 2]
        assert "refresh()" not in body, (
            f"{fn} calls refresh(), which refetches /api/status on every keystroke or click")
        assert "rerenderJobs()" in body, f"{fn} does not re-render locally"
    assert "function rerenderJobs() { renderJobsTable(LAST_JOBS || [], isEditingJobs()); }" in src


def test_the_search_box_is_static_markup():
    """It must NOT be rendered by JS. `refresh()` replaces #jobs wholesale every 2.5s and
    renderJobFilters() replaces #jobFilters — an input rendered into either is destroyed
    mid-keystroke, which is the bug the contact-notes focus guard exists to work around."""
    html = (web_dashboard._STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="jobSearch"' in html, "the search input is not in the static page"
    src = _js()
    assert "id=\"jobSearch\"" not in src and "id='jobSearch'" not in src, \
        "the search input is rendered by JS, so typing will be wiped by the 2.5s refresh"


def test_the_links_column_is_gone_and_the_tab_still_has_the_urls():
    """The column was replaced, not duplicated. The Job tab is where both URLs live in full —
    the table's link was truncated and uncopyable, which is why that tab exists at all."""
    html = (web_dashboard._STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<th>Tags</th>" in html and "<th>Links</th>" not in html
    src = _js()
    assert "links-cell" not in src, "the old links cell is still rendered"
    assert "jd-url" in src, "the Job tab no longer shows the full application URL"
