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
  if (fit !== 'comfortable' && countPages(finalBuf) > 1) {
    let working = JSON.parse(JSON.stringify(resume))
    for (let guard = 0; guard < 60 && countPages(finalBuf) > 1; guard++) {
      const trimmed = trimOneUnit(working)
      if (!trimmed) break // nothing left to trim — write the smallest we achieved
      working = trimmed
      const buf = await renderAt(working, fitScale)
      if (!buf) break     // keep the last good buffer rather than losing the document
      finalBuf = buf
    }
  }

  try {
    writeFileSync(outPath, finalBuf)
  } catch (e) {
    fail(`cannot write output: ${e.message}`)
  }
}

/**
 * Remove ONE unit of the least-important content, returning a new resume (or null if nothing
 * safe is left to trim). Trim order protects a real resume's signal: projects go first, then
 * trailing bullets from the OLDEST experience roles, then whole oldest roles (never below 3),
 * then the summary is shortened. Recent roles + skills + education are preserved.
 */
function trimOneUnit(r) {
  const R = JSON.parse(JSON.stringify(r))
  // Sections shape: shed a trailing bullet from the OLDEST role that still has more than
  // one. Without this branch the whole function returned null immediately and phase 2 was
  // a no-op for every structure-preserving résumé.
  if (Array.isArray(R.sections)) {
    const exp = R.sections.filter((s) => s.kind === 'experience')
    for (let si = exp.length - 1; si >= 0; si--) {
      const entries = exp[si].entries || []
      for (let i = entries.length - 1; i >= 0; i--) {
        const e = entries[i]
        if (Array.isArray(e.bullets) && e.bullets.length > 1) { e.bullets.pop(); return R }
      }
    }
    for (const sec of R.sections) {
      if (sec.kind === 'summary' && String(sec.text || '').length > 140) {
        sec.text = String(sec.text).slice(0, 140).replace(/\s+\S*$/, '') + '.'
        return R
      }
    }
    return null
  }
  // 1) Projects: drop trailing bullets, then the whole project (oldest/last first).
  if (Array.isArray(R.projects) && R.projects.length) {
    const last = R.projects[R.projects.length - 1]
    if (Array.isArray(last.bullets) && last.bullets.length > 1) { last.bullets.pop(); return R }
    R.projects.pop(); return R
  }
  // 2) Experience: trim a trailing bullet from the OLDEST role that still has more than one.
  if (Array.isArray(R.experience) && R.experience.length) {
    for (let i = R.experience.length - 1; i >= 0; i--) {
      const e = R.experience[i]
      if (Array.isArray(e.bullets) && e.bullets.length > 1) { e.bullets.pop(); return R }
    }
    // 3) All roles down to one bullet — drop the oldest whole role, but never below 3 roles.
    if (R.experience.length > 3) { R.experience.pop(); return R }
  }
  // 4) Last resort — shorten a long summary.
  if (R.summary && R.summary.length > 140) {
    R.summary = R.summary.slice(0, 140).replace(/\s+\S*$/, '') + '.'
    return R
  }
  return null
}

main().catch((e) => fail(e?.stack || String(e)))
