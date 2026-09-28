// lib/site-salesforce-meeting.tsx — the meeting screen's "Salesforce" section
// (delivery_type = salesforce_site), rendered by recording/[key]/index.tsx
// only when SITE_SALESFORCE_ENABLED.
//
// WHAT THE MEETING IS ABOUT comes first, in words: "Salesforce: Asha Rao".
// It is AUTOMATIC — tagging a speaker with a synced Salesforce Contact makes
// that Contact the meeting's relationship (the backend uses the Contact's
// stored Salesforce id; nobody picks an object, an API name or an id).
// "Link to a Salesforce record" is the advanced path: only after tapping it does
// the person see the tagged-Contact choices and the object/record pickers
// (Account, Opportunity, a custom object...). Ids and API names never show.
//
// Two SEPARATE actions, in this order:
//   Push Meeting   this meeting -> a MinuteX Meeting record in Salesforce
//                  (transcript, audio link, participants, summary, action
//                  items, and the relationship above). Once pushed, the card
//                  says WHERE: "In Salesforce: MinuteX Meeting · <title>,
//                  related to Asha Rao · Acme", with Open in Salesforce.
//   Push tasks     this meeting's EXISTING tasks -> Salesforce Tasks attached
//                  to THAT meeting record (Related To) and the meeting's
//                  Contact (Name) — so it stays disabled until the meeting is
//                  pushed (site-salesforce/push-tasks, with the meeting key)
// plus the tasks already in Salesforce (tap to push again = update).
import { useCallback, useMemo, useState } from "react";
import { Linking, Pressable, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { Button } from "./ui";
import {
  SiteJob, SiteSfConnection, SiteSfMeetingRecord, SiteSfMeetingRelation, SiteSfObject, SiteSfTask,
  getSiteMeetingDelivery, getSiteMeetingRelation, getSiteSalesforce,
  listSiteSalesforceTasks, pushSiteMeeting, retrySiteMeeting, setSiteMeetingRelation,
} from "./api";
import { Flag, FlagRow, siteSfError, syncLabel, usePolling, useSiteSalesforceVersion } from "./site-salesforce-ui";
import {
  hasRelation, isInFlight, meetingPushLine, meetingRecordLine, relationLines, relationSourceNote,
  relationSummary, taskRelationOf, taskTargetLines,
} from "./site-salesforce-flow";
import { invalidateSiteSalesforce } from "./site-salesforce-events";
import { ObjectPicker, RecordPicker } from "./site-salesforce-pickers";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    card: { borderWidth: 1, borderColor: C.border, borderRadius: R.card, padding: S.lg, marginTop: S.xl },
    title: { fontFamily: FONT.bold, fontSize: 15, color: C.text },
    sub: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim, marginTop: 4 },
    line: { fontFamily: FONT.medium, fontSize: 12.5, marginTop: S.sm },
    err: { fontFamily: FONT.medium, fontSize: 12.5, color: C.danger, marginTop: S.sm },
    relation: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text, marginTop: S.sm, flexShrink: 1 },
    relationLabel: { fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim },
    sideRow: { flexDirection: "row", flexWrap: "wrap", marginTop: 2 },
    link: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary, marginTop: S.sm },
    advanced: { borderTopWidth: 1, borderTopColor: C.border, marginTop: S.md, paddingTop: S.md },
    step: { fontFamily: FONT.bold, fontSize: 12.5, color: C.text, marginTop: S.md, marginBottom: 4 },
    candidate: { paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: C.border },
    candidateOn: { backgroundColor: C.primarySoft },
    candidateName: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    candidateSub: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
    tasksHead: { fontFamily: FONT.bold, fontSize: 13, color: C.text, marginTop: S.lg },
    placed: { backgroundColor: C.surface, borderRadius: R.md, padding: S.md, marginTop: S.sm },
    placedHead: { fontFamily: FONT.bold, fontSize: 11.5, letterSpacing: 0.4, color: C.textFaint },
    placedName: { fontFamily: FONT.semibold, fontSize: 14, color: C.text, marginTop: 4 },
    placedMeta: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 2 },
    taskRow: { paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.border, marginTop: S.sm },
    taskTitle: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    taskMeta: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
  });
}

export function SiteSalesforceMeetingBlock({
  meetingKey, workspaceId, title,
}: {
  meetingKey: string;
  workspaceId: string;
  title: string;
}) {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [conn, setConn] = useState<SiteSfConnection | null>(null);
  const [endpointEnabled, setEndpointEnabled] = useState(false);
  const [job, setJob] = useState<SiteJob | null>(null);
  const [tasks, setTasks] = useState<SiteSfTask[]>([]);
  const [relation, setRelation] = useState<SiteSfMeetingRelation | null>(null);
  // Where Push Meeting put the meeting in Salesforce (null until pushed).
  const [meetingRecord, setMeetingRecord] = useState<SiteSfMeetingRecord | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // "Change Salesforce record" (advanced) state.
  const [changing, setChanging] = useState(false);
  const [obj, setObj] = useState<SiteSfObject | null>(null);
  const [saving, setSaving] = useState(false);
  const [relationError, setRelationError] = useState("");

  // Reload the moment People, objects, the connection or this relationship
  // change anywhere (a sync finishing, a tag on the People tab).
  const version = useSiteSalesforceVersion("people", "objects", "connection", "relation");

  const load = useCallback(async () => {
    if (!workspaceId || !meetingKey) return;
    try {
      const [c, d] = await Promise.all([
        getSiteSalesforce(workspaceId),
        getSiteMeetingDelivery(meetingKey),
      ]);
      setConn(c);
      setEndpointEnabled(d.endpoint.enabled);
      setJob(d.job);
      setMeetingRecord(d.meeting_record ?? null);
      if (c.connected) {
        const [ts, rel] = await Promise.all([
          listSiteSalesforceTasks(workspaceId, meetingKey).catch(() => [] as SiteSfTask[]),
          getSiteMeetingRelation(workspaceId, meetingKey).catch(() => null),
        ]);
        setTasks(ts);
        setRelation(rel);
      } else {
        setTasks([]);
        setRelation(null);
      }
      setError("");
    } catch (e) {
      setError(siteSfError(e));
    } finally {
      setLoaded(true);
    }
    // `version` is a dependency on purpose: an invalidation reloads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, meetingKey, version]);
  useFocusEffect(useCallback(() => { void load(); }, [load]));
  usePolling(isInFlight(job?.status) || tasks.some((t) => isInFlight(t.sync_status)),
    () => { void load(); });

  const push = async () => {
    setBusy(true); setError("");
    try {
      setJob(job?.status === "FAILED" || job?.status === "RECONNECT_REQUIRED"
        ? await retrySiteMeeting(meetingKey)
        : await pushSiteMeeting(meetingKey));
    } catch (e) {
      setError(siteSfError(e));
    } finally {
      setBusy(false);
    }
  };

  const openChange = async () => {
    setChanging(true); setRelationError(""); setObj(null);
  };

  const applyRelation = async (
    change: Parameters<typeof setSiteMeetingRelation>[2],
  ) => {
    setSaving(true); setRelationError("");
    try {
      setRelation(await setSiteMeetingRelation(workspaceId, meetingKey, change));
      setChanging(false); setObj(null);
      invalidateSiteSalesforce("relation");
    } catch (e) {
      setRelationError(siteSfError(e));
    } finally {
      setSaving(false);
    }
  };

  const openPush = (minutexTaskId?: string) => {
    router.push({
      pathname: "/site-salesforce/push-tasks" as never,
      params: {
        meeting: meetingKey, title, workspace: workspaceId,
        ...(minutexTaskId ? { task: minutexTaskId } : {}),
      },
    } as never);
  };

  // Nothing flashes while loading.
  if (!loaded) return null;

  if (!conn) {
    return (
      <View style={st.card}>
        <Text style={st.title}>Salesforce</Text>
        <Text style={st.err}>{error || "Could not load the Salesforce status."}</Text>
        <Button label="Try again" variant="secondary" onPress={() => { setError(""); void load(); }}
          style={{ marginTop: S.md }} />
      </View>
    );
  }

  if (!endpointEnabled && !conn.connected) {
    return (
      <View style={st.card}>
        <Text style={st.title}>Salesforce</Text>
        <Text style={st.sub}>Connect this workspace&apos;s Salesforce Site to push meetings and create Tasks.</Text>
        <Button label="Set up Salesforce (Site)" variant="secondary"
          onPress={() => router.push("/site-salesforce" as never)} style={{ marginTop: S.md }} />
      </View>
    );
  }

  const pushLine = meetingPushLine(job);
  const pushing = isInFlight(job?.status);
  const lineColor = pushLine.tone === "success" ? C.success : pushLine.tone === "error" ? C.danger : C.textDim;
  const failed = job?.status === "FAILED" || job?.status === "RECONNECT_REQUIRED";
  const related = hasRelation(relation);
  const summary = relationSummary(relation);
  const manualWho = !!relation?.who && !relation.automatic;
  // A Task is attached to the pushed meeting record (Related To) and the
  // meeting's Contact (Name), if Salesforce allows Tasks on that person.
  const taskWho = taskRelationOf(relation).relation?.who;
  const taskTarget = taskTargetLines({ meetingRecord, who: taskWho });
  const placedRelated = (meetingRecord?.related_to || []).map((r) => r.name).filter(Boolean).join(" · ");
  // The relationship changed after the last push: the record in Salesforce
  // still shows the old one until the meeting is pushed again.
  const relationChanged = !!meetingRecord && !pushing && placedRelated !== summary;

  return (
    <View style={st.card}>
      <Text style={st.title}>Salesforce</Text>
      {error ? <Text style={st.err}>{error}</Text> : null}

      {conn.connected ? (
        <>
          <Text style={st.relation} numberOfLines={3}>
            {related ? `Linked to: ${summary}` : "Not linked to a Salesforce record yet"}
          </Text>
          {relationLines(relation).length > 1 ? relationLines(relation).map((l) => (
            <View key={`${l.label}-${l.name}`} style={st.sideRow}>
              <Text style={st.relationLabel} numberOfLines={1}>{l.label}: </Text>
              <Text style={[st.relationLabel, { color: C.text, flexShrink: 1 }]} numberOfLines={1}>{l.name}</Text>
            </View>
          )) : null}
          <Text style={st.sub}>{relationSourceNote(relation)}</Text>
          {!changing ? (
            <Pressable onPress={() => { void openChange(); }} accessibilityRole="button" hitSlop={8}>
              <Text style={st.link}>{related ? "Change linked record" : "Link to a Salesforce record"}</Text>
            </Pressable>
          ) : (
            <View style={st.advanced}>
              {relationError ? <Text style={st.err}>{relationError}</Text> : null}
              <Text style={st.sub}>
                Push Meeting always creates a new MinuteX Meeting record in Salesforce. Here you
                choose an EXISTING record it is linked to — nothing new is created in that object.
              </Text>
              {relation?.candidates.length ? (
                <>
                  <Text style={st.step}>Link to a Contact from this meeting</Text>
                  {relation.candidates.map((c) => {
                    const on = relation.who?.person_key === c.person_key;
                    return (
                      <Pressable key={c.person_key} disabled={saving || on}
                        onPress={() => { void applyRelation({ person_key: c.person_key }); }}
                        style={[st.candidate, on && st.candidateOn]}
                        accessibilityRole="button" accessibilityState={{ selected: on }}
                        accessibilityLabel={`Relate this meeting to ${c.name}`}>
                        <Text style={st.candidateName} numberOfLines={1}>{c.name}{on ? "  ✓" : ""}</Text>
                        {c.company || c.title ? (
                          <Text style={st.candidateSub} numberOfLines={1}>
                            {[c.title, c.company].filter(Boolean).join(" · ")}
                          </Text>
                        ) : null}
                      </Pressable>
                    );
                  })}
                </>
              ) : null}

              <Text style={st.step}>Or link to an existing record</Text>
              <ObjectPicker workspaceId={workspaceId} value={obj} onChange={setObj} capability="meeting" />
              {obj ? (
                <>
                  <Text style={st.step}>Which {obj.label} is this meeting about?</Text>
                  <RecordPicker workspaceId={workspaceId} objectApiName={obj.api_name} objectLabel={obj.label}
                    value={null}
                    onChange={(r) => {
                      if (r) void applyRelation({ object_api_name: obj.api_name, record_id: r.id, record_name: r.name });
                    }} />
                </>
              ) : null}

              {manualWho ? (
                <Button label="Use the tagged Salesforce Contact" variant="ghost" disabled={saving}
                  onPress={() => { void applyRelation({ reset: "who" }); }} style={{ marginTop: S.sm }} />
              ) : null}
              {relation?.what ? (
                <Button label={`Remove ${relation.what.name || relation.what.object_label}`} variant="ghost"
                  disabled={saving} onPress={() => { void applyRelation({ reset: "what" }); }} />
              ) : null}
              <Button label="Done" variant="secondary" disabled={saving}
                onPress={() => { setChanging(false); setObj(null); setRelationError(""); }}
                style={{ marginTop: S.sm }} />
            </View>
          )}
        </>
      ) : null}

      <Text style={st.step}>1 · Push the meeting</Text>
      <Text style={[st.line, { color: lineColor, marginTop: 0 }]}>
        {pushLine.tone === "success" ? "✓ " : ""}{pushLine.text}
      </Text>
      {meetingRecord ? (
        <View style={st.placed}>
          <Text style={st.placedHead}>IN SALESFORCE</Text>
          <Text style={st.placedName} numberOfLines={2}>{meetingRecordLine(meetingRecord)}</Text>
          <Text style={st.placedMeta} numberOfLines={2}>
            {placedRelated ? `Related to ${placedRelated}` : "Not related to a Contact or record"}
          </Text>
          {relationChanged ? (
            <Text style={[st.placedMeta, { color: C.warn }]}>
              The Salesforce record changed since this push. Push the meeting again to update it.
            </Text>
          ) : null}
          {meetingRecord.url ? (
            <Pressable onPress={() => { void Linking.openURL(meetingRecord.url); }}
              accessibilityRole="link" hitSlop={8}>
              <Text style={st.link}>Open in Salesforce</Text>
            </Pressable>
          ) : null}
        </View>
      ) : null}
      <Button
        label={failed ? "Retry Push" : job?.status === "SYNCED" ? "Push Meeting Again" : "Push Meeting"}
        variant="secondary" onPress={() => { void push(); }} loading={busy}
        disabled={busy || pushing || !endpointEnabled} style={{ marginTop: S.sm }} />
      {!meetingRecord ? (
        <Text style={st.sub}>Creates a MinuteX Meeting record in Salesforce (transcript, audio link, participants, summary, action items){related ? `, related to ${summary}` : ""}.</Text>
      ) : null}

      <Text style={st.step}>2 · Push its tasks</Text>
      <Button label="Push tasks to Salesforce" onPress={() => openPush()}
        disabled={!conn?.connected || !conn.task_permissions?.createable || !meetingRecord}
        style={{ marginTop: S.xs }} />
      <Text style={st.sub}>
        {!meetingRecord
          ? "Push the meeting first — its Tasks are attached to the meeting's Salesforce record."
          : `Each Salesforce Task is attached to ${taskTarget.map((l) => `${l.label}: ${l.name}`).join(", ")}. You choose who each is assigned to.`}
      </Text>
      {!conn?.connected ? (
        <Text style={st.sub}>Connect Salesforce to push tasks.</Text>
      ) : !conn.task_permissions?.createable ? (
        <Text style={st.sub}>Salesforce permissions do not allow the integration user to create Tasks.</Text>
      ) : null}

      {tasks.length ? (
        <>
          <Text style={st.tasksHead}>Tasks in Salesforce</Text>
          {tasks.map((t) => {
            const relatedTo = (t.related_to || []).map((r) => r.name).filter(Boolean).join(" · ")
              || t.related_record_name;
            return (
              <Pressable key={t.task_ref} style={st.taskRow}
                onPress={() => openPush(t.minutex_task_id || undefined)}
                accessibilityRole="button" accessibilityLabel={`Salesforce Task ${t.subject}`}>
                <Text style={st.taskTitle} numberOfLines={2}>{t.subject}</Text>
                <Text style={st.taskMeta} numberOfLines={2}>
                  {[relatedTo, t.assignee_name, t.due_date ? `due ${t.due_date}` : ""].filter(Boolean).join(" · ")}
                </Text>
                <FlagRow>
                  <Flag label={t.sync_status === "SYNCED" ? "In Salesforce" : syncLabel(t.sync_status)} on
                    tone={t.sync_status === "FAILED" ? "warn" : isInFlight(t.sync_status) ? "info" : undefined} />
                </FlagRow>
              </Pressable>
            );
          })}
        </>
      ) : null}
    </View>
  );
}
