/**
 * One-page fitting. Ported from the Resume Formatting Tool's onePageHandler
 * (adjustStylingMinimally) and adapted:
 *   - includes projects in the density estimate
 *   - returns a pure, deep-copied theme (no mutation, no randomness/time)
 *   - accepts an `extraShrink` multiplier so render.mjs can iterate down to 1 page
 */

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
  const exp = resume.experience || []
  const proj = resume.projects || []
  const entries = exp.length + proj.length
  const bullets =
    exp.reduce((s, e) => s + (e.bullets?.length || 0), 0) +
    proj.reduce((s, e) => s + (e.bullets?.length || 0), 0)
  const summaryLen = resume.summary ? String(resume.summary).length : 0
  const skillsLen = (resume.skills || []).reduce(
    (s, k) => s + (k.category?.length || 0) + (k.value?.length || 0), 0)
  const eduCount = (resume.education || []).length

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
