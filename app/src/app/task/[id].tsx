// src/app/task/[id].tsx — one task: who owes it, where it came from, and why.
//
// Reached from the Task Tracker, so it must stand on its own without a meeting
// in context — hence getTaskDetail, which returns the task plus the contact
// and source meeting in one call.
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
  Avatar, Button, Card, ErrorText, Loading, SectionTitle,
} from "../../../lib/ui";
import {
  ApiContact, ApiError, AssigneeCandidate, CREATOR_PERMISSIONS, TaskDetail,
  TaskStatusV2,
  assigneeLabel, getAssigneeCandidates, getParticipants,
  getTaskDetail, needsAssigneeResolution, patchTaskById, resolveTaskAssignee,
} from "../../../lib/api";
import { ContactPicker } from "../../../lib/contact-picker";
import { DueDatePicker, isPlottableDue } from "../../../lib/due-date-picker";
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
  // Fetched lazily when the picker opens rather than on every
  // task view — most visits never assign anyone.
  const [meetingContacts, setMeetingContacts] = useState<ApiContact[]>([]);

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

  // Pull the meeting's tagged people so the picker can rank "in this meeting"
  // above everyone else. Failures
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
      } catch {
        setMeetingContacts([]);
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

  const { task, contact, recording, assigned_by: assignedBy } = detail;
  // WHAT THIS USER MAY DO, decided by the backend and merely rendered here.
  //
  // The creator controls the task's configuration; the assignee executes it
  // and may change only its status. Hiding a control the caller cannot use is
  // a courtesy, NOT the enforcement point — every one of these actions is
  // re-checked server-side, so a client that ignored this object would get a
  // 403 rather than a write. Falling back to CREATOR_PERMISSIONS keeps an
  // older backend (which sends no `permissions`) behaving exactly as before.
  const perms = detail.permissions ?? CREATOR_PERMISSIONS;
  const { name, confirmed } = assigneeLabel(task);
  // An unresolved assignee is only the CREATOR's question to answer. For an
  // assignee the resolution UI would be a prompt they cannot act on, so the
  // task reads as normally assigned to them instead.
  const needsPerson = needsAssigneeResolution(task) && perms.can_resolve_assignment;
  // Only ids the BACKEND validated against this meeting's transcript
  // reach here - the app never re-derives, repairs or invents a reference.
  const evidenceIds = task?.ai_evidence_segment_ids ?? [];

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
            <Text style={[st.pillTxt, { color: C.accent }]}>AI generated</Text>
          </View>
        )}
        {/* The MODEL's own confidence, shown as the band it actually reported.
            Never a percentage: the backend stores a 3-value enum precisely
            because a number would be precision the model did not have. Absent
            for manual tasks and for AI rows the model never scored, which is
            why this renders nothing rather than "unknown". */}
        {task.source_type === "AI" && !!task.ai_confidence && (
          <View
            style={[
              st.pill,
              { backgroundColor: confidenceTint(task.ai_confidence, C).bg },
            ]}
          >
            <Text
              style={[
                st.pillTxt,
                { color: confidenceTint(task.ai_confidence, C).fg },
              ]}
            >
              {confidenceLabel(task.ai_confidence)} confidence
            </Text>
          </View>
        )}
      </View>

      {/* NEEDS REVIEW. Driven by the server's `needs_review`, never
          re-derived here — the app and the API must not be able to disagree
          about whether a task is safe. It says what MinuteX could not do and
          points at the control that fixes it, rather than being a bare
          warning the user cannot act on. */}
      {task.needs_review && (
        <View style={st.reviewBanner}>
          <Icon name="exclamationmark.triangle.fill" size={16} tintColor={C.warn} />
          <View style={{ flex: 1 }}>
            <Text style={st.reviewTitle}>Needs review</Text>
            <Text style={st.reviewBody}>
              MinuteX could not confidently identify who this is for
              {task.assignee_name_legacy
                ? ` — it heard “${task.assignee_name_legacy}”.`
                : "."}{" "}
              Confirm the assignee below.
            </Text>
          </View>
        </View>
      )}

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
                    {/* Their real photo when we have one — a candidate list
                        is exactly where a face disambiguates two people with
                        the same name. Avatar falls back to these same
                        coloured initials otherwise. */}
                    <Avatar
                      name={c.name}
                      photoUri={c.avatar_view_url}
                      size={34}
                      fontSize={12}
                    />
                    <View style={{ flex: 1 }}>
                      <Text style={st.candidateName}>{c.name}</Text>
                      {!!(c.email || c.company) && (
                        <Text style={st.candidateSub} numberOfLines={1}>
                          {c.email || c.company}
                        </Text>
                      )}
                    </View>
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
          // The card is only a LINK when there is a contact record this user
          // can actually open. An assignee is shown themselves, built from the
          // task row rather than the creator's address book (which is not
          // theirs to read), so it carries no id — tapping it would 404.
          <Pressable
            style={st.assigneeCard}
            onPress={
              contact.id
                ? () =>
                    router.push({
                      pathname: "/contact/[id]",
                      params: { id: contact.id },
                    } as any)
                : undefined
            }
            disabled={!contact.id}
            accessibilityRole={contact.id ? "button" : "text"}
            accessibilityLabel={
              contact.id ? `Open ${contact.name}` : `Assigned to ${contact.name}`
            }
          >
            <Avatar
              name={contact.name}
              photoUri={contact.avatar_view_url}
              size={44}
              fontSize={15}
            />
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
            {contact.id ? (
              <Icon name="chevron.right" size={15} tintColor={C.textFaint} />
            ) : null}
          </Pressable>
        ) : (
          <Card style={{ gap: S.md }}>
            <Text style={st.warnBody}>Nobody is assigned to this task.</Text>
            {perms.can_change_assignee && (
              <Button
                label="Choose a Contact"
                variant="secondary"
                onPress={openAssigneePicker}
                disabled={busy}
              />
            )}
          </Card>
        )}

        {confirmed && perms.can_change_assignee && (
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
        {/* READ-ONLY for an assignee. The deadline is the creator's to set —
            an assignee who could move it could excuse their own lateness. The
            row still renders (they need to know when it is due), it simply
            does not open the picker, and it announces itself as a value
            rather than a button so screen readers do not offer an action that
            would be refused. */}
        <Pressable
          style={st.dueRow}
          onPress={perms.can_change_deadline ? () => setDueOpen(true) : undefined}
          disabled={busy || !perms.can_change_deadline}
          accessibilityRole={perms.can_change_deadline ? "button" : "text"}
          accessibilityLabel={
            !perms.can_change_deadline
              ? `Due ${task.due || "not set"} — only the task creator can change this`
              : task.due
                ? `Change due date, currently ${task.due}`
                : "Set a due date"
          }
        >
          <Icon
            name="calendar"
            size={16}
            tintColor={perms.can_change_deadline ? C.primary : C.textFaint}
          />
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
          {perms.can_change_deadline ? (
            <Icon name="chevron.right" size={15} tintColor={C.textFaint} />
          ) : null}
        </Pressable>
      </View>

      {/* ---------------- STATUS ---------------- */}
      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Status</SectionTitle>
        {/* Says WHY the other controls are read-only, once, near the one
            control that is not. Without this an assignee just finds a screen
            that mostly does not respond — the rule is not a punishment, it is
            "this task is yours to do, not to redefine". Shown only to an
            assignee who is not also the creator. */}
        {!perms.is_creator && perms.is_assignee ? (
          <Text style={st.roleNote}>
            This task is assigned to you. You can move it along; its deadline
            and details stay with whoever created it.
          </Text>
        ) : null}
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

      {/* ---------------- ASK AI ABOUT THIS TASK (§4) ----------------
          Placed between the task's own FACTS above and its PROVENANCE below,
          because that is exactly what it bridges: it explains why this task
          exists, using the meeting it came from, without ever restating the
          state the screen already shows authoritatively.

          A launcher rather than an inline answer. The assistant needs a
          composer and a scrollable thread, and both belong on their own
          screen; embedding them here would put a chat inside a ScrollView
          that already owns the page's scroll. The task id is passed as a
          route param so the conversation opens scoped to this task — the
          backend still authorizes that id on every tool call, so the param
          is addressing, not access. */}
      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Ask AI</SectionTitle>
        <Pressable
          onPress={() =>
            router.push({
              pathname: "/assistant", params: { taskId: task.id },
            } as never)
          }
          accessibilityRole="button"
          accessibilityLabel="Ask AI about this task"
          style={({ pressed }) => [st.askAi, pressed && { opacity: 0.7 }]}
        >
          <View style={st.askAiIcon}>
            <Icon name="sparkles" size={15} tintColor={C.primary} />
          </View>
          <View style={{ flex: 1 }}>
            <Text style={st.askAiTitle}>Ask about this task</Text>
            <Text style={st.askAiSub}>
              {recording
                ? "Why it was created, what was discussed, and what to do next."
                : "What this task covers and what to do next."}
            </Text>
          </View>
          <Icon name="chevron.right" size={14} tintColor={C.textFaint} />
        </Pressable>
      </View>

      {/* ---------------- CONTEXT: where and why ---------------- */}
      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Where this came from</SectionTitle>
        <Card>
          {/* WHO GAVE ME THIS. Sent by the backend only to an assignee, so
              this renders for them and never for the creator (who does not
              need to be told they made their own task). Answers the first
              question anyone asks of work that appeared in their list. */}
          {!!assignedBy?.name && (
            <View style={st.ctxRow}>
              <Avatar
                name={assignedBy.name}
                photoUri={assignedBy.avatar_view_url}
                size={22}
                fontSize={9}
              />
              <View style={{ flex: 1 }}>
                <Text style={st.ctxLabel}>Assigned by</Text>
                <Text style={st.ctxValue}>{assignedBy.name}</Text>
              </View>
            </View>
          )}
          {/* WHERE THE TASK CAME FROM — and, now, a way in.
              BOTH roles get a tappable row, but to DIFFERENT screens, chosen
              by the backend's `access` field rather than by re-deriving "am I
              the creator?" here:

                owner     -> /recording/[key], the full meeting (audio,
                             transcript, AI, editing).
                assignee  -> /meeting/[key]/shared, the read-only notes. The
                             full route still 404s for them; sending them
                             there would produce a dead end.

              `access` is absent on responses from an older backend, and the
              fallback treats that as "owner" — before this field existed, a
              key was only ever sent to the owner, so that is what an
              unlabelled key means. An assignee on an old backend gets no key
              at all and the row stays inert, exactly as it did before. */}
          {!!recording && (() => {
            const readOnly = recording.access === "assignee";
            const target = readOnly
              ? "/meeting/[key]/shared"
              : "/recording/[key]";
            const canOpen = !!recording.audio_s3_key;
            return (
              <Pressable
                style={st.ctxRow}
                onPress={
                  canOpen
                    ? () =>
                        router.push({
                          pathname: target,
                          params: { key: recording.audio_s3_key },
                        } as any)
                    : undefined
                }
                disabled={!canOpen}
                accessibilityRole={canOpen ? "button" : "text"}
                accessibilityLabel={
                  canOpen
                    ? readOnly
                      ? "Open meeting notes, view only"
                      : "Open source meeting"
                    : `From the meeting ${recording.title || "Untitled meeting"}`
                }
              >
                <Icon name="waveform" size={15} tintColor={C.primary} />
                <View style={{ flex: 1 }}>
                  <Text style={st.ctxLabel}>Meeting</Text>
                  <Text style={st.ctxValue} numberOfLines={2}>
                    {recording.title || "Untitled meeting"}
                  </Text>
                  <View style={st.ctxSubRow}>
                    {/* The date is the other half of "where did this come
                        from". */}
                    {!!recording.recorded_at && (
                      <Text style={st.ctxSub}>
                        {new Date(recording.recorded_at).toLocaleDateString(
                          undefined,
                          { day: "numeric", month: "short", year: "numeric" }
                        )}
                      </Text>
                    )}
                    {/* Says what the tap will give them BEFORE they take it.
                        Without this, an assignee taps expecting the recording,
                        finds notes only, and reads it as the app failing to
                        load rather than as the boundary it is. */}
                    {readOnly && canOpen && (
                      <>
                        {!!recording.recorded_at && (
                          <Text style={st.ctxSub}>·</Text>
                        )}
                        <Text style={st.ctxTag}>View only</Text>
                      </>
                    )}
                  </View>
                </View>
                {canOpen ? (
                  <Icon name="chevron.right" size={14} tintColor={C.textFaint} />
                ) : null}
              </Pressable>
            );
          })()}
          {/* The AI's own words. Showing the evidence is what makes an
              extracted task auditable rather than something to take on faith. */}
          {!!task.ai_evidence && (
            <View style={[st.ctxRow, { borderBottomWidth: 0 }]}>
              <Icon name="text.bubble" size={15} tintColor={C.accent} />
              <View style={{ flex: 1 }}>
                <Text style={st.ctxLabel}>What was said</Text>
                <Text style={st.evidence}>&ldquo;{task.ai_evidence}&rdquo;</Text>
                {/* Offered ONLY when the backend stored segment ids it
                    validated against this meeting's transcript. A button that
                    scrolled nowhere would be worse than no button, and older
                    tasks (and any extraction the model gave no usable
                    reference for) legitimately have none — they still show the
                    quote above. */}
                {evidenceIds.length > 0 && !!recording ? (
                  <Pressable
                    onPress={() =>
                      router.push({
                        pathname: "/recording/[key]/transcript",
                        params: {
                          key: recording.audio_s3_key,
                          evidence: evidenceIds.join(","),
                        },
                      } as never)
                    }
                    hitSlop={6}
                    style={({ pressed }) => [
                      st.viewEvidence, pressed && { opacity: 0.6 },
                    ]}
                    accessibilityRole="button"
                    accessibilityLabel="View this evidence in the transcript"
                  >
                    <Icon name="magnifyingglass" size={13} tintColor={C.primary} />
                    <Text style={st.viewEvidenceTxt}>View in transcript</Text>
                  </Pressable>
                ) : null}
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
        title="Assign Task To"
      />
    </ScrollView>
  );
}

/** The colour role for a confidence band. Low is deliberately WARN, not
 *  danger: a low-confidence extraction is a thing to check, not a failure —
 *  and the task itself may be perfectly real even when its owner is unclear. */
function confidenceTint(level: string, C: ColorScale) {
  switch (level) {
    case "high":
      return { bg: C.successSoft, fg: C.success };
    case "medium":
      return { bg: C.primarySoft, fg: C.primary };
    case "low":
      return { bg: C.warnSoft, fg: C.warn };
    default:
      return { bg: C.surface2, fg: C.textFaint };
  }
}

/** "high" -> "High". The backend's enum is the source of truth; this only
 *  capitalises it for display and never maps it onto a different vocabulary. */
function confidenceLabel(level: string): string {
  return level ? level.charAt(0).toUpperCase() + level.slice(1) : "";
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
    altLink: {
      fontFamily: FONT.medium, fontSize: 12.5, color: C.primary,
      lineHeight: 18,
    },
    assigneeCard: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      shadowColor: C.shadow, ...ELEV.sm,
    },
    assigneeName: { fontFamily: FONT.bold, fontSize: 15.5, color: C.text },
    assigneeSub: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint,
      marginTop: 1,
    },
    notifyLine: { fontFamily: FONT.medium, fontSize: 11.5, marginTop: 4 },
    ctxSub: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 2,
    },
    // The date and the "View only" tag sit on one line. A row rather than two
    // stacked Texts so the tag reads as a qualifier ON the meeting, not as
    // another fact about it.
    ctxSubRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      flexWrap: "wrap" as const,
    },
    ctxTag: {
      fontFamily: FONT.semibold, fontSize: 12, color: C.textDim, marginTop: 2,
    },
    roleNote: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint,
      lineHeight: 18, marginBottom: S.sm,
    },
    statusRow: {
      flexDirection: "row" as const, flexWrap: "wrap" as const, gap: S.sm,
    },
    statusBtn: {
      paddingHorizontal: 14, paddingVertical: 9, borderRadius: R.pill,
      borderWidth: 1, borderColor: C.border, backgroundColor: C.surface,
    },
    statusBtnTxt: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.textDim },
    askAi: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      backgroundColor: C.surface, borderWidth: 1, borderColor: C.border,
      borderRadius: R.card, padding: 13, shadowColor: C.shadow, ...ELEV.sm,
    },
    askAiIcon: {
      width: 32, height: 32, borderRadius: 10, backgroundColor: C.primarySoft,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    askAiTitle: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.text },
    askAiSub: {
      fontFamily: FONT.regular, fontSize: 11.5, lineHeight: 16,
      color: C.textDim, marginTop: 2,
    },
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
    reviewBanner: {
      flexDirection: "row", alignItems: "flex-start", gap: S.md,
      backgroundColor: C.warnSoft,
      borderRadius: R.card, borderWidth: 1, borderColor: C.warn + "40",
      padding: S.md, marginTop: S.lg,
    },
    reviewTitle: {
      fontFamily: FONT.bold, fontSize: 13, color: C.warn, marginBottom: 2,
    },
    reviewBody: { fontFamily: FONT.regular, fontSize: 13, color: C.text, lineHeight: 18 },
    viewEvidence: {
      flexDirection: "row", alignItems: "center", gap: 6, marginTop: 8,
      alignSelf: "flex-start",
    },
    viewEvidenceTxt: {
      fontFamily: FONT.semibold, fontSize: 13, color: C.primary,
    },
    evidence: {
      fontFamily: FONT.regular, fontSize: 13, color: C.textDim, marginTop: 3,
      fontStyle: "italic" as const, lineHeight: 19,
    },
  });
}
