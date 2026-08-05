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
  JOB_DESC.clear();
  JOB_DESC_LOADED = false;
  const url = new URL(location.href);
  url.searchParams.set('space', id);
  history.replaceState(null, '', url);   // replace, not push: the back button should leave the
  refresh();                             // dashboard, not walk a tab history nobody asked for
}

function renderSpaceNav(spaces, current, note) {
  const nav = document.getElementById('spaceNav');
  const noteEl = document.getElementById('spaceNote');
  if (noteEl) {
    noteEl.textContent = note || '';
    noteEl.hidden = !note;
  }
  if (!nav) return;
  const list = spaces || [];
  // One tab is furniture. Hidden rather than disabled, because there is nothing to choose —
  // the disabled-with-a-reason rule (§Lessons 43) is for controls that WOULD do something.
  nav.hidden = list.length < 2;
  if (nav.hidden) { nav.innerHTML = ''; return; }
  nav.innerHTML = list.map(s => {
    const on = s.id === current;
    return `<button class="space-tab${on ? ' on' : ''}" ${on ? 'aria-current="page"' : ''}`
         + ` onclick="switchSpace('${esc(s.id)}')">${esc(s.name)}</button>`;
  }).join('');
}

//: The shape of the Space on screen. Rows carry it too (`j.shape`), because a renderer that
//: reads a global would render correctly and then be impossible to test one row at a time.
let SPACE_SHAPE = 'pipeline/jobs';

// Swap the console for the Space's shape. Wholesale, not by disabling buttons: "Prepare
// Materials" and "Fill application" are not unavailable in a targets Space, they are
// meaningless there, and a disabled control asserts an action exists (§Lessons 43).
function renderSpaceShape(shape, offer) {
  SPACE_SHAPE = shape || 'pipeline/jobs';
  const targets = SPACE_SHAPE === 'pipeline/targets';
  const jobs = document.getElementById('jobControls');
  const tgt = document.getElementById('targetControls');
  if (jobs) jobs.hidden = targets;
  if (tgt) tgt.hidden = !targets;
  const box = document.getElementById('offerInput');
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
      const imp = await post('/api/import', {urls});
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
};

// ── Job filter buckets: map the 12 granular statuses → a few meaningful stages you filter by. ──
const JOB_BUCKETS = {
  all:       { label: 'All',         icon: '',    statuses: null },  // null = everything
  needs_you: { label: 'Needs you',   icon: '👉',  statuses: ['ready','ready_to_submit','needs_human','failed'] },
  progress:  { label: 'In progress', icon: '🚀',  statuses: ['imported','enriched','scored','detail_failed','in_progress','dryrun'] },
  applied:   { label: 'Applied',     icon: '✅',  statuses: ['applied'] },
  rejected:  { label: 'Rejected',    icon: '✕',   statuses: ['rejected'] },
};
const JOB_FILTER_ORDER = ['all','needs_you','progress','applied','rejected'];
let JOB_FILTER = 'all';  // client-side view state; persists across the 2.5s auto-refresh

function jobInBucket(j, bucketKey) {
  const b = JOB_BUCKETS[bucketKey];
  if (!b || !b.statuses) return true;             // 'all'
  return b.statuses.includes(j.status);
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
function renderJobFilters(jobs) {
  const el = document.getElementById('jobFilters');
  if (!el) return;
  el.innerHTML = JOB_FILTER_ORDER.map(key => {
    const b = JOB_BUCKETS[key];
    const n = key === 'all' ? jobs.length : jobs.filter(j => jobInBucket(j, key)).length;
    const active = key === JOB_FILTER ? ' active' : '';
    return `<button class="filter-pill${active}" onclick="setJobFilter('${key}')">${b.icon ? b.icon + ' ' : ''}${b.label} <span class="fp-n">${n}</span></button>`;
  }).join('');
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
// gmail.readonly granted? Decides whether we can offer "⤓ Fetch from Gmail" at all. False on a
// default install, where pasting is the only path.
let CONTENT_SCOPE = false;
//: The most recent /api/status jobs array, for click handlers that run after render.
let LAST_JOBS = [];
//: How often the dashboard's background poller runs, mirrored from the server so the
//: Interactions tab can state the real cadence instead of a hardcoded guess.
let POLL_EVERY_S = 300;
// `wantEmail` / `wantLi` select ONE channel — the contact panel shows them as tabs now, so
// rendering both at once is what made every contact card ~200px tall. Omit both to get the
// old stacked behaviour.
function draftBlock(c, wantEmail, wantLi) {
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
    emailHtml = `
      <div class="d-label">Email</div>
      <input class="d-subj" value="${subj}" placeholder="Subject…" ${sent?'disabled':''} />
      <textarea class="d-body" rows="4" ${sent?'disabled':''} placeholder="${has ? '' : 'No draft yet — click Regenerate'}">${body}</textarea>
      ${sent?'':`<input class="d-style" placeholder="✨ Tweak the vibe, then Regenerate — e.g. 'more casual', 'add a joke'">`}
      <div class="dbtns">
        ${sent?'':`<button onclick="saveDraft('${esc(c.id)}', this)">Save</button>
        <button class="secondary" onclick="regenDraft('${esc(c.id)}', this)">Regenerate</button>`}
        <button onclick="copyDraft(this)">Copy email</button>
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
    ladder = st === 'replied' ? `<span class="sent-tag">✓ replied — sequence stopped</span>`
           : st === 'stopped' ? `<span class="muted">sequence stopped</span>`
           : st === 'finished' ? `<span class="muted">ladder finished (${total} of ${total} sent)</span>`
           : st === 'due' ? `<span class="fu-due">↻ follow-up ${touch} of ${total} due</span>`
           : st === 'waiting' && c.sms_followup_due_in_h != null
             ? `<span class="muted">next text in ${Math.round(c.sms_followup_due_in_h / 24)}d</span>`
             : `<span class="muted">first text ${esc(when)}</span>`;
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
  return `<button class="secondary" onclick="fuAct('${esc(c.id)}','sms_sent',this)"
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

function contactNotes(c) {
  // Apollo will not hand a direct dial to a local tool (reveal_phone_number is
  // webhook-only), so the number is copied out of the Apollo UI by hand and kept here.
  const open = (NOTES_OPEN.has(c.id) || c.phone || c.notes) ? ' open' : '';
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
function toggleContact(cid) { if (CONTACT_OPEN.has(cid)) CONTACT_OPEN.delete(cid); else CONTACT_OPEN.add(cid); refresh(); }
function setChannel(cid, ch) { CHANNEL_TAB.set(cid, ch); CONTACT_OPEN.add(cid); refresh(); }
// CRM-4. Someone the OTHER side added to a thread — a recruiter looping in a hiring manager
// is the single most valuable event in a job-search conversation, and a boolean `replied` threw
// it away. Surfaced as an offer, never auto-created: a contact added here is one an automated
// follow-up ladder would then email, and threads collect schedulers and ATS robots.
function introBanner(j) {
  const intros = j.introductions || [];
  if (!intros.length) return '';
  return intros.map(i => {
    const args = [i.email, i.name || '', i.introduced_by || ''].map(v => `decodeURIComponent('${encodeURIComponent(v)}')`).join(', ');
    const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
    return `<div class="intro-bar">
      <span>👋 <strong>${esc(i.introduced_by || 'Someone')}</strong> added <strong>${esc(i.name || i.email)}</strong> (${esc(i.email)}) to the thread — they may be handling this now.</span>
      <button class="primary" onclick="addIntroduced(${u}, ${args}, this)">+ Add as contact</button>
    </div>`;
  }).join('');
}
async function addIntroduced(url, email, name, by, btn) {
  btn.disabled = true; btn.textContent = 'Adding…';
  const r = await post('/api/contact/add-introduced', {job_url: url, email, name, introduced_by: by});
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
function peopleList(j) {
  const cs = j.contacts || [];
  let intro = introBanner(j);
  if (!cs.length) return intro + `<div class="pane-empty">No contacts yet. ${findContactsPrompt(j)}</div>`;
  intro += anotherRoundPrompt(j, cs);
  const hot = cs.filter(c => c.hot), cold = cs.filter(c => !c.hot);
  let out = intro + bulkBar(j);
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
  return `
    <div class="prow ${open ? 'is-open' : ''}" onclick="toggleContact('${esc(c.id)}')">
      <span class="av" style="background:${avatarColor(c.full_name)}">${initials(c.full_name)}</span>
      <span class="pwho"><span class="pname">${esc(c.full_name)}</span> <span class="prole">— ${esc(c.title)}</span>
        ${c.hot ? `<span class="chip conn">🤝</span>` : ''}</span>
      <span class="pills">${c.confidence === 'medium' ? `<span class="pill warn" title="Nothing confirms this person works there — no company email and no employer on file">? unconfirmed</span>` : ''}${pills.filter(Boolean).join('')}<span class="caret">${open ? '▾' : '▸'}</span></span>
    </div>
    ${open ? contactPanel(c) : ''}`;
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
function gmailThreadUrl(c) {
  const tid = (c.reply_to || {}).thread_id || '';
  return tid ? `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(tid)}` : '';
}
function gmailLink(c, label) {
  const u = gmailThreadUrl(c);
  return u ? `<a class="gm-link" href="${esc(u)}" target="_blank" rel="noopener">${label}</a>` : '';
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
         <span class="conv-acts"><button class="linklike" onclick="openReplyHere('${esc(c.id)}')">Answer now</button>${gmailLink(c, 'Open in Gmail ↗')}</span></div>`
    : conv.state === 'awaiting_them'
      ? `<div class="conv-turn them"><span>Answered ${esc(agoPhrase(conv))} — waiting on ${esc(first)}.</span>
         <span class="conv-acts">${gmailLink(c, 'Open in Gmail ↗')}</span></div>`
      : '';
  const intro = c.introduced_by
    ? `<div class="th-intro">👋 ${esc(c.introduced_by)} added them to this thread</div>` : '';
  // Long threads collapse in the middle. This block is re-rendered every 2.5s and an
  // unbounded list pushes the composer — the only thing you came here to use — off screen.
  const shown = msgs.length > 6
    ? [msgs[0], {_gap: msgs.length - 3}, ...msgs.slice(-2)]
    : msgs;
  let seenCc = [];
  const rows = shown.map(m => {
    if (m._gap) return `<div class="cm-gap">· ${m._gap} earlier messages ·</div>`;
    const html = convMessage(c, m, seenCc);
    seenCc = (m.cc_addrs || []).map(x => addrOf(x));
    return html;
  }).join('');
  return `<div class="conv">${banner}${intro}
    <div class="conv-msgs">${rows}</div>
    ${replyBox(c)}
  </div>`;
}
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
  const text = body
    ? `<div class="cm-body">${esc(body)}</div>`
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
const REPLY_DRAFT = new Map();   // contact id -> body
const REPLY_DROP  = new Map();   // contact id -> Set of cc addresses removed

// Replying, not following up. The distinction is real: a follow-up is a ladder step with a
// schedule and a stop condition, a reply answers a person who wrote to us. `reply_to` is null
// until somebody actually does, which is what keeps the two from blurring together.
function replyBox(c) {
  const t = c.reply_to;
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
  const dropped = REPLY_DROP.get(c.id) || new Set();
  const cc = (t.cc || []).filter(x => !dropped.has(x));
  // The Cc is the whole reason this exists: answering only the sender drops whoever they
  // introduced, and nothing on screen would show that it happened.
  const chips = (t.cc || []).map(x => {
    const off = dropped.has(x);
    return `<button class="cc-chip${off ? ' off' : ''}" title="${off ? 'Add back' : 'Remove from this reply'}"
      onclick="toggleCc('${esc(c.id)}', decodeURIComponent('${encodeURIComponent(x)}'))">${esc(x)} ${off ? '＋' : '✕'}</button>`;
  }).join('');
  const body = REPLY_DRAFT.get(c.id) || '';
  return `<div class="reply-box" data-reply-for="${esc(c.id)}" data-cc="${esc(JSON.stringify(cc))}" data-to="${esc(t.to)}">
    <div class="reply-hdr">↩ Reply to <strong>${esc(t.to)}</strong>${
      cc.length ? ` · cc ${cc.length}` : (t.cc || []).length ? ' · <span class="cc-none">cc removed</span>' : ''}</div>
    ${(t.cc || []).length ? `<div class="cc-row">${chips}</div>` : ''}
    ${lastReplyCard(c)}
    <div class="reply-subj">${esc(t.subject)}</div>
    <textarea class="reply-body" rows="6" placeholder="Write your reply…"
      oninput="REPLY_DRAFT.set('${esc(c.id)}', this.value)">${esc(body)}</textarea>
    <div class="reply-actions">
      <button class="secondary" onclick="draftReply('${esc(c.id)}', this)">✍ Draft an answer</button>
      <button class="primary" onclick="sendReply('${esc(c.id)}', this)">Send reply</button>
      <span class="reply-hint">Goes into this thread. No attachments.</span>
    </div>
    ${replyMsg(c.id)}
    <input class="r-style" placeholder="✨ Tweak the vibe, then Draft again — e.g. 'warmer', 'shorter', 'more direct'"
      value="${esc(REPLY_STYLE.get(c.id) || '')}"
      oninput="REPLY_STYLE.set('${esc(c.id)}', this.value)">
  </div>`;
}

// What they actually said. Stored automatically when gmail.readonly is on (CRM-4b); otherwise
// the operator pastes it. The drafter does not care which — but SOMETHING has to be here, or
// the "contextual" reply is a generic follow-up wearing a Re: subject line.
const REPLY_SAID = new Map();    // contact id -> pasted text, survives the 2.5s refresh
const REPLY_STYLE = new Map();   // contact id -> vibe directive
const SAID_EDIT = new Set();     // contact ids whose paste box is deliberately open
function editSaid(cid) { SAID_EDIT.add(cid); refresh(); }
function doneSaid(cid) { SAID_EDIT.delete(cid); refresh(); }

function lastReplyCard(c) {
  const r = c.last_reply;
  const editing = SAID_EDIT.has(c.id) || !r;
  if (!editing) {
    const tag = r.label ? `<span class="intent-chip ${esc(r.intent)}">${esc(r.label)}</span>` : '';
    const act = r.action ? `<div class="intent-act">${esc(r.action)}</div>` : '';
    return `<div class="said">
      <div class="said-hdr">${esc(r.from || 'They')} wrote ${tag}
        <button class="linklike" onclick="editSaid('${esc(c.id)}')">✎ edit</button></div>
      <div class="said-txt">“${esc(r.text)}”</div>${act}
    </div>`;
  }
  const text = REPLY_SAID.has(c.id) ? REPLY_SAID.get(c.id) : (r ? r.text : '');
  return `<div class="said">
    <div class="said-hdr">What they wrote
      ${r ? `<button class="linklike" onclick="doneSaid('${esc(c.id)}')">done</button>`
          : `<span class="said-why">paste it and the draft can actually answer it</span>`}</div>
    <textarea class="said-box" rows="4" placeholder="Paste their reply here…"
      oninput="REPLY_SAID.set('${esc(c.id)}', this.value)">${esc(text)}</textarea>
  </div>`;
}

// Feedback belongs NEXT TO THE BUTTON. Both of these used to write to #command at the very top
// of the page: with no reply text stored, clicking Draft returned a perfectly clear "paste what
// they wrote first" that rendered a full screen away from the click — reported, reasonably, as
// "the draft an answer button is not working".
const REPLY_MSG = new Map();     // contact id -> {text, bad} — survives the 2.5s refresh
function replyMsg(cid) {
  const m = REPLY_MSG.get(cid);
  return m ? `<div class="reply-msg ${m.bad ? 'bad' : 'good'}">${esc(m.text)}</div>` : '';
}
function setReplyMsg(cid, text, bad) {
  if (text) REPLY_MSG.set(cid, {text, bad: !!bad}); else REPLY_MSG.delete(cid);
}

async function draftReply(cid, btn) {
  const card = btn.closest('.reply-box');
  const box = card ? card.querySelector('.said-box') : null;
  const said = box ? box.value.trim() : (REPLY_SAID.get(cid) || '');
  btn.disabled = true; btn.textContent = 'Drafting…';
  setReplyMsg(cid, 'Reading the conversation and writing an answer…', false);
  const live = card ? card.querySelector('.reply-msg') : null;
  if (live) { live.textContent = 'Reading the conversation and writing an answer…'; live.className = 'reply-msg good'; }
  const r = await post('/api/contact/draft-reply',
                       {contact_id: cid, their_reply: said, style: REPLY_STYLE.get(cid) || ''});
  if (r.ok && r.body) {
    // Into the shared store, not straight into the DOM — the 2.5s refresh replaces #jobs
    // wholesale and would wipe a value written only to the textarea.
    REPLY_DRAFT.set(cid, r.body);
    if (said) { REPLY_SAID.set(cid, said); SAID_EDIT.delete(cid); }
    setReplyMsg(cid, r.message || 'Draft ready — read it before you send it.', false);
  } else {
    setReplyMsg(cid, r.message || 'Draft failed.', true);
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
  setTimeout(() => {
    const el = document.querySelector(`[data-reply-for="${cid}"] .reply-body`);
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
  setReplyMsg(cid, r.message || (r.ok ? 'Read.' : 'Could not read it.'), !r.ok);
  btn.disabled = false; btn.textContent = '⤓ Fetch from Gmail';
  refresh();
}
// From the banner: put the cursor in the composer. The contact is already open when the banner
// is visible, so this is a focus, not a navigation.
function openReplyHere(cid) {
  const el = document.querySelector(`[data-reply-for="${cid}"] .reply-body`);
  if (el) { el.focus(); el.scrollIntoView({block: 'center', behavior: 'smooth'}); }
}
function toggleCc(cid, address) {
  const set = REPLY_DROP.get(cid) || new Set();
  if (set.has(address)) set.delete(address); else set.add(address);
  REPLY_DROP.set(cid, set);
  refresh();
}
async function sendReply(cid, btn) {
  const card = btn.closest('.reply-box');
  const body = (REPLY_DRAFT.get(cid) || '').trim();
  const say = (m, bad) => { setReplyMsg(cid, m, bad); refresh(); };
  if (!body) { say('Write a reply before sending — or click “Draft an answer”.', true); return; }
  // The Cc travels as data, not as scraped chip text — the recipients of a real email are not
  // something to re-derive from innerText.
  let cc = [];
  try { cc = JSON.parse(card.dataset.cc || '[]'); } catch { cc = []; }
  const who = card.dataset.to || 'them';
  const also = cc.length ? `\n\nAlso going to: ${cc.join(', ')}` : '\n\nNobody is Cc\'d.';
  if (!confirm(`Send this reply to ${who}?${also}`)) return;
  btn.disabled = true; btn.textContent = 'Sending…';
  const r = await post('/api/contact/reply', {contact_id: cid, body, cc});
  setReplyMsg(cid, r.message || (r.ok ? 'Sent.' : 'Failed.'), !r.ok);
  if (r.ok) {
    // Clear ALL of it. REPLY_STYLE and REPLY_MSG were missed: the previous reply's vibe
    // directive would pre-fill the next one, and the green "replied to …" banner would stay
    // pinned under the composer for the rest of the session — reopening the contact an hour
    // later still showed it, indistinguishable from a fresh confirmation.
    REPLY_DRAFT.delete(cid); REPLY_DROP.delete(cid); REPLY_SAID.delete(cid);
    REPLY_STYLE.delete(cid);
    setTimeout(() => { REPLY_MSG.delete(cid); }, 6000);
  }
  else { btn.disabled = false; btn.textContent = 'Send reply'; }
  refresh();
}
function contactPanel(c) {
  // A tab with nothing behind it is not a choice. Offering all three regardless meant clicking
  // "LinkedIn" on an email-only contact got you "No LinkedIn profile." — and setChannel wrote
  // that dead choice into CHANNEL_TAB, so the contact reopened on the empty tab every time.
  const usable = {email: !!c.email, linkedin: !!c.linkedin_url, phone: true};
  const stored = CHANNEL_TAB.get(c.id);
  const ch = (stored && usable[stored]) ? stored
           : (c.email ? 'email' : (c.linkedin_url ? 'linkedin' : 'phone'));
  const tab = (k, label, on) => usable[k]
    ? `<span class="${ch === k ? 'on' : ''}" onclick="event.stopPropagation();setChannel('${esc(c.id)}','${k}')">${label}${on || ''}</span>`
    : '';
  let body = '';
  if (ch === 'email')    body = c.email ? emailChannel(c) : `<div class="pane-empty">No email address for ${esc(c.full_name)}.</div>`;
  if (ch === 'linkedin') body = c.linkedin_url ? linkedinChannel(c) : `<div class="pane-empty">No LinkedIn profile.</div>`;
  if (ch === 'phone')    body = smsChannel(c);
  return `<div class="pbody" onclick="event.stopPropagation()">
      <div class="cmeta">
        ${c.email ? `✉ <a href="mailto:${esc(c.email)}">${esc(c.email)}</a> ${emailBadge(c.email_status)}` : '✉ —'}
        ${c.linkedin_url ? ` · <a href="${esc(c.linkedin_url)}" target="_blank">LinkedIn ↗</a>` : ''}
        ${c.apollo_url ? ` · <a class="apollo-link" href="${esc(c.apollo_url)}" target="_blank" rel="noopener">Apollo ↗</a>` : ''}
        ${c.apollo_search_url ? `<a class="apollo-alt" href="${esc(c.apollo_search_url)}" target="_blank" rel="noopener">search ↗</a>` : ''}
        ${c.phone ? ` · 📱 <a href="tel:${esc(c.phone)}">${esc(c.phone)}</a> <a class="sms" href="sms:${esc(c.phone)}">text</a>` : ''}
        ${c.connection_company ? `<span class="conn-co"> · ${esc(c.connection_company)}</span>` : ''}
        ${c.verify_note ? `<div class="verify-note ${esc(c.confidence)}">${c.confidence === 'high' ? '✓' : '?'} ${esc(c.verify_note)}</div>` : ''}
        ${syncGmailBtn(c)}
      </div>
      <div class="chan">${tab('email','✉ Email')}${tab('linkedin','🔗 LinkedIn')}${tab('phone','💬 Text' + (c.sms_sent_at ? ' ✓' : ''))}</div>
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
function hasConversation(c) {
  return ((c.thread || []).some(m => m.direction === 'in'));
}
function emailChannel(c) {
  if (hasConversation(c)) return conversationView(c);
  // A due follow-up is the more urgent thing to write, so it takes the channel.
  if (c.followup_state === 'due' || (c.followup_message || '').trim())
    return followupCard(c, {touch: (c.followup_count || 0) + 1}, c.followup_total);
  return draftBlock(c, true);
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
function onNotesToggle(el, cid) { if (el.open) NOTES_OPEN.add(cid); else NOTES_OPEN.delete(cid); }
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
  return !!(el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.closest('#jobs'));
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
    if (j.status === 'rejected' || j.interview_at) continue;
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
    ...ch, n: js.reduce((a, j) => a + (j.status === 'rejected' ? 0 : dueByChannel(j)[ch.name]), 0),
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
function renderJobsTable(allJobs, editing) {
  renderJobFilters(allJobs);
  renderActiveTags();
  // Bucket → tags → search, narrowing at each step. The bucket counts above deliberately keep
  // counting the WHOLE set: a filter pill that renumbers itself as you type cannot tell you
  // where the thing you are searching for lives.
  const shown = allJobs
    .filter(j => jobInBucket(j, JOB_FILTER))
    .filter(jobMatchesTags)
    .filter(jobMatchesQuery);
  const emptyEl = document.getElementById('jobsEmpty');
  if (emptyEl) {
    emptyEl.hidden = shown.length > 0;
    // Say which filter emptied the table, and offer the way out. "No applications in All" is
    // the message a naive version prints while a search term is quietly hiding everything.
    const bits = [];
    if (JOB_QUERY.trim()) bits.push(`matching “${JOB_QUERY.trim()}”`);
    if (TAG_FILTER.size) bits.push(`with ${TAG_FILTER.size} tag filter${TAG_FILTER.size > 1 ? 's' : ''}`);
    emptyEl.textContent = allJobs.length === 0 ? ''
      : bits.length ? `No applications ${bits.join(' ')}.`
      : `No applications in "${JOB_BUCKETS[JOB_FILTER].label}".`;
  }
  // The one destructive write: replacing #jobs discards whatever is being typed inside it.
  // Everything above has already run, so the header, badge and logs stay live while you type.
  if (editing) return;
  document.getElementById('jobs').innerHTML = shown.map(j => {
    return `
    <tr class="${j.interview_at ? 'row-won' : ''}">
      <td class="status-cell"><div class="status-head">${badge(j.status)}${j.interview_at ? ` <span class="won-chip" title="Scheduled ${esc(fmtDate(j.interview_at))}">${wonLabel(j).icon} ${esc(wonLabel(j).label.toLowerCase())}</span>` : ''}</div>${j.status === 'rejected' && j.rejected_at ? `<div class="rejected-on">Rejected ${fmtDate(j.rejected_at)}</div>` : (j.applied_at ? `<div class="applied-on">Applied ${fmtDate(j.applied_at)}</div>` : '')}</td>
      <td class="job-cell"><div class="job-title">${esc(j.title)}</div><div class="job-co">${esc(j.company)}</div>${matchedVia(j)}</td>
      <td class="desc"><div class="desc-text">${esc(j.description)}</div></td>
      <td class="tags-cell">${jobTags(j).map(t =>
        `<button class="tag-chip${TAG_FILTER.has(t.k) ? ' on' : ''}" onclick="event.stopPropagation();toggleTag(${tagArg(t.k)})" title="Filter by ${esc(t.value)}">${esc(t.label)}</button>`
      ).join('') || '<span class="tags-none">—</span>'}</td>
    </tr>
    <tr class="job-foot"><td colspan="4">
      ${stepStrip(j)}
      ${PANEL_OPEN.has(j.url) ? jobTabs(j) + `<div class="pane">${jobPane(j)}</div>` : ''}
    </td></tr>`;
  }).join('');
  // A <details> restored with the `open` attribute does NOT fire `toggle` on parse, so the
  // 2.5s refresh would leave an already-open menu unpositioned. Re-measure them here.
  document.querySelectorAll('details.rowmenu[open]').forEach(positionRowMenu);
}

// Re-filter without hitting the network. LAST_JOBS is the payload the most recent refresh
// already fetched.
function rerenderJobs() { renderJobsTable(LAST_JOBS || [], isEditingJobs()); }

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
  renderSpaceShape(data.space_shape, data.space_offer);
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
  CONTENT_SCOPE = !!data.content_scope;
  if (data.poll_every_s) POLL_EVERY_S = data.poll_every_s;
  const allJobs = data.jobs || [];
  // Kept for handlers that need the payload AFTER a click rather than during render — the
  // bulk Gmail fetch has to know which contacts a job has, and an inline onclick cannot be
  // handed an array.
  LAST_JOBS = allJobs;
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
  btn.disabled = true;
  const r = await post('/api/mark-rejected', {url});
  if (r.ok) refresh(); else { btn.disabled = false; alert(r.message || 'Failed'); }
}
async function unmarkRejected(url, btn) {
  btn.disabled = true;
  const r = await post('/api/unmark-rejected', {url});
  if (r.ok) refresh(); else { btn.disabled = false; alert(r.message || 'Failed'); }
}
// The People toggle in the footer: the expandable contacts panel when contacts exist, or a
// Round two. Offered ONLY when the first round is genuinely spent: everybody found has been
// written to, every ladder on every channel has run out, and nobody answered. Showing it any
// earlier competes with the follow-ups that have not been sent yet — and the cheapest next
// move is always finishing the sequence you already started, not buying more contacts.
//
// It spends Apollo credits, so the label says what it will do rather than being a bare verb.
function anotherRoundPrompt(j, cs) {
  if (!NET_AVAIL || !cs.length) return '';

  // ALWAYS rendered once contacts exist, and disabled with the reason when it is not the right
  // move. The first version returned '' unless every ladder was spent, which is correct
  // behaviour and unusable feedback — reported as "I'm not seeing the button" while looking at
  // a job whose sequences were simply still running. §Lessons 41, written two commits before
  // this one and then repeated: a control that is conditionally ABSENT reads as a control that
  // is missing, and the operator cannot tell "not yet" from "broken".
  const answered = cs.filter(c => c.replied_at || (c.conversation || {}).state === 'awaiting_us');
  const spent = cs.filter(c => c.exhausted);
  const untouched = cs.filter(c => !c.exhausted && !c.emailed
                                   && !(c.dm_status === 'sent' || c.dm_status === 'manual'));
  const running = cs.length - spent.length - untouched.length - answered.length;

  let why = '', head = '';
  if (answered.length) {
    // Somebody is talking to you. Buying more strangers is never the next move (§Lessons 27).
    why = `${answered.map(c => esc(firstName(c.full_name))).join(', ')} ${answered.length > 1 ? 'are' : 'is'} waiting on you — answer first.`;
    head = `<b>Someone replied.</b>`;
  } else if (running > 0) {
    why = `${running} sequence${running > 1 ? 's are' : ' is'} still running. Finishing what you started is free; this is not.`;
    head = `<b>Not yet.</b>`;
  } else if (untouched.length) {
    why = `${untouched.length} contact${untouched.length > 1 ? 's have' : ' has'} never been written to. Send those before buying more.`;
    head = `<b>Not yet.</b>`;
  } else {
    head = `<b>No response from any of the ${cs.length}.</b>`;
    why = 'Every follow-up has been sent and nobody replied.';
  }
  const ready = !why || (!answered.length && running === 0 && !untouched.length);
  const busy = j.network_running;
  const dis = (busy || !ready) ? 'disabled' : '';
  const label = busy ? '⏳ looking for new people…' : '🔄 Find a new round of contacts';
  return `<div class="round2${ready ? ' ready' : ''}">
      <div class="round2-txt">${head} ${esc(why)}</div>
      <button class="secondary" ${dis}
        title="${ready ? 'Searches this company again, skipping everyone above, and drafts fresh outreach. Spends Apollo credits.' : esc(why)}"
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
      if (s.state === 'done') { cls += ' done'; mark = '✓'; }
      else if (s.state === 'na') { cls += ' na'; mark = '–'; }
      else if (!currentFound) { cls += ' now'; mark = s.key === 'followup' ? '↻' : '!'; currentFound = true; }
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
  if (j.status === 'rejected') return '';
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
    return `<button class="secondary" onclick="restartJob(${u}, this, false)">🔄 Restart end-to-end</button>`;
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
  return dueByChannel(j).total ? 'followups' : 'people';
}
function jobTabs(j) {
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const cur = activeTab(j);
  const defs = [
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
    : `<div class="jd-desc">${esc(open && full ? full : excerpt)}${
        !open && excerpt.length >= 900 ? '…' : ''}</div>
       ${excerpt.length >= 900 ? `<button class="linklike" onclick="toggleJobDesc(${
         `decodeURIComponent('${encodeURIComponent(j.url)}')`}, this)">${
         open ? 'Show less' : 'Show the full description'}</button>` : ''}`;

  return `<div class="jd">
    ${links}
    <div class="jd-facts">
      ${row('Title', esc(j.title))}
      ${row('Company', esc(j.company || j.contact_company))}
      ${row('Location', esc(j.location))}
      ${row('Salary', esc(j.salary))}
      ${row('Fit', score)}
      ${row('Status', esc(j.status) + (j.applied_at ? ` · applied ${esc(fmtDate(j.applied_at))}` : ''))}
      ${row('Attempts', j.apply_attempts ? String(j.apply_attempts) : '')}
      ${urls}
    </div>
    <div class="jd-label">Description</div>
    ${desc}
  </div>`;
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

function jobPane(j) {
  const t = activeTab(j);
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
function followupBody(j, f) {
  const byId = {}; (j.contacts || []).forEach(c => byId[c.id] = c);
  let out = `<div class="fu-sched">Sequence: ${f.schedule.map((h,i)=>`touch ${i+1} at ${fuWhen(h).replace('in ','')}`).join(' · ')}</div>`;
  if (f.due.length) {
    out += f.due.map(d => followupCard(byId[d.id], d, f.total_touches)).join('');
  } else {
    out += `<div class="fu-empty">Nothing due right now.</div>`;
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
// Explanatory line under the strip, for the states where the next action needs context.
function nextHint(j) {
  if (j.status === 'ready_to_submit')
    return 'Review &amp; submit in the open Chrome window, then confirm.';
  if (j.status === 'needs_human')
    return esc(BLOCKER_ASK[j.apply_error] || BLOCKER_ASK.blocker);
  if (j.status === 'failed')
    return `${j.apply_error ? esc(j.apply_error) : 'Last attempt failed.'} Regenerates materials, then re-applies.`;
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
  if (['applied', 'ready_to_submit', 'rejected', 'in_progress'].includes(j.status)) return '';
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
  if (j.status === 'rejected' || j.status === 'in_progress') return '';
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
  if (j.status === 'in_progress' || j.status === 'rejected' || j.status === 'failed') return '';
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
  items.push(j.status === 'rejected'
    ? `<button onclick="unmarkRejected(${u}, this)">↩ Restore<span>Move back out of the rejected pile</span></button>`
    : `<button onclick="markRejected(${u}, this)">✕ Mark rejected<span>Move to the rejected pile</span></button>`);
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
async function markSubmitted(url, btn) {
  // The user has reviewed + submitted the filled application in the open Chrome window.
  if (!confirm('Confirm you reviewed and submitted this application in the browser?')) return;
  btn.disabled = true; btn.textContent = 'Saving…';
  const r = await post('/api/mark-submitted', {url});
  if (r.ok) { refresh(); } else { btn.disabled = false; btn.textContent = 'Mark submitted ✓'; alert(r.message || 'Failed'); }
}
setInterval(refresh, 2500);
refresh();
