// lib/docx-export.ts — export a generated document as a real .docx.
//
// A .docx is a ZIP of XML parts (OOXML/WordprocessingML) — there is no way to
// produce one honestly with string concatenation the way lib/export-doc.ts's
// PDF path builds HTML. `fflate` (pure JS, no native module, ~8KB) does the
// zipping; everything else here is hand-written XML for the small Markdown
// subset the document prompts emit — headings, bold/italic, bullets, numbered
// lists, tables, paragraphs, rules. Same subset as lib/document-renderer.tsx
// and lib/export-doc.ts's markdownToHtml, so all three renderings of a
// document agree with each other.
import { zipSync, strToU8 } from "fflate";
import { shareFileBytes } from "./file-share";
import { exportFilename, unescapeMarkdown } from "./export-doc";
import type { ExportResult } from "./export-doc";

function escapeXml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

// Bold/italic runs within a line. Each run is its own <w:r>, since
// WordprocessingML has no inline tag mixing — formatting is per-run.
function inlineRuns(text: string): string {
  // unescapeMarkdown BEFORE the split: the split matches bare asterisks, so an
  // escaped `\*bold\*` would otherwise carry its backslashes into the .docx —
  // the same defect the on-screen renderer had. See export-doc.ts for why
  // unescaping is unconditionally correct across this shared subset.
  const parts = unescapeMarkdown(text)
    .split(/(\*\*[^*]+\*\*|\*[^*\n]+\*)/g).filter(Boolean);
  return parts.map((p) => {
    if (/^\*\*[^*]+\*\*$/.test(p)) {
      return `<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">${escapeXml(p.slice(2, -2))}</w:t></w:r>`;
    }
    if (/^\*[^*\n]+\*$/.test(p)) {
      return `<w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve">${escapeXml(p.slice(1, -1))}</w:t></w:r>`;
    }
    return `<w:r><w:t xml:space="preserve">${escapeXml(p)}</w:t></w:r>`;
  }).join("");
}

function paragraph(runsXml: string, style?: string): string {
  const pPr = style ? `<w:pPr><w:pStyle w:val="${style}"/></w:pPr>` : "";
  return `<w:p>${pPr}${runsXml}</w:p>`;
}

function bulletParagraph(text: string): string {
  return `<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>${inlineRuns(text)}</w:p>`;
}

function numberedParagraph(text: string): string {
  return `<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr>${inlineRuns(text)}</w:p>`;
}

const isTableDivider = (l: string) => /^\|?[\s:|-]+\|[\s:|-]*$/.test(l) && l.includes("-");
const tableCells = (l: string) => l.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

function tableXml(header: string[], rows: string[][]): string {
  const cellWidth = Math.floor(9350 / Math.max(header.length, 1));
  // Cells render directly rather than through inlineRuns(), so they unescape
  // here — otherwise a `\*` inside a table survives into the .docx.
  const cellXml = (raw: string, bold: boolean) => {
    const text = unescapeMarkdown(raw);
    return `<w:tc><w:tcPr><w:tcW w:w="${cellWidth}" w:type="dxa"/></w:tcPr>` +
      `<w:p>${bold
        ? `<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">${escapeXml(text)}</w:t></w:r>`
        : `<w:r><w:t xml:space="preserve">${escapeXml(text)}</w:t></w:r>`}</w:p></w:tc>`;
  };
  const rowXml = (cells: string[], bold: boolean) =>
    `<w:tr>${cells.map((c) => cellXml(c, bold)).join("")}</w:tr>`;
  return `<w:tbl>` +
    `<w:tblPr><w:tblW w:w="9350" w:type="dxa"/><w:tblBorders>` +
    `<w:top w:val="single" w:sz="4" w:color="D8D2C6"/><w:left w:val="single" w:sz="4" w:color="D8D2C6"/>` +
    `<w:bottom w:val="single" w:sz="4" w:color="D8D2C6"/><w:right w:val="single" w:sz="4" w:color="D8D2C6"/>` +
    `<w:insideH w:val="single" w:sz="4" w:color="D8D2C6"/><w:insideV w:val="single" w:sz="4" w:color="D8D2C6"/>` +
    `</w:tblBorders></w:tblPr>` +
    rowXml(header, true) +
    rows.map((r) => rowXml(r, false)).join("") +
    `</w:tbl>`;
}

/** Markdown body -> the <w:body> paragraph/table XML (no document wrapper). */
function markdownToBodyXml(md: string): string {
  const lines = (md || "").split("\n");
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i].trim();
    if (!line) { i++; continue; }

    if (line.startsWith("|") && isTableDivider((lines[i + 1] ?? "").trim())) {
      const header = tableCells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(tableCells(lines[i].trim()));
        i++;
      }
      out.push(tableXml(header, rows));
      out.push(paragraph("")); // spacing after a table
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      out.push(paragraph(inlineRuns(heading[2]), heading[1].length <= 2 ? "Heading2" : "Heading3"));
      i++; continue;
    }

    const bullet = /^[-*+]\s+(.*)$/.exec(line);
    if (bullet) { out.push(bulletParagraph(bullet[1])); i++; continue; }

    const numbered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) { out.push(numberedParagraph(numbered[1])); i++; continue; }

    if (/^(---|___|\*\*\*)$/.test(line)) {
      out.push(`<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:color="E4DFD5"/></w:pBdr></w:pPr></w:p>`);
      i++; continue;
    }

    out.push(paragraph(inlineRuns(line)));
    i++;
  }
  return out.join("");
}

const CONTENT_TYPES_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
  `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
  `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
  `<Default Extension="xml" ContentType="application/xml"/>` +
  `<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>` +
  `<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>` +
  `<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>` +
  `</Types>`;

const RELS_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
  `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
  `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>` +
  `</Relationships>`;

const DOCUMENT_RELS_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
  `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
  `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>` +
  `<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>` +
  `</Relationships>`;

// Bulleted (numId 1) and numbered (numId 2) list definitions.
const NUMBERING_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
  `<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">` +
  `<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>` +
  `<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>` +
  `<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>` +
  `<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>` +
  `</w:numbering>`;

const STYLES_XML =
  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
  `<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">` +
  `<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/><w:sz w:val="23"/></w:rPr></w:rPrDefault></w:docDefaults>` +
  `<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>` +
  `<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>` +
  `<w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/><w:sz w:val="40"/></w:rPr></w:style>` +
  `<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>` +
  `<w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/><w:sz w:val="26"/><w:b/></w:rPr></w:style>` +
  `<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>` +
  `<w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/><w:sz w:val="23"/><w:b/></w:rPr></w:style>` +
  `</w:styles>`;

function documentXml(content: string, label: string, meetingTitle: string): string {
  const dateStr = new Date().toLocaleDateString(undefined, {
    day: "numeric", month: "long", year: "numeric",
  });
  const titlePara = paragraph(
    `<w:r><w:t xml:space="preserve">${escapeXml(meetingTitle || "Meeting")}</w:t></w:r>`,
    "Title"
  );
  const kickerPara = `<w:p><w:pPr><w:spacing w:after="60"/></w:pPr>` +
    `<w:r><w:rPr><w:caps/><w:color w:val="B4531F"/><w:sz w:val="16"/></w:rPr>` +
    `<w:t xml:space="preserve">${escapeXml(label)}</w:t></w:r></w:p>`;
  const metaPara = `<w:p><w:pPr><w:spacing w:after="240"/><w:pBdr><w:bottom w:val="single" w:sz="8" w:color="16130F"/></w:pBdr></w:pPr>` +
    `<w:r><w:rPr><w:color w:val="6B6459"/><w:sz w:val="17"/></w:rPr>` +
    `<w:t xml:space="preserve">Generated by MinuteX &#183; ${escapeXml(dateStr)}</w:t></w:r></w:p>`;

  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">` +
    `<w:body>` +
    kickerPara + titlePara + metaPara +
    markdownToBodyXml(content) +
    `<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>` +
    `</w:body></w:document>`;
}

/** Build a real, minimal OOXML .docx as raw bytes. */
export function buildDocx(content: string, label: string, meetingTitle: string): Uint8Array {
  const files: Record<string, Uint8Array> = {
    "[Content_Types].xml": strToU8(CONTENT_TYPES_XML),
    "_rels/.rels": strToU8(RELS_XML),
    "word/document.xml": strToU8(documentXml(content, label, meetingTitle)),
    "word/_rels/document.xml.rels": strToU8(DOCUMENT_RELS_XML),
    "word/numbering.xml": strToU8(NUMBERING_XML),
    "word/styles.xml": strToU8(STYLES_XML),
  };
  return zipSync(files, { level: 6 });
}

/** Render the document to .docx and hand it to the OS share sheet. */
export async function exportDocx(
  content: string, label: string, meetingTitle: string
): Promise<ExportResult> {
  try {
    const bytes = buildDocx(content, label, meetingTitle);
    const filename = exportFilename(label, meetingTitle, "docx");
    return await shareFileBytes(
      bytes, filename,
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    );
  } catch {
    return "failed";
  }
}
