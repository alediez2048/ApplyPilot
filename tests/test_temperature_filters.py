"""The band legend, and filtering the table by band.

Reported as "not sure this is the best way to know what's up with an application" — and the
confusion was the finding rather than a misreading. Four of the seven bands (new → active →
cooling → cold) are a countdown of OUR OWN sending; `warm` and `won` are about what THEY did.
One chip presenting both as a single scale reads as if warm were simply more than active, which
it is not: a cooling job can get a reply tomorrow and an active one can stay silent forever.

So the legend groups the bands by which QUESTION each answers, and the filter is a second axis
that ANDs with the status bucket rather than replacing it.

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

# `getElementById` has to hand back a DISTINCT node per id and remember what was written to it:
# the legend and the filter bar are rendered by different functions into different nodes, and a
# shared stub would let a test pass while both wrote to the same place.
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

_EXPORTS = """; return { TEMP_BANDS, TEMP_ORDER, TEMP_AXES, jobInTemp, jobInBucket,
  setTempFilter, setJobFilter, renderJobFilters, renderTempLegend,
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


BOARD = [
    _job("j/1", "applied", "warm"),
    _job("j/2", "applied", "active"),
    _job("j/3", "applied", "active"),
    _job("j/4", "needs_human", "active"),
    _job("j/5", "applied", "cooling"),
    _job("j/6", "rejected", "cold"),
]


# ── the legend ──────────────────────────────────────────────────────────────

def test_the_legend_explains_every_band_it_can_show(tmp_path):
    """A band that appears on a row and not in the legend is the vocabulary problem this was
    built to fix, one entry short."""
    out = _run("console.log(JSON.stringify((F.renderTempLegend(), "
               "{html: __nodes.tempLegend.innerHTML, order: F.TEMP_ORDER})));", tmp_path)
    html = out["html"]
    assert html.strip(), "the legend rendered nothing"
    for band in out["order"]:
        assert f">{band}<" in html or f" {band}<" in html, f"{band} is missing from the legend"


def test_the_legend_says_what_each_band_MEANS(tmp_path):
    """Listing the words without their meanings reproduces the original complaint in a new
    place: the reader still cannot tell active from warm.

    The emptiness check is not padding — it is the whole test. The first version asserted only
    `meaning in html`, and `"" in html` is True for every string, so blanking a band's
    explanation passed cleanly. §Lessons 13: a mutation that empties the thing under test must
    fail, or the assertion is measuring nothing.
    """
    out = _run("console.log(JSON.stringify((F.renderTempLegend(), "
               "{html: __nodes.tempLegend.innerHTML, bands: F.TEMP_BANDS})));", tmp_path)
    for band, meta in out["bands"].items():
        # Two, not three: "interview booked" is a complete explanation and the shortest honest
        # one on the list. The bar only has to be high enough to kill "" and a bare "yes".
        words = (meta["meaning"] or "").split()
        assert len(words) >= 2, f"{band} has no usable explanation: {meta['meaning']!r}"
        assert meta["meaning"] in out["html"], f"{band} has no explanation in the legend"


def test_the_legend_separates_what_THEY_did_from_what_WE_did(tmp_path):
    """The whole point. `warm`/`won` answer "did they respond"; new/active/cooling/cold count
    our own sending. Presented as one scale they read as a single ladder, which is what made
    the chip unreadable — so the grouping is the explanation, not decoration."""
    out = _run("console.log(JSON.stringify(F.TEMP_BANDS));", tmp_path)
    them = {k for k, v in out.items() if v["axis"] == "them"}
    us = {k for k, v in out.items() if v["axis"] == "us"}
    assert them == {"warm", "won"}, them
    assert us == {"new", "active", "cooling", "cold"}, us
    assert not (them & us), "a band cannot be on both axes"


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
