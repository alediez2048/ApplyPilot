"""What a pasted sheet SUPPLIES, as against how many rows it imported.

SHEET-3. The two numbers came apart on the first real sheet and only one of them was ever shown:
45 companies and 105 people imported with zero rejected rows, of whom **85 had no email address,
none had a LinkedIn URL, 55 were a first name alone, and 30 of the 45 companies had nobody
reachable at all**. The message read "Imported 45 companies, 105 people." — §Lessons 15, where a
run that kept nobody was byte-identical to a button that never fired.

The distinction this file exists to protect is between a MISSING COLUMN and an EMPTY ONE. They
have different fixes — add a heading, or fill cells in — and a single percentage cannot say
which one you are looking at.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from applypilot import web_dashboard as wd
from applypilot.domain import sheet

sys.path.insert(0, str(Path(__file__).parent))
from browser_stubs import BROWSER_GLOBALS  # noqa: E402

TSV = "\t".join


def cov(text: str) -> dict[str, dict]:
    return {c["field"]: c for c in sheet.coverage(sheet.parse(text))}


FULL = "\n".join([
    TSV(["Company", "Name", "Position", "Email", "LinkedIn", "Website", "About"]),
    TSV(["Ridgeline", "Dana Okafor", "VP Eng", "d@ridge.test", "/in/dana", "ridge.test", "Freight"]),
    TSV(["Northwind", "Alex Roy", "Head of Talent", "a@nw.test", "/in/alex", "nw.test", "Data"]),
])

#: The operator's real sheet, reduced: one person per company filled in, the rest bare first
#: names. It imports perfectly and can reach almost nobody.
SPARSE = "\n".join([
    TSV(["Company", "First Name", "Position", "Email"]),
    TSV(["Apex", "Frank Tiemann", "VP Engineering", "frank@apex.test"]),
    TSV(["Apex", "Lanie", "", ""]),
    TSV(["Apex", "Sean", "", ""]),
    TSV(["Bissell", "Brandon", "", ""]),
])


# ── the finding ─────────────────────────────────────────────────────────────

def test_a_sheet_that_imports_cleanly_can_still_report_gaps():
    """The whole point: zero rejected rows and four separate things missing."""
    assert sheet.parse(SPARSE)["rejected"] == []
    c = cov(SPARSE)
    assert c["email"]["have"] == 1 and c["email"]["total"] == 4
    assert c["linkedin_url"]["have"] == 0
    assert c["title"]["have"] == 1
    assert not any(x["ok"] for x in cov(SPARSE).values())


def test_a_complete_sheet_reports_no_gap_at_all():
    """The other direction, or the check is one that can never pass."""
    assert all(c["ok"] for c in cov(FULL).values())
    assert all(c["have"] == c["total"] for c in cov(FULL).values())


# ── missing column vs empty column ──────────────────────────────────────────

def test_a_column_nobody_added_is_reported_as_absent():
    """`column: False` is "change the shape of your sheet", and it is a different instruction."""
    assert cov(SPARSE)["linkedin_url"]["column"] is False


def test_a_column_that_is_present_and_empty_is_not_reported_as_absent():
    """Same zero count, opposite fix. Merged, the operator adds a column they already have."""
    text = "\n".join([
        TSV(["Company", "Name", "LinkedIn"]),
        TSV(["Apex", "Frank Tiemann", ""]),
    ])
    c = cov(text)["linkedin_url"]
    assert c["have"] == 0
    assert c["column"] is True


# ── a name is not a name ────────────────────────────────────────────────────

def test_a_bare_first_name_does_not_count_as_a_full_name():
    """The vacuous version of this check reports 100%.

    Every person that survives `parse` HAS a name — a nameless row is a rejection — so counting
    non-empty names passes on exactly the sheet this exists to catch. 55 of the operator's 105
    people were a first name alone and the import called all 105 complete.
    """
    c = cov(SPARSE)["full_name"]
    assert c["have"] == 1                     # Frank Tiemann, and nobody else
    assert c["total"] == 4


def test_a_first_name_column_with_no_last_name_column_is_a_header_problem():
    """`First Name` alone is a fix to the sheet's shape; the operator's sheet had exactly this."""
    assert cov(SPARSE)["full_name"]["column"] is False


def test_a_first_and_last_pair_counts_as_having_the_column():
    """Every CRM export splits them, so the split IS the supported shape, not a defect."""
    text = "\n".join([
        TSV(["Company", "First Name", "Last Name"]),
        TSV(["Apex", "Frank", "Tiemann"]),
    ])
    c = cov(text)["full_name"]
    assert c["column"] is True
    assert c["have"] == 1


def test_a_single_name_column_counts_too():
    assert cov(FULL)["full_name"]["column"] is True


# ── the unit is the thing the cell describes ────────────────────────────────

def test_company_fields_are_counted_over_companies_not_over_rows():
    """One blurb per COMPANY, filled on one of its rows, is a sheet that is filled in correctly.

    Counted over rows it reads 1 of 4 and sends the operator to repeat a paragraph down a
    column — which is precisely the thing first-non-empty-wins was built to make unnecessary.
    """
    text = "\n".join([
        TSV(["Company", "Name", "About"]),
        TSV(["Apex", "Frank Tiemann", "Clearing and custody"]),
        TSV(["Apex", "Lanie Doe", ""]),
        TSV(["Apex", "Sean Roe", ""]),
        TSV(["Bissell", "Brandon Walsh", "Floor care"]),
    ])
    c = cov(text)["about"]
    assert c["unit"] == "companies"
    assert (c["have"], c["total"]) == (2, 2)
    assert c["ok"] is True


def test_every_field_says_what_the_gap_costs():
    """A count is a statistic; the consequence is what decides whether to go back to the sheet."""
    for c in cov(SPARSE).values():
        assert len(c["cost"]) > 15
        assert c["label"]
    assert "only channel that sends" in cov(SPARSE)["email"]["cost"]


def test_an_empty_paste_reports_nothing_rather_than_dividing_by_zero():
    """`parse` raises on a truly empty paste, so the reachable case is a header with no rows."""
    text = TSV(["Company", "Name", "Email"])
    assert sheet.coverage(sheet.parse(text)) == []
    assert sheet.coverage({}) == []


# ── the column list the operator is shown ───────────────────────────────────

def test_the_column_list_is_generated_from_the_parser_itself():
    """A hand-written list of what the importer reads goes stale on the first added spelling,
    and the operator cannot tell which of the two is lying."""
    got = {c["field"]: set(c["spellings"]) for c in sheet.recognised_columns()}
    assert set(got) == set(sheet._FIELDS)
    for field, spellings in sheet._FIELDS.items():
        assert got[field] == set(spellings)


def test_the_column_list_marks_the_two_that_are_actually_required():
    """Only Company and a name are refused when absent — everything else parses without it,
    which is exactly why a sheet can import cleanly and reach nobody."""
    required = {c["field"] for c in sheet.recognised_columns() if c["required"]}
    assert required == {"company", "name", "first", "last"}


def test_the_column_list_names_the_about_spellings_nobody_could_guess():
    """`What they do` and `Summary` are read and there is nowhere else that says so."""
    about = next(c for c in sheet.recognised_columns() if c["field"] == "about")
    assert {"whattheydo", "summary", "overview"} <= set(about["spellings"])


# ── and the browser RENDERS it ──────────────────────────────────────────────
#
# Driven with what `coverage()` actually returns rather than a hand-written object. A frontend
# test that supplies its own input can only prove the frontend agrees with itself — which is how
# fifteen tests of the cancelled state all passed while the server was emitting `applied` for it
# (§Lessons 93). Here the fixture IS the server's output, so a field renamed in Python fails
# here rather than silently rendering "undefined of undefined".

def _render(rows: list[dict], tmp_path: Path) -> str:
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "cov.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const box = { innerHTML:'', hidden:true, textContent:'' };
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
globalThis.document = { getElementById:(id)=> id === 'sheetCoverage' ? box : el(),
  querySelector:()=>el(), querySelectorAll:()=>[], addEventListener(){}, body:el(),
  hasFocus:()=>false, activeElement:null };
const SRC = """ + json.dumps(src) + """;
const F = (new Function(SRC + '; return { renderSheetCoverage };'))();
F.renderSheetCoverage(""" + json.dumps(rows) + """);
console.log(JSON.stringify({ html: box.innerHTML, hidden: box.hidden }));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["hidden"] is False, "the coverage block never became visible"
    return out["html"]


def test_the_browser_renders_the_count_that_is_MISSING(tmp_path):
    """"1 of 4 have an email" and "3 of 4 have no email" are the same fact and opposite readings.

    The panel exists to say what to go and fix, so it reports the gap. Rendering `have` under a
    heading that says "does not carry" is the version of this bug that looks entirely fine.
    """
    html = _render(sheet.coverage(sheet.parse(SPARSE)), tmp_path)
    assert "3 of 4" in html, html          # emails missing, not the 1 present
    assert "4 of 4" in html                # LinkedIn: nobody at all
    assert "1 of 4" not in html


def _row(html: str, label: str) -> str:
    """The one rendered row carrying this label.

    Split on the row boundary rather than slicing a window around the label. The window version
    of this helper took `html[i - 200 : i + 60]`, and on the FIRST row `i - 200` is negative —
    Python read it as an offset from the end, the slice came back empty, and
    `"no such column" not in ""` is true whatever the renderer does. It passed under a mutation
    that tagged every row (§Lessons 71).
    """
    rows = [r for r in html.split('<div class="cov-row') if f"<b>{label}</b>" in r]
    assert len(rows) == 1, f"expected one {label!r} row, found {len(rows)}"
    return rows[0]


def test_the_browser_marks_an_absent_column_and_only_an_absent_one(tmp_path):
    """Both directions. Tagging everything satisfies the first half and tells the operator to add
    a column that is already in their sheet."""
    text = "\n".join([
        TSV(["Company", "Name", "Email"]),
        TSV(["Apex", "Frank Tiemann", ""]),
    ])
    html = _render(sheet.coverage(sheet.parse(text)), tmp_path)
    assert "no such column" in _row(html, "LinkedIn URL")
    assert "no such column" not in _row(html, "Email"), "a column that IS present was called absent"


def test_the_browser_shows_the_consequence_not_only_the_number(tmp_path):
    html = _render(sheet.coverage(sheet.parse(SPARSE)), tmp_path)
    assert "only channel that sends" in html
    assert "cannot be matched to a real person" in html


def test_the_worst_gap_is_rendered_first(tmp_path):
    """A field nobody supplied is a decision about the sheet; a few blanks is an oversight in it.

    So the list is sorted by the PROPORTION filled, not left in the parser's field order — in
    which Email comes first and the column nobody has sits below it.
    """
    rows = sheet.coverage(sheet.parse(SPARSE))
    assert [c["field"] for c in rows][0] == "email", "fixture no longer exercises the sort"
    html = _render(rows, tmp_path)
    assert html.index("LinkedIn URL") < html.index("<b>Email</b>")


def test_a_complete_sheet_renders_something_rather_than_an_empty_box(tmp_path):
    """An empty result and a clean one look identical, and `assert x in html` passes on both when
    x is '' (§Lessons 71). The clean case gets its own sentence."""
    html = _render(sheet.coverage(sheet.parse(FULL)), tmp_path)
    assert "Every column this sheet needs is filled in" in html
    assert "no such column" not in html


def test_it_says_the_sheet_can_simply_be_pasted_again(tmp_path):
    """The operator's next question is "do I have to redo this". Re-importing a grown sheet
    updates in place (SHEET-1b), and a panel listing four gaps without saying so reads as a
    demand to start over."""
    html = _render(sheet.coverage(sheet.parse(SPARSE)), tmp_path)
    assert "paste the whole thing again" in html
    assert "updated in place" in html


def test_importing_a_sheet_ACTUALLY_RENDERS_the_coverage(tmp_path):
    """Nothing above proves `importSheet` calls it.

    Deleting that one line left every test in this file green while the panel was unreachable —
    §Lessons 94, where thirty-six passing tests called the editor's functions directly and none
    of them drove the render, so the feature was completely broken and fully covered.

    So this drives `importSheet` itself, with `post` stubbed to return what the endpoint returns.
    """
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    payload = {"ok": True, "message": "Imported 2 companies, 4 people.", "rejected": [],
               "coverage": sheet.coverage(sheet.parse(SPARSE))}
    script = tmp_path / "imp.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const box = { innerHTML:'', hidden:true, textContent:'' };
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
const input = { ...el(), value:'Company\\tName\\nApex\\tFrank Tiemann' };
globalThis.document = { getElementById:(id)=> id === 'sheetCoverage' ? box
    : id === 'sheetInput' ? input : el(),
  querySelector:()=>el(), querySelectorAll:()=>[], addEventListener(){}, body:el(),
  hasFocus:()=>false, activeElement:null };
const SRC = """ + json.dumps(src) + """;
const PAYLOAD = """ + json.dumps(payload) + """;
// `post` and `refresh` are the two things importSheet reaches for that need a server.
const F = (new Function(SRC + `
  post = async () => (${JSON.stringify(PAYLOAD)});
  refresh = () => {};
  return { importSheet };`))();
await F.importSheet({ disabled:false, textContent:'Import' });
console.log(JSON.stringify({ html: box.innerHTML, hidden: box.hidden }));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["hidden"] is False, "importing a sheet does not show the coverage panel"
    assert "What this sheet does not carry" in out["html"]
    assert "only channel that sends" in out["html"]


def test_the_endpoint_that_feeds_the_column_list_exists(tmp_path):
    """The list is fetched, not hardcoded in the frontend — two copies of what the parser reads
    is how one of them goes stale with nothing raising."""
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert "/api/sheet-columns" in src
    assert "/api/sheet-columns" in Path(wd.__file__).read_text(encoding="utf-8")
