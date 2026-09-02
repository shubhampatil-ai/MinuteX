// lib/meeting-outputs.ts — every shareable thing a meeting produces.
//
// WHY THIS EXISTS. Sharing used to be MoM-shaped: the share sheet held a
// `{pdf, docx}` pair and asked lib/mom-pdf/mom-docx for exactly one document.
// That is the wrong shape for the product — a meeting also produces an
// Executive Summary, an Action Item Report, a Follow-up Email draft, a Sales
// Meeting Report, and any number of freeform `custom_*` documents the user
// asked the AI for. Every one of them is a thing a person would reasonably
// want to email, and none of them were reachable.
//
// So the unit here is a MeetingOutput, not a MoM. A new AI document type
// added to the backend's prompts.DOCUMENTS becomes shareable with NO change to
// this file, the share sheet, or the API — it simply appears in the
// documents list the meeting already loads.
//
// TWO RENDERERS, DELIBERATELY. The codebase has two document pipelines and
// both are correct:
//
//   * The STRUCTURED MoM (lib/mom-docx.ts / lib/mom-html.ts) renders from the
//     Mom object — real column widths, a coloured header row, a repeating
//     table header. It is what the MoM editor previews and exports.
//   * MARKDOWN documents (lib/docx-export.ts / markdownToHtml) render the
//     Markdown subset the other seven AI types emit.
//
// This module picks the right one per output rather than forcing everything
// through one. Collapsing them would either downgrade the MoM to plain
// Markdown or pretend a Markdown document has a structure it does not.
//
// NOTHING IS RE-IMPLEMENTED HERE. Every byte comes from a renderer that
// already shipped and that the user has already previewed — which is the
// property that matters: the recipient opens the same document the sender saw.
import { Buffer } from "buffer";
import type { AiDocument, EmailAttachment, Mom } from "./api";
import {
  canExportPdf, exportFilename, markdownToHtml,
} from "./export-doc";
import { buildDocx } from "./docx-export";
import { buildMomDocx } from "./mom-docx";
import { momHtml } from "./mom-html";

// Mirrors MAX_ATTACHMENT_BYTES in cloud/shared/email_message.py. Checked here
// too — not because the client is trusted, but because failing on the device
// is instant and explains itself, while failing at the API costs a round trip
// and arrives as a generic rejection.
const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;

/** Mirrors MAX_ATTACHMENTS in cloud/shared/email_message.py.
 *
 *  Enforced in the UI as well as the backend because of WHEN the two fail:
 *  the backend rejects the request only after the device has rendered every
 *  file, which on a phone is several seconds of work thrown away — and the
 *  user learns their selection was invalid at the moment they expected the
 *  mail to go out. Four outputs at three formats each is 12 files, so this
 *  ceiling is genuinely reachable, not theoretical. */
export const MAX_ATTACHMENTS = 5;

/** The formats a meeting output can be sent as.
 *
 *  `md` exists because some outputs are genuinely text — the Follow-up Email
 *  draft is meant to be pasted, not opened in Word — and because it is the
 *  lossless form of what the backend stores. */
export type OutputFormat = "pdf" | "docx" | "md";

/** One shareable thing a meeting produced.
 *
 *  `kind` is what decides the renderer, and it is the only place the MoM's
 *  special status is encoded. Everything downstream — selection, filenames,
 *  the share sheet — treats all outputs identically. */
export type MeetingOutput = {
  /** The document type key ("minutes_of_meeting", "custom_a1b2…"). Unique
   *  within a meeting, so it doubles as the selection key. */
  id: string;
  label: string;
  /** "mom" renders from the structured Mom; "markdown" from doc.content. */
  kind: "mom" | "markdown";
  /** Formats offered for THIS output. PDF is dropped when the build has no
   *  expo-print, so the UI never offers a box that cannot produce a file. */
  formats: OutputFormat[];
  /** The Markdown body. Empty for a mom-kind output with no mirrored content. */
  content: string;
};

/** Formats a Markdown document can be sent as. PDF needs a native module. */
function formatsFor(): OutputFormat[] {
  return canExportPdf() ? ["pdf", "docx", "md"] : ["docx", "md"];
}

export const FORMAT_LABEL: Record<OutputFormat, string> = {
  pdf: "PDF", docx: "DOCX", md: "Markdown",
};

/**
 * Everything this meeting can currently share.
 *
 * ONLY WHAT ACTUALLY EXISTS. A document with no content is not listed —
 * offering an output that would produce an empty attachment is worse than not
 * offering it, because the user only finds out after the recipient opens it.
 *
 * `mom` is the structured Minutes when the user has generated one. When it is
 * present it REPLACES the mirrored `minutes_of_meeting` Markdown document
 * rather than sitting beside it: they are the same deliverable (the backend
 * mirrors the structure into that document), and listing both would offer the
 * user two "Minutes of Meeting" rows that differ only in typography.
 */
export function meetingOutputs(
  documents: AiDocument[], mom: Mom | null
): MeetingOutput[] {
  const out: MeetingOutput[] = [];

  if (mom) {
    out.push({
      id: "minutes_of_meeting",
      label: mom.title || "Minutes of Meeting",
      kind: "mom",
      // No `md` for the structured MoM: its Markdown mirror is a lossy
      // rendering of the structure, so offering it here would hand the
      // recipient a worse copy of a document we can render properly.
      formats: canExportPdf() ? ["pdf", "docx"] : ["docx"],
      content: "",
    });
  }

  for (const doc of documents) {
    if (!doc?.content?.trim()) continue;               // never offer an empty file
    if (mom && doc.type === "minutes_of_meeting") continue;  // structured wins
    out.push({
      id: doc.type,
      label: doc.label || doc.type,
      kind: "markdown",
      formats: formatsFor(),
      content: doc.content,
    });
  }

  return out;
}

/** The default format for a freshly-listed output — PDF where possible, since
 *  it is the one every recipient can open without Office. */
export function defaultFormat(output: MeetingOutput): OutputFormat {
  return output.formats.includes("pdf") ? "pdf" : output.formats[0];
}

export type BuildResult =
  | { ok: true; attachment: EmailAttachment }
  // "unsupported" is distinct from "failed" for the same reason
  // lib/export-doc.ts distinguishes them: the UI can say the app needs
  // rebuilding rather than reporting a failure the user cannot act on.
  | { ok: false; reason: "unsupported" | "failed" | "too_large"; label: string };

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

function withinLimit(base64: string): boolean {
  // base64 inflates by 4/3; compare the DECODED size against the same ceiling
  // the backend applies, so the two agree on what is too big.
  return (base64.length * 3) / 4 <= MAX_ATTACHMENT_BYTES;
}

function bytesToBase64(bytes: Uint8Array): string {
  // Buffer is a project dependency and is what the rest of the app uses for
  // binary work. btoa() is not reliably present in the RN runtime.
  return Buffer.from(bytes).toString("base64");
}

/**
 * Render ONE output in ONE format into an attachment.
 *
 * `mom` is required only for a mom-kind output; passing it for a Markdown one
 * is harmless and ignored.
 */
export async function buildOutputAttachment(
  output: MeetingOutput, format: OutputFormat, meetingTitle: string,
  mom: Mom | null
): Promise<BuildResult> {
  const filename = exportFilename(output.label, meetingTitle, format);
  const fail = (reason: "unsupported" | "failed" | "too_large"): BuildResult =>
    ({ ok: false, reason, label: `${output.label} (${FORMAT_LABEL[format]})` });

  try {
    if (format === "md") {
      const base64 = Buffer.from(output.content, "utf-8").toString("base64");
      if (!withinLimit(base64)) return fail("too_large");
      return { ok: true, attachment: { type: "md", filename, content_base64: base64 } };
    }

    if (format === "docx") {
      // The structured MoM keeps its own renderer; everything else goes
      // through the Markdown one. Two pipelines, one call site.
      const bytes = output.kind === "mom" && mom
        ? buildMomDocx(mom, meetingTitle)
        : buildDocx(output.content, output.label, meetingTitle);
      if (bytes.length > MAX_ATTACHMENT_BYTES) return fail("too_large");
      return {
        ok: true,
        attachment: { type: "docx", filename, content_base64: bytesToBase64(bytes) },
      };
    }

    // PDF.
    if (!print) return fail("unsupported");
    const html = output.kind === "mom" && mom
      ? momHtml(mom, meetingTitle)
      : markdownToHtml(output.content, output.label, meetingTitle);
    // base64:true asks the print engine for the bytes directly, rather than
    // writing a file and reading it back for something we only hand to the
    // network.
    const { base64 } = await print.printToFileAsync({ html, base64: true });
    if (!base64) return fail("failed");
    if (!withinLimit(base64)) return fail("too_large");
    return { ok: true, attachment: { type: "pdf", filename, content_base64: base64 } };
  } catch {
    return fail("failed");
  }
}

/** A user-facing sentence for a failed build. Names WHICH output failed —
 *  with several selected, "Couldn't build the PDF" leaves the user guessing. */
export function describeBuildFailure(r: Extract<BuildResult, { ok: false }>): string {
  switch (r.reason) {
    case "unsupported":
      return `This build can’t make PDFs, so ${r.label} can’t be attached. `
        + `Rebuild the app, or send it as DOCX.`;
    case "too_large":
      return `${r.label} is too large to email.`;
    default:
      return `Couldn’t prepare ${r.label}. Try removing it and sending again.`;
  }
}

export { canExportPdf };
