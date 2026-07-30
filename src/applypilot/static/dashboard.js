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
    const data = await (await fetch('/api/status')).json();
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
  const status = await (await fetch('/api/status')).json();
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
    const status = await (await fetch('/api/status')).json();
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
async function findContacts(url) {
  const r = await post('/api/network', {url, per_job: 5});
  if (!r.ok) alert(r.message || 'Could not start');
  refresh();
}
function emailBadge(s) {
  if (s === 'verified') return '<span class="ebadge ok">verified</span>';
  if (s === 'unverified') return '<span class="ebadge warn">unverified</span>';
  return '<span class="ebadge none">no email</span>';
}
let GMAIL_AVAIL = false;
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
async function saveLinkedin(cid, btn) {
  const d = btn.closest('.draft');
  await post('/api/outreach', {contact_id: cid,
    subject: d.querySelector('.d-subj').value, body: d.querySelector('.d-body').value,
    linkedin: d.querySelector('.d-linkedin').value});
  btn.textContent = 'Saved ✓'; setTimeout(()=>btn.textContent='Save note', 1200);
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
  btn.disabled = true; btn.textContent = 'Sending…';
  const r = await post('/api/outreach/send', {contact_id: cid, confirm_unverified: !verified});
  if (r.ok) { refresh(); }
  else { btn.disabled = false; btn.textContent = 'Send email'; alert(r.message || 'Send failed'); }
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
function peopleList(j) {
  const cs = j.contacts || [];
  if (!cs.length) return `<div class="pane-empty">No contacts yet. ${findContactsPrompt(j)}</div>`;
  const hot = cs.filter(c => c.hot), cold = cs.filter(c => !c.hot);
  let out = bulkBar(j);
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
  if (c.replied_at) pills.push(`<span class="pill on">✓ replied ${esc(shortDate(c.replied_at))}</span>`);
  else if (c.followup_state === 'due')      pills.push(`<span class="pill due">↻ due</span>`);
  else if (c.followup_status === 'replied') pills.push(`<span class="pill on">✓ replied</span>`);
  else if (c.followup_state === 'waiting')  pills.push(`<span class="pill off">↻ ${fuWhen(c.followup_due_in_h)}</span>`);
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
function threadView(c) {
  const msgs = c.thread || [];
  if (msgs.length < 2) return '';   // one outbound message is not a conversation
  const rows = msgs.map(m => {
    const who = m.direction === 'in' ? (m.from_name || m.from_addr) : 'You';
    const cc = (m.cc_addrs || []).length ? ` <span class="th-cc">cc ${(m.cc_addrs||[]).map(esc).join(', ')}</span>` : '';
    return `<div class="th-row ${m.direction}"><span class="th-who">${esc(who)}</span>` +
           `<span class="th-when">${esc(shortDate(m.sent_at))}</span>${cc}</div>`;
  }).join('');
  const intro = c.introduced_by
    ? `<div class="th-intro">👋 ${esc(c.introduced_by)} added them to this thread</div>` : '';
  return `<details class="thread"><summary>💬 Conversation (${msgs.length})</summary>${intro}${rows}</details>`;
}
function contactPanel(c) {
  const ch = CHANNEL_TAB.get(c.id) || (c.email ? 'email' : (c.linkedin_url ? 'linkedin' : 'phone'));
  const tab = (k, label, on) => `<span class="${ch === k ? 'on' : ''}" onclick="event.stopPropagation();setChannel('${esc(c.id)}','${k}')">${label}${on || ''}</span>`;
  let body = '';
  if (ch === 'email')    body = c.email ? emailChannel(c) : `<div class="pane-empty">No email address for ${esc(c.full_name)}.</div>`;
  if (ch === 'linkedin') body = c.linkedin_url ? linkedinChannel(c) : `<div class="pane-empty">No LinkedIn profile.</div>`;
  if (ch === 'phone')    body = contactNotes(c);
  return `<div class="pbody" onclick="event.stopPropagation()">
      <div class="cmeta">
        ${c.email ? `✉ <a href="mailto:${esc(c.email)}">${esc(c.email)}</a> ${emailBadge(c.email_status)}` : '✉ —'}
        ${c.linkedin_url ? ` · <a href="${esc(c.linkedin_url)}" target="_blank">LinkedIn ↗</a>` : ''}
        ${c.apollo_url ? ` · <a class="apollo-link" href="${esc(c.apollo_url)}" target="_blank" rel="noopener">Apollo ↗</a>` : ''}
        ${c.apollo_search_url ? `<a class="apollo-alt" href="${esc(c.apollo_search_url)}" target="_blank" rel="noopener">search ↗</a>` : ''}
        ${c.phone ? ` · 📱 <a href="tel:${esc(c.phone)}">${esc(c.phone)}</a> <a class="sms" href="sms:${esc(c.phone)}">text</a>` : ''}
        ${c.connection_company ? `<span class="conn-co"> · ${esc(c.connection_company)}</span>` : ''}
        ${c.verify_note ? `<div class="verify-note ${esc(c.confidence)}">${c.confidence === 'high' ? '✓' : '?'} ${esc(c.verify_note)}</div>` : ''}
      </div>
      <div class="chan">${tab('email','✉ Email')}${tab('linkedin','🔗 LinkedIn')}${tab('phone','📇 Phone & notes')}</div>
      ${threadView(c)}
      ${body}
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
  refresh();
}
function emailChannel(c) {
  // A due follow-up is the more urgent thing to write, so it takes the channel.
  if (c.followup_state === 'due' || (c.followup_message || '').trim())
    return followupCard(c, {touch: (c.followup_count || 0) + 1}, 3);
  return draftBlock(c, true);
}
function linkedinChannel(c) { return draftBlock(c, false, true); }

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
  return `<div class="bulkbar">${emailBtn}<span class="li-hint">LinkedIn: use “Compose on LinkedIn” per contact →</span><span class="bulknote" data-bulk="${esc(j.url)}"></span></div>`;
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
  await post('/api/outreach', {contact_id: cid, subject: d.querySelector('.d-subj').value,
    body: d.querySelector('.d-body').value, linkedin: d.querySelector('.d-linkedin').value});
  btn.textContent = 'Saved ✓'; setTimeout(()=>btn.textContent='Save', 1200);
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
function needsYou(j) {
  if (j.status === 'ready_to_submit' || j.status === 'needs_human') return true;
  const f = j.followups || {};
  return ((f.due_count || 0) + (f.li_due_count || 0)) > 0;
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

async function refresh() {
  if (isEditingJobs()) return;
  const data = await (await fetch('/api/status')).json();
  document.getElementById('appDir').textContent = data.app_dir;
  const s = data.stats || {};
  const stats = [['URL Jobs',s.total],['URL Applied',s.applied],['Lifetime Applied',s.lifetime_applied],['Enriched',s.enriched],['User-approved',s.scored],['Tailored',s.tailored],['Covers',s.covers],['Ready',s.ready],['Errors',s.errors]];
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
  const allJobs = data.jobs || [];
  renderJobFilters(allJobs);
  const shown = allJobs.filter(j => jobInBucket(j, JOB_FILTER));
  const emptyEl = document.getElementById('jobsEmpty');
  if (emptyEl) {
    emptyEl.hidden = shown.length > 0;
    emptyEl.textContent = allJobs.length === 0 ? '' : `No applications in "${JOB_BUCKETS[JOB_FILTER].label}".`;
  }
  document.getElementById('jobs').innerHTML = shown.map(j => {
    return `
    <tr>
      <td class="status-cell"><div class="status-head">${badge(j.status)}</div>${j.status === 'rejected' && j.rejected_at ? `<div class="rejected-on">Rejected ${fmtDate(j.rejected_at)}</div>` : (j.applied_at ? `<div class="applied-on">Applied ${fmtDate(j.applied_at)}</div>` : '')}</td>
      <td class="job-cell"><div class="job-title">${esc(j.title)}</div><div class="job-co">${esc(j.company)}</div></td>
      <td class="desc"><div class="desc-text">${esc(j.description)}</div></td>
      <td class="links-cell"><a href="${esc(j.url)}" target="_blank">job</a>${j.application_url ? `<br><a href="${esc(j.application_url)}" target="_blank">apply page</a>` : ''}</td>
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
      <div class="next">${na ? `<span class="next-label">Next</span>${na}` : `<span class="next-done">🏆 fully worked</span>`}${signinButton(j)}${restartButton(j)}${rowMenu(j)}</div>
    </div>${signinBar(j)}${hint ? `<div class="strip-hint">${hint}</div>` : ''}`;
}
// The ONE thing to do next, in priority order. Returns '' when the job is fully worked.
function nextAction(j) {
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const cl = j.checklist || {}, f = j.followups || {};
  const cs = j.contacts || [];
  if (j.status === 'rejected') return '';
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
  const dueN = (f.due_count || 0) + (f.li_due_count || 0);
  if (dueN)
    return `<button class="amber" onclick="openTab(${u},'followups')">↻ ${dueN} follow-up${dueN>1?'s':''} due</button>`;
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
  return (j.followups && j.followups.due_count) ? 'followups' : 'people';
}
function jobTabs(j) {
  const u = `decodeURIComponent('${encodeURIComponent(j.url)}')`;
  const cur = activeTab(j);
  const f = j.followups || {};
  const defs = [
    ['people',    'People',     (j.contacts || []).length, false],
    ['followups', 'Follow-ups', (f.due_count + (f.li_due_count || 0)) || 0,
      !!(f.due_count || f.li_due_count)],
    ['materials', 'Materials',  (j.materials || []).length, false],
    ['activity',  'Activity',   (j.activity || []).length, false],
  ];
  return `<div class="tabs">` + defs.map(([k, label, n, due]) =>
    `<button class="tab ${cur === k ? 'on' : ''}" onclick="openTab(${u},'${k}')">${label}${n ? ` <span class="n ${due?'due':''}">${n}</span>` : ''}</button>`
  ).join('') + `</div>`;
}
function jobPane(j) {
  const t = activeTab(j);
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
function signinButton(j) {
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
// Re-apply stays visible on the row rather than living only in the ⋯ menu: on an applied
// job it is the main thing you might still want, and burying it made it unfindable.
function restartButton(j) {
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
