/**
 * One-page fitting. Ported from the Resume Formatting Tool's onePageHandler
 * (adjustStylingMinimally) and adapted:
 *   - includes projects in the density estimate
 *   - returns a pure, deep-copied theme (no mutation, no randomness/time)
 *   - accepts an `extraShrink` multiplier so render.mjs can iterate down to 1 page
 */

/**
 * Collapse a sections-shaped résumé into the flat keys the density estimate understands.
 * Read-only: used for MEASUREMENT, never for rendering.
 */
export function flattenSections(resume) {
  const out = { experience: [], projects: [], skills: [], education: [], summary: '' }
  for (const sec of resume.sections || []) {
    if (sec.kind === 'summary') out.summary += String(sec.text || '')
    else if (sec.kind === 'experience') out.experience.push(...(sec.entries || []))
    else if (sec.kind === 'education') out.education.push(...(sec.education || []))
    else out.skills.push(...(sec.skills || []))
  }
  return out
}

const MIN_FONT = 8
const MAX_FONT = 28  // headroom for the 24pt name; body/section fonts never approach it

function clampFont(v, scale) {
  return Math.max(MIN_FONT, Math.min(MAX_FONT, Math.round(v * scale)))
}

/**
 * Rough content-volume score. Higher = more content = shrink harder.
 */
export function densityScore(resume) {
  if (!resume) return 0
  // Structure-preserving résumés carry their content under `sections`; reading the old flat
  // keys scored them 0, which picked the EXPAND scale for a dense document.
  const r = resume.sections ? flattenSections(resume) : resume
  const exp = r.experience || []
  const proj = r.projects || []
  const entries = exp.length + proj.length
  const bullets =
    exp.reduce((s, e) => s + (e.bullets?.length || 0), 0) +
    proj.reduce((s, e) => s + (e.bullets?.length || 0), 0)
  const summaryLen = r.summary ? String(r.summary).length : 0
  const skillsLen = (r.skills || []).reduce(
    (s, k) => s + (k.category?.length || 0) + (k.value?.length || 0), 0)
  const eduCount = (r.education || []).length

  return entries * 90 + bullets * 30 + summaryLen / 10 + skillsLen / 18 + eduCount * 40
}

/**
 * Initial scale from density. Only *expand* sparse resumes to fill the page;
 * for normal/dense resumes we render at the base size and let the refit loop
 * shrink ONLY if the content actually overflows one page. (Pre-shrinking dense
 * resumes made everything needlessly small.)
 */
export function baseScale(resume) {
  const score = densityScore(resume)
  if (score < 360) return 1.12
  if (score < 540) return 1.05
  return 1.0
}

/**
 * Return a new theme with font sizes and spacing scaled to fit one page.
 * @param {object} theme       base stylingSpecs
 * @param {object} resume      normalized resume (for density)
 * @param {number} extraShrink additional multiplier from the refit loop (<=1)
 */
export function adjustStyling(theme, resume, extraShrink = 1) {
  const t = JSON.parse(JSON.stringify(theme))
  const scale = baseScale(resume) * extraShrink
  const shrinking = scale < 1

  if (t.fonts) {
    for (const key of Object.keys(t.fonts)) {
      if (typeof t.fonts[key]?.size === 'number') {
        t.fonts[key].size = clampFont(t.fonts[key].size, scale)
      }
    }
    // Keep the theme's line height (design intent); nudge tighter only when
    // shrinking to reclaim vertical space.
    if (t.fonts.body?.lineHeight) {
      t.fonts.body.lineHeight = shrinking
        ? Math.max(1.15, t.fonts.body.lineHeight * 0.95)
        : t.fonts.body.lineHeight
    }
  }

  // Scale spacing FROM the theme's base values (never hardcode) so the tuned
  // look is preserved. Left/right margins are design intent — keep them fixed;
  // only top/bottom flex modestly under heavy shrink.
  const base = theme.layout || {}
  const bm = base.margins || {}
  const vScale = shrinking ? Math.max(scale, 0.86) : 1
  t.layout = {
    sectionSpacing: Math.max(5, Math.round((base.sectionSpacing ?? 10) * scale)),
    paragraphSpacing: Math.max(3, Math.round((base.paragraphSpacing ?? 8) * scale)),
    margins: {
      top: Math.max(40, Math.round((bm.top ?? 72) * vScale)),
      bottom: Math.max(28, Math.round((bm.bottom ?? 42) * vScale)),
      left: bm.left ?? 72,
      right: bm.right ?? 72,
    },
  }

  // Bullet spacing/indent scale from theme base too.
  const bb = theme.bullets || {}
  t.bullets = {
    ...t.bullets,
    style: bb.style ?? '•',
    lineSpacing: bb.lineSpacing ?? (shrinking ? 1.2 : 1.3),
    indentation: bb.indentation ?? 18,
  }

  return t
}
