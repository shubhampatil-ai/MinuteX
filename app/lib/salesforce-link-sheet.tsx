// lib/salesforce-link-sheet.tsx — the "Link to Salesforce" sheet (Contract v3).
//
// Three short steps, one decision each, with Back / Next always at the bottom:
//   1 Records   search the customer's Salesforce and add up to the limit; one
//               is the MAIN record ("Make main"). With a limit of one, it's a
//               single record picker: picking another record replaces it.
//   2 Send      switches grouped as Meeting / People / Tasks, with the
//               admin's defaults and locks ("Set by your admin"); tasks follow
//               the main record unless chosen per task.
//   3 Review    what each record gets, then Link.
//
// All decisions come from lib/salesforce-link-flow.ts; this file only
// renders them. It owns no data: the caller passes the configuration, the
// meeting's tasks, a record search and what to do on Link, so the sheet is
// the same whichever backend route feeds it.
//
// BEHIND A FLAG: off unless the build sets EXPO_PUBLIC_SALESFORCE_LINK=1.
// Nothing mounts it until the backend that stores links exists (step 4).
import { useEffect, useMemo, useState } from "react";
import { ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Switch, Text, View } from "react-native";
import { Icon } from "./icons";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { Button, KeyboardAwareSheet, SearchBar } from "./ui";
import {
  LinkConfig, LinkOutput, LinkedRecord, MeetingOutputs, OutputOption, TaskTarget, addLink,
  documentNames, initialDocuments, initialOutputs, linkProblem, maxLinks, objectOf,
  outputOptions, pruneTaskTargets, removeLink, reviewLines, sameRecord, sendableOutputs,
  setPrimary, singleRecord, taskTargetLabel, toLinkPayload, toggleDocument, toggleOutput,
} from "./salesforce-link-flow";

export const SALESFORCE_LINK_ENABLED = process.env.EXPO_PUBLIC_SALESFORCE_LINK === "1";

/** One search result: a record the user may pick. Never shows its id. */
export type LinkSearchHit = { object: string; id: string; name: string; secondary?: string };

export type LinkTask = { id: string; title: string; assignee?: string; due?: string };

export type LinkSubmit = ReturnType<typeof toLinkPayload>;

const SEARCH_DEBOUNCE_MS = 350;
const MIN_SEARCH_CHARS = 2;

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl,
      paddingHorizontal: 20, paddingTop: S.lg, paddingBottom: S.xl, maxHeight: "92%", flexShrink: 1,
    },
    head: { flexDirection: "row", alignItems: "center", justifyContent: "space-between",
            marginBottom: S.sm },
    title: { fontFamily: FONT.bold, fontSize: 18, color: C.text },
    section: { fontFamily: FONT.bold, fontSize: 11.5, letterSpacing: 0.6, color: C.textDim,
               marginTop: S.lg, marginBottom: S.sm },
    note: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim },
    err: { fontFamily: FONT.medium, fontSize: 13, color: C.danger, marginTop: S.sm },
    hit: { borderWidth: 1, borderColor: C.border, borderRadius: R.md, paddingHorizontal: S.md,
           paddingVertical: 10, marginBottom: S.sm, minHeight: 48, justifyContent: "center" },
    name: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    sub: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
    optLabel: { fontFamily: FONT.medium, fontSize: 14.5, color: C.text },
    optNote: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim },
    task: { borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.border, paddingVertical: 10 },
    taskRel: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary, marginTop: 4 },
    taskRelChanged: { color: C.accent },
    choice: { paddingVertical: 8, paddingHorizontal: S.md, borderRadius: R.md, marginTop: 4,
              borderWidth: 1, borderColor: C.border },
    choicePicked: { borderColor: C.primary, backgroundColor: C.primarySoft },
    review: { backgroundColor: C.surface, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
              padding: S.md, gap: S.md },
    reviewRecord: { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.border, paddingTop: S.md },
    steps: { flexDirection: "row", gap: 6, marginTop: S.xs },
    stepBar: { flex: 1, height: 4, borderRadius: 2, backgroundColor: C.surface2 },
    stepBarOn: { backgroundColor: C.primary },
    stepLabel: { fontFamily: FONT.semibold, fontSize: 12, color: C.textDim, marginTop: S.md },
    stepTitle: { fontFamily: FONT.bold, fontSize: 20, color: C.text, marginTop: 2, marginBottom: S.sm },
    help: { fontFamily: FONT.regular, fontSize: 14, lineHeight: 20, color: C.textDim },
    addText: { fontFamily: FONT.semibold, fontSize: 14, color: C.primary },
    recordCard: { borderWidth: 1, borderColor: C.border, borderRadius: R.lg, padding: S.md, marginBottom: S.sm,
                  backgroundColor: C.surface },
    recordCardMain: { borderColor: C.primary, backgroundColor: C.primarySoft },
    recordTop: { flexDirection: "row", alignItems: "flex-start", gap: S.sm },
    mainPill: { fontFamily: FONT.bold, fontSize: 11, letterSpacing: 0.6, color: C.textOnPrimary,
                backgroundColor: C.primary, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 3,
                overflow: "hidden" },
    recordActions: { flexDirection: "row", justifyContent: "flex-end", alignItems: "center", gap: S.lg,
                     marginTop: S.sm },
    action: { fontFamily: FONT.semibold, fontSize: 14, color: C.primary, paddingVertical: 4 },
    group: { backgroundColor: C.surface, borderRadius: R.lg, borderWidth: 1, borderColor: C.border,
             paddingHorizontal: S.md, paddingVertical: S.sm, marginTop: S.md },
    groupTitle: { fontFamily: FONT.bold, fontSize: 13, color: C.text, marginTop: S.xs, marginBottom: 2 },
    switchRow: { flexDirection: "row", alignItems: "center", gap: S.md, paddingVertical: S.sm, minHeight: 52 },
    taskSummary: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: S.md,
                   paddingVertical: S.sm, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.border },
    footer: { flexDirection: "row", gap: S.sm, paddingTop: S.md, borderTopWidth: StyleSheet.hairlineWidth,
              borderTopColor: C.border, marginTop: S.sm },
    reviewLine: { fontFamily: FONT.regular, fontSize: 13, color: C.textDim },
    reviewStrong: { fontFamily: FONT.semibold, color: C.text },
  });
}

/** Debounced record search, shared by the Records part and a task's chooser. */
function useRecordSearch(term: string, enabled: boolean,
                         search: (q: string) => Promise<LinkSearchHit[]>) {
  const [hits, setHits] = useState<LinkSearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const q = term.trim();
    if (!enabled || q.length < MIN_SEARCH_CHARS) { setHits([]); setError(""); return undefined; }
    let live = true;
    const t = setTimeout(() => {
      setLoading(true);
      search(q)
        .then((r) => { if (live) { setHits(r); setError(""); } })
        // The server's own reason (e.g. "Salesforce isn't answering right
        // now"), so a failure says what to fix; the generic line is a fallback.
        .catch((e) => {
          if (!live) return;
          setHits([]);
          const msg = e instanceof Error && e.message ? e.message : "";
          setError(msg ? `Couldn't search Salesforce: ${msg}` : "Couldn't search Salesforce. Try again.");
        })
        .finally(() => { if (live) setLoading(false); });
    }, SEARCH_DEBOUNCE_MS);
    return () => { live = false; clearTimeout(t); };
  }, [term, enabled, search]);
  return { hits, loading, error };
}

/** One output as a switch. One the meeting doesn't have yet can't be turned
 *  on and says how to make it; one the admin fixed can't be changed. */
function OutputSwitch({ st, C, option, label, on, onChange }: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  option: OutputOption;
  label: string;
  on: boolean;
  onChange: () => void;
}) {
  const disabled = option.locked || !option.available;
  const notes = [option.note, option.locked ? "Set by your admin" : "", option.missing].filter(Boolean);
  return (
    <View style={st.switchRow}>
      <View style={{ flex: 1 }}>
        <Text style={[st.optLabel, !option.available && { color: C.textDim }]}>{label}</Text>
        {notes.length ? <Text style={st.optNote}>{notes.join(" · ")}</Text> : null}
      </View>
      <Switch value={on} onValueChange={onChange} disabled={disabled}
        trackColor={{ true: C.primary, false: C.surface2 }}
        accessibilityLabel={[label, ...notes].join(", ")} />
    </View>
  );
}

export function SalesforceLinkSheet({
  visible, onClose, config, tasks, participantCount, has = null, initial, searchRecords, onSubmit,
  submitLabel = "Link meeting",
}: {
  visible: boolean;
  onClose: () => void;
  config: LinkConfig | null;
  tasks: LinkTask[];
  participantCount: number;
  /** Whether the meeting has a MoM, and its documents by name. What it
   *  lacks can't be ticked; null when that couldn't be loaded. */
  has?: MeetingOutputs | null;
  /** The meeting's current links, when it was linked before. `documents`
   *  null means every document (how meetings were linked before). */
  initial?: {
    links: LinkedRecord[]; outputs: LinkOutput[]; targets: Record<string, TaskTarget>;
    documents?: string[] | null;
  };
  searchRecords: (q: string) => Promise<LinkSearchHit[]>;
  /** Saves the choice. Throw an Error with a readable message to show it. */
  onSubmit: (payload: LinkSubmit) => Promise<void>;
  submitLabel?: string;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);

  const [links, setLinks] = useState<LinkedRecord[]>([]);
  const [outputs, setOutputs] = useState<LinkOutput[]>([]);
  const [outputsTouched, setOutputsTouched] = useState(false);
  const [docs, setDocs] = useState<string[]>([]);
  const [targets, setTargets] = useState<Record<string, TaskTarget>>({});
  const [query, setQuery] = useState("");
  const [addError, setAddError] = useState("");
  const [openTask, setOpenTask] = useState<string | null>(null);
  const [taskQuery, setTaskQuery] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [step, setStep] = useState(1);
  const [showTasks, setShowTasks] = useState(false);

  // Each open starts from the meeting's current links, or empty.
  useEffect(() => {
    if (!visible) return;
    setLinks(initial?.links ?? []);
    setOutputs(initial?.outputs ?? []);
    setDocs(initialDocuments(has?.documents ?? [], initial?.documents, initial?.outputs ?? []));
    setOutputsTouched(!!initial);
    setTargets(initial?.targets ?? {});
    setQuery(""); setAddError(""); setOpenTask(null); setTaskQuery(""); setSaveError("");
    setStep(1); setShowTasks(false);
  }, [visible, initial, has]);

  const options = useMemo(() => outputOptions(links, config, has), [links, config, has]);
  // Until the user changes an output, the ticks follow the main record's
  // defaults, so picking a different main record re-applies its defaults.
  useEffect(() => {
    if (outputsTouched) return;
    const next = initialOutputs(options);
    setOutputs(next);
    setDocs(initialDocuments(has?.documents ?? [], null, next));
  }, [options, outputsTouched, has]);
  // What the meeting lacks never goes, even if it was ticked before.
  const chosen = useMemo(() => sendableOutputs(outputs, options), [outputs, options]);
  const docOption = options.find((o) => o.output === "documents");
  // Documents are ticked one by one, by their own names, once they're known
  // and the admin hasn't fixed them; otherwise it's the one Documents row.
  const docsOneByOne = !!has && has.documents.length > 0 && !docOption?.locked;
  const docNames = has
    ? documentNames(has.documents, docsOneByOne ? docs : has.documents.map((d) => d.type))
    : undefined;

  const single = singleRecord(config);
  const max = maxLinks(config);
  const full = !single && links.length >= max;
  const recordSearch = useRecordSearch(query, visible && !full, searchRecords);
  const taskSearch = useRecordSearch(taskQuery, visible && !!openTask, searchRecords);

  const linkableHits = recordSearch.hits.filter((h) => !!objectOf(config, h.object));
  const problem = linkProblem(links, config);
  const review = reviewLines({
    links, selected: chosen, config, taskCount: tasks.length, participantCount,
    documentNames: docNames,
  });

  function pick(hit: LinkSearchHit) {
    const res = addLink(links, hit, config);
    setAddError(res.error ?? "");
    if (res.error) return;
    if (single && links[0] && !sameRecord(links[0].id, hit.id)) {
      setTargets((t) => pruneTaskTargets(t, links[0].id));
    }
    setLinks(res.links);
    setQuery("");
  }

  function unlink(id: string) {
    setLinks((l) => removeLink(l, id, config));
    setTargets((t) => pruneTaskTargets(t, id));
  }

  function chooseTarget(taskId: string, target: TaskTarget) {
    setTargets((t) => ({ ...t, [taskId]: target }));
    setOpenTask(null);
    setTaskQuery("");
  }

  async function submit() {
    if (problem || saving) return;
    setSaving(true);
    setSaveError("");
    try {
      await onSubmit(toLinkPayload(links, chosen, targets, docsOneByOne ? docs : undefined));
      onClose();
    } catch (e) {
      setSaveError(e instanceof Error && e.message ? e.message : "Couldn't link the meeting. Try again.");
    } finally {
      setSaving(false);
    }
  }

  const notAllowed = options.filter((o) => !o.offered && links.length);
  const primary = links.find((l) => l.primary) ?? null;
  const movedTasks = tasks.filter((t) => !!targets[t.id]).length;
  const canNext = step === 1 ? links.length > 0 && !problem : true;
  const STEP_TITLES = ["Which records?", "What to send?", "Check and link"];

  // One switch per output; documents one by one when their names are known.
  function outputRows(list: LinkOutput[]) {
    return options.filter((o) => o.offered && list.includes(o.output)).flatMap((o) => (
      o.output === "documents" && docsOneByOne && has
        ? has.documents.map((d) => (
          <OutputSwitch key={`doc:${d.type}`} st={st} C={C} option={o}
            label={`${d.label} (PDF)`} on={docs.includes(d.type)}
            onChange={() => {
              const next = toggleDocument(outputs, docs, d.type, options);
              setOutputs(next.outputs); setDocs(next.documents); setOutputsTouched(true);
            }} />
        ))
        : [<OutputSwitch key={o.output} st={st} C={C} option={o} label={o.label}
            on={chosen.includes(o.output)}
            onChange={() => { setOutputs((x) => toggleOutput(x, o.output, options)); setOutputsTouched(true); }} />]
    ));
  }

  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      <KeyboardAwareSheet>
        <View style={st.backdrop}>
          <View style={st.sheet}>
            <View style={st.head}>
              <Text style={st.title}>Link to Salesforce</Text>
              <Pressable onPress={onClose} hitSlop={12} accessibilityRole="button" accessibilityLabel="Close">
                <Icon name="xmark" size={20} tintColor={C.textFaint} />
              </Pressable>
            </View>
            {/* Where you are: three short steps. */}
            <View style={st.steps} accessibilityRole="progressbar"
              accessibilityLabel={`Step ${step} of 3: ${STEP_TITLES[step - 1]}`}>
              {[1, 2, 3].map((n) => (
                <View key={n} style={[st.stepBar, n <= step && st.stepBarOn]} />
              ))}
            </View>
            <Text style={st.stepLabel}>Step {step} of 3</Text>
            <Text style={st.stepTitle}>{STEP_TITLES[step - 1]}</Text>

            <ScrollView style={{ flexShrink: 1, minHeight: 0 }} keyboardShouldPersistTaps="handled">
              {step === 1 ? (
                <>
                  {/* ---------------- 1. Records ---------------- */}
                  <Text style={st.help}>
                    {single
                      ? "Search your Salesforce for the record this meeting is about."
                      : "Search your Salesforce for the records this meeting is about. The main record gets the tasks unless you choose otherwise."}
                  </Text>
                  {!full ? (
                    <SearchBar value={query} onChangeText={setQuery}
                      placeholder={single && links.length ? "Search to change the record…" : "Search Salesforce…"}
                      style={{ marginTop: S.md }} />
                  ) : null}
                  {recordSearch.loading ? <ActivityIndicator style={{ marginTop: S.sm }} /> : null}
                  {recordSearch.error ? <Text style={st.err}>{recordSearch.error}</Text> : null}
                  {addError ? <Text style={st.err}>{addError}</Text> : null}
                  {!full ? (
                    <View style={{ marginTop: S.sm }}>
                      {linkableHits.map((h) => (
                        <Pressable key={`${h.object}:${h.id}`} style={st.hit} onPress={() => pick(h)}
                          accessibilityRole="button"
                          accessibilityLabel={`Add ${h.name}, ${objectOf(config, h.object)?.label ?? h.object}`}>
                          <View style={{ flex: 1 }}>
                            <Text style={st.name} numberOfLines={2}>{h.name}</Text>
                            <Text style={st.sub}>
                              {objectOf(config, h.object)?.label ?? h.object}{h.secondary ? ` · ${h.secondary}` : ""}
                            </Text>
                          </View>
                          <Text style={st.addText}>Add</Text>
                        </Pressable>
                      ))}
                      {query.trim().length >= MIN_SEARCH_CHARS && !recordSearch.loading
                        && !recordSearch.error && linkableHits.length === 0 ? (
                          <Text style={st.note}>No records match “{query.trim()}”.</Text>
                        ) : null}
                    </View>
                  ) : (
                    <Text style={[st.note, { marginTop: S.sm }]}>That&apos;s the most records one meeting can be linked to.</Text>
                  )}

                  <Text style={st.section}>
                    {single ? "RECORD" : `RECORDS · ${links.length} OF ${max}`}
                  </Text>
                  {links.length === 0 ? (
                    <Text style={st.note}>
                      Search for the {single ? "record" : "records"} this meeting is about.
                    </Text>
                  ) : null}
                  {links.map((l) => {
                    const label = objectOf(config, l.object)?.label ?? l.object;
                    const canMain = !!objectOf(config, l.object)?.can_be_primary;
                    return (
                      <View key={l.id} style={[st.recordCard, l.primary && !single && st.recordCardMain]}>
                        <View style={st.recordTop}>
                          <View style={{ flex: 1 }}>
                            <Text style={st.name} numberOfLines={2}>{l.name}</Text>
                            <Text style={st.sub}>{label}{l.secondary ? ` · ${l.secondary}` : ""}</Text>
                          </View>
                          {!single && l.primary ? <Text style={st.mainPill}>MAIN</Text> : null}
                        </View>
                        <View style={st.recordActions}>
                          {single ? null : (
                            !l.primary && canMain ? (
                              <Pressable onPress={() => setLinks((x) => setPrimary(x, l.id, config))}
                                hitSlop={8} accessibilityRole="button" accessibilityLabel={`Make ${l.name} the main record`}>
                                <Text style={st.action}>Make main</Text>
                              </Pressable>
                            ) : !l.primary ? (
                              <Text style={st.sub}>{label} can&apos;t be the main record</Text>
                            ) : null
                          )}
                          <Pressable onPress={() => unlink(l.id)} hitSlop={8} accessibilityRole="button"
                            accessibilityLabel={`Remove ${l.name}`}>
                            <Text style={[st.action, { color: C.danger }]}>Remove</Text>
                          </Pressable>
                        </View>
                      </View>
                    );
                  })}
                  {links.length && problem ? <Text style={st.err}>{problem}</Text> : null}
                </>
              ) : null}

              {step === 2 ? (
                <>
                  {/* ---------------- 2. What to send ---------------- */}
                  <Text style={st.help}>
                    Choose what goes to Salesforce with this meeting. Your admin may have fixed some of these.
                  </Text>
                  {[
                    { title: "Meeting", list: ["overview", "mom", "documents", "transcript", "recording"] as LinkOutput[] },
                    { title: "People", list: ["participants", "participant_contributions"] as LinkOutput[] },
                    { title: "Tasks", list: ["tasks"] as LinkOutput[] },
                  ].map((g) => {
                    const rows = outputRows(g.list);
                    if (!rows.length) return null;
                    return (
                      <View key={g.title} style={st.group}>
                        <Text style={st.groupTitle}>{g.title}</Text>
                        {rows}
                        {g.title === "Tasks" && chosen.includes("tasks") && tasks.length ? (
                          <View style={st.taskSummary}>
                            <Text style={st.sub}>
                              {movedTasks === 0
                                ? `All ${tasks.length} ${tasks.length === 1 ? "task goes" : "tasks go"} to ${primary?.name ?? "the main record"}.`
                                : `${tasks.length - movedTasks} to ${primary?.name ?? "the main record"}, ${movedTasks} to other records.`}
                            </Text>
                            <Pressable onPress={() => setShowTasks((v) => !v)} hitSlop={8} accessibilityRole="button">
                              <Text style={st.action}>{showTasks ? "Hide tasks" : "Choose per task"}</Text>
                            </Pressable>
                          </View>
                        ) : null}
                        {g.title === "Tasks" && showTasks && chosen.includes("tasks") ? tasks.map((t) => {
                          const target = targets[t.id] ?? null;
                          const rel = taskTargetLabel(target, links);
                          const open = openTask === t.id;
                          return (
                            <View key={t.id} style={st.task}>
                              <Pressable onPress={() => { setOpenTask(open ? null : t.id); setTaskQuery(""); }}
                                accessibilityRole="button"
                                accessibilityLabel={`${t.title}, goes to ${rel.text}. Change`}>
                                <Text style={st.name} numberOfLines={2}>{t.title}</Text>
                                {t.assignee || t.due ? (
                                  <Text style={st.sub}>{[t.assignee, t.due].filter(Boolean).join(" · ")}</Text>
                                ) : null}
                                <Text style={[st.taskRel, !rel.followsPrimary && st.taskRelChanged]}>
                                  {rel.text} ›
                                </Text>
                              </Pressable>
                              {open ? (
                                <View style={{ marginTop: S.sm }}>
                                  <Pressable style={[st.choice, !target && st.choicePicked]}
                                    onPress={() => chooseTarget(t.id, null)} accessibilityRole="button">
                                    <Text style={st.name}>Same as the main record</Text>
                                  </Pressable>
                                  {links.filter((l) => !l.primary).map((l) => (
                                    <Pressable key={l.id}
                                      style={[st.choice, !!target && sameRecord(target.id, l.id) && st.choicePicked]}
                                      onPress={() => chooseTarget(t.id, { object: l.object, id: l.id, name: l.name })}
                                      accessibilityRole="button">
                                      <Text style={st.name}>{l.name}</Text>
                                      <Text style={st.sub}>{objectOf(config, l.object)?.label ?? l.object} · linked</Text>
                                    </Pressable>
                                  ))}
                                  <SearchBar value={taskQuery} onChangeText={setTaskQuery}
                                    placeholder="Search another record…" style={{ marginTop: S.sm }} />
                                  {taskSearch.loading ? <ActivityIndicator style={{ marginTop: S.sm }} /> : null}
                                  {taskSearch.error ? <Text style={st.err}>{taskSearch.error}</Text> : null}
                                  {taskSearch.hits.map((h) => (
                                    <Pressable key={`${h.object}:${h.id}`} style={st.choice}
                                      onPress={() => chooseTarget(t.id, { object: h.object, id: h.id, name: h.name })}
                                      accessibilityRole="button">
                                      <Text style={st.name}>{h.name}</Text>
                                      <Text style={st.sub}>{h.secondary || h.object}</Text>
                                    </Pressable>
                                  ))}
                                </View>
                              ) : null}
                            </View>
                          );
                        }) : null}
                      </View>
                    );
                  })}
                  {notAllowed.length ? (
                    <Text style={st.note}>
                      Not allowed for {links.length === 1 ? "this record" : "these records"}:{" "}
                      {notAllowed.map((o) => o.label).join(", ")}.
                    </Text>
                  ) : null}
                </>
              ) : null}

              {step === 3 ? (
                <>
                  {/* ---------------- 3. Review ---------------- */}
                  <View style={st.review}>
                    <Text style={[st.reviewLine, st.reviewStrong]}>{review.summary}</Text>
                    {review.perRecord.map((r) => (
                      <View key={r.name} style={st.reviewRecord}>
                        <Text style={st.name}>{r.name}</Text>
                        <Text style={st.sub}>{r.label}</Text>
                        <Text style={[st.reviewLine, { marginTop: 4 }]}>{r.outputs}</Text>
                      </View>
                    ))}
                  </View>
                  {saveError ? <Text style={st.err}>{saveError}</Text> : null}
                </>
              ) : null}
            </ScrollView>

            {/* The way forward, always in the same place. */}
            <View style={st.footer}>
              {step > 1 ? (
                <Button label="Back" variant="secondary" onPress={() => setStep((n) => n - 1)}
                  style={{ flex: 1 }} />
              ) : null}
              {step < 3 ? (
                <Button label="Next" onPress={() => setStep((n) => n + 1)} disabled={!canNext}
                  style={{ flex: 2 }} />
              ) : (
                <Button label={submitLabel} onPress={() => { void submit(); }} loading={saving}
                  disabled={!!problem} style={{ flex: 2 }} />
              )}
            </View>
          </View>
        </View>
      </KeyboardAwareSheet>
    </Modal>
  );
}
