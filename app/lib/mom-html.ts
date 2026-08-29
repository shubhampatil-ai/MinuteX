// lib/mom-html.ts — the structured MoM as print-ready HTML.
//
// This is the PDF path: expo-print renders it via lib/mom-pdf.ts.
//
// The in-app preview does NOT use this HTML — react-native-webview is not in
// the project, so lib/mom-preview.tsx draws native views instead. The two are
// kept honest by both reading lib/mom-template.ts: same colours, same type
// scale, same column weights, same section order. See that file's header.
//
// This does NOT go through lib/export-doc.ts's markdownToHtml. That function
// renders the small Markdown subset the AI document prompts emit, and it is
// still exactly right for the seven other document types. A structured MoM has
// something Markdown cannot express — per-column widths, zebra rows, repeated
// table headers across a page break, a masthead and a running footer — so it
// renders from the STRUCTURE instead. Both paths share lib/mom-template.ts's
// constants, so the two never drift on colour or type size.
//
// Everything is escaped before any markup is introduced (escapeMarkup), so
// document text can never inject HTML into the PDF.
import type { Mom, MomSection } from "./api";
import { isSectionEmpty } from "./mom-model";
import {
  MOM_TEMPLATE as T, columnWeights, escapeMarkup, templateDate,
} from "./mom-template";

/** Preserve author line breaks in prose without allowing any other markup. */
function paragraphs(text: string): string {
  return (text || "")
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => `<p>${escapeMarkup(block).replace(/\n/g, "<br/>")}</p>`)
    .join("");
}

function fieldsHtml(section: MomSection): string {
  const rows = (section.fields ?? [])
    .filter((f) => f.visible && (f.label || f.value))
    .map((f) => `<tr><th scope="row">${escapeMarkup(f.label)}</th>`
      + `<td>${escapeMarkup(f.value).replace(/\n/g, "<br/>")}</td></tr>`)
    .join("");
  return rows ? `<table class="kv">${rows}</table>` : "";
}

function tableHtml(section: MomSection): string {
  const columns = section.columns ?? [];
  const rows = (section.rows ?? []).filter((r) => r.visible);
  if (!columns.length || !rows.length) return "";

  const weights = columnWeights(columns.map((c) => c.label));
  const cols = weights
    .map((w) => `<col style="width:${(w * 100).toFixed(2)}%"/>`)
    .join("");
  // <thead> rather than a plain first row: a table that spans a page break
  // repeats its header in print, so page 2 of a long Action Items table is
  // still readable.
  const head = columns
    .map((c) => `<th>${escapeMarkup(c.label)}</th>`)
    .join("");
  const body = rows
    .map((r) => "<tr>" + columns
      .map((c) => `<td>${escapeMarkup(r.cells[c.id] ?? "").replace(/\n/g, "<br/>")}</td>`)
      .join("") + "</tr>")
    .join("");
  return `<table class="grid"><colgroup>${cols}</colgroup>`
    + `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function listHtml(section: MomSection): string {
  const items = (section.items ?? [])
    .filter((i) => i.visible && i.text.trim())
    .map((i) => `<li>${escapeMarkup(i.text).replace(/\n/g, "<br/>")}</li>`)
    .join("");
  return items ? `<ul>${items}</ul>` : "";
}

function sectionHtml(section: MomSection): string {
  let body = "";
  if (section.kind === "fields") body = fieldsHtml(section);
  else if (section.kind === "table") body = tableHtml(section);
  else if (section.kind === "text") body = paragraphs(section.text ?? "");
  else body = listHtml(section);
  if (!body) return "";
  return `<section class="sec"><h2>${escapeMarkup(section.title)}</h2>${body}</section>`;
}

/** The whole MoM as a standalone, print-ready HTML document. */
export function momHtml(mom: Mom, meetingTitle: string): string {
  const sections = mom.sections
    .filter((s) => s.visible && !isSectionEmpty(s))
    .map(sectionHtml)
    .filter(Boolean)
    .join("");

  const title = mom.title?.trim() || "Minutes of Meeting";
  const subtitle = mom.subtitle?.trim() || meetingTitle || "";

  const empty = '<section class="sec"><p class="empty">This MoM has no visible '
    + 'content yet. Add a section in the editor to see it here.</p></section>';

  return `<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>${escapeMarkup(title)}</title>
<style>
  @page { size: ${T.pageWidthIn}in ${T.pageHeightIn}in; margin: ${T.marginIn}in; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
    font-size: ${T.bodyPt}pt; line-height: 1.5; color: #${T.ink};
    background: #fff;
    -webkit-text-size-adjust: 100%;
  }
  /* Masthead ------------------------------------------------------------ */
  .kicker { font-size: ${T.kickerPt}pt; letter-spacing: 1.4px;
            text-transform: uppercase; color: #${T.accent};
            font-weight: 700; margin: 0 0 3px; }
  h1 { font-size: ${T.titlePt}pt; line-height: 1.15; margin: 0 0 2px;
       font-weight: 700; letter-spacing: -0.3px; }
  .subtitle { font-size: ${T.subtitlePt}pt; color: #${T.inkDim};
              margin: 0 0 10px; font-weight: 600; }
  .masthead { border-bottom: 2.5px solid #${T.accent};
              padding-bottom: 9px; margin-bottom: 4px; }
  .meta { font-size: ${T.kickerPt}pt; color: #${T.inkFaint};
          margin: 6px 0 20px; display: flex; justify-content: space-between;
          gap: 12px; }
  /* Sections ------------------------------------------------------------ */
  .sec { margin: 0 0 18px; }
  /* A heading must never be the last thing on a page. */
  h2 { font-size: ${T.headingPt}pt; margin: 0 0 7px; font-weight: 700;
       color: #${T.accent}; text-transform: uppercase; letter-spacing: 0.7px;
       page-break-after: avoid; break-after: avoid; }
  p { margin: 0 0 8px; }
  ul { margin: 0 0 8px; padding-left: 17px; }
  li { margin-bottom: 4px; }
  .empty { color: #${T.inkFaint}; font-style: italic; }
  /* Tables -------------------------------------------------------------- */
  table { border-collapse: collapse; width: 100%; margin: 0 0 6px;
          font-size: ${T.tablePt}pt; }
  /* Never avoid breaking a LONG table — forcing a 40-row table onto one page
     is what pushes content off the sheet entirely. Rows break individually
     and thead repeats. */
  .grid thead th {
    background: #${T.accent}; color: #fff; text-align: left;
    font-size: ${T.kickerPt}pt; text-transform: uppercase;
    letter-spacing: 0.6px; padding: 6px 8px; font-weight: 700;
  }
  .grid td { border: 1px solid #${T.rule}; padding: 5px 8px;
             vertical-align: top; word-break: break-word; }
  .grid tbody tr:nth-child(even) { background: #${T.zebra}; }
  .grid tr { page-break-inside: avoid; break-inside: avoid; }
  thead { display: table-header-group; }
  /* Meeting Details reads as label/value pairs, not as a grid. */
  .kv th { text-align: left; width: 33%; padding: 4px 10px 4px 0;
           color: #${T.inkDim}; font-weight: 600; vertical-align: top;
           font-size: ${T.bodyPt}pt; }
  .kv td { padding: 4px 0; vertical-align: top; font-size: ${T.bodyPt}pt; }
  .kv tr { page-break-inside: avoid; break-inside: avoid; }
  /* Footer -------------------------------------------------------------- */
  .footer { margin-top: 22px; padding-top: 8px;
            border-top: 1px solid #${T.rule};
            font-size: ${T.footerPt}pt; color: #${T.inkFaint};
            display: flex; justify-content: space-between; gap: 12px; }
</style></head><body>
<div class="masthead">
  <p class="kicker">${escapeMarkup(T.company)}</p>
  <h1>${escapeMarkup(title)}</h1>
  ${subtitle ? `<p class="subtitle">${escapeMarkup(subtitle)}</p>` : ""}
</div>
<div class="meta"><span>${escapeMarkup(templateDate())}</span></div>
${sections || empty}
<div class="footer"><span>${escapeMarkup(T.footerNote)}</span></div>
</body></html>`;
}
