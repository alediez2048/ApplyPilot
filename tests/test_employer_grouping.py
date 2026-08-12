"""CO-1 — two roles at one employer render under one band.

The split this rests on: two roles are two **applications** and one **relationship**. The row
stays the unit for the application, because every control on it is written against one job and
§Lessons 43/89 is what moving them costs. The band carries what belongs to the employer — and
carries it DEDUPLICATED, which is the only part that is not cosmetic.

`store.contact_id()` hashes `job_url`, so the same recruiter found for a second role is a second
contact row with its own follow-up ladder. Each job's own panel shows its own contacts and both
look complete; nothing on the page can currently say "these eight people are the same eight
people". `shared` is that number.

Executed under the shared browser stub, not grepped. Four assertions in `test_cancelled_job.py`
survived a real mutation to `isClosed` because they only proved a string was present
(§Lessons 48), and the grouping has the same shape: a version returning one group per job
satisfies every assertion about markup.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from browser_stubs import BROWSER_GLOBALS

from applypilot import web_dashboard as wd


def _job(url, company, contacts=(), due=(), **kw):
    j = {"url": url, "company": company, "title": kw.get("title", "Role"), "description": "",
         "status": kw.get("status", "applied"), "contacts": list(contacts),
         "followups": {"due": list(due)}, "applied_at": "", "rejected_at": "",
         "interview_at": "", "temperature": {}, "checklist": {}, "activity": []}
    j.update(kw)
    return j


def _c(cid, email=None, **kw):
    c = {"id": cid, "email": email, "full_name": cid, "emailed": False,
         "submitted_at": None, "replied_at": None, "last_reply": None}
    c.update(kw)
    return c


def _run(payload_js, tmp_path):
    """Evaluate the real dashboard.js and call the grouping functions from it."""
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "grouping.mjs"
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
const F = (new Function(SRC + `; return { groupByEmployer, employerStats, coHeadRow,
                                          toggleCoGroup, CO_COLLAPSED };`))();
""" + payload_js, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── grouping ────────────────────────────────────────────────────────────────

def test_two_roles_at_one_employer_form_one_group(tmp_path):
    jobs = [_job("u1", "Google"), _job("u2", "Google")]
    out = _run(f"""
const g = F.groupByEmployer({json.dumps(jobs)});
console.log(JSON.stringify({{ groups: g.length, sizes: g.map(x => x.jobs.length),
                              name: g[0].name }}));
""", tmp_path)
    assert out["groups"] == 1
    assert out["sizes"] == [2]
    assert out["name"] == "Google"


def test_a_single_role_employer_is_its_own_group_of_one(tmp_path):
    """Non-vacuity for everything below. A version that returns one group per job satisfies every
    markup assertion in this file, so the grouping has to be shown to actually collapse."""
    jobs = [_job("u1", "Google"), _job("u2", "Google"), _job("u3", "Stripe")]
    out = _run(f"""
const g = F.groupByEmployer({json.dumps(jobs)});
console.log(JSON.stringify({{ sizes: g.map(x => x.jobs.length) }}));
""", tmp_path)
    assert out["sizes"] == [2, 1], "the grouping did not collapse anything"


def test_the_employer_is_matched_case_and_space_insensitively(tmp_path):
    jobs = [_job("u1", "Google"), _job("u2", " google ")]
    out = _run(f"""
console.log(JSON.stringify({{ n: F.groupByEmployer({json.dumps(jobs)}).length }}));
""", tmp_path)
    assert out["n"] == 1


def test_rows_with_no_employer_are_never_bundled(tmp_path):
    """§Lessons 85 left rows whose company resolved to nothing. Keying on "" would collect every
    unresolved row into one band labelled with an empty string — a group that asserts a
    relationship between companies that have nothing to do with each other."""
    jobs = [_job("u1", ""), _job("u2", ""), _job("u3", None)]
    out = _run(f"""
const g = F.groupByEmployer({json.dumps(jobs)});
console.log(JSON.stringify({{ sizes: g.map(x => x.jobs.length) }}));
""", tmp_path)
    assert out["sizes"] == [1, 1, 1]


def test_a_group_sits_at_its_best_members_position(tmp_path):
    """The server's ORDER BY sinks closed rows. A company holding one live and one cancelled role
    has no single position, and taking the worst buries live work under a dead requisition.
    Walking the already-sorted list gives the group its FIRST member's slot for free — no second
    ranking that could disagree with the sort beside it."""
    jobs = [_job("live", "Acme"), _job("other", "Zendesk"), _job("dead", "Acme")]
    out = _run(f"""
const g = F.groupByEmployer({json.dumps(jobs)});
console.log(JSON.stringify({{ order: g.map(x => x.name),
                              acme: g[0].jobs.map(j => j.url) }}));
""", tmp_path)
    assert out["order"] == ["Acme", "Zendesk"], "the group did not take its best member's slot"
    assert out["acme"] == ["live", "dead"], "members lost their relative order"


# ── the numbers on the band ─────────────────────────────────────────────────

def test_people_are_counted_once_across_roles(tmp_path):
    """The reason the band exists. Two rows each showing "3 contacts" for the same three humans
    reads as six, and six is the number that would justify sending six emails."""
    same = [_c("a", "a@x.com"), _c("b", "b@x.com"), _c("c", "c@x.com")]
    jobs = [_job("u1", "Google", same), _job("u2", "Google", same)]
    out = _run(f"""
console.log(JSON.stringify(F.employerStats({json.dumps(jobs)})));
""", tmp_path)
    assert out["people"] == 3, "the same people were counted once per role"
    assert out["roles"] == 2


def test_a_person_on_two_roles_is_flagged(tmp_path):
    """`contact_id` hashes job_url, so this person is two rows with two independent ladders and
    can receive the same pitch twice. Neither job's own panel can show it."""
    jobs = [_job("u1", "Google", [_c("a", "a@x.com"), _c("b", "b@x.com")]),
            _job("u2", "Google", [_c("a2", "a@x.com")])]
    out = _run(f"""
console.log(JSON.stringify(F.employerStats({json.dumps(jobs)})));
""", tmp_path)
    assert out["people"] == 2
    assert out["shared"] == 1


def test_nothing_is_flagged_when_the_rosters_are_distinct(tmp_path):
    """Guard the guard: `shared = people` would satisfy the test above and light the warning on
    every employer that has any contacts at all."""
    jobs = [_job("u1", "Google", [_c("a", "a@x.com")]),
            _job("u2", "Google", [_c("b", "b@x.com")])]
    out = _run(f"""
console.log(JSON.stringify(F.employerStats({json.dumps(jobs)})));
""", tmp_path)
    assert out["shared"] == 0
    assert out["people"] == 2


def test_a_contact_with_no_address_is_still_one_person(tmp_path):
    """Falling back to the id matters: keying on email alone makes every address-less contact the
    same person, and the band would report one human for eight."""
    jobs = [_job("u1", "Google", [_c("a"), _c("b"), _c("c")])]
    out = _run(f"""
console.log(JSON.stringify(F.employerStats({json.dumps(jobs)})));
""", tmp_path)
    assert out["people"] == 3
    assert out["shared"] == 0


def test_emailed_and_replied_are_deduplicated_too(tmp_path):
    jobs = [_job("u1", "Google", [_c("a", "a@x.com", emailed=True, replied_at="2026-08-01")]),
            _job("u2", "Google", [_c("a2", "a@x.com", emailed=True, replied_at="2026-08-01")])]
    out = _run(f"""
console.log(JSON.stringify(F.employerStats({json.dumps(jobs)})));
""", tmp_path)
    assert out["emailed"] == 1
    assert out["replied"] == 1


def test_due_follow_ups_are_summed_by_contact(tmp_path):
    jobs = [_job("u1", "Google", due=[{"id": "x"}, {"id": "y"}]),
            _job("u2", "Google", due=[{"id": "y"}, {"id": "z"}])]
    out = _run(f"""
console.log(JSON.stringify(F.employerStats({json.dumps(jobs)})));
""", tmp_path)
    assert out["due"] == 3


# ── the band itself ─────────────────────────────────────────────────────────

def test_the_band_names_the_employer_and_the_role_count(tmp_path):
    jobs = [_job("u1", "Google", [_c("a", "a@x.com", emailed=True)]), _job("u2", "Google")]
    out = _run(f"""
const g = F.groupByEmployer({json.dumps(jobs)});
console.log(JSON.stringify({{ html: F.coHeadRow(g[0]) }}));
""", tmp_path)
    assert "Google" in out["html"]
    assert "2 roles" in out["html"]
    assert "1 emailed" in out["html"]


def test_the_duplicate_warning_only_renders_when_there_is_one(tmp_path):
    shared = [_job("u1", "Google", [_c("a", "a@x.com")]), _job("u2", "Google", [_c("a2", "a@x.com")])]
    apart = [_job("u1", "Google", [_c("a", "a@x.com")]), _job("u2", "Google", [_c("b", "b@x.com")])]
    out = _run(f"""
const g1 = F.groupByEmployer({json.dumps(shared)});
const g2 = F.groupByEmployer({json.dumps(apart)});
console.log(JSON.stringify({{ shared: F.coHeadRow(g1[0]).includes('co-dupe'),
                              apart:  F.coHeadRow(g2[0]).includes('co-dupe') }}));
""", tmp_path)
    assert out["shared"] is True
    assert out["apart"] is False, "the warning is permanently lit"


def test_the_employer_name_is_escaped(tmp_path):
    """The band is the one place a company name is written into markup as a heading rather than
    a cell, and `company` is operator-typed on every hand-pasted row."""
    jobs = [_job("u1", "<img src=x onerror=1>"), _job("u2", "<img src=x onerror=1>")]
    out = _run(f"""
const g = F.groupByEmployer({json.dumps(jobs)});
console.log(JSON.stringify({{ html: F.coHeadRow(g[0]) }}));
""", tmp_path)
    assert "<img" not in out["html"]
    assert "&lt;img" in out["html"]


def test_collapsing_survives_and_is_reversible(tmp_path):
    """The 2.5s refresh replaces #jobs wholesale, so the state lives outside the DOM — same
    reason PANEL_OPEN and TAB_OPEN do. A group that could not be re-opened would be a delete."""
    out = _run("""
F.toggleCoGroup('google');
const shut = F.CO_COLLAPSED.has('google');
F.toggleCoGroup('google');
console.log(JSON.stringify({ shut, open: !F.CO_COLLAPSED.has('google') }));
""", tmp_path)
    assert out["shut"] is True
    assert out["open"] is True


# ── the row keeps its own controls ──────────────────────────────────────────

def test_the_member_row_is_unchanged_apart_from_the_rail(tmp_path):
    """The whole design. Merging the roles into one card would take the ⋯ menu, the four tabs,
    the status strip and the two documents away from the thing they act on."""
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    body = js[js.index("function jobRows("):js.index("\n// Re-filter without hitting")]
    for control in ("stepStrip(j)", "jobTabs(j)", "jobPane(j)", "jobTags(j)"):
        assert control in body, f"{control} left the row"


@pytest.mark.parametrize("needle", ["groupByEmployer(shown)", "coHeadRow(g)", "jobRows(j, banded)"])
def test_the_render_path_uses_the_grouping(needle):
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert needle in js


# ── the whole render path, end to end ───────────────────────────────────────
#
# Everything above tests the pieces. This drives `renderJobsTable` itself and reads the markup it
# writes into #jobs, which is the only thing that proves the band reaches the page — and it is
# what caught the one mutation the rest of this file missed (`length > 1` weakened to `> 0`,
# which puts a header over every single-role employer on the board).

def _render(jobs, tmp_path):
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "render.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = (id) => ({ id, innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
// #jobs must be the SAME object every lookup, or the render writes into a throwaway and the
// assertion reads an empty string that looks like "nothing rendered".
const NODES = {};
const node = (id) => (NODES[id] = NODES[id] || el(id));
globalThis.document = { getElementById: node, querySelector:()=>el(),
  querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,
  activeElement:null };
const SRC = """ + json.dumps(src) + """;
const F = (new Function(SRC + '; return { renderJobsTable };'))();
F.renderJobsTable(""" + json.dumps(jobs) + """, false);
const html = node('jobs').innerHTML;
console.log(JSON.stringify({
  bands: (html.match(/class="co-head/g) || []).length,
  solo: (html.match(/class="co-head co-solo"/g) || []).length,
  carets: (html.match(/co-caret/g) || []).length,
  members: (html.match(/co-member/g) || []).length,
  names: (html.match(/class="co-name[^"]*"[^>]*>([^<]*)</g) || []),
  hasStrip: html.includes('step-strip'),
  html,
}));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_every_employer_gets_a_band(tmp_path):
    """This asserted the OPPOSITE — that a band needs 2+ roles — and the threshold was the bug.

    With one role the company appeared only as a small grey subtitle under the job title, so
    scanning the board you read the employer in the top-left of a grouped block and hunted for it
    everywhere else. Reported as "when jobs are individual the name of the company is nowhere to
    be seen". The STATS over one row really were furniture, which is what the old comment was
    right about; the NAME is the thing that has to be in the same place on every row.
    """
    out = _render([_job("u1", "Google"), _job("u2", "Google"), _job("u3", "Stripe")], tmp_path)
    assert out["bands"] == 2, "expected a band for Google AND one for Stripe"
    assert out["solo"] == 1, "Stripe's single role did not get a band"
    joined = " ".join(out["names"])
    assert "Google" in joined and "Stripe" in joined


def test_a_solo_band_is_not_a_collapsible_group(tmp_path):
    """No caret over one row: collapsing a single row IS the furniture the old threshold avoided,
    and a control that hides the only thing under it is worse than none."""
    out = _render([_job("u1", "Google"), _job("u2", "Stripe"), _job("u3", "Okta")], tmp_path)
    assert out["bands"] == 3 and out["solo"] == 3
    assert out["carets"] == 0, "a lone row was given a collapse toggle"


def test_a_multi_role_band_keeps_its_caret(tmp_path):
    """Both directions, or "no caret" is satisfied by removing the toggle everywhere."""
    out = _render([_job("u1", "Google"), _job("u2", "Google")], tmp_path)
    assert out["solo"] == 0
    assert out["carets"] == 1, "the collapsible group lost its toggle"


def test_no_rule_turns_a_table_cell_into_a_flex_container():
    """A `td` with `display:flex` leaves the table formatting context, so `colspan` stops
    applying and the cell collapses to its first column's width.

    That is exactly what shipped: `tr.co-solo > td { display:flex }` made the solo band render
    "Ey" then "6 / people / · 5 / emailed" stacked one word per line, in a cell about 150px wide.
    Reported as "it is breaking the cards", and no test could see it — every test here reads
    MARKUP, and this is a layout consequence of a rule that is correct-looking in isolation.
    §Lessons 62's family: `hidden` lost to an author `display` for the same reason, and the Node
    test that asserted the property was true and useless.

    The multi-role band had it right all along — its flex lives on `.co-toggle` INSIDE the td.
    """
    css = (wd._STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    # Selectors whose LAST element is a td (`… td {`, `… > td {`, `td.foo {`).
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector, body = block.group(1).strip(), block.group(2)
        if not re.search(r"(^|[\s>+~])td[.\w-]*\s*$", selector):
            continue
        display = re.search(r"(?<![-\w])display\s*:\s*([\w-]+)", body)
        if display and display.group(1) in {"flex", "grid", "block", "inline-flex", "inline-grid"}:
            raise AssertionError(
                f"`{selector}` sets display:{display.group(1)} on a table CELL. That drops it out "
                "of table layout, so colspan stops applying and the cell collapses. Put the "
                "layout on a wrapper inside the td, as `.co-toggle` does.")


def test_the_solo_band_puts_its_layout_on_a_wrapper_not_the_cell(tmp_path):
    """The structural half of the rule above, asserted on what actually renders."""
    html = _render([_job("u1", "Stripe")], tmp_path)["html"]
    band = html[html.index("co-solo"):]
    band = band[:band.index("</tr>")]
    assert "co-solo-inner" in band, "the solo band lost its layout wrapper"
    assert band.index("<td") < band.index("co-solo-inner"), "the wrapper is outside the cell"


def test_the_solo_bands_name_is_still_editable(tmp_path):
    """Banding a row HIDES the company subtitle under its title, which was that row's editor.

    Moving the name without moving its editor leaves the only way to fix a wrong employer three
    clicks away in the Job tab — §Lessons 97, reported as "I still can't edit descriptions" when
    exactly that happened to the description. A multi-role band is deliberately NOT editable:
    which of its rows an edit would write to has no answer.
    """
    out = _render([_job("u1", "Stripe")], tmp_path)
    band = out["html"][out["html"].index("co-solo"):]
    band = band[:band.index("</tr>")]
    assert "startEdit" in band, "the solo band's company name cannot be edited"
    assert "'company'" in band, "the band's editor is wired to the wrong field"

    grouped = _render([_job("u1", "Google"), _job("u2", "Google")], tmp_path)["html"]
    head = grouped[grouped.index("co-head"):]
    head = head[:head.index("</tr>")]
    assert "startEdit" not in head, "a multi-role band offered an edit with no unambiguous target"


def test_a_row_with_no_employer_gets_no_band(tmp_path):
    """A band needs a NAME, not a count. Rows whose employer never resolved (§Lessons 85's
    "Uploaded", now "") would otherwise each print an empty header, and the subtitle they keep
    instead is the only place a name can be typed in."""
    out = _render([_job("u1", ""), _job("u2", "")], tmp_path)
    assert out["bands"] == 0, "an empty employer name was banded"


def test_every_banded_row_gets_the_rail(tmp_path):
    """The rail marks a row that belongs to the band above it, so it follows banding rather than
    group size — otherwise a solo band floats over a row that is not visibly under it."""
    out = _render([_job("u1", "Google"), _job("u2", "Google"), _job("u3", "Stripe")], tmp_path)
    assert out["members"] == 6, "expected the rail on all three rows and their three feet"
    assert _render([_job("u1", "")], tmp_path)["members"] == 0, \
        "an unbanded row was given the rail"


def test_a_collapsed_band_keeps_its_header(tmp_path):
    """Collapsing must not remove the only control that can un-collapse it."""
    jobs = [_job("u1", "Google"), _job("u2", "Google")]
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "collapsed.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = (id) => ({ id, innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
const NODES = {}; const node = (id) => (NODES[id] = NODES[id] || el(id));
globalThis.document = { getElementById: node, querySelector:()=>el(),
  querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,
  activeElement:null };
const SRC = """ + json.dumps(src) + """;
const F = (new Function(SRC + '; return { renderJobsTable, CO_COLLAPSED };'))();
F.CO_COLLAPSED.add('google');
F.renderJobsTable(""" + json.dumps(jobs) + """, false);
const html = node('jobs').innerHTML;
console.log(JSON.stringify({ bands: (html.match(/class="co-head/g) || []).length,
                             rows: (html.match(/step-strip/g) || []).length }));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["bands"] == 1, "the collapsed group lost the button that reopens it"
    assert out["rows"] == 0, "collapsing hid nothing"
