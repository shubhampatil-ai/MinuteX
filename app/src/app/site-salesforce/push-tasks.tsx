// src/app/site-salesforce/push-tasks.tsx — push a meeting's EXISTING MinuteX
// tasks (the ones the AI extracted and the ones people added) to Salesforce.
//
//   (meeting pushed) -> Meeting tasks (tick) -> Salesforce User per task -> Push
//
// Nothing is typed in again: each Salesforce Task takes its subject,
// description, due date, status and priority from the MinuteX task itself
// (the backend reads the task), and is ATTACHED TO THE MEETING: Related To =
// the meeting's Salesforce record (from Push Meeting), Name = the meeting's
// Contact. So the screen blocks until the meeting is pushed, and shows
// exactly what each Task will be attached to. What the person chooses is who
// owns each Task in Salesforce.
// "Change related Salesforce record" is the advanced path: only then do the
// object and record pickers appear, and the pick replaces one side of the
// meeting's relationship for these Tasks. No Salesforce id or API name is
// ever shown. A task that is already in Salesforce is UPDATED when pushed
// again, never duplicated. Opened from the meeting's Salesforce section;
// `task` preselects one task.
import { useCallback, useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { FONT, R, S, useTheme, ColorScale } from "../../../lib/theme";
import { Button, Card, EmptyState, ErrorText, KeyboardAware, Loading } from "../../../lib/ui";
import { Icon } from "../../../lib/icons";
import {
  ApiTask, SitePerson, SiteSfMeetingRelation, SiteSfObject, SiteSfTask, getSiteMeetingRelation,
  getTasks, listSiteSalesforceTasks, pushTaskToSalesforce, searchSitePeople,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import {
  Flag, FlagRow, siteSfError, usePolling, useSiteSalesforceVersion,
} from "../../../lib/site-salesforce-ui";
import { ObjectPicker, RecordPicker, UserPicker } from "../../../lib/site-salesforce-pickers";
import {
  defaultPushSelection, isInFlight, pushProblem, pushedRowFor,
  salesforceDue, salesforceStateOf, siteErrorMessage, suggestSalesforceUser,
  taskRelationOf, taskTargetLines,
} from "../../../lib/site-salesforce-flow";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.x4 },
    meetingLabel: { fontFamily: FONT.medium, fontSize: 12, color: C.textFaint },
    meetingTitle: { fontFamily: FONT.bold, fontSize: 16, color: C.text, marginTop: 2 },
    step: { fontFamily: FONT.bold, fontSize: 13, color: C.text, marginTop: S.xl, marginBottom: S.sm },
    help: { fontFamily: FONT.regular, fontSize: 12, lineHeight: 17, color: C.textFaint, marginTop: 4 },
    picked: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary, marginBottom: S.sm },
    row: { flexDirection: "row", gap: S.md, paddingVertical: 12, borderBottomWidth: 1, borderBottomColor: C.border },
    box: { width: 22, height: 22, borderRadius: R.sm, borderWidth: 1.5, borderColor: C.border,
           alignItems: "center", justifyContent: "center", marginTop: 1 },
    boxOn: { backgroundColor: C.primary, borderColor: C.primary },
    title: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    meta: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 3 },
    owner: { fontFamily: FONT.medium, fontSize: 12.5, color: C.text, marginTop: 6 },
    change: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary, marginTop: 6 },
    err: { fontFamily: FONT.medium, fontSize: 12, color: C.danger, marginTop: 4 },
    problem: { fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim, marginTop: S.md },
    relation: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    link: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary, marginTop: S.sm },
  });
}

type Outcome = { ok: boolean; updated: boolean; message: string };

export default function PushTasksToSalesforce() {
  const params = useLocalSearchParams<{ meeting?: string; title?: string; workspace?: string; task?: string }>();
  const meetingKey = params.meeting ? String(params.meeting) : "";
  const only = params.task ? String(params.task) : "";
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId } = useWorkspace();
  const ws = params.workspace ? String(params.workspace) : activeId;

  const [tasks, setTasks] = useState<ApiTask[] | null>(null);
  const [pushed, setPushed] = useState<SiteSfTask[]>([]);
  const [relation, setRelation] = useState<SiteSfMeetingRelation | null>(null);
  const [people, setPeople] = useState<SitePerson[]>([]);
  const [error, setError] = useState("");

  const [selected, setSelected] = useState<string[] | null>(null);
  // "Change related Salesforce record" (advanced): off until tapped.
  const [changing, setChanging] = useState(false);
  const [obj, setObj] = useState<SiteSfObject | null>(null);
  const [record, setRecord] = useState<{ id: string; name: string } | null>(null);
  const [owners, setOwners] = useState<Record<string, SitePerson>>({});
  const [choosingFor, setChoosingFor] = useState("");
  const [pushing, setPushing] = useState(false);
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});

  const refreshPushed = useCallback(async () => {
    if (!ws || !meetingKey) return;
    try { setPushed(await listSiteSalesforceTasks(ws, meetingKey)); } catch { /* next tick */ }
  }, [ws, meetingKey]);

  // Synced Users and the meeting's relationship change elsewhere (a sync, a
  // tag, "Change Salesforce record" on the meeting): reload when they do.
  const version = useSiteSalesforceVersion("people", "relation", "connection");

  const load = useCallback(async () => {
    if (!ws || !meetingKey) return;
    try {
      const [ts, ps, rel, users] = await Promise.all([
        getTasks(meetingKey),
        listSiteSalesforceTasks(ws, meetingKey),
        getSiteMeetingRelation(ws, meetingKey).catch(() => null),
        searchSitePeople(ws, { type: "user", limit: 100 }).catch(() => [] as SitePerson[]),
      ]);
      setTasks(ts); setPushed(ps); setRelation(rel); setPeople(users);
      setSelected((cur) => cur ?? defaultPushSelection(ts, ps, only));
      setError("");
    } catch (e) {
      setError(siteSfError(e));
      setTasks((t) => t ?? []);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws, meetingKey, only, version]);

  const openChange = async () => {
    setChanging(true);
  };
  const cancelChange = () => { setChanging(false); setObj(null); setRecord(null); };
  useFocusEffect(useCallback(() => { void load(); }, [load]));
  usePolling(pushed.some((p) => isInFlight(p.sync_status)), () => { void refreshPushed(); });

  // The Salesforce User for a task: the person's pick, else a factual
  // suggestion (the User it went to before, or an exact email match).
  const suggestion = useCallback((t: ApiTask) => suggestSalesforceUser(t, people, pushed), [people, pushed]);
  const ownerFor = useCallback((t: ApiTask): { person: SitePerson | null; reason: string } => {
    if (owners[t.id]) return { person: owners[t.id], reason: "chosen" };
    const s = suggestion(t);
    const person = people.find((p) => p.person_key === s.personKey) || null;
    return { person, reason: person ? s.reason : "" };
  }, [owners, suggestion, people]);

  const toggle = (id: string) =>
    setSelected((cur) => (cur || []).includes(id) ? (cur || []).filter((x) => x !== id) : [...(cur || []), id]);

  const push = async () => {
    if (!tasks || !selected) return;
    const override = changing && obj && record ? { obj, record } : null;
    setPushing(true); setError("");
    const next: Record<string, Outcome> = {};
    // One at a time, in order: each call is idempotent (one Salesforce Task
    // per MinuteX task), so a retry after a partial failure is safe.
    for (const id of selected) {
      const t = tasks.find((x) => x.id === id);
      const owner = t ? ownerFor(t).person : null;
      if (!t || !owner) continue;
      try {
        const out = await pushTaskToSalesforce(ws, {
          minutex_task_id: id, assignee_person_key: owner.person_key,
          // Only for "Change related Salesforce record"; otherwise the backend
          // relates the Task to the meeting's relationship.
          ...(override ? { object_api_name: override.obj.api_name, record_id: override.record.id,
                           record_name: override.record.name } : {}),
        });
        next[id] = { ok: true, updated: out.updated, message: "" };
      } catch (e) {
        next[id] = { ok: false, updated: false, message: siteSfError(e) };
      }
      setOutcomes({ ...next });
    }
    setPushing(false);
    await refreshPushed();
  };

  // -------------------------------------------------------------------------
  if (!meetingKey) {
    return (
      <View style={[st.screen, st.body]}>
        <Stack.Screen options={{ title: "Push to Salesforce" }} />
        <EmptyState icon="doc.text" title="Open a meeting first"
          subtitle="Tasks are pushed from their meeting: open it and tap Push tasks to Salesforce."
          action={<Button label="Back" variant="secondary" onPress={() => router.back()} />} />
      </View>
    );
  }
  if (tasks === null || selected === null) return <Loading />;

  const inFlight = pushed.some((p) => isInFlight(p.sync_status));
  // Tasks are attached to the pushed meeting record; null until it is pushed.
  const meetingRecord = relation?.meeting_record ?? null;
  const problem = pushProblem({
    selected, meetingRelated: !!meetingRecord, meetingPushed: !!meetingRecord,
    objectApiName: changing ? obj?.api_name || "" : "", recordId: changing ? record?.id || "" : "",
    assigneeFor: (id) => { const t = tasks.find((x) => x.id === id); return t ? ownerFor(t).person?.person_key || "" : ""; },
  });
  // What each Task will be attached to, in words (Related To / Name).
  const target = taskTargetLines({
    meetingRecord,
    who: taskRelationOf(relation).relation?.who,
    override: changing && obj && record
      ? { relation: obj.task_relation, objectApiName: obj.api_name, name: record.name } : null,
  });

  return (
    <KeyboardAware style={st.screen}>
      <ScrollView style={st.screen} contentContainerStyle={st.body} keyboardShouldPersistTaps="handled">
        <Stack.Screen options={{ title: "Push to Salesforce" }} />
        <Card>
          <Text style={st.meetingLabel}>Meeting</Text>
          <Text style={st.meetingTitle}>{params.title ? String(params.title) : "This meeting"}</Text>
        </Card>
        {error ? <ErrorText>{error}</ErrorText> : null}

        {!meetingRecord ? (
          <Card style={{ marginTop: S.md }}>
            <Text style={st.relation}>Push the meeting first</Text>
            <Text style={st.help}>
              Salesforce Tasks are attached to this meeting&apos;s Salesforce record. Go back and tap
              Push Meeting; once it is in Salesforce, its tasks can be pushed here.
            </Text>
            <Button label="Back to meeting" variant="secondary" onPress={() => router.back()}
              style={{ marginTop: S.md }} />
          </Card>
        ) : (
          <Card style={{ marginTop: S.md }}>
            <Text style={st.meetingLabel}>Each Salesforce Task is attached to</Text>
            {target.map((l) => (
              <Text key={l.label} style={[st.relation, { marginTop: 4 }]} numberOfLines={2}>
                <Text style={st.help}>{l.label}: </Text>{l.name}
              </Text>
            ))}
            {!changing && !target.some((l) => l.label === "Name") ? (
              <Text style={st.help}>Tag a synced Salesforce Contact on this meeting to add them as the Task&apos;s Name.</Text>
            ) : null}
            {!changing ? (
              <Pressable onPress={() => { void openChange(); }} disabled={pushing}
                accessibilityRole="button" hitSlop={8}>
                <Text style={st.link}>Change related Salesforce record</Text>
              </Pressable>
            ) : (
              <>
                <Text style={st.help}>
                  A person (Contact, Lead) replaces the Name; any other record replaces the Related To
                  for these Tasks only.
                </Text>
                <Text style={st.step}>Salesforce object</Text>
                <ObjectPicker workspaceId={ws} value={obj} capability="task"
                  onChange={(o) => { setObj(o); setRecord(null); }} />
                {obj ? (
                  <>
                    <Text style={st.step}>{obj.label}</Text>
                    {record ? <Text style={st.picked}>Selected: {record.name}</Text> : null}
                    <RecordPicker workspaceId={ws} objectApiName={obj.api_name} objectLabel={obj.label}
                      value={record} onChange={setRecord} />
                  </>
                ) : null}
                <Pressable onPress={cancelChange} disabled={pushing} accessibilityRole="button" hitSlop={8}>
                  <Text style={st.link}>Use the meeting&apos;s Salesforce record</Text>
                </Pressable>
              </>
            )}
          </Card>
        )}

        {!meetingRecord ? null : tasks.length === 0 ? (
          <EmptyState icon="checklist" title="No tasks in this meeting"
            subtitle="Tasks the AI finds in the meeting, and tasks you add, appear here to push." />
        ) : (
          <>
            <Text style={st.step}>Tasks to push</Text>
            <Text style={st.help}>
              Each Salesforce Task gets the task&apos;s own title, description, due date, status and
              priority. A task already in Salesforce is updated, not duplicated.
            </Text>
            {tasks.map((t) => {
              const on = selected.includes(t.id);
              const state = salesforceStateOf(t.id, pushed);
              const due = salesforceDue(t);
              const owner = ownerFor(t);
              const outcome = outcomes[t.id];
              const row = pushedRowFor(t.id, pushed);
              return (
                <View key={t.id} style={st.row}>
                  <Pressable onPress={() => toggle(t.id)} disabled={pushing}
                    accessibilityRole="checkbox" accessibilityState={{ checked: on }}
                    accessibilityLabel={t.title || t.task}
                    style={[st.box, on && st.boxOn]} hitSlop={10}>
                    {on ? <Icon name="checkmark" size={13} tintColor={C.bg} /> : null}
                  </Pressable>
                  <View style={{ flex: 1 }}>
                    <Text style={st.title}>{t.title || t.task}</Text>
                    <Text style={st.meta}>
                      {t.from_action_item ? "From the meeting (AI)" : "Added in MinuteX"}
                      {t.assignee?.name ? ` · ${t.assignee.name}` : ""}
                      {due.date ? ` · due ${due.date}` : due.spoken ? ` · “${due.spoken}”` : ""}
                      {` · ${t.status}`}
                    </Text>
                    <FlagRow>
                      <Flag label={state.label} on={state.key !== "none"}
                        tone={state.key === "failed" ? "warn" : state.key === "sending" ? "info" : undefined} />
                    </FlagRow>
                    {state.key === "synced" && row ? (
                      <Text style={st.meta} numberOfLines={2}>
                        {outcome?.updated ? "Updated in Salesforce" : "In Salesforce"}
                        {(row.related_to || []).length
                          ? ` · ${(row.related_to || []).map((r) => r.name).filter(Boolean).join(" · ")}`
                          : row.related_record_name ? ` · ${row.related_record_name}` : ""}
                      </Text>
                    ) : null}
                    {state.key === "failed" && row?.last_error ? (
                      <Text style={st.err}>{siteErrorMessage(row.last_error.code, row.last_error.message)}</Text>
                    ) : null}
                    {outcome && !outcome.ok ? <Text style={st.err}>{outcome.message}</Text> : null}
                    {on ? (
                      <>
                        <Text style={st.owner}>
                          Salesforce User: {owner.person ? owner.person.name : "not chosen"}
                          {owner.reason === "email" ? " (matched by email)"
                            : owner.reason === "previous" ? " (as last pushed)" : ""}
                        </Text>
                        <Pressable onPress={() => setChoosingFor(choosingFor === t.id ? "" : t.id)} disabled={pushing}>
                          <Text style={st.change}>{choosingFor === t.id ? "Done" : owner.person ? "Change" : "Choose"}</Text>
                        </Pressable>
                        {choosingFor === t.id ? (
                          <UserPicker workspaceId={ws} value={owner.person}
                            onChange={(p) => {
                              setOwners((cur) => { const n = { ...cur }; if (p) n[t.id] = p; else delete n[t.id]; return n; });
                              if (p) setChoosingFor("");
                            }} />
                        ) : null}
                      </>
                    ) : null}
                  </View>
                </View>
              );
            })}

            {problem ? <Text style={st.problem}>{problem}</Text> : null}
            <Button
              label={pushing ? "Pushing…" : `Push ${selected.length} task${selected.length === 1 ? "" : "s"} to Salesforce`}
              onPress={() => { void push(); }} loading={pushing}
              disabled={!!problem || pushing || inFlight} style={{ marginTop: S.xl }} />
            {inFlight ? <Text style={st.help}>Waiting for Salesforce to confirm the last push…</Text> : null}
            {Object.keys(outcomes).length && !pushing && !inFlight ? (
              <Button label="Back to meeting" variant="secondary" onPress={() => router.back()}
                style={{ marginTop: S.sm }} />
            ) : null}
          </>
        )}
      </ScrollView>
    </KeyboardAware>
  );
}
