import React from 'react'
import { Document, Page, Text, View, StyleSheet } from '@react-pdf/renderer'
import { DEFAULT_THEME, createDynamicStyles } from './styles.mjs'
import { ContactLine } from './document.mjs'

const h = React.createElement

/**
 * Letter-body styling. The HEADER (name + contact) is shared with the résumé
 * via createDynamicStyles(DEFAULT_THEME) + ContactLine, so the two documents
 * are visually identical up top. Times New Roman throughout, no rules.
 */
export function createCoverStyles(styling = {}) {
  const fs = styling.fontSize || DEFAULT_THEME.fonts.body.size
  return StyleSheet.create({
    page: {
      flexDirection: 'column',
      backgroundColor: '#ffffff',
      paddingTop: styling.margins?.top || DEFAULT_THEME.layout.margins.top,
      paddingBottom: styling.margins?.bottom || DEFAULT_THEME.layout.margins.bottom,
      paddingLeft: styling.margins?.left || DEFAULT_THEME.layout.margins.left,
      paddingRight: styling.margins?.right || DEFAULT_THEME.layout.margins.right,
      fontFamily: 'Times-Roman',
      fontSize: fs,
      lineHeight: styling.lineHeight || 1.4,
      color: '#000000',
    },
    date: { marginTop: 16, marginBottom: 14, fontSize: fs },
    salutation: { marginBottom: 10, fontSize: fs },
    bodyPara: { marginBottom: 10, textAlign: 'justify', textIndent: 18, fontSize: fs },
    signOff: { marginTop: 14, fontSize: fs },
    signName: { fontFamily: 'Times-Bold', fontSize: fs, marginTop: 2 },
  })
}

function paragraphs(text) {
  return String(text || '').split('\n\n').map((p) => p.trim()).filter(Boolean)
}

/**
 * @param {object} props.cover { candidate: {name, email, phone, links, ...}, date, body }
 */
export function CoverLetterDocument({ cover }) {
  const head = createDynamicStyles(DEFAULT_THEME)  // shared résumé header styling
  const styles = createCoverStyles(cover.styling || {})
  const candidate = cover.candidate || {}
  const paras = paragraphs(cover.body)

  // ApplyPilot letters are full prose: first paragraph is the salutation,
  // last is the sign-off ("Sincerely,\nName"). Style them like a letter; if the
  // structure is unexpected, everything renders as body paragraphs.
  const hasStructure = paras.length >= 2
  const salutation = hasStructure ? paras[0] : null
  const signoff = hasStructure ? paras[paras.length - 1] : null
  const bodyParas = hasStructure ? paras.slice(1, -1) : paras
  const signLines = signoff ? signoff.split('\n').map((l) => l.trim()).filter(Boolean) : []

  const children = []

  // Shared header — identical to the résumé (centered black name, blue links, no rule).
  children.push(h(View, { key: 'hd', style: head.header }, [
    h(Text, { key: 'n', style: head.name }, candidate.name || 'Your Name'),
    ContactLine(head, candidate),
  ]))

  if (cover.date) children.push(h(Text, { key: 'dt', style: styles.date }, String(cover.date)))
  if (salutation) children.push(h(Text, { key: 'sal', style: styles.salutation }, salutation))

  children.push(h(View, { key: 'body' },
    bodyParas.map((p, i) => h(Text, { key: i, style: styles.bodyPara }, p))))

  if (signLines.length) {
    children.push(h(View, { key: 'sign', style: styles.signOff },
      signLines.map((line, i) =>
        h(Text, { key: i, style: i === signLines.length - 1 ? styles.signName : { fontSize: styles.date.fontSize } }, line))))
  }

  return h(Document, null, h(Page, { size: 'LETTER', style: styles.page }, children))
}
