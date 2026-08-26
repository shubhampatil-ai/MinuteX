// lib/mom-docx.ts — the structured MoM as a real .docx.
//
// Same reasoning as lib/docx-export.ts, which this deliberately sits beside
// rather than inside: that module renders the Markdown subset the seven other
// AI document types emit, and it stays exactly right for them. A structured
// MoM needs things Markdown cannot express — per-column widths, a coloured
// header row, zebra shading, a header row that repeats across a page break,
// and a real running footer — so it is built from the STRUCTURE instead.
//
// Both files hand-write OOXML and zip it with fflate for the same reason: a
// .docx is a ZIP of XML parts, and there is no honest way to produce one by
// string-concatenating a single file. Neither pulls in a Word library.
//
// Every measurement comes from lib/mom-template.ts, the same constants
// lib/mom-html.ts renders the PDF and preview from — which is what makes a
// DOCX and a PDF of the same MoM actually look like the same document.
import { zipSync, strToU8 } from "fflate";
import type { Mom, MomSection } from "./api";
import { exportFilename } from "./export-doc";
import type { ExportResult } from "./export-doc";
import { shareFileBytes } from "./file-share";
import { isSectionEmpty } from "./mom-model";
import {
  CONTENT_WIDTH_TWIPS, MOM_TEMPLATE as T, columnWeights, escapeMarkup,
  halfPt, templateDate, twips,
} from "./mom-template";

// ---------------------------------------------------------------------------
// Runs and paragraphs
// ---------------------------------------------------------------------------
type RunOpts = {
  bold?: boolean; color?: string; size?: number; caps?: boolean;
  spacing?: number;
};

function run(text: string, o: RunOpts = {}): string {
  const props = [
    o.bold ? "<w:b/>" : "",
    o.caps ? "<w:caps/>" : "",
    o.color ? `<w:color w:val="${o.color}"/>` : "",
    o.size ? `<w:sz w:val="${halfPt(o.size)}"/>` : "",
    o.spacing ? `<w:spacing w:val="${o.spacing}"/>` : "",
  ].join("");
  // Word collapses runs of spaces without xml:space="preserve".
  return `<w:r>${props ? `<w:rPr>${props}</w:rPr>` : ""}`
    + `<w:t xml:space="preserve">${escapeMarkup(text)}</w:t></w:r>`;
}

type ParaOpts = {
  after?: number; before?: number; border?: string; borderSize?: number;
  keepNext?: boolean; indent?: number;
};

function para(runs: string, o: ParaOpts = {}): string {
  const props = [
    o.keepNext ? "<w:keepNext/>" : "",
    o.indent ? `<w:ind w:left="${o.indent}"/>` : "",
    (o.after != null || o.before != null)
      ? `<w:spacing${o.before != null ? ` w:before="${o.before}"` : ""}`
        + `${o.after != null ? ` w:after="${o.after}"` : ""}/>`
      : "",
    o.border
      ? `<w:pBdr><w:bottom w:val="single" w:sz="${o.borderSize ?? 8}" `
        + `w:color="${o.border}"/></w:pBdr>`
      : "",
  ].join("");
  return `<w:p>${props ? `<w:pPr>${props}</w:pPr>` : ""}${runs}</w:p>`;
}

/** Author line breaks inside one field/cell become real paragraph breaks. */
function multiline(text: string, o: RunOpts & ParaOpts = {}): string {
  const lines = String(text ?? "").split("\n");
  return lines.map((line, i) => para(run(line, o), {
    ...o, after: i === lines.length - 1 ? (o.after ?? 0) : 0,
  })).join("");
}

// ---------------------------------------------------------------------------
// Tables
// ---------------------------------------------------------------------------
function cell(content: string, widthTwips: number, shade?: string): string {
  return `<w:tc><w:tcPr><w:tcW w:w="${widthTwips}" w:type="dxa"/>`
    + (shade ? `<w:shd w:val="clear" w:fill="${shade}"/>` : "")
    + `<w:tcMar><w:top w:w="60" w:type="dxa"/><w:bottom w:w="60" w:type="dxa"/>`
    + `<w:left w:w="90" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tcMar>`
    + `</w:tcPr>${content}</w:tc>`;
}

function gridTable(section: MomSection): string {
  const columns = section.columns ?? [];
  const rows = (section.rows ?? []).filter((r) => r.visible);
  if (!columns.length || !rows.length) return "";

  const widths = columnWeights(columns.map((c) => c.label))
    .map((w) => Math.max(400, Math.round(w * CONTENT_WIDTH_TWIPS)));

  const border = (side: string) =>
    `<w:${side} w:val="single" w:sz="4" w:color="${T.rule}"/>`;

  // tblHeader on the header row is what makes Word repeat it when the table
  // spans a page — without it page 2 of a long Action Items table is a wall
  // of unlabelled cells.
  const header = `<w:tr><w:trPr><w:tblHeader/></w:trPr>`
    + columns.map((c, i) => cell(
      para(run(c.label, { bold: true, color: "FFFFFF", size: T.kickerPt, caps: true, spacing: 8 }),
        { after: 0 }),
      widths[i], T.accent)).join("")
    + `</w:tr>`;

  const body = rows.map((r, ri) => `<w:tr>`
    + columns.map((c, ci) => cell(
      multiline(r.cells[c.id] ?? "", { size: T.tablePt }),
      widths[ci], ri % 2 === 1 ? T.zebra : undefined)).join("")
    + `</w:tr>`).join("");

  return `<w:tbl><w:tblPr><w:tblW w:w="${CONTENT_WIDTH_TWIPS}" w:type="dxa"/>`
    + `<w:tblLayout w:type="fixed"/><w:tblBorders>`
    + ["top", "left", "bottom", "right", "insideH", "insideV"].map(border).join("")
    + `</w:tblBorders></w:tblPr>`
    + `<w:tblGrid>${widths.map((w) => `<w:gridCol w:w="${w}"/>`).join("")}</w:tblGrid>`
    + header + body + `</w:tbl>`;
}

/** Meeting Details renders as label/value pairs — a borderless two-column
 * table, so the values align in a column the way the reference design shows,
 * which tab stops cannot guarantee across Word versions. */
function fieldsTable(section: MomSection): string {
  const fields = (section.fields ?? []).filter((f) => f.visible && (f.label || f.value));
  if (!fields.length) return "";
  const labelWidth = Math.round(CONTENT_WIDTH_TWIPS * 0.33);
  const valueWidth = CONTENT_WIDTH_TWIPS - labelWidth;
  const rows = fields.map((f) => `<w:tr>`
    + cell(para(run(f.label, { bold: true, color: T.inkDim, size: T.bodyPt }), { after: 40 }),
      labelWidth)
    + cell(multiline(f.value, { size: T.bodyPt, after: 40 }), valueWidth)
    + `</w:tr>`).join("");
  return `<w:tbl><w:tblPr><w:tblW w:w="${CONTENT_WIDTH_TWIPS}" w:type="dxa"/>`
    + `<w:tblLayout w:type="fixed"/>`
    + `<w:tblBorders><w:top w:val="none"/><w:left w:val="none"/>`
    + `<w:bottom w:val="none"/><w:right w:val="none"/>`
    + `<w:insideH w:val="none"/><w:insideV w:val="none"/></w:tblBorders></w:tblPr>`
    + `<w:tblGrid><w:gridCol w:w="${labelWidth}"/><w:gridCol w:w="${valueWidth}"/></w:tblGrid>`
    + rows + `</w:tbl>`;
}

function listParagraphs(section: MomSection): string {
  return (section.items ?? [])
    .filter((i) => i.visible && i.text.trim())
    .map((i) => `<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/>`
      + `</w:numPr><w:spacing w:after="40"/></w:pPr>`
      + run(i.text, { size: T.bodyPt }) + `</w:p>`)
    .join("");
}

function sectionXml(section: MomSection): string {
  let body = "";
  if (section.kind === "fields") body = fieldsTable(section);
  else if (section.kind === "table") body = gridTable(section);
  else if (section.kind === "text") {
    body = (section.text ?? "").split(/\n{2,}/).map((b) => b.trim()).filter(Boolean)
      .map((b) => multiline(b, { size: T.bodyPt, after: 120 })).join("");
  } else body = listParagraphs(section);

  if (!body) return "";
  // keepNext so a heading is never orphaned at the foot of a page.
  const heading = para(
    run(section.title, {
      bold: true, color: T.accent, size: T.headingPt, caps: true, spacing: 12,
    }),
    { before: 220, after: 90, keepNext: true });
  return heading + body + para("", { after: 60 });
}

// ---------------------------------------------------------------------------
// Package parts
// ---------------------------------------------------------------------------
const CONTENT_TYPES_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`
  + `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">`
  + `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>`
  + `<Default Extension="xml" ContentType="application/xml"/>`
  + `<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>`
  + `<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>`
  + `<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>`
  + `<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>`
  + `</Types>`;

const RELS_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`
  + `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
  + `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>`
  + `</Relationships>`;

const DOCUMENT_RELS_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`
  + `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
  + `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>`
  + `<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>`
  + `<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>`
  + `</Relationships>`;

const NUMBERING_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`
  + `<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">`
  + `<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/>`
  + `<w:lvlText w:val="&#8226;"/><w:pPr><w:ind w:left="340" w:hanging="240"/></w:pPr>`
  + `</w:lvl></w:abstractNum>`
  + `<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>`
  + `</w:numbering>`;

const STYLES_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`
  + `<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">`
  + `<w:docDefaults><w:rPrDefault><w:rPr>`
  + `<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>`
  + `<w:sz w:val="${halfPt(T.bodyPt)}"/><w:color w:val="${T.ink}"/>`
  + `</w:rPr></w:rPrDefault>`
  + `<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/>`
  + `</w:pPr></w:pPrDefault></w:docDefaults>`
  + `<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>`
  + `</w:styles>`;

const FOOTER_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`
  + `<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">`
  + `<w:p><w:pPr><w:pBdr><w:top w:val="single" w:sz="4" w:color="${T.rule}"/></w:pBdr>`
  + `<w:spacing w:before="60"/><w:tabs><w:tab w:val="right" w:pos="${CONTENT_WIDTH_TWIPS}"/></w:tabs></w:pPr>`
  + run(T.footerNote, { size: T.footerPt, color: T.inkFaint })
  + `<w:r><w:tab/></w:r>`
  // A real PAGE field, so the page number is Word's, not an estimate of ours.
  + `<w:r><w:rPr><w:sz w:val="${halfPt(T.footerPt)}"/><w:color w:val="${T.inkFaint}"/></w:rPr>`
  + `<w:fldChar w:fldCharType="begin"/></w:r>`
  + `<w:r><w:rPr><w:sz w:val="${halfPt(T.footerPt)}"/><w:color w:val="${T.inkFaint}"/></w:rPr>`
  + `<w:instrText xml:space="preserve">PAGE</w:instrText></w:r>`
  + `<w:r><w:rPr><w:sz w:val="${halfPt(T.footerPt)}"/><w:color w:val="${T.inkFaint}"/></w:rPr>`
  + `<w:fldChar w:fldCharType="end"/></w:r>`
  + `</w:p></w:ftr>`;

function documentXml(mom: Mom, meetingTitle: string): string {
  const title = mom.title?.trim() || "Minutes of Meeting";
  const subtitle = mom.subtitle?.trim() || meetingTitle || "";

  const masthead =
    para(run(T.company, { color: T.accent, size: T.kickerPt, bold: true, caps: true, spacing: 26 }),
      { after: 40 })
    + para(run(title, { bold: true, size: T.titlePt }), { after: subtitle ? 20 : 60 })
    + (subtitle
      ? para(run(subtitle, { size: T.subtitlePt, color: T.inkDim, bold: true }),
        { after: 60, border: T.accent, borderSize: 18 })
      : para("", { after: 0, border: T.accent, borderSize: 18 }))
    + para(run(templateDate(), { size: T.kickerPt, color: T.inkFaint }),
      { before: 80, after: 220 });

  const sections = mom.sections
    .filter((s) => s.visible && !isSectionEmpty(s))
    .map(sectionXml)
    .join("");

  const body = sections
    || para(run("This MoM has no visible content yet.",
      { size: T.bodyPt, color: T.inkFaint }));

  const sectPr = `<w:sectPr>`
    + `<w:footerReference w:type="default" r:id="rId3"/>`
    + `<w:pgSz w:w="${twips(T.pageWidthIn)}" w:h="${twips(T.pageHeightIn)}"/>`
    + `<w:pgMar w:top="${twips(T.marginIn)}" w:right="${twips(T.marginIn)}" `
    + `w:bottom="${twips(T.marginIn)}" w:left="${twips(T.marginIn)}" `
    + `w:footer="${twips(0.45)}"/>`
    + `</w:sectPr>`;

  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`
    + `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" `
    + `xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">`
    + `<w:body>${masthead}${body}${sectPr}</w:body></w:document>`;
}

/** Build the MoM as real OOXML .docx bytes. */
export function buildMomDocx(mom: Mom, meetingTitle: string): Uint8Array {
  return zipSync({
    "[Content_Types].xml": strToU8(CONTENT_TYPES_XML),
    "_rels/.rels": strToU8(RELS_XML),
    "word/document.xml": strToU8(documentXml(mom, meetingTitle)),
    "word/_rels/document.xml.rels": strToU8(DOCUMENT_RELS_XML),
    "word/numbering.xml": strToU8(NUMBERING_XML),
    "word/styles.xml": strToU8(STYLES_XML),
    "word/footer1.xml": strToU8(FOOTER_XML),
  }, { level: 6 });
}

/** Render the MoM to .docx and hand it to the OS share sheet. */
export async function exportMomDocx(
  mom: Mom, meetingTitle: string
): Promise<ExportResult> {
  try {
    const bytes = buildMomDocx(mom, meetingTitle);
    const filename = exportFilename(
      mom.title || "Minutes of Meeting", meetingTitle, "docx");
    return await shareFileBytes(
      bytes, filename,
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
  } catch {
    return "failed";
  }
}
