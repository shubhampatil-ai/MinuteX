// lib/salesforce-link-flow.ts — linking a meeting to Salesforce records
// (Contract v3) — the UI logic.
//
// PURE: no React Native, no network — imported directly by
// lib/__tests__/salesforce-link-flow.test.mjs. The link sheet only renders
// what these return. Nothing uses it yet; the screens come next, behind a
// flag.
//
// THE MODEL (cloud/shared/contract_v3.py is the sender-side twin):
//   * a meeting links to 1-5 records, exactly one PRIMARY;
//   * the admin configures each linkable OBJECT: which outputs it allows,
//     which are on by default, which a user may change, whether it can be
//     the primary;
//   * the user picks outputs once for the meeting; each record then gets
//     only what its own object allows;
//   * every task follows the primary unless the user points it elsewhere.
// A record's Salesforce id is kept for the payload but NEVER shown: people
// see its name and object label only.

export type LinkOutput =
  | "overview" | "mom" | "tasks" | "participants" | "participant_contributions"
  | "transcript" | "recording" | "documents";

/** Canonical order, the same as contract_v3.OUTPUTS. */
export const LINK_OUTPUTS: LinkOutput[] = [
  "overview", "mom", "tasks", "participants", "participant_contributions",
  "transcript", "recording", "documents",
];

export const OUTPUT_LABELS: Record<LinkOutput, string> = {
  overview: "Overview & highlights",
  mom: "MoM (PDF)",
  tasks: "Tasks",
  participants: "Participants",
  participant_contributions: "What each person said",
  transcript: "Transcript",
  recording: "Recording link",
  documents: "Documents (PDF)",
};

/** The MoM's own Markdown mirror among the meeting's documents. It goes as
 *  the MoM PDF, never as a document (contract_v3.MOM_DOCUMENT_TYPE). */
export const MOM_DOCUMENT_TYPE = "minutes_of_meeting";

/** One of the meeting's documents, by the name people know it by. */
export type MeetingDocument = { type: string; label: string };

/** What the meeting actually has to send. Only what a person creates is
 *  gated: the MoM and the documents. null = not known (the load failed),
 *  and then nothing is gated, as before. */
export type MeetingOutputs = { mom: boolean; documents: MeetingDocument[] };

/** The meeting's documents that can go to Salesforce: not the MoM mirror,
 *  not an empty one, in name order. */
export function sendableDocuments(
  docs: Record<string, { label?: string; content?: string }> | null | undefined,
): MeetingDocument[] {
  return Object.entries(docs ?? {})
    .filter(([type, d]) => type !== MOM_DOCUMENT_TYPE && !!d?.content?.trim())
    .map(([type, d]) => ({ type, label: (d.label || "").trim() || type }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

/** What to tell a person about an output the meeting doesn't have yet. */
export const MISSING_HINT: Partial<Record<LinkOutput, string>> = {
  mom: "Not generated yet. Generate the MoM in the Documents tab first.",
  documents: "No documents yet. Create one in the Documents tab first.",
};

/** Fallback only, and deliberately the strictest value. The real limit
 *  comes from the backend in LinkConfig.max_links: cloud/shared/contract_v3.py
 *  MAX_LINKS is the ONE place it's set. Always read it through
 *  maxLinks(config); nothing can be linked before the configuration loads
 *  anyway (no linkable objects yet). */
export const MAX_LINKS = 1;

/** One object the admin enabled (mx__Linkable_Object__c, as MinuteX reads it). */
export type LinkableObject = {
  object: string;                       // API name, e.g. "Property__c"
  label: string;                        // e.g. "Property"
  can_be_primary: boolean;
  outputs_allowed: LinkOutput[];
  outputs_default_on: LinkOutput[];
  outputs_user_can_change: LinkOutput[];
};

export type LinkConfig = {
  objects: LinkableObject[];
  /** Records per meeting, from the backend. 1 means "one record only". */
  max_links?: number;
};

/** The record limit in force: the backend's, else the fallback. */
export function maxLinks(config: LinkConfig | null | undefined): number {
  const n = config?.max_links;
  return typeof n === "number" && n >= 1 ? Math.floor(n) : MAX_LINKS;
}

/** True when only one record can be linked: the sheet shows a single
 *  record picker, and picking another record replaces it. */
export function singleRecord(config: LinkConfig | null | undefined): boolean {
  return maxLinks(config) === 1;
}

function records(n: number): string {
  return n === 1 ? "1 record" : `${n} records`;
}

export type LinkedRecord = {
  object: string;
  id: string;
  name: string;
  secondary?: string;
  primary: boolean;
};

/** A task's chosen record, or null to follow the primary. */
export type TaskTarget = { object: string; id: string; name: string } | null;

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

export function objectOf(config: LinkConfig | null | undefined, object: string): LinkableObject | null {
  return config?.objects.find((o) => o.object === object) ?? null;
}

/** 15- and 18-character forms of one id are the same record. */
export function sameRecord(a: string, b: string): boolean {
  return a.slice(0, 15) === b.slice(0, 15);
}

function canBePrimary(config: LinkConfig | null | undefined, rec: LinkedRecord): boolean {
  return !!objectOf(config, rec.object)?.can_be_primary;
}

/** Re-pick the primary when it's missing: the first record allowed to be one. */
function ensurePrimary(links: LinkedRecord[], config: LinkConfig | null | undefined): LinkedRecord[] {
  if (links.some((l) => l.primary)) return links;
  const first = links.findIndex((l) => canBePrimary(config, l));
  return links.map((l, i) => ({ ...l, primary: i === first }));
}

/** Add a record. Returns the new list, or an error a person can read.
 *  With a limit of one, picking a record REPLACES the current one. */
export function addLink(
  links: LinkedRecord[], rec: Omit<LinkedRecord, "primary">, config: LinkConfig | null | undefined,
): { links: LinkedRecord[]; error?: string } {
  const obj = objectOf(config, rec.object);
  if (!obj) return { links, error: "Records of this kind can't be linked." };
  if (links.some((l) => sameRecord(l.id, rec.id))) {
    return { links, error: `${rec.name} is already linked.` };
  }
  const max = maxLinks(config);
  if (max === 1) return { links: ensurePrimary([{ ...rec, primary: false }], config) };
  if (links.length >= max) {
    return { links, error: `A meeting can be linked to at most ${records(max)}.` };
  }
  return { links: ensurePrimary([...links, { ...rec, primary: false }], config) };
}

export function removeLink(
  links: LinkedRecord[], id: string, config: LinkConfig | null | undefined,
): LinkedRecord[] {
  return ensurePrimary(links.filter((l) => !sameRecord(l.id, id)), config);
}

/** Make `id` the primary, if its object may be one; otherwise unchanged. */
export function setPrimary(
  links: LinkedRecord[], id: string, config: LinkConfig | null | undefined,
): LinkedRecord[] {
  const target = links.find((l) => sameRecord(l.id, id));
  if (!target || !canBePrimary(config, target)) return links;
  return links.map((l) => ({ ...l, primary: sameRecord(l.id, id) }));
}

export function primaryOf(links: LinkedRecord[]): LinkedRecord | null {
  return links.find((l) => l.primary) ?? null;
}

/** Why Link can't be pressed yet, or null when it can. */
export function linkProblem(links: LinkedRecord[], config: LinkConfig | null | undefined): string | null {
  if (!links.length) return "Choose at least one record.";
  if (links.length > maxLinks(config)) return `Link at most ${records(maxLinks(config))}.`;
  if (links.some((l) => !objectOf(config, l.object))) {
    return "One of these records can't be linked any more. Remove it.";
  }
  const primary = primaryOf(links);
  if (!primary || !canBePrimary(config, primary)) {
    return "Choose a main record. None of these kinds can be the main one.";
  }
  return null;
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

export type OutputOption = {
  output: LinkOutput;
  label: string;
  /** False when no linked record's object allows it. */
  offered: boolean;
  /** True when the user may not change it (the admin fixed it). */
  locked: boolean;
  /** Ticked when the sheet opens. */
  defaultOn: boolean;
  /** Where it won't go, e.g. "Property only"; "" when it goes everywhere. */
  note: string;
  /** False when the meeting doesn't have it yet (no MoM, no documents):
   *  it can't be ticked, and `missing` says how to make it. */
  available: boolean;
  missing: string;
};

/** What the link sheet offers, given the records chosen so far. Defaults
 *  come from the primary record's object; an output only other objects
 *  allow takes its default from the first of those. */
export function outputOptions(
  links: LinkedRecord[], config: LinkConfig | null | undefined,
  has: MeetingOutputs | null = null,
): OutputOption[] {
  const objs = links.map((l) => objectOf(config, l.object)).filter((o): o is LinkableObject => !!o);
  const primary = primaryOf(links);
  const primaryObj = primary ? objectOf(config, primary.object) : null;
  return LINK_OUTPUTS.map((output) => {
    const allowing = objs.filter((o) => o.outputs_allowed.includes(output));
    const offered = allowing.length > 0;
    const source = primaryObj && primaryObj.outputs_allowed.includes(output) ? primaryObj : allowing[0];
    const defaultOn = !!source && source.outputs_default_on.includes(output);
    const locked = offered && !allowing.some((o) => o.outputs_user_can_change.includes(output));
    const uniqueLabels = [...new Set(allowing.map((o) => o.label))];
    const everyone = new Set(objs.map((o) => o.object)).size === new Set(allowing.map((o) => o.object)).size;
    const note = offered && !everyone ? `${uniqueLabels.join(", ")} only` : "";
    const available = !has || (output === "mom" ? has.mom
      : output === "documents" ? has.documents.length > 0 : true);
    const missing = available ? "" : MISSING_HINT[output] ?? "";
    return { output, label: OUTPUT_LABELS[output], offered, locked, defaultOn, note, available, missing };
  });
}

/** What can actually go: an output the meeting doesn't have yet is dropped
 *  unless the admin fixed it on (then it goes once it's made). */
export function sendableOutputs(selected: LinkOutput[], options: OutputOption[]): LinkOutput[] {
  return normalizeOutputs(selected.filter((o) => {
    const opt = options.find((x) => x.output === o);
    return !opt || opt.available || opt.locked;
  }));
}

/** The outputs ticked when the sheet opens. */
export function initialOutputs(options: OutputOption[]): LinkOutput[] {
  return sendableOutputs(options.filter((o) => o.offered && o.defaultOn).map((o) => o.output), options);
}

/** "What each person said" only travels with the participants. */
export function normalizeOutputs(selected: LinkOutput[]): LinkOutput[] {
  const set = new Set(selected);
  if (!set.has("participants")) set.delete("participant_contributions");
  return LINK_OUTPUTS.filter((o) => set.has(o));
}

/** Tick or untick one output. Locked or unoffered outputs don't change. */
export function toggleOutput(
  selected: LinkOutput[], output: LinkOutput, options: OutputOption[],
): LinkOutput[] {
  const opt = options.find((o) => o.output === output);
  if (!opt || !opt.offered || opt.locked || !opt.available) return normalizeOutputs(selected);
  const set = new Set(selected);
  if (set.has(output)) {
    set.delete(output);
  } else {
    set.add(output);
    // Turning on what people said turns on the participants it belongs to.
    if (output === "participant_contributions") set.add("participants");
  }
  return normalizeOutputs([...set]);
}

/** What ONE linked record receives: the meeting's choice, limited to what
 *  that record's object allows. */
export function outputsForRecord(
  rec: LinkedRecord, selected: LinkOutput[], config: LinkConfig | null | undefined,
): LinkOutput[] {
  const obj = objectOf(config, rec.object);
  if (!obj) return [];
  return normalizeOutputs(selected.filter((o) => obj.outputs_allowed.includes(o)));
}

// ---------------------------------------------------------------------------
// Documents, one by one
// ---------------------------------------------------------------------------

/** The documents ticked when the sheet opens: the ones saved before (null
 *  there means "all of them", how meetings were linked before the choice
 *  existed), else all of them when Documents starts ticked. */
export function initialDocuments(
  docs: MeetingDocument[], stored: string[] | null | undefined, outputs: LinkOutput[],
): string[] {
  if (!outputs.includes("documents")) return [];
  if (stored == null) return docs.map((d) => d.type);
  return docs.filter((d) => stored.includes(d.type)).map((d) => d.type);
}

/** Tick or untick one document. Documents is on while any one is ticked.
 *  When the admin fixed Documents, every document follows it instead. */
export function toggleDocument(
  selected: LinkOutput[], docs: string[], type: string, options: OutputOption[],
): { outputs: LinkOutput[]; documents: string[] } {
  const opt = options.find((o) => o.output === "documents");
  if (!opt || !opt.offered || opt.locked || !opt.available) {
    return { outputs: normalizeOutputs(selected), documents: docs };
  }
  const next = docs.includes(type) ? docs.filter((d) => d !== type) : [...docs, type];
  const set = new Set(selected);
  if (next.length) set.add("documents"); else set.delete("documents");
  return { outputs: normalizeOutputs([...set]), documents: next };
}

/** The names of the chosen documents, for the review. */
export function documentNames(docs: MeetingDocument[], chosen: string[]): string[] {
  return docs.filter((d) => chosen.includes(d.type)).map((d) => d.label);
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

/** How a task's record reads on the sheet. */
export function taskTargetLabel(target: TaskTarget, links: LinkedRecord[]): {
  text: string; followsPrimary: boolean;
} {
  if (target) return { text: target.name, followsPrimary: false };
  const primary = primaryOf(links);
  return { text: primary ? `${primary.name} (main record)` : "The main record", followsPrimary: true };
}

/** After a link is removed, tasks that pointed at that record go back to
 *  following the primary. */
export function pruneTaskTargets(
  targets: Record<string, TaskTarget>, removedId: string,
): Record<string, TaskTarget> {
  const out: Record<string, TaskTarget> = {};
  for (const [taskId, t] of Object.entries(targets)) {
    out[taskId] = t && sameRecord(t.id, removedId) ? null : t;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Review and payload
// ---------------------------------------------------------------------------

export function reviewLines(args: {
  links: LinkedRecord[];
  selected: LinkOutput[];
  config: LinkConfig | null | undefined;
  taskCount: number;
  participantCount: number;
  /** The documents that will go, by name; omitted when not known. */
  documentNames?: string[];
}): { summary: string; perRecord: { name: string; label: string; outputs: string }[] } {
  const { links, selected, config, taskCount, participantCount, documentNames: docNames } = args;
  const label = (o: LinkOutput) => (o === "documents" && docNames?.length
    ? docNames.map((n) => `${n} (PDF)`).join(", ") : OUTPUT_LABELS[o]);
  const parts = ["1 meeting"];
  if (selected.includes("tasks") && taskCount) parts.push(`${taskCount} task${taskCount === 1 ? "" : "s"}`);
  if (selected.includes("mom")) parts.push("MoM PDF");
  if (selected.includes("participants") && participantCount) {
    parts.push(`${participantCount} participant${participantCount === 1 ? "" : "s"}`);
  }
  if (selected.includes("transcript")) parts.push("transcript");
  if (selected.includes("documents") && docNames?.length) {
    parts.push(docNames.length === 1 ? docNames[0] : `${docNames.length} documents`);
  }
  const perRecord = links.map((l) => {
    const outs = outputsForRecord(l, selected, config);
    return {
      name: l.primary ? `${l.name} (main)` : l.name,
      label: objectOf(config, l.object)?.label ?? l.object,
      outputs: outs.length ? outs.map(label).join(", ") : "Meeting details only",
    };
  });
  return { summary: parts.join(" · "), perRecord };
}

/** The choice as the backend stores it and Contract v3 sends it. */
export function toLinkPayload(
  links: LinkedRecord[], selected: LinkOutput[], targets: Record<string, TaskTarget>,
  /** The documents ticked one by one; omitted = all of them (the backend's
   *  default, and what happens when the meeting's documents couldn't load). */
  documents?: string[],
): {
  links: { object: string; id: string; name: string; primary: boolean }[];
  included_outputs: LinkOutput[];
  task_relations: Record<string, { object: string; id: string }>;
  included_documents?: string[];
} {
  const relations: Record<string, { object: string; id: string }> = {};
  for (const [taskId, t] of Object.entries(targets)) {
    if (t) relations[taskId] = { object: t.object, id: t.id };
  }
  const outputs = normalizeOutputs(selected);
  return {
    links: links.map((l) => ({ object: l.object, id: l.id, name: l.name, primary: l.primary })),
    included_outputs: outputs,
    task_relations: relations,
    ...(documents ? { included_documents: outputs.includes("documents") ? [...documents] : [] } : {}),
  };
}

/** One linkable object as the Salesforce settings screen describes it:
 *  "Property" / "Main record or linked · Sends overview, MoM, tasks…" and
 *  which of those are on by default. */
export function linkableObjectSummary(o: {
  label: string; can_be_primary: boolean; outputs_allowed: string[]; outputs_default_on: string[];
}): { title: string; role: string; sends: string; onByDefault: string } {
  const label = (x: string) => OUTPUT_LABELS[x as LinkOutput] ?? x;
  const allowed = LINK_OUTPUTS.filter((x) => o.outputs_allowed.includes(x));
  const defaults = allowed.filter((x) => o.outputs_default_on.includes(x));
  return {
    title: o.label,
    role: o.can_be_primary ? "Can be the main record" : "Can be linked, not the main record",
    sends: allowed.length ? allowed.map(label).join(", ") : "Meeting details only",
    onByDefault: defaults.length ? defaults.map(label).join(", ") : "Nothing",
  };
}
