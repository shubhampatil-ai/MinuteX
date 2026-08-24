// lib/add-task-sheet.tsx — add a task the AI didn't catch.
//
// WHY THIS HAS TO EXIST. The AI is deliberately conservative: prompts.
// SUMMARY_SYSTEM forbids inventing a task or inferring an owner, so it misses
// real commitments rather than guessing at them. And on a short, quiet or
// failed recording it extracts nothing at all — several meetings on this
// account are titled "Insufficient content" with an empty transcript.
//
// Until now that left the person who recorded the meeting with no way to write
// a task down. createTask existed in lib/api.ts and was even imported into
// meeting-context.tsx, but nothing ever called it: there was no button,
// anywhere. So a task the AI missed was simply lost.
//
// Deliberately small. Description is the only required field, because the
// common case is "note this before I forget" and anything else standing between
// the user and a saved task is a reason not to bother. Assignee, due date and
// priority are all optional and can be set later from Task Detail.
import { useEffect, useMemo, useState } from "react";
import {
  Modal, Pressable, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Icon } from "./icons";
import { S, R, CAPS, FONT, useTheme, ColorScale } from "./theme";
import {
  Button, ErrorText, KeyboardAwareSheet, TextField, scrollFormProps,
} from "./ui";
import { ContactPicker } from "./contact-picker";
import { ApiContact, getParticipants } from "./api";
import { avatarColorFor, initialsOf, type Task } from "./task-model";

const PRIORITIES: Task["priority"][] = ["Low", "Medium", "High"];

export type AddTaskSheetProps = {
  visible: boolean;
  onClose: () => void;
  /** The meeting this task belongs to — used to rank the assignee picker. */
  recordingKey: string;
  onSubmit: (input: {
    task: string;
    due?: string;
    priority?: Task["priority"];
    assigneeContactId?: string;
  }) => Promise<unknown>;
};

export function AddTaskSheet({
  visible, onClose, recordingKey, onSubmit,
}: AddTaskSheetProps) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);

  const [text, setText] = useState("");
  const [due, setDue] = useState("");
  const [priority, setPriority] = useState<Task["priority"]>("Medium");
  const [assignee, setAssignee] = useState<ApiContact | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [meetingContacts, setMeetingContacts] = useState<ApiContact[]>([]);
  const [folderContacts, setFolderContacts] = useState<ApiContact[]>([]);
  const [folderId, setFolderId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // Reset on open so a half-typed task from a previous open never reappears
  // attached to a different meeting.
  useEffect(() => {
    if (!visible) return;
    setText("");
    setDue("");
    setPriority("Medium");
    setAssignee(null);
    setPickerOpen(false);
    setError("");

    // Ranking context for the assignee picker: who is tagged in this meeting,
    // then this folder. Best-effort — a failure only affects ORDERING, and the
    // picker still lists every contact.
    let alive = true;
    getParticipants(recordingKey)
      .then((p) => {
        if (!alive) return;
        setMeetingContacts(
          p.participants
            .map((x) => x.contact)
            .filter((c): c is ApiContact => !!c)
        );
        setFolderContacts(p.folder_contacts);
        setFolderId(p.folder_id);
      })
      .catch(() => {
        if (alive) setMeetingContacts([]);
      });
    return () => { alive = false; };
  }, [visible, recordingKey]);

  const trimmed = text.trim();

  const save = async () => {
    if (!trimmed) {
      setError("Describe the task.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await onSubmit({
        task: trimmed,
        due: due.trim() || undefined,
        priority,
        assigneeContactId: assignee?.id,
      });
      onClose();
    } catch {
      // meeting-context has already alerted with the reason; keep the sheet
      // open so the typed text is not lost.
      setError("Could not add the task. Your text is still here — try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent
      onRequestClose={onClose}
    >
      <KeyboardAwareSheet>
        <View style={st.backdrop}>
          <Pressable style={st.backdropTap} onPress={onClose} />
          <View style={st.sheet}>
            <View style={st.head}>
              <Text style={st.title}>Add task</Text>
              <Pressable
                onPress={onClose}
                hitSlop={14}
                accessibilityRole="button"
                accessibilityLabel="Close"
              >
                <Icon name="xmark" size={19} tintColor={C.textDim} />
              </Pressable>
            </View>
            <Text style={st.blurb}>
              For something that was agreed but the AI didn&apos;t pick up.
            </Text>

            <ScrollView {...scrollFormProps}>
              <Text style={st.label}>Task</Text>
              <TextField
                value={text}
                onChangeText={setText}
                placeholder="What needs doing?"
                autoFocus
                multiline
                maxLength={300}
                style={st.textArea}
              />

              <Text style={st.label}>Assign to</Text>
              <Pressable
                style={st.assigneeRow}
                onPress={() => setPickerOpen(true)}
                accessibilityRole="button"
                accessibilityLabel={
                  assignee ? `Assigned to ${assignee.name}. Change` : "Choose a contact"
                }
              >
                {assignee ? (
                  <>
                    <View
                      style={[st.avatar, { backgroundColor: avatarColorFor(assignee.name) }]}
                    >
                      <Text style={st.avatarTxt}>{initialsOf(assignee.name)}</Text>
                    </View>
                    <Text style={st.assigneeName}>{assignee.name}</Text>
                    <Text style={st.change}>Change</Text>
                  </>
                ) : (
                  <>
                    <View style={[st.avatar, { backgroundColor: C.surface2 }]}>
                      <Icon name="person.2.fill" size={16} tintColor={C.textFaint} />
                    </View>
                    <Text style={st.assigneePlaceholder}>
                      Nobody yet (optional)
                    </Text>
                    <Icon name="chevron.right" size={15} tintColor={C.textFaint} />
                  </>
                )}
              </Pressable>

              <Text style={st.label}>Due</Text>
              <TextField
                value={due}
                onChangeText={setDue}
                placeholder="e.g. Friday, or 2026-08-29 (optional)"
                maxLength={100}
              />

              <Text style={st.label}>Priority</Text>
              <View style={st.priorityRow}>
                {PRIORITIES.map((p) => {
                  const on = p === priority;
                  return (
                    <Pressable
                      key={p}
                      onPress={() => setPriority(p)}
                      style={[
                        st.priorityChip,
                        on && {
                          backgroundColor: C.primarySoft,
                          borderColor: C.primary,
                        },
                      ]}
                      accessibilityRole="button"
                      accessibilityState={{ selected: on }}
                    >
                      <Text
                        style={[st.priorityTxt, on && { color: C.primary }]}
                      >
                        {p}
                      </Text>
                    </Pressable>
                  );
                })}
              </View>

              {!!error && (
                <View style={{ marginTop: S.md }}>
                  <ErrorText>{error}</ErrorText>
                </View>
              )}

              <View style={st.cta}>
                <Button
                  label="Add task"
                  onPress={save}
                  loading={saving}
                  // An empty task is the one thing the backend refuses, so
                  // don't offer a button that must fail.
                  disabled={saving || !trimmed}
                />
              </View>
            </ScrollView>
          </View>
        </View>
      </KeyboardAwareSheet>

      {/* Sibling, not nested: a Modal inside a Modal renders behind on
          Android. The add-task sheet stays open underneath, so cancelling the
          picker returns to the half-filled form rather than losing it. */}
      <ContactPicker
        visible={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onPick={(c) => {
          setAssignee(c);
          setPickerOpen(false);
        }}
        onClear={assignee ? () => setAssignee(null) : undefined}
        meetingContacts={meetingContacts}
        folderContacts={folderContacts}
        folderId={folderId}
        title="Assign Task To"
      />
    </Modal>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)" },
    backdropTap: { flex: 1 },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.xl,
      borderTopRightRadius: R.xl, paddingHorizontal: 22, paddingTop: S.lg,
      paddingBottom: S.xxl, maxHeight: "88%", flexShrink: 1,
    },
    head: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const,
    },
    title: { ...T.headline, fontSize: 23 },
    blurb: { ...T.caption, marginTop: 4, marginBottom: S.md },
    label: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.2,
      color: C.textFaint, marginTop: S.md, marginBottom: 6,
    },
    // Tall enough that a two-line commitment is visible while typing — the
    // usual case is a sentence, not a word.
    textArea: { minHeight: 74, textAlignVertical: "top" as const },
    assigneeRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      borderWidth: 1, borderColor: C.border, borderRadius: R.md,
      paddingHorizontal: 12, paddingVertical: 10,
      backgroundColor: C.surface2,
    },
    avatar: {
      width: 32, height: 32, borderRadius: 16,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 12, color: "#fff" },
    assigneeName: {
      fontFamily: FONT.semibold, fontSize: 14, color: C.text, flex: 1,
    },
    assigneePlaceholder: {
      fontFamily: FONT.regular, fontSize: 14, color: C.textFaint, flex: 1,
    },
    change: { fontFamily: FONT.bold, fontSize: 12.5, color: C.primary },
    priorityRow: { flexDirection: "row" as const, gap: S.sm },
    priorityChip: {
      flex: 1, alignItems: "center" as const, paddingVertical: 10,
      borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      backgroundColor: C.surface,
    },
    priorityTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.textDim },
    cta: { marginTop: S.lg },
  });
}
