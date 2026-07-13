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
import { CoverLetterDocument, createCoverStyles } from './cover.mjs'

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
  const styles = createCoverStyles(req.coverLetter.styling || {})
  const el = h(CoverLetterDocument, { cover: req.coverLetter, styles })
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
  //   compact/auto -> render, and if it spills past 1 page, shrink and retry
  const shrinkSteps = fit === 'comfortable' ? [1] : [1, 0.94, 0.88, 0.82, 0.76]

  let finalBuf = null
  for (let i = 0; i < shrinkSteps.length; i++) {
    const theme = adjustStyling(baseTheme, resume, shrinkSteps[i])
    const styles = createDynamicStyles(theme)
    const el = h(ResumeDocument, { resume, styles, theme })
    let buf
    try {
      buf = await renderToBuffer(el)
    } catch (e) {
      fail(`render failed: ${e.message}`)
    }
    finalBuf = buf
    if (countPages(buf) <= 1) break
  }

  try {
    writeFileSync(outPath, finalBuf)
  } catch (e) {
    fail(`cannot write output: ${e.message}`)
  }
}

main().catch((e) => fail(e?.stack || String(e)))
