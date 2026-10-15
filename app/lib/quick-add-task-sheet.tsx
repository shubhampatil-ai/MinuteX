// lib/quick-add-task-sheet.tsx — Quick Add from the Task Action Center (§13).
//
// WHY THIS EXISTS SEPARATELY FROM lib/add-task-sheet.tsx.
// AddTaskSheet is opened from INSIDE a meeting, so it already knows which
// recording the task belongs to. The Action Center does not: it is a
// cross-meeting screen. And task creation is meeting-scoped on the backend —
// the only create route is POST /recordings/ai/tasks/{key+} (see _ROUTES in
// lambda-userapi); there is no standalone POST /tasks.
//
// So the honest quick-add is: pick the meeting, then write the task. The most
// recent meeting is preselected because that is overwhelmingly the one a
// just-remembered commitment came out of, but it is a real, changeable choice
// rather than a silent assumption. If the account has no meetings at all,
// the sheet says so plainly instead of offering a form that cannot submit.
//
// It uses the SAME createTask + ApiTask schema as every other creation path —
// no second task model (§13).
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Icon } from "./icons";
import { S, R, CAPS, FONT, useTheme, ColorScale } from "./theme";
import {
  Button, ErrorText, KeyboardAwareSheet, TextField, scrollFormProps,
} from "./ui";
import { ContactPicker } from "./contact-picker";
import { DueDatePicker, dueDateLabel } from "./due-date-picker";
import {
  ApiContact, ApiError, ApiTask, RecordingSummary, createTask, getParticipants,
  getRecordings,
} from "./api";
import { avatarColorFor, initialsOf } from "./task-model";

const PRIORITIES: ApiTask["priority"][] = ["Low", "Medium", "High"];

export type QuickAddTaskSheetProps = {
  visible: boolean;
  onClose: () => void;
  /** Called after a successful create so the dashboard can refetch. */
  onCreated: (task: ApiTask) => void;
};

export function QuickAddTaskSheet({
  visible, onClose, onCreated,
}: QuickAddTaskSheetProps) {
  // The body is MOUNTED only while the sheet is open, so "reset on open" is a
  // fresh mount rather than an effect that writes state during render. That
  // also guarantees a half-typed task can never reappear against a different
  // meeting — the state it lived in no longer exists.
  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={onClose}
    >
      {visible ? (
        <QuickAddBody onClose={onClose} onCreated={onCreated} />
      ) : null}
    </Modal>
  );
}

function QuickAddBody({
  onClose, onCreated,
}: Omit<QuickAddTaskSheetProps, "visible">) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);

  const [text, setText] = useState("");
  const [due, setDue] = useState("");
  const [priority, setPriority] = useState<ApiTask["priority"]>("Medium");
  const [assignee, setAssignee] = useState<ApiContact | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [meetingPickerOpen, setMeetingPickerOpen] = useState(false);
  const [dueOpen, setDueOpen] = useState(false);

  const [recordings, setRecordings] = useState<RecordingSummary[]>([]);
  const [meeting, setMeeting] = useState<RecordingSummary | null>(null);
  const [loadingMeetings, setLoadingMeetings] = useState(true);

  const [meetingContacts, setMeetingContacts] = useState<ApiContact[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const now = useMemo(() => new Date(), []);

  // Load the meeting list once, on mount. No reset needed: this component
  // only exists while the sheet is open (see QuickAddTaskSheet above).
  useEffect(() => {
    let alive = true;
    getRecordings()
      .then((rs) => {
        if (!alive) return;
        // Newest first: the meeting a just-remembered task came from.
        const sorted = [...rs].sort((a, b) =>
          String(b.recorded_at || b.created_at || "").localeCompare(
            String(a.recorded_at || a.created_at || "")
          )
        );
        setRecordings(sorted);
        setMeeting(sorted[0] ?? null);
      })
      .catch(() => {
        if (alive) setRecordings([]);
      })
      .finally(() => {
        if (alive) setLoadingMeetings(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  // Ranking context for the assignee picker, refreshed whenever the chosen
  // meeting changes. Best-effort: a failure only affects ORDERING, and the
  // picker still lists every contact.
  useEffect(() => {
    if (!meeting) return;
    let alive = true;
    getParticipants(meeting.audio_s3_key)
      .then((p) => {
        if (!alive) return;
        setMeetingContacts(
          p.participants.map((x) => x.contact).filter((c): c is ApiContact => !!c)
        );
      })
      .catch(() => {
        if (alive) {
          setMeetingContacts([]);
        }
      });
    return () => {
      alive = false;
    };
  }, [meeting]);

  const submit = useCallback(async () => {
    const title = text.trim();
    if (!title || !meeting) return;
    setSaving(true);
    setError("");
    try {
      const created = await createTask(meeting.audio_s3_key, {
        task: title,
        due: due || undefined,
        priority,
        // A real contact_id — never a name. That is what makes the task
        // RESOLVED and notification-ready (§16).
        assignee_contact_id: assignee?.id || undefined,
      });
      onCreated(created);
      onClose();
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : "Could not save that task."
      );
    } finally {
      setSaving(false);
    }
  }, [text, meeting, due, priority, assignee, onCreated, onClose]);

  const canSubmit = !!text.trim() && !!meeting && !saving;

  return (
    <>
      <KeyboardAwareSheet>
        {/* Tap-outside to dismiss. Inside the keyboard-aware wrapper so the
            sheet still rides above the keyboard when a field has focus. */}
        <Pressable
          style={st.scrim}
          onPress={onClose}
          accessibilityRole="button"
          accessibilityLabel="Close quick add"
        />
        <View style={st.sheet}>
          <View style={st.grabber} />
          <View style={st.head}>
            <Text style={st.title}>Quick add</Text>
            <Pressable
              onPress={onClose}
              hitSlop={10}
              accessibilityRole="button"
              accessibilityLabel="Close"
            >
              <Icon name="xmark" size={16} tintColor={C.textFaint} />
            </Pressable>
          </View>

          <ScrollView
            {...scrollFormProps}
            style={st.body}
            showsVerticalScrollIndicator={false}
          >
            <TextField
              value={text}
              onChangeText={setText}
              placeholder="What needs to get done?"
              multiline
              autoFocus
              style={st.input}
            />

            <Text style={st.label}>Due</Text>
            {/* The shared picker, so Quick Add can reach ANY day rather than
                the four shortcuts it used to hardcode — and so all three
                creation paths agree on what a due date is. */}
            <Pressable
              onPress={() => setDueOpen(true)}
              accessibilityRole="button"
              accessibilityLabel={due ? `Due ${due}, change it` : "Set a due date"}
              style={({ pressed }) => [st.row, pressed && { opacity: 0.7 }]}
            >
              <View style={[st.avatar, { backgroundColor: C.surface2 }]}>
                <Icon
                  name="calendar"
                  size={14}
                  tintColor={due ? C.primary : C.textFaint}
                />
              </View>
              <Text style={[st.rowValue, !due && { color: C.textFaint }]}>
                {due ? dueDateLabel(due, now) : "No due date (optional)"}
              </Text>
              <Icon name="chevron.right" size={14} tintColor={C.textFaint} />
            </Pressable>

            <Text style={st.label}>Priority</Text>
            <View style={st.chipRow}>
              {PRIORITIES.map((p) => {
                const on = priority === p;
                return (
                  <Pressable
                    key={p}
                    onPress={() => setPriority(p)}
                    accessibilityRole="button"
                    accessibilityState={{ selected: on }}
                    style={({ pressed }) => [
                      st.chip,
                      on && { backgroundColor: C.primarySoft, borderColor: C.primary },
                      pressed && { opacity: 0.7 },
                    ]}
                  >
                    <Text style={[st.chipTxt, on && { color: C.primary }]}>{p}</Text>
                  </Pressable>
                );
              })}
            </View>

            <Text style={st.label}>Assignee</Text>
            <Pressable
              onPress={() => setPickerOpen(true)}
              disabled={!meeting}
              accessibilityRole="button"
              accessibilityLabel="Choose an assignee"
              style={({ pressed }) => [st.row, pressed && { opacity: 0.7 }]}
            >
              {assignee ? (
                <>
                  <View
                    style={[
                      st.avatar,
                      { backgroundColor: avatarColorFor(assignee.name) },
                    ]}
                  >
                    <Text style={st.avatarTxt}>{initialsOf(assignee.name)}</Text>
                  </View>
                  <Text style={st.rowValue} numberOfLines={1}>
                    {assignee.name}
                  </Text>
                </>
              ) : (
                <>
                  <View style={[st.avatar, { backgroundColor: C.surface2 }]}>
                    <Icon name="person.fill" size={14} tintColor={C.textFaint} />
                  </View>
                  <Text style={[st.rowValue, { color: C.textFaint }]}>
                    Unassigned — you can set this later
                  </Text>
                </>
              )}
              <Icon name="chevron.right" size={14} tintColor={C.textFaint} />
            </Pressable>

            {/* The meeting this task is filed against. Required by the API,
                so it is shown as a real choice rather than hidden. */}
            <Text style={st.label}>Meeting</Text>
            {loadingMeetings ? (
              <View style={st.row}>
                <ActivityIndicator color={C.primary} />
                <Text style={[st.rowValue, { color: C.textFaint }]}>
                  Loading meetings
                </Text>
              </View>
            ) : meeting ? (
              <Pressable
                onPress={() => setMeetingPickerOpen(true)}
                accessibilityRole="button"
                accessibilityLabel="Choose which meeting this task belongs to"
                style={({ pressed }) => [st.row, pressed && { opacity: 0.7 }]}
              >
                <View style={[st.avatar, { backgroundColor: C.accentSoft }]}>
                  <Icon name="waveform" size={14} tintColor={C.accent} />
                </View>
                <Text style={st.rowValue} numberOfLines={1}>
                  {meeting.title || "Untitled meeting"}
                </Text>
                <Icon name="chevron.right" size={14} tintColor={C.textFaint} />
              </Pressable>
            ) : (
              // No meetings, no create route. Say so rather than offering a
              // form that cannot submit.
              <Text style={st.empty}>
                Tasks are filed against a meeting, and this account has none
                yet. Record or upload a meeting first.
              </Text>
            )}

            {!!error && (
              <View style={{ marginTop: S.md }}>
                <ErrorText>{error}</ErrorText>
              </View>
            )}
          </ScrollView>

          <View style={st.footer}>
            <Button
              label={saving ? "Saving…" : "Add task"}
              onPress={submit}
              disabled={!canSubmit}
            />
          </View>
        </View>
      </KeyboardAwareSheet>

      <DueDatePicker
        visible={dueOpen}
        onClose={() => setDueOpen(false)}
        value={due}
        onChange={setDue}
      />

      <ContactPicker
        visible={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onPick={(c) => {
          setAssignee(c);
          setPickerOpen(false);
        }}
        meetingContacts={meetingContacts}
        title="Assign Task To"
      />

      {/* A plain list — the meeting choice is a one-tap correction, not a
          search problem, and the newest handful covers nearly every case. */}
      <Modal
        visible={meetingPickerOpen}
        transparent
        animationType="slide"
        onRequestClose={() => setMeetingPickerOpen(false)}
      >
        <Pressable
          style={st.scrim}
          onPress={() => setMeetingPickerOpen(false)}
          accessibilityRole="button"
          accessibilityLabel="Close meeting list"
        />
        <View style={[st.sheet, st.meetingSheet]}>
          <View style={st.grabber} />
          <Text style={[st.title, { marginBottom: S.md }]}>File under</Text>
          <ScrollView showsVerticalScrollIndicator={false}>
            {recordings.slice(0, 30).map((r) => {
              const on = r.audio_s3_key === meeting?.audio_s3_key;
              return (
                <Pressable
                  key={r.audio_s3_key}
                  onPress={() => {
                    setMeeting(r);
                    setMeetingPickerOpen(false);
                  }}
                  accessibilityRole="button"
                  accessibilityState={{ selected: on }}
                  style={({ pressed }) => [
                    st.meetingRow,
                    pressed && { opacity: 0.7 },
                  ]}
                >
                  <View style={{ flex: 1 }}>
                    <Text style={st.meetingTitle} numberOfLines={1}>
                      {r.title || "Untitled meeting"}
                    </Text>
                    <Text style={st.meetingSub}>
                      {String(r.recorded_at || "").slice(0, 10)}
                    </Text>
                  </View>
                  {on ? (
                    <Icon name="checkmark" size={15} tintColor={C.primary} />
                  ) : null}
                </Pressable>
              );
            })}
          </ScrollView>
        </View>
      </Modal>
    </>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    scrim: { flex: 1, backgroundColor: "rgba(0,0,0,0.35)" },
    sheet: {
      backgroundColor: C.surface,
      borderTopLeftRadius: R.xl,
      borderTopRightRadius: R.xl,
      paddingHorizontal: 20,
      paddingTop: S.sm,
      paddingBottom: S.xl,
      maxHeight: "88%",
    },
    meetingSheet: { maxHeight: "62%" },
    grabber: {
      alignSelf: "center",
      width: 36,
      height: 4,
      borderRadius: 2,
      backgroundColor: C.borderStrong,
      marginBottom: S.md,
    },
    head: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
    },
    title: { ...T.h1 },
    body: { marginTop: S.md },
    input: { minHeight: 74, textAlignVertical: "top" },
    label: {
      ...CAPS,
      fontFamily: FONT.bold,
      fontSize: 9.5,
      letterSpacing: 1.2,
      color: C.textFaint,
      marginTop: S.lg,
      marginBottom: S.sm,
    },
    chipRow: { flexDirection: "row", flexWrap: "wrap", gap: S.sm },
    chip: {
      borderRadius: R.pill,
      borderWidth: 1,
      borderColor: C.border,
      backgroundColor: C.surface,
      paddingHorizontal: 12,
      paddingVertical: 7,
    },
    chipTxt: { fontFamily: FONT.semibold, fontSize: 12, color: C.textDim },
    row: {
      flexDirection: "row",
      alignItems: "center",
      gap: S.md,
      backgroundColor: C.surface2,
      borderRadius: R.card,
      paddingHorizontal: S.md,
      paddingVertical: 11,
    },
    rowValue: {
      flex: 1,
      fontFamily: FONT.medium,
      fontSize: 13.5,
      color: C.text,
    },
    avatar: {
      width: 30,
      height: 30,
      borderRadius: 15,
      alignItems: "center",
      justifyContent: "center",
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 11.5, color: "#fff" },
    empty: {
      fontFamily: FONT.regular,
      fontSize: 12.5,
      lineHeight: 19,
      color: C.textFaint,
    },
    footer: { marginTop: S.lg },
    meetingRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: S.md,
      paddingVertical: 12,
      borderBottomWidth: 1,
      borderBottomColor: C.border,
    },
    meetingTitle: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    meetingSub: {
      fontFamily: FONT.regular,
      fontSize: 11.5,
      color: C.textFaint,
      marginTop: 2,
    },
  });
}
