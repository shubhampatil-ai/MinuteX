// src/app/task/[id].tsx — one task: who owes it, where it came from, and why.
//
// Reached from the Task Tracker, so it must stand on its own without a meeting
// in context — hence getTaskDetail, which returns the task plus the contact,
// folder and source meeting in one call.
//
// The important screen here is the RESOLUTION flow. When the AI extracts
// "Rahul, send the proposal by Friday", it records the name and which speaker
// said it, and refuses to guess which Rahul that is. This screen is where a
// human closes that gap, and it is deliberately built so that:
//
//   * an unresolved task never displays its name as a confirmed assignee;
//   * the candidate list is a CHOICE, never a pre-selection — even when there
//     is exactly one candidate, because one name match is not proof of
//     identity and quietly picking it is how a task ends up with the wrong
//     person;
//   * folder members are shown first (a hint), labelled as such, not ranked
//     silently;
//   * "Choose someone else" is always available, because the right person may
//     not be a name match at all.
//
// Resolving through a speaker mapping (Participants screen) is the other route
// to the same outcome, and is better when the whole meeting needs identifying.
// This screen mentions it when the task carries a speaker id.
import { useCallback, useMemo, useState } from "react";
import {
  Alert, Pressable, RefreshControl, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { Icon } from "../../../lib/icons";
import {
  S, R, ELEV, CAPS, FONT, useTheme, ColorScale,
} from "../../../lib/theme";
import {
  Button, Card, ErrorText, Loading, SectionTitle,
} from "../../../lib/ui";
import {
  ApiContact, ApiError, AssigneeCandidate, TaskDetail, TaskStatusV2,
  assigneeLabel, getAssigneeCandidates, getFolderContacts, getParticipants,
  getTaskDetail, needsAssigneeResolution, patchTaskById, resolveTaskAssignee,
} from "../../../lib/api";
import { ContactPicker } from "../../../lib/contact-picker";
import { DueDatePicker, isPlottableDue } from "../../../lib/due-date-picker";
import { avatarColorFor, initialsOf } from "../../../lib/task-model";
import { dayHeading } from "../../../lib/task-insights";

const STATUSES: TaskStatusV2[] = [
  "Open",
  "In Progress",
  "Completed",
  "Cancelled",
];

export default function TaskDetailScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id: string }>();
  const taskId = String(id || "");

  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [candidates, setCandidates] = useState<AssigneeCandidate[]>([]);
  const [searchedName, setSearchedName] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [dueOpen, setDueOpen] = useState(false);
  // Ranking context for the assignee picker: who was in this meeting, and who
  // is in its folder. Fetched lazily when the picker opens rather than on every
  // task view — most visits never assign anyone.
  const [meetingContacts, setMeetingContacts] = useState<ApiContact[]>([]);
  const [folderContacts, setFolderContacts] = useState<ApiContact[]>([]);

  const load = useCallback(
    async (isRefresh = false) => {
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        const res = await getTaskDetail(taskId);
        setDetail(res);
        // Only ask for candidates when there is actually something to resolve —
        // a resolved task has no question to answer.
        if (needsAssigneeResolution(res.task)) {
          try {
            const c = await getAssigneeCandidates(taskId);
            setCandidates(c.candidates);
            setSearchedName(c.searched_name);
          } catch {
            setCandidates([]);
          }
        } else {
          setCandidates([]);
        }
      } catch (e) {
        setError(
          e instanceof ApiError && e.status === 404
            ? "This task no longer exists."
            : e instanceof ApiError
              ? e.message
              : "Could not load this task."
        );
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [taskId]
  );

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  const setStatus = useCallback(
    async (status: TaskStatusV2) => {
      setBusy(true);
      try {
        await patchTaskById(taskId, { status });
        await load();
      } catch (e) {
        Alert.alert(
          "Could not update",
          e instanceof ApiError ? e.message : "Please try again."
        );
      } finally {
        setBusy(false);
      }
    },
    [taskId, load]
  );

  /** Set (or clear) the due date. The picker only ever emits a real
   * YYYY-MM-DD, so anything saved here is a date the calendar can plot —
   * which is the whole point of having a picker rather than a text field. */
  const setDue = useCallback(
    async (dayKey: string) => {
      setBusy(true);
      try {
        await patchTaskById(taskId, { due: dayKey });
        await load();
      } catch (e) {
        Alert.alert(
          "Could not update",
          e instanceof ApiError ? e.message : "Please try again."
        );
      } finally {
        setBusy(false);
      }
    },
    [taskId, load]
  );

  // Pull the meeting's tagged people and the folder's contacts so the picker can
  // rank "in this meeting" above "in this folder" above everyone else. Failures
  // are swallowed on purpose: this only affects ORDERING, and a picker that
  // still lists every contact is far better than one that refuses to open.
  const openAssigneePicker = useCallback(async () => {
    setPickerOpen(true);
    const key = detail?.recording?.audio_s3_key;
    if (key) {
      try {
        const p = await getParticipants(key);
        setMeetingContacts(
          p.participants
            .map((x) => x.contact)
            .filter((c): c is ApiContact => !!c)
        );
        // The participants call already returns the folder's contacts, so this
        // costs nothing extra.
        setFolderContacts(p.folder_contacts);
      } catch {
        setMeetingContacts([]);
      }
    }
    // No meeting (or it had none) but the task is filed — still rank the folder.
    if (!key && detail?.folder?.id) {
      try {
        setFolderContacts(await getFolderContacts(detail.folder.id));
      } catch {
        setFolderContacts([]);
      }
    }
  }, [detail]);

  const resolveTo = useCallback(
    async (contact: ApiContact) => {
      setBusy(true);
      try {
        await resolveTaskAssignee(taskId, contact.id);
        await load();
      } catch (e) {
        Alert.alert(
          "Could not assign",
          e instanceof ApiError ? e.message : "Please try again."
        );
      } finally {
        setBusy(false);
      }
    },
    [taskId, load]
  );

  if (loading) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title: "Task" }} />
        <Loading label="Loading task" />
      </View>
    );
  }

  if (error || !detail) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title: "Task" }} />
        <View style={{ gap: S.md, marginTop: S.lg }}>
          <ErrorText>{error || "Task not found."}</ErrorText>
          <Button label="Retry" variant="secondary" onPress={() => load()} />
          <Button label="Back" variant="ghost" onPress={() => router.back()} />
        </View>
      </View>
    );
  }

  const { task, contact, folder, recording } = detail;
  const { name, confirmed } = assigneeLabel(task);
  const needsPerson = needsAssigneeResolution(task);

  return (
    <ScrollView
      style={st.container}
      contentContainerStyle={{ paddingBottom: S.xxl * 2 }}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={() => load(true)}
          tintColor={C.primary}
        />
      }
      showsVerticalScrollIndicator={false}
    >
      <Stack.Screen options={{ title: "Task" }} />

      <Text style={st.title}>{task.task}</Text>
      <View style={st.metaRow}>
        <View
          style={[st.pill, { backgroundColor: statusTint(task.status, C).bg }]}
        >
          <Text style={[st.pillTxt, { color: statusTint(task.status, C).fg }]}>
            {task.status}
          </Text>
        </View>
        {task.is_overdue && (
          <View style={[st.pill, { backgroundColor: C.dangerSoft }]}>
            <Text style={[st.pillTxt, { color: C.danger }]}>Overdue</Text>
          </View>
        )}
        {!!task.due && <Text style={st.due}>Due {task.due}</Text>}
        {task.source_type === "AI" && (
          <View style={[st.pill, { backgroundColor: C.accentSoft }]}>
            <Text style={[st.pillTxt, { color: C.accent }]}>From meeting</Text>
          </View>
        )}
      </View>

      {/* ---------------- ASSIGNEE ---------------- */}
      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Assignee</SectionTitle>

        {needsPerson ? (
          <Card style={{ gap: S.md }}>
            <View style={st.warnHead}>
              <Icon
                name="exclamationmark.triangle"
                size={15}
                tintColor={C.warn}
              />
              <Text style={st.warnTitle}>
                {searchedName || name
                  ? `Who is "${searchedName || name}"?`
                  : "This task needs an assignee"}
              </Text>
            </View>
            <Text style={st.warnBody}>
              {searchedName || name
                ? "This name came from the meeting. MinuteX will not guess which person it means — pick the right one so the task reaches them."
                : "Nobody is assigned to this task yet."}
            </Text>

            {candidates.length > 0 && (
              <>
                <Text style={st.candidateHead}>
                  {candidates.length === 1
                    ? "Possible match"
                    : `${candidates.length} possible matches`}
                </Text>
                {candidates.map((c) => (
                  <Pressable
                    key={c.id}
                    style={st.candidate}
                    onPress={() => resolveTo(c)}
                    disabled={busy}
                    accessibilityRole="button"
                    accessibilityLabel={`Assign to ${c.name}`}
                  >
                    <View
                      style={[
                        st.avatarSm,
                        { backgroundColor: avatarColorFor(c.name) },
                      ]}
                    >
                      <Text style={st.avatarSmTxt}>{initialsOf(c.name)}</Text>
                    </View>
                    <View style={{ flex: 1 }}>
                      <Text style={st.candidateName}>{c.name}</Text>
                      {!!(c.email || c.company) && (
                        <Text style={st.candidateSub} numberOfLines={1}>
                          {c.email || c.company}
                        </Text>
                      )}
                    </View>
                    {/* Being in this task's folder is a HINT for the human,
                        labelled as such — it never auto-selects. */}
                    {c.in_folder && !!folder && (
                      <View style={st.inFolderPill}>
                        <Text style={st.inFolderTxt}>{folder.name}</Text>
                      </View>
                    )}
                  </Pressable>
                ))}
              </>
            )}

            <Button
              label={
                candidates.length ? "Choose someone else" : "Choose a Contact"
              }
              variant="secondary"
              onPress={openAssigneePicker}
              disabled={busy}
            />

            {/* The other route to the same answer, offered only when it
                actually applies to this task. */}
            {!!task.assignee_speaker_id && !!recording && (
              <Pressable
                onPress={() =>
                  router.push({
                    pathname: "/recording/[key]/participants",
                    params: { key: recording.audio_s3_key },
                  } as any)
                }
                accessibilityRole="button"
                accessibilityLabel="Map speakers for this meeting"
              >
                <Text style={st.altLink}>
                  {/* Named through the meeting's current speaker_names when
                      there is a name — a renamed speaker reads as their name
                      here too, not the raw label the AI extracted. */}
                  This came from {task.speaker_name
                    || `Speaker ${task.assignee_speaker_id}`}. Mapping
                  that speaker resolves every task they own →
                </Text>
              </Pressable>
            )}
          </Card>
        ) : confirmed && contact ? (
          <Pressable
            style={st.assigneeCard}
            onPress={() =>
              router.push({
                pathname: "/contact/[id]",
                params: { id: contact.id },
              } as any)
            }
            accessibilityRole="button"
            accessibilityLabel={`Open ${contact.name}`}
          >
            <View
              style={[st.avatar, { backgroundColor: avatarColorFor(contact.name) }]}
            >
              <Text style={st.avatarTxt}>{initialsOf(contact.name)}</Text>
            </View>
            <View style={{ flex: 1 }}>
              <Text style={st.assigneeName}>{contact.name}</Text>
              {!!contact.email && (
                <Text style={st.assigneeSub}>{contact.email}</Text>
              )}
              {/* Whether this task can actually notify them, stated plainly. */}
              <Text
                style={[
                  st.notifyLine,
                  {
                    color: task.assignee_user_id ? C.success : C.textFaint,
                  },
                ]}
              >
                {task.assignee_user_id
                  ? "Has a MinuteX account — can be notified in the app"
                  : "No MinuteX account — reach them by email or phone"}
              </Text>
            </View>
            <Icon name="chevron.right" size={15} tintColor={C.textFaint} />
          </Pressable>
        ) : (
          <Card style={{ gap: S.md }}>
            <Text style={st.warnBody}>Nobody is assigned to this task.</Text>
            <Button
              label="Choose a Contact"
              variant="secondary"
              onPress={openAssigneePicker}
              disabled={busy}
            />
          </Card>
        )}

        {confirmed && (
          <View style={{ marginTop: S.sm }}>
            <Button
              label="Reassign"
              variant="ghost"
              onPress={openAssigneePicker}
              disabled={busy}
            />
          </View>
        )}
      </View>

      {/* ---------------- DUE DATE ---------------- */}
      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Due date</SectionTitle>
        <Pressable
          style={st.dueRow}
          onPress={() => setDueOpen(true)}
          disabled={busy}
          accessibilityRole="button"
          accessibilityLabel={
            task.due ? `Change due date, currently ${task.due}` : "Set a due date"
          }
        >
          <Icon name="calendar" size={16} tintColor={C.primary} />
          <View style={{ flex: 1 }}>
            <Text style={[st.dueValue, !task.due && { color: C.textFaint }]}>
              {task.due
                ? isPlottableDue(task.due)
                  ? dayHeading(task.due, new Date())
                  : task.due
                : "No due date"}
            </Text>
            {/* A legacy task can carry free text ("Friday") from before dates
                were picked rather than typed. The backend stores due_date
                verbatim, so it persists — but the calendar can only plot a
                real day. Say so, instead of leaving someone wondering why
                their task never appears there. */}
            {task.due && !isPlottableDue(task.due) ? (
              <Text style={st.dueHint}>
                Not a calendar date — pick one so it shows on the calendar
              </Text>
            ) : null}
          </View>
          <Icon name="chevron.right" size={15} tintColor={C.textFaint} />
        </Pressable>
      </View>

      {/* ---------------- STATUS ---------------- */}
      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Status</SectionTitle>
        <View style={st.statusRow}>
          {STATUSES.map((s) => {
            const on = task.status === s;
            return (
              <Pressable
                key={s}
                style={[
                  st.statusBtn,
                  on && {
                    backgroundColor: statusTint(s, C).bg,
                    borderColor: statusTint(s, C).fg,
                  },
                ]}
                onPress={() => setStatus(s)}
                disabled={busy || on}
                accessibilityRole="button"
                accessibilityLabel={`Set status to ${s}`}
              >
                <Text
                  style={[
                    st.statusBtnTxt,
                    on && { color: statusTint(s, C).fg },
                  ]}
                >
                  {s}
                </Text>
              </Pressable>
            );
          })}
        </View>
      </View>

      {/* ---------------- CONTEXT: where and why ---------------- */}
      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Where this came from</SectionTitle>
        <Card>
          {!!recording && (
            <Pressable
              style={st.ctxRow}
              onPress={() =>
                router.push({
                  pathname: "/recording/[key]",
                  params: { key: recording.audio_s3_key },
                } as any)
              }
              accessibilityRole="button"
              accessibilityLabel="Open source meeting"
            >
              <Icon name="waveform" size={15} tintColor={C.primary} />
              <View style={{ flex: 1 }}>
                <Text style={st.ctxLabel}>Meeting</Text>
                <Text style={st.ctxValue} numberOfLines={2}>
                  {recording.title || "Untitled meeting"}
                </Text>
              </View>
              <Icon name="chevron.right" size={14} tintColor={C.textFaint} />
            </Pressable>
          )}
          {!!folder && (
            <Pressable
              style={st.ctxRow}
              onPress={() =>
                router.push({
                  pathname: "/folder/[id]",
                  params: { id: folder.id },
                } as any)
              }
              accessibilityRole="button"
              accessibilityLabel="Open folder"
            >
              <Icon name="folder" size={15} tintColor={C.primary} />
              <View style={{ flex: 1 }}>
                <Text style={st.ctxLabel}>Folder</Text>
                <Text style={st.ctxValue}>{folder.name}</Text>
              </View>
              <Icon name="chevron.right" size={14} tintColor={C.textFaint} />
            </Pressable>
          )}
          {/* The AI's own words. Showing the evidence is what makes an
              extracted task auditable rather than something to take on faith. */}
          {!!task.ai_evidence && (
            <View style={[st.ctxRow, { borderBottomWidth: 0 }]}>
              <Icon name="text.bubble" size={15} tintColor={C.accent} />
              <View style={{ flex: 1 }}>
                <Text style={st.ctxLabel}>What was said</Text>
                <Text style={st.evidence}>&ldquo;{task.ai_evidence}&rdquo;</Text>
              </View>
            </View>
          )}
        </Card>
      </View>

      <DueDatePicker
        visible={dueOpen}
        onClose={() => setDueOpen(false)}
        value={isPlottableDue(task.due) ? task.due : ""}
        onChange={setDue}
      />

      <ContactPicker
        visible={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onPick={resolveTo}
        meetingContacts={meetingContacts}
        folderContacts={folderContacts}
        folderId={folder?.id}
        folderName={folder?.name}
        title="Assign Task To"
      />
    </ScrollView>
  );
}

function statusTint(status: TaskStatusV2 | string, C: ColorScale) {
  switch (status) {
    case "Completed":
      return { bg: C.successSoft, fg: C.success };
    case "In Progress":
      return { bg: C.primarySoft, fg: C.primary };
    case "Cancelled":
      return { bg: C.surface2, fg: C.textFaint };
    default:
      return { bg: C.warnSoft, fg: C.warn };
  }
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    title: { ...T.headline, marginTop: S.md },
    metaRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      flexWrap: "wrap" as const, gap: 6, marginTop: S.sm,
    },
    pill: { paddingHorizontal: 9, paddingVertical: 4, borderRadius: R.pill },
    pillTxt: { fontFamily: FONT.bold, fontSize: 11 },
    due: { fontFamily: FONT.medium, fontSize: 12, color: C.textFaint },
    dueRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      shadowColor: C.shadow, ...ELEV.sm,
    },
    dueValue: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    dueHint: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.warn, marginTop: 3,
    },
    warnHead: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
    },
    warnTitle: { fontFamily: FONT.bold, fontSize: 15, color: C.text, flex: 1 },
    warnBody: { ...T.bodyDim, fontSize: 13 },
    candidateHead: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.2,
      color: C.textFaint, marginTop: 2,
    },
    candidate: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      padding: S.md, borderRadius: R.md, backgroundColor: C.surface2,
      borderWidth: 1, borderColor: C.border,
    },
    candidateName: { fontFamily: FONT.bold, fontSize: 14, color: C.text },
    candidateSub: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 1,
    },
    inFolderPill: {
      paddingHorizontal: 8, paddingVertical: 3, borderRadius: R.pill,
      backgroundColor: C.primarySoft,
    },
    inFolderTxt: { fontFamily: FONT.bold, fontSize: 10, color: C.primary },
    altLink: {
      fontFamily: FONT.medium, fontSize: 12.5, color: C.primary,
      lineHeight: 18,
    },
    assigneeCard: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      shadowColor: C.shadow, ...ELEV.sm,
    },
    avatar: {
      width: 46, height: 46, borderRadius: 23, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 17, color: "#fff" },
    avatarSm: {
      width: 34, height: 34, borderRadius: 17, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    avatarSmTxt: { fontFamily: FONT.bold, fontSize: 12.5, color: "#fff" },
    assigneeName: { fontFamily: FONT.bold, fontSize: 15.5, color: C.text },
    assigneeSub: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint,
      marginTop: 1,
    },
    notifyLine: { fontFamily: FONT.medium, fontSize: 11.5, marginTop: 4 },
    statusRow: {
      flexDirection: "row" as const, flexWrap: "wrap" as const, gap: S.sm,
    },
    statusBtn: {
      paddingHorizontal: 14, paddingVertical: 9, borderRadius: R.pill,
      borderWidth: 1, borderColor: C.border, backgroundColor: C.surface,
    },
    statusBtnTxt: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.textDim },
    ctxRow: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.md, paddingVertical: 11, borderBottomWidth: 1,
      borderBottomColor: C.border,
    },
    ctxLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.2,
      color: C.textFaint,
    },
    ctxValue: {
      fontFamily: FONT.bold, fontSize: 13.5, color: C.text, marginTop: 2,
    },
    evidence: {
      fontFamily: FONT.regular, fontSize: 13, color: C.textDim, marginTop: 3,
      fontStyle: "italic" as const, lineHeight: 19,
    },
  });
}
