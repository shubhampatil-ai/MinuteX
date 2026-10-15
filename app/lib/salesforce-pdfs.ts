// lib/salesforce-pdfs.ts — the MoM and the generated documents go to
// Salesforce as PDFs only, never as text.
//
// The server has no PDF renderer, so the app renders them:
//   MoM        at Link (salesforce-link-card) and after every MoM change
//              (save, regenerate, reset in mom-editor);
//   documents  at Link and whenever the meeting's documents change
//              (meeting-context). The SERVER says which ones are missing or
//              were made from older text (stale_documents), so the app
//              renders only those — the renderer stamps the time into every
//              PDF, so comparing bytes would resend everything.
//
// Best effort and in the background: a meeting that isn't linked, doesn't
// include the output, or a build without expo-print sends nothing new.
import {
  getMeetingLinks, listAiDocuments, uploadMeetingDocumentPdf, uploadMeetingMomPdf, type Mom,
} from "./api";
import { documentPdfAttachment, momPdfAttachment } from "./email-attachments";
import { SALESFORCE_LINK_ENABLED } from "./salesforce-link-sheet";

/** Mirrors contract_v3.MAX_FILE_BYTES: the most one Salesforce file may be. */
export const SALESFORCE_MAX_FILE_BYTES = 2 * 1024 * 1024;

type Why = "" | "unsupported" | "too_large" | "failed";

function tooBig(base64: string): boolean {
  return (base64.length * 3) / 4 > SALESFORCE_MAX_FILE_BYTES;
}

/** Render the MoM to PDF and upload it. "" when sent; otherwise why not. */
export async function uploadMomPdf(meetingKey: string, mom: Mom): Promise<Why> {
  const built = await momPdfAttachment(mom, mom.title || "Minutes of Meeting");
  if (!built.ok) return built.reason;
  if (tooBig(built.attachment.content_base64)) return "too_large";
  await uploadMeetingMomPdf(meetingKey, built.attachment.content_base64, built.attachment.filename);
  return "";
}

/** After the MoM changed: refresh its PDF in Salesforce, when the meeting
 *  is linked with the MoM included. Never throws. */
export async function refreshSalesforceMomPdf(meetingKey: string, mom: Mom | null | undefined): Promise<void> {
  if (!SALESFORCE_LINK_ENABLED || !mom) return;
  try {
    const links = await getMeetingLinks(meetingKey);
    if (!links?.included_outputs.includes("mom")) return;
    await uploadMomPdf(meetingKey, mom);
  } catch {
    // Not linked here, no permission, or offline: the next change retries.
  }
}

// One refresh per meeting at a time; a change during one runs it again after.
const docsRunning = new Map<string, Promise<void>>();
const docsAgain = new Set<string>();

/** Make and upload the PDFs of the meeting's generated documents that the
 *  server lists as missing or outdated, when Documents is included. Never
 *  throws. */
export function refreshSalesforceDocumentPdfs(meetingKey: string, meetingTitle = ""): Promise<void> {
  if (!SALESFORCE_LINK_ENABLED || !meetingKey) return Promise.resolve();
  const running = docsRunning.get(meetingKey);
  if (running) {
    docsAgain.add(meetingKey);
    return running;
  }
  const run = (async () => {
    do {
      docsAgain.delete(meetingKey);
      await refreshDocumentsOnce(meetingKey, meetingTitle);
    } while (docsAgain.has(meetingKey));
  })().finally(() => docsRunning.delete(meetingKey));
  docsRunning.set(meetingKey, run);
  return run;
}

async function refreshDocumentsOnce(meetingKey: string, meetingTitle: string): Promise<void> {
  try {
    const links = await getMeetingLinks(meetingKey);
    const stale = links?.stale_documents ?? [];
    if (!links?.included_outputs.includes("documents") || !stale.length) return;
    const { documents } = await listAiDocuments(meetingKey);
    for (const type of stale) {
      const doc = documents[type];
      if (!doc?.content?.trim()) continue;
      const built = await documentPdfAttachment(doc.content, doc.label || type, meetingTitle);
      if (!built.ok) {
        if (built.reason === "unsupported") return;   // no PDFs in this build at all
        continue;
      }
      if (tooBig(built.attachment.content_base64)) continue;
      try {
        await uploadMeetingDocumentPdf(meetingKey, type, built.attachment.content_base64,
                                       built.attachment.filename);
      } catch {
        // This one failed (deleted meanwhile, too large): the others still go.
      }
    }
  } catch {
    // Not linked here, no permission, or offline: the next change retries.
  }
}
