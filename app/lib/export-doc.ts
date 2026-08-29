// lib/export-doc.ts — export a generated AI document.
//
// Four paths, in order of how universally they work:
//   copy      -> clipboard (expo-clipboard, Share-sheet fallback)
//   share     -> the OS share sheet, as Markdown text
//   markdown  -> a real .md file handed to the share sheet
//   pdf       -> a rendered PDF via expo-print
//
// expo-print and expo-file-system are NATIVE modules, loaded with the same
// defensive require() the rest of this codebase uses (see lib/clipboard.ts and
// lib/audio-player.tsx). A dev client built before they were added must not
// crash — it degrades to the share sheet, and canExportPdf() lets the UI hide
// the button rather than offer an action that will fail. Rebuild the dev client
// (EAS: `eas build --profile development`) to light PDF up.
//
// Markdown is the storage format, so copy/share are lossless and PDF is the
// only path that transforms anything.
import { Share } from "react-native";
import { copyText, type CopyResult } from "./clipboard";

type PrintModule = {
  // numberOfPages is the print engine's OWN pagination of the rendered HTML —
  // the only honest page count available, since nothing on the JS side knows
  // how the text will actually lay out. lib/mom-preview.tsx uses it to replace
  // its estimate once a PDF has been rendered.
  printToFileAsync: (opts: { html: string; base64?: boolean }) =>
    Promise<{ uri: string; numberOfPages?: number; base64?: string }>;
  printAsync: (opts: { html: string }) => Promise<void>;
};

type SharingModule = {
  isAvailableAsync: () => Promise<boolean>;
  shareAsync: (uri: string, opts?: Record<string, unknown>) => Promise<void>;
};

// expo-file-system's newer File/Directory API (SDK 54+). Only the bits used
// here are typed; `Paths.cache` is where a throwaway export file belongs.
type FsModule = {
  File: new (...parts: any[]) => {
    create: (opts?: { overwrite?: boolean }) => void;
    write: (contents: string) => void;
    uri: string;
  };
  Paths: { cache: any };
};

let print: PrintModule | null = null;
let sharing: SharingModule | null = null;
let fs: FsModule | null = null;

try {
  const mod = require("expo-print") as any;
  if (mod && typeof mod.printToFileAsync === "function") {
    print = mod;
  }
} catch {
  // expo-print not in this build
}

try {
  const mod = require("expo-sharing") as any;
  if (mod && typeof mod.shareAsync === "function") {
    sharing = mod;
  }
} catch {
  // expo-sharing not in this build
}

try {
  const mod = require("expo-file-system") as any;
  if (mod && typeof mod.File === "function") {
    fs = mod;
  }
} catch {
  // expo-file-system not in this build
}

/** True when a real PDF can be produced in this build. */
export function canExportPdf(): boolean {
  return !!print;
}

/** True when a file (rather than plain text) can be shared in this build. */
export function canExportFile(): boolean {
  return !!(fs && (sharing || print));
}

export type ExportResult = "copied" | "shared" | "saved" | "failed" | "unsupported";

// ---------------------------------------------------------------------------
// Filename. Derived from the document label + meeting title so a file landing
// in Downloads is identifiable, with everything a filesystem might object to
// stripped out.
// ---------------------------------------------------------------------------
export function exportFilename(
  label: string, meetingTitle: string, ext: string
): string {
  const base = [meetingTitle, label]
    .map((s) => (s || "").trim())
    .filter(Boolean)
    .join(" - ")
    .replace(/[^\w\s.-]/g, "")   // drop punctuation the FS may dislike
    .replace(/\s+/g, "_")
    .slice(0, 80);               // keep well under any path length limit
  return `${base || "MinuteX_Document"}.${ext}`;
}

/** Copy the document's Markdown to the clipboard. */
export async function copyDocument(content: string): Promise<CopyResult> {
  return copyText(content);
}

/** Hand the Markdown to the OS share sheet as text. Always available. */
export async function shareDocument(
  content: string, title?: string
): Promise<ExportResult> {
  try {
    await Share.share({ message: content, title });
    return "shared";
  } catch {
    return "failed";
  }
}

/**
 * Write the document as a real .md file and share it.
 *
 * Falls back to sharing the text when the filesystem module isn't in this
 * build — the user still gets their content, just not as an attachment.
 */
export async function exportMarkdown(
  content: string, label: string, meetingTitle: string
): Promise<ExportResult> {
  if (!fs) return shareDocument(content, label);
  try {
    const file = new fs.File(fs.Paths.cache, exportFilename(label, meetingTitle, "md"));
    file.create({ overwrite: true });
    file.write(content);
    return shareFile(file.uri, "text/markdown", label);
  } catch {
    // A filesystem hiccup shouldn't cost the user their export.
    return shareDocument(content, label);
  }
}

/**
 * Render the document to PDF and share it.
 *
 * Returns "unsupported" when expo-print isn't in the build, so the caller can
 * say why instead of showing a generic failure. Guarded by canExportPdf() at
 * the call site, but re-checked here because this is also a public API.
 */
export async function exportPdf(
  content: string, label: string, meetingTitle: string
): Promise<ExportResult> {
  return printHtml(markdownToHtml(content, label, meetingTitle), label);
}

/**
 * Render ARBITRARY print HTML to a PDF and share it.
 *
 * The half of exportPdf that has nothing to do with Markdown, exposed so
 * lib/mom-pdf.ts can render the structured MoM's own HTML through exactly
 * this path — the module loading, the "unsupported" contract and the
 * share/save fallback are all subtle enough that a second copy would drift.
 *
 * `label` names the share sheet's dialog only. expo-print chooses the temp
 * file's own name, so there is no filename parameter to honour here.
 */
export async function printHtml(
  html: string, label: string
): Promise<ExportResult> {
  return (await printHtmlDetailed(html, label)).result;
}

/** printHtml, plus the print engine's own page count.
 *
 * Separate from printHtml so the common caller keeps the simple
 * ExportResult contract, while the MoM preview can report a REAL page count
 * ("3 pages") in place of its own estimate. `pages` is 0 when expo-print
 * isn't in the build or the render failed — callers must not present 0 as a
 * page count.
 */
export async function printHtmlDetailed(
  html: string, label: string
): Promise<{ result: ExportResult; pages: number }> {
  if (!print) return { result: "unsupported", pages: 0 };
  try {
    const { uri, numberOfPages } = await print.printToFileAsync({ html });
    return {
      result: await shareFile(uri, "application/pdf", label),
      pages: numberOfPages ?? 0,
    };
  } catch {
    return { result: "failed", pages: 0 };
  }
}

async function shareFile(
  uri: string, mimeType: string, label: string
): Promise<ExportResult> {
  // expo-sharing is the right tool (it shares a file URI as an attachment).
  if (sharing) {
    try {
      if (await sharing.isAvailableAsync()) {
        await sharing.shareAsync(uri, { mimeType, dialogTitle: label });
        return "shared";
      }
    } catch {
      // fall through — report the file as saved rather than lose it
    }
  }
  // Without expo-sharing the file still exists in the cache directory; saying
  // "saved" is honest, where "shared" would not be.
  return "saved";
}

// ---------------------------------------------------------------------------
// Markdown -> HTML for the PDF renderer.
//
// A deliberately SMALL subset — headings, bold/italic, bullets, numbered
// lists, tables, paragraphs — because that is exactly what the document
// prompts are instructed to emit (see lambda-shared/prompts.py). Pulling in a
// full Markdown library to render a document whose grammar we control would be
// dependency weight for no gain.
//
// Everything is escaped BEFORE any markup is introduced, so document text can
// never inject HTML into the PDF.
// ---------------------------------------------------------------------------
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inlineMarkdown(s: string): string {
  return escapeHtml(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*(?!\s)(.+?)(?<!\s)\*(?=\W|$)/g, "$1<em>$2</em>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
}

function tableRow(line: string): string[] {
  return line.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
}

const isTableDivider = (line: string) => /^\|?[\s:|-]+\|[\s:|-]*$/.test(line)
  && line.includes("-");

export function markdownToHtml(
  md: string, label: string, meetingTitle: string
): string {
  const lines = (md || "").split("\n");
  const out: string[] = [];
  let list: "ul" | "ol" | null = null;
  let inTable = false;

  const closeList = () => {
    if (list) { out.push(`</${list}>`); list = null; }
  };
  const closeTable = () => {
    if (inTable) { out.push("</tbody></table>"); inTable = false; }
  };
  const closeAll = () => { closeList(); closeTable(); };

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trim();

    if (!line) { closeAll(); continue; }

    // Tables: a pipe row followed by a --- divider row starts one.
    if (line.startsWith("|") && !inTable && isTableDivider(lines[i + 1]?.trim() ?? "")) {
      closeList();
      const headers = tableRow(line).map((c) => `<th>${inlineMarkdown(c)}</th>`);
      out.push(`<table><thead><tr>${headers.join("")}</tr></thead><tbody>`);
      inTable = true;
      i++;                              // consume the divider
      continue;
    }
    if (inTable) {
      if (!line.startsWith("|")) { closeTable(); }
      else {
        const cells = tableRow(line).map((c) => `<td>${inlineMarkdown(c)}</td>`);
        out.push(`<tr>${cells.join("")}</tr>`);
        continue;
      }
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      closeAll();
      // The document's own ## headings become h2 etc.; h1 is reserved for the
      // page title we add ourselves, so everything shifts down one level.
      const level = Math.min(6, heading[1].length + 1);
      out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = /^[-*+]\s+(.*)$/.exec(line);
    if (bullet) {
      closeTable();
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inlineMarkdown(bullet[1])}</li>`);
      continue;
    }

    const numbered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      closeTable();
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inlineMarkdown(numbered[1])}</li>`);
      continue;
    }

    if (/^(---|___|\*\*\*)$/.test(line)) { closeAll(); out.push("<hr/>"); continue; }

    closeAll();
    out.push(`<p>${inlineMarkdown(line)}</p>`);
  }
  closeAll();

  // Print styling, not screen styling: serif body, generous margins, and
  // page-break rules so a heading never lands alone at the foot of a page.
  return `<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
  @page { margin: 20mm 16mm; }
  body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 11.5pt;
         line-height: 1.55; color: #12131A; }
  .kicker { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 8pt;
            letter-spacing: 1.2px; text-transform: uppercase; color: #3E6BFF;
            margin: 0 0 4px; }
  h1 { font-size: 20pt; line-height: 1.2; margin: 0 0 4px; font-weight: 700; }
  .meta { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 8.5pt;
          color: #5B6072; margin: 0 0 18px; padding-bottom: 12px;
          border-bottom: 1.5px solid #E6E9F0; }
  h2 { font-size: 13pt; margin: 20px 0 7px; page-break-after: avoid; }
  h3 { font-size: 11.5pt; margin: 15px 0 5px; page-break-after: avoid; }
  p { margin: 0 0 9px; }
  ul, ol { margin: 0 0 10px; padding-left: 20px; }
  li { margin-bottom: 4px; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0 14px;
          font-size: 10pt; page-break-inside: avoid; }
  th, td { border: 1px solid #E6E9F0; padding: 6px 8px; text-align: left;
           vertical-align: top; }
  th { background: #F0F2F7; font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
       font-size: 8.5pt; text-transform: uppercase; letter-spacing: 0.6px; }
  code { font-family: 'Courier New', monospace; font-size: 10pt; }
  hr { border: 0; border-top: 1px solid #E6E9F0; margin: 16px 0; }
  .footer { margin-top: 26px; padding-top: 9px; border-top: 1px solid #E6E9F0;
            font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 8pt;
            color: #9297A8; }
</style></head><body>
<p class="kicker">${escapeHtml(label)}</p>
<h1>${escapeHtml(meetingTitle || "Meeting")}</h1>
<p class="meta">Generated by MinuteX${
    meetingTitle ? "" : ""} &middot; ${new Date().toLocaleDateString(undefined, {
      day: "numeric", month: "long", year: "numeric",
    })}</p>
${out.join("\n")}
<p class="footer">Generated by MinuteX AI from the meeting recording. Review before circulating.</p>
</body></html>`;
}
