// lib/mom-pdf.ts — the structured MoM as a PDF.
//
// Thin on purpose. lib/export-doc.ts already owns the expo-print/expo-sharing
// plumbing, the defensive require() that keeps an older dev client from
// crashing, and the "unsupported" result the UI uses to explain WHY rather
// than showing a generic failure. All of that applies unchanged here — the
// only difference is the HTML, which comes from the structure (lib/mom-html.ts)
// instead of from Markdown.
//
// So this module exists to swap the HTML source and nothing else. Duplicating
// the module-loading dance would mean two places to get the degradation
// behaviour right.
import type { Mom } from "./api";
import { canExportPdf, printHtmlDetailed } from "./export-doc";
import type { ExportResult } from "./export-doc";
import { momHtml } from "./mom-html";

export { canExportPdf };

/**
 * Render the MoM to PDF and hand it to the OS share sheet.
 *
 * Returns "unsupported" when expo-print isn't in this build, so the caller can
 * say the app needs rebuilding rather than reporting a failure the user cannot
 * act on — the same contract lib/export-doc.ts's exportPdf offers.
 *
 * `pages` is the print engine's own pagination of the rendered document, and
 * is 0 when nothing was rendered. It is the ONLY honest page count available:
 * where the text breaks is decided by the renderer, not by anything the app
 * can compute in advance.
 */
export async function exportMomPdf(
  mom: Mom, meetingTitle: string
): Promise<{ result: ExportResult; pages: number }> {
  return printHtmlDetailed(momHtml(mom, meetingTitle),
                           mom.title || "Minutes of Meeting");
}
