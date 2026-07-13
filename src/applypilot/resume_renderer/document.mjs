import React from 'react'
import { Document, Page, Text, View } from '@react-pdf/renderer'

const h = React.createElement

function bulletChar(theme) {
  return theme?.bullets?.style || '•'
}

function isLink(s) {
  return s.includes('@') || s.includes('.com') || s.includes('.io') || s.includes('.dev')
    || s.includes('.net') || s.includes('.org') || s.includes('/')
}

/**
 * Contact line: phone – email – links, centered, with links in blue underline.
 * Matches the reference resume's "(phone) – email – linkedin – github" style.
 * Exported so the cover letter can share the exact same header.
 */
export function ContactLine(styles, contact) {
  // Matches the reference: phone – email – links (location is not shown here).
  const parts = []
  if (contact.phone) parts.push(String(contact.phone))
  if (contact.email) parts.push(String(contact.email))
  for (const link of contact.links || []) if (link) parts.push(String(link))

  const children = []
  parts.forEach((p, i) => {
    if (i > 0) children.push(h(Text, { key: `s${i}`, style: styles.contactSep }, '–'))
    const style = isLink(p) ? styles.contactLink : styles.contactItem
    children.push(h(Text, { key: `p${i}`, style }, p))
  })
  return h(View, { key: 'contact', style: styles.contactInfo }, children)
}

/** A single experience/project entry: bold header + right date, subtitle, bullets. */
function Entry(styles, theme, item, key) {
  if (!item) return null
  const bullets = Array.isArray(item.bullets) ? item.bullets : []
  const marker = bulletChar(theme)
  return h(View, { key, style: styles.entry, wrap: false }, [
    h(View, { key: 'hd', style: styles.entryHeader }, [
      item.header ? h(Text, { key: 'c', style: styles.companyName }, String(item.header)) : null,
      item.date ? h(Text, { key: 'd', style: styles.date }, String(item.date)) : null,
    ]),
    item.subtitle ? h(Text, { key: 'st', style: styles.subtitle }, String(item.subtitle)) : null,
    bullets.length
      ? h(View, { key: 'bl', style: styles.bulletsContainer },
          bullets.filter(Boolean).map((b, i) =>
            h(View, { key: i, style: styles.bulletRow }, [
              h(Text, { key: 'm', style: styles.bulletMarker }, marker),
              h(Text, { key: 't', style: styles.bulletText }, String(b)),
            ])))
      : null,
  ])
}

function Section(styles, title, children, key) {
  return h(View, { key, style: styles.section }, [
    h(Text, { key: 't', style: styles.sectionTitle }, title),
    ...children,
  ])
}

/**
 * @param {object} props.resume normalized resume (see RenderRequest schema)
 * @param {object} props.styles built StyleSheet (createDynamicStyles output)
 * @param {object} props.theme  the stylingSpecs used to build styles
 */
export function ResumeDocument({ resume, styles, theme }) {
  const c = resume.contactInfo || {}
  const skills = resume.skills || []
  const experience = resume.experience || []
  const projects = resume.projects || []
  const education = resume.education || []
  const marker = bulletChar(theme)

  const sections = []

  if (resume.summary) {
    sections.push(Section(styles, 'Professional Summary', [
      h(Text, { key: 's', style: styles.summary }, String(resume.summary)),
    ], 'summary'))
  }

  // Skills as bold-category bullets (matches the reference "KEY STRENGTHS").
  const skillRows = skills.filter((k) => k && k.value && String(k.value).trim())
  if (skillRows.length) {
    sections.push(Section(styles, 'Technical Skills',
      skillRows.map((k, i) =>
        h(View, { key: i, style: styles.bulletRow }, [
          h(Text, { key: 'm', style: styles.bulletMarker }, marker),
          h(Text, { key: 't', style: styles.bulletText }, [
            k.category ? h(Text, { key: 'c', style: styles.skillCat }, `${k.category}: `) : null,
            h(Text, { key: 'v' }, String(k.value || '')),
          ]),
        ])), 'skills'))
  }

  if (experience.length) {
    sections.push(Section(styles, 'Work Experience',
      experience.map((e, i) => Entry(styles, theme, e, i)), 'experience'))
  }

  if (projects.length) {
    sections.push(Section(styles, 'Projects',
      projects.map((e, i) => Entry(styles, theme, e, i)), 'projects'))
  }

  if (education.length) {
    sections.push(Section(styles, 'Education',
      education.filter(Boolean).map((ed, i) => {
        const label = [ed.school, ed.degree, ed.detail].filter(Boolean).join(' — ')
        return h(View, { key: i, style: styles.eduRow }, [
          h(Text, { key: 't', style: styles.eduText }, label),
          ed.date ? h(Text, { key: 'd', style: styles.eduDate }, String(ed.date)) : null,
        ])
      }), 'education'))
  }

  const header = h(View, { key: 'header', style: styles.header }, [
    h(Text, { key: 'n', style: styles.name }, c.name || 'Your Name'),
    ContactLine(styles, c),
  ])

  return h(Document, null,
    h(Page, { size: 'LETTER', style: styles.page }, [header, ...sections]))
}
