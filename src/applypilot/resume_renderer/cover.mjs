import React from 'react'
import { Document, Page, Text, View, StyleSheet } from '@react-pdf/renderer'

const h = React.createElement

/**
 * Classic business-letter styling. Uses React-PDF's built-in Times family
 * (no external fonts, works offline). Ported from the Resume Formatting Tool's
 * CoverLetterDocument.
 */
export function createCoverStyles(styling = {}) {
  const fs = styling.fontSize || 11
  return StyleSheet.create({
    page: {
      flexDirection: 'column',
      backgroundColor: '#ffffff',
      paddingTop: styling.margins?.top || 54,
      paddingBottom: styling.margins?.bottom || 54,
      paddingLeft: styling.margins?.left || 60,
      paddingRight: styling.margins?.right || 60,
      fontFamily: 'Times-Roman',
      fontSize: fs,
      lineHeight: styling.lineHeight || 1.5,
      color: '#1a1a1a',
    },
    header: { marginBottom: 18, borderBottom: '1pt solid #1a3a5c', paddingBottom: 8 },
    name: { fontSize: fs + 6, fontFamily: 'Times-Bold', color: '#1a3a5c', marginBottom: 3 },
    contact: { fontSize: fs - 1.5, color: '#555' },
    date: { marginBottom: 16, marginTop: 4, fontSize: fs },
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
 * @param {object} props.cover  { candidate:{name,contact}, date, body }
 * @param {object} props.styles createCoverStyles() output
 */
export function CoverLetterDocument({ cover, styles }) {
  const candidate = cover.candidate || {}
  const paras = paragraphs(cover.body)

  // ApplyPilot letters are full prose: first paragraph is the salutation,
  // last is the sign-off ("Sincerely,\nName"). Style them like a letter;
  // if the structure is unexpected, everything renders as body paragraphs.
  const hasStructure = paras.length >= 2
  const salutation = hasStructure ? paras[0] : null
  const signoff = hasStructure ? paras[paras.length - 1] : null
  const bodyParas = hasStructure ? paras.slice(1, -1) : paras

  const signLines = signoff ? signoff.split('\n').map((l) => l.trim()).filter(Boolean) : []

  const children = []
  children.push(h(View, { key: 'hd', style: styles.header }, [
    h(Text, { key: 'n', style: styles.name }, candidate.name || 'Candidate Name'),
    candidate.contact ? h(Text, { key: 'c', style: styles.contact }, String(candidate.contact)) : null,
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
