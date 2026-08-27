// lib/email-attachments.ts — turn a MoM into the attachments an email carries.
//
// WHY THE RENDERING STAYS ON THE DEVICE. The PDF and DOCX renderers already
// live here (lib/mom-pdf.ts, lib/mom-docx.ts, both driven by the shared
// measurements in lib/mom-template.ts), and they are what the user PREVIEWS
// before sending. Re-implementing them in the Lambda would mean two renderers
// of the same document, and the first time they drifted a recipient would open
// a file that did not match what the sender saw. So the app renders, and the
// bytes travel base64 with the send request. The backend treats them as
// untrusted input and validates type, name and size (cloud/shared/
// email_message.py).
//
// This module is the ONLY place that conversion happens, so the size ceiling
// and the failure vocabulary are stated once.
import { Buffer } from "buffer";
import type { EmailAttachment, EmailAttachmentType, Mom } from "./api";
import { canExportPdf, exportFilename } from "./export-doc";
import { buildMomDocx } from "./mom-docx";
import { momHtml } from "./mom-html";

// Mirrors MAX_ATTACHMENT_BYTES in cloud/shared/email_message.py. Checked here
// too, not because the client is trusted, but because failing on the device is
// instant and explains itself, while failing at the API costs a round trip and
// arrives as a generic rejection.
const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;

type PrintModule = {
  printToFileAsync: (opts: { html: string; base64?: boolean }) =>
    Promise<{ uri: string; base64?: string }>;
};

// Same defensive require() as lib/export-doc.ts and the rest of this codebase:
// a dev client built before expo-print was added must degrade, not crash.
let print: PrintModule | null = null;
try {
  const mod = require("expo-print") as any;
  if (mod && typeof mod.printToFileAsync === "function") print = mod;
} catch {
  // expo-print not in this build
}

export type AttachmentBuild =
  | { ok: true; attachment: EmailAttachment }
  // "unsupported" is distinct from "failed" for the same reason
  // lib/export-doc.ts distinguishes them: the UI can say the app needs
  // rebuilding rather than reporting a failure the user cannot act on.
  | { ok: false; reason: "unsupported" | "failed" | "too_large" };

function bytesToBase64(bytes: Uint8Array): string {
  // Buffer is a project dependency (see package.json) and is what the rest of
  // the app uses for binary work. btoa() is not reliably present in the RN
  // runtime and would need a manual chunking loop for large inputs anyway.
  return Buffer.from(bytes).toString("base64");
}

/** The MoM as a PDF attachment. Returns "unsupported" when expo-print is not
 *  in this build, so the caller can hide the option instead of offering an
 *  action that will fail. */
export async function momPdfAttachment(
  mom: Mom, meetingTitle: string
): Promise<AttachmentBuild> {
  if (!print) return { ok: false, reason: "unsupported" };
  try {
    // base64:true asks the print engine for the bytes directly. The
    // alternative — write a file, read it back — is a round trip through the
    // filesystem for something we only ever hand to the network.
    const { base64 } = await print.printToFileAsync({
      html: momHtml(mom, meetingTitle), base64: true,
    });
    if (!base64) return { ok: false, reason: "failed" };
    // base64 inflates by 4/3; compare the DECODED size against the same
    // ceiling the backend applies, so the two agree on what is too big.
    if ((base64.length * 3) / 4 > MAX_ATTACHMENT_BYTES) {
      return { ok: false, reason: "too_large" };
    }
    return {
      ok: true,
      attachment: {
        type: "pdf",
        filename: exportFilename(
          mom.title || "Minutes of Meeting", meetingTitle, "pdf"),
        content_base64: base64,
      },
    };
  } catch {
    return { ok: false, reason: "failed" };
  }
}

/** The MoM as a DOCX attachment. Always available — fflate is a plain JS
 *  dependency, so unlike PDF there is no native module that can be missing. */
export function momDocxAttachment(
  mom: Mom, meetingTitle: string
): AttachmentBuild {
  try {
    const bytes = buildMomDocx(mom, meetingTitle);
    if (bytes.length > MAX_ATTACHMENT_BYTES) {
      return { ok: false, reason: "too_large" };
    }
    return {
      ok: true,
      attachment: {
        type: "docx",
        filename: exportFilename(
          mom.title || "Minutes of Meeting", meetingTitle, "docx"),
        content_base64: bytesToBase64(bytes),
      },
    };
  } catch {
    return { ok: false, reason: "failed" };
  }
}

/** Arbitrary text (a generated document's Markdown, say) as an attachment.
 *  Used for the document types that have no structured renderer — they are
 *  Markdown in the backend, and Markdown is what should arrive. */
export function textAttachment(
  content: string, filename: string, type: EmailAttachmentType = "md"
): AttachmentBuild {
  try {
    const base64 = Buffer.from(content, "utf-8").toString("base64");
    if ((base64.length * 3) / 4 > MAX_ATTACHMENT_BYTES) {
      return { ok: false, reason: "too_large" };
    }
    return { ok: true, attachment: { type, filename, content_base64: base64 } };
  } catch {
    return { ok: false, reason: "failed" };
  }
}

/** Whether a PDF can be produced in this build. Re-exported so a caller needs
 *  only this module, and so the PDF checkbox is hidden rather than offered and
 *  then failing. */
export { canExportPdf };
