// lib/processing.ts — a recording's backend status, as the steps a person sees.
//
// PURE (no React Native), so the mapping is pinned by
// lib/__tests__/processing.test.mjs.
//
// The pipeline really has THREE observable stages, and that is all this shows:
//
//   uploading ─▶ uploaded ─▶ transcribing ─▶ generating_ai ─▶ complete
//   └ Upload ┘   └──── Transcribe ─────┘   └─ Write brief ─┘
//
// "uploaded" is the gap between the bytes landing in S3 and the transcribe
// Lambda picking them up, so it belongs to the transcribe step (waiting to
// start), not to a step of its own. There is deliberately no "Analysing" or
// "Finalising" step: the backend reports no such states, and a step that
// ticks over on a timer would be a progress bar by another name.
//
// Nor is there a percentage. The backend says WHICH stage it is in, never how
// far through it, so any number would be invented.

export type StepState = "pending" | "active" | "done" | "failed";
export type PipelineStep = {
  key: "upload" | "transcribe" | "brief";
  label: string;
  state: StepState;
  detail?: string;
};

const LABELS = {
  upload: "Uploading recording",
  transcribe: "Transcribing audio",
  brief: "Writing the brief",
} as const;

const ORDER = ["upload", "transcribe", "brief"] as const;

/** Index of the step in progress, or the terminal outcome. */
function position(status: string): { at: number; outcome: "running" | "complete" | "failed" } {
  switch (status) {
    case "uploading": return { at: 0, outcome: "running" };
    case "uploaded":
    case "transcribing": return { at: 1, outcome: "running" };
    case "generating_ai": return { at: 2, outcome: "running" };
    // "failed" is an unrecoverable TRANSCRIPTION error (see RecordingStatus).
    case "failed": return { at: 1, outcome: "failed" };
    // Legacy: transcript stored, AI analysis failed.
    case "transcribed": return { at: 2, outcome: "failed" };
    // "complete", and any blank/unknown status — rows written before the
    // lifecycle existed were only ever written once fully processed (the same
    // rule statusMeta applies in lib/sources.ts).
    default: return { at: 3, outcome: "complete" };
  }
}

export function pipelineSteps(
  status: string | null | undefined,
  opts: { stuck?: boolean } = {},
): PipelineStep[] {
  const s = String(status ?? "");
  const { at, outcome } = position(s);
  return ORDER.map((key, i) => {
    let state: StepState;
    if (i < at) state = "done";
    else if (i === at) state = outcome === "failed" ? "failed" : outcome === "complete" ? "done" : "active";
    else state = "pending";

    let detail: string | undefined;
    if (state === "active") {
      if (opts.stuck) detail = "Taking much longer than usual";
      else if (s === "uploaded") detail = "Waiting to start";
    } else if (state === "failed") {
      detail = key === "transcribe"
        ? "Transcription didn't return usable text"
        : "The transcript is ready, but the brief didn't come through";
    }
    return { key, label: LABELS[key], state, ...(detail ? { detail } : {}) };
  });
}

/** One line for the current stage — a list row, a screen headline. */
export function stageLabel(status: string | null | undefined): string {
  switch (String(status ?? "")) {
    case "uploading": return "Uploading";
    case "uploaded": return "Queued";
    case "transcribing": return "Transcribing";
    case "generating_ai": return "Writing brief";
    case "failed": return "Couldn't finish";
    default: return "Ready";
  }
}

/** Is the backend still working on it? Mirrors statusMeta's "processing". */
export function isProcessingStatus(status: string | null | undefined): boolean {
  const s = String(status ?? "");
  return s === "uploading" || s === "uploaded" || s === "transcribing" || s === "generating_ai";
}
