"""What each filter pill means, and filtering the table by band.

Reported as "not sure this is the best way to know what's up with an application" — and the
confusion was the finding rather than a misreading. Four of the seven bands (new → active →
cooling → cold) are a countdown of OUR OWN sending; `warm` and `won` are about what THEY did.
One chip presenting both as a single scale reads as if warm were simply more than active, which
it is not: a cooling job can get a reply tomorrow and an active one can stay silent forever.

There was a standing legend for a day; it is gone. Each pill explains itself on hover instead,
so the explanation sits on the control it describes and is read at the moment it is needed. The
axis a band belongs to therefore has to be stated in its own tooltip — a per-pill tip has no
grouping to carry it, and dropping it would keep the vocabulary and throw away the point.

These run the real functions under node. The failure mode that matters is a handler that throws
at runtime, which blanks the whole jobs table exactly as silently as a syntax error (§Lessons 7).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from applypilot import web_dashboard

from browser_stubs import BROWSER_GLOBALS

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not available")

# `getElementById` has to hand back a DISTINCT node per id and remember what was written to it,
# so a test cannot pass because two renderers wrote to the same shared stub.
_STUBS = """
const NODES = {};
const el = (id) => (NODES[id] = NODES[id] || { id, innerHTML:'', textContent:'', hidden:false,
  value:'', style:{}, dataset:{},
  closest:()=>el('_'), querySelector:()=>el('_'), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){}, scrollIntoView(){},
  classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, appendChild(){} });
globalThis.document = { getElementById: el, querySelectorAll: ()=>[], querySelector: ()=>el('_'),
  addEventListener(){}, activeElement:null, body: el('body') };
globalThis.__nodes = NODES;
""" + BROWSER_GLOBALS

_EXPORTS = """; return { TEMP_BANDS, TEMP_ORDER, TEMP_AXIS_TIP, tempTip, JOB_BUCKETS,
  jobInTemp, jobInBucket, setTempFilter, setJobFilter, renderJobFilters,
  get TEMP_FILTER(){ return TEMP_FILTER; }, get JOB_FILTER(){ return JOB_FILTER; } };"""


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
        # `refresh` is what setTempFilter calls; stubbed so a filter change does not try to fetch.
        + "globalThis.refresh = () => {};\n"
        + f"const F = (new Function(SRC + {json.dumps(_EXPORTS)}))();\n"
        + "".join(f"const {k} = {json.dumps(v)};\n" for k, v in payload.items())
        + driver,
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _job(url, status="applied", band="active"):
    return {"url": url, "status": status, "title": "T", "company": "C",
            "temperature": {"band": band, "label": band, "icon": "●", "reason": "why."}}


TEMP_ALL = ['won','warm','active','cooling','cold','new','undeliverable']

BOARD = [
    _job("j/1", "applied", "warm"),
    _job("j/2", "applied", "active"),
    _job("j/3", "applied", "active"),
    _job("j/4", "needs_human", "active"),
    _job("j/5", "applied", "cooling"),
    _job("j/6", "rejected", "cold"),
]


# ── filtering ───────────────────────────────────────────────────────────────

def test_a_band_filter_narrows_to_that_band(tmp_path):
    out = _run("F.setTempFilter('warm');"
               "console.log(JSON.stringify(JOBS.filter(j => F.jobInTemp(j, F.TEMP_FILTER))"
               ".map(j => j.url)));", tmp_path, JOBS=BOARD)
    assert out == ["j/1"], out


def test_the_band_filter_ANDs_with_the_bucket_instead_of_replacing_it(tmp_path):
    """The reason this is a second axis rather than five more entries in JOB_BUCKETS. Choosing
    a band must not silently clear "Applied" — "applied AND active" is the actual question, and
    a single radio group cannot express it."""
    out = _run("F.setJobFilter('applied'); F.setTempFilter('active');"
               "console.log(JSON.stringify({bucket: F.JOB_FILTER, band: F.TEMP_FILTER,"
               " rows: JOBS.filter(j => F.jobInBucket(j, F.JOB_FILTER) &&"
               " F.jobInTemp(j, F.TEMP_FILTER)).map(j => j.url)}));", tmp_path, JOBS=BOARD)
    assert out["bucket"] == "applied", "picking a band cleared the status bucket"
    assert out["band"] == "active"
    assert out["rows"] == ["j/2", "j/3"], out["rows"]   # j/4 is active but not applied


def test_clicking_the_selected_band_clears_it(tmp_path):
    """There is no "All" pill in this group — seven bands plus an eighth is a wall of chips — so
    the selected pill is the only way out. Without this the escape from a one-row view is
    reloading the page."""
    out = _run("F.setTempFilter('warm'); const on = F.TEMP_FILTER;"
               "F.setTempFilter('warm');"
               "console.log(JSON.stringify({on, off: F.TEMP_FILTER}));", tmp_path)
    assert out == {"on": "warm", "off": "all"}, out


# ── the pills ───────────────────────────────────────────────────────────────

def _pills(tmp_path, jobs, bucket="all", band="all"):
    return _run(f"F.setJobFilter({json.dumps(bucket)});"
                f"if ({json.dumps(band)} !== 'all') F.setTempFilter({json.dumps(band)});"
                "F.renderJobFilters(JOBS);"
                "console.log(JSON.stringify(__nodes.jobFilters.innerHTML));",
                tmp_path, JOBS=jobs)


def test_the_active_BAND_pill_is_not_rendered_as_SELECTED(tmp_path):
    """One of the bands is called `active`, and `active` is also the selected-state class every
    filter pill already uses. Unprefixed, that pill carries `class="... active"` at all times
    and renders as permanently applied — a filter that looks ON when it is OFF, which is worse
    than one that looks off when it is on. Hence the `tb-` prefix on band tints."""
    html = _pills(tmp_path, BOARD, bucket="all", band="all")
    assert "tb-active" in html, "the band tint is not prefixed"
    marker = 'class="filter-pill temp-pill tb-active'
    assert marker in html, html
    tail = html.split(marker, 1)[1].split('"', 1)[0]
    assert "active" not in tail, (
        f"the active-band pill renders as selected when nothing is selected: {marker + tail!r}")


def test_selecting_a_band_marks_exactly_that_pill(tmp_path):
    html = _pills(tmp_path, BOARD, bucket="all", band="cooling")
    assert 'tb-cooling active"' in html, html
    assert 'tb-warm active"' not in html, "two bands render as selected at once"


def test_an_empty_band_gets_no_pill_but_the_selected_one_always_does(tmp_path):
    """Pills for bands with nothing behind them are noise. The SELECTED one is the exception:
    drop it when its last job changes band and the active filter vanishes, leaving an empty
    table and no visible cause."""
    quiet = _pills(tmp_path, BOARD, bucket="all", band="all")
    assert "tb-undeliverable" not in quiet, "a band with no jobs still got a pill"

    stuck = _pills(tmp_path, BOARD, bucket="rejected", band="warm")   # no rejected job is warm
    assert "tb-warm" in stuck, "the selected band lost its pill and became unclearable"


def test_band_counts_respect_the_bucket_you_are_in(tmp_path):
    """A pill's number has to be what clicking it gives you. A board-wide count under an active
    bucket promises rows the click cannot produce."""
    html = _pills(tmp_path, BOARD, bucket="needs_you", band="all")
    seg = html.split("tb-active", 1)[1]
    assert ">1<" in seg.split("</button>", 1)[0], (
        f"active counts the whole board while the bucket is needs_you: {seg[:160]!r}")


# ── the tooltips (what the legend used to be) ───────────────────────────────

def test_every_band_pill_carries_its_own_explanation(tmp_path):
    """The legend is gone, so the pill is the only place the meaning survives. A band that
    renders with no tip is the original complaint — a word with nothing behind it."""
    board = [_job(f"j/{i}", "applied", b) for i, b in enumerate(TEMP_ALL)]
    html = _pills(tmp_path, board)
    for band in TEMP_ALL:
        seg = html.split(f"tb-{band}", 1)
        assert len(seg) == 2, f"{band} has no pill"
        tip = seg[1].split('data-tip="', 1)[1].split('"', 1)[0]
        assert len(tip.split()) >= 4, f"{band} has no usable tooltip: {tip!r}"


def test_a_band_tip_states_WHOSE_action_it_describes(tmp_path):
    """The legend grouped the bands under "Did they respond?" and "How far through your
    outreach", and that grouping was the explanation — four bands count our own sending, two
    are about what they did. A per-pill tooltip has no grouping to carry it, so each tip has to
    say its axis outright. Without this, removing the legend keeps the vocabulary and throws
    away the point of it."""
    out = _run("console.log(JSON.stringify({"
               " tips: Object.fromEntries(F.TEMP_ORDER.map(k => [k, F.tempTip(k)])),"
               " axes: F.TEMP_AXIS_TIP, bands: F.TEMP_BANDS}));", tmp_path)
    for band, tip in out["tips"].items():
        axis = out["bands"][band]["axis"]
        assert tip.startswith(out["axes"][axis]), f"{band} does not say whose action it is: {tip!r}"
    # ...and the two axes must not describe themselves the same way, or stating it is a no-op.
    assert out["axes"]["them"] != out["axes"]["us"], out["axes"]


def test_the_selected_band_tip_says_how_to_clear_it(tmp_path):
    """There is no "All" pill in this group, so clicking the selected band is the only way out
    — and an affordance you have to guess is one nobody finds (§Lessons 43)."""
    html = _pills(tmp_path, BOARD, bucket="all", band="cooling")
    tip = html.split("tb-cooling active", 1)[1].split('data-tip="', 1)[1].split('"', 1)[0]
    assert "clear" in tip.lower(), f"the selected pill does not say how to clear it: {tip!r}"


def test_every_status_bucket_pill_explains_itself_too(tmp_path):
    """"Needs you" and "In progress" each stand for a specific set of statuses that the label
    does not name."""
    html = _pills(tmp_path, BOARD)
    out = _run("console.log(JSON.stringify(F.JOB_BUCKETS));", tmp_path)
    for key, meta in out.items():
        assert len(str(meta.get("tip") or "").split()) >= 4, f"{key} has no tooltip: {meta}"
        assert meta["tip"] in html, f"{key}'s tooltip never reaches the markup"


def test_the_tooltip_is_not_a_native_title(tmp_path):
    """`title` waits about a second, cannot be styled, and would stack a SECOND box under the
    CSS one. The accessible name carries the same words instead, because a ::after is invisible
    to a screen reader — so dropping aria-label makes these pills unreadable to one."""
    html = _pills(tmp_path, BOARD)
    assert "title=" not in html, f"a native title tooltip is back and will double up: {html[:200]!r}"
    assert html.count("aria-label=") >= 5, "the tips are invisible to a screen reader"


def test_the_legend_is_really_gone(tmp_path):
    """Deleting the renderer while leaving the markup gives an empty panel that opens onto
    nothing; deleting the markup while leaving the call gives a dead function on the 2.5s path."""
    html = (web_dashboard._STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (web_dashboard._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert "tempLegend" not in html, "the legend markup is still there"
    assert "renderTempLegend" not in js, "the legend renderer is still called"
