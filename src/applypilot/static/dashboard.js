// ---- Spaces (SPACE-2) ----
//: Which Space is on screen. Seeded from ?space= so a bookmark or a reload lands where you
//: left off, then owned here. The SERVER decides whether the id is real — an unknown one comes
//: back with `space_note` explaining the fallback rather than an empty table, which would be
//: indistinguishable from a Space with nothing in it.
let SPACE_ID = new URLSearchParams(location.search).get('space') || '';

//: Every read of /api/status goes through this. There are four call sites and a rule applied to
//: three of them is not applied (§Lessons 49) — the one that got missed would silently show
//: another Space's counts in the apply guard.
function statusUrl() {
  return SPACE_ID ? `/api/status?space=${encodeURIComponent(SPACE_ID)}` : '/api/status';
}

// Switching Space is a different working set, not a filter over the current one. So the caches
// that hold the previous Space's rows are dropped: LAST_JOBS feeds click handlers that run
// after render, and JOB_DESC is the ~130KB search corpus fetched once per session (UX-6) — one
// per SESSION was right when there was one Space, and is a cross-Space leak now.
function switchSpace(id) {
  if (!id || id === SPACE_ID) return;
  SPACE_ID = id;
  LAST_JOBS = [];
  // Or the skip-if-unchanged compare in renderJobsTable is against the PREVIOUS Space's markup.
  // Two Spaces rendering identical HTML is far-fetched; an empty one and another empty one is
  // not, and the failure mode is a tab that renders nothing at all.
  LAST_JOBS_HTML = null;
  JOB_DESC.clear();
  JOB_DESC_LOADED = false;
  const url = new URL(location.href);
  url.searchParams.set('space', id);
  history.replaceState(null, '', url);   // replace, not push: the back button should leave the
  refresh();                             // dashboard, not walk a tab history nobody asked for
}

//: The templates the + button offers, from /api/status. The server owns this list so the
//: picker cannot describe a template differently from what the manifest actually builds.
let SPACE_TEMPLATES = [];
//: See renderSpaceNav.
let SPACE_LIST = [];

function renderSpaceNav(spaces, current, note) {
  const nav = document.getElementById('spaceNav');
  const noteEl = document.getElementById('spaceNote');
  if (noteEl) {
    noteEl.textContent = note || '';
    noteEl.hidden = !note;
  }
  if (!nav) return;
  const list = spaces || [];
  //: The Space list the nav last rendered, so `renameSpace` can show the current name in its
  //: prompt without re-fetching. Outside the DOM for the same reason PANEL_OPEN is.
  SPACE_LIST = list;
  // Shown from ONE Space up, because the + lives here. The first version hid the whole strip
  // below two Spaces on the grounds that a lone tab is furniture — true of the tab, false of
  // the button beside it, and hiding both meant a fresh install had exactly one Space and
  // nowhere to make another. §Lessons 43: a control nobody can find is a broken feature, and
  // this one could not be found because it was inside something that hid itself.
  nav.hidden = list.length < 1;
  if (nav.hidden) { nav.innerHTML = ''; return; }
  // Double-click renames. `repo.rename` has existed since SPACE-2 and was reachable from
  // nothing at all — no endpoint, no button (§Lessons 31: a function nobody can invoke is not a
  // feature). The NAME changes; the id never does, because it is hashed into every targets
  // `contact_id` and `Space.with_()` refuses to change it.
  const tabs = list.length > 1 ? list.map(s => {
    const on = s.id === current;
    return `<button class="space-tab${on ? ' on' : ''}" ${on ? 'aria-current="page"' : ''}`
         + ` onclick="switchSpace('${esc(s.id)}')" ondblclick="renameSpace('${esc(s.id)}')"`
         + ` title="Double-click to rename">${esc(s.name)}</button>`;
  }).join('') : `<span class="space-tab on solo" ondblclick="renameSpace('${esc((list[0] || {}).id || '')}')"`
    + ` title="Double-click to rename">${esc((list[0] || {}).name || '')}</span>`;
  nav.innerHTML = tabs
    + `<button class="space-add" onclick="toggleNewSpace()" title="New Space">＋</button>`;
}

//: Rename the Space on screen. A prompt() rather than an inline editor: the nav is rebuilt on
//: every refresh from the server's list, and a tab is one word — the inline machinery the table
//: rows use would cost more than it saves here.
async function renameSpace(id) {
  if (!id) return;
  const list = (SPACE_LIST || []).find(s => s.id === id) || {};
  const name = prompt('Rename this Space', list.name || '');
  if (name === null) return;                       // cancelled
  const r = await post('/api/space/rename', {space: id, name: name.trim()});
  if (!r || r.ok === false) { alert((r && r.message) || 'Could not rename that.'); return; }
  refresh();
}

// ---- Creating a Space (the + button) ----

function toggleNewSpace(show) {
  const form = document.getElementById('newSpaceForm');
  if (!form) return;
  form.hidden = show === false ? true : !form.hidden;
  document.getElementById('newSpaceStatus').textContent = '';
  if (!form.hidden) {
    renderTemplatePicker();
    const box = document.getElementById('newSpaceName');
    box.value = '';
    previewSpaceId();
    box.focus();
  }
}

// Enter picks the FIRST template. Named rather than an inline `if` in the attribute: every
// other handler here is a single named call, and the scope probe parses those attributes with
// a regex that read `if` as the handler's name and tried to resolve it.
function onNewSpaceKey(event) {
  if (event && event.key === 'Enter') {
    const first = document.querySelector('#newSpaceTemplates .ns-template');
    if (first) first.click();
  }
}

function renderTemplatePicker() {
  const el = document.getElementById('newSpaceTemplates');
  if (!el) return;
  el.innerHTML = (SPACE_TEMPLATES || []).map(t =>
    `<button class="ns-template" onclick="createSpace(this, '${esc(t.id)}')">`
    + `<span class="ns-t-name">${esc(t.name)}</span>`
    + `<span class="ns-t-blurb">${esc(t.blurb)}</span></button>`).join('');
}

// The id is derived from the name and SHOWN, because it is permanent: for a targets Space it
// is hashed into every contact key (§13.2). A value the operator can never change should not
// be invisible at the moment it is chosen.
function previewSpaceId() {
  const name = (document.getElementById('newSpaceName').value || '').trim();
  const id = name.normalize('NFKD').replace(/[^ -~]/g, '')
    .toLowerCase().split(/[^a-z0-9]+/).filter(Boolean).join('-').slice(0, 48);
  const el = document.getElementById('newSpaceId');
  el.textContent = id ? `id: ${id} — permanent, the name stays editable` : '';
  return id;
}

async function createSpace(btn, template) {
  const name = (document.getElementById('newSpaceName').value || '').trim();
  const out = document.getElementById('newSpaceStatus');
  if (!name) { out.textContent = 'Give it a name first.'; return; }
  btn.disabled = true;
  const r = await post('/api/space/create', {name, template});
  btn.disabled = false;
  out.textContent = r.message || '';
  if (!r.ok) return;
  toggleNewSpace(false);
  // Land IN the new Space. Creating one and staying where you were means the only feedback is
  // a tab appearing somewhere above, which reads as nothing having happened.
  switchSpace(r.id);
}

//: The shape of the Space on screen. Rows carry it too (`j.shape`), because a renderer that
//: reads a global would render correctly and then be impossible to test one row at a time.
let SPACE_SHAPE = 'pipeline/jobs';

// Swap the console for the Space's shape. Wholesale, not by disabling buttons: "Prepare
// Materials" and "Fill application" are not unavailable in a targets Space, they are
// meaningless there, and a disabled control asserts an action exists (§Lessons 43).
// Whether the premise box has already been auto-collapsed this session. Without it every
// 2.5s tick would slam it shut again while the operator is editing.
let PREMISE_SETTLED = false;

function renderSpaceShape(shape, offer, copy, voice) {
  SPACE_SHAPE = shape || 'pipeline/jobs';
  const targets = SPACE_SHAPE === 'pipeline/targets';
  const jobs = document.getElementById('jobControls');
  const tgt = document.getElementById('targetControls');
  if (jobs) jobs.hidden = targets;
  if (tgt) tgt.hidden = !targets;
  // SHEET-1b. In a sheet Space the IMPORTER goes first. Both boxes accept a paste and the one
  // on top is the one that gets used: a lead sheet went into "Add one company" and became 106
  // cards, each named after a whole tab-separated row, the header row included.
  //
  // Ordered rather than hidden. Adding a single company by hand is still a real thing to do —
  // removing the control would trade one missing capability for another (§Lessons 43) — but it
  // is the rarer one here, so it goes second.
  const add = document.getElementById('targetAddSection');
  const imp = document.getElementById('sheetImportSection');
  const sheetFirst = (voice || '') === 'premise';
  if (add) add.style.order = sheetFirst ? '2' : '1';
  if (imp) imp.style.order = sheetFirst ? '1' : '2';
  // CTX-1. #premiseControls is deliberately NOT toggled — both shapes have a constant
  // paragraph. It used to live inside #targetControls, so on a jobs Space there was nowhere to
  // type one, which is why `gauntlet` and `job-search` sent the same email as each other.
  const c = copy || {};
  const title = document.getElementById('premiseTitle');
  const hint = document.getElementById('premiseHint');
  const box = document.getElementById('offerInput');
  if (title && c.title && title.textContent !== c.title) title.textContent = c.title;
  if (hint && c.hint && hint.innerHTML !== c.hint) hint.innerHTML = c.hint;
  if (box && c.placeholder && box.placeholder !== c.placeholder) box.placeholder = c.placeholder;
  // SPACE-0. Collapse once there IS a premise: written once per campaign, then read almost
  // never, while costing 290px above the table on every render. Collapsed only on the FIRST
  // render that finds one — after that the operator's own toggle wins, or opening it to edit
  // would be undone by the next 2.5s tick.
  const det = document.getElementById('premiseBox');
  const mark = document.getElementById('premiseMark');
  const filled = !!(offer || '').trim();
  if (det && filled && !PREMISE_SETTLED) { det.open = false; PREMISE_SETTLED = true; }
  if (det && !filled) PREMISE_SETTLED = false;
  if (mark) mark.textContent = filled ? '✓ in every draft here' : '';
  // Never while it has focus. `refresh()` runs every 2.5s and this is a textarea the operator
  // types a paragraph into — the same reason the whole jobs table skips its rewrite mid-edit.
  if (box && document.activeElement !== box && box.value !== (offer || '')) {
    box.value = offer || '';
  }
}

async function addTargets(btn) {
  const box = document.getElementById('targetInput');
  const out = document.getElementById('targetStatus');
  const text = (box.value || '').trim();
  if (!text) { out.textContent = 'Type a company name first.'; return; }
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = 'Adding…';
  const r = await post('/api/target/add', {space: SPACE_ID, text});
  btn.disabled = false;
  btn.textContent = label;
  out.textContent = r.message || '';
  // Only clear what was accepted. Clearing the whole box on a partial import throws away the
  // lines that failed along with the ones that worked, and the operator cannot retype what
  // they can no longer see.
  if (r.ok && !(r.rejected || []).length) box.value = '';
  else if (r.ok) box.value = (r.rejected || []).join('\n');
  refresh();
}

// SHEET-1. A pasted spreadsheet becomes one card per company and one contact per person.
//
// The paste is NEVER cleared, even on a clean import. `addTargets` above can clear its box
// because it holds a handful of typed lines; this one holds a range copied out of a sheet the
// operator may have spent an afternoon assembling, and re-importing a GROWN version of it is
// the normal way to use this. Throwing that away to signal success is a bad trade.
async function importSheet(btn) {
  const box = document.getElementById('sheetInput');
  const out = document.getElementById('sheetStatus');
  const rej = document.getElementById('sheetRejects');
  const text = (box.value || '').trim();
  rej.hidden = true; rej.innerHTML = '';
  if (!text) { out.textContent = 'Paste some rows first, including the header row.'; return; }
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = 'Importing…';
  const r = await post('/api/import-sheet', {space: SPACE_ID, text});
  btn.disabled = false;
  btn.textContent = label;
  out.textContent = r.message || '';
  // Skipped rows are listed with the line number the operator sees in their own sheet. A count
  // alone is not something anyone can act on (§Lessons 15) — "3 skipped" sends them hunting,
  // "line 7: no company" sends them to line 7.
  const bad = r.rejected || [];
  if (bad.length) {
    rej.hidden = false;
    rej.innerHTML = '<b>Skipped rows</b><br>' + bad.slice(0, 25).map(x =>
      `line ${esc(String(x.line || '?'))}: ${esc(x.reason || '')}` +
      (x.text ? ` <span class="muted">${esc(String(x.text).slice(0, 70))}</span>` : '')
    ).join('<br>') + (bad.length > 25 ? `<br><span class="muted">…and ${bad.length - 25} more</span>` : '');
  }
  renderSheetCoverage(r.coverage || []);
  refresh();
}

//: What the sheet SUPPLIES, under what it imported.
//:
//: Rows imported and rows you can act on are different numbers, and only the first was ever
//: shown. The first real sheet imported 45 companies and 105 people with no errors at all — and
//: 85 of those people had no address, none had a LinkedIn URL, and 30 of the 45 companies had
//: nobody reachable. Every one of those is a clean success by the old message.
//:
//: A MISSING COLUMN AND AN EMPTY ONE ARE SEPARATE FINDINGS and are labelled separately: one is
//: fixed by adding a heading, the other by filling cells in. Collapsed into a percentage, the
//: half that tells you what to go and do is the half that is lost.
function renderSheetCoverage(rows) {
  const box = document.getElementById('sheetCoverage');
  if (!box) return;
  if (!rows.length) { box.hidden = true; box.innerHTML = ''; return; }
  const gaps = rows.filter(c => !c.ok);
  box.hidden = false;
  if (!gaps.length) {
    box.innerHTML = '<div class="cov-ok">Every column this sheet needs is filled in.</div>';
    return;
  }
  // Worst first. A field nobody supplied outranks one with a handful of blanks, because it is
  // the one that is a decision about the sheet rather than an oversight in it.
  gaps.sort((a, b) => (a.have / a.total) - (b.have / b.total));
  box.innerHTML = `<div class="cov"><b>What this sheet does not carry</b>${gaps.map(c => {
    const unit = c.unit === 'people' ? 'people' : 'companies';
    const miss = c.total - c.have;
    return `<div class="cov-row${c.have === 0 ? ' cov-none' : ''}">
      <span class="cov-n">${esc(String(miss))} of ${esc(String(c.total))}</span>
      <span class="cov-l">${esc(unit)} have no <b>${esc(c.label)}</b>${
        c.column ? '' : ' <span class="cov-tag">no such column</span>'}</span>
      <span class="cov-why">${esc(c.cost)}</span></div>`;
  }).join('')}<div class="hint" style="margin-top:8px">Nothing here needs redoing — add the
    columns to your sheet, fill them in, and paste the whole thing again. Matching people are
    updated in place and their drafts, replies and follow-ups are kept.</div></div>`;
}

//: The header spellings this parser accepts, served from `_FIELDS` so the two cannot disagree.
//: Fetched on first open rather than shipped in /api/status, which re-sends every 2.5 seconds.
let SHEET_COLUMNS = null;
async function toggleSheetColumns(btn) {
  const box = document.getElementById('sheetColumns');
  if (!box.hidden) { box.hidden = true; btn.textContent = 'Which columns are read?'; return; }
  if (!SHEET_COLUMNS) {
    const r = await post('/api/sheet-columns', {});
    if (!r || r.ok === false) return;
    SHEET_COLUMNS = r.columns || [];
  }
  btn.textContent = 'Hide the column list';
  box.hidden = false;
  box.innerHTML = `<div class="cov"><b>Any of these headings work</b>
    ${SHEET_COLUMNS.map(c => `<div class="cov-row">
      <span class="cov-n">${esc(c.field)}${c.required ? ' <span class="cov-tag">needed</span>' : ''}</span>
      <span class="cov-l" style="grid-column: 2 / span 2">${esc(c.spellings.join(', '))}</span>
    </div>`).join('')}
    <div class="hint" style="margin-top:8px">Case and punctuation are ignored, so
      <b>LinkedIn URL</b>, <b>linkedin_url</b> and <b>LINKEDIN</b> are the same column. Anything
      not on this list is ignored rather than rejected, so extra columns are safe to leave in.</div></div>`;
}

async function saveOffer(btn) {
  const box = document.getElementById('offerInput');
  const out = document.getElementById('offerStatus');
  btn.disabled = true;
  const r = await post('/api/space/offer', {space: SPACE_ID, offer: box.value || ''});
  btn.disabled = false;
  out.textContent = r.message || '';
}

async function post(path, payload) {
  const res = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload || {})});
  const data = await res.json();
  if (!res.ok) alert(data.error || data.message || 'Request failed');
  return data;
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// ---- Pipeline visualizer: a live stepper (Import → Enrich → Tailor → Cover → Apply) ----
const PIPE_STAGES = [
  { key: 'import', label: 'Import', icon: '1', stat: 'total' },
  { key: 'enrich', label: 'Enrich', icon: '2', stat: 'enriched' },
  { key: 'tailor', label: 'Tailor', icon: '3', stat: 'tailored' },
  { key: 'cover',  label: 'Cover',  icon: '4', stat: 'covers' },
  { key: 'apply',  label: 'Apply',  icon: '5', stat: 'applied' },
];
const _PIPE_ORDER = { idle: 0, active: 1, done: 2, failed: 3 };
let PIPE_STATUS = {};
let PIPE_STATS = {};

function pipeShow(on) { document.getElementById('pipeline').style.display = on ? 'block' : 'none'; }
function pipeReset() {
  PIPE_STATUS = {}; PIPE_STAGES.forEach(s => PIPE_STATUS[s.key] = 'idle');
  document.getElementById('pipeLog').innerHTML = ''; pipeShow(true); pipeRender();
}
function pipeSet(key, status) { PIPE_STATUS[key] = status; pipeRender(); }
// Monotonic upgrade — never downgrade a stage (log for a later command won't reset earlier ones).
function pipeUp(key, status) {
  if ((_PIPE_ORDER[status] || 0) > (_PIPE_ORDER[PIPE_STATUS[key]] || 0)) PIPE_STATUS[key] = status;
}
function pipeRender() {
  document.getElementById('pipeSteps').innerHTML = PIPE_STAGES.map(s => {
    const st = PIPE_STATUS[s.key] || 'idle';
    const inner = st === 'active' ? '<span class="pipe-spin"></span>' : st === 'done' ? '✓' : st === 'failed' ? '✗' : s.icon;
    const cnt = PIPE_STATS[s.stat] != null ? `${PIPE_STATS[s.stat]}` : '';
    return `<div class="pipe-step ${st}"><div class="pnode">${inner}</div><div class="plabel">${s.label}</div><div class="pcount">${cnt}</div></div>`;
  }).join('');
}
function pipeRenderLog(lines) {
  const box = document.getElementById('pipeLog');
  box.innerHTML = (lines || []).map(l => {
    const e = esc(l);
    if (/^STAGE:/.test(l)) return `<span class="lg-stage">${e}</span>`;
    if (/RESULT:APPLIED|complete ✓|success|✓/i.test(l)) return `<span class="lg-ok">${e}</span>`;
    if (/error|fail|429|400|denied|not found/i.test(l)) return `<span class="lg-err">${e}</span>`;
    return e;
  }).join('\n');
  box.scrollTop = box.scrollHeight;
}
// Derive enrich/tailor/cover sub-progress from the backend's "STAGE:" log lines (monotonic).
function advanceStagesFromLog(lines) {
  const txt = (lines || []).join('\n');
  if (/STAGE:\s*enrich/i.test(txt)) pipeUp('enrich', 'active');
  if (/STAGE:\s*(tailor|score bypass)/i.test(txt)) { pipeUp('enrich', 'done'); pipeUp('tailor', 'active'); }
  if (/STAGE:\s*cover/i.test(txt)) { pipeUp('enrich', 'done'); pipeUp('tailor', 'done'); pipeUp('cover', 'active'); }
  if (/prepare complete/i.test(txt)) {
    pipeUp('enrich', 'done');
    // Reflect the REAL outcome: the prepare exits 0 even when tailoring/cover errored, so parse
    // the result dict and mark a stage FAILED (red) when it errored — never a false green ✓.
    const m = txt.match(/prepare complete:\s*\{([^}]*)\}/i);
    const num = (k) => { const mm = m && m[1].match(new RegExp("'" + k + "':\\s*(\\d+)")); return mm ? parseInt(mm[1], 10) : null; };
    const tErr = num('tailor_errors'), cErr = num('cover_errors');
    PIPE_STATUS.tailor = tErr > 0 ? 'failed' : 'done';
    PIPE_STATUS.cover = cErr > 0 ? 'failed' : 'done';
  }
}

// Poll /api/status until the background command (prepare/apply) finishes, keeping the status
// line + pipeline visualizer live the whole time and refreshing the table so materials appear
// the moment they're ready. Resolves with the final command object.
async function pollCommandUntilDone(label) {
  const cmdEl = document.getElementById('command');
  for (let i = 0; i < 600; i++) { // ~20 min ceiling (2s * 600)
    const data = await (await fetch(statusUrl())).json();
    const c = data.command || {};
    PIPE_STATS = data.stats || {};
    pipeRenderLog(c.log || []);
    advanceStagesFromLog(c.log || []);
    pipeRender();
    await refresh();
    if (c.running) {
      cmdEl.textContent = `${label}… running (${i * 2}s)`;
    } else {
      const rc = c.returncode;
      cmdEl.textContent = rc === 0 || rc == null ? `${label} complete ✓` : `${label} failed (exit ${rc}) — see log below`;
      return c;
    }
    await new Promise(r => setTimeout(r, 2000));
  }
  cmdEl.textContent = `${label} still running — check the log below`;
  return null;
}

async function prepareJobs() {
  const btn = document.getElementById('prepareBtn');
  const cmdEl = document.getElementById('command');
  const data = await post('/api/prepare', {});
  if (!data.ok) { cmdEl.textContent = data.message || 'Could not start prepare'; return; }
  if (btn) btn.disabled = true;
  cmdEl.textContent = 'Preparing materials… (enrich → tailor → cover, ~30–60s)';
  await pollCommandUntilDone('Prepare materials');
  if (btn) btn.disabled = false;
}

async function applyJobs() {
  const cmdEl = document.getElementById('command');
  // Guard: apply only works on jobs that are already prepared (tailored + cover). If none are
  // Ready, launching apply just silently does nothing — so tell the user instead of no-op'ing.
  const status = await (await fetch(statusUrl())).json();
  const ready = (status.stats || {}).ready || 0;
  const dryRun = document.getElementById('dryRun').checked;
  if (ready < 1) {
    cmdEl.textContent = 'No prepared materials to apply with. Click "Prepare Materials" first and wait for it to finish.';
    alert('Nothing is ready to apply yet.\n\nClick "Prepare Materials" first and wait for "Prepare materials complete ✓", then Apply.');
    return;
  }
  if (!dryRun && !confirm(`Fill ${ready} application(s) for your review?\n\nApplyPilot fills each application in Chrome, then STOPS before submitting and leaves the browser open for you to review + click Submit. It never auto-submits.`)) return;
  const btn = document.getElementById('applyBtn');
  const data = await post('/api/apply', {limit: document.getElementById('limit').value, dry_run: dryRun, copilot: !dryRun});
  if (!data.ok) { cmdEl.textContent = data.message || 'Could not start apply'; return; }
  if (btn) btn.disabled = true;
  cmdEl.textContent = dryRun ? 'Applying (DRY RUN — no submit)…' : 'Filling the application in Chrome for your review…';
  const ac = await pollCommandUntilDone(dryRun ? 'Dry-run apply' : 'Fill for review');
  if (!dryRun && !(ac && ac.returncode && ac.returncode !== 0)) {
    cmdEl.textContent = '✅ Filled — review in the open Chrome window, click Submit, then "Mark submitted ✓" on the job row.';
  }
  if (btn) btn.disabled = false;
}
// The one button: import (if URLs pasted) -> prepare -> apply, streaming live status through
// each phase. Stops early with a clear message if a phase fails or nothing ends up Ready.
async function runEverything() {
  const btn = document.getElementById('runBtn');
  const cmdEl = document.getElementById('command');
  const urls = document.getElementById('urls').value.trim();
  btn.disabled = true;
  pipeReset(); // show the visualizer, all stages idle
  try {
    // 1) Import any pasted URLs (skip if the box is empty — re-runs work on already-imported jobs).
    if (urls) {
      pipeSet('import', 'active');
      cmdEl.textContent = 'Importing URLs…';
      // SPACE_ID, or the row lands in whatever the column default is — 'job-search' — and a
      // posting pasted while standing in Gauntlet turns up under a different tab. Separation is
      // the whole reason the tabs exist. `addTargets` has passed its Space since SPACE-3.
      const imp = await post('/api/import', {urls, space: SPACE_ID});
      if (imp.ok === false) {
        document.getElementById('importStatus').textContent = imp.message || 'Import refused.';
        pipeSet('import', 'failed');
        cmdEl.textContent = imp.message || 'Import refused.';
        return;
      }
      document.getElementById('importStatus').textContent =
        `Imported ${imp.inserted || 0} new URL(s); ${imp.duplicates || 0} already known.`;
      pipeSet('import', 'done');
      await refresh();
    } else {
      pipeSet('import', 'done'); // working on already-imported jobs
    }

    // 2) Prepare materials (enrich -> tailor -> cover), poll to completion. Sub-stages advance
    //    from the backend's STAGE: log lines inside pollCommandUntilDone.
    const prep = await post('/api/prepare', {});
    if (!prep.ok) { cmdEl.textContent = prep.message || 'Could not start prepare.'; pipeSet('enrich', 'failed'); return; }
    pipeSet('enrich', 'active');
    cmdEl.textContent = 'Preparing materials… (enrich → tailor → cover, ~30–60s)';
    const pc = await pollCommandUntilDone('Prepare materials');
    if (pc && pc.returncode && pc.returncode !== 0) { // prepare failed — mark the last active stage failed
      ['cover','tailor','enrich'].some(k => { if (PIPE_STATUS[k] === 'active') { pipeSet(k, 'failed'); return true; } return false; });
      return;
    }
    pipeSet('enrich', 'done');
    // advanceStagesFromLog already set tailor/cover to done-or-FAILED from the prepare result;
    // don't blindly force them green. If either genuinely failed, surface it and stop.
    if (PIPE_STATUS.tailor === 'failed' || PIPE_STATUS.cover === 'failed') {
      pipeRender();
      cmdEl.textContent = 'Prepare finished but tailoring/cover failed (see log) — no materials to apply. Check your LLM keys.';
      return;
    }
    pipeSet('tailor', 'done'); pipeSet('cover', 'done');

    // 3) Apply — co-pilot mode: fill the form in Chrome, then STOP and leave it open for you to
    //    review + submit. Only runs if something's Ready (else say so, don't launch a no-op).
    const status = await (await fetch(statusUrl())).json();
    const ready = (status.stats || {}).ready || 0;
    if (ready < 1) { cmdEl.textContent = 'Materials prepared, but no jobs are Ready to apply.'; return; }
    const dryRun = document.getElementById('dryRun').checked;
    if (!dryRun && !confirm(`Fill ${ready} application(s) for your review?\n\nApplyPilot opens Chrome and fills each application, then STOPS before submitting and leaves the browser open for you to review and click Submit yourself. It never auto-submits.`)) {
      cmdEl.textContent = `Prepared ${ready} job(s). Apply cancelled.`;
      return;
    }
    // copilot=true (default) unless dry-run.
    const ap = await post('/api/apply', {limit: document.getElementById('limit').value, dry_run: dryRun, copilot: !dryRun});
    if (!ap.ok) { cmdEl.textContent = ap.message || 'Could not start apply.'; return; }
    pipeSet('apply', 'active');
    cmdEl.textContent = dryRun ? 'Applying (DRY RUN — no submit)…' : 'Filling the application in Chrome — then handing it to you to review + submit…';
    const ac = await pollCommandUntilDone(dryRun ? 'Dry-run apply' : 'Fill for review');
    pipeSet('apply', ac && ac.returncode && ac.returncode !== 0 ? 'failed' : 'done');
    if (!dryRun && !(ac && ac.returncode && ac.returncode !== 0)) {
      cmdEl.textContent = '✅ Filled — review the application in the open Chrome window, click Submit, then hit "Mark submitted ✓" on the job row.';
    }
  } finally {
    btn.disabled = false;
  }
}
function toggleAdvanced() {
  const el = document.getElementById('advancedControls');
  el.style.display = el.style.display === 'none' ? 'flex' : 'none';
}
async function stopCommand() { await post('/api/stop', {}); refresh(); }
// Pause is NOT Stop. Stop killpg's the run, which reaches Chrome and loses a part-filled form.
// Pause stops only the agent and leaves the browser up for you to finish in.
// Manual poke at the same poller the background thread uses — for when you know a reply just
// landed and do not want to wait out the 5-minute cycle.
async function checkReplies(btn) {
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = 'Checking…';
  const r = await post('/api/check-replies', {});
  const cmdEl = document.getElementById('command');
  if (cmdEl) cmdEl.textContent = r.message || '';
  btn.disabled = false; btn.textContent = was;
  refresh();
}
async function pauseApply() {
  const cmdEl = document.getElementById('command');
  const r = await post('/api/pause-apply', {});
  if (cmdEl) cmdEl.textContent = r.message || (r.ok ? 'Pausing…' : 'Nothing to pause.');
  refresh();
}
async function deleteJob(url, label) {
  if (!confirm(`Delete this application?\n\n${label}`)) return;
  const data = await post('/api/delete', {url});
  if (data.message) document.getElementById('command').textContent = data.message;
  await refresh();
}
// Every job state → one clear indicator (icon + label + color class). Order here also documents
// the pipeline: imported → enriched → scored → ready → (applying) → review/needs-you → applied.
const STATUS_META = {
  imported:        { icon: '•',  label: 'Imported',        cls: 'st-muted' },
  enriched:        { icon: '•',  label: 'Enriched',        cls: 'st-muted' },
  detail_failed:   { icon: '✗',  label: 'Enrich failed',   cls: 'st-red' },
  scored:          { icon: '◆',  label: 'Scored',          cls: 'st-grey' },
  ready:           { icon: '✓',  label: 'Ready to fill',   cls: 'st-blue' },
  in_progress:     { icon: '⏳', label: 'Applying…',        cls: 'st-yellow st-pulse' },
  dryrun:          { icon: '✓',  label: 'Dry-run filled',  cls: 'st-blue' },
  ready_to_submit: { icon: '⚠',  label: 'Review & submit', cls: 'st-amber' },
  needs_human:     { icon: '⚠',  label: 'Needs you',       cls: 'st-red' },
  failed:          { icon: '✗',  label: 'Failed',          cls: 'st-red' },
  applied:         { icon: '✓',  label: 'Applied',         cls: 'st-green' },
  rejected:        { icon: '✕',  label: 'Rejected',        cls: 'st-rejected' },
  cancelled:       { icon: '⊘',  label: 'Cancelled',       cls: 'st-cancelled' },
  ghost:           { icon: '👻', label: 'Ghost job',       cls: 'st-ghost' },
};

// ── Job filter buckets: map the 12 granular statuses → a few meaningful stages you filter by. ──
const JOB_BUCKETS = {
  all:       { label: 'All',         icon: '',    statuses: null,   // null = everything
               tip: 'Every application in this Space' },
  needs_you: { label: 'Needs you',   icon: '👉',  statuses: ['ready','ready_to_submit','needs_human','failed'],
               tip: 'Stopped and waiting on a human — a form to review, a sign-in wall, a failure' },
  progress:  { label: 'In progress', icon: '🚀',  statuses: ['imported','enriched','scored','detail_failed','in_progress','dryrun'],
               tip: 'Still being prepared — imported, scored, or filling right now' },
  applied:   { label: 'Applied',     icon: '✅',  statuses: ['applied'],
               tip: 'Submitted. Where the outreach and follow-up work happens' },
  rejected:  { label: 'Rejected',    icon: '✕',   statuses: ['rejected'],
               tip: 'They said no. An outcome, and it counts in your funnel' },
  // Separate from Rejected on purpose. A pulled requisition, a hiring freeze or a role filled
  // internally is not a decision about you, and filing it under Rejected makes the rejection
  // rate describe something that never happened.
  cancelled: { label: 'Cancelled',   icon: '⊘',   statuses: ['cancelled'],
               tip: 'The posting went away — req pulled, frozen, or filled internally. Not a rejection' },
  // Its own bucket rather than a flavour of Cancelled. Cancelled means something real STOPPED;
  // this means it never started, and the two teach opposite things — one is bad luck, the other
  // is a board or an employer worth avoiding next time.
  ghost:     { label: 'Ghost jobs',   icon: '👻',  statuses: ['ghost'],
               tip: 'The opening was never real — evergreen req, endless repost, listing kept up for show. Not a rejection' },
};
const JOB_FILTER_ORDER = ['all','needs_you','progress','applied','rejected','cancelled','ghost'];

// A job that has LEFT the pipeline, for whichever reason. `status === 'rejected'` was checked in
// eight separate places to mean this, and adding a second closed status by scattering a second
// magic string beside each one is §Lessons 49 with a guarantee of missing one — the miss would
// be silent, and it would show up as a cancelled job still being offered follow-ups.
const CLOSED_STATUSES = ['rejected', 'cancelled', 'ghost'];
function isClosed(j) { return CLOSED_STATUSES.includes(j && j.status); }
//: What a closed row says above its date. A ternary handled two states and would have
//: printed 'Rejected' for a ghost job — the same silent miss the predicate above exists to
//: prevent, one line lower.
const CLOSED_LABELS = { rejected: 'Rejected', cancelled: 'Cancelled', ghost: 'Ghost job' };
function closedLabel(status) { return CLOSED_LABELS[status] || 'Closed'; }
let JOB_FILTER = 'all';  // client-side view state; persists across the 2.5s auto-refresh

function jobInBucket(j, bucketKey) {
  const b = JOB_BUCKETS[bucketKey];
  if (!b || !b.statuses) return true;             // 'all'
  return b.statuses.includes(j.status);
}

// ── Temperature: a SECOND axis, deliberately not folded into JOB_BUCKETS ─────
//
// Reported as "not sure this is the best way to know what's up with an application" — and the
// confusion is the finding, not a misreading. Four of the seven bands (new → active → cooling →
// cold) are a countdown of OUR OWN sending; `warm` and `won` are about what THEY did. Those are
// different questions, and one chip presenting them as a single scale reads as if warm were
// simply more than active. It is not: a cooling job can get a reply tomorrow, and an active one
// can stay silent forever.
//
// The filter is its own axis that ANDs with the status bucket — "applied AND warm" is a real
// thing to ask for, and folding the bands into JOB_BUCKETS would have made choosing one
// silently clear the other.
const TEMP_BANDS = {
  won:           { icon: '🏆', label: 'won',    axis: 'them',
                   meaning: 'an interview is booked' },
  warm:          { icon: '●',  label: 'warm',   axis: 'them',
                   meaning: 'a person replied or opened your deck' },
  active:        { icon: '●',  label: 'active', axis: 'us',
                   meaning: 'still scheduled, nobody has answered yet' },
  cooling:       { icon: '◐',  label: 'cooling', axis: 'us',
                   meaning: 'nearly every email sent, still no answer' },
  cold:          { icon: '○',  label: 'cold',   axis: 'us',
                   meaning: 'all sent, silent long enough to be over' },
  new:           { icon: '·',  label: 'new',    axis: 'us',
                   meaning: 'nothing sent yet' },
  undeliverable: { icon: '⚠',  label: 'undeliverable', axis: 'fix',
                   meaning: 'mail is bouncing — fix the address, do not chase' },
};
const TEMP_ORDER = ['won','warm','active','cooling','cold','new','undeliverable'];
//: The axis a band belongs to, as a PREFIX on its tooltip.
//:
//: The legend this replaced grouped the bands under these headings, and the grouping was doing
//: real work: four of the seven count OUR OWN sending and two are about what THEY did, which is
//: the distinction that made one chip unreadable as a single scale. Per-pill tooltips have no
//: grouping to carry that, so each tip states its axis outright — otherwise removing the legend
//: throws away the explanation and keeps only the vocabulary.
//:
//: `them` matters most: §Lessons 35 — effort must never read as progress, and this is the only
//: axis that means you are closer to an interview.
const TEMP_AXIS_TIP = {
  them: 'They responded',
  us:   'Your outreach',
  fix:  'Needs fixing',
};
let TEMP_FILTER = 'all';

//: What a band pill says on hover. Composed from axis + meaning rather than written out per
//: band, so a band cannot end up on one axis in the data and another in its own description.
function tempTip(key) {
  const b = TEMP_BANDS[key];
  return b ? `${TEMP_AXIS_TIP[b.axis]} — ${b.meaning}` : '';
}

function jobInTemp(j, key) {
  if (!key || key === 'all') return true;
  return ((j.temperature || {}).band || '') === key;
}

// ── Tags: the last column, and the facets you filter by ─────────────────────
//
// Replaces the old Links column. Those links were already redundant — the `job` one was
// truncated and uncopyable, which is the whole reason the Job TAB exists and carries both URLs
// in full. The column's width is better spent on what actually distinguishes one row from
// another when you are scanning sixteen of them.
//
// Every tag is DERIVED from fields already on the wire (location, salary, fit_score,
// applied_at, company). No schema change, no new query, nothing to keep in sync — a tag cannot
// drift from the job because it is not stored.

//: Free-text salary → something that fits in a chip. "$150,000 - $200,000/yr" → "$150–200k".
//: Returns the raw string when it cannot parse, and "" only when there is nothing at all: a
//: salary we failed to prettify is still worth showing.
function salaryTag(raw) {
  const s = String(raw || '').trim();
  if (!s) return '';
  const nums = (s.match(/\d[\d,]*(?:\.\d+)?/g) || [])
    .map(n => parseFloat(n.replace(/,/g, ''))).filter(n => n > 0);
  if (!nums.length) return s.slice(0, 24);
  // Hourly and small numbers stay as written — "$45/hr" must not become "$0k".
  const lo = Math.min(...nums), hi = Math.max(...nums);
  if (lo < 1000 && hi < 1000) return s.slice(0, 24);
  const n = v => Math.round(v / 1000);
  // The unit goes on the range, not on each end: "$180–220k", not "$180k–220k".
  return lo === hi ? `$${n(lo)}k` : `$${n(lo)}–${n(hi)}k`;
}

//: Long locations eat the column. "Austin, Texas, United States" → "Austin, TX".
const _STATE_ABBR = {
  alabama:'AL',alaska:'AK',arizona:'AZ',arkansas:'AR',california:'CA',colorado:'CO',
  connecticut:'CT',delaware:'DE',florida:'FL',georgia:'GA',hawaii:'HI',idaho:'ID',
  illinois:'IL',indiana:'IN',iowa:'IA',kansas:'KS',kentucky:'KY',louisiana:'LA',maine:'ME',
  maryland:'MD',massachusetts:'MA',michigan:'MI',minnesota:'MN',mississippi:'MS',
  missouri:'MO',montana:'MT',nebraska:'NE',nevada:'NV','new hampshire':'NH','new jersey':'NJ',
  'new mexico':'NM','new york':'NY','north carolina':'NC','north dakota':'ND',ohio:'OH',
  oklahoma:'OK',oregon:'OR',pennsylvania:'PA','rhode island':'RI','south carolina':'SC',
  'south dakota':'SD',tennessee:'TN',texas:'TX',utah:'UT',vermont:'VT',virginia:'VA',
  washington:'WA','west virginia':'WV',wisconsin:'WI',wyoming:'WY',
};
function locationTag(raw) {
  let s = String(raw || '').trim();
  if (!s) return '';
  if (/^(remote|anywhere)\b/i.test(s)) return 'Remote';
  s = s.replace(/,?\s*(united states|usa|u\.s\.a?\.?)$/i, '').trim().replace(/,$/, '');
  const parts = s.split(',').map(x => x.trim()).filter(Boolean);
  if (parts.length >= 2) {
    const abbr = _STATE_ABBR[parts[1].toLowerCase()];
    return `${parts[0]}, ${abbr || parts[1]}`.slice(0, 28);
  }
  return s.slice(0, 28);
}

//: Tags for one job. `k` is the filter key (stable, lowercased); `label` is what you read.
//: Kept to at most five so the column stays scannable — a row wearing nine chips is noise.
function jobTags(j) {
  const out = [];
  const push = (kind, value, icon) => {
    const v = String(value || '').trim();
    if (v) out.push({ k: `${kind}:${v.toLowerCase()}`, kind, label: `${icon} ${v}`, value: v });
  };
  push('loc', locationTag(j.location), '📍');
  push('pay', salaryTag(j.salary), '💰');
  if (j.fit_score !== null && j.fit_score !== undefined && j.fit_score !== '')
    push('fit', `${j.fit_score}/10`, '⭐');
  push('src', j.company, '🏢');
  // Date only — fmtDate carries a time ("Jul 20, 05:00 AM") which is three times the width
  // of the chip and tells you nothing you would ever filter on.
  const when = j.applied_at || j.rejected_at || '';
  if (when) push('when', String(fmtDate(when)).split(',')[0], '📅');
  return out;
}

// A tag key inside a single-quoted onclick, made safe. `esc()` is NOT enough: it turns ' into
// &#39;, which the HTML parser turns back into ' before JS ever sees the attribute, so an
// apostrophe (O'Fallon, MO) breaks the handler — and a broken onclick throws silently, leaving
// a chip that just does nothing when clicked. Same encode/decode pair `deleteContact` uses.
// encodeURIComponent does NOT escape ' ( ) ! * — they are "unreserved marks" in RFC 2396 and
// it leaves them alone. So the apostrophe survives into the single-quoted attribute and closes
// the string early. Escaping it explicitly is the whole point; decodeURIComponent reverses %27
// like any other escape, so the key still round-trips exactly.
function tagArg(k) { return `decodeURIComponent('${encodeURIComponent(k).replace(/'/g, '%27')}')`; }

// Active tag filters. A SET of `k` values; a row must carry ALL of them (AND, not OR) — with OR
// a second click widens the result, which reads as the filter not working.
const TAG_FILTER = new Set();
let JOB_QUERY = '';

function toggleTag(k) {
  if (TAG_FILTER.has(k)) TAG_FILTER.delete(k); else TAG_FILTER.add(k);
  rerenderJobs();
}
function clearTags() { TAG_FILTER.clear(); rerenderJobs(); }

function jobMatchesTags(j) {
  if (!TAG_FILTER.size) return true;
  const have = new Set(jobTags(j).map(t => t.k));
  for (const k of TAG_FILTER) if (!have.has(k)) return false;
  return true;
}

// Search covers what is ON the row plus the description, because "the one about the drone
// startup" is how you actually remember a job. Every term must match somewhere (AND), so
// adding a word always narrows.
// Search covered nine fields and never looked at CONTACTS — so a recruiter's name returned
// nothing while the dashboard was displaying that name one click away. It also searched
// `j.description`, which is a 900-char EXCERPT, so a term in paragraph six of a posting was
// unfindable. Both were reported as "it only filters by job name".
//
// Returns WHY it matched, not just whether: a row whose visible text contains none of the
// search terms looks like a bug unless it says "matched: Sarah Chen".
function jobSearchMatch(j) {
  const q = JOB_QUERY.trim().toLowerCase();
  if (!q) return { hit: true, via: [] };
  const terms = q.split(/\s+/);
  const jobHay = [j.title, j.company, j.location, j.salary, j.description, j.status,
                  j.url, j.application_url, JOB_DESC.get(j.url) || '',
                  ...jobTags(j).map(t => t.value)]
    .filter(Boolean).join(' ').toLowerCase();

  const people = (j.contacts || []).map(c => ({
    name: c.full_name || '',
    hay: [c.full_name, c.title, c.email, c.company].filter(Boolean).join(' ').toLowerCase(),
  }));

  // AND across terms, but a term may be satisfied by the job OR by any one person — otherwise
  // "google sarah" fails, since no single field holds both.
  const via = new Set();
  for (const term of terms) {
    if (jobHay.includes(term)) continue;
    const who = people.filter(p => p.hay.includes(term));
    if (!who.length) return { hit: false, via: [] };
    who.forEach(p => { if (p.name) via.add(p.name); });
  }
  return { hit: true, via: [...via] };
}

function jobMatchesQuery(j) { return jobSearchMatch(j).hit; }

// Why this row is here, when nothing visible on it contains what was typed. Searching a
// recruiter's name and getting back a list of jobs that do not mention them reads as broken
// unless the row says which person matched.
function matchedVia(j) {
  const via = jobSearchMatch(j).via;
  if (!via.length) return '';
  return `<div class="matched-via">matched: ${esc(via.slice(0, 3).join(', '))}${via.length > 3 ? ` +${via.length - 3}` : ''}</div>`;
}

// One request, once per session, the first time anything is typed. The two rejected options
// were shipping every full description on the 2.5s refresh (~130KB forever) and a round trip
// per keystroke-batch. Until it lands, search covers the excerpt — which is what it did
// before, so it degrades to the old behaviour rather than to nothing.
let JOB_DESC_LOADED = false;
async function warmDescriptions() {
  if (JOB_DESC_LOADED) return;
  JOB_DESC_LOADED = true;
  const r = await post('/api/job-descriptions', {space: SPACE_ID});
  if (!r.ok) { JOB_DESC_LOADED = false; return; }
  for (const [url, text] of Object.entries(r.descriptions || {})) {
    if (!JOB_DESC.has(url)) JOB_DESC.set(url, text || '');
  }
  rerenderJobs();
}

function onJobSearch(v) {
  JOB_QUERY = v || '';
  const clear = document.getElementById('jobSearchClear');
  if (clear) clear.hidden = !JOB_QUERY;
  // Never refetch /api/status while typing — that path is 50 SQL statements (§Lessons 11, 26).
  // This is a different endpoint, guarded to run at most once.
  if (JOB_QUERY) warmDescriptions();
  rerenderJobs();
}
function clearJobSearch() {
  const el = document.getElementById('jobSearch');
  if (el) el.value = '';
  onJobSearch('');
  if (el) el.focus();
}

function renderActiveTags() {
  const el = document.getElementById('activeTags');
  if (!el) return;
  if (!TAG_FILTER.size) { el.innerHTML = ''; return; }
  // Label from the key, so a removed job cannot leave an unlabelable chip stuck on screen.
  el.innerHTML = [...TAG_FILTER].map(k => {
    const value = k.slice(k.indexOf(':') + 1);
    return `<button class="tag-chip on" onclick="toggleTag(${tagArg(k)})" title="Remove this filter">${esc(value)} ✕</button>`;
  }).join('') + `<button class="tag-clear" onclick="clearTags()">clear</button>`;
}
function setJobFilter(key) { JOB_FILTER = key; refresh(); }
//: Clicking the band you are already on clears it. There is no "All" pill in this group —
//: seven bands plus an eighth is a wall of chips — so the selected pill has to be its own way
//: out, or the only escape from a one-row view is reloading the page.
function setTempFilter(key) { TEMP_FILTER = (TEMP_FILTER === key) ? 'all' : key; refresh(); }

function renderJobFilters(jobs) {
  const el = document.getElementById('jobFilters');
  if (!el) return;
  // Each group counts with the OTHER group's filter applied, so a pill's number is what you
  // will actually get by clicking it rather than a board-wide total that lies under a filter.
  const status = JOB_FILTER_ORDER.map(key => {
    const b = JOB_BUCKETS[key];
    const n = jobs.filter(j => jobInBucket(j, key) && jobInTemp(j, TEMP_FILTER)).length;
    const on = key === JOB_FILTER ? ' active' : '';
    return `<button class="filter-pill${on}" data-tip="${esc(b.tip)}" `
         + `aria-label="${esc(`${b.label} — ${b.tip}`)}" onclick="setJobFilter('${key}')">`
         + `${b.icon ? b.icon + ' ' : ''}${b.label} <span class="fp-n">${n}</span></button>`;
  }).join('');

  const bands = TEMP_ORDER.map(key => {
    const b = TEMP_BANDS[key];
    const n = jobs.filter(j => jobInBucket(j, JOB_FILTER) && jobInTemp(j, key)).length;
    const on = key === TEMP_FILTER;
    // Empty bands are noise — but never drop the one that is SELECTED, or the active filter
    // vanishes the moment its last job changes band and the table looks broken with no way back.
    if (!n && !on) return '';
    // The tip is what the legend used to be, per pill: axis + meaning, plus the way out when
    // this one is the selected filter. `data-tip` and not `title` — a native tooltip waits
    // about a second, cannot be styled, and would stack a SECOND box under the CSS one. The
    // accessible name carries the same words, because a ::after is invisible to a screen reader.
    const tip = on ? `${tempTip(key)}. Click to clear this filter.` : tempTip(key);
    // `tb-` prefix, not the bare band name: one of the bands IS called "active", which is also
    // the selected-state class every filter pill uses. Unprefixed, the active-band pill carries
    // `active` at all times and renders as permanently selected — a filter that looks applied
    // when it is not, which is worse than one that looks unapplied when it is.
    return `<button class="filter-pill temp-pill tb-${esc(key)}${on ? ' active' : ''}" `
         + `data-tip="${esc(tip)}" aria-label="${esc(`${b.label} — ${tip}`)}" `
         + `onclick="setTempFilter('${key}')">`
         + `${b.icon} ${b.label} <span class="fp-n">${n}</span></button>`;
  }).join('');

  el.innerHTML = status + (bands ? `<span class="filter-sep" aria-hidden="true"></span>${bands}` : '');
}

// Every one of these ends the same way — you act in the open tab, then Continue, which
// reconnects a FRESH agent to that same browser and carries on from the current page. The
// wording differs because "the agent is stuck" and "you chose to take over" call for different
// reactions, and the generic 'blocker' text made a deliberate pause read like a failure.
const BLOCKER_ASK = {
  captcha: 'Solve the captcha in the open Chrome window, then click Continue.',
  login: 'Sign up or log in in the open Chrome window, then click Continue.',
  field: 'Fill the field it got stuck on in the open Chrome window, then click Continue.',
  paused: 'Paused. Do whatever you need in the open Chrome window — sign up, log in, fix a field — then click Continue and the agent picks up from there.',
  timeout: 'The agent ran out of time with the form part-filled. Finish or unblock it in the open Chrome window, then click Continue.',
  no_result_line: 'The agent stopped without saying why — the form may already be complete. Check the open Chrome window, then Continue (or Mark submitted).',
  blocker: 'Resolve the blocker in the open Chrome window, then click Continue.',
};
function badge(status) {
  const m = STATUS_META[status];
  if (!m) return `<span class="badge st-muted">${esc(status || 'new')}</span>`;
  return `<span class="badge ${m.cls}"><span class="st-icon">${m.icon}</span> ${esc(m.label)}</span>`;
}
//: The auto-sync snippet cap, SERVED rather than hardcoded — `messages.py` owns it, and a bound
//: written down twice is two bounds that drift. 0 until the first payload lands, which reads as
//: "nothing is clipped" and is the safe way to be wrong for 2.5 seconds.
let CONV_SNIPPET_MAX = 0;
let NET_AVAIL = false;
async function findContacts(url, skipKnown) {
  const r = await post('/api/network', {url, per_job: 5, skip_known: skipKnown ? 1 : ''});
  if (!r.ok) alert(r.message || 'Could not start');
  refresh();
}
function emailBadge(s) {
  if (s === 'verified') return '<span class="ebadge ok">verified</span>';
  if (s === 'unverified') return '<span class="ebadge warn">unverified</span>';
  return '<span class="ebadge none">no email</span>';
}
let GMAIL_AVAIL = false;
// Mirrors `attach_docs` from /api/status. The card reads this rather than the checkbox's own
// state, so what the row SAYS and what the sender DOES cannot disagree.
let ATTACH_DOCS = true;
// gmail.readonly granted? Decides whether we can offer "⤓ Fetch from Gmail" at all. False on a
// default install, where pasting is the only path.
let CONTENT_SCOPE = false;
//: The most recent /api/status jobs array, for click handlers that run after render.
let LAST_JOBS = [];
//: The exact HTML last written into #jobs. The 2.5s poll rebuilds that subtree from scratch, and
//: in the steady state it rebuilds it IDENTICALLY — so comparing against this skips the write
//: entirely and leaves the operator's scroll position, text selection and open menus alone.
//: It is the rendered string rather than a hash or a payload fingerprint on purpose: it is the
//: thing actually being written, so it cannot disagree with what is on screen.
let LAST_JOBS_HTML = null;
//: How often the dashboard's background poller runs, mirrored from the server so the
//: Interactions tab can state the real cadence instead of a hardcoded guess.
let POLL_EVERY_S = 300;
// `wantEmail` / `wantLi` select ONE channel — the contact panel shows them as tabs now, so
// rendering both at once is what made every contact card ~200px tall. Omit both to get the
// old stacked behaviour.
// `historyShown` means the conversation is already rendered ABOVE this block, so a sent email
// must not be repeated here as a disabled compose box. The actions stay — Copy email, the sent
// tag, the follow-up button — because those are the only parts of a sent draft still worth
// having, and dropping the whole block would take them with it.
function draftBlock(c, wantEmail, wantLi, historyShown) {
  const only = (wantEmail === undefined && wantLi === undefined);
  const hasEmail = !!c.email && (only || !!wantEmail);
  const hasLi = !!c.linkedin_url && (only || !!wantLi);
  if (!hasEmail && !hasLi) return '';

  const sent = !!c.emailed;
  // --- Email section (only when there's an address) ---
  let emailHtml = '';
  if (hasEmail) {
    const has = c.outreach_message || c.outreach_subject;
    const subj = esc(c.outreach_subject);
    const body = esc(c.outreach_message);
    let sendBtn;
    if (sent) sendBtn = `<span class="sent-tag">✓ Gmail sent</span>`;
    else if (!GMAIL_AVAIL) sendBtn = `<button disabled title="Set GMAIL_ADDRESS + GMAIL_APP_PASSWORD">Send email</button>`;
    else sendBtn = `<button class="send" onclick="sendEmail('${esc(c.id)}', ${c.email_status==='verified'}, this)">Send email</button>`;
    // A real button, in the row with the other actions. The first version was a <span> beside
    // the EMAIL label: it looked exactly like a control, and the first thing anyone did was
    // click it and report that it was broken. Rendering something button-shaped that is not a
    // button is a worse §Lessons 43 than hiding it — a hidden control is merely missing, a fake
    // one is a promise the page does not keep.
    //
    // It is GLOBAL and the label says so, because it lives on one person's card and changes
    // every email. "Docs ON/OFF · all emails" is the shortest form of that which still fits.
    const attachBtn = sent ? '' : `<button class="attach-btn${ATTACH_DOCS ? '' : ' attach-off'}"
        onclick="toggleAttachDocs(this)"
        title="Attach the tailored résumé + cover letter to the FIRST email of every outreach thread. This is a global setting, not per contact. Follow-ups never attach.">
        📎 ${ATTACH_DOCS ? 'Docs ON' : 'Docs OFF'} · all emails</button>`;
    // The sent copy is the history above; repeating it as a disabled form is the thing that
    // read as "here is what to send".
    const echo = sent && historyShown;
    emailHtml = `
      ${echo ? '' : `<div class="d-label">Email</div>
      <input class="d-subj" value="${subj}" placeholder="Subject…" ${sent?'disabled':''} />
      <textarea class="d-body" rows="4" ${sent?'disabled':''} placeholder="${has ? '' : 'No draft yet — click Regenerate'}">${body}</textarea>`}
      ${(sent || echo)?'':`<input class="d-style" placeholder="✨ Tweak the vibe, then Regenerate — e.g. 'more casual', 'add a joke'">`}
      <div class="dbtns">
        ${sent?'':`<button onclick="saveDraft('${esc(c.id)}', this)">Save</button>
        <button class="secondary" onclick="regenDraft('${esc(c.id)}', this)">Regenerate</button>`}
        ${echo ? '' : `<button onclick="copyDraft(this)">Copy email</button>`}
        ${attachBtn}
        ${sendBtn}
        ${followupButton(c)}
      </div>`;
  }

  // --- LinkedIn section (only when there's a profile) ---
  let liHtml = '';
  if (hasLi) {
    const note = esc(c.linkedin_message);
    const noteLen = (c.linkedin_message || '').length;
    const overClass = noteLen > 300 ? 'over' : '';
    const regenNote = hasEmail ? '' : `<button class="secondary" onclick="regenDraft('${esc(c.id)}', this)">Regenerate</button>`;
    liHtml = `
      <div class="d-label">LinkedIn note <span class="d-count ${overClass}"><span class="lcount">${noteLen}</span>/300</span></div>
      <textarea class="d-linkedin" rows="3" oninput="updCount(this)" placeholder="Short connection note (≤300 chars)">${note}</textarea>
      <div class="dbtns">
        <button onclick="saveLinkedin('${esc(c.id)}', this)">Save note</button>
        <button onclick="copyLinkedin(this)">Copy note</button>
        ${regenNote}
        ${dmButton(c)}
      </div>`;
  }

  return `<div class="draft" data-cid="${esc(c.id)}">${emailHtml}${liHtml}</div>`;
}
// Only offered once a follow-up is actually owed — an email that went out an hour ago
// shouldn't show a follow-up button, and one already logged shows its state instead.
function followupButton(c) {
  if (!c.emailed) return '';
  if (c.followed_up_at) return `<span class="sent-tag">✓ followed up</span>`;
  if (!c.followup_due) return '';
  return `<button class="secondary fu" onclick="markFollowedUp('${esc(c.id)}', this)" title="Record that you sent a follow-up — clears this job's follow-up step">↻ Mark followed up</button>`;
}
async function markFollowedUp(cid, btn) {
  btn.disabled = true;
  const r = await post('/api/contact/followup', {contact_id: cid});
  if (r.ok) { btn.textContent = 'Recorded ✓'; setTimeout(refresh, 700); }
  else { btn.disabled = false; alert(r.message || 'Could not record'); }
}
function dmButton(c) {
  if (!c.linkedin_url || !c.linkedin_message)
    return `<button disabled title="Needs a LinkedIn URL and a drafted note">Copy note + open LinkedIn</button>`;
  // Already recorded as connected — show the state instead of offering it again.
  if (c.dm_status === 'sent' || c.dm_status === 'manual')
    return `<span class="sent-tag">✓ connected on LinkedIn</span>`;
  const url = encodeURIComponent(c.linkedin_url);
  return `<button class="send" onclick="copyAndOpenLinkedin('${url}', this)" title="Copies your note and opens their profile — then Connect ▸ Add a note ▸ paste ▸ Send">Copy note + open LinkedIn</button>`
       + `<button class="secondary" onclick="markConnected('${esc(c.id)}', this)" title="Record that you sent the invite — logs it to the job's activity and stops it re-appearing in the queue">✓ I sent it</button>`;
}
async function markConnected(cid, btn) {
  btn.disabled = true;
  const r = await post('/api/contact/dm-status', {contact_id: cid, status: 'manual'});
  if (r.ok) { btn.textContent = 'Recorded ✓'; setTimeout(refresh, 700); }
  else { btn.disabled = false; alert(r.error || 'Could not record'); }
}
function copyAndOpenLinkedin(encUrl, btn) {
  // Reliable + zero-risk: copy the (possibly edited) note, open the profile in a new tab.
  // You then do Connect ▸ Add a note ▸ paste (Cmd+V) ▸ Send yourself.
  const d = btn.closest('.draft');
  const note = d ? d.querySelector('.d-linkedin').value : '';
  if (note) { try { navigator.clipboard.writeText(note); } catch { /* clipboard denied — the note stays on screen to copy by hand */ } }
  window.open(decodeURIComponent(encUrl), '_blank', 'noopener');
  btn.textContent = 'Copied ✓ — Connect ▸ Add a note ▸ paste';
  setTimeout(()=>btn.textContent='Copy note + open LinkedIn', 3500);
}
function updCount(ta) {
  const wrap = ta.closest('.draft');
  const el = wrap.querySelector('.lcount');
  const badge = wrap.querySelector('.d-count');
  if (el) { el.textContent = ta.value.length; badge.classList.toggle('over', ta.value.length > 300); }
}
// Read a field that may not be on THIS tab. `draftBlock(c, true)` emits no `.d-linkedin` and
// `draftBlock(c, false, true)` emits no `.d-subj`/`.d-body`, so a bare `.value` throws — and an
// exception inside an onclick is swallowed by the browser, so the POST never fires and the
// button just sits there. Both Save buttons were dead this way. `regenDraft` was hardened
// against exactly this and the two save paths were missed.
function fieldVal(d, sel) {
  const el = d ? d.querySelector(sel) : null;
  return el ? el.value : undefined;      // undefined, not '' — the server must not be told to
}                                        // blank a field that this tab never showed.

async function saveLinkedin(cid, btn) {
  const d = btn.closest('.draft');
  const r = await post('/api/outreach', {contact_id: cid,
    subject: fieldVal(d, '.d-subj'), body: fieldVal(d, '.d-body'),
    linkedin: fieldVal(d, '.d-linkedin')});
  btn.textContent = r && r.ok === false ? 'Failed' : 'Saved ✓';
  setTimeout(()=>btn.textContent='Save note', 1200);
}
function copyLinkedin(btn) {
  const d = btn.closest('.draft');
  navigator.clipboard.writeText(d.querySelector('.d-linkedin').value);
  btn.textContent = 'Copied ✓'; setTimeout(()=>btn.textContent='Copy note', 1200);
}
async function sendEmail(cid, verified, btn) {
  const first = verified
    ? 'Send this outreach email now?'
    : '⚠ This email address is UNVERIFIED — it may bounce. Send anyway?';
  if (!confirm(first)) return;
  // Save what is ON SCREEN first. The server sends its STORED copy, and `.d-subj`/`.d-body` are
  // backed by no Map, so an edit you typed and did not explicitly Save was sent as the old text
  // and then erased by the next refresh — wrong twice, and silently. `fuAct` has always done
  // this for follow-ups; the cold-email path never did.
  const card = btn.closest('.draft');
  if (card) {
    const subject = fieldVal(card, '.d-subj'), body = fieldVal(card, '.d-body');
    if (subject !== undefined || body !== undefined)
      await post('/api/outreach', {contact_id: cid, subject, body});
  }
  btn.disabled = true; btn.textContent = 'Sending…';
  const r = await post('/api/outreach/send', {contact_id: cid, confirm_unverified: !verified});
  if (r.ok) { refresh(); }
  else { btn.disabled = false; btn.textContent = 'Send email'; alert(r.message || 'Send failed'); }
}
// ── Text (iMessage/SMS) ─────────────────────────────────────────────────────
//
// Copy → open Messages → you paste → "✓ I sent it". The same shape as LinkedIn and for the
// same reason: Apple exposes no send API, and driving a messaging app from outside is the
// mistake this codebase already made twice (§Lessons 3). Nothing here auto-sends.
//
// The number is entered by hand (Apollo will not release a direct dial to a local tool), so
// the notes block stays on this tab — enter the number, then write the text, in one place.
const SMS_LIMIT = 320;

//: `sms:` wants digits and a leading +, not the pretty form the operator pasted.
function smsHref(phone) {
  const clean = String(phone || '').replace(/[^\d+]/g, '');
  return 'sms:' + clean;
}

function smsChannel(c) {
  const phone = (c.phone || '').trim();
  // No number: render the composer anyway, DISABLED. The first version returned a one-line
  // "add a number below" and the notes block, which was accurate and still read as "this tab
  // is empty" — reported twice as "I'm not seeing the text UI" while looking straight at it.
  // A disabled control shows what the channel does and that it is one step away; a sentence
  // describing a control you cannot see does not.
  const off = phone ? '' : ' disabled';
  const draft = c.sms_followup_message || '';
  const len = draft.length;
  const started = !!c.sms_sent_at;
  const touch = (c.sms_followup_count || 0) + 1;
  const total = c.sms_followup_total || 2;
  const st = c.sms_followup_state || '';

  // Once the first text is recorded this is a LADDER, so say where you are in it. Before that
  // it is just the first message and a touch count would be noise.
  let ladder = '';
  if (started) {
    const when = String(c.sms_sent_at).slice(0, 10);
    // The acknowledgement has to be LOUD, because it is the only thing that tells the operator
    // their click landed — the button that recorded it disappears, and "first text 13 Aug" in
    // grey is what made a working feature read as a dead one. `.sent-tag` is the same green
    // affirmative the email side uses when a send really happened.
    ladder = st === 'replied' ? `<span class="sent-tag">✓ replied — sequence stopped</span>`
           : st === 'stopped' ? `<span class="muted">sequence stopped</span>`
           : st === 'finished' ? `<span class="sent-tag">✓ all ${total} texts sent</span>`
           : st === 'due' ? `<span class="fu-due">↻ follow-up ${touch} of ${total} due</span>`
           : st === 'waiting' && c.sms_followup_due_in_h != null
             ? `<span class="sent-tag">✓ texted ${esc(when)}</span>`
               + `<span class="muted">next in ${Math.max(1, Math.round(c.sms_followup_due_in_h / 24))}d</span>`
             : `<span class="sent-tag">✓ texted ${esc(when)}</span>`;
  }

  // The compose control is an <a> when it can work and a disabled <button> when it cannot —
  // an anchor has no disabled attribute, and a greyed-out link that still navigates is worse
  // than no link.
  const openBtn = phone
    ? `<a class="btn-like send" href="${esc(smsHref(phone))}" onclick="copySmsFirst(this)"
         title="Copies the text and opens Messages — then paste and send. Nothing sends itself.">Copy &amp; open Messages ↗</a>`
    : `<button class="send" disabled title="Add a phone number below first">Copy &amp; open Messages ↗</button>`;

  return `<div class="draft">
      <div class="d-label">Text message
        <span class="d-count ${len > SMS_LIMIT ? 'over' : ''}"><span class="smscount">${len}</span>/${SMS_LIMIT}</span>
        <span class="sms-to">${phone ? 'to ' + esc(phone) : '— no number yet'}</span>
        ${ladder}
      </div>
      ${phone ? '' : `<div class="sms-locked">Add a phone number below and Save — then this
        composer turns on. Apollo won't release direct dials to a local tool, so it is pasted
        by hand.</div>`}
      <textarea class="d-sms" rows="3" oninput="updSmsCount(this)"${off}
        placeholder="${phone ? (draft ? '' : 'No draft yet — click Regenerate')
                             : 'Your text to ' + esc(c.full_name) + ' appears here once they have a number.'}">${esc(draft)}</textarea>
      <input class="d-style"${off} placeholder="✨ Tweak the vibe, then Regenerate — e.g. 'we met at the AITX hackathon'">
      <div class="dbtns">
        <button onclick="saveSms('${esc(c.id)}', this)"${off}>Save</button>
        <button class="secondary" onclick="regenSms('${esc(c.id)}', this)"${off}>Regenerate</button>
        ${openBtn}
        ${smsSentButton(c, !phone)}
      </div>
      <div class="sms-hint">Written for a phone: no links (a URL from an unknown number is the
        strongest spam signal there is) and it says who you are, because they do not have your
        number saved.</div>
    </div>` + contactNotes(c);
}

// The phone CALL pane. The only channel here with nothing to write — so there is no composer,
// no draft, no Regenerate, and nothing that could ever be sent automatically. What it holds is
// the two things a call actually needs: the number, dialable, and the record that it happened.
//
// It renders whether or not there is a number, for the same reason Text always did: the empty
// pane is where a number gets ENTERED, and hiding a channel that has no identifier removes the
// dead end together with the only place the identifier could ever be supplied (§Lessons 99).
function callChannel(c) {
  const phone = (c.phone || '').trim();
  const made = !!c.call_made_at;
  const st = c.call_followup_state || '';
  const total = c.call_followup_total || 2;
  let mark = '';
  if (made) {
    const when = String(c.call_made_at).slice(0, 10);
    mark = st === 'replied' ? `<span class="sent-tag">✓ replied — sequence stopped</span>`
         : st === 'stopped' ? `<span class="muted">sequence stopped</span>`
         : st === 'finished' ? `<span class="sent-tag">✓ all ${total} calls made</span>`
         : st === 'due' ? `<span class="fu-due">↻ second call due</span>`
         : st === 'waiting' && c.call_followup_due_in_h != null
           ? `<span class="sent-tag">✓ called ${esc(when)}</span>`
             + `<span class="muted">call again in ${Math.max(1, Math.round(c.call_followup_due_in_h / 24))}d</span>`
           : `<span class="sent-tag">✓ called ${esc(when)}</span>`;
  }
  // Offered only when one is genuinely owed — the same rule the text button needed. Recording a
  // call nobody made is worse here than anywhere else: it is asserted by the operator, so
  // nothing downstream can ever contradict it.
  const btn = !made
    ? `<button class="secondary" onclick="fuAct('${esc(c.id)}','call_connected',this)"${phone ? '' : ' disabled'}
        title="Record that you spoke to them or left a message — starts the 3-day clock for the second call">✓ I called</button>`
    : (st === 'due'
        ? `<button class="secondary" onclick="fuAct('${esc(c.id)}','call_sent',this)"
            title="Record the second call">✓ I called again</button>` : '');
  return `<div class="draft">
      <div class="d-label">Phone call
        <span class="sms-to">${phone ? 'to ' + esc(phone) : '— no number yet'}</span>
        ${mark}
      </div>
      ${phone
        ? `<div class="call-num"><a class="btn-like send" href="tel:${esc(phone.replace(/[^+\d]/g, ''))}">📞 Call ${esc(phone)}</a></div>`
        : `<div class="sms-locked">Add a phone number below and Save — then this turns on.
           Apollo won't release direct dials to a local tool, so it is pasted by hand.</div>`}
      <div class="dbtns">${btn}</div>
      <div class="sms-hint">Nothing here dials for you and nothing is recorded automatically —
        a call happens away from this machine, so "✓ I called" is the only evidence there is.
        Leaving a voicemail counts.</div>
    </div>` + contactNotes(c);
}

// "I sent it" means two different things depending on where you are, and conflating them is
// how a ladder loses its anchor: the FIRST text stamps sms_sent_at and starts the clock, and
// every later one is a touch. Both are operator-asserted — nothing can watch Messages.app.
function smsSentButton(c, off) {
  const d = off ? ' disabled' : '';
  if (!c.sms_sent_at)
    return `<button class="secondary" onclick="fuAct('${esc(c.id)}','sms_connected',this)"${d}
      title="Record that you sent the first text — starts the follow-up clock">✓ I sent it</button>`;
  const st = c.sms_followup_state || '';
  if (st === 'replied' || st === 'stopped' || st === 'finished') return '';
  // Reported as "the I sent it button on text messages does not do anything". It did: the
  // server stamps `sms_sent_at` and five live contacts carry one, two of them recorded minutes
  // before the report. What did nothing was the SCREEN — the button re-rendered identical to
  // itself, because the follow-up variant was offered the instant the first text was recorded.
  // Click, "Done ✓", refresh, same button. §Lessons 43's fourth form: the feature works
  // perfectly and the result is imperceptible.
  //
  // Worse than cosmetic. Offered while the ladder is still WAITING, a second click records a
  // touch for a text nobody sent, moving the whole cadence forward on operator-asserted
  // evidence that is simply wrong. It is offered only when a follow-up is genuinely due.
  if (st !== 'due') return '';
  return `<button class="secondary" onclick="fuAct('${esc(c.id)}','sms_sent',this)"${d}
    title="Record that you sent this follow-up text">✓ I sent it</button>`;
}

function updSmsCount(ta) {
  const wrap = ta.closest('.draft');
  const el = wrap.querySelector('.smscount');
  const badge = wrap.querySelector('.d-count');
  if (el) { el.textContent = ta.value.length; badge.classList.toggle('over', ta.value.length > SMS_LIMIT); }
}

// Copy, then let the browser follow the sms: href natively. Assigning location.href for a
// custom scheme is unreliable and window.open gets popup-blocked; a real <a> is the one that
// works. The copy has to happen synchronously inside the handler or the clipboard write is
// dropped as untrusted once navigation starts.
function copySmsFirst(a) {
  const d = a.closest('.draft');
  const ta = d ? d.querySelector('.d-sms') : null;
  if (ta) navigator.clipboard.writeText(ta.value);
  a.textContent = 'Copied ✓ — paste in Messages';
  setTimeout(() => { a.innerHTML = 'Copy &amp; open Messages ↗'; }, 2500);
}

async function saveSms(cid, btn) {
  const d = btn.closest('.draft');
  const r = await post('/api/followup', {contact_id: cid, action: 'sms_save',
    subject: '', body: fieldVal(d, '.d-sms')});
  btn.textContent = r && r.ok === false ? 'Failed' : 'Saved ✓';
  setTimeout(() => btn.textContent = 'Save', 1200);
}

async function regenSms(cid, btn) {
  btn.disabled = true; btn.textContent = 'Writing…';
  const d = btn.closest('.draft');
  const r = await post('/api/followup', {contact_id: cid, action: 'sms_draft',
    style: fieldVal(d, '.d-style')});
  btn.disabled = false; btn.textContent = 'Regenerate';
  if (r && r.ok === false) { alert(r.message || 'Could not draft that text.'); return; }
  refresh();
}

//: The channel has no identifier — so this pane IS the box that gives it one.
//:
//: Modelled on the SMS composer's empty state, and on what that one cost: it shipped as the
//: accurate sentence "No phone number for Blake — add one below", and was reported TWICE as "I
//: am not seeing the text UI" by someone looking straight at it. A pane that describes an
//: absence reads as an empty tab (§Lessons 41). This one says what the channel does, shows the
//: field, and is one click from working.
//:
//: The input lives on the CHANNEL rather than in one shared details box, so each identifier is
//: entered where the thing it enables is — and there is never a second copy of the same field
//: on screen going stale against the first (§Lessons 89).
// `noun` is written out rather than lower-cased from a label: `'LinkedIn profile'.toLowerCase()`
// is "linkedin profile", and a brand name in the wrong case is the tell that a machine wrote the
// sentence — the same class of thing as the em dash.
const ADD_COPY = {
  email: {
    noun: 'email address',
    ph: 'name@company.com',
    does: 'An address turns on the cold email, the follow-up ladder and reply detection — it is the only channel that sends by itself.',
    how: 'Apollo ↗ has it if they are in it; otherwise the company\'s own site, a signature, or the pattern their colleagues use.',
  },
  linkedin: {
    noun: 'LinkedIn profile',
    ph: 'linkedin.com/in/their-handle — or just their-handle',
    does: 'A profile turns on the connection invitation, and gives you something to read before writing to them.',
    how: 'Open their profile and copy the address bar. A bare handle works too.',
  },
};
function addIdentifier(c, kind) {
  const t = ADD_COPY[kind];
  const field = kind === 'email' ? 'email' : 'linkedin_url';
  return `<div class="draft add-id" data-cid="${esc(c.id)}" data-field="${field}">
      <div class="d-label">Add ${t.noun === 'email address' ? 'an' : 'a'} ${esc(t.noun)} for ${esc(c.full_name)}</div>
      <div class="add-does">${esc(t.does)}</div>
      <input class="c-add" placeholder="${esc(t.ph)}" onkeydown="onAddIdKey(event)" />
      <div class="dbtns">
        <button class="primary add-save" onclick="saveIdentifier(this)">Save</button>
        ${kind === 'email' && c.apollo_url
          ? `<button class="secondary" onclick="window.open('${esc(c.apollo_url)}','_blank','noopener')">Open Apollo ↗</button>` : ''}
        ${kind === 'linkedin' && c.apollo_search_url
          ? `<button class="secondary" onclick="window.open('${esc(c.apollo_search_url)}','_blank','noopener')">Search Apollo ↗</button>` : ''}
      </div>
      <div class="add-msg"></div>
      <div class="sms-hint">${esc(t.how)}</div>
    </div>`;
}

//: Enter saves. A one-field form where Enter does nothing is a form people retype into.
//: A named function rather than an inline statement: `test_every_inline_handler_resolves_at_
//: global_scope` reads `typeof <handler>` off every `on*=` attribute, and a multi-statement
//: body is not an expression — it fails the probe rather than the page.
function onAddIdKey(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const btn = e.target.closest('.add-id').querySelector('.add-save');
  if (btn) btn.click();
}

//: One writer for both fields. The server refuses an address that cannot be one, and the
//: refusal is SHOWN — a box that clears itself on save is an edit the operator watched succeed
//: and which never happened (§Lessons 75).
async function saveIdentifier(btn) {
  const box = btn.closest('.add-id');
  const msg = box.querySelector('.add-msg');
  const value = (box.querySelector('.c-add').value || '').trim();
  if (!value) { msg.textContent = 'Type it in first.'; return; }
  btn.disabled = true;
  const r = await post('/api/contact/details',
    {contact_id: box.getAttribute('data-cid'), [box.getAttribute('data-field')]: value});
  btn.disabled = false;
  if (!r.ok) { msg.textContent = r.message || 'Could not save that.'; return; }
  msg.textContent = '';
  // The refresh re-renders this contact with the identifier present, so the pane becomes the
  // real channel. Clearing the stored tab first would bounce them somewhere else at the moment
  // it starts working.
  refresh();
}

function contactNotes(c) {
  // Apollo will not hand a direct dial to a local tool (reveal_phone_number is
  // webhook-only), so the number is copied out of the Apollo UI by hand and kept here.
  //
  // It defaults OPEN when there is NO number, which is the inverse of what it used to do.
  // The composer directly above says "Add a phone number below and Save" and is DISABLED
  // until one exists — so the only case that needs this block is the one case it was
  // collapsed for, while everyone who already had a number got it expanded. Reported as
  // "the text feature is not working", which is exactly what a disabled control plus a
  // hidden way to enable it is (§Lessons 43, 88, 89: findable is not the same as findable
  // FROM WHERE THE WORK IS, and the operator saying it does not work is the measurement).
  const wants = !c.phone || c.notes;
  const open = (NOTES_OPEN.has(c.id) || (wants && !NOTES_CLOSED.has(c.id))) ? ' open' : '';
  const key = encodeURIComponent(c.id);
  return `
    <details class="cnotes"${open} ontoggle="onNotesToggle(this, decodeURIComponent('${key}'))">
      <summary>📇 Phone &amp; notes${c.phone ? '' : (c.apollo_url ? ' — no number yet' : '')}</summary>
      <div class="cnote-body" data-cid="${esc(c.id)}">
        ${c.phone ? '' : `<div class="c-howto">Apollo won't release direct dials to a local tool, so this one is manual:
          open <b>Apollo ↗</b> → click <b>Access direct dial</b> on their profile (spends a phone credit) → paste it here.
          Only some people have one; many show just the company switchboard.</div>`}
        <input class="c-phone" value="${esc(c.phone)}" placeholder="+1 555 123 4567 — paste from Apollo" />
        <textarea class="c-notes" rows="2" placeholder="Notes — call outcome, best time to reach, referral…">${esc(c.notes)}</textarea>
        <div class="dbtns">
          <button onclick="saveContactDetails('${esc(c.id)}', this)">Save</button>
          ${c.apollo_url ? `<button class="secondary" onclick="window.open('${esc(c.apollo_url)}','_blank','noopener')">Open Apollo ↗</button>` : ''}
        </div>
      </div>
    </details>`;
}
async function saveContactDetails(cid, btn) {
  const b = btn.closest('.cnote-body');
  const r = await post('/api/contact/details', {contact_id: cid,
    phone: b.querySelector('.c-phone').value, notes: b.querySelector('.c-notes').value});
  btn.textContent = r.ok ? 'Saved ✓' : 'Failed';
  setTimeout(()=>{ btn.textContent='Save'; if (r.ok) refresh(); }, 900);
}
// ── People: one line each until you open one ────────────────────────────────
const CONTACT_OPEN = new Set();
const CHANNEL_TAB = new Map();
function toggleContact(cid) {
  if (CONTACT_OPEN.has(cid)) CONTACT_OPEN.delete(cid);
  else { CONTACT_OPEN.add(cid); autoSyncGmail(cid); }
  refresh();
}
function setChannel(cid, ch) {
  CHANNEL_TAB.set(cid, ch);
  if (!CONTACT_OPEN.has(cid)) autoSyncGmail(cid);
  CONTACT_OPEN.add(cid);
  refresh();
}

// Opening a card pulls the latest Gmail for that person, once.
//
// Asked for as: "every time I click on a contact card it autofetches the latest gmail
// conversation... I keep having to click fetch to make sure I got the latest email I sent, this
// is important for context for the next email." Correct — the poller only covers contacts in
// play and runs every five minutes, so an email sent by hand from Gmail two minutes ago is
// simply not on the card yet, and the next draft is written without it.
//
// Three things make this safe to do automatically, and all three are deliberate:
//
//   * it is hooked to the CLICK, never to a render. `#jobs` is rebuilt every 2.5s, so a fetch
//     on the render path would be a Gmail round-trip every 2.5 seconds per open card —
//     §Lessons 26, where one HTTP call per job took /api/status from 0.04s to 2.4s.
//   * once per contact per SYNC_TTL, tracked outside the DOM like every other open-state set.
//     Re-opening a card you looked at ten seconds ago costs nothing.
//   * it stores HEADERS and Gmail's own snippet — exactly what the five-minute poller already
//     stores for contacts in play. The documented narrowing is untouched: no message BODY is
//     ever read automatically, and ⤓ Fetch from Gmail on one thread stays a deliberate click.
const SYNC_AT = new Map();          // contact id -> ms, so re-opening a card is free
const SYNC_TTL = 60000;

async function autoSyncGmail(cid) {
  if (!CONTENT_SCOPE) return;                       // no gmail.readonly: nothing to search with
  const c = (LAST_JOBS || []).flatMap(j => j.contacts || []).find(x => x.id === cid);
  if (!c || !c.email) return;                       // the search is BY ADDRESS
  const last = SYNC_AT.get(cid) || 0;
  if (Date.now() - last < SYNC_TTL) return;
  SYNC_AT.set(cid, Date.now());
  SYNC_MSG.set(cid, {text: 'checking Gmail…', bad: false});
  const r = await post('/api/contact/sync-gmail', {contact_id: cid});
  // Silent on success with nothing new: an "up to date" note on every card you open is noise
  // within a day, and a message that is always there is one nobody reads (CRM-3a's rule).
  // A real change, and every failure, still says so.
  //
  // Keyed on the COUNT the response already carries, never on its prose. `messages` is the
  // number of new rows; matching a sentence would break the moment the wording changes, and
  // silently — the note would simply start appearing on every card.
  if (r.ok && !r.messages) SYNC_MSG.delete(cid);
  else SYNC_MSG.set(cid, {text: r.message || (r.ok ? 'Synced.' : 'Could not reach Gmail.'),
                          bad: !r.ok});
  setTimeout(() => { SYNC_MSG.delete(cid); refresh(); }, 12000);
  refresh();
}
// CRM-4. Someone the OTHER side added to a thread — a recruiter looping in a hiring manager
// is the single most valuable event in a job-search conversation, and a boolean `replied` threw
// it away. Surfaced as an offer, never auto-created: a contact added here is one an automated
// follow-up ladder would then email, and threads collect schedulers and ATS robots.
//: How many handoff banners render before they collapse into one line.
//:
//: A busy thread introduces everybody. One real card produced ELEVEN stacked banners above the
//: People tab, which pushed the conversation the operator had just clicked "Fetch from Gmail" to
//: read completely off screen — reported as "this gave me this view instead of all my threads".
//: Two is enough to notice; the rest are a list you open when you want it.
const INTRO_SHOWN = 2;
const INTRO_OPEN = new Set();
function toggleIntros(url) {
  if (INTRO_OPEN.has(url)) INTRO_OPEN.delete(url); else INTRO_OPEN.add(url);
  rerenderJobs(true);
}

function introBanner(j) {
  const all = j.introductions || [];
  if (!all.length) return '';
  const open = INTRO_OPEN.has(j.url);
  const intros = open ? all : all.slice(0, INTRO_SHOWN);
  const hidden = all.length - intros.length;
  const more = (hidden > 0 || open)
    ? `<button class="linklike intro-more" onclick="toggleIntros(${
        `decodeURIComponent('${encodeURIComponent(j.url)}')`})">${
        open ? 'Show fewer' : `${hidden} more ${hidden === 1 ? 'person was' : 'people were'} added — show`}</button>`
    : '';
  return intros.map(i => {
    const args = [i.email, i.name || '', i.introduced_by || ''].map(v => `decodeURIComponent('${encodeURIComponent(v)}')`).join(', ');
    const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
    return `<div class="intro-bar">
      <span>👋 <strong>${esc(i.introduced_by || 'Someone')}</strong> added <strong>${esc(i.name || i.email)}</strong> (${esc(i.email)}) to the thread — they may be handling this now.</span>
      <button class="primary" onclick="addIntroduced(${u}, ${args}, this)">+ Add as contact</button>
    </div>`;
  }).join('') + more;
}
async function addIntroduced(url, email, name, by, btn) {
  btn.disabled = true; btn.textContent = 'Adding…';
  // `on_thread` is what earns this address `email_status: 'verified'`. It came off a Cc on a
  // live thread, so it is real by construction — which a hand-typed one is not.
  const r = await post('/api/contact/add-introduced',
                       {job_url: url, email, name, introduced_by: by, on_thread: 1});
  const cmdEl = document.getElementById('command');
  if (cmdEl) cmdEl.textContent = r.message || '';
  // #command sits above the whole jobs table, often screens away from the banner that was
  // clicked. Success is self-evident (the person appears in the list); a FAILURE that only
  // shows up there reads as a button that did nothing.
  if (!r.ok) {
    alert(r.message || 'Could not add them as a contact.');
    btn.disabled = false; btn.textContent = '+ Add as contact';
  }
  refresh();
}
// Somebody hands you a name. `introBanner` above only fires on a Cc DETECTED in a live Gmail
// thread, so "you should talk to Priya, here's her LinkedIn" — said on a call, in a LinkedIn
// DM, or in a reply that names her without copying her — had nowhere to go at all. That is the
// warmest lead this system can produce and the only way to record it was to go and look the
// person up in Apollo. §Lessons 37: the tool has to be reachable from the state it repairs.
//
// The typed values live HERE and not in the DOM. `refresh()` replaces #jobs wholesale every
// 2.5s and only holds off while a field HAS focus (`isEditingJobs`), so moving between fields
// opens a window where the tick lands between blur and focus and takes the half-filled form
// with it. Same reason PANEL_OPEN exists, one layer down.
const ADD_FORM = new Map();
function addState(url) {
  if (!ADD_FORM.has(url)) {
    ADD_FORM.set(url, {open: false, name: '', email: '', linkedin: '', by: '', err: ''});
  }
  return ADD_FORM.get(url);
}
function toggleAddContact(url) { const s = addState(url); s.open = !s.open; s.err = ''; refresh(); }
function onAddField(url, field, value) { addState(url)[field] = value; }
async function submitAddContact(url, btn) {
  const s = addState(url);
  btn.disabled = true; btn.textContent = 'Adding…';
  const r = await post('/api/contact/add-introduced', {job_url: url, name: s.name,
    email: s.email, linkedin_url: s.linkedin, introduced_by: s.by});
  const cmdEl = document.getElementById('command');
  if (cmdEl) cmdEl.textContent = r.message || '';
  // The failure has to land IN the form. #command sits above the whole table, often screens
  // away from the button that was clicked, and a rejection that only shows up there reads as a
  // button that did nothing — which is exactly what addIntroduced learned the hard way.
  if (r.ok) ADD_FORM.delete(url);
  else { s.err = r.message || 'Could not add them.'; btn.disabled = false; btn.textContent = 'Add contact'; }
  refresh();
}
function addContactForm(j) {
  const s = addState(j.url);
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  if (!s.open) {
    return `<div class="addc-row"><button class="ghost addc-open" onclick="toggleAddContact(${u})"
        title="Someone referred you to a person Apollo never found — a name from a reply, a call or a DM">＋ Add someone by hand</button></div>`;
  }
  const f = (k, ph) => `<input class="addc-f" type="text" placeholder="${esc(ph)}" `
      + `value="${esc(s[k])}" oninput="onAddField(${u}, '${k}', this.value)">`;
  return `<div class="addc">
      <div class="addc-head">Someone gave you a name</div>
      <div class="addc-grid">
        ${f('name', 'Name')}
        ${f('email', 'Email')}
        ${f('linkedin', 'LinkedIn profile URL')}
        ${f('by', 'Who referred them?')}
      </div>
      <div class="addc-actions">
        <button class="primary" onclick="submitAddContact(${u}, this)">Add contact</button>
        <button class="ghost" onclick="toggleAddContact(${u})">Cancel</button>
        <span class="addc-hint">${s.err ? `<span class="addc-err">${esc(s.err)}</span>`
          : 'An email or a LinkedIn URL — either one is enough. Naming who referred them makes the draft warmer.'}</span>
      </div>
    </div>`;
}
// ── CO-2: move contacts from a dead role to a live one ──────────────────────
// State lives OUTSIDE the DOM, like PANEL_OPEN and CONV_EXPANDED: `#jobs` is replaced wholesale
// every 2.5s, so anything held in the markup is destroyed mid-decision.
const MIGRATE = new Map();          // job url -> {open, targets, dst, plan, picked, busy, err}
let MIGRATE_UNDO = null;            // {token, moved, title} — one at a time, until the reload

function migState(url) {
  if (!MIGRATE.has(url)) MIGRATE.set(url, {open: false, targets: null, dst: '', plan: null,
                                           picked: null, busy: false, err: ''});
  return MIGRATE.get(url);
}

async function toggleMigrate(url) {
  const s = migState(url);
  s.open = !s.open;
  if (!s.open) { refresh(); return; }
  s.busy = true; s.err = ''; refresh();
  const r = await post('/api/contacts/migrate-plan', {src: url});
  s.targets = r.targets || [];
  s.err = r.error || '';
  // Preselected when there is exactly one — and still NAMED in the dialog. The ticket left this
  // open ("is one target enough?"); showing it costs a line and moving eleven people on an
  // unstated assumption costs the move.
  if (s.targets.length === 1) { await pickMigrateTarget(url, s.targets[0].url); return; }
  s.busy = false; refresh();
}

async function pickMigrateTarget(url, dst) {
  const s = migState(url);
  s.dst = dst; s.busy = true; s.err = ''; s.plan = null; refresh();
  const r = await post('/api/contacts/migrate-plan', {src: url, dst});
  s.targets = r.targets || s.targets;
  s.plan = r.plan || null;
  s.err = (r.plan && !r.plan.ok ? r.plan.error : '') || r.error || '';
  // Everyone movable starts ticked. The excluded are not in this set and cannot be added to it
  // from here — the server re-derives the plan and refuses them anyway.
  s.picked = new Set(((s.plan && s.plan.movable) || []).map(p => p.id));
  s.busy = false; refresh();
}

function toggleMigratePick(url, id) {
  const s = migState(url);
  if (!s.picked) return;
  if (s.picked.has(id)) s.picked.delete(id); else s.picked.add(id);
  refresh();
}

async function runMigrate(url, btn) {
  const s = migState(url);
  const ids = [...(s.picked || [])];
  if (!ids.length) return;
  const p = s.plan || {};
  const names = (p.movable || []).filter(m => s.picked.has(m.id)).map(m => m.full_name || m.email);
  // Named, not counted. Scoped to one move the confirm can list who it reaches, which is the
  // difference between a decision and an "are you sure?" (§Lessons 29).
  if (!confirm(`Move ${ids.length} ${ids.length === 1 ? 'person' : 'people'} to `
      + `“${p.dst_title || 'the other application'}”?\n\n${names.join(', ')}`
      + (p.drafts_cleared ? `\n\n${p.drafts_cleared} unsent draft`
          + `${p.drafts_cleared > 1 ? 's' : ''} naming the old role will be cleared.` : ''))) return;
  s.busy = true; btn.disabled = true; refresh();
  const r = await post('/api/contacts/migrate', {src: url, dst: s.dst, ids});
  s.busy = false;
  if (!r.ok) { s.err = r.error || 'Could not move them.'; refresh(); return; }
  MIGRATE.delete(url);
  // Keyed to the SOURCE job, so the undo renders where the operator is standing rather than in
  // a global console two screens away — the placement mistake the bulk follow-up bar made
  // (§Lessons 89).
  MIGRATE_UNDO = {token: r.undo, moved: r.moved, title: r.dst_title || '',
                  backup: r.backup || '', src: url};
  refresh();
}

async function undoMigrate(btn) {
  if (!MIGRATE_UNDO) return;
  btn.disabled = true; btn.textContent = 'Undoing…';
  const r = await post('/api/contacts/migrate-undo', {token: MIGRATE_UNDO.token});
  if (!r.ok) { btn.disabled = false; btn.textContent = '↩ Undo'; alert(r.error || 'Could not undo.'); return; }
  MIGRATE_UNDO = null;
  refresh();
}
function dismissMigrateUndo() { MIGRATE_UNDO = null; refresh(); }

function migrateBar(j) {
  // Only where the situation exists: a role that has CLOSED, with people on it. A live job's
  // contacts are not stranded and the button would be noise on every card.
  if (!isClosed(j) || !(j.contacts || []).length) return '';
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const s = migState(j.url);
  if (!s.open) {
    return `<div class="mig-row"><button class="ghost mig-open" onclick="toggleMigrate(${u})"
        title="This role is closed. Move the people you were already talking to onto a live application at the same company."
        >→ Move contacts to another application</button></div>`;
  }
  if (s.busy && !s.plan) return `<div class="mig"><div class="mig-head">Working…</div></div>`;

  const targets = s.targets || [];
  let body = '';
  if (!targets.length) {
    // Not a failure and not silence: it says which condition is unmet, because "same employer,
    // still open" is a rule the operator can act on.
    body = `<div class="mig-none">No other open application at ${esc(j.contact_company || j.company || 'this employer')}.
      Import the live posting first, then come back — contacts only move between roles at the same company.</div>`;
  } else {
    const pick = targets.length === 1
      ? `<div class="mig-to">To <strong>${esc(targets[0].title || targets[0].url)}</strong></div>`
      : `<div class="mig-to">To <select onchange="pickMigrateTarget(${u}, this.value)">
           <option value="">Choose an application…</option>
           ${targets.map(t => `<option value="${esc(t.url)}" ${t.url === s.dst ? 'selected' : ''}>${esc(t.title || t.url)}</option>`).join('')}
         </select></div>`;
    body = pick + migratePlanBody(j, s, u);
  }
  return `<div class="mig">
      <div class="mig-head">Move contacts off “${esc(j.title || 'this role')}”
        <button class="ghost mig-x" onclick="toggleMigrate(${u})" title="Close">✕</button></div>
      ${body}
      ${s.err ? `<div class="mig-err">${esc(s.err)}</div>` : ''}
    </div>`;
}

const MIG_GROUPS = [
  ['replied', 'in conversation', 'they answered — the conversation continues on the new card'],
  ['emailed', 'emailed, no reply', 'their ladder resets, so the new role is a genuine first contact'],
  ['fresh',   'never contacted',  'no outreach yet'],
];

function migratePlanBody(j, s, u) {
  const p = s.plan;
  if (!p || !p.ok) return '';
  const picked = s.picked || new Set();
  let out = '';
  for (const [key, label, why] of MIG_GROUPS) {
    const rows = p.groups[key] || [];
    if (!rows.length) continue;
    out += `<div class="mig-g"><div class="mig-g-h">${rows.length} ${esc(label)}
        <span class="mig-g-why">${esc(why)}</span></div>`
      + rows.map(r => `<label class="mig-p"><input type="checkbox" ${picked.has(r.id) ? 'checked' : ''}
          onchange="toggleMigratePick(${u}, '${esc(r.id)}')">
          <span class="mig-n">${esc(r.full_name || r.email)}</span>
          <span class="mig-t">${esc(r.title || '')}</span>
          ${r.messages ? `<span class="mig-m">${r.messages} message${r.messages > 1 ? 's' : ''}</span>` : ''}
        </label>`).join('') + `</div>`;
  }
  // Excluded people are SHOWN with the reason, never hidden. Live this is 5 of 16, and silently
  // dropping five of sixteen reads as a bug (§Lessons 98: a partial result that renders as a
  // complete one is harder to see than a zero).
  if ((p.excluded || []).length) {
    out += `<div class="mig-g out"><div class="mig-g-h">${p.excluded.length} not moving</div>`
      + p.excluded.map(r => `<div class="mig-p out"><span class="mig-n">${esc(r.full_name || r.email || '(no name)')}</span>
          <span class="mig-t">${esc(r.why)}</span></div>`).join('') + `</div>`;
  }
  const warn = [];
  for (const c of (p.collisions || [])) {
    warn.push(`${c.full_name} is on both applications. The row with the conversation is kept`
      + `${c.keeps === 'moved' ? '' : ' — the one already there'}.`);
  }
  for (const r of (p.refused || [])) warn.push(r.why);
  if (p.drafts_cleared) {
    warn.push(`${p.drafts_cleared} unsent draft${p.drafts_cleared > 1 ? 's' : ''} name the old `
      + `role and will be cleared. Nothing already sent is touched.`);
  }
  const n = picked.size;
  return out
    + warn.map(w => `<div class="mig-warn">⚠ ${esc(w)}</div>`).join('')
    + `<div class="mig-actions">
        <button class="primary" onclick="runMigrate(${u}, this)" ${n ? '' : 'disabled'}>
          ${n ? `Move ${n} ${n === 1 ? 'person' : 'people'}` : 'Nobody selected'}</button>
        <span class="mig-hint">Reversible — an ↩ Undo appears after the move, and the database is
          backed up first.</span>
      </div>`;
}

function migrateUndoBar(j) {
  if (!MIGRATE_UNDO || MIGRATE_UNDO.src !== j.url) return '';
  const m = MIGRATE_UNDO;
  return `<div class="mig-undo">✓ Moved ${m.moved} ${m.moved === 1 ? 'person' : 'people'}
      ${m.title ? `to “${esc(m.title)}”` : ''}
      <button class="ghost" onclick="undoMigrate(this)">↩ Undo</button>
      <button class="ghost mig-x" onclick="dismissMigrateUndo()" title="Dismiss">✕</button>
      ${m.backup ? `<span class="mig-hint">backup: ${esc(m.backup)}</span>` : ''}</div>`;
}

function peopleList(j) {
  const cs = j.contacts || [];
  let intro = introBanner(j) + migrateUndoBar(j);
  if (!cs.length) {
    // The undo has to render here too: moving EVERYONE lands on this branch, which is exactly
    // the moment the operator is most likely to want the move back.
    return intro + `<div class="pane-empty">No contacts yet. ${findContactsPrompt(j)}</div>`
         + addContactForm(j);
  }
  // CO-2 sits at the TOP of the closed job's People tab, above the people it moves. The row
  // menu was the obvious home and is the wrong one: this is not destructive, and burying a
  // control is how the interview button was reported as doing nothing (§Lessons 43). The test
  // for placement is not "can it be reached" but "is it on the thing it acts on" (§Lessons 97).
  intro += migrateBar(j) + anotherRoundPrompt(j, cs) + addContactForm(j);
  // 💡 outranks the hot/cold split and is pulled OUT of both groups rather than sorted to the
  // front of its own. Every other grouping here is derived — `hot` means "you already know
  // them", which the system worked out — and this is the one the operator DECIDED, so it wins.
  // Sorting within a group would leave a flagged cold contact below fifteen hot ones, which is
  // not "the top" by any reading of it.
  //
  // It is a GROUP, not a silent reordering, for the same reason the other two are: a row that
  // moved for an unstated reason reads as a bug. The header names the reason and carries the
  // count, and pulling flagged people out keeps the other two counts honest — they say what is
  // rendered beneath them, not what would have been.
  const flagged = cs.filter(c => c.flagged);
  const rest = cs.filter(c => !c.flagged);
  const hot = rest.filter(c => c.hot), cold = rest.filter(c => !c.hot);
  let out = intro + bulkBar(j);
  if (flagged.length) out += `<div class="ppl-group flagged">💡 Highlighted <span class="ppl-g-n">${flagged.length}</span></div>` + flagged.map(c => contactRow(c)).join('');
  if (hot.length)  out += `<div class="ppl-group hot">🔥 People you know here <span class="ppl-g-n">${hot.length}</span></div>` + hot.map(c => contactRow(c)).join('');
  if (cold.length) out += `<div class="ppl-group cold">🧊 New contacts <span class="ppl-g-n">${cold.length}</span></div>` + cold.map(c => contactRow(c)).join('');
  return `<div class="plist">${out}</div>`;
}
// "Jul 28" from an ISO timestamp, without dragging in a formatter.
function shortDate(iso) {
  try {
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleDateString([], {month:'short', day:'numeric'});
  } catch { return ''; }
}
function contactRow(c) {
  const open = CONTACT_OPEN.has(c.id);
  const pills = [];
  pills.push(c.emailed ? `<span class="pill on">✉ sent</span>`
    : (c.email ? `<span class="pill off">✉ draft</span>` : `<span class="pill off">✉ no email</span>`));
  pills.push((c.dm_status === 'sent' || c.dm_status === 'manual') ? `<span class="pill on">🔗 connected</span>`
    : (c.linkedin_url ? `<span class="pill off">🔗 —</span>` : ''));
  // `replied_at` is authoritative (CRM-1 writes it); `followup_status` is the ARCH-3 legacy
  // shim and stays as a fallback for rows recorded before reply detection existed.
  // "They are waiting on you" outranks "they replied": both are true, but only one is a job.
  // Shown on the COLLAPSED row, because a state you must expand a contact to discover is a
  // state that goes unnoticed for days — which is the failure this whole ticket is about.
  // THREE independent facts, three independent groups. They were one else-if chain, and
  // inserting the deck pill into the middle of it detached the follow-up branches from the
  // reply branch — so "✓ replied Jul 31" and the legacy "✓ replied" shim both fired at once.
  // Chained conditions that mix unrelated signals break like this every time they are edited.
  const conv = c.conversation || {};

  // 1. Whose turn. `replied_at` is authoritative (CRM-1 writes it); `followup_status` is the
  //    ARCH-3 legacy shim and may ONLY speak when the real column is empty.
  if (conv.state === 'awaiting_us')
    pills.push(`<span class="pill due" title="They replied and nobody has answered">💬 your turn${conv.days >= 1 ? ` · ${conv.days}d` : ''}</span>`);
  else if (conv.stalled && (conv.unanswered || 0) < 2)
    pills.push(`<span class="pill due" title="They replied, you answered, and it has gone quiet since">🕓 quiet ${conv.days}d</span>`);
  else if (conv.stalled)
    pills.push(`<span class="pill off" title="Two messages sent with no answer — the nudge is spent">🕓 quiet ${conv.days}d · nudged ${conv.unanswered}×</span>`);
  else if (c.replied_at)
    pills.push(`<span class="pill on">✓ replied ${esc(shortDate(c.replied_at))}</span>`);
  else if (c.followup_status === 'replied')
    pills.push(`<span class="pill on">✓ replied</span>`);

  // 2. The follow-up ladder — only meaningful while nobody has replied.
  if (!c.replied_at && conv.state !== 'awaiting_us') {
    // `exhausted` first: every channel we used has run out and nobody answered. It is the END
    // of the ladder, so showing "↻ due" beside it would be contradictory — and a contact who
    // was never written to must NEVER wear this, which is why the server computes it from
    // ladder state rather than from "no reply".
    if (c.exhausted)                         pills.push(`<span class="pill none" title="Every follow-up on every channel has been sent and nobody answered">🚫 no response</span>`);
    else if (c.followup_state === 'due')     pills.push(`<span class="pill due">↻ due</span>`);
    else if (c.followup_state === 'waiting') pills.push(`<span class="pill off">↻ ${fuWhen(c.followup_due_in_h)}</span>`);
  }

  // 3. A deck click. Shown even when they also replied — "read the deck AND answered" is a
  //    different person from "answered without looking".
  if (c.deck_viewed_at)
    pills.push(`<span class="pill deck" title="Clicked the intro deck link${
      c.deck_views > 1 ? ` — ${c.deck_views} times, last ${esc(shortDate(c.deck_last_at))}` : ''
    }">👁 opened the deck${c.deck_views > 1 ? ` ×${c.deck_views}` : ''}</span>`);
  if (c.phone) pills.push(`<span class="pill on">📱</span>`);
  // 💡 The operator's own marker — "this is one of the people I care about here". It is the ONE
  // signal on this row that is not derived from what happened; everything else says what the
  // system observed, and this says what the human decided.
  //
  // On the COLLAPSED row, and a real <button>, not a span dressed as one (§Lessons 88 — the
  // attachment toggle shipped as a `<span>` and the first thing it got was a click and a bug
  // report). `stopPropagation`, or pressing it opens the contact panel underneath.
  const flag = `<button class="flagbtn${c.flagged ? ' on' : ''}" aria-pressed="${!!c.flagged}"
      title="${c.flagged ? 'Remove your flag' : 'Flag this person — the row stays marked until you press it again'}"
      onclick="event.stopPropagation();toggleFlag('${esc(c.id)}', this)">${c.flagged ? '💡' : '○'}</button>`;
  return `
    <div class="prow ${open ? 'is-open' : ''}${c.flagged ? ' is-flagged' : ''}" onclick="toggleContact('${esc(c.id)}')">
      ${flag}
      <span class="av" style="background:${avatarColor(c.full_name)}">${initials(c.full_name)}</span>
      <span class="pwho"><span class="pname">${esc(c.full_name)}</span> <span class="prole">— ${esc(c.title)}</span>
        ${c.hot ? `<span class="chip conn">🤝</span>` : ''}</span>
      <span class="pills">${c.confidence === 'medium' ? `<span class="pill warn" title="Nothing confirms this person works there — no company email and no employer on file">? unconfirmed</span>` : ''}${pills.filter(Boolean).join('')}<span class="caret">${open ? '▾' : '▸'}</span></span>
    </div>
    ${open ? contactPanel(c) : ''}`;
}

//: Flip the marker. The state is decided HERE and sent, never toggled on the server: two clicks
//: either side of the 2.5s refresh would otherwise leave the button and the database disagreeing
//: with no way to tell which is right.
//:
//: The button repaints before the request goes out, because a marker that waits up to 2.5s to
//: appear reads as a dead control and gets clicked again (§Lessons 43). It is put back if the
//: write fails — an optimistic paint that cannot be undone is just a lie with better timing.
async function toggleFlag(cid, btn) {
  const want = !btn.classList.contains('on');
  btn.classList.toggle('on', want);
  btn.textContent = want ? '💡' : '○';
  btn.setAttribute('aria-pressed', String(want));
  btn.closest('.prow').classList.toggle('is-flagged', want);
  const r = await post('/api/contact/flag', { contact_id: cid, on: want ? 1 : '' });
  if (!r.ok) {
    btn.classList.toggle('on', !want);
    btn.textContent = !want ? '💡' : '○';
    btn.setAttribute('aria-pressed', String(!want));
    btn.closest('.prow').classList.toggle('is-flagged', !want);
    alert(r.message || 'Could not save that flag');
    return;
  }
  // Then MOVE it to the Highlighted group. The paint above is instant but leaves the row where
  // it was, and the next natural refresh is up to 2.5s away — long enough that the click reads
  // as "the bulb lit and nothing happened", which is how half the controls in this file got
  // reported as broken.
  //
  // `rerenderJobs()` renders from LAST_JOBS, so the flag has to be written there first or the
  // row re-renders with its OLD value and drops straight back down — §Lessons 21, a value one
  // layer holds that the other cannot see. Mirrors the server's READ-BACK (`r.flagged`), never
  // the optimistic `want`: the POST is what decided, and the two cannot be allowed to disagree.
  setFlagIn(LAST_JOBS, cid, !!r.flagged);
  rerenderJobs();
}

//: Write a flag into the payload the renderer reads. Contact ids are unique across jobs, so this
//: scans every job rather than needing to know which one the row belongs to. The next refresh
//: overwrites LAST_JOBS wholesale with the server's answer, so this only has to survive ~2.5s.
function setFlagIn(jobs, cid, on) {
  for (const j of (jobs || [])) {
    for (const c of (j.contacts || [])) if (c.id === cid) c.flagged = on;
  }
}
// Channels become tabs inside the open contact, so the email draft, the LinkedIn note and
// the phone field stop competing for the same vertical space.
// CRM-4. The conversation, from stored HEADERS only — who wrote, when, and who was added.
// No bodies are stored, so this deliberately shows structure rather than pretending to be an
// email client: the point is "there is a live conversation here and somebody new is on it",
// which a boolean `replied` threw away entirely.
// The conversation, as the primary content of the Email tab. Timeline first, composer anchored
// to the bottom of it — the shape Front/Missive/Superhuman all converge on, and the one this
// panel had inverted.
// Gmail can always show what we cannot. Under the metadata scope the honest answer to "where is
// her reply?" is "one click away, and here is the link" — an href, no backend, no scope change.
//: TWO sources, and the order matters. `reply_to.thread_id` is read off a thread somebody
//: actually answered on — 11 contacts. `contacts.thread_id` is what Gmail handed back when we
//: SENT — 141. Reading only the first meant the link was missing for 130 people we have a real
//: thread with, which is most of them.
function gmailThreadUrl(c) {
  const tid = ((c.reply_to || {}).thread_id || c.thread_id || '').trim();
  return tid ? `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(tid)}` : '';
}

//: When no thread was ever captured, search Gmail for the address instead of rendering nothing.
//: This is not a consolation prize — it is the ONLY thing that finds the conversations the
//: stored `thread_id` cannot: a thread they started, a reply from a different address, or one
//: where we were merely Cc'd. Those are exactly the threads `replies.sync_all_with()` exists to
//: catch, and the same reason it searches by address rather than by id.
function gmailSearchUrl(c) {
  const e = (c.email || '').trim();
  return e ? `https://mail.google.com/mail/u/0/#search/${encodeURIComponent(e)}` : '';
}

function gmailLink(c, label) {
  const u = gmailThreadUrl(c);
  return u ? `<a class="gm-link" href="${esc(u)}" target="_blank" rel="noopener">${label}</a>` : '';
}

//: The meta-row link, beside LinkedIn and Apollo. Always offered when there is an address —
//: a link that is present for some contacts and absent for others is one the operator stops
//: looking for (§Lessons 43).
//:
//: The LABEL is honest about which of the two it is, because they do different things: one opens
//: the conversation, the other opens a search that may return nothing. Same idiom the row
//: already uses one link to the left — `Apollo ↗` for the profile, faint `search ↗` when all we
//: can do is look.
function gmailMetaLink(c) {
  const thread = gmailThreadUrl(c);
  if (thread)
    return ` · <a class="gm-link" href="${esc(thread)}" target="_blank" rel="noopener"
      title="Open this conversation in Gmail">✉ Gmail ↗</a>`;
  const search = gmailSearchUrl(c);
  if (!search) return '';
  return ` · <a class="gm-alt" href="${esc(search)}" target="_blank" rel="noopener"
      title="No thread was captured for ${esc(c.full_name || 'this contact')} — this searches Gmail for ${esc(c.email)}"
      >✉ Gmail search ↗</a>`;
}
// "5 days ago" / "6 hours ago" / "just now". `days >= 1 ? Nd : 'today'` called a reply from
// 20 hours ago "today" when it had landed yesterday evening.
function agoPhrase(conv) {
  const h = conv.hours;
  if (h == null) return '';
  if (h < 1) return 'just now';
  if (h < 24) return `${Math.round(h)} hour${Math.round(h) === 1 ? '' : 's'} ago`;
  const d = conv.days || Math.floor(h / 24);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}

function conversationView(c) {
  const msgs = c.thread || [];
  const conv = c.conversation || {};
  // `conv.who` is the INBOUND sender, so it is empty in `awaiting_them` — the last message was
  // ours. Falling back to the literal "They" produced "waiting on They."; the contact's own
  // name is right there and is who we are waiting on.
  const first = (conv.who || c.full_name || 'them').split(/\s+/)[0];
  // Urgency with the action attached. A banner that only accuses is a label to read; Superhuman's
  // whole point is that "needs a reply" is a bucket you act on.
  const banner = conv.state === 'awaiting_us'
    ? `<div class="conv-turn us"><span>⚠ Your turn — ${esc(first)} replied ${esc(agoPhrase(conv))}</span>
         <span class="conv-acts"><button class="linklike" onclick="openReplyHere('${esc(c.id)}', '${esc((c.reply_to || {}).thread_key || '')}')">Answer now</button>${gmailLink(c, 'Open in Gmail ↗')}</span></div>`
    : conv.state === 'awaiting_them'
      ? `<div class="conv-turn them"><span>Answered ${esc(agoPhrase(conv))} — waiting on ${esc(first)}.</span>
         <span class="conv-acts">${gmailLink(c, 'Open in Gmail ↗')}</span></div>`
      : '';
  const intro = c.introduced_by
    ? `<div class="th-intro">👋 ${esc(c.introduced_by)} added them to this thread</div>` : '';
  // Long threads collapse in the middle. This block is re-rendered every 2.5s and an unbounded
  // list pushes the composer — the only thing you came here to use — off screen.
  //
  // The collapse was UNEXPANDABLE, and that is the bug rather than the collapsing. An eight
  // message thread with Google rendered three and said "· 5 earlier messages ·" as plain text,
  // so the middle of a live conversation — including the reply that asked a question — was
  // stored, on the wire, and unreachable. Reported as "I'm not getting the entire interaction",
  // which is what it looked like. It is a button now, and the expanded state survives the 2.5s
  // refresh like every other open thing on this page.
  // GROUPED BY THREAD, because these are not one conversation.
  //
  // `thread_for_contact` returns every message stored for a person, merged and sorted by date —
  // so one contact with seven Gmail threads (a calendar invite, an intro, two separate deal
  // conversations) rendered as a single continuous stream with Laura, Kevin, Diego and the
  // operator interleaved. Reported as "this just looks like one long conversation which is not".
  //
  // Groups are ordered by their LAST message, so the live conversation sits at the bottom, next
  // to the composer. Only that one is open by default: seven expanded threads is the wall of
  // text this replaced.
  const groups = groupThreads(msgs);
  // WHICH THREAD OPENS BY DEFAULT: the newest one you can ANSWER, falling back to the newest.
  //
  // It was simply the newest by date, which was right when there was one composer at the bottom
  // of the card. With a composer inside each thread it puts the box you need behind a click:
  // the last thing on a card is often our own unanswered email, or a calendar acceptance, so a
  // banner reading "your turn — X replied 1d ago" would sit above a COLLAPSED reply box while
  // an unanswerable thread was the one hanging open.
  const answerable = groups.filter(g => (c.reply_targets || {})[g.id]);
  const newest = (answerable.length ? answerable[answerable.length - 1]
                                    : groups[groups.length - 1] || {}).id || '';
  const rows = groups.map(g => {
    const key = `${c.id}|${g.id}`;
    const open = groups.length === 1 || g.id === newest
      ? !CONV_SHUT.has(key)
      : CONV_OPEN.has(key);
    // ANSWERABLE, marked on the COLLAPSED header. The composer lives inside the thread, which is
    // right — but only one thread is open by default, so without this a card still shows six
    // closed rows and nothing saying any of them can be replied to. That is the shape reported
    // as "I can only answer the latest", and shipping the composers without the marker would
    // have reproduced it (§Lessons 43: a control nobody can find is a broken feature).
    const canReply = !!(c.reply_targets || {})[g.id];
    const mark = canReply ? '<span class="th-can" title="You can reply to this one">↩</span>' : '';
    const head = `<div class="th-sep${open ? '' : ' shut'}${canReply ? ' can' : ''}">
        <button class="linklike" onclick="toggleThread(${tagArg(key)})">
          <span class="th-caret">${open ? '▾' : '▸'}</span>
          <span class="th-subj">${esc(g.subject || '(no subject)')}</span>
          <span class="th-meta">${mark}${g.msgs.length} message${g.msgs.length === 1 ? '' : 's'} · ${
            esc(shortDate(msgAt(g.msgs[g.msgs.length - 1])))}</span>
        </button>
      </div>`;
    if (!open) return head;
    // The within-thread collapse is unchanged, just keyed per THREAD now — an 11-message thread
    // still folds in the middle, and the gap is still a button (§Lessons 90).
    const full = CONV_EXPANDED.has(key);
    const shown = (g.msgs.length > 6 && !full)
      ? [g.msgs[0], {_gap: g.msgs.length - 3}, ...g.msgs.slice(-2)]
      : g.msgs;
    let seenCc = [];   // reset per thread: a Cc carried across threads is a different thread's
    const body = shown.map(m => {
      if (m._gap)
        return `<div class="cm-gap"><button class="linklike" onclick="expandConv(${tagArg(key)})"
          >· show ${m._gap} earlier message${m._gap === 1 ? '' : 's'} ·</button></div>`;
      const html = convMessage(c, m, seenCc);
      seenCc = (m.cc_addrs || []).map(x => addrOf(x));
      return html;
    }).join('');
    const shut = (full && g.msgs.length > 6)
      ? `<div class="cm-gap"><button class="linklike" onclick="collapseConv(${tagArg(key)})"
         >· collapse ·</button></div>` : '';
    // THE COMPOSER BELONGS TO THE THREAD, not to the contact.
    //
    // It used to render once, below every thread, wired to whichever conversation held the
    // newest inbound message. On a live card that meant seven threads and one reply box pinned
    // to the last of them: the other six could be read and not answered, and nothing said why.
    // §Lessons 89 — a control has to be beside the thing it acts on, and "at the bottom of a
    // list of seven" is beside none of them.
    //
    // Only a thread somebody has actually written on gets one. A conversation where only we
    // have spoken is a FOLLOW-UP, which has its own ladder and stop conditions, so offering a
    // reply box there would quietly turn one into the other.
    const target = (c.reply_targets || {})[g.id];
    return head + body + shut + (target ? replyBox(c, target) : '');
  }).join('');
  // A contact with a live conversation and NO answerable thread still needs to be told why —
  // `replyBox` renders that explanation, and with nothing to loop over it would never run.
  //
  // ...except on a BORROWED conversation, where there is nothing wrong: we know exactly who to
  // reply to, it simply is not this card's to answer. "Couldn't work out who to reply to" would
  // be a false alarm, and `borrowedBanner` has already said the true thing.
  const anyTarget = groups.some(g => (c.reply_targets || {})[g.id]);
  const fallback = (anyTarget || c.thread_from) ? '' : replyBox(c, c.reply_to);
  return `<div class="conv">${banner}${intro}
    <div class="conv-msgs">${rows}</div>
    ${fallback}
  </div>`;
}

//: Messages -> one entry per Gmail thread, oldest thread first.
//:
//: Keyed on `thread_id`, falling back to the SUBJECT when a row has none — pasted messages and
//: anything synced before threading was stored carry no id, and bucketing them all under "" would
//: rebuild the merge this exists to undo.
//: When a message happened. The stored row calls it `sent_at`; `timeline()` renames it to `at`
//: on the fetch path, so both reach this code and reading one of them silently loses the date.
function msgAt(m) { return String((m && (m.sent_at || m.at)) || ''); }

//: 'Re: RE: Fwd: hi' -> 'hi'. Repeated prefixes accumulate on a long thread, and stripping only
//: the first leaves 'fwd: hi' and 'hi' in two different groups.
//: MIRRORS `domain/conversations.py:_strip_re` — see `threadKey`.
function stripRe(subject) {
  let s = String(subject || '').trim();
  for (;;) {
    const m = /^\s*(re|fwd|fw)\s*(\[\d+\])?\s*:\s*/i.exec(s);
    if (!m) return s.trim();
    s = s.slice(m[0].length);
  }
}

//: Which Gmail conversation a stored row belongs to.
//:
//: **This rule exists twice, here and in `domain/conversations.py:thread_key`**, because the
//: browser decides which thread the composer sits under and the server has to resolve the same
//: name to work out who a reply reaches. `tests/test_thread_grouping_agrees.py` runs both over
//: one fixture and fails if they disagree — two implementations of one rule is how one enforces
//: it and the other quietly does not (§Lessons 49), and here the second one decides recipients.
function threadKey(m) {
  const tid = String((m && m.thread_id) || '').trim();
  return tid || `subj:${stripRe((m && m.subject) || '').trim().toLowerCase()}`;
}

function groupThreads(msgs) {
  const by = new Map();
  for (const m of msgs || []) {
    const id = threadKey(m);
    if (!by.has(id)) by.set(id, {id, subject: m.subject || '', msgs: []});
    by.get(id).msgs.push(m);
  }
  const out = [...by.values()];
  for (const g of out) {
    g.msgs.sort((a, b) => msgAt(a).localeCompare(msgAt(b)));
    // The SHORTEST subject in the thread: "Re: Re: Ormus <> AMSYS" is the same conversation as
    // "Ormus <> AMSYS", and the separator should read as the latter.
    g.subject = g.msgs.map(m => m.subject || '').filter(Boolean)
      .sort((a, b) => a.length - b.length)[0] || '';
  }
  return out.sort((a, b) => msgAt(a.msgs[a.msgs.length - 1])
    .localeCompare(msgAt(b.msgs[b.msgs.length - 1])));
}

//: Per-thread open state. Two sets rather than one, because the DEFAULT differs: the newest
//: thread starts open (so a card still lands on the live conversation) and the rest start shut.
//: One set would make "shut the newest" and "open an old one" the same fact.
const CONV_OPEN = new Set();
const CONV_SHUT = new Set();
function toggleThread(key) {
  if (CONV_OPEN.has(key)) { CONV_OPEN.delete(key); CONV_SHUT.add(key); }
  else if (CONV_SHUT.has(key)) { CONV_SHUT.delete(key); CONV_OPEN.add(key); }
  else { CONV_OPEN.add(key); CONV_SHUT.add(key); CONV_OPEN.delete(key); CONV_SHUT.add(key); }
  rerenderJobs(true);
}

//: Which threads the operator has opened out. Outside the DOM, because `refresh()` rewrites
//: #jobs every 2.5s — the same reason PANEL_OPEN, TAB_OPEN and CONTACT_OPEN live here.
const CONV_EXPANDED = new Set();
function expandConv(cid) { CONV_EXPANDED.add(cid); rerenderJobs(); }
function collapseConv(cid) { CONV_EXPANDED.delete(cid); rerenderJobs(); }
function addrOf(raw) {
  const m = /<([^>]+)>/.exec(String(raw || ''));
  return (m ? m[1] : String(raw || '')).trim().toLowerCase();
}

// One message. We hold HEADERS for everything and TEXT only where it was pasted or the content
// scope filled it in, so this says which it is rather than rendering an empty bubble and
// leaving the operator to wonder whether the message was blank or merely unread.
function convMessage(c, m, prevCc) {
  const mine = m.direction !== 'in';
  const who = mine ? 'You' : (m.from_name || m.from_addr || 'They');
  // The one thing headers are uniquely good at: WHERE somebody joined. Printing the identical
  // full Cc list on every row buries it — the join is the signal, the repetition is noise.
  const nowCc = (m.cc_addrs || []).map(addrOf);
  const joined = (m.cc_addrs || []).filter(x => !(prevCc || []).includes(addrOf(x)));
  const cc = (prevCc && joined.length)
    ? `<div class="cm-join">👋 ${joined.map(esc).join(', ')} joined</div>`
    : (nowCc.length && !prevCc ? `<div class="cm-cc">cc ${(m.cc_addrs || []).map(esc).join(', ')}</div>` : '');
  // Our own outreach body lives on the contact, not in `messages`. Match the first OUTBOUND
  // message by index — `m === c.thread[0]` was an object-identity check against position zero,
  // so every later outbound row rendered blank (including the reply you had just sent), and a
  // thread we were looped into starts inbound so the outreach never rendered at all.
  const firstOutIdx = (c.thread || []).findIndex(x => (x.direction || '') !== 'in');
  const isFirstOut = mine && (c.thread || []).indexOf(m) === firstOutIdx;
  const body = m.snippet || (isFirstOut ? (c.outreach_message || '') : '');
  // A message sitting exactly on the auto-sync cap was CUT, and nothing said so — it simply
  // stopped mid-sentence, which reads as the message having been lost rather than as the
  // deliberate 200-character bound CRM-4b chose. Naming it also surfaces the way to get more:
  // "⤓ Fetch from Gmail" pulls the same thread at PASTED_MAX and is otherwise a button whose
  // purpose is invisible until you press it.
  const clipped = (m.snippet || '').length >= CONV_SNIPPET_MAX
    ? `<span class="cm-clip" title="ApplyPilot stores the first ${CONV_SNIPPET_MAX} characters of an automatically synced message. Use “⤓ Fetch from Gmail” above to pull this thread in full.">…truncated</span>`
    : '';
  const text = body
    ? `<div class="cm-body">${esc(body)}${clipped}</div>`
    // Never the "not stored" line on our OWN message — saying that about something we sent
    // reads as data loss rather than a scope we chose.
    : (mine ? `<div class="cm-nobody">Sent from ApplyPilot.</div>`
            : `<div class="cm-nobody">ApplyPilot stores who and when, not what.
                 ${CONTENT_SCOPE ? `<button class="linklike" onclick="fetchReplyText('${esc(c.id)}', this)">⤓ Fetch from Gmail</button>` : ''}
                 ${gmailLink(c, 'Read it in Gmail ↗')}
                 <button class="linklike" onclick="editSaid('${esc(c.id)}')">Paste it here</button></div>`);
  return `<div class="cm ${mine ? 'out' : 'in'}">
    <div class="cm-hdr"><span class="cm-who">${esc(who)}</span>
      <span class="cm-when">${esc(shortDate(m.sent_at))}</span></div>
    ${cc}${text}</div>`;
}
// THREAD_OPEN / THREAD_SHUT / onThreadToggle are gone with the <details> they controlled. The
// conversation is no longer something to expand — for a contact who replied it IS the tab.

// What the operator typed, and any Cc they removed — held here rather than in the DOM because
// the 2.5s refresh replaces #jobs wholesale. It skips while an input has FOCUS, which saves you
// mid-sentence but not the moment you click away to read the thread above the box.
const REPLY_DRAFT = new Map();   // rkey -> body
const REPLY_DROP  = new Map();   // rkey -> Set of cc addresses removed

//: EVERY piece of composer state is keyed by (contact, THREAD), through this one function.
//:
//: They were keyed by contact alone, which was correct while there was one composer. With one
//: per conversation a contact-only key makes seven boxes share a draft: type into the deal
//: thread, and the same words appear in the calendar-invite box — and then Send picks whichever
//: one you clicked. One key function rather than seven inline templates, so a new piece of state
//: cannot quietly use a different one (§Lessons 49).
function rkey(cid, tk) { return `${cid}|${tk || ''}`; }

// Replying, not following up. The distinction is real: a follow-up is a ladder step with a
// schedule and a stop condition, a reply answers a person who wrote to us. A thread with no
// inbound message gets NO composer, which is what keeps the two from blurring together —
// measured 2026-08-13, 72 of 238 threads are replyable and the rest correctly offer nothing.
function replyBox(c, t) {
  // `_reply_target()` swallows every exception and returns None, so a thread we KNOW has an
  // inbound message can arrive with no reply target. Returning '' here rendered a conversation
  // with no composer, no explanation and no error — while the row still said "your turn".
  // A zero must be as loud as a failure (§Lessons 15); the log.debug is invisible from here.
  if (!t || !t.to_addr) {
    return hasConversation(c)
      ? `<div class="reply-box"><div class="reply-msg bad">Couldn’t work out who to reply to on
         this thread. Reply in Gmail, or use “📥 Check replies” to re-read it.</div></div>`
      : '';
  }
  const tk = t.thread_key || '';
  const k = rkey(c.id, tk);
  const ka = `'${esc(c.id)}', '${esc(tk)}'`;
  const dropped = REPLY_DROP.get(k) || new Set();
  const cc = (t.cc || []).filter(x => !dropped.has(x));
  // The Cc is the whole reason this exists: answering only the sender drops whoever they
  // introduced, and nothing on screen would show that it happened.
  const chips = (t.cc || []).map(x => {
    const off = dropped.has(x);
    return `<button class="cc-chip${off ? ' off' : ''}" title="${off ? 'Add back' : 'Remove from this reply'}"
      onclick="toggleCc(${ka}, decodeURIComponent('${encodeURIComponent(x)}'))">${esc(x)} ${off ? '＋' : '✕'}</button>`;
  }).join('');
  const body = REPLY_DRAFT.get(k) || '';
  return `<div class="reply-box" data-reply-for="${esc(c.id)}" data-cc="${esc(JSON.stringify(cc))}" data-to="${esc(t.to)}" data-thread="${esc(tk)}">
    <div class="reply-hdr">↩ Reply to <strong>${esc(t.to)}</strong>${
      cc.length ? ` · cc ${cc.length}` : (t.cc || []).length ? ' · <span class="cc-none">cc removed</span>' : ''}</div>
    ${(t.cc || []).length ? `<div class="cc-row">${chips}</div>` : ''}
    ${lastReplyCard(c, tk)}
    <div class="reply-subj">${esc(t.subject)}</div>
    <textarea class="reply-body" rows="6" placeholder="Write your reply…"
      oninput="REPLY_DRAFT.set('${esc(k)}', this.value)">${esc(body)}</textarea>
    <div class="reply-actions">
      <button class="secondary" onclick="draftReply(${ka}, this)">✍ Draft an answer</button>
      <button class="primary" onclick="sendReply(${ka}, this)">Send reply</button>
      <span class="reply-hint">Goes into this thread. No attachments.</span>
    </div>
    ${replyMsg(c.id, tk)}
    <input class="r-style" placeholder="✨ Tweak the vibe, then Draft again — e.g. 'warmer', 'shorter', 'more direct'"
      value="${esc(REPLY_STYLE.get(k) || '')}"
      oninput="REPLY_STYLE.set('${esc(k)}', this.value)">
  </div>`;
}

// What they actually said. Stored automatically when gmail.readonly is on (CRM-4b); otherwise
// the operator pastes it. The drafter does not care which — but SOMETHING has to be here, or
// the "contextual" reply is a generic follow-up wearing a Re: subject line.
const REPLY_SAID = new Map();    // rkey -> pasted text, survives the 2.5s refresh
const REPLY_STYLE = new Map();   // rkey -> vibe directive
const SAID_EDIT = new Set();     // rkeys whose paste box is deliberately open
function editSaid(k) { SAID_EDIT.add(k); refresh(); }
function doneSaid(k) { SAID_EDIT.delete(k); refresh(); }

function lastReplyCard(c, tk) {
  const r = c.last_reply;
  const k = rkey(c.id, tk);
  const editing = SAID_EDIT.has(k) || !r;
  if (!editing) {
    const tag = r.label ? `<span class="intent-chip ${esc(r.intent)}">${esc(r.label)}</span>` : '';
    const act = r.action ? `<div class="intent-act">${esc(r.action)}</div>` : '';
    return `<div class="said">
      <div class="said-hdr">${esc(r.from || 'They')} wrote ${tag}
        <button class="linklike" onclick="editSaid('${esc(k)}')">✎ edit</button></div>
      <div class="said-txt">“${esc(r.text)}”</div>${act}
    </div>`;
  }
  const text = REPLY_SAID.has(k) ? REPLY_SAID.get(k) : (r ? r.text : '');
  return `<div class="said">
    <div class="said-hdr">What they wrote
      ${r ? `<button class="linklike" onclick="doneSaid('${esc(k)}')">done</button>`
          : `<span class="said-why">paste it and the draft can actually answer it</span>`}</div>
    <textarea class="said-box" rows="4" placeholder="Paste their reply here…"
      oninput="REPLY_SAID.set('${esc(k)}', this.value)">${esc(text)}</textarea>
  </div>`;
}

// Feedback belongs NEXT TO THE BUTTON. Both of these used to write to #command at the very top
// of the page: with no reply text stored, clicking Draft returned a perfectly clear "paste what
// they wrote first" that rendered a full screen away from the click — reported, reasonably, as
// "the draft an answer button is not working".
const REPLY_MSG = new Map();     // rkey -> {text, bad} — survives the 2.5s refresh
function replyMsg(cid, tk) {
  const m = REPLY_MSG.get(rkey(cid, tk));
  return m ? `<div class="reply-msg ${m.bad ? 'bad' : 'good'}">${esc(m.text)}</div>` : '';
}
function setReplyMsg(cid, tk, text, bad) {
  const k = rkey(cid, tk);
  if (text) REPLY_MSG.set(k, {text, bad: !!bad}); else REPLY_MSG.delete(k);
}

async function draftReply(cid, tk, btn) {
  const k = rkey(cid, tk);
  const card = btn.closest('.reply-box');
  const box = card ? card.querySelector('.said-box') : null;
  const said = box ? box.value.trim() : (REPLY_SAID.get(k) || '');
  btn.disabled = true; btn.textContent = 'Drafting…';
  setReplyMsg(cid, tk, 'Reading the conversation and writing an answer…', false);
  const live = card ? card.querySelector('.reply-msg') : null;
  if (live) { live.textContent = 'Reading the conversation and writing an answer…'; live.className = 'reply-msg good'; }
  // The THREAD goes with it. `_draft_reply` reads the stored conversation to work out what it
  // is answering, and unscoped that is every thread merged — so a draft written under one
  // subject would answer the newest message under another.
  const r = await post('/api/contact/draft-reply',
                       {contact_id: cid, thread: tk || '', their_reply: said,
                        style: REPLY_STYLE.get(k) || ''});
  if (r.ok && r.body) {
    // Into the shared store, not straight into the DOM — the 2.5s refresh replaces #jobs
    // wholesale and would wipe a value written only to the textarea.
    REPLY_DRAFT.set(k, r.body);
    if (said) { REPLY_SAID.set(k, said); SAID_EDIT.delete(k); }
    setReplyMsg(cid, tk, r.message || 'Draft ready — read it before you send it.', false);
  } else {
    setReplyMsg(cid, tk, r.message || 'Draft failed.', true);
  }
  btn.disabled = false; btn.textContent = '✍ Draft an answer';
  refresh();
}
function firstName(name) { return String(name || '').trim().split(/\s+/)[0] || 'them'; }

// Jump straight from the row's Next action into the composer: open the panel, the People tab,
// the contact, their email channel and the conversation. Anything less leaves the operator to
// find the thread themselves, which is how a reply ends up unanswered for a week.
function openReply(url, cid) {
  PANEL_OPEN.add(url);
  TAB_OPEN.set(url, 'people');
  CONTACT_OPEN.add(cid);
  CHANNEL_TAB.set(cid, 'email');
  refresh();
  // The refresh replaces #jobs wholesale, so the textarea only exists after it has run.
  // LAST, not first: composers render in thread order, oldest conversation at the top, and the
  // row's Next action is about the person who just wrote — landing on their oldest thread is
  // the same misdirection this whole change removes, one level up.
  setTimeout(() => {
    const boxes = document.querySelectorAll(`[data-reply-for="${cid}"] .reply-body`);
    const el = boxes[boxes.length - 1];
    if (el) { el.focus(); el.scrollIntoView({block: 'center', behavior: 'smooth'}); }
  }, 60);
}
// Pull EVERY Gmail conversation with this person — not just the thread ApplyPilot sent.
// Until this existed the CRM's memory stopped at its own outbox: a thread they started, an
// email sent straight from Gmail, or one where they only CC'd you was invisible, because
// everything was looked up by a thread id captured at send time.
// Lives on the contact's META row, which renders for EVERY open contact — not inside the
// conversation view, where it started. That was a chicken-and-egg: the conversation view only
// renders once an inbound message exists, so the button for "my thread is missing or broke"
// was hidden inside the thread that was missing. The repair tool has to be reachable from the
// broken state, which is the only state anyone needs it in.
function syncGmailBtn(c) {
  if (!c.email) return '';
  if (!CONTENT_SCOPE) {
    return `<div class="sync-gm off" title="Needs gmail.readonly">⟳ Fetch from Gmail — off.
      Enable with <code>network --gmail-connect --with-content</code></div>`;
  }
  const msg = SYNC_MSG.get(c.id);
  return `<div class="sync-gm">
      <button class="linklike" title="Search Gmail for every conversation with ${esc(c.email)} — threads they started, mail sent from Gmail directly, and threads where they only Cc'd you"
        onclick="syncGmail('${esc(c.id)}', this)">⟳ Fetch from Gmail</button>
      ${msg ? `<span class="sync-note ${msg.bad ? 'bad' : ''}">${esc(msg.text)}</span>` : ''}
    </div>`;
}
const SYNC_MSG = new Map();   // contact id -> {text, bad}; survives the 2.5s refresh
async function syncGmail(cid, btn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Searching Gmail…';
  const r = await post('/api/contact/sync-gmail', {contact_id: cid});
  // Reported HERE, beside the button, not in the reply composer — which may not exist yet, and
  // when it does not is exactly when this button is being used.
  SYNC_MSG.set(cid, {text: r.message || (r.ok ? 'Synced.' : 'Could not sync.'), bad: !r.ok});
  setTimeout(() => { SYNC_MSG.delete(cid); }, 15000);
  btn.disabled = false; btn.textContent = label;
  refresh();
}

// Read THIS conversation's text from Gmail, because you asked for this one. Never automatic:
// the poller and `tick` store no message text at all, whatever the token allows.
async function fetchReplyText(cid, btn) {
  btn.disabled = true; btn.textContent = 'Reading…';
  const r = await post('/api/contact/fetch-reply', {contact_id: cid});
  // Reports into the composer of the thread this button sits in. It is rendered inside a
  // `.reply-box`, so the thread is on the card rather than something to re-derive.
  const card = btn.closest('.reply-box');
  setReplyMsg(cid, card ? (card.dataset.thread || '') : '',
              r.message || (r.ok ? 'Read.' : 'Could not read it.'), !r.ok);
  btn.disabled = false; btn.textContent = '⤓ Fetch from Gmail';
  refresh();
}
// From the banner: put the cursor in the composer. The contact is already open when the banner
// is visible, so this is a focus, not a navigation.
//
// There are now several composers for one contact, one per answerable thread. The banner says
// "X replied N ago", which is the NEWEST inbound — so it must land on that thread's box and not
// simply the first in the DOM, which is the OLDEST conversation.
function openReplyHere(cid, tk) {
  const sel = tk ? `[data-reply-for="${cid}"][data-thread="${tk}"]` : `[data-reply-for="${cid}"]`;
  const boxes = document.querySelectorAll(`${sel} .reply-body`);
  const el = boxes[boxes.length - 1];
  if (el) { el.focus(); el.scrollIntoView({block: 'center', behavior: 'smooth'}); }
}
function toggleCc(cid, tk, address) {
  const k = rkey(cid, tk);
  const set = REPLY_DROP.get(k) || new Set();
  if (set.has(address)) set.delete(address); else set.add(address);
  REPLY_DROP.set(k, set);
  refresh();
}
async function sendReply(cid, tk, btn) {
  const k = rkey(cid, tk);
  const card = btn.closest('.reply-box');
  const body = (REPLY_DRAFT.get(k) || '').trim();
  const say = (m, bad) => { setReplyMsg(cid, tk, m, bad); refresh(); };
  if (!body) { say('Write a reply before sending — or click “Draft an answer”.', true); return; }
  // The Cc travels as data, not as scraped chip text — the recipients of a real email are not
  // something to re-derive from innerText.
  let cc = [];
  try { cc = JSON.parse(card.dataset.cc || '[]'); } catch { cc = []; }
  const who = card.dataset.to || 'them';
  const also = cc.length ? `\n\nAlso going to: ${cc.join(', ')}` : '\n\nNobody is Cc\'d.';
  // NAME THE CONVERSATION in the confirm. With a composer under every thread, "Send this reply
  // to John?" is the same sentence whichever box you clicked, and the one thing you cannot
  // check afterwards is which conversation it went into.
  const subj = (card.querySelector('.reply-subj') || {}).textContent || '';
  const on = subj.trim() ? `\n\nOn: ${subj.trim()}` : '';
  if (!confirm(`Send this reply to ${who}?${on}${also}`)) return;
  btn.disabled = true; btn.textContent = 'Sending…';
  // The thread is a SELECTOR, not a recipient — the server still derives every address from the
  // stored rows of that conversation. Sending it is what stops a reply landing on whichever
  // thread happened to hold the newest inbound message.
  const r = await post('/api/contact/reply',
    {contact_id: cid, body, cc, thread: card.dataset.thread || ''});
  setReplyMsg(cid, tk, r.message || (r.ok ? 'Sent.' : 'Failed.'), !r.ok);
  if (r.ok) {
    // Clear ALL of it, and only for THIS thread. REPLY_STYLE and REPLY_MSG were missed once
    // already: the previous reply's vibe directive pre-filled the next one, and the green
    // "replied to …" banner stayed pinned under the composer for the rest of the session.
    // Clearing by contact would now also wipe a draft the operator has half-written in another
    // conversation with the same person.
    REPLY_DRAFT.delete(k); REPLY_DROP.delete(k); REPLY_SAID.delete(k);
    REPLY_STYLE.delete(k); SAID_EDIT.delete(k);
    setTimeout(() => { REPLY_MSG.delete(k); }, 6000);
  }
  else { btn.disabled = false; btn.textContent = 'Send reply'; }
  refresh();
}
function contactPanel(c) {
  // ALL THREE TABS ARE ALWAYS OFFERED, and the reason is the same one that already made Text
  // unconditional: the tab is where its identifier gets ENTERED, so hiding it without one hides
  // the only way to add one.
  //
  // They were hidden to fix a real bug — clicking "LinkedIn" on an email-only contact got you
  // the dead end "No LinkedIn profile.", and setChannel wrote that choice into CHANNEL_TAB so
  // the contact reopened on the empty tab forever. That fix treated the sentence as the cost
  // when the sentence WAS the cost: a pane describing an absence instead of offering the box
  // that ends it (§Lessons 41). 85 of 105 imported people had no address and none had a
  // LinkedIn URL, and there was nowhere in the app to type one in.
  //
  // `＋` on the tab so which identifiers are missing is legible without opening all three.
  // Now purely "does this channel have its identifier" — it drives the ＋ and which body to
  // render, never whether the tab exists. Text is in it for the marker alone: its own pane
  // already handles an absent number, but a strip where two empty channels are flagged and the
  // third is not reads as the third being fine.
  const usable = {email: !!c.email, linkedin: !!c.linkedin_url, phone: !!c.phone,
                  call: !!c.phone, meetings: !!(c.transcripts || []).length};
  const stored = CHANNEL_TAB.get(c.id);
  // Still OPENS on a channel that works, or every contact lands on a form instead of their
  // conversation. The stored choice is honoured even when empty — clicking a ＋ tab has to stay
  // put across the 2.5s refresh or the box vanishes mid-type.
  const ch = stored || (c.email ? 'email' : (c.linkedin_url ? 'linkedin' : 'phone'));
  const tab = (k, label, on) =>
    `<span class="${ch === k ? 'on' : ''}${usable[k] ? '' : ' add'}" onclick="event.stopPropagation();setChannel('${esc(c.id)}','${k}')">${label}${on || ''}${usable[k] ? '' : ' ＋'}</span>`;
  let body = '';
  if (ch === 'email')    body = c.email ? emailChannel(c) : addIdentifier(c, 'email');
  if (ch === 'linkedin') body = c.linkedin_url ? linkedinChannel(c) : addIdentifier(c, 'linkedin');
  if (ch === 'phone')    body = smsChannel(c);
  if (ch === 'call')     body = callChannel(c);
  if (ch === 'meetings') body = transcriptSection(c);
  return `<div class="pbody" onclick="event.stopPropagation()">
      <div class="cmeta">
        ${c.email ? `✉ <a href="mailto:${esc(c.email)}">${esc(c.email)}</a> ${emailBadge(c.email_status)}` : '✉ —'}
        ${c.linkedin_url ? ` · <a href="${esc(c.linkedin_url)}" target="_blank">LinkedIn ↗</a>` : ''}
        ${c.apollo_url ? ` · <a class="apollo-link" href="${esc(c.apollo_url)}" target="_blank" rel="noopener">Apollo ↗</a>` : ''}
        ${c.apollo_search_url ? `<a class="apollo-alt" href="${esc(c.apollo_search_url)}" target="_blank" rel="noopener">search ↗</a>` : ''}
        ${gmailMetaLink(c)}
        ${c.phone ? ` · 📱 <a href="tel:${esc(c.phone)}">${esc(c.phone)}</a> <a class="sms" href="sms:${esc(c.phone)}">text</a>` : ''}
        ${c.connection_company ? `<span class="conn-co"> · ${esc(c.connection_company)}</span>` : ''}
        ${c.verify_note ? `<div class="verify-note ${esc(c.confidence)}">${c.confidence === 'high' ? '✓' : '?'} ${esc(c.verify_note)}</div>` : ''}
        ${syncGmailBtn(c)}
      </div>
      <div class="chan">${tab('email','✉ Email')}${tab('linkedin','🔗 LinkedIn')}${tab('phone','💬 Text' + (c.sms_sent_at ? ' ✓' : ''))}${tab('call','📞 Call' + (c.call_made_at ? ' ✓' : ''))}${tab('meetings','📝 Meetings' + ((c.transcripts || []).length ? ' ' + c.transcripts.length : ''))}</div>
      ${body}
      ${engagementLog(c)}
      <div class="crow-del"><button class="link-danger" onclick="deleteContact('${esc(c.id)}', decodeURIComponent('${encodeURIComponent(c.full_name || '')}'), ${!!c.emailed})">🗑 Not at this company — remove</button></div>
    </div>`;
}
// Verification errs towards KEEPING an unconfirmed person (dropping a real contact is worse
// than showing a doubtful one), so wrong people do reach the list. This is how they leave.
// Inside the expanded panel, not on the collapsed row: deletion is destructive and should take
// a deliberate open-then-click, never a stray click while scanning.
async function deleteContact(id, name, emailed) {
  const warn = emailed
    ? `Remove ${name}?\n\nYou have ALREADY EMAILED this person. Removing them deletes the draft and the follow-up schedule; the activity log keeps a record that the email was sent.`
    : `Remove ${name}?\n\nThis deletes the contact and any drafted outreach for them.`;
  if (!confirm(warn)) return;
  const r = await post('/api/contact/delete', {contact_id: id});
  const cmdEl = document.getElementById('command');
  if (cmdEl) cmdEl.textContent = r.message || '';
  // On success the row vanishes, which is the feedback. A failure leaves the contact sitting
  // there looking exactly as it did before the click.
  if (!r.ok) alert(r.message || 'Could not remove that contact.');
  refresh();
}
// Once somebody has REPLIED, the email tab is a conversation — not an outreach form that
// happens to have a thread stapled above it.
//
// The old order put an editable copy of an email sent five days ago at the top of the panel,
// expanded, with Copy/Regenerate controls, while the live exchange sat collapsed. That form is
// a dead artifact: the message is already delivered and cannot be changed. Every CRM that does
// this well (Front, Missive, Superhuman) leads with the timeline and anchors the composer to
// the bottom of it; the sent message appears as one entry in that timeline, not as a form.
//
// A follow-up ladder still wins when there is no conversation — chasing silence is the right
// action then. It never wins over an actual reply.
// You are already talking to this person, on another application. The whole point is that it
// appears BEFORE any compose box, because the compose box is what caused the double-send.
function borrowedBanner(c) {
  const msgs = c.thread || [];
  const theirs = msgs.filter(m => m.direction === 'in').length;
  const other = (LAST_JOBS || []).find(j => j.url === c.thread_from);
  const where = other ? esc(other.title || other.company || 'another application')
                      : 'another application';
  const go = other
    ? `<button class="linklike" onclick="openReply(${tagArg(c.thread_from)}, ${tagArg(c.id)})">Open that application ↗</button>`
    : '';
  return `<div class="borrowed">
    <div><strong>You are already in touch with ${esc(firstName(c.full_name))}</strong> —
      ${msgs.length} message${msgs.length === 1 ? '' : 's'}${
        theirs ? `, ${theirs} from them` : ''} on <strong>${where}</strong>.</div>
    <div class="borrowed-why">Reply there so the conversation stays in one place. ${go}</div>
  </div>`;
}
function hasConversation(c) {
  return ((c.thread || []).some(m => m.direction === 'in'));
}
function emailChannel(c) {
  // WHAT HAS ALREADY HAPPENED COMES FIRST — every time, not only once they reply.
  //
  // `hasConversation` asks whether an INBOUND message exists, so a person we had emailed and
  // who had not answered opened on a compose box with their draft in it. Measured: **126 of 141
  // emailed contacts** rendered that way. On a brand-new card for somebody already contacted,
  // the dominant thing on screen was an editable draft, which reads as "here is what to send"
  // rather than "here is what you sent" — and that is how the same person gets written to
  // twice.
  //
  // §Lessons 31 recorded exactly this for the REPLIED case and the fix was applied only there:
  // "a contact who has REPLIED gets a conversation, not a form". The rule was always about
  // whether an email had gone out, not about whether one came back (§Lessons 49, half a rule).
  const history = (c.thread || []).length ? conversationView(c) : '';
  // BORROWED FROM ANOTHER ROLE'S CARD. `contact_id` hashes the job url, so the same person
  // found for a second role is a second row with an empty history — and opening it showed a
  // compose box for somebody nine messages into a conversation. Live: 5 cards, one of them
  // with three replies already on it.
  //
  // Shown READ-ONLY and clearly attributed. Sending from here would resolve recipients from
  // this row's own messages (there are none) and would file the reply under a second contact,
  // splitting the conversation further — so the offer is to go to the card that owns it.
  if (c.thread_from) return borrowedBanner(c) + history;
  // With an inbound message the composers already live inside the history, per thread.
  if (hasConversation(c)) return history;
  // A due follow-up is the more urgent thing to WRITE, but it no longer replaces the record of
  // what was already said — it sits under it.
  if (c.followup_state === 'due' || (c.followup_message || '').trim())
    return history + followupCard(c, {touch: (c.followup_count || 0) + 1}, c.followup_total);
  return history + draftBlock(c, true, false, !!history);
}
function linkedinChannel(c) {
  return linkedinThread(c) + draftBlock(c, false, true) + noticedBox(c);
}

// The middle of the conversation. `dm_status` is 'sent'|'manual' — both meaning WE sent an
// invite — so nothing recorded what THEY sent back, and `messages` cannot hold it: that table
// is keyed on Gmail's own message id and carries thread_id, rfc_message_id and from_addr, none
// of which a DM has. Faking them to fit would corrupt the join reply detection runs on.
//
// So these live in `interactions`, typed in. Nothing reads LinkedIn — automating it was
// abandoned twice (§Lessons 3) and it risks the account the whole outreach ladder runs on.
function linkedinThread(c) {
  const msgs = (c.interactions || []).filter(r => r.kind === 'linkedin_in' || r.kind === 'linkedin_out');
  const rows = msgs.slice().reverse().map(m => `
    <div class="li-msg ${m.kind === 'linkedin_in' ? 'them' : 'us'}">
      <div class="li-who">${m.kind === 'linkedin_in' ? esc(firstName(c.full_name)) : 'You'}
        <span class="li-when">${esc(shortDate(m.at))}</span></div>
      <div class="li-text">${esc(m.detail)}</div>
    </div>`).join('');
  return `<div class="li-thread">
      <div class="d-label">LinkedIn messages${msgs.length ? '' : ' <span class="ix-none">none logged</span>'}</div>
      ${rows}
      <textarea class="li-paste" id="lip-${esc(c.id)}" rows="2"
        placeholder="Paste what they wrote, or what you sent — LinkedIn cannot be read from here, so this is by hand."></textarea>
      <div class="dbtns">
        <button onclick="logLinkedinMsg('${esc(c.id)}','linkedin_in', this)">They messaged me</button>
        <button onclick="logLinkedinMsg('${esc(c.id)}','linkedin_out', this)">I replied</button>
        <span class="ix-why">Logging an inbound message stops the LinkedIn follow-up ladder.</span>
      </div>
    </div>`;
}

async function logLinkedinMsg(cid, kind, btn) {
  const box = document.getElementById('lip-' + cid);
  const detail = (box && box.value || '').trim();
  if (!detail) { alert('Paste the message first.'); return; }
  btn.disabled = true;
  const r = await post('/api/contact/interaction', {contact_id: cid, kind, detail});
  if (!r.ok) { btn.disabled = false; alert(r.message || 'Failed'); return; }
  if (box) box.value = '';
  refresh();
}

// What this person has actually DONE.
//
// OUTSIDE the channel tabs, deliberately. A reply, a deck open and a booked call belong to the
// PERSON, not to email or LinkedIn — putting the timeline behind one channel means the answer
// to "has this person engaged?" is one click away and invisible from the other two, which is
// the exact failure the retired Interactions tab had at job level. First attempt at this
// ticket put it on the LinkedIn tab and it was reported as unchanged from the Email tab.
//
// The one manual button lives here too: profile views are absent from LinkedIn's data export
// and generate no notification email, so the only source is their UI — which this project
// abandoned automating twice (§Lessons 3). Everything else on the list arrives by itself.
//
// Our own actions are never engagement. `dm_status` is sent|manual, both meaning WE sent it,
// and counting those is how the retired tab reported "3/3 engaged" when the honest number
// across every job was 2 of 58 (§Lessons 35).
// ── 📝 Meeting transcripts ──────────────────────────────────────────────────
//
// GRAN-1 phase 1. A PASTE, and that is the whole integration: Granola's own API is
// Business/Enterprise only, so an automated route is gated on a subscription rather than on
// code. The same reasoning that made the sheet import and the LinkedIn thread reader pastes —
// no plan, no install, no API key, nothing to revoke.
//
// Beside the conversation rather than in a tab of its own. §Lessons 89: findable is not the same
// as findable FROM WHERE THE WORK IS, and what was said on a call belongs with the person.

//: Which contacts have the paste box open. Outside the DOM like PANEL_OPEN — #jobs is replaced
//: wholesale every 2.5s.
const TR_OPEN = new Set();
function toggleTranscript(cid) {
  if (TR_OPEN.has(cid)) TR_OPEN.delete(cid); else TR_OPEN.add(cid);
  rerenderJobs(true);
}

function transcriptSection(c) {
  const rows = c.transcripts || [];
  const open = TR_OPEN.has(c.id);
  const list = rows.map(t => `
    <div class="tr-row">
      <span class="tr-title" title="${esc(t.title || 'Meeting')}">${esc(t.title || 'Meeting')}</span>
      <button class="linklike" onclick="showTranscript('${esc(t.id)}', this)">Read</button>
      <button class="link-danger tr-del"
        onclick="dropTranscript('${esc(t.id)}','${esc(c.id)}')" title="Remove from this contact">✕</button>
      <span class="tr-meta"><span class="tr-when">${esc(shortDate(t.started_at))}</span
        ><span class="tr-dot">·</span><span class="tr-len">${transcriptSize(t.body_len)}</span
        ><span class="tr-dot">·</span><span class="tr-sum">${esc(t.summary || '')}</span></span>
    </div>`).join('');
  return `<div class="tr-wrap">
      <div class="cnote-body">
        ${list || '<div class="hint" style="margin:0">No transcripts yet.</div>'}
        ${open ? `
          <div class="tr-form" data-cid="${esc(c.id)}">
            <input class="tr-title-in" placeholder="Title — e.g. Intro call" />
            <input class="tr-date" type="date" />
            <textarea class="tr-sum-in" rows="2"
              placeholder="Summary (paste Granola's). Leave empty and the opening of the call stands in."></textarea>
            <textarea class="tr-body-in" rows="6"
              placeholder="Paste the transcript here — open the note, select all, copy."></textarea>
            <div class="dbtns">
              <button class="primary" onclick="saveTranscript(this)">Save transcript</button>
              <button class="ghost" onclick="toggleTranscript('${esc(c.id)}')">Cancel</button>
            </div>
            <div class="tr-msg"></div>
            <div class="sms-hint">Attached to <b>${esc(c.full_name)}</b>. A call with several
              people is stored once and attached to each of them separately.</div>
          </div>`
        : `<button class="secondary" onclick="toggleTranscript('${esc(c.id)}')">＋ Add a transcript</button>`}
      </div>
    </div>`;
}

async function saveTranscript(btn) {
  const f = btn.closest('.tr-form');
  const msg = f.querySelector('.tr-msg');
  const body = fieldVal(f, '.tr-body-in');
  if (!body.trim()) { msg.textContent = 'Paste the transcript first.'; return; }
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = 'Saving…';
  const r = await post('/api/contact/transcript', {
    contact_ids: [f.getAttribute('data-cid')],
    body, title: fieldVal(f, '.tr-title-in'),
    summary: fieldVal(f, '.tr-sum-in'),
    started_at: fieldVal(f, '.tr-date') ? fieldVal(f, '.tr-date') + 'T12:00:00+00:00' : '',
  });
  btn.disabled = false;
  btn.textContent = was;
  // The paste is NEVER cleared on failure -- it may be the only copy the operator has in hand,
  // and re-copying a long note out of another app is not a thing to make anyone do twice.
  if (!r.ok) { msg.textContent = r.message || 'Could not save that.'; return; }
  TR_OPEN.delete(f.getAttribute('data-cid'));
  refresh();
}

//: The body is not on the wire — `/api/status` carries summaries and a LENGTH so the panel can
//: say how long a call was without shipping 40 KB per contact every 2.5 seconds.
//: How long the call was, in words a human reads. `Math.round(len/1000)+'k'` printed **0k** for
//: every transcript under 500 characters — a number that says nothing about a real meeting and
//: reads as an error. Under a thousand characters the honest unit is characters.
function transcriptSize(len) {
  const n = Number(len) || 0;
  if (!n) return 'no text stored';
  if (n < 1000) return `${n} chars`;
  if (n < 10000) return `${(n / 1000).toFixed(1)}k`;
  return `${Math.round(n / 1000)}k`;
}

async function showTranscript(id, btn) {
  btn.disabled = true;
  const r = await post('/api/contact/transcript-body', {id});
  btn.disabled = false;
  if (!r || r.ok === false) { alert((r && r.message) || 'Could not load it.'); return; }
  const box = btn.closest('.tr-row');
  const already = box.parentNode.querySelector('.tr-full');
  if (already) already.remove();
  const pre = document.createElement('pre');
  pre.className = 'tr-full';
  pre.textContent = r.body || '';
  box.parentNode.insertBefore(pre, box.nextSibling);
}

async function dropTranscript(id, cid) {
  if (!confirm('Remove this transcript from this contact?\n\nIf nobody else is attached to it, the transcript is deleted.')) return;
  const r = await post('/api/contact/transcript-delete', {id, contact_id: cid});
  if (!r.ok) alert(r.message || 'Could not remove it.');
  refresh();
}

function engagementLog(c) {
  const rows = (c.interactions || []).map(r => `
    <div class="ix-row ${esc(r.kind)}">
      <span class="ix-icon">${r.icon}</span>
      <span class="ix-label">${esc(r.label)}</span>
      <span class="ix-when">${esc(shortDate(r.at))}</span>
      ${r.detail ? `<span class="ix-detail">${esc(r.detail)}</span>` : ''}
      ${r.source === 'manual' ? `<span class="ix-manual" title="Logged by you, not detected">noted</span>` : ''}
    </div>`).join('');
  return `<div class="ix-block">
      <div class="d-label">Engagement${c.engaged ? '' : ' <span class="ix-none">nothing yet</span>'}</div>
      ${rows || `<div class="ix-row empty">Replies, booked calls and intro-deck opens appear here by themselves.</div>`}
      <div class="ix-log">
        <button class="linklike" onclick="logInteraction('${esc(c.id)}','profile_view')">🔗 Note: they viewed my LinkedIn</button>
        <span class="ix-why">LinkedIn does not export profile views or email about them, so this one is by hand.</span>
      </div>
    </div>`;
}

// The personalisation input, placed where you are already standing. "Copy note + open LinkedIn"
// puts you ON their profile — this is the box for what you see there. Deliberately NOT scraped:
// reading LinkedIn programmatically was abandoned twice (§Lessons 3), it risks the account the
// whole outreach ladder runs on, and five seconds of human judgement beats "posted about X three
// days ago". Kept separate from Notes, which is scratch and would be noise in a prompt.
function noticedBox(c) {
  const has = (c.noticed || '').trim();
  return `<div class="noticed" data-cid="${esc(c.id)}">
      <div class="d-label">Anything you noticed?${has ? ' <span class="noticed-on">✓ in the draft</span>' : ''}</div>
      <textarea class="c-noticed" rows="2"
        placeholder="A recent post, a talk, a shared background — whatever you'd mention if you knew them. Used in the draft; left out if it doesn't fit.">${esc(c.noticed)}</textarea>
      <div class="dbtns">
        <button onclick="saveNoticed('${esc(c.id)}', this)">Save</button>
        <button class="secondary" onclick="regenDraft('${esc(c.id)}', this)">Rewrite the email with it</button>
      </div>
    </div>`;
}
async function saveNoticed(cid, btn) {
  const box = btn.closest('.noticed');
  const r = await post('/api/contact/details',
                       {contact_id: cid, noticed: box.querySelector('.c-noticed').value});
  btn.textContent = r.ok ? 'Saved ✓' : 'Failed';
  setTimeout(() => { btn.textContent = 'Save'; if (r.ok) refresh(); }, 900);
}

// LinkedIn-style initials avatar: 1–2 initials + a stable color derived from the name.
function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  const first = parts[0][0] || '';
  const last = parts.length > 1 ? parts[parts.length - 1][0] : '';
  return (first + last).toUpperCase();
}
const _AVATAR_COLORS = ['#0a66c2','#057642','#915907','#7a3e9d','#0e7490','#b45309','#9f1239','#3730a3'];
function avatarColor(name) {
  let h = 0; const s = String(name || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return _AVATAR_COLORS[h % _AVATAR_COLORS.length];
}
const NOTES_OPEN = new Set(); // contact ids whose phone/notes panel is expanded (survives refresh)
//: Explicitly COLLAPSED, which has to be recorded separately now that the default is open.
//: With one set, "never touched" and "closed by hand" are the same state — so a block that
//: defaults open would spring back open on the next 2.5s refresh and could never be shut.
const NOTES_CLOSED = new Set();
function onNotesToggle(el, cid) {
  if (el.open) { NOTES_OPEN.add(cid); NOTES_CLOSED.delete(cid); }
  else { NOTES_CLOSED.add(cid); NOTES_OPEN.delete(cid); }
}
function bulkBar(j) {
  const cs = j.contacts || [];
  const emailN = cs.filter(c => c.email && c.outreach_message && !c.emailed && c.email_status === 'verified').length;
  const emailBtn = (GMAIL_AVAIL && emailN)
    ? `<button class="bulk send" onclick="sendAllEmails(decodeURIComponent('${encodeURIComponent(j.url)}'), this)">Send all emails (${emailN})</button>`
    : `<button class="bulk" disabled title="${GMAIL_AVAIL ? 'No verified emails ready' : 'Connect Gmail first'}">Send all emails (${emailN})</button>`;
  // LinkedIn is per-contact "Compose" (you click Send) — no bulk, since each compose
  // navigates the one browser away from the previous unsent invite.
  // Repair the whole application at once. Per-contact fetch is fine when you know WHICH
  // conversation is wrong; when something broke you usually do not, and checking six contacts
  // one at a time is how you stop checking.
  const syncN = cs.filter(c => c.email).length;
  const syncBtn = (CONTENT_SCOPE && syncN)
    ? `<button class="bulk" title="Search Gmail for every conversation with all ${syncN} contact(s) here" onclick="syncAllGmail(decodeURIComponent('${encodeURIComponent(j.url)}'), this)">⟳ Fetch all from Gmail</button>`
    : '';
  return `<div class="bulkbar">${emailBtn}${syncBtn}<span class="li-hint">LinkedIn: use “Compose on LinkedIn” per contact →</span><span class="bulknote" data-bulk="${esc(j.url)}"></span></div>`;
}
async function syncAllGmail(url, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  const note = document.querySelector(`.bulknote[data-bulk="${CSS.escape(url)}"]`);
  const say = t => { if (note) note.textContent = t; };
  const job = (LAST_JOBS || []).find(j => j.url === url) || {};
  const targets = (job.contacts || []).filter(c => c.email);
  let threads = 0, msgs = 0, failed = 0;
  for (let i = 0; i < targets.length; i++) {
    btn.textContent = `Fetching ${i + 1}/${targets.length}…`;
    // Sequentially, not in parallel: this hits the Gmail API once per contact and a burst of
    // six concurrent searches is how a personal token starts getting rate-limited.
    const r = await post('/api/contact/sync-gmail', {contact_id: targets[i].id});
    if (r.ok) { threads += r.threads || 0; msgs += r.messages || 0; } else { failed++; }
  }
  say(`Checked ${targets.length} contact(s): ${threads} conversation(s), ${msgs} new message(s)`
      + (failed ? `, ${failed} failed` : ''));
  btn.disabled = false; btn.textContent = label;
  refresh();
}
async function sendAllEmails(url, btn) {
  if (!confirm('Send ALL verified-email drafts for this company now?')) return;
  btn.disabled = true; btn.textContent = 'Sending…';
  const r = await post('/api/outreach/send-all-emails', {job_url: url});
  const note = document.querySelector(`.bulknote[data-bulk="${cssEsc(url)}"]`);
  if (note) note.textContent = r.message || '';
  if (r.ok) setTimeout(refresh, 2500); else { btn.disabled = false; btn.textContent = 'Send all emails'; alert(r.message||'Failed'); }
}
function cssEsc(s){ return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/["\\\]]/g,'\\$&'); }
async function saveDraft(cid, btn) {
  const d = btn.closest('.draft');
  const r = await post('/api/outreach', {contact_id: cid, subject: fieldVal(d, '.d-subj'),
    body: fieldVal(d, '.d-body'), linkedin: fieldVal(d, '.d-linkedin')});
  // Checking r.ok matters: `_save_or_regen_draft` returns ok:false for an unknown contact, and
  // this said "Saved ✓" to that too.
  btn.textContent = r && r.ok === false ? 'Failed' : 'Saved ✓';
  setTimeout(()=>btn.textContent='Save', 1200);
}
async function regenDraft(cid, btn) {
  const d = btn.closest('.draft');
  const style = d && d.querySelector('.d-style') ? d.querySelector('.d-style').value.trim() : '';
  btn.disabled = true; btn.textContent = 'Drafting…';
  const r = await post('/api/outreach', {contact_id: cid, regenerate: true, style});
  btn.disabled = false; btn.textContent = 'Regenerate';
  if (r.ok) {
    // Email fields only exist for contacts with an email; null-check (LinkedIn-only contacts).
    const subj = d.querySelector('.d-subj'); if (subj) subj.value = r.subject;
    const body = d.querySelector('.d-body'); if (body) body.value = r.body;
    const ln = d.querySelector('.d-linkedin');
    if (ln && r.linkedin != null) { ln.value = r.linkedin; updCount(ln); }
  } else alert(r.message || 'Failed');
}
function copyDraft(btn) {
  const d = btn.closest('.draft');
  const text = `Subject: ${d.querySelector('.d-subj').value}\n\n${d.querySelector('.d-body').value}`;
  navigator.clipboard.writeText(text); btn.textContent = 'Copied ✓'; setTimeout(()=>btn.textContent='Copy', 1200);
}
function materialLinks(materials) {
  if (!materials || !materials.length) return '';
  return materials.map(m => `<a href="${esc(m.url)}" target="_blank">${esc(m.label)}</a>`).join(' · ');
}
function renderProgress(progress, stats) {
  const p = progress || {};
  const pct = Math.max(0, Math.min(100, Number(p.percent || 0)));
  document.getElementById('progressLabel').textContent = p.label || 'Idle';
  document.getElementById('progressPercent').textContent = `${pct}%`;
  document.getElementById('progressFill').style.width = `${pct}%`;
  const jobs = p.in_progress_jobs || [];
  const active = p.running ? `<span class="job-chip"><span class="pulse"></span>${esc(p.in_progress || 0)} in progress</span>` : `<span class="job-chip">Idle</span>`;
  const ready = `<span class="job-chip">${esc(stats.ready || 0)} ready</span>`;
  const applied = `<span class="job-chip">${esc(stats.applied || 0)} applied</span>`;
  const jobChips = jobs.map(j => `<span class="job-chip">${esc(j.company)} · ${esc(j.title)}</span>`).join('');
  document.getElementById('progressMeta').innerHTML = [active, ready, applied, jobChips].filter(Boolean).join('');
}
// The 2.5s refresh replaces #jobs wholesale, which would discard whatever you are
// mid-way through typing (a phone number pasted from Apollo, an edited draft). Hold the
// re-render while a field in that subtree has focus; it resumes as soon as you click away.
function isEditingJobs() {
  const el = document.activeElement;
  if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.closest('#jobs')) return true;
  // EDIT-1. An open inline editor holds the refresh even before it takes focus. `startEdit`
  // re-renders and THEN focuses, so for one tick `activeElement` is still the old node — and a
  // refresh landing in that window would replace the input the operator just opened.
  if (EDITING || DESC_EDIT.size) return true;
  return hasSelectionInJobs();
}

//: Is the operator part-way through selecting something inside #jobs?
//:
//: Focus is not enough, and "I can't copy the text" is what that costs. A selection dragged
//: across a job description, a conversation transcript or a draft lives on the SELECTION, not on
//: `activeElement` — none of those are inputs, so the guard above returns false for all of them
//: and the next tick rebuilt the nodes out from under the drag. Inside a textarea it is the same
//: story once the pointer leaves the field.
//:
//: Only a non-collapsed range counts: a plain caret is `isCollapsed`, and treating that as
//: "busy" would freeze the table for anyone who merely clicked once.
function hasSelectionInJobs() {
  const sel = typeof getSelection === 'function' ? getSelection() : null;
  if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
  const jobs = document.getElementById('jobs');
  if (!jobs) return false;
  const within = n => !!(n && (n.nodeType === 1 ? n : n.parentElement)?.closest?.('#jobs'));
  return within(sel.anchorNode) || within(sel.focusNode);
}
const BASE_TITLE = 'ApplyPilot Operator';
// Jobs already acknowledged by looking at the tab. A badge that counts EVERY actionable row
// would be permanently lit — two stale needs_human rows would show "(2)" forever and train you
// to ignore it. The badge is for what is NEW since you last looked.
const NEEDS_SEEN = new Set();

// What "needs you" means, and it is exactly what the row's own Next action offers:
// a filled form waiting to be submitted, a blocker only a human can clear, or follow-ups due.
// ── What is outstanding, computed ONCE ──────────────────────────────────────
//
// The tab badge, the per-job Next button, the Follow-ups tab count and the header aggregator
// all have to agree. They did not: `needsYou` and `nextAction` summed email + LinkedIn and
// silently ignored SMS the moment that channel shipped, so a job whose only outstanding action
// was a text read as "nothing to do". §Lessons 21 — a derived number computed in two places is
// two numbers. Everything below reads `dueByChannel`.

//: Every follow-up ladder that is due on one job, per channel. Reads the payload keys
//: `followup_panel` emits, which are built from CHANNELS — so a fourth channel appears here
//: with no change, the same property the ladder engine has.
const FOLLOWUP_CHANNELS = [
  { key: '',      name: 'email',    icon: '✉',  label: 'email' },
  { key: 'li_',   name: 'linkedin', icon: '🔗', label: 'LinkedIn' },
  { key: 'sms_',  name: 'sms',      icon: '💬', label: 'text' },
];
function dueByChannel(j) {
  const f = j.followups || {};
  const out = {};
  let total = 0;
  for (const ch of FOLLOWUP_CHANNELS) {
    const n = f[`${ch.key}due_count`] || 0;
    out[ch.name] = n;
    total += n;
  }
  out.total = total;
  return out;
}

function needsYou(j) {
  if (j.interview_at) return false;          // arrived; nothing here needs chasing
  if (j.status === 'ready_to_submit' || j.status === 'needs_human') return true;
  if ((j.awaiting_reply || []).length) return true;   // somebody answered and is still waiting
  return dueByChannel(j).total > 0;
}

//: Every outstanding action across every application, grouped and ordered by what should be
//: done first. Ordering is the whole value: a flat count of 31 tells you nothing, and a list
//: that puts "3 LinkedIn invites left" above "someone replied 4 days ago" is actively harmful.
//: A human who wrote to you outranks every ladder (§Lessons 27).
function pendingActions(jobs) {
  const js = jobs || [];
  const g = (key, icon, label, urgent) => ({ key, icon, label, urgent, n: 0, jobs: [] });
  const groups = [
    g('replies',   '💬', 'waiting on your reply',   true),
    g('stalled',   '🕓', 'live threads gone quiet',  true),
    g('submit',    '📋', 'filled, ready to submit', true),
    g('human',     '⚠',  'need you at the keyboard', true),
    g('fill',      '▶',  'ready to fill',           false),
    g('followups', '↻',  'follow-ups due',          false),
    g('outreach',  '✉',  'contacts not emailed',    false),
    g('contacts',  '🔍', 'no contacts found yet',   false),
    g('failed',    '✕',  'failed, need a decision', false),
  ];
  const by = Object.fromEntries(groups.map(x => [x.key, x]));
  const add = (key, j, n) => { if (n > 0) { by[key].n += n; by[key].jobs.push(j.url); } };

  for (const j of js) {
    // A rejected job has left the pipeline; an interviewing job has ARRIVED. Both are done
    // asking for work, and leaving either in the counter keeps the badge permanently lit.
    if (isClosed(j) || j.interview_at) continue;
    add('replies', j, (j.awaiting_reply || []).length);
    // A conversation that stalled outranks every cold ladder: they already engaged, which is
    // the hardest part, and letting it go quiet wastes the only thing outreach is for. Capped
    // at `unanswered < 2` — once two messages have gone unanswered the honest move is to stop,
    // not to keep the prompt lit forever.
    add('stalled', j, (j.contacts || []).filter(c => {
      const cv = c.conversation || {};
      return cv.stalled && (cv.unanswered || 0) < 2;
    }).length);
    if (j.status === 'ready_to_submit') add('submit', j, 1);
    if (j.status === 'needs_human') add('human', j, 1);
    if (j.status === 'ready') add('fill', j, 1);
    if (j.status === 'failed') add('failed', j, 1);
    add('followups', j, dueByChannel(j).total);
    if (!(j.contacts || []).length) add('contacts', j, 1);
    else {
      // Contacts found but never written to. From the checklist, which already knows the
      // denominator — recomputing it here is how the two would disagree.
      const step = ((j.checklist || {}).steps || []).find(s => s.key === 'emailed');
      if (step && step.state !== 'na') add('outreach', j, Math.max(0, (step.total || 0) - (step.done || 0)));
    }
  }
  // Per-channel breakdown for the follow-ups line, so "6 follow-ups due" can say which kind.
  const channels = FOLLOWUP_CHANNELS.map(ch => ({
    ...ch, n: js.reduce((a, j) => a + (isClosed(j) ? 0 : dueByChannel(j)[ch.name]), 0),
  })).filter(c => c.n > 0);

  const live = groups.filter(x => x.n > 0);
  return { total: live.reduce((a, x) => a + x.n, 0), groups: live, channels,
           urgent: live.filter(x => x.urgent).reduce((a, x) => a + x.n, 0) };
}

let TODO_OPEN = false;
function toggleTodo() {
  TODO_OPEN = !TODO_OPEN;
  renderTodo(LAST_JOBS || []);
}
// Jump to the first job with this kind of work outstanding, open on the right tab.
function gotoTodo(url, tab) {
  TODO_OPEN = false;
  PANEL_OPEN.add(url);
  if (tab) TAB_OPEN.set(url, tab);
  renderTodo(LAST_JOBS || []);
  rerenderJobs();
  const row = document.getElementById('jobs');
  if (row && row.scrollIntoView) row.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

const _TODO_TAB = { replies: 'people', stalled: 'people', followups: 'followups', outreach: 'people',
                    contacts: 'people' };

function renderTodo(jobs) {
  const p = pendingActions(jobs);
  const btn = document.getElementById('todoBtn');
  const count = document.getElementById('todoCount');
  const panel = document.getElementById('todoPanel');
  const list = document.getElementById('todoList');
  if (!btn || !count || !panel || !list) return p;

  count.textContent = p.total;
  // Amber only when something is genuinely time-sensitive. A badge that is permanently lit
  // trains you to ignore it — the same reason the tab badge counts what is NEW (CRM-3a).
  btn.classList.toggle('urgent', p.urgent > 0);
  btn.classList.toggle('idle', p.total === 0);
  btn.setAttribute('aria-expanded', TODO_OPEN ? 'true' : 'false');
  panel.hidden = !TODO_OPEN;

  if (!TODO_OPEN) return p;
  if (!p.total) {
    list.innerHTML = `<div class="todo-empty">Nothing waiting. Every application is up to date.</div>`;
    return p;
  }
  list.innerHTML = p.groups.map(x => {
    const first = x.jobs[0];
    const sub = x.key === 'followups' && p.channels.length > 1
      ? `<div class="todo-sub">${p.channels.map(c => `${c.icon} ${c.n} ${c.label}`).join(' · ')}</div>`
      : '';
    const where = x.jobs.length > 1 ? ` <span class="todo-where">across ${x.jobs.length} jobs</span>` : '';
    return `<button class="todo-row${x.urgent ? ' urgent' : ''}"
      onclick="gotoTodo(${tagArg(first)},'${_TODO_TAB[x.key] || ''}')">
      <span class="todo-n">${x.n}</span>
      <span class="todo-label">${x.icon} ${esc(x.label)}${where}</span>${sub}</button>`;
  }).join('');
  return p;
}

// ── sign-in walls ───────────────────────────────────────────────────────────
// A wall is per EMPLOYER, not per job: one Salesforce Workday account covers every Salesforce
// job forever. So this list stays short, and clearing it in one sitting is the difference
// between eight interruptions across a week and ten minutes once.
let ACCOUNTS_OPEN = false;
let LAST_ACCOUNTS = { blocking: [], ready: [] };

function toggleAccounts() { ACCOUNTS_OPEN = !ACCOUNTS_OPEN; renderAccounts(LAST_ACCOUNTS); }

function accountRow(a) {
  // "You have cookies for this site" is a HINT, never an answer — a Workday cookie is set by
  // viewing a job. Shown so the operator can settle it in one click, not used to decide.
  const hint = a.session_seen
    ? `<div class="acct-hint">You have visited this site before, so you may already have an account.</div>`
    : '';
  const why = a.kind === 'sso'
    ? `Requires signing in with your ${esc(a.vendor || 'provider')}.`
    : `Requires an account on their site before you can apply.`;
  const blocked = a.blocked ? ` <span class="acct-blocked">blocked ${a.blocked} application${a.blocked > 1 ? 's' : ''}</span>` : '';
  return `<div class="acct-row">
    <div class="acct-main">
      <div class="acct-name">${esc(a.label)}${blocked}</div>
      <div class="acct-why">${why}</div>${hint}
    </div>
    <div class="acct-actions">
      <button class="primary acct-btn" onclick="openAccount(${tagArg(a.realm)}, this)">🔐 Open sign-in</button>
      <button class="secondary acct-btn" onclick="haveAccount(${tagArg(a.realm)}, this)">✓ I have an account</button>
    </div>
  </div>`;
}

function renderAccounts(payload) {
  const bar = document.getElementById('accountsBar');
  if (!bar) return;
  LAST_ACCOUNTS = payload || { blocking: [], ready: [] };
  const blocking = LAST_ACCOUNTS.blocking || [];
  const ready = LAST_ACCOUNTS.ready || [];
  // Nothing known at all — say nothing. An empty panel about a problem you do not have is
  // noise, and noise is what makes a real warning ignorable.
  if (!blocking.length && !ready.length) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.classList.toggle('blocking', blocking.length > 0);

  const head = blocking.length
    ? `🔐 <strong>${blocking.length} employer${blocking.length > 1 ? 's need' : ' needs'} an account</strong>
       before ${blocking.length > 1 ? 'their' : 'its'} jobs can run`
    : `🔐 Sign-ins: <strong>${ready.length}</strong> employer${ready.length > 1 ? 's' : ''} set up`;

  if (!ACCOUNTS_OPEN) {
    bar.innerHTML = `<button class="acct-summary" onclick="toggleAccounts()">
      <span>${head}</span>
      <span class="acct-open">${blocking.length ? 'Set these up' : 'Manage'} →</span></button>`;
    return;
  }

  // The already-done list is worth showing: it is the evidence that the system read your
  // browser rather than asking you to re-answer something you had already handled.
  const readyList = ready.length
    ? `<div class="acct-ready"><span class="acct-ready-h">Already set up</span>
        ${ready.map(a => `<span class="acct-chip" title="${esc(a.evidence)}">✓ ${esc(a.label)}</span>`).join('')}</div>`
    : '';
  const none = !blocking.length
    ? `<div class="acct-none">Nothing is blocked. Every employer with a sign-in wall is set up.</div>`
    : '';

  bar.innerHTML = `
    <button class="acct-summary" onclick="toggleAccounts()">
      <span>${head}</span><span class="acct-open">Close ▲</span></button>
    <div class="acct-body">
      ${none}${blocking.map(accountRow).join('')}${readyList}
      <div class="acct-foot">
        <button class="linklike" onclick="syncAccounts(this)">↻ Re-scan browser</button>
        <button class="linklike" onclick="purgeCredentials(this)"
          title="Delete saved passwords and cards from the browser the apply agent drives. Cookies and sessions are kept.">🛡 Remove saved passwords from the apply browser</button>
      </div>
    </div>`;
}

async function openAccount(realm, btn) {
  btn.disabled = true; btn.textContent = 'Opening…';
  const r = await post('/api/accounts/open', { realm });
  alert(r.message || (r.ok ? 'Chrome is open.' : 'Failed'));
  btn.disabled = false; btn.textContent = '🔐 Open sign-in';
}

async function haveAccount(realm, btn) {
  btn.disabled = true; btn.textContent = 'Saving…';
  const r = await post('/api/accounts/have', { realm, have: true });
  if (r.ok) refresh();
  else { btn.disabled = false; btn.textContent = '✓ I have an account'; alert(r.message || 'Failed'); }
}

async function syncAccounts(btn) {
  btn.disabled = true; btn.textContent = 'Scanning…';
  const r = await post('/api/accounts/sync', {});
  alert(r.message || 'Done');
  btn.disabled = false; btn.textContent = '↻ Re-scan browser';
  refresh();
}

async function purgeCredentials(btn) {
  if (!confirm('Remove saved passwords, cards and autofill from the APPLY browser?\n\n'
             + 'Cookies and sessions are kept, so no sign-in wall has to be paid twice. '
             + 'Your normal Chrome profile is not touched.')) return;
  btn.disabled = true; btn.textContent = 'Removing…';
  const r = await post('/api/accounts/purge', {});
  alert(r.message || 'Done');
  btn.disabled = false;
  btn.textContent = '🛡 Remove saved passwords from the apply browser';
}

// Co-pilot apply ENDS by waiting for the operator, and the queue stays paused until they act.
// Nothing pulled them back to the tab — no sound, no notification, no badge — so a filled
// application sat until someone happened to look, and a restart eventually closed it.
function updateNeedsYouBadge(jobs) {
  const actionable = (jobs || []).filter(needsYou).map(j => j.url);
  const focused = typeof document.hasFocus === 'function' ? document.hasFocus() : true;
  if (focused) {
    // Looking at it counts as seeing it. Marks the CURRENT set, so a job that becomes
    // actionable later still raises the badge.
    actionable.forEach(u => NEEDS_SEEN.add(u));
  }
  for (const u of [...NEEDS_SEEN]) if (!actionable.includes(u)) NEEDS_SEEN.delete(u);
  const unseen = actionable.filter(u => !NEEDS_SEEN.has(u)).length;
  document.title = unseen ? `(${unseen}) \u26a0 ${BASE_TITLE}` : BASE_TITLE;
  return unseen;
}

// CRM-2. Every rate renders WITH its n, and a rate below the meaningful threshold shows the
// raw counts instead of a percentage — "1 of 3" is information, "33%" from the same three is a
// lie with a decimal point.
function rateRow(r) {
  const thin = !r.meaningful;
  const value = thin ? `${r.hits} of ${r.n}` : `${r.pct}% <span class="m-n">(${r.hits}/${r.n})</span>`;
  return `<div class="m-rate ${thin ? 'thin' : ''}"><span>${esc(r.label || '')}</span><span class="v">${value}</span></div>`;
}
function cut(title, rates) {
  if (!rates || !rates.length) return '';
  return `<div class="m-cut"><h4>${esc(title)}</h4>${rates.map(rateRow).join('')}</div>`;
}
function renderMetrics(mx) {
  const panel = document.getElementById('metricsPanel');
  if (!panel) return;
  if (!mx || !mx.funnel) { panel.hidden = true; return; }
  panel.hidden = false;
  const f = mx.funnel, o = mx.overall || {};

  const head = document.getElementById('metricsHeadline');
  if (head) {
    head.textContent = o.n
      ? (o.meaningful ? `${o.pct}% reply rate (${o.hits}/${o.n})`
                      : `${o.hits} repl${o.hits === 1 ? 'y' : 'ies'} from ${o.n} delivered`)
      : 'nothing sent yet';
  }

  const steps = (f.steps || []).map(s =>
    `<div class="m-step"><strong>${s.n}</strong><span>${esc(s.label)}</span></div>`).join('');
  // A bounce is a real leak in the funnel, not a non-answer: the mail never arrived. Shown
  // beside the stages so "emailed 33, replied 1" cannot quietly include sends that failed.
  const leak = f.bounced
    ? `<div class="m-step leak"><strong>${f.bounced}</strong><span>bounced</span></div>` : '';

  const ttr = mx.median_hours_to_reply;
  const notes = [];
  if (ttr != null) notes.push(`Median time to reply: <strong>${ttr}h</strong>.`);
  if (f.bounced) notes.push(`${f.bounced} email(s) never arrived — those addresses are excluded from every rate above.`);
  notes.push(`Rates need n\u2265${mx.min_meaningful_n} to be shown as a percentage.`);

  document.getElementById('metricsBody').innerHTML = `
    <div class="m-funnel">${steps}${leak}</div>
    <div class="m-cuts">
      ${cut('Warm vs cold', mx.by_layer)}
      ${cut('By verification confidence', mx.by_confidence)}
      ${cut('By follow-ups sent', mx.by_touch)}
    </div>
    <div class="m-note">${notes.join(' ')}</div>`;
}

// Renders the jobs table from a payload ALREADY IN HAND. Split out of refresh() so typing in
// the search box re-filters locally instead of refetching /api/status — that endpoint costs 50
// SQL statements, and putting it behind a keystroke is §Lessons 11 and 26 with a new trigger.
// ---- CO-1: one employer, many roles ----------------------------------------
//
// Two roles at one company are two APPLICATIONS and one RELATIONSHIP. The row stays the unit for
// the application — every control on it (the ⋯ menu, the four tabs, the status strip, Re-apply,
// the two documents) is written against one job, and §Lessons 43/89 is what moving them costs.
// What gets a header is the relationship: the same humans, counted ONCE.
//
// Collapsed state survives the 2.5s rewrite of #jobs, like PANEL_OPEN and TAB_OPEN.
const CO_COLLAPSED = new Set();

//: Group the ALREADY-FILTERED rows. Grouping the whole set instead would print "3 roles" above
//: two visible ones whenever a filter hid the third — a header that describes rows it is not
//: sitting on top of.
//:
//: Order comes free. A Map keeps insertion order, so walking `shown` (already in the server's
//: ORDER BY) puts every group at its FIRST member's position — which is the best-member rule the
//: ticket asked for, without a second ranking that could disagree with the sort beside it.
//: Picking the worst instead would bury live work under a dead requisition.
function groupByEmployer(shown) {
  const groups = new Map();
  for (const j of shown) {
    const key = (j.company || '').trim().toLowerCase();
    // No employer, no group. A row whose company never resolved (§Lessons 85's "Uploaded", now
    // "") must not collect every other unresolved row into one meaningless bundle.
    if (!key) { groups.set(`__solo__${j.url}`, { name: '', jobs: [j] }); continue; }
    if (!groups.has(key)) groups.set(key, { name: j.company, jobs: [] });
    groups.get(key).jobs.push(j);
  }
  return [...groups.entries()].map(([key, g]) => ({ key, ...g }));
}

//: What is TRUE of the employer rather than of one posting. Every number here is deduplicated
//: across the roles, because the point of the header is that these are the same people: two rows
//: each showing "8 contacts" for the same eight humans is the misreading this exists to stop.
//:
//: Computed from the payload already on screen — no new query, and `/api/status` has six
//: statements of headroom (§The dashboard).
function employerStats(jobs) {
  const person = c => (c.email || '').trim().toLowerCase() || `id:${c.id}`;
  const seen = new Map();     // person -> Set of job urls they appear on
  const people = new Map();   // person -> the contact record (first seen wins)
  for (const j of jobs) {
    for (const c of (j.contacts || [])) {
      const k = person(c);
      if (!seen.has(k)) { seen.set(k, new Set()); people.set(k, c); }
      seen.get(k).add(j.url);
    }
  }
  const all = [...people.values()];
  const due = new Set();
  for (const j of jobs) for (const d of ((j.followups || {}).due || [])) due.add(d.id);
  return {
    roles: jobs.length,
    people: all.length,
    emailed: all.filter(c => c.emailed || c.submitted_at).length,
    replied: all.filter(c => c.replied_at || c.last_reply).length,
    due: due.size,
    // THE number this header exists for. One person on two roles is one person who can receive
    // the same pitch twice, and nothing else on the page can show it — each row shows its own
    // contacts and both look complete. See CO-1: `contact_id` hashes job_url, so they are
    // genuinely separate rows with separate ladders.
    shared: [...seen.values()].filter(s => s.size > 1).length,
  };
}

//: EVERY employer gets the band, not only the ones with two roles.
//:
//: It used to render at 2+ and the comment called a header over one row "furniture", which was
//: true of the STATS and false of the NAME: with one role the company appeared only as a small
//: grey subtitle under the job title, so scanning the board you read "Google" in the top-left of
//: one group and hunted for the employer on every other row. Consistent placement is worth more
//: than the line it costs.
//:
//: A single-role band is deliberately NOT the same control. There is no caret — collapsing one
//: row really is furniture — and the name is double-click editable, because banding the row
//: hides the subtitle that used to be its editor (§Lessons 97: a control three clicks away in
//: the Job tab is the same as absent). A multi-role band stays exactly as it was; which of the
//: two rows an edit there would write to has no answer.
function coHeadRow(g) {
  if (g.jobs.length === 1) return coSoloRow(g);
  const s = employerStats(g.jobs);
  const open = !CO_COLLAPSED.has(g.key);
  const bits = [`${s.roles} roles`];
  if (s.people) bits.push(`${s.people} ${s.people === 1 ? 'person' : 'people'}`);
  if (s.emailed) bits.push(`${s.emailed} emailed`);
  if (s.replied) bits.push(`${s.replied} replied`);
  if (s.due) bits.push(`${s.due} follow-up${s.due === 1 ? '' : 's'} due`);
  // Not a count among the others. It is a warning, and it reads as one.
  const dupe = s.shared
    ? `<span class="co-dupe" title="${s.shared} ${s.shared === 1 ? 'person is' : 'people are'} stored separately on more than one of these roles, each with their own follow-up ladder. They can receive the same pitch twice.">⚠ ${s.shared} on both</span>`
    : '';
  return `
    <tr class="co-head${open ? '' : ' co-shut'}">
      <td colspan="4">
        <button class="co-toggle" onclick="toggleCoGroup(${tagArg(g.key)})"
                aria-expanded="${open}"
                title="${open ? 'Collapse' : 'Expand'} the ${esc(g.name)} roles">
          <span class="co-caret">${open ? '▾' : '▸'}</span>
          <span class="co-name">${esc(g.name)}</span>
          <span class="co-stats">${bits.map(esc).join(' · ')}</span>
          ${dupe}
        </button>
      </td>
    </tr>`;
}

//: One role: the employer's name where every other employer's name is, and the stats that are
//: about the COMPANY rather than the posting. `roles` is dropped — "1 role" above one row is the
//: furniture the old 2+ threshold was right about.
function coSoloRow(g) {
  const j = g.jobs[0];
  const s = employerStats(g.jobs);
  const bits = [];
  if (s.people) bits.push(`${s.people} ${s.people === 1 ? 'person' : 'people'}`);
  if (s.emailed) bits.push(`${s.emailed} emailed`);
  if (s.replied) bits.push(`${s.replied} replied`);
  if (s.due) bits.push(`${s.due} follow-up${s.due === 1 ? '' : 's'} due`);
  return `
    <tr class="co-head co-solo">
      <td colspan="4">
        <div class="co-solo-inner">
          ${editable(j, 'company', j.company, 'co-name')}
          <span class="co-stats">${bits.map(esc).join(' · ')}</span>
        </div>
      </td>
    </tr>`;
}

function toggleCoGroup(key) {
  if (CO_COLLAPSED.has(key)) CO_COLLAPSED.delete(key); else CO_COLLAPSED.add(key);
  rerenderJobs();
}

//: `force` means the CALLER changed what should be on screen (an editor opened, a description
//: went into edit mode) and the edit guards must not veto showing it.
function renderJobsTable(allJobs, editing, force) {
  renderJobFilters(allJobs);
  renderActiveTags();
  // Bucket → band → tags → search, narrowing at each step. The bucket counts above deliberately
  // keep counting the WHOLE set: a filter pill that renumbers itself as you type cannot tell you
  // where the thing you are searching for lives.
  const shown = allJobs
    .filter(j => jobInBucket(j, JOB_FILTER))
    .filter(j => jobInTemp(j, TEMP_FILTER))
    .filter(jobMatchesTags)
    .filter(jobMatchesQuery);
  const emptyEl = document.getElementById('jobsEmpty');
  if (emptyEl) {
    emptyEl.hidden = shown.length > 0;
    // Say which filter emptied the table, and offer the way out. "No applications in All" is
    // the message a naive version prints while a search term is quietly hiding everything.
    // The band belongs in this list for the same reason: it ANDs with the bucket, so it can
    // empty the table on its own and leave the bucket pill looking like the culprit.
    const bits = [];
    if (JOB_QUERY.trim()) bits.push(`matching “${JOB_QUERY.trim()}”`);
    if (TEMP_FILTER !== 'all') bits.push(`marked ${TEMP_BANDS[TEMP_FILTER].label}`);
    if (TAG_FILTER.size) bits.push(`with ${TAG_FILTER.size} tag filter${TAG_FILTER.size > 1 ? 's' : ''}`);
    emptyEl.textContent = allJobs.length === 0 ? ''
      : bits.length ? `No applications ${bits.join(' ')}.`
      : `No applications in "${JOB_BUCKETS[JOB_FILTER].label}".`;
  }
  // The one destructive write: replacing #jobs discards whatever is being typed inside it.
  // Everything above has already run, so the header, badge and logs stay live while you type.
  //
  // Re-checked HERE rather than trusting the `editing` argument. `refresh()` computes that flag
  // BEFORE `await fetch(...)`, so it describes the page ~100ms ago: click into a draft during
  // that window and the guard says "not editing", the write lands, and the focus, selection and
  // scroll position all go. §Lessons 26's shape — the cheap check was in the right place for a
  // synchronous function and this one stopped being synchronous.
  if (!force && (editing || isEditingJobs())) return;
  // Grouped, but only where grouping says something. A header over one row is furniture, and a
  // collapsed group still has to be re-openable — so the header renders whatever the state is
  // and only the MEMBER rows come and go.
  const html = groupByEmployer(shown).map(g => {
    // A band needs a NAME, not a count. Rows whose employer never resolved (§Lessons 85's
    // "Uploaded", now "") each land in their own `__solo__` group with an empty name — banding
    // those would print an empty header over every one of them, so they keep the company
    // subtitle on the row instead, which is also the only place they can be given a name.
    const banded = !!(g.name || '').trim();
    const head = banded ? coHeadRow(g) : '';
    // Only a multi-role band collapses; a solo band has no caret, so it can never be in the set.
    if (banded && g.jobs.length > 1 && CO_COLLAPSED.has(g.key)) return head;
    return head + g.jobs.map(j => jobRows(j, banded)).join('');
  }).join('');
  // Nothing changed -> do not touch the DOM. This is the fix for "it keeps taking me back up
  // when I scroll" and for text being uncopyable, and both were the same cause: `innerHTML =`
  // destroys and rebuilds every node under #jobs, which resets each textarea's scrollTop to 0
  // and collapses any selection — every 2.5 seconds, forever. Scrolling a textarea does not
  // move `document.activeElement`, so the focus guard above never saw it.
  //
  // Measured on the live dashboard: two /api/status bodies 3s apart were IDENTICAL across
  // 1.65 MB except two `due_in_h` countdowns on one contact, which is an HOURLY change. So the
  // steady state re-rendered a byte-identical tree ~1,440 times an hour for nothing.
  //
  // A string compare of what we were about to write is the whole guard — no diffing library, no
  // keyed nodes, and it cannot go stale because it IS the output. When something really does
  // change the write still happens immediately.
  const el = document.getElementById('jobs');
  if (html === LAST_JOBS_HTML) return;
  LAST_JOBS_HTML = html;
  el.innerHTML = html;
  // A <details> restored with the `open` attribute does NOT fire `toggle` on parse, so the
  // 2.5s refresh would leave an already-open menu unpositioned. Re-measure them here.
  document.querySelectorAll('details.rowmenu[open]').forEach(positionRowMenu);
}

// ── Edit in place (EDIT-1) ──────────────────────────────────────────────────
//
// Double-click any of these to edit it. Global across templates on purpose: a row is a `jobs`
// row whatever the Space's shape, so a job-search card and a company card from a sheet get the
// same editor.
//
// The list is the SERVER's whitelist, not a second copy of it. `url` is absent from both and
// that is the point — it is the anchor, `contact_id` hashes it, and editing it would orphan
// every contact and ladder on the row without raising anything.
const EDITABLE = ['title', 'company', 'location', 'salary'];

//: Rows whose description is being edited. Outside the DOM like PANEL_OPEN, for the same
//: reason: the 2.5s refresh replaces #jobs wholesale.
const DESC_EDIT = new Set();
async function editDesc(url, on) {
  if (!on) { DESC_EDIT.delete(url); rerenderJobs(true); return; }
  // NEVER open on the excerpt. The row carries 900 characters and a real posting runs 4-10KB,
  // so seeding the textarea with what is on screen would silently truncate the description the
  // moment it was saved — a destructive edit that looks like a successful one.
  const job = (LAST_JOBS || []).find(x => x.url === url);
  const truncated = ((job && job.description) || '').length >= 900;
  if (truncated && !JOB_DESC.has(url)) {
    const r = await post('/api/job-description', {url});
    if (!r || r.ok === false) { alert((r && r.message) || 'Could not load the description.'); return; }
    JOB_DESC.set(url, r.description || '');
  }
  DESC_EDIT.add(url);
  JOB_DESC_OPEN.add(url);
  rerenderJobs(true);
}

//: Which cell is open right now, as `{url, field}`. Outside the DOM like PANEL_OPEN, because
//: the 2.5s refresh replaces #jobs wholesale and anything stored in a node is destroyed.
//:
//: An OBJECT, never a joined string that gets split back apart on commit. A url may contain a
//: space, so a split on one would hand the server a truncated anchor plus a "field" that was
//: really the tail of a path — and `set_fields` refuses an unknown field, so the operator's
//: edit would vanish citing a field they never touched.
let EDITING = null;
function editingIs(url, field) {
  return !!EDITING && EDITING.url === url && EDITING.field === field;
}

//: A span you can double-click, or the input once you have. Rendered by `jobRows`, so it comes
//: back correctly after every refresh.
function editable(j, field, value, cls) {
  // Refuses a field the server would refuse. Two lists that can drift is how a control ends up
  // offering an edit that always fails — better to render nothing than a box that cannot save.
  if (!EDITABLE.includes(field)) return esc(String(value == null ? '' : value));
  const v = String(value == null ? '' : value);
  if (editingIs(j.url, field)) {
    // The url and the field ride as SEPARATE attributes. The input is a real <input>, so the
    // existing focus guard holds the refresh while it is open.
    return `<input class="cell-edit ${cls}" data-edit="1"
      data-url="${esc(j.url)}" data-field="${esc(field)}" value="${esc(v)}"
      onkeydown="onEditKey(event)" onblur="commitEdit(this)">`;
  }
  const empty = v.trim() === '';
  // Same encoding the row menu uses for a url in a handler. A raw url inside an attribute
  // breaks on the first quote or backslash in it.
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  return `<div class="${cls} editable${empty ? ' is-empty' : ''}" ondblclick="startEdit(${u}, '${field}')"
    title="Double-click to edit">${empty ? esc(PLACEHOLDER[field] || 'Add') : esc(v)}</div>`;
}

//: What an empty field offers instead of nothing. A blank cell is not double-clickable in any
//: way the operator can see — §Lessons 43, where the control existed and was imperceptible.
const PLACEHOLDER = { title: 'Untitled — double-click', company: 'Add company',
                      location: 'Add location', salary: 'Add salary' };

//: An editable row in the Job tab's facts table. That table is where `location` and `salary`
//: get edited at all: they are otherwise only rendered as tag CHIPS, and both are empty on
//: every row in this database — so there is no chip to double-click, and the two tag types
//: have never appeared once. `row()` also hides a fact with no value, which would make an empty
//: field unreachable by construction.
function erow(j, label, field, value) {
  return `<div class="jd-row"><span class="jd-k">${esc(label)}</span>` +
         `<span class="jd-v">${editable(j, field, value, 'jd-edit')}</span></div>`;
}

function startEdit(url, field) {
  EDITING = {url, field};
  rerenderJobs(true);
  // Only one editor is ever open, so the freshly rendered input is the only `[data-edit]` on
  // the page — no selector has to be built out of the url, which is what the joined key needed.
  const el = document.querySelector('[data-edit]');
  if (el) { el.focus(); el.select(); }
}

function onEditKey(e) {
  if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }        // blur commits
  else if (e.key === 'Escape') { EDITING = null; rerenderJobs(true); }   // and discards
}

async function commitEdit(el) {
  const url = el.getAttribute('data-url') || '';
  const field = el.getAttribute('data-field') || '';
  const value = el.value;
  EDITING = null;
  // The description editor is a textarea keyed in DESC_EDIT rather than EDITING, and that Set
  // holds the refresh — leaving a url in it freezes the table.
  if (field === 'full_description') { DESC_EDIT.delete(url); JOB_DESC.set(url, value); }
  if (!url || !field) { rerenderJobs(true); return; }
  // Write it into LAST_JOBS before the request. `rerenderJobs()` renders from that, so without
  // this the cell snaps back to its old value for up to 2.5s and the edit reads as rejected —
  // §Lessons 21, and the same fix the 💡 flag needed.
  const job = (LAST_JOBS || []).find(x => x.url === url);
  // The payload calls it `description` and carries a 900-char EXCERPT of `full_description`.
  // Writing the full text into that key would make the row's cell disagree with every other
  // reader of it until the next refresh.
  const key = field === 'full_description' ? 'description' : field;
  const before = job ? job[key] : undefined;
  if (job) job[key] = field === 'full_description' ? value.slice(0, 900) : value;
  rerenderJobs(true);
  const r = await post('/api/job/edit', {url, [field]: value});
  if (!r || r.ok === false) {
    if (job && before !== undefined) job[key] = before;    // put it back; nothing was stored
    rerenderJobs(true);
    alert((r && r.message) || 'Could not save that.');
    return;
  }
  // Adopt what the SERVER stored rather than what was typed: it caps the length, and a silently
  // truncated value that the screen still shows in full is a disagreement the operator cannot
  // see (§Lessons 90's shape — a bound that does not say it is a bound).
  if (job && r.values && r.values[field] !== undefined)
    job[key] = field === 'full_description' ? r.values[field].slice(0, 900) : r.values[field];
  rerenderJobs(true);
}

//: The description cell, double-clickable like the title beside it.
//:
//: The ✎ button in the Job tab shipped first and was reported as "I still cannot edit any
//: descriptions" — it is three clicks away (open the row, switch to Job, scroll), while the
//: description the operator is looking at is right here in the table next to a job name that
//: double-clicks fine. §Lessons 89, for the third time this week.
//:
//: A textarea rather than the single-line editor: these run 4-10KB on a real posting.
function descCell(j) {
  if (DESC_EDIT.has(j.url)) {
    const full = JOB_DESC.get(j.url);
    return `<textarea class="desc-edit" data-edit="1"
      data-url="${esc(j.url)}" data-field="full_description"
      onkeydown="onDescKey(event)" onblur="commitEdit(this)"
      >${esc(full || j.description || '')}</textarea>`;
  }
  const empty = !String(j.description || '').trim();
  return `<div class="desc-text editable${empty ? ' is-empty' : ''}"
    ondblclick="editDesc(${`decodeURIComponent('${encodeURIComponent(j.url)}')`}, true)"
    title="Double-click to edit">${empty ? 'No description — double-click to add one'
      : esc(j.description)}</div>`;
}

//: Enter inserts a NEWLINE here, unlike the single-line fields — a description has paragraphs.
//: Escape discards, and Cmd/Ctrl+Enter commits without reaching for the mouse.
function onDescKey(e) {
  if (e.key === 'Escape') { DESC_EDIT.delete(e.target.getAttribute('data-url')); rerenderJobs(true); }
  else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); e.target.blur(); }
}

//: One job's two rows — the row itself and its `job-foot`. Extracted from `renderJobsTable`
//: unchanged so the grouping above has something to interleave headers with; `inGroup` only
//: adds the indent rail.
function jobRows(j, inGroup) {
  const co = inGroup ? ' co-member' : '';
  return `
    <tr class="${j.interview_at ? 'row-won' : (isClosed(j) ? 'row-closed' : '')}${co}">
      <td class="status-cell"><div class="status-head">${badge(j.status)}${j.interview_at ? ` <span class="won-chip" title="Scheduled ${esc(fmtDate(j.interview_at))}">${wonLabel(j).icon} ${esc(wonLabel(j).label.toLowerCase())}</span>` : ''}</div>${isClosed(j) && j.rejected_at ? `<div class="rejected-on">${esc(closedLabel(j.status))} ${fmtDate(j.rejected_at)}</div>` : (j.applied_at ? `<div class="applied-on">Applied ${fmtDate(j.applied_at)}</div>` : '')}</td>
      <td class="job-cell">${editable(j, 'title', j.title, 'job-title')}${inGroup ? '' : editable(j, 'company', j.company, 'job-co')}${matchedVia(j)}</td>
      <td class="desc">${descCell(j)}</td>
      <td class="tags-cell">${jobTags(j).map(t =>
        `<button class="tag-chip${TAG_FILTER.has(t.k) ? ' on' : ''}" onclick="event.stopPropagation();toggleTag(${tagArg(t.k)})" title="Filter by ${esc(t.value)}">${esc(t.label)}</button>`
      ).join('') || '<span class="tags-none">—</span>'}</td>
    </tr>
    <tr class="job-foot${co}"><td colspan="4">
      ${stepStrip(j)}
      ${PANEL_OPEN.has(j.url) ? jobTabs(j) + `<div class="pane">${jobPane(j)}</div>` : ''}
    </td></tr>`;
}

// Re-filter without hitting the network. LAST_JOBS is the payload the most recent refresh
// already fetched.
//: `force` distinguishes a DELIBERATE re-render from the 2.5s timer's.
//:
//: The edit guard exists to stop the timer destroying an open editor. Applied to a deliberate
//: render it does the opposite: `startEdit` sets EDITING, calls this, and `isEditingJobs()` is
//: now true BECAUSE of that — so the table bailed and the <input> was never written. The
//: feature was reported as "I click and nothing happens", and nothing was: the double-click
//: handler fired, the state changed, and the render that would have shown it refused to run.
function rerenderJobs(force) {
  renderJobsTable(LAST_JOBS || [], force ? false : isEditingJobs(), !!force);
}

// The aggregator counts the WHOLE set, never the filtered view. A search that hides a job
// does not mean its follow-up stopped being due — a counter that drops as you type is
// worse than none, because it reads as work disappearing.

async function refresh() {
  // NOTE: this used to `return` here, aborting the WHOLE refresh — stats, progress, the apply
  // log, the metrics panel and the (N) ⚠ tab badge all froze along with the jobs table. Leave
  // the cursor in a notes field and switch tabs, and the badge CRM-3a exists to raise never
  // appears. The guard belongs on the one write that would destroy what you are typing.
  const editing = isEditingJobs();
  // Which Space this response will be ABOUT. Captured before the await, because a refresh is in
  // flight for ~100ms and the 2.5s poller means there is almost always one.
  const asked = SPACE_ID;
  const data = await (await fetch(statusUrl())).json();
  // Drop a response for a Space we have since left. Every field in it is stale, not just this
  // one — the rows, the counts and the 🔔 aggregate all describe the panel you just navigated
  // away from.
  //
  // Found in a browser, not in a test: clicking a tab set SPACE_ID and called refresh(), and
  // then the poller's ALREADY-IN-FLIGHT response landed, said `space: 'job-search'`, and the
  // line below adopted it — so the URL read ?space=tmp-preview while the panel, the counts and
  // the highlighted tab all snapped back and STAYED back. Sticky, not transient, because
  // statusUrl() then kept asking for the old Space. No Node test can see this: they call the
  // renderers directly, with no second request racing the first.
  if (asked !== SPACE_ID) return;
  // The server is the authority on which Space is on screen — it may have fallen back from an
  // id that does not resolve — so adopting its answer keeps `statusUrl()` and the highlighted
  // tab from disagreeing on the next tick.
  if (data.space) SPACE_ID = data.space;
  renderSpaceNav(data.spaces, data.space, data.space_note);
  SPACE_TEMPLATES = data.space_templates || SPACE_TEMPLATES;
  renderSpaceShape(data.space_shape, data.space_offer, data.space_offer_copy, data.space_voice);
  document.getElementById('appDir').textContent = data.app_dir;
  const s = data.stats || {};
  // Counters that mean something for the shape on screen. In a targets Space "Tailored",
  // "Covers" and "Ready" are structurally always 0 — there is nothing to tailor — and seven
  // permanently-zero boxes are the same furniture as a one-tab nav, teaching you to stop
  // reading the row. The target counts are derived from `data.jobs`, which is already loaded:
  // no query, so the budget does not move.
  const stats = SPACE_SHAPE === 'pipeline/targets'
    ? [['Targets', s.total],
       ['Contacted', (data.jobs || []).filter(j => (j.contacts || []).some(c => c.emailed)).length],
       ['Replied', (data.jobs || []).filter(j => (j.contacts || []).some(c => c.replied_at)).length],
       ['Booked', (data.jobs || []).filter(j => j.interview_at).length]]
    : [['URL Jobs',s.total],['URL Applied',s.applied],['Lifetime Applied',s.lifetime_applied],['Enriched',s.enriched],['User-approved',s.scored],['Tailored',s.tailored],['Covers',s.covers],['Ready',s.ready],['Errors',s.errors]];
  // The list is not "Applications" when its rows are companies you are pitching.
  const heading = document.getElementById('rowsHeading');
  if (heading) heading.textContent = SPACE_SHAPE === 'pipeline/targets' ? 'Targets' : 'Applications';
  document.getElementById('stats').innerHTML = stats.map(([k,v]) => `<div class="stat"><strong>${v||0}</strong><span>${k}</span></div>`).join('');
  renderProgress(data.progress, s);
  const c = data.command || {};
  document.getElementById('command').textContent = c.running ? `Running: ${c.name}` : (c.name ? `Last: ${c.name}, exit ${c.returncode}` : 'Idle');
  document.getElementById('cmdLog').textContent = (c.log || []).join('\n');
  document.getElementById('applyLog').textContent = [...(data.worker_log || []), '', ...(data.claude_log || [])].join('\n');
  updateNeedsYouBadge(data.jobs);
  renderMetrics(data.metrics);
  NET_AVAIL = !!data.networking_available;
  GMAIL_AVAIL = !!data.gmail_available;
  // The button that reads this is re-rendered by the contact card on every tick, so there is
  // nothing to sync by hand — and nothing to fight a click mid-toggle.
  ATTACH_DOCS = data.attach_docs !== false;
  CONTENT_SCOPE = !!data.content_scope;
  CONV_SNIPPET_MAX = data.snippet_max || 0;
  if (data.poll_every_s) POLL_EVERY_S = data.poll_every_s;
  const allJobs = data.jobs || [];
  // Kept for handlers that need the payload AFTER a click rather than during render — the
  // bulk Gmail fetch has to know which contacts a job has, and an inline onclick cannot be
  // handed an array.
  LAST_JOBS = allJobs;
  // The badge on the ✉ Follow-ups button, and the open panel's list if it is showing. Both read
  // LAST_JOBS, so they cannot drift from the table beside them.
  renderBulkDue();
  renderTodo(allJobs);
  renderAccounts(data.accounts);
  renderJobsTable(allJobs, editing);
  document.querySelectorAll('details.rowmenu[open]').forEach(positionRowMenu);
}
async function markInterview(url, btn) {
  // The confirm names the thing the operator is about to assert. "Mark an interview" on a
  // company you are pitching describes something that did not happen.
  const j = (LAST_JOBS || []).find(x => x.url === url) || {};
  const what = wonLabel(j).label.toLowerCase();
  if (!confirm(`Mark ${what} as scheduled?\n\nThe row greys out and every follow-up `
             + 'sequence for this job stops. Chasing someone after they agreed to meet is the '
             + 'one follow-up guaranteed to cost you something.')) return;
  btn.disabled = true;
  // Immediate acknowledgement. The refresh takes a moment and the row may be off-screen, so
  // without this the only feedback is a change the operator might not be looking at.
  btn.textContent = 'Saving…';
  const r = await post('/api/mark-interview', {url});
  if (r.ok) { btn.textContent = '🎯 Scheduled ✓'; refresh(); }
  else { btn.disabled = false; btn.textContent = '🎯 Interview'; alert(r.message || 'Failed'); }
}
async function unmarkInterview(url, btn) {
  btn.disabled = true;
  btn.textContent = 'Undoing…';
  const r = await post('/api/unmark-interview', {url});
  if (r.ok) refresh(); else { btn.disabled = false; alert(r.message || 'Failed'); }
}
async function markRejected(url, btn) {
  if (!confirm('Move this application to the rejected pile?')) return;
  return closeJob(url, btn, 'rejected');
}
// The posting went away — req pulled, hiring freeze, filled internally. Same terminal state as a
// rejection (row greys, sequences stop, no temperature reading) and a different FACT, so it is
// stored and counted separately. The confirm says which, because the two are one menu row apart
// and only one of them belongs in a funnel.
async function markCancelled(url, btn) {
  if (!confirm('Close this as removed / cancelled?\n\n' +
               'For a posting that was pulled, frozen, or filled internally. It stops every ' +
               'sequence like a rejection does, but it is NOT counted as one.')) return;
  return closeJob(url, btn, 'cancelled');
}
// A ghost job is not a cancelled one, and the confirm says which. Cancelled is bad luck;
// this is a listing that was never going to be filled — an evergreen requisition, a role
// reposted every few weeks, a board kept stocked to look like growth. Marking it records the
// SOURCE, which is the only thing a ghost job can teach.
async function markGhost(url, btn) {
  if (!confirm('Close this as a ghost job?\n\n' +
               'For a listing that was never a real opening — evergreen requisition, endless ' +
               'repost, or kept up for appearances. It stops every sequence like a rejection ' +
               'does, but it is NOT counted as one: nobody read it.')) return;
  return closeJob(url, btn, 'ghost');
}
// "I should not close out a job application without having texted and called some of the people
// whose contacts we found."
//
// ONE guard, on the single funnel all three closing actions already went through — rejected,
// cancelled and ghost. A copy per action is how a rule gets implemented at one of its call
// sites and quietly not at the others (§Lessons 49, which has fired here at two call sites, at
// three, and at seven).
//
// ADVISORY, and that is the deliberate half. It names what is unspent and asks once more; it
// never refuses. A req that was genuinely pulled has to be filable without first faking work
// nobody did, and the row does not know what the operator knows — the same correction the
// round-two panel needed when it was disabling a button for three true statements about what
// was merely cheapest (§Lessons 69). It is silent when somebody replied: the point of the
// outreach was a conversation, and one happened.
function closeGuard(url) {
  const j = (LAST_JOBS || []).find(x => x.url === url);
  const w = ((j || {}).coverage || {}).close_warning;
  if (!w || !w.warn) return true;
  const who = (w.names || []).length ? `\n\n${w.names.join(', ')}` : '';
  return confirm('You have not finished working this one:\n\n  · '
    + w.lines.join('\n  · ') + who
    + '\n\nClose it anyway?');
}
async function closeJob(url, btn, status) {
  if (!closeGuard(url)) return;
  btn.disabled = true;
  const r = await post('/api/mark-rejected', {url, status});
  if (r.ok) refresh(); else { btn.disabled = false; alert(r.message || 'Failed'); }
}
async function unmarkRejected(url, btn) {
  btn.disabled = true;
  const r = await post('/api/unmark-rejected', {url});
  if (r.ok) refresh(); else { btn.disabled = false; alert(r.message || 'Failed'); }
}
// The People toggle in the footer: the expandable contacts panel when contacts exist, or a
// Round two. ALWAYS available once contacts exist; the panel recommends, it does not gate.
// It reads the state of the first round — who replied, whose ladder is still running, who has
// never been written to — and says which of those is the cheaper next move, because buying
// strangers costs Apollo credits and finishing a started sequence does not.
//
// It spends Apollo credits, so the label says what it will do rather than being a bare verb.
function anotherRoundPrompt(j, cs) {
  if (!NET_AVAIL || !cs.length) return '';

  // Two earlier versions of this control were both unusable, in opposite directions. The first
  // returned '' unless every ladder was spent — correct behaviour, no feedback, reported as
  // "I'm not seeing the button" on a job whose sequences were simply still running (§Lessons
  // 41). The second rendered it disabled with the reason, which answers "why" but still refuses
  // a judgement call the operator is better placed to make than the row is. The reason survives;
  // the refusal does not.
  const answered = cs.filter(c => c.replied_at || (c.conversation || {}).state === 'awaiting_us');
  const spent = cs.filter(c => c.exhausted);
  const untouched = cs.filter(c => !c.exhausted && !c.emailed
                                   && !(c.dm_status === 'sent' || c.dm_status === 'manual'));
  const running = cs.length - spent.length - untouched.length - answered.length;

  let why = '', head = '';
  if (answered.length) {
    // Somebody is talking to you. Buying more strangers is rarely the next move (§Lessons 27).
    why = `${answered.map(c => esc(firstName(c.full_name))).join(', ')} ${answered.length > 1 ? 'are' : 'is'} waiting on you — worth answering before you buy more.`;
    head = `<b>Someone replied.</b>`;
  } else if (running > 0) {
    why = `${running} sequence${running > 1 ? 's are' : ' is'} still running. Finishing what you started is free; this is not.`;
    head = `<b>Sequences still running.</b>`;
  } else if (untouched.length) {
    why = `${untouched.length} contact${untouched.length > 1 ? 's have' : ' has'} never been written to. Cheaper to send those first.`;
    head = `<b>Some contacts are untouched.</b>`;
  } else {
    head = `<b>No response from any of the ${cs.length}.</b>`;
    why = 'Every follow-up has been sent and nobody replied.';
  }
  const ready = !why || (!answered.length && running === 0 && !untouched.length);
  const busy = j.network_running;
  // Only a search already in flight disables this. The three reasons above are ADVICE about
  // what is cheapest to do next, and advice does not belong on a disabled attribute — the
  // operator knows things the row does not (a hiring manager named in the posting, a team that
  // just reorganised, a first round that resolved to the wrong company entirely). Spending
  // Apollo credits is their call to make, so the panel says what it thinks and gets out of the
  // way. `ready` still drives the accent styling, so "now is the moment" stays visible.
  const dis = busy ? 'disabled' : '';
  const label = busy ? '⏳ looking for new people…' : '🔄 Find a new round of contacts';
  const what = 'Searches this company again, skipping everyone above, and drafts fresh outreach. Spends Apollo credits.';
  return `<div class="round2${ready ? ' ready' : ''}">
      <div class="round2-txt">${head} ${esc(why)}</div>
      <button class="secondary" ${dis}
        title="${ready ? what : esc(why) + ' — ' + what}"
        onclick="findContacts(decodeURIComponent('${encodeURIComponent(j.url)}'), true)">${label}</button>
      ${j.network_note && !busy ? `<div class="netnote">${esc(j.network_note)}</div>` : ''}
    </div>`;
}

// "Find contacts" action when there are none. Sits right next to Activity so both are obvious.
// Shown in the People tab when no contacts have been found yet.
function findContactsPrompt(j) {
  const running = j.network_running;
  const dis = (running || !NET_AVAIL) ? 'disabled' : '';
  const title = NET_AVAIL ? '' : 'Set APOLLO_API_KEY (paid plan) to enable';
  const label = running ? '⏳ finding contacts…' : '👥 Find contacts';
  let out = `<button class="find-link" ${dis} title="${title}" onclick="findContacts(decodeURIComponent('${encodeURIComponent(j.url)}'))">${label}</button>`;
  if (j.network_error) out += `<div class="neterr">${esc(j.network_error)}</div>`;
  // The note is the only place a COMPLETED-but-empty search shows up. /api/status has always
  // sent it and nothing rendered it, so a run that considered 5 people and dropped all 5 as
  // working elsewhere looked exactly like a button that never fired.
  else if (j.network_note && !running) out += `<div class="netnote">${esc(j.network_note)}</div>`;
  return out;
}
function fmtDate(iso) {
  try {
    const d = new Date(iso);
    const now = new Date();
    const opts = d.getFullYear() === now.getFullYear()
      ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
      : { year: 'numeric', month: 'short', day: 'numeric' };
    return d.toLocaleString([], opts);
  } catch { return iso; }
}
// Render a job's activity log as a compact timeline. Times shown local + short.
const STAGE_ICON = { enrich:'🔎', score:'◆', tailor:'📝', cover:'✉', pdf:'📄', apply:'🚀', outreach:'📧', system:'•' };
// ── Completion checklist ("did I actually work this job?") ──────────────────
// ── Status strip: always visible, so you never expand anything just to learn where
// you are. Same five steps as the checklist, laid out as a PATH — a ring gives you a
// percentage, a path gives you the step you're standing on.
const PANEL_OPEN = new Set();
function onPanelToggle(url) { if (PANEL_OPEN.has(url)) PANEL_OPEN.delete(url); else PANEL_OPEN.add(url); refresh(); }
const STEP_LABEL = { contacts:'Found', applied:'Applied', emailed:'Emailed',
                     linkedin:'LinkedIn', followup:'Follow up' };
function stepStrip(j) {
  const cl = j.checklist;
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const open = PANEL_OPEN.has(j.url);
  let steps = '';
  if (cl && cl.steps) {
    // The first step that isn't finished is where you are; everything before it is done.
    let currentFound = false;
    steps = cl.steps.map((s, i) => {
      let cls = 'sstep', mark = '·';
      // Follow-up is the one step whose TOTAL is "done + currently due" — see the checklist,
      // where it is built as `len(done), len(done) + len(due)` precisely so it reads 100% until
      // something actually comes due. So a gap HERE means work is overdue right now, not work
      // that arrives later, and it has to highlight wherever it sits in the path rather than
      // queue behind an earlier unfinished step.
      //
      // Reported on a job reading `! Emailed 9/10 — · LinkedIn 11/12 — · Follow up 4/9` with
      // ten follow-ups due: `Emailed` took the highlight for being first, so the only overdue
      // thing on the row was the one rendered grey. The Next button and the tab badge both said
      // 10 an inch away, which is §Lessons 56 — one row, two facts.
      //
      // `due` carries the same amber as `now` rather than a louder colour, because nearly every
      // applied job has follow-ups outstanding and an alarm state that is always on gets
      // ignored. The ↻ mark is what distinguishes late from in-flight.
      const overdue = s.key === 'followup' && s.state !== 'na' && s.total > s.done;
      if (s.state === 'done') { cls += ' done'; mark = '✓'; }
      else if (s.state === 'na') { cls += ' na'; mark = '–'; }
      else if (!currentFound) { cls += ' now'; mark = s.key === 'followup' ? '↻' : '!'; currentFound = true; }
      if (overdue) { cls += ' due'; mark = '↻'; }
      const count = s.total > 1 ? ` ${s.done}/${s.total}` : '';
      const arrow = i < cl.steps.length - 1 ? '<span class="sarrow"></span>' : '';
      return `<span class="${cls}" title="${esc(s.hint || s.label)}"><span class="mk">${mark}</span> ${esc(STEP_LABEL[s.key] || s.label)}${count}</span>${arrow}`;
    }).join('');
  }
  const na = nextAction(j);
  const hint = nextHint(j);
  return `<div class="strip">
      <button class="strip-toggle" onclick="onPanelToggle(${u})" title="${open ? 'Collapse' : 'Open details'}">${open ? '▾' : '▸'}</button>
      <div class="steps">${steps}</div>
      ${tempChip(j)}${lastInteraction(j)}
      <div class="next">${na ? `<span class="next-label">Next</span>${na}` : `<span class="next-done">🏆 fully worked</span>`}${signinButton(j)}${interviewButton(j)}${restartButton(j)}${rowMenu(j)}</div>
    </div>${signinBar(j)}${hint ? `<div class="strip-hint">${hint}</div>` : ''}`;
}
// How the application is DOING, not how far it has travelled (UX-5).
//
// A dot AND a word, never colour alone: colour-blind readers and screenshots pasted into a
// document both have to survive. The `title` carries the sentence that produced the band —
// an unexplained colour stops being read within a week, which is §Lessons 43 applied to
// information rather than to controls.
function tempChip(j) {
  const t = j.temperature;
  if (!t || !t.band) return '';
  return `<span class="temp ${esc(t.band)}" title="${esc(t.reason)}">${t.icon} ${esc(t.label)}</span>`;
}

// When something last happened, and WHO did it (UX-3).
//
// On the COLLAPSED row on purpose. The strip says how far a job has travelled and never said
// when it last moved, and a state you must expand a job to discover goes unnoticed for days —
// which is the whole argument of §Lessons 27.
//
// Direction is the information. "You emailed them 6 days ago" and "they replied 6 days ago"
// are the same age and opposite situations: one is work done, the other is work owed. So
// inbound gets weight and outbound is muted, rather than both being a grey timestamp.
function lastInteraction(j) {
  const li = j.last_interaction;
  if (!li || !li.at) return '';
  return `<span class="lastix ${li.direction === 'in' ? 'in' : 'out'}"
    title="${esc(li.label)} — ${esc(fmtDate(li.at))}">${li.direction === 'in' ? '←' : '→'} ${esc(li.label)} · ${esc(agoShort(li.at))}</span>`;
}

// "2d", "3h", "just now" — a table row has no space for "2 days ago" next to everything else.
function agoShort(iso) {
  const t = Date.parse(iso);
  if (!t) return '';
  const mins = Math.max(0, (Date.now() - t) / 60000);
  if (mins < 60) return 'just now';
  const hrs = mins / 60;
  if (hrs < 24) return `${Math.floor(hrs)}h ago`;
  const days = hrs / 24;
  if (days < 30) return `${Math.floor(days)}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

// The ONE thing to do next, in priority order. Returns '' when the job is fully worked.
function nextAction(j) {
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const cl = j.checklist || {};
  const cs = j.contacts || [];
  if (isClosed(j)) return '';
  if (j.interview_at)
    return `<span class="won-next" title="Scheduled ${esc(fmtDate(j.interview_at))}">${
      wonLabel(j).icon} ${esc(wonLabel(j).done)}</span>`;
  if (j.status === 'ready')
    return `<button class="primary" onclick="fillOne(${u}, this)">▶ Fill application</button>`;
  if (j.status === 'ready_to_submit')
    return `<button class="primary" onclick="markSubmitted(${u}, this)">Mark submitted ✓</button>`;
  if (j.status === 'needs_human')
    return `<button class="primary" onclick="continueJob(${u}, this)">▶ Continue</button>`;
  if (j.status === 'failed')
    // What this button does belongs ON the button. It used to be appended to the failure line
    // under the strip, where it read as part of the error message.
    return `<button class="secondary" title="Regenerates the résumé and cover letter, then applies again from scratch." onclick="restartJob(${u}, this, false)">🔄 Restart end-to-end</button>`;
  if (!cs.length)
    return NET_AVAIL ? `<button onclick="findContacts(${u})">Find contacts</button>` : '';
  // A human who wrote to you outranks every ladder. Follow-ups chase people who said nothing;
  // this one already answered, and leaving them waiting wastes the only thing outreach is for.
  const waiting = j.awaiting_reply || [];
  if (waiting.length) {
    const w = waiting[0];
    // "0h" is not an age. Under an hour the row's own last-interaction line already says
    // "just now", and the button disagreeing with it reads as two different facts.
    const ago = w.days >= 1 ? `${w.days}d` : (w.hours >= 1 ? `${w.hours}h` : 'just now');
    const more = waiting.length > 1 ? ` +${waiting.length - 1}` : '';
    return `<button class="primary" onclick="openReply(${u},'${esc(w.id)}')">💬 Answer ${esc(firstName(w.full_name))} (${ago})${more}</button>`;
  }
  // A stalled live thread beats a cold ladder. Someone who answered once and went quiet is a
  // better prospect than three strangers who never answered at all.
  const quiet = (j.contacts || []).filter(c => {
    const cv = c.conversation || {};
    return cv.stalled && (cv.unanswered || 0) < 2;
  });
  if (quiet.length) {
    const q = quiet[0], cv = q.conversation || {};
    const more = quiet.length > 1 ? ` +${quiet.length - 1}` : '';
    // openReply, not a bespoke opener: a stalled thread needs the composer focused, which is
    // exactly what answering a live reply needs. The action is the same; only the reason differs.
    return `<button class="amber" onclick="openReply(${u},'${esc(q.id)}')">🕓 ${esc(firstName(q.full_name))} went quiet ${cv.days}d ago${more}</button>`;
  }
  const due = dueByChannel(j);
  if (due.total) {
    // Name the channel when only one kind is outstanding — "1 text due" is an instruction,
    // "1 follow-up due" makes you open the tab to find out which.
    const only = FOLLOWUP_CHANNELS.filter(c => due[c.name] > 0);
    const what = only.length === 1 ? `${only[0].icon} ${due.total} ${only[0].label}` : `↻ ${due.total} follow-up`;
    return `<button class="amber" onclick="openTab(${u},'followups')">${what}${due.total>1?'s':''} due</button>`;
  }
  const step = (cl.steps || []).find(s => s.state === 'todo' || s.state === 'partial');
  if (step && step.key === 'emailed')
    return `<button class="primary" onclick="openTab(${u},'people')">✉ Email ${step.total - step.done} more</button>`;
  if (step && step.key === 'linkedin')
    return `<button onclick="openTab(${u},'people')">🔗 ${step.total - step.done} LinkedIn invite${step.total-step.done>1?'s':''} left</button>`;
  return '';
}
function openTab(url, tab) { PANEL_OPEN.add(url); TAB_OPEN.set(url, tab); refresh(); }

// ── One panel with tabs, replacing four sibling accordions ──────────────────
const TAB_OPEN = new Map();
function activeTab(j) {
  const t = TAB_OPEN.get(j.url);
  if (t) return t;
  // Summary is the default: "every card as soon as you open it should have its own summary
  // tab". It replaces `people` as the resting default and does NOT replace `followups` —
  // landing on work that is actually owed still beats landing on a description of it.
  return dueByChannel(j).total ? 'followups' : 'summary';
}
function jobTabs(j) {
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const cur = activeTab(j);
  const defs = [
    // Asked for as "every card as soon as you open it should have its own summary tab that's
    // not the email tab". FIRST, and the default — see `activeTab`. It is the one pane that
    // answers "where is this whole thing up to" without opening anyone.
    ['summary',   'Summary',    0, false],
    ['people',    'People',     (j.contacts || []).length, false],
    ['followups', 'Follow-ups', dueByChannel(j).total, dueByChannel(j).total > 0],
    ['materials', 'Materials',  (j.materials || []).length, false],
    ['activity',  'Activity',   (j.activity || []).length, false],
    // No 'interactions' tab. Engagement lives on the PERSON now (UX-1) — it answered
    // "has anyone engaged?" in a different room from the people, and had two rows to show
    // across 187 contacts. The ledger underneath it is kept: it is the only record of an
    // operator-noted event and the source UX-2 and UX-3 build on.
    ['job',       'Job',        0, false],
  ];
  return `<div class="tabs">` + defs.map(([k, label, n, due]) =>
    `<button class="tab ${cur === k ? 'on' : ''}" onclick="openTab(${u},'${k}')">${label}${n ? ` <span class="n ${due?'due':''}">${n}</span>` : ''}</button>`
  ).join('') + `</div>`;
}
// The posting itself: the links, and the facts that decide whether it is worth the effort.
// Everything here is already on the payload except the full description, which is fetched on
// demand — the list carries a 900-char excerpt, and shipping 8KB × every job on a 2.5s refresh
// to render a pane that is usually closed would be pure waste.
const JOB_DESC = new Map();     // job url -> full description, fetched once per session
const JOB_DESC_OPEN = new Set();

async function saveJobDescription(url, btn) {
  const box = btn.closest('.jd-paste').querySelector('.jd-paste-box');
  const text = (box.value || '').trim();
  btn.disabled = true; btn.textContent = 'Saving…';
  const r = await post('/api/job-description/save', {url, description: text});
  if (!r.ok) {
    btn.disabled = false; btn.textContent = 'Save description';
    alert(r.message || 'Could not save that.');
    return;
  }
  btn.textContent = 'Saved ✓';
  // Leave edit mode. `DESC_EDIT.size` holds the refresh, so a url left in it after a successful
  // save freezes the whole table permanently — the editor would stay open showing the old text
  // and nothing would ever update again.
  DESC_EDIT.delete(url);
  // Both caches carry the old text; the excerpt on the row comes back with the next payload.
  JOB_DESC.set(url, text);
  const job = (LAST_JOBS || []).find(x => x.url === url);
  if (job) job.description = text.slice(0, 900);
  refresh();
}
function jobDetail(j) {
  const link = (href, label, cls) => href
    ? `<a class="${cls}" href="${esc(href)}" target="_blank" rel="noopener">${label} ↗</a>` : '';
  const row = (label, value) => value
    ? `<div class="jd-row"><span class="jd-k">${esc(label)}</span><span class="jd-v">${value}</span></div>` : '';

  const applyDiffers = j.application_url && j.application_url !== j.url;
  const links = `<div class="jd-links">
      ${link(j.url, 'Open the posting', 'primary-link')}
      ${applyDiffers ? link(j.application_url, 'Application page', '') : ''}
    </div>`;

  // The URL in full, selectable. A truncated link you cannot copy is the reason this tab exists.
  const urls = row('Posting URL', `<code class="jd-url">${esc(j.url)}</code>`)
             + (applyDiffers ? row('Apply URL', `<code class="jd-url">${esc(j.application_url)}</code>`) : '');

  const score = j.fit_score != null
    ? `${j.fit_score}/10${j.reasoning ? ` <span class="jd-why">— ${esc(j.reasoning)}</span>` : ''}` : '';

  const open = JOB_DESC_OPEN.has(j.url);
  const full = JOB_DESC.get(j.url);
  const excerpt = (j.description || '').trim();
  // No description means the job is DEAD: tailor and cover both need it, and a row whose
  // detail_scraped_at is already stamped is never re-queued. Some postings are JavaScript-
  // rendered and return an empty shell to a plain fetch (Google's careers site is one), so the
  // escape hatch is the same one LinkedIn and SMS use — the human is already on the page.
  const desc = !excerpt
    ? `<div class="jd-paste">
         <div class="jd-paste-why">${j.detail_error
             ? esc(j.detail_error)
             : 'No description was read from this page.'}
           <b>Nothing else can run without one</b> — tailoring and the cover letter both need it.</div>
         <textarea class="jd-paste-box" rows="6"
           placeholder="Open the posting, select the whole description, and paste it here."></textarea>
         <div class="dbtns">
           <button class="primary" onclick="saveJobDescription(${
             `decodeURIComponent('${encodeURIComponent(j.url)}')`}, this)">Save description</button>
           ${link(j.url, 'Open the posting', '')}
         </div>
       </div>`
    : DESC_EDIT.has(j.url)
    // Editing an EXISTING description. The paste box above renders only when there is none, so
    // a job that scraped fine could never be corrected — which is half of what "I cannot edit
    // the description" meant. A textarea rather than the inline single-line editor because
    // these run 4-10KB.
    ? `<div class="jd-paste">
         <textarea class="jd-paste-box" rows="12">${esc(full || excerpt)}</textarea>
         <div class="dbtns">
           <button class="primary" onclick="saveJobDescription(${
             `decodeURIComponent('${encodeURIComponent(j.url)}')`}, this)">Save description</button>
           <button class="ghost" onclick="editDesc(${
             `decodeURIComponent('${encodeURIComponent(j.url)}')`}, false)">Cancel</button>
         </div>
         <div class="hint">Saving replaces the stored description.</div>
       </div>`
    : `<div class="jd-desc">${esc(open && full ? full : excerpt)}${
        !open && excerpt.length >= 900 ? '…' : ''}</div>
       <div class="dbtns">
       ${excerpt.length >= 900 ? `<button class="linklike" onclick="toggleJobDesc(${
         `decodeURIComponent('${encodeURIComponent(j.url)}')`}, this)">${
         open ? 'Show less' : 'Show the full description'}</button>` : ''}
       <button class="linklike" onclick="editDesc(${
         `decodeURIComponent('${encodeURIComponent(j.url)}')`}, true)">✎ Edit description</button>
       </div>`;

  return `<div class="jd">
    ${links}
    <div class="jd-facts">
      ${erow(j, 'Title', 'title', j.title)}
      ${erow(j, 'Company', 'company', j.company || j.contact_company)}
      ${erow(j, 'Location', 'location', j.location)}
      ${erow(j, 'Salary', 'salary', j.salary)}
      ${row('Fit', score)}
      ${row('Status', esc(j.status) + (j.applied_at ? ` · applied ${esc(fmtDate(j.applied_at))}` : ''))}
      ${row('Attempts', j.apply_attempts ? String(j.apply_attempts) : '')}
      ${urls}
    </div>
    ${contextBox(j)}
    <div class="jd-label">Description</div>
    ${desc}
  </div>`;
}

// CTX-2. What the operator knows and the scraper cannot. Everything else this tab shows about
// a job was read off the posting; these two boxes are the only place their own knowledge goes.
//
// A <details>, so it survives the 2.5s refresh through the same native mechanism the metrics
// panel uses — and OPEN by default when there is nothing in it yet, because a collapsed empty
// box is indistinguishable from no feature (§Lessons 43, six occurrences, every one reported
// as "it does nothing").
function contextBox(j) {
  // Typed-but-unsaved values live HERE, not in the DOM — the same reason `ADD_FORM` exists.
  // `isEditingJobs()` only holds the refresh off while a field HAS focus, so clicking from the
  // textarea to anything that is not an input hands the next 2.5s tick a paragraph to destroy.
  // A lost name is annoying; a lost paragraph is the feature failing at the moment it is used.
  const pending = CTX_FORM.get(j.url);
  const ctx = pending ? pending.context : (j.job_context || '');
  const ask = pending ? pending.ask : (j.job_ask || '');
  const u = j.context_use || {};
  const url = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const saved = !!((j.job_context || '').trim() || (j.job_ask || '').trim());
  const has = saved;
  const dirty = !!pending && (ctx !== (j.job_context || '') || ask !== (j.job_ask || ''));

  // Only ever reports UNSENT drafts as regenerable. A sent one is the record of what went out.
  let note = '';
  if (has && u.stale) {
    note = `<span class="ctx-stale">↻ ${u.stale} draft${u.stale === 1 ? '' : 's'} here ${
      u.stale === 1 ? 'was' : 'were'} written before this — regenerate ${
      u.stale === 1 ? 'it' : 'them'} from the People tab</span>`;
  } else if (has && u.used) {
    note = `<span class="ctx-on">✓ used in ${u.used} draft${u.used === 1 ? '' : 's'}</span>`;
  }
  if (dirty) note = '<span class="ctx-stale">● unsaved</span>';
  const sentNote = has && u.sent
    ? `<div class="hint">${u.sent} already sent — those are left exactly as they went out.</div>` : '';

  return `<details class="ctx" ${has && !dirty ? '' : 'open'}>
    <summary><span class="jd-label">Context for this application</span> ${note}</summary>
    <div class="ctx-body">
      <div class="d-label">What you know about this company and role</div>
      <textarea class="ctx-what" rows="3" oninput="onCtxField(${url}, this)"
        placeholder="Met their Head of Eng at a meetup; they're rebuilding intake on agents. Whatever you would mention if you already knew them.">${esc(ctx)}</textarea>
      <div class="d-label">What you want from this person</div>
      <textarea class="ctx-ask" rows="2" oninput="onCtxField(${url}, this)"
        placeholder="An intro to whoever owns the intake rebuild. Leave empty to ask for a call as usual.">${esc(ask)}</textarea>
      <div class="dbtns">
        <button class="primary" onclick="saveJobContext(${url}, this)">Save context</button>
        <span class="ctx-status hint"></span>
      </div>
      <div class="hint">Facts, not phrasing — this is rewritten for each person, never pasted.
        Saving does not redraft: existing drafts stay until you regenerate them.</div>
      ${sentNote}
    </div>
  </details>`;
}

// Unsaved context per job url, surviving the 2.5s rewrite of #jobs.
const CTX_FORM = new Map();

function onCtxField(url, el) {
  const wrap = el.closest('.ctx-body');
  CTX_FORM.set(url, {
    context: wrap.querySelector('.ctx-what').value || '',
    ask: wrap.querySelector('.ctx-ask').value || '',
  });
}

async function saveJobContext(url, btn) {
  const wrap = btn.closest('.ctx-body');
  const out = wrap.querySelector('.ctx-status');
  btn.disabled = true;
  const r = await post('/api/job/context', {
    job_url: url,
    context: wrap.querySelector('.ctx-what').value || '',
    ask: wrap.querySelector('.ctx-ask').value || '',
  });
  btn.disabled = false;
  out.textContent = r.message || '';
  // Only on success. Dropping the buffer after a failed save discards the paragraph AND leaves
  // the box rendering the stale server copy, so the operator loses work and cannot see that
  // they did.
  if (r.ok) CTX_FORM.delete(url);
  refresh();
}
async function toggleJobDesc(url, btn) {
  if (JOB_DESC_OPEN.has(url)) { JOB_DESC_OPEN.delete(url); refresh(); return; }
  if (!JOB_DESC.has(url)) {
    btn.disabled = true; btn.textContent = 'Loading…';
    const r = await post('/api/job-description', {url});
    if (r.ok) JOB_DESC.set(url, r.description || '');
    btn.disabled = false;
  }
  JOB_DESC_OPEN.add(url);
  refresh();
}

// What people have actually DONE, per person. The pieces existed but each lived somewhere
// else — sends on the contact, replies in the thread, deck clicks in three columns — so
// "has anyone engaged?" meant opening four panels and holding the answer in your head.
//
// People with NO engagement are listed too, and say so. A tab showing only the people who did
// something cannot answer "has anyone?", which is the question being asked.
async function logInteraction(cid, kind) {
  const detail = kind === 'profile_view'
    ? prompt('Anything to remember? (optional — e.g. "saw it in LinkedIn notifications")') : '';
  if (detail === null) return;                 // cancelled
  const r = await post('/api/contact/interaction', {contact_id: cid, kind, detail});
  const el = document.getElementById('command');
  if (el) el.textContent = r.message || '';
  refresh();
}

// ── Summary: the whole job at a glance, in plan order ───────────────────────
// The three asks that turned out to be one computation — the ordered sequence, the summary
// pane, and the close guard — all read `j.coverage`, so they cannot disagree about how far a
// job has been worked. Derived server-side from rows and ladders that were already loaded, at
// a cost of zero queries and zero network calls.
// The channels the SEQUENCE runs on, in plan order. LinkedIn is deliberately absent from the
// track: it has no ladder (the invite is the whole channel), so a marker for it would sit
// permanently at 1/1 and imply a step that is never owed.
const SEQ_CHANNELS = [
  {key: 'email', icon: '✉', label: 'email'},
  {key: 'sms',   icon: '💬', label: 'text'},
  {key: 'call',  icon: '📞', label: 'call'},
];

// How long ago, in words. Time was the whole thing missing from the first version of this pane:
// four rows in five read "waiting" with no hint of what they were waiting for or for how long.
function agoWords(iso) {
  if (!iso) return '';
  const h = (Date.now() - new Date(iso).getTime()) / 36e5;
  if (!isFinite(h) || h < 0) return '';
  if (h < 1) return 'just now';
  if (h < 24) return `${Math.round(h)}h ago`;
  const d = Math.round(h / 24);
  return d < 14 ? `${d}d ago` : `${Math.round(d / 7)}w ago`;
}
function inWords(h) {
  if (h == null) return '';
  if (h <= 0) return 'now';
  if (h < 24) return `in ${Math.round(h)}h`;
  return `in ${Math.round(h / 24)}d`;
}

function summaryPane(j) {
  const cov = j.coverage;
  if (!cov || !cov.people) {
    return `<div class="pane-empty">Nobody has been found for this job yet.
      ${findContactsPrompt(j)}</div>`;
  }
  const rows = cov.rows || [];
  // Grouped by whether the row is asking for something. A flat list makes the one person who
  // needs you look exactly like the four who do not — which is the failure the 🔔 counter
  // exists to prevent, reproduced one level down. Headers only appear when both groups are
  // non-empty, so the ordinary case is not wrapped in furniture that says nothing.
  const act = rows.filter(r => r.replied || r.next);
  const idle = rows.filter(r => !(r.replied || r.next));
  const group = (label, n, list, cls) => !list.length ? ''
    : `${(act.length && idle.length) ? `<div class="sum-g ${cls}">${label}
        <span class="sum-g-n">${list.length}</span></div>` : ''}`
      + list.map(r => sumRow(j, r)).join('');

  return `<div class="sum">
      ${sumHeader(cov)}
      ${group('Needs you', act.length, act, 'do')}
      ${group('In sequence', idle.length, idle, '')}
      ${closeNotice(j)}
    </div>`;
}

// The job's own progress along the plan, as a segmented bar — one segment per stage, filled by
// how many people have cleared it. It replaces four count chips that said what had been SENT
// and never what it added up to.
function sumHeader(cov) {
  const rows = cov.rows || [];
  const n = rows.length || 1;
  const at = i => rows.filter(r => r.stage.index > i || r.stage.key === 'replied'
                                   || r.stage.key === 'done').length;
  const segs = STAGE_LABELS.map((label, i) => {
    const pct = Math.round(100 * at(i) / n);
    return `<div class="sum-seg" data-tip="${esc(label)} — ${at(i)} of ${n} past this"
        aria-label="${esc(label)}">
        <div class="sum-seg-bar"><i style="width:${pct}%"></i></div>
        <div class="sum-seg-l">${esc(label)}</div>
      </div>`;
  }).join('');
  const bits = [
    `${cov.people} ${cov.people === 1 ? 'person' : 'people'}`,
    `${cov.emails} email${cov.emails === 1 ? '' : 's'}`,
    cov.texts ? `${cov.texts} text${cov.texts === 1 ? '' : 's'}` : '',
    cov.calls ? `${cov.calls} call${cov.calls === 1 ? '' : 's'}` : '',
    cov.invites ? `${cov.invites} invite${cov.invites === 1 ? '' : 's'}` : '',
  ].filter(Boolean).join(' · ');
  return `<div class="sum-head">
      <div class="sum-h-l">${esc(bits)}</div>
      ${cov.replied ? `<span class="sum-s good">↩ ${cov.replied} replied</span>` : ''}
      ${cov.due ? `<span class="sum-s do">${cov.due} need${cov.due === 1 ? 's' : ''} you</span>` : ''}
    </div>
    <div class="sum-bar">${segs}</div>`;
}
const STAGE_LABELS = ['Emails', 'Text + call', 'Text + call again'];

// One person. Two lines rather than one: the name and where they are in the plan on top, who
// they are and what has actually happened underneath. The first version was a single row of
// four cells stretched across the table, so the eye crossed an inch of nothing to get from a
// name to its status.
function sumRow(j, r) {
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const track = SEQ_CHANNELS.map(c => {
    const ch = (r.channels || {})[c.key] || {};
    if (!ch.possible) {
      return `<span class="sq off" data-tip="no ${c.key === 'email' ? 'address' : 'number'}"
        aria-label="no ${c.key === 'email' ? 'address' : 'number'}">${c.icon}</span>`;
    }
    // A pip per PLANNED message, filled for each one actually sent. This is what makes the
    // pane read as a sequence rather than a list — "1 of 4 emails, no texts yet" is legible
    // without reading a number, and it is the same shape for every channel.
    const pips = Array.from({length: ch.planned}, (_, i) =>
      `<i class="${i < ch.sent ? 'on' : ch.due && i === ch.sent ? 'due' : ''}"></i>`).join('');
    return `<span class="sq" data-tip="${ch.sent} of ${ch.planned} ${esc(c.label)}${ch.planned === 1 ? '' : 's'} sent"
      aria-label="${ch.sent} of ${ch.planned} ${esc(c.label)}s sent">${c.icon}${pips}</span>`;
  }).join('');

  const when = r.replied ? agoWords(r.replied_at) || agoWords(r.last_at)
             : r.next ? '' : inWords(r.next_in_h);
  const status = r.replied
    ? `<span class="sum-st good">↩ they replied${when ? ' · ' + esc(when) : ''}</span>`
    : r.next
      ? `<span class="sum-st do">${esc(r.next.what)}</span>`
      // Names the MESSAGE, not just the clock. "next in 1d" was the same non-answer as the
      // "waiting" it replaced: it says a timer is running and nothing about what it will do.
      : `<span class="sum-st">${esc(r.next_label || 'next')}${when ? ' · ' + esc(when) : ''}</span>`;
  // The action, inline. A summary whose job is to drive work and offers nothing to click is a
  // report (§Lessons 43's family) — so the row carries the button for whatever it is asking.
  const act = sumAction(j, r, u);
  const sub = [r.title, r.last_at ? `last touch ${agoWords(r.last_at)}` : 'never contacted']
    .filter(Boolean).join(' · ');

  return `<div class="sum-row ${r.replied ? 'rep' : r.next ? 'act' : ''}">
      <button class="sum-name" onclick="openContactFrom(${u}, '${esc(r.id)}')"
        title="Open ${esc(r.full_name || r.email)}">${esc(r.full_name || r.email || '(no name)')}</button>
      <span class="sum-track">${track}</span>
      ${status}
      <span class="sum-sub">${esc(sub)}</span>
      <span class="sum-act">${act}</span>
    </div>`;
}

// Which channel the next action belongs to decides which tab opening the card lands on — the
// button says "text them", so it must not open on email.
const NEXT_TAB = {email: 'email', sms: 'phone', call: 'call', phone: 'phone'};
function sumAction(j, r, u) {
  const go = (label, ch) => `<button class="sum-go" onclick="openContactFrom(${u},
    '${esc(r.id)}', '${ch}')">${label}</button>`;
  if (r.replied) return go('Reply ↗', 'email');
  if (!r.next) return '';
  return go(r.next.channel === 'phone' ? 'Add number ↗'
          : r.next.channel === 'sms' ? 'Text ↗'
          : r.next.channel === 'call' ? 'Call ↗' : 'Write ↗',
          NEXT_TAB[r.next.channel] || 'email');
}

// The close guard, shown IN the summary rather than only in a dialog: "I should not close out a
// job application without having texted and called some of the people". Seeing it before you
// reach for the row menu is what makes it advice rather than an interruption.
function closeNotice(j) {
  const w = (j.coverage || {}).close_warning;
  if (!w || !w.warn || isClosed(j) || j.interview_at) return '';
  return `<div class="sum-warn">⚠ Not fully worked yet — ${esc(w.lines.join(' · '))}.
      <span class="sum-warn-why">Closing this is still your call; this is what is left.</span>
    </div>`;
}
function openContactFrom(url, cid, channel) {
  TAB_OPEN.set(url, 'people');
  CONTACT_OPEN.add(cid);
  // Land on the channel the button named. "Text ↗" opening the email composer is the same
  // promise-the-page-does-not-keep as a span shaped like a button (§Lessons 88).
  if (channel) CHANNEL_TAB.set(cid, channel);
  autoSyncGmail(cid);
  refresh();
}

function jobPane(j) {
  const t = activeTab(j);
  if (t === 'summary')   return summaryPane(j);
  if (t === 'job')       return jobDetail(j);
  if (t === 'activity')  return `<div class="timeline">${activityHtml(j.activity)}</div>`;
  if (t === 'materials') return materialLinks(j.materials) || `<div class="pane-empty">No materials generated yet.</div>`;
  if (t === 'followups') return j.followups ? followupBody(j, j.followups)
                                            : `<div class="pane-empty">Nobody has been emailed yet.</div>`;
  return peopleList(j);
}

// ── Follow-ups: a standalone panel, peer of Checklist / Activity / People ────
function fuWhen(h) {
  if (h == null) return '';
  if (h <= 0) return 'now';
  if (h < 24) return `in ${h}h`;
  return `in ${Math.round(h / 24)}d`;
}
// Where the rest of the Space's follow-ups actually are, for a job that has none of them.
// Returns '' when there is nothing elsewhere — a pointer that fires on every empty tab would
// be noise, and the ordinary case is genuinely "nothing to do".
function elsewhereDue(j) {
  const others = (LAST_JOBS || []).filter(o => o.url !== j.url && !isClosed(o) && !o.interview_at
                                               && dueByChannel(o).total > 0);
  if (!others.length) return '';
  const n = others.reduce((a, o) => a + dueByChannel(o).total, 0);
  const first = others[0];
  const what = `${n} due on ${others.length} other application${others.length === 1 ? '' : 's'}`;
  const name = esc((first.title || first.company || 'the first one').slice(0, 34));
  return ` <span class="fu-elsewhere">${what} — <button class="linklike"`
    + ` onclick="gotoTodo(${tagArg(first.url)}, 'followups')">go to ${name} ↗</button></span>`;
}

function followupBody(j, f) {
  const byId = {}; (j.contacts || []).forEach(c => byId[c.id] = c);
  let out = `<div class="fu-sched">Sequence: ${f.schedule.map((h,i)=>`touch ${i+1} at ${fuWhen(h).replace('in ','')}`).join(' · ')}</div>`;
  out += fuBulkBar(j, f, byId);
  if (f.due.length) {
    out += f.due.map(d => followupCard(byId[d.id], d, f.total_touches)).join('');
  } else {
    // "Nothing due right now" is true of THIS job and useless next to a counter reporting the
    // whole Space. Measured live: 19 follow-ups due across 6 jobs, and **24 jobs showing an
    // empty Follow-ups tab** — so four times out of five, opening the tab the badge sent you
    // looking for lands somewhere with nothing in it and no way to tell where the work is.
    // Reported as the follow-ups feature not working end to end; both numbers were right and
    // neither pointed at the other.
    out += `<div class="fu-empty">Nothing due right now.${elsewhereDue(j)}</div>`;
  }
  const rest = [];
  f.waiting.forEach(w => rest.push(`${esc(w.full_name)} — touch ${w.touch} ${fuWhen(w.due_in_h)}`));
  f.finished.forEach(w => rest.push(`${esc(w.full_name)} — sequence complete`));
  f.stopped.forEach(w => rest.push(`${esc(w.full_name)} — ${w.state === 'replied' ? 'replied ✓' : 'stopped'}`));
  if (rest.length) out += `<div class="fu-rest">${rest.map(r => `<span>${r}</span>`).join('')}</div>`;

  // ── LinkedIn ladder, on its own clock ──
  out += `<div class="fu-sec">🔗 LinkedIn <span class="fu-sched-inline">accepted your invite, went quiet · ${(f.li_schedule||[]).map(h=>Math.round(h/24)+'d').join(' · ')}</span></div>`;
  if ((f.li_due || []).length) {
    out += f.li_due.map(d => liFollowupCard(byId[d.id], d, f.li_total_touches)).join('');
  } else {
    out += `<div class="fu-empty">No LinkedIn follow-ups due.</div>`;
  }
  const liRest = [];
  (f.li_waiting || []).forEach(w => liRest.push(`${esc(w.full_name)} — touch ${w.touch} ${fuWhen(w.due_in_h)}`));
  // Anyone with a profile but no recorded invite can't be scheduled — offer to start the clock.
  (j.contacts || []).filter(c => c.linkedin_url && !c.dm_sent_at).forEach(c => {
    liRest.push(`${esc(c.full_name)} — no invite recorded `
      + `<button class="link-btn" onclick="fuAct('${esc(c.id)}','li_connected',this)">mark connected</button>`);
  });
  if (liRest.length) out += `<div class="fu-rest">${liRest.map(r => `<span>${r}</span>`).join('')}</div>`;
  return out;
}
function liFollowupCard(c, d, total) {
  if (!c) return '';
  const has = !!(c.li_followup_message || '').trim();
  const url = encodeURIComponent(c.linkedin_url || '');
  return `
    <div class="fu-card li" data-cid="${esc(c.id)}">
      <div class="fu-head">
        <strong>${esc(c.full_name)}</strong> <span class="fu-role">— ${esc(c.title)}</span>
        <span class="fu-touch li">LinkedIn · touch ${d.touch} of ${total || 2}</span>
      </div>
      <div class="fu-meta">Connected ${fmtDate(c.dm_sent_at)} · no reply recorded</div>
      ${has ? `
        <textarea class="li-body" rows="4">${esc(c.li_followup_message)}</textarea>
        <div class="dbtns">
          <button class="send" onclick="liCopyOpen('${esc(c.id)}','${url}',this)" title="Copies the message and opens their profile — paste it into the chat and send">Copy + open LinkedIn</button>
          <button onclick="fuAct('${esc(c.id)}','li_save',this)">Save</button>
          <button class="secondary" onclick="fuAct('${esc(c.id)}','li_draft',this)">Regenerate</button>
          <button onclick="fuAct('${esc(c.id)}','li_sent',this)" title="Record that you sent it">✓ I sent it</button>
          <button class="ghost" onclick="fuAct('${esc(c.id)}','li_replied',this)">They replied</button>
          <button class="ghost" onclick="fuAct('${esc(c.id)}','li_stop',this)">Stop</button>
        </div>`
      : `<div class="dbtns">
          <button class="send" onclick="fuAct('${esc(c.id)}','li_draft',this)">✍ Draft LinkedIn message</button>
          <button class="ghost" onclick="fuAct('${esc(c.id)}','li_replied',this)">They replied</button>
          <button class="ghost" onclick="fuAct('${esc(c.id)}','li_stop',this)">Stop</button>
        </div>`}
    </div>`;
}
// Copy the (possibly edited) message and open the profile. You paste and send — we never
// drive LinkedIn ourselves; that architecture was abandoned twice.
function liCopyOpen(cid, encUrl, btn) {
  const card = btn.closest('.fu-card');
  const msg = card ? card.querySelector('.li-body').value : '';
  if (msg) { try { navigator.clipboard.writeText(msg); } catch { /* clipboard denied — the message stays on screen to copy by hand */ } }
  post('/api/followup', {contact_id: cid, action: 'li_save', body: msg});
  window.open(decodeURIComponent(encUrl), '_blank', 'noopener');
  btn.textContent = 'Copied ✓ — paste in the chat, then "I sent it"';
  setTimeout(() => { btn.textContent = 'Copy + open LinkedIn'; }, 4000);
}
// ── Bulk draft / send, on the job's own Follow-ups tab ──────────────────────
//
// The global console button does the same job across every application, and it was reported
// three times as not existing — because this is where the work is actually done. A bulk control
// belongs beside the things it acts on, not two screens up (§Lessons 43, again).
//
// Scoped to THIS job's due list, which is also what makes it safe to read: eight names you can
// see, not fifty-seven you cannot.
function fuBulkBar(j, f, byId) {
  const due = (f.due || []).map(d => byId[d.id]).filter(Boolean);
  if (due.length < 2) return '';                    // one contact is just the button on the card
  const drafted = due.filter(c => (c.followup_message || '').trim());
  const undrafted = due.length - drafted.length;
  const key = `decodeURIComponent('${encodeURIComponent(j.url)}')`;

  // Disabled with the REASON on it rather than hidden: "nothing to send yet" is a state the
  // operator needs to understand, and a control that vanishes reads as a bug (§Lessons 41).
  const draftBtn = undrafted
    ? `<button class="secondary" onclick="fuBulk(${key}, 'draft', this)">✍ Draft all ${undrafted}</button>`
    : `<button class="secondary" disabled title="Every due follow-up already has a draft">✍ Draft all</button>`;
  const sendBtn = drafted.length
    ? `<button class="send" onclick="fuBulk(${key}, 'send', this)">Send all ${drafted.length} drafted</button>`
    : `<button class="send" disabled title="Draft them first — there is nothing written to send">Send all drafted</button>`;

  return `<div class="fu-bulk">
    <span class="fu-bulk-n">${due.length} due</span>
    <span class="fu-bulk-sub">${drafted.length} drafted · ${undrafted} not yet</span>
    ${draftBtn}${sendBtn}
    <span class="fu-bulk-status" data-bulk-status></span>
  </div>`;
}

async function fuBulk(url, action, btn) {
  const j = (LAST_JOBS || []).find(x => x.url === url);
  if (!j) return;
  const byId = {}; (j.contacts || []).forEach(c => byId[c.id] = c);
  let due = ((j.followups || {}).due || []).map(d => byId[d.id]).filter(Boolean);
  // Sending acts ONLY on what is already written. Drafting acts only on what is not — otherwise
  // "Draft all" silently discards drafts the operator has edited by hand.
  due = action === 'send'
    ? due.filter(c => (c.followup_message || '').trim())
    : due.filter(c => !(c.followup_message || '').trim());
  if (!due.length) return;

  if (action === 'send') {
    const who = due.map(c => c.full_name).join(', ');
    if (!confirm(`Send ${due.length} follow-up email(s) for ${j.contact_company || j.company}?\n\n` +
                 `${who}\n\nThis cannot be undone.`)) return;
  }

  const status = btn.parentElement.querySelector('[data-bulk-status]');
  const was = btn.textContent;
  btn.disabled = true;
  btn.textContent = action === 'draft' ? 'Drafting…' : 'Sending…';
  // Highlight the cards this is about to touch, so it is obvious WHO is included before anything
  // happens — the whole reason this belongs on the job rather than in a global panel.
  due.forEach(c => fuFlash(c.id, 'working'));
  if (status) status.textContent = `${due.length} queued…`;

  try {
    const r = await post('/api/followup/bulk', { action, contact_ids: due.map(c => c.id) });
    if (status) status.textContent = r.message || (r.ok ? 'done' : 'failed');
    const failed = new Set((r.results || []).filter(x => x.contact_id && !x.ok).map(x => x.contact_id));
    due.forEach(c => fuFlash(c.id, failed.has(c.id) ? 'failed' : 'ok'));
  } catch (e) {
    if (status) status.textContent = String(e);
    due.forEach(c => fuFlash(c.id, 'failed'));
  } finally {
    btn.disabled = false;
    btn.textContent = was;
    refresh();
  }
}

// Mark one person's card. `refresh()` rebuilds the tab every 2.5s, so this is a signal for the
// moment the operator is watching, not stored state.
function fuFlash(cid, state) {
  const card = document.querySelector(`.fu-card[data-cid="${cid}"]`);
  if (!card) return;
  card.classList.remove('fu-working', 'fu-ok', 'fu-failed');
  card.classList.add(state === 'working' ? 'fu-working' : state === 'failed' ? 'fu-failed' : 'fu-ok');
}


function followupCard(c, d, totalTouches) {
  if (!c) return '';
  const has = !!(c.followup_message || '').trim();
  const warn = c.threaded ? '' : `<span class="fu-warn" title="This email predates threading, so the follow-up arrives as a new message rather than a reply">⚠ won't thread</span>`;
  const err = c.followup_error ? `<div class="fu-err">${esc(c.followup_error)}</div>` : '';
  return `
    <div class="fu-card" data-cid="${esc(c.id)}">
      <div class="fu-head">
        <strong>${esc(c.full_name)}</strong> <span class="fu-role">— ${esc(c.title)}</span>
        <span class="fu-touch">touch ${d.touch} of ${totalTouches || 3}</span>${warn}
      </div>
      <div class="fu-meta">First emailed ${fmtDate(c.submitted_at)} · no reply recorded</div>
      ${err}
      ${has ? `
        <input class="fu-subj" value="${esc(c.followup_subject)}" placeholder="Subject…" />
        <textarea class="fu-body" rows="5">${esc(c.followup_message)}</textarea>
        <div class="dbtns">
          <button class="send" onclick="fuAct('${esc(c.id)}','send',this)">Send follow-up</button>
          <button onclick="fuAct('${esc(c.id)}','save',this)">Save</button>
          <button class="secondary" onclick="fuAct('${esc(c.id)}','draft',this)">Regenerate</button>
          <button class="ghost" onclick="fuAct('${esc(c.id)}','replied',this)" title="They already got back to you — stop the sequence">They replied</button>
          <button class="ghost" onclick="fuAct('${esc(c.id)}','stop',this)">Stop</button>
        </div>`
      : `<div class="dbtns">
          <button class="send" onclick="fuAct('${esc(c.id)}','draft',this)">✍ Draft follow-up</button>
          <button class="ghost" onclick="fuAct('${esc(c.id)}','replied',this)">They replied</button>
          <button class="ghost" onclick="fuAct('${esc(c.id)}','stop',this)">Stop</button>
        </div>`}
    </div>`;
}
async function fuAct(cid, action, btn) {
  const card = btn.closest('.fu-card');   // null for row-level actions like "mark connected"
  const body = { contact_id: cid, action };
  if (action === 'save') {
    body.subject = card.querySelector('.fu-subj').value;
    body.body = card.querySelector('.fu-body').value;
  }
  // ARCH-3: one wire shape for every channel. LinkedIn has no subject; it sends body only.
  if (action === 'li_save') body.body = card.querySelector('.li-body').value;
  if (action === 'send') {
    // Send what's on screen, so an un-saved edit is never silently dropped.
    await post('/api/followup', { contact_id: cid, action: 'save',
      subject: card.querySelector('.fu-subj').value, body: card.querySelector('.fu-body').value });
    if (!confirm('Send this follow-up now?')) return;
  }
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = action === 'draft' ? 'Writing…' : 'Working…';
  const r = await post('/api/followup', body);
  if (!r.ok) { btn.disabled = false; btn.textContent = label; alert(r.message || 'Failed'); return; }
  btn.textContent = 'Done ✓';
  refresh();
}
function activityHtml(events) {
  if (!events || !events.length) return `<div class="tl-empty">No recorded activity yet.</div>`;
  return events.map(e => {
    let t = '';
    try { t = new Date(e.ts).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}); } catch { t = e.ts; }
    const cls = e.status === 'failed' ? 'tl-fail' : (e.status === 'ok' ? 'tl-ok' : 'tl-info');
    return `<div class="tl-row ${cls}"><span class="tl-ico">${STAGE_ICON[e.stage]||'•'}</span><span class="tl-body"><span class="tl-detail">${esc(e.detail || (e.stage+' '+e.status))}</span><span class="tl-time">${esc(t)}</span></span></div>`;
  }).join('');
}
// The one thing to do next for this job, rendered right under its status badge so state + action
// are always visible together (they used to be 10 columns apart in a 1320px-wide table).
// ONE primary action per row. Restart / rejected / delete are secondary and live in the
// ⋯ menu — they were adding two or three stacked lines to every row, including finished
// ones whose only useful content is the badge and the date.
//: Why an apply FAILED, in English. The row used to print the raw code and then run straight
//: into the Restart button's own description with nothing between them, so the line read:
//:
//:     copilot_violation_agent_submitted Regenerates materials, then re-applies.
//:
//: Two unrelated sentences welded together — one about what happened, one about what a button
//: would do — and the half that mattered was a machine token. Reported as "what does this mean?".
//:
//: The button's description moved ONTO the button (§Lessons 88: a control describes itself where
//: it is), and the raw code is kept as the hover text here, because it is the exact thing to
//: search the logs for and no sentence replaces it.
const FAIL_WHY = {
  // The agent pressed Submit in a mode whose whole purpose is that YOU press Submit. Nobody
  // reviewed it — and it may genuinely have gone out, which is why the wording refuses to say
  // it did not. `applied_at` stays empty until a human confirms (§Lessons 19).
  copilot_violation_agent_submitted:
    'The agent submitted this itself, which co-pilot mode is meant to prevent — so it was never '
    + 'reviewed. It may still have reached the employer. Check, then "✅ Mark as applied" in the ⋯ menu if it did.',
  dryrun_violation_agent_submitted:
    'This was a dry run and the agent submitted anyway. Check whether the employer received it.',
  no_result_line: 'The agent stopped without saying how it went. The form may already be complete.',
  timeout: 'The agent ran out of time with the form part-filled.',
  unknown: 'The agent finished without a clear result.',
  expired: 'The posting is gone — the listing expired or was taken down.',
  captcha: 'A captcha blocked it, and captchas are yours to solve, not the agent’s.',
  login_issue: 'It could not get past the sign-in. Try 🔐 Sign in first, then restart.',
  account_required: 'This employer requires an account before you can apply. 🔐 Sign in first.',
  sso_required: 'The site demands single sign-on, which the agent cannot complete.',
  already_applied: 'The site says you have already applied to this one.',
  not_eligible_location: 'The posting rules you out on location.',
  not_eligible_salary: 'The posting rules you out on pay.',
  not_a_job_application: 'That link is not an application form.',
  unsafe_permissions: 'The form asked for something the agent is not allowed to give.',
  unsafe_verification: 'It needed an identity check the agent must not attempt.',
  site_blocked: 'The site blocked automated access.',
  cloudflare_blocked: 'Cloudflare blocked automated access.',
  blocked_by_cloudflare: 'Cloudflare blocked automated access.',
};

//: The code, in English, or the code itself when it is one nobody has written a sentence for —
//: never a generic "it failed", which throws away the only thing that says what to do next.
function failWhy(code) {
  const c = (code || '').trim();
  if (!c) return 'The last attempt failed.';
  return FAIL_WHY[c] || `The last attempt failed: ${c}`;
}

// Explanatory line under the strip, for the states where the next action needs context.
function nextHint(j) {
  if (j.status === 'ready_to_submit')
    return 'Review &amp; submit in the open Chrome window, then confirm.';
  if (j.status === 'needs_human')
    return esc(BLOCKER_ASK[j.apply_error] || BLOCKER_ASK.blocker);
  if (j.status === 'failed') {
    const raw = (j.apply_error || '').trim();
    // The code survives as hover text. It is what you grep the apply log for, and an English
    // sentence is not a substitute for it.
    return raw
      ? `<span title="${esc(raw)}">${esc(failWhy(raw))}</span>`
      : esc(failWhy(''));
  }
  return '';
}
// "Sign in first" — for employers whose ATS makes you register before you can apply
// (Deloitte, Workday, Salesforce). Opens the SAME persistent Chrome profile the agent uses,
// with no agent attached, so you create the account once and every later application to that
// employer is already authenticated.
//
// Only offered before an application has gone through: once it is applied or waiting for
// review, signing in is not the thing to do next. It is deliberately NOT shown as the primary
// action — most jobs never need it, and it should not compete with "Fill application".
//: Whether apply-shaped controls mean anything for this row. Read off the ROW, not the global
//: SPACE_SHAPE, so a renderer can be driven one row at a time — and so a payload that ever
//: mixes shapes cannot be rendered wrong by a stale global.
function isTargetRow(j) { return (j && j.shape) === 'pipeline/targets'; }

//: What success is CALLED here. Read off the row's `terminal`, not inferred from its shape:
//: shape says what a row IS and terminal says what winning means, and a jobs-shaped Space that
//: sets terminal='booked' deliberately would be mislabelled by the proxy.
const WON_LABEL = { interview: {icon:'🎯', label:'Interview', done:'Interview scheduled'},
                    booked:    {icon:'📞', label:'Call booked', done:'Call booked'} };
function wonLabel(j) { return WON_LABEL[(j && j.terminal) || 'interview'] || WON_LABEL.interview; }

function signinButton(j) {
  // Nothing to sign in TO. A target is a company, not an application form behind an ATS wall.
  if (isTargetRow(j)) return '';
  if (j.signin_open) return '';
  if (isClosed(j) || ['applied', 'ready_to_submit', 'in_progress'].includes(j.status)) return '';
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  return `<button class="restart-inline" onclick="signIn(${u}, this)" title="Open this application in Chrome so you can register or log in. The session is saved, so later applications to this employer skip it.">🔐 Sign in first</button>`;
}
// The waiting state. Deliberately two exits: hand the open window straight to the agent, or
// keep the session and come back later. "Fill it now" resumes INTO this browser rather than
// relaunching, so the login that was just created is the one the agent uses.
function signinBar(j) {
  if (!j.signin_open) return '';
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  return `<div class="signin-bar">
    <span>🔐 Chrome is open on this application. Register or sign in, then:</span>
    <button class="primary" onclick="signinDone(${u}, true, this)">▶ Fill it now</button>
    <button class="ghost" onclick="signinDone(${u}, false, this)">✓ Done for now</button>
  </div>`;
}
async function signIn(url, btn) {
  btn.disabled = true; btn.textContent = 'Opening…';
  const r = await post('/api/signin', {url});
  const cmdEl = document.getElementById('command');
  if (cmdEl) cmdEl.textContent = r.message || '';
  if (!r.ok) { btn.disabled = false; btn.textContent = '🔐 Sign in first'; }
  refresh();
}
async function signinDone(url, fill, btn) {
  btn.disabled = true; btn.textContent = fill ? 'Starting…' : 'Closing…';
  const r = await post('/api/signin-done', {url, fill});
  const cmdEl = document.getElementById('command');
  if (cmdEl) cmdEl.textContent = r.message || '';
  if (fill && r.ok) { await pollCommandUntilDone('Fill'); }
  refresh();
}
// The success metric, ON THE ROW. It shipped inside the ⋯ menu and was reported as missing —
// the third control this session placed somewhere invisible, after the SMS composer and the
// round-two panel. The comment directly below already recorded the lesson for Re-apply
// ("burying it made it unfindable") and it was repeated anyway.
//
// Nothing renders here once it is set: `nextAction` already replaces the whole Next slot with
// the 🎯 chip, and a second control saying the same thing is noise. Undo lives in the ⋯ menu,
// which is the right home for a rare, corrective action.
function interviewButton(j) {
  if (isClosed(j) || j.status === 'in_progress') return '';
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  // The undo lives HERE, not only in the ⋯ menu. Marking an interview is the one action that
  // halts every sequence on a job, so misclicking it is expensive — and the revert was buried
  // in the same overflow menu the 🎯 button itself had to be dragged out of (§Lessons 43).
  // Same column, same write, same halting of every sequence — only the word changes, because
  // what success MEANS is the Space's `terminal` (spaces-prd §7). Booking detection already
  // runs automatically (cal.com mails the host), so this shape's success metric was
  // instrumented before the shape existed.
  const won = wonLabel(j);
  if (j.interview_at)
    return `<button class="won-btn undo" onclick="unmarkInterview(${u}, this)"
      title="Scheduled ${esc(fmtDate(j.interview_at))} — undo. Sequences this stopped stay stopped; reopen any you want back.">↩ Not scheduled</button>`;
  return `<button class="won-btn" onclick="markInterview(${u}, this)"
    title="Greys this row and stops every follow-up sequence for it">${won.icon} ${esc(won.label)}</button>`;
}

// Re-apply stays visible on the row rather than living only in the ⋯ menu: on an applied
// job it is the main thing you might still want, and burying it made it unfindable.
function restartButton(j) {
  // There is no application to re-apply to. Omitted rather than disabled: a disabled button
  // asserts the action exists and is unavailable, and here it does not exist (§Lessons 43).
  if (isTargetRow(j)) return '';
  if (isClosed(j) || j.status === 'in_progress' || j.status === 'failed') return '';
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const applied = j.status === 'applied';
  return `<button class="restart-inline" onclick="restartJob(${u}, this, ${applied})" title="Regenerate materials, then run the whole application again from scratch">🔄 Re-apply</button>`;
}
// Overflow menu. Only one is open at a time, and the open row survives the 2.5s refresh.
const ROWMENU_OPEN = new Set();
function onRowMenuToggle(el, url) {
  if (el.open) { ROWMENU_OPEN.clear(); ROWMENU_OPEN.add(url); } else { ROWMENU_OPEN.delete(url); }
  positionRowMenu(el);
}
// `.table-wrap` clips with overflow:hidden (it rounds the table's corners), so an absolutely
// positioned menu is CUT rather than scrolled to. CSS handles the horizontal side by anchoring
// right; the bottom edge needs measuring, because whether a row is the last one is not
// expressible in CSS. Flip above the ⋯ when the panel would spill past the wrapper.
function positionRowMenu(el) {
  const body = el && el.querySelector && el.querySelector('.rowmenu-body');
  if (!body || !body.classList) return;
  body.classList.remove('flip-up');
  if (!el.open) return;
  const clip = el.closest && el.closest('.table-wrap');
  if (!clip || !clip.getBoundingClientRect || !body.getBoundingClientRect) return;
  if (body.getBoundingClientRect().bottom > clip.getBoundingClientRect().bottom) {
    body.classList.add('flip-up');
  }
}
function rowMenu(j) {
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const label = `decodeURIComponent('${encodeURIComponent(`${j.company} - ${j.title}`)}')`;
  // Restart lives on the strip as "🔄 Re-apply" (restartButton) — not duplicated here.
  const items = [];
  // The success metric goes first: it is the outcome every other action exists to cause.
  items.push(j.interview_at
    ? `<button onclick="unmarkInterview(${u}, this)">↩ Not scheduled after all<span>Sequences stay stopped</span></button>`
    : `<button onclick="markInterview(${u}, this)">🎯 Interview scheduled<span>Greys the row and stops every sequence</span></button>`);
  // "I applied to this myself." Distinct from the strip's "Mark submitted ✓", which confirms a
  // co-pilot run the agent actually filled — that one is gated on the job having been attempted
  // and refuses a job ApplyPilot never opened. This is for the case that gate exists to block
  // and the operator legitimately needs: applied by hand, on the company's own site.
  //
  // Not offered on a targets row: there is no application to have submitted.
  if (!isTargetRow(j)) {
    items.push(j.applied_at
      ? `<button onclick="unmarkApplied(${u}, this)">↩ Not applied<span>Undo — keeps the agent's run history</span></button>`
      : `<button onclick="markApplied(${u}, this)">✅ Mark as applied<span>You applied to this yourself</span></button>`);
  }
  // Three ways OUT that are not an interview, and they are different facts. "Rejected" is an
  // outcome — somebody read it and said no. "Cancelled" is the posting ceasing to exist: a req
  // pulled, a hiring freeze, a role filled internally. "Ghost" is a listing that was never a
  // real opening at all. Filing any of them under Rejected makes the rejection rate describe
  // decisions nobody made, and folding ghost into cancelled loses the one thing it teaches:
  // cancelled is bad luck, ghost is a source worth avoiding.
  //
  // Both are reversible from the same Restore, because `rejected_at` is one column meaning
  // "when it left" and undoing either is the same operation.
  if (isClosed(j)) {
    items.push(`<button onclick="unmarkRejected(${u}, this)">↩ Restore<span>Move back into the pipeline</span></button>`);
  } else {
    items.push(`<button onclick="markRejected(${u}, this)">✕ Mark rejected<span>They said no — counts in your funnel</span></button>`);
    items.push(`<button onclick="markCancelled(${u}, this)">⊘ Job removed / cancelled<span>Posting pulled or frozen — not a rejection</span></button>`);
    items.push(`<button onclick="markGhost(${u}, this)">👻 Ghost job<span>Never a real opening — evergreen or reposted forever</span></button>`);
  }
  items.push(`<button class="danger" onclick="deleteJob(${u}, ${label})">🗑 Delete<span>Remove this job and its contacts</span></button>`);
  return `<details class="rowmenu" ${ROWMENU_OPEN.has(j.url) ? 'open' : ''} ontoggle="onRowMenuToggle(this, ${u})">
    <summary title="More actions">⋯</summary>
    <div class="rowmenu-body">${items.join('')}</div>
  </details>`;
}
// Click anywhere outside an open row menu closes it (a <details> won't do this itself).
document.addEventListener('click', (e) => {
  document.querySelectorAll('details.rowmenu[open]').forEach(d => {
    if (!d.contains(e.target)) { d.open = false; ROWMENU_OPEN.delete(d.dataset.url || ''); ROWMENU_OPEN.clear(); }
  });
});
async function restartJob(url, btn, applied) {
  // End-to-end: fix missing materials, then co-pilot apply. For apps that didn't go through.
  const msg = applied
    ? 'This application is marked as ALREADY APPLIED.\n\nRestart anyway? ApplyPilot will regenerate materials and fill a NEW application in Chrome (it never auto-submits — you review + submit). Only do this if it did not actually go through.'
    : 'Restart this application end-to-end?\n\nApplyPilot will regenerate any missing résumé/cover letter, then fill the application in Chrome and hand it to you to review + submit.';
  if (!confirm(msg)) return;
  btn.disabled = true; btn.textContent = 'Restarting…';
  const cmdEl = document.getElementById('command');
  const r = await post('/api/restart', {url});
  if (!r.ok) { btn.disabled = false; btn.textContent = '🔄 Restart end-to-end'; cmdEl.textContent = r.message || 'Could not restart'; return; }
  cmdEl.textContent = 'Restarting end-to-end — regenerating materials, then filling in Chrome…';
  await pollCommandUntilDone('Restart');
  cmdEl.textContent = '✅ Restarted — review in the open Chrome window, submit, then "Mark submitted ✓" (or resolve a blocker + Continue).';
  await refresh();
}
async function fillOne(url, btn) {
  // Per-row co-pilot fill for ONE job: opens Chrome, fills it, hands it back to review + submit.
  btn.disabled = true; btn.textContent = 'Filling…';
  const cmdEl = document.getElementById('command');
  const r = await post('/api/fill-one', {url});
  if (!r.ok) { btn.disabled = false; btn.textContent = '▶ Fill application'; cmdEl.textContent = r.message || 'Could not start'; return; }
  cmdEl.textContent = 'Filling the application in Chrome — then handing it to you to review + submit…';
  await pollCommandUntilDone('Fill for review');
  cmdEl.textContent = '✅ Done — review in the open Chrome window, submit, then "Mark submitted ✓" (or resolve a blocker and Continue).';
  await refresh();
}
async function continueJob(url, btn) {
  // The human resolved the blocker (captcha/login/field) in the open browser; resume the agent.
  btn.disabled = true; btn.textContent = 'Resuming…';
  const cmdEl = document.getElementById('command');
  const r = await post('/api/continue', {url});
  if (!r.ok) { btn.disabled = false; btn.textContent = '▶ Continue'; cmdEl.textContent = r.message || 'Could not resume'; return; }
  cmdEl.textContent = 'Resuming in the open browser — continuing where it left off…';
  await pollCommandUntilDone('Continue');
  await refresh();
}
async function markApplied(url, btn) {
  // The confirm names what is being asserted. "Mark applied?" invites a reflex yes; saying it
  // back as a claim about the outside world is what makes it a decision.
  if (!confirm('Record that YOU applied to this yourself, outside ApplyPilot?\n\n'
             + 'Use this for applications you submitted on the company\'s own site. It is '
             + 'reversible from the same menu.')) return;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const r = await post('/api/mark-applied', {url});
  if (r.ok) refresh();
  else { btn.disabled = false; btn.textContent = '✅ Mark as applied'; alert(r.message || 'Failed'); }
}

async function unmarkApplied(url, btn) {
  btn.disabled = true;
  btn.textContent = 'Undoing…';
  const r = await post('/api/unmark-applied', {url});
  if (r.ok) refresh(); else { btn.disabled = false; alert(r.message || 'Failed'); }
}

async function markSubmitted(url, btn) {
  // The user has reviewed + submitted the filled application in the open Chrome window.
  if (!confirm('Confirm you reviewed and submitted this application in the browser?')) return;
  btn.disabled = true; btn.textContent = 'Saving…';
  const r = await post('/api/mark-submitted', {url});
  if (r.ok) { refresh(); } else { btn.disabled = false; btn.textContent = 'Mark submitted ✓'; alert(r.message || 'Failed'); }
}
setInterval(refresh, 2500);
refresh();

// ── Bulk email follow-ups ───────────────────────────────────────────────────
//
// Clicking through 57 due follow-ups one at a time is what this replaces. The design point is
// that the operator SEES the set before it goes: bulk send is the least reversible action in
// the app, and this session already produced two "what the hell went out" moments where the
// answer was only visible after the fact.
//
// The list is built from LAST_JOBS — the payload already on screen — so the ids sent are the
// ids shown. The server does NOT re-derive "everything due": the poller moves that set every
// five minutes, and a re-derivation could send a message that was never listed.

function bulkDueContacts() {
  const rows = [];
  for (const j of (LAST_JOBS || [])) {
    if (j.interview_at || j.rejected_at) continue;   // left the pipeline; nothing to chase
    for (const item of ((j.followups || {}).due || [])) {
      rows.push({ id: item.id, name: item.full_name || item.name || 'contact',
                  company: j.contact_company || j.company || 'Unknown', touch: item.touch || 1 });
    }
  }
  return rows;
}

function renderBulkDue() {
  const rows = bulkDueContacts();
  const badge = document.getElementById('bulkDueCount');
  if (badge) badge.textContent = rows.length ? `(${rows.length})` : '';
  const box = document.getElementById('bulkBreakdown');
  if (!box) return rows;
  if (!rows.length) {
    box.innerHTML = '<div class="hint">Nothing is due right now.</div>';
    return rows;
  }
  // Grouped BY COMPANY, because that is the unit the recipient experiences. Six people at Okta
  // hearing from you inside a minute is a different thing from six people at six companies, and
  // it is invisible in a flat count of 57.
  const byCo = {};
  for (const r of rows) (byCo[r.company] = byCo[r.company] || []).push(r);
  const parts = Object.keys(byCo).sort().map(co => {
    const n = byCo[co].length;
    const names = byCo[co].map(r => esc(r.name)).join(', ');
    return `<div class="bulk-co"><span class="bulk-co-n">${n}</span>
              <span class="bulk-co-name">${esc(co)}</span>
              <span class="bulk-co-people">${names}</span></div>`;
  });
  box.innerHTML = `<div class="hint" style="margin-bottom:6px">${rows.length} follow-up(s) due to
      ${Object.keys(byCo).length} employer(s). Every one is a real email.</div>` + parts.join('');
  return rows;
}

function toggleBulkFollowups(force) {
  const p = document.getElementById('bulkPanel');
  if (!p) return;
  p.hidden = force === false ? true : !p.hidden;
  if (!p.hidden) renderBulkDue();
}

async function bulkFollowups(action, btn) {
  const rows = renderBulkDue();
  if (!rows.length) return;
  const status = document.getElementById('bulkStatus');
  const out = document.getElementById('bulkResults');

  if (action === 'send') {
    // The one confirm in this flow, and it names the number and the employers rather than
    // asking "are you sure?" — a dialog that does not say what will happen is a dialog people
    // click through.
    const cos = [...new Set(rows.map(r => r.company))];
    const ok = confirm(
      `Send ${rows.length} follow-up email(s) now, to ${cos.length} employer(s)?\n\n` +
      cos.slice(0, 8).map(c => `  • ${c}: ${rows.filter(r => r.company === c).length}`).join('\n') +
      (cos.length > 8 ? `\n  …and ${cos.length - 8} more` : '') +
      `\n\nThis cannot be undone. Anything without a draft is skipped.`);
    if (!ok) return;
  }

  const was = btn.textContent;
  btn.disabled = true;
  btn.textContent = action === 'draft' ? 'Drafting…' : 'Sending…';
  if (status) status.textContent = `${rows.length} queued…`;
  try {
    const r = await post('/api/followup/bulk',
                         { action, contact_ids: rows.map(x => x.id) });
    if (status) status.textContent = r.message || (r.ok ? 'done' : 'failed');
    // Failures are listed individually. A batch that reports "12 sent, 5 failed" and does not
    // say WHICH five leaves the operator to diff the board by hand.
    const bad = (r.results || []).filter(x => x.contact_id && !x.ok && x.message);
    if (out) {
      const byId = {};
      for (const x of rows) byId[x.id] = x;
      out.innerHTML = (r.results || [])
        .filter(x => x.message)
        .map(x => {
          const who = byId[x.contact_id];
          const label = who ? `${esc(who.name)} · ${esc(who.company)}` : '';
          return `<div class="bulk-line">${label ? label + ' — ' : ''}${esc(x.message)}</div>`;
        }).join('');
    }
    if (bad.length === 0 && out && !r.results?.length) out.innerHTML = '';
  } catch (e) {
    if (status) status.textContent = String(e);
  } finally {
    btn.disabled = false;
    btn.textContent = was;
    refresh();
  }
}


// ── Global attachment toggle ────────────────────────────────────────────────
//
// Server-persisted rather than a page variable: it has to survive the 2.5s refresh AND a
// dashboard restart. A toggle that quietly reverts to "attach" sends documents somebody had
// decided not to send, and the only place they would find out is their Sent folder.
//
// Only the FIRST outreach email ever attached anything — follow-ups never did — so this changes
// cold outreach and nothing else.
async function toggleAttachDocs(btn) {
  const want = !ATTACH_DOCS;
  btn.disabled = true;
  try {
    const r = await post('/api/attach-docs', { on: want });
    // Read back what the SERVER now holds rather than trusting the click. If the write failed,
    // the label must show the truth instead of an intent nothing honoured.
    ATTACH_DOCS = r.attach_docs !== false;
    const s = document.getElementById('importStatus');
    if (s) s.textContent = r.message || '';
  } catch {
    // Leave ATTACH_DOCS alone; refresh() below re-renders from the served state.
  } finally {
    btn.disabled = false;
    refresh();
  }
}
