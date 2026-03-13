/**
 * APA 7th Edition Word Document Generator
 *
 * Receives JSON via stdin:
 * {
 *   "title": "...",
 *   "author": "...",
 *   "institution": "...",
 *   "course": "...",
 *   "instructor": "...",
 *   "date": "...",
 *   "paragraphs": ["paragraph 1 text...", "paragraph 2..."],
 *   "references": ["Author, A. (2020). Title...", "..."],
 *   "style": "apa"   // apa | mla | chicago
 * }
 *
 * Outputs: apa_output.docx
 */

const {
  Document, Packer, Paragraph, TextRun, AlignmentType,
  PageNumber, NumberFormat, Header, Footer, Tab,
  TabStopType, TabStopPosition, HeadingLevel, PageBreak,
  UnderlineType
} = require('docx');
const fs = require('fs');

// ── Read stdin ────────────────────────────────
let raw = '';
process.stdin.on('data', d => raw += d);
process.stdin.on('end', () => {
  const data = JSON.parse(raw);
  generateDoc(data);
});

// ── APA spacing constants ─────────────────────
const DOUBLE = 480;       // 24pt line spacing = 480 twips
const FONT   = "Times New Roman";
const SIZE   = 24;        // 12pt = 24 half-points
const INDENT = 720;       // 0.5 inch first-line indent = 720 twips
const MARGIN = 1440;      // 1 inch = 1440 twips

// ── Helper: make a standard APA body paragraph ──
function bodyParagraph(text, firstLineIndent = true) {
  // Split text on in-text citations like (Author, 2021) and keep them inline
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
    indent: firstLineIndent ? { firstLine: INDENT } : {},
    children: [new TextRun({ text, font: FONT, size: SIZE })]
  });
}

// ── Helper: running head + page number header ──
function makeHeader(runningHead, includePageNum = true) {
  const children = [];
  if (runningHead) {
    children.push(
      new TextRun({ text: runningHead.toUpperCase(), font: FONT, size: SIZE, allCaps: false })
    );
  }
  if (includePageNum) {
    // Right-aligned page number via tab stop
    children.push(
      new TextRun({ children: [new Tab()], font: FONT, size: SIZE }),
      new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: SIZE })
    );
  }
  return new Header({
    children: [new Paragraph({
      tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
      children
    })]
  });
}

// ── Helper: reference entry (hanging indent) ──
function refEntry(text) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
    indent: { left: INDENT, hanging: INDENT },
    children: [new TextRun({ text, font: FONT, size: SIZE })]
  });
}

// ── Helper: blank line (APA doesn't add space between paras,
//    but we need one blank between reference entries) ──
function blankLine() {
  return new Paragraph({
    spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
    children: [new TextRun({ text: '', font: FONT, size: SIZE })]
  });
}

// ── Main generator ────────────────────────────
function generateDoc(data) {
  const {
    title       = "Untitled Paper",
    author      = "Author Name",
    institution = "Institution Name",
    course      = "",
    instructor  = "",
    date        = new Date().toLocaleDateString('en-US', { year:'numeric', month:'long', day:'numeric' }),
    paragraphs  = [],
    references  = [],
    style       = "apa",
    output_path = "apa_output.docx"
  } = data;

  // Running head: first 50 chars of title, uppercase
  const runningHead = title.substring(0, 50).toUpperCase();

  // ── Section 1: Title page ─────────────────
  const titlePageChildren = [
    // Vertical spacer (~1/3 down the page) — APA puts title block ~1/3 from top
    ...Array(8).fill(null).map(() => blankLine()),

    // Title — bold, centered, title case
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
      children: [new TextRun({ text: title, font: FONT, size: SIZE, bold: true })]
    }),
    blankLine(),

    // Author
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
      children: [new TextRun({ text: author, font: FONT, size: SIZE })]
    }),

    // Institution
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
      children: [new TextRun({ text: institution, font: FONT, size: SIZE })]
    }),

    // Course (optional)
    ...(course ? [new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
      children: [new TextRun({ text: course, font: FONT, size: SIZE })]
    })] : []),

    // Instructor (optional)
    ...(instructor ? [new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
      children: [new TextRun({ text: instructor, font: FONT, size: SIZE })]
    })] : []),

    // Date
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
      children: [new TextRun({ text: date, font: FONT, size: SIZE })]
    }),
  ];

  // ── Section 2: Body ───────────────────────
  const bodyChildren = [
    // Repeat title at top of body (APA 7 requirement)
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
      children: [new TextRun({ text: title, font: FONT, size: SIZE, bold: true })]
    }),
    blankLine(),

    // Body paragraphs
    ...paragraphs.map(p => bodyParagraph(p.trim())),
  ];

  // ── Section 3: References page ────────────
  const uniqueRefs = [...new Set(references.filter(Boolean))];
  const refChildren = [
    // "References" heading — centered, bold
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: DOUBLE, lineRule: 'auto', before: 0, after: 0 },
      children: [new TextRun({ text: "References", font: FONT, size: SIZE, bold: true })]
    }),
    blankLine(),

    // Each reference entry with hanging indent
    ...uniqueRefs.map(ref => refEntry(ref)),
  ];

  // ── Assemble document ─────────────────────
  const doc = new Document({
    sections: [
      // ── Title page (page 1, no header needed per APA 7 student papers) ──
      {
        properties: {
          page: {
            size:   { width: 12240, height: 15840 },
            margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
            pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL }
          }
        },
        headers: { default: makeHeader(runningHead, true) },
        children: titlePageChildren
      },

      // ── Body (starts on page 2) ──
      {
        properties: {
          page: {
            size:   { width: 12240, height: 15840 },
            margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
          }
        },
        headers: { default: makeHeader(runningHead, true) },
        children: bodyChildren
      },

      // ── References (new page) ──
      {
        properties: {
          page: {
            size:   { width: 12240, height: 15840 },
            margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
          }
        },
        headers: { default: makeHeader(runningHead, true) },
        children: refChildren
      }
    ]
  });

  Packer.toBuffer(doc).then(buffer => {
    fs.writeFileSync(output_path, buffer);
    console.log(`✅ Word document saved: ${output_path}`);
  }).catch(err => {
    console.error('❌ Failed to generate docx:', err);
    process.exit(1);
  });
}