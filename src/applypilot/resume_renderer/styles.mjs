import { StyleSheet } from '@react-pdf/renderer'

/**
 * Map an arbitrary font name to a React-PDF built-in font family base.
 * The reference resume uses Times New Roman; React-PDF's built-in Times is
 * metric-compatible, so no font registration is needed.
 */
export function mapFontFamily(fontName) {
  if (!fontName) return 'Times-Roman'
  const f = String(fontName).toLowerCase()
  if (f.includes('helvetica') || f.includes('arial') || f.includes('sans')) return 'Helvetica'
  if (f.includes('courier') || f.includes('mono')) return 'Courier'
  return 'Times-Roman'
}

/**
 * Default theme — reproduces the user's reference resume
 * ("Technical SEO Manager") look:
 *   - Times New Roman throughout (serif)
 *   - large bold centered name; contact line with " – " separators, links in blue
 *   - NO rules/borders anywhere
 *   - section headers: bold, UPPERCASE, left-aligned, body-size
 *   - company/role bold; dates bold-italic, right-aligned
 *   - justified body; filled round bullets with a hanging indent; 1" side margins
 *
 * In the reference every element except the name is the same point size —
 * hierarchy comes from weight/caps/italics, not size.
 */
export const DEFAULT_THEME = {
  fonts: {
    name: { size: 19, family: 'Times-Roman' },
    contact: { size: 9 },
    sectionTitle: { size: 9.5, letterSpacing: 0.4 },
    body: { size: 9, lineHeight: 1.3 },
    companyName: { size: 9.5 },
    roleTitle: { size: 9 },
    date: { size: 9 },
    bulletText: { size: 9 },
    skills: { size: 9 },
    education: { size: 9 },
  },
  layout: {
    margins: { top: 80, bottom: 42, left: 72, right: 72 },
    sectionSpacing: 10,
    paragraphSpacing: 8,
  },
  bullets: { style: '•', indentation: 18, lineSpacing: 1.28 },
  transforms: { name: 'none', sectionTitle: 'uppercase' },
  colors: {
    text: '#000000',
    muted: '#000000',   // reference keeps dates/subtitles black (bold/italic, not grey)
    link: '#1155cc',    // blue hyperlinks
    background: '#ffffff',
  },
}

const SERIF = 'Times-Roman'
const SERIF_BOLD = 'Times-Bold'
const SERIF_ITALIC = 'Times-Italic'
const SERIF_BOLDITALIC = 'Times-BoldItalic'

/**
 * Build a React-PDF StyleSheet from a theme (stylingSpecs). No rules/borders —
 * hierarchy is carried by weight, caps and italics, matching the reference.
 */
export function createDynamicStyles(specs) {
  const fonts = specs?.fonts || {}
  const layout = specs?.layout || {}
  const bullets = specs?.bullets || {}
  const transforms = specs?.transforms || {}
  const colors = specs?.colors || {}

  const text = colors.text || '#000000'
  const link = colors.link || '#1155cc'
  const base = mapFontFamily(fonts.body?.family)
  const bold = base === 'Times-Roman' ? SERIF_BOLD : 'Helvetica-Bold'
  const italic = base === 'Times-Roman' ? SERIF_ITALIC : 'Helvetica-Oblique'
  const boldItalic = base === 'Times-Roman' ? SERIF_BOLDITALIC : 'Helvetica-BoldOblique'

  const tt = (key, fallback) =>
    transforms[key] === 'uppercase' ? 'uppercase'
      : transforms[key] === 'lowercase' ? 'lowercase'
        : transforms[key] === 'capitalize' ? 'capitalize'
          : fallback

  return StyleSheet.create({
    page: {
      paddingTop: layout.margins?.top ?? 52,
      paddingBottom: layout.margins?.bottom ?? 42,
      paddingLeft: layout.margins?.left ?? 72,
      paddingRight: layout.margins?.right ?? 72,
      fontSize: fonts.body?.size ?? 10.5,
      fontFamily: base,
      color: text,
      backgroundColor: colors.background || '#ffffff',
    },

    header: { alignItems: 'center', marginBottom: (layout.sectionSpacing ?? 10) },
    name: {
      fontSize: fonts.name?.size ?? 22,
      fontFamily: bold,
      color: text,
      marginBottom: 8,
      textTransform: tt('name', 'none'),
    },
    contactInfo: {
      flexDirection: 'row',
      flexWrap: 'wrap',
      justifyContent: 'center',
      fontSize: fonts.contact?.size ?? 10.5,
      color: text,
    },
    contactItem: {},
    contactLink: { color: link, textDecoration: 'underline' },
    contactSep: { color: text, marginHorizontal: 5 },

    section: { marginTop: layout.sectionSpacing ?? 12 },
    sectionTitle: {
      fontSize: fonts.sectionTitle?.size ?? 11,
      fontFamily: bold,
      color: text,
      textTransform: tt('sectionTitle', 'uppercase'),
      letterSpacing: fonts.sectionTitle?.letterSpacing ?? 0.4,
      marginBottom: 2,
    },

    summary: {
      fontSize: fonts.body?.size ?? 10.5,
      lineHeight: fonts.body?.lineHeight ?? 1.32,
      color: text,
      textAlign: 'justify',
    },

    entry: { marginBottom: layout.paragraphSpacing ?? 8 },
    entryHeader: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'flex-start',
    },
    companyName: {
      flex: 1,
      fontSize: fonts.companyName?.size ?? 11,
      fontFamily: bold,
      color: text,
    },
    subtitle: {
      fontSize: fonts.roleTitle?.size ?? 10.5,
      fontFamily: italic,
      color: text,
      marginTop: 1,
    },
    date: {
      fontSize: fonts.date?.size ?? 10.5,
      fontFamily: boldItalic,
      color: text,
      marginLeft: 8,
    },
    bulletsContainer: { marginTop: 3, paddingLeft: bullets.indentation ?? 18 },
    bulletRow: { flexDirection: 'row', marginBottom: 2 },
    bulletMarker: { width: 12, color: text },
    bulletText: {
      flex: 1,
      fontSize: fonts.bulletText?.size ?? 10.5,
      lineHeight: bullets.lineSpacing ?? 1.28,
      color: text,
      textAlign: 'justify',
    },
    skillCat: { fontFamily: bold, color: text },

    eduRow: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'flex-start',
      marginBottom: 2,
    },
    eduText: { flex: 1, fontSize: fonts.education?.size ?? 10.5, color: text },
    eduDate: { fontSize: fonts.education?.size ?? 10.5, fontFamily: boldItalic, color: text },
  })
}
