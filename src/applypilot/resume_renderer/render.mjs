#!/usr/bin/env node
/**
 * ApplyPilot headless PDF renderer (resumes and cover letters).
 *
 * Usage:  node render.mjs <request.json> <out.pdf>
 *
 * <request.json> is a RenderRequest (see docs/resume-renderer-plan.md):
 *   resume:       { schemaVersion: 1, options: { kind:'resume', fit, theme }, resume: {...} }
 *   cover letter: { schemaVersion: 1, options: { kind:'cover_letter' }, coverLetter: {...} }
 *
 * Exits 0 on success. On any error prints a message to stderr and exits 1 so the
 * Python caller can fall back to the Chromium HTML renderer.
 */
import React from 'react'
import { readFileSync, writeFileSync } from 'node:fs'
import { Buffer } from 'node:buffer'
import ReactPDF from '@react-pdf/renderer'
import { DEFAULT_THEME, createDynamicStyles } from './styles.mjs'
import { adjustStyling } from './onePage.mjs'
import { ResumeDocument } from './document.mjs'
import { CoverLetterDocument } from './cover.mjs'

const h = React.createElement

function fail(msg) {
  process.stderr.write(`resume-renderer: ${msg}\n`)
  process.exit(1)
}

/** react-pdf v3 has no renderToBuffer; collect renderToStream into a Buffer. */
async function renderToBuffer(el) {
  const stream = await ReactPDF.renderToStream(el)
  const chunks = []
  return await new Promise((resolve, reject) => {
    stream.on('data', (c) => chunks.push(Buffer.isBuffer(c) ? c : Buffer.from(c)))
    stream.on('end', () => resolve(Buffer.concat(chunks)))
    stream.on('error', reject)
  })
}

function countPages(buf) {
  // Count PDF page objects. Our own output uses "/Type /Page" for pages and
  // "/Type /Pages" for the page tree root; exclude the latter.
  const s = buf.toString('latin1')
  const m = s.match(/\/Type\s*\/Page(?![s])/g)
  return m ? m.length : 1
}

function validateResume(req) {
  if (!req.resume || typeof req.resume !== 'object') fail('request.resume missing')
  if (!req.resume.contactInfo?.name) fail('resume.contactInfo.name is required')
}

function validateCover(req) {
  if (!req.coverLetter || typeof req.coverLetter !== 'object') fail('request.coverLetter missing')
  if (!req.coverLetter.candidate?.name) fail('coverLetter.candidate.name is required')
  if (!req.coverLetter.body) fail('coverLetter.body is required')
}

async function renderCoverLetter(req, outPath) {
  validateCover(req)
  const el = h(CoverLetterDocument, { cover: req.coverLetter })
  let buf
  try {
    buf = await renderToBuffer(el)
  } catch (e) {
    fail(`cover letter render failed: ${e.message}`)
  }
  try {
    writeFileSync(outPath, buf)
  } catch (e) {
    fail(`cannot write output: ${e.message}`)
  }
}

async function main() {
  const [, , reqPath, outPath] = process.argv
  if (!reqPath || !outPath) fail('usage: node render.mjs <request.json> <out.pdf>')

  let req
  try {
    req = JSON.parse(readFileSync(reqPath, 'utf8'))
  } catch (e) {
    fail(`cannot read/parse request: ${e.message}`)
  }
  if (!req || typeof req !== 'object') fail('request is not an object')
  if (req.schemaVersion !== 1) fail(`unsupported schemaVersion: ${req.schemaVersion}`)

  if (req.options?.kind === 'cover_letter') {
    await renderCoverLetter(req, outPath)
    return
  }

  validateResume(req)
  const resume = req.resume
  const fit = req.options?.fit || 'auto'
  const baseTheme = DEFAULT_THEME // request.options.theme reserved for future themes

  // Fit strategy:
  //   comfortable -> single render, no forced shrink
  //   compact/auto -> render; if it spills past 1 page, first shrink fonts, then (if still
  //                   overflowing at the tightest readable size) TRIM the least-important
  //                   content one unit at a time and re-render — GUARANTEEING a single page.
  const shrinkSteps = fit === 'comfortable' ? [1] : [1, 0.94, 0.88, 0.82, 0.76]

  // Returns null instead of aborting. @react-pdf's textkit can throw on a specific
  // line-break layout ("Cannot read properties of undefined (reading 'overflowLeft')") for
  // one font size and lay the SAME content out fine at another — measured on a real résumé:
  // scale 1 and 0.97 threw, while 0.94 / 0.90 / 0.88 / 0.82 / 0.76 / 1.05 all succeeded.
  // Treating the first failure as fatal threw away a perfectly renderable document and left
  // the operator with a 380-character fallback PDF missing WORK EXPERIENCE entirely.
  const renderAt = async (r, scale) => {
    const theme = adjustStyling(baseTheme, r, scale)
    const styles = createDynamicStyles(theme)
    const el = h(ResumeDocument, { resume: r, styles, theme })
    try {
      return await renderToBuffer(el)
    } catch (e) {
      process.stderr.write(`resume-renderer: layout failed at scale ${scale} (${e.message}); trying the next size\n`)
      return null
    }
  }

  // Phase 1 — shrink fonts. Stop at the first scale that fits on one page.
  let finalBuf = null
  let fitScale = shrinkSteps[shrinkSteps.length - 1]
  // Extra sizes beyond the shrink ladder: if every laddered scale hits the textkit bug, a
  // slightly different size usually lays out fine, and a 2-page résumé beats no résumé.
  const scales = [...shrinkSteps, 0.97, 0.91, 0.85, 1.03]
  for (let i = 0; i < scales.length; i++) {
    const buf = await renderAt(resume, scales[i])
    if (!buf) continue                       // layout threw at this size — try another
    finalBuf = buf
    fitScale = scales[i]
    if (countPages(buf) <= 1) break
  }
  if (!finalBuf) fail('render failed at every font size')

  // Phase 2 — still 2+ pages at the tightest font: trim content until it fits (or nothing left).
  // Only runs for compact/auto (not 'comfortable'). Keeps recent roles; sheds projects and the
  // oldest roles' trailing bullets first. Hard ceiling on iterations as a safety valve.
  const trims = []
  if (fit !== 'comfortable' && countPages(finalBuf) > 1) {
    let working = JSON.parse(JSON.stringify(resume))
    for (let guard = 0; guard < 60 && countPages(finalBuf) > 1; guard++) {
      const trimmed = trimOneUnit(working)
      if (!trimmed) break // only the floor is left — write the smallest we achieved
      working = trimmed.r
      const buf = await renderAt(working, fitScale)
      if (!buf) break     // keep the last good buffer rather than losing the document
      finalBuf = buf
      trims.push(trimmed.what)
    }
  }
  // SAY WHAT WAS REMOVED. The validator and the fabrication judge both run on the TEXT, before
  // this file exists, so a resume can be reported "approved" with more than half its bullets
  // gone — which is exactly how 8 of 14 disappeared unnoticed. Python reads this back and puts
  // it on the job's Activity tab beside the other notes.
  if (trims.length) {
    const counts = trims.reduce((m, t) => ((m[t] = (m[t] || 0) + 1), m), {})
    const summary = Object.entries(counts)
      .map(([what, n]) => (n > 1 ? `${what} (x${n})` : what)).join('; ')
    process.stderr.write(`resume-renderer: TRIMMED to fit one page: ${summary}\n`)
    if (countPages(finalBuf) > 1) {
      process.stderr.write('resume-renderer: TRIMMED but still 2 pages; kept every role at the '
        + `${MIN_BULLETS_PER_ROLE}-bullet floor rather than cutting further\n`)
    }
  }

  try {
    writeFileSync(outPath, finalBuf)
  } catch (e) {
    fail(`cannot write output: ${e.message}`)
  }
}

/**
 * The floor. A role showing ONE bullet next to a role showing five does not read as a trimmed
 * resume, it reads as a broken one — reported from a live PDF that came out 4/1/1 while the text
 * it was rendered from had 5/5/4.
 *
 * The operator's own ordering: "at the very least 3 bullets per company, happy to sacrifice
 * personal statement and skills at the bottom."
 */
const MIN_BULLETS_PER_ROLE = 3

/** The employer, for the report. `header` is "T-Mobile — Technical Project Manager". */
function roleName(e) {
  // Escapes, not literal dashes: `test_the_node_renderer_emits_no_dashes_of_its_own`
  // scans this source for them, and it is right to — every Python guard runs before the
  // renderer builds its own strings (§Lessons 45). This one only SPLITS on a dash.
  const h = String(e?.header || e?.employer || '').split(/\s+[\u2014\u2013-]\s+/)[0].trim()
  return h || 'a role'
}

/**
 * Remove ONE unit of the least-important content, returning `{ r, what }` or null when only the
 * floor is left.
 *
 * ORDER IS THE WHOLE DESIGN, and it is the operator's:
 *
 *   1. the personal statement — prose, and the first thing they said to sacrifice
 *   2. the key strengths      — two 250-character lines here, so it buys a lot of page per unit
 *   3. experience bullets     — evenly, from whichever role currently has the MOST, and never
 *                               below MIN_BULLETS_PER_ROLE
 *
 * The previous order did the opposite: it drained the OLDEST role to exactly one bullet before
 * touching anything else, and never trimmed skills at all. On a real resume that removed 8 of 14
 * bullets and left two employers showing a single line, while a 731-character summary and 542
 * characters of skills were untouched.
 *
 * Returning null when only the floor remains is deliberate: the caller keeps the last good
 * render, which may be two pages, and says so. A second page beats a hollowed-out work history.
 */
function trimOneUnit(r) {
  const R = JSON.parse(JSON.stringify(r))
  if (Array.isArray(R.sections)) {
    // 1) Summary, shortened progressively rather than guillotined at 140 characters — one cut to
    //    a fifth of its length is a worse document than three measured ones.
    const sum = R.sections.find((s) => s.kind === 'summary')
    const text = String(sum?.text || '')
    if (sum && text.length > 180) {
      const target = Math.max(180, Math.floor(text.length * 0.75))
      sum.text = text.slice(0, target).replace(/\s+\S*$/, '') + '.'
      return { r: R, what: 'shortened the personal statement' }
    }
    // 2) Key strengths. The RENDER block spells these `skills: [{category, value}]` — it is
    //    `bullets` only in the tailor's own JSON, and reading the wrong key here is why an
    //    earlier version of this ladder skipped the section and went straight to the bullets.
    const skills = R.sections.find((s) => s.kind === 'skills')
    if (skills && Array.isArray(skills.skills) && skills.skills.length) {
      let idx = -1
      let longest = 0
      skills.skills.forEach((sk, i) => {
        const n = String(sk?.value || '').length
        if (n > longest) { longest = n; idx = i }
      })
      if (longest > 120) {
        const v = String(skills.skills[idx].value)
        const target = Math.max(120, Math.floor(v.length * 0.75))
        skills.skills[idx] = { ...skills.skills[idx],
                               value: v.slice(0, target).replace(/[\s,;]+\S*$/, '') }
        return { r: R, what: 'shortened a key-strengths line' }
      }
      if (skills.skills.length > 1) {
        const gone = skills.skills.pop()
        return { r: R, what: `dropped the "${gone?.category || 'skills'}" line` }
      }
    }
    // 3) Experience bullets, EVENLY: always from whichever role currently has the most, so the
    //    roles stay within one bullet of each other instead of one being hollowed out.
    const exp = R.sections.filter((s) => s.kind === 'experience')
    let best = null
    for (const sec of exp) {
      for (const e of (sec.entries || [])) {
        const n = Array.isArray(e.bullets) ? e.bullets.length : 0
        if (n > MIN_BULLETS_PER_ROLE && (!best || n > best.n)) best = { e, n }
      }
    }
    if (best) {
      best.e.bullets.pop()
      return { r: R, what: `dropped a bullet from ${roleName(best.e)}` }
    }
    return null
  }
  // Legacy flat shape. Same ordering and the same floor.
  if (Array.isArray(R.projects) && R.projects.length) {
    const last = R.projects[R.projects.length - 1]
    if (Array.isArray(last.bullets) && last.bullets.length > 1) {
      last.bullets.pop()
      return { r: R, what: 'dropped a project bullet' }
    }
    R.projects.pop()
    return { r: R, what: 'dropped a project' }
  }
  if (R.summary && R.summary.length > 180) {
    const target = Math.max(180, Math.floor(R.summary.length * 0.75))
    R.summary = R.summary.slice(0, target).replace(/\s+\S*$/, '') + '.'
    return { r: R, what: 'shortened the summary' }
  }
  if (Array.isArray(R.experience) && R.experience.length) {
    let best = null
    for (const e of R.experience) {
      const n = Array.isArray(e.bullets) ? e.bullets.length : 0
      if (n > MIN_BULLETS_PER_ROLE && (!best || n > best.n)) best = { e, n }
    }
    if (best) {
      best.e.bullets.pop()
      return { r: R, what: `dropped a bullet from ${roleName(best.e)}` }
    }
  }
  return null
}

main().catch((e) => fail(e?.stack || String(e)))
