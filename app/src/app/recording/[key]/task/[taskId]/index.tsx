// src/app/recording/[key]/task/[taskId]/index.tsx — Task Detail.
//
// Description, which meeting it came from, due date, priority, assignee, and
// a single "Assign Task" primary action (relabeled "Reassign" once someone is
// already assigned). Below: More Options (subtask / attachment / notes,
// expanded inline rather than as separate screens — none of the three need
// more room than a text field), a status stepper (Open -> In Progress ->
// Completed), and the task's own activity timeline.
import { useMemo, useState } from "react";
import {
  Alert, Modal, Pressable, ScrollView,
  StyleSheet, Text, TextInput, View,
} from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import * as DocumentPicker from "expo-document-picker";
import { FONT, R, S, useTheme, ColorScale } from "../../../../../../lib/theme";
import { Button, KeyboardAwareSheet, StatusPill } from "../../../../../../lib/ui";
import { Icon } from "../../../../../../lib/icons";
import { useMeeting } from "../../../../../../lib/meeting-context";
import { DueDatePicker, isPlottableDue } from "../../../../../../lib/due-date-picker";
import { initialsOf, nextId, type Task, type TaskStatus } from "../../../../../../lib/task-model";
import { Pop, PressSpring } from "../../../../../../lib/motion";

const STATUS_STEPS: TaskStatus[] = ["Open", "In Progress", "Completed"];
const PRIORITY_COLOR: Record<Task["priority"], (C: ColorScale) => string> = {
  High: (C) => C.danger,
  Medium: (C) => C.warn,
  Low: (C) => C.textFaint,
};

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    body: { paddingHorizontal: 20, paddingTop: S.lg, paddingBottom: 60 },
    titleRow: { flexDirection: "row" as const, alignItems: "flex-start" as const, gap: S.md },
    checkbox: { marginTop: 2 },
    title: { fontFamily: FONT.extrabold, fontSize: 20, lineHeight: 26, color: C.text, flex: 1 },
    titleDone: { textDecorationLine: "line-through" as const, color: C.textFaint },
    descLabel: { fontFamily: FONT.semibold, fontSize: 11, letterSpacing: 0.6, textTransform: "uppercase" as const, color: C.textFaint, marginTop: S.xl },
    descTxt: { fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: C.textDim, marginTop: 6 },
    row: {
      flexDirection: "row" as const, alignItems: "center" as const, justifyContent: "space-between" as const,
      paddingVertical: 13, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    rowLabel: { fontFamily: FONT.medium, fontSize: 14, color: C.textDim },
    rowValue: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    linkChip: { flexDirection: "row" as const, alignItems: "center" as const, gap: 6 },
    stepperTrack: { flexDirection: "row" as const, alignItems: "center" as const, marginTop: S.md },
    stepDot: { width: 26, height: 26, borderRadius: 13, alignItems: "center" as const, justifyContent: "center" as const },
    stepLine: { flex: 1, height: 2 },
    stepLabel: { fontFamily: FONT.semibold, fontSize: 11, marginTop: 6, textAlign: "center" as const },
    moreRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      paddingVertical: 14, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    moreTxt: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text, flex: 1 },
    subtaskRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm, paddingVertical: 9 },
    subtaskTxt: { fontFamily: FONT.regular, fontSize: 14, color: C.text, flex: 1 },
    subtaskDone: { textDecorationLine: "line-through" as const, color: C.textFaint },
    addInput: {
      flex: 1, fontFamily: FONT.regular, fontSize: 14, color: C.text,
      backgroundColor: C.surface2, borderRadius: R.md, paddingHorizontal: 12, paddingVertical: 10,
    },
    notesInput: {
      backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      paddingHorizontal: 14, paddingVertical: 12, fontFamily: FONT.regular, fontSize: 14, color: C.text,
      minHeight: 90, marginTop: 8, textAlignVertical: "top" as const,
    },
    activityRow: { flexDirection: "row" as const, gap: 10, marginTop: 12 },
    activityDot: { width: 7, height: 7, borderRadius: 3.5, backgroundColor: undefined, marginTop: 6 },
    sheetBackdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" as const },
    sheet: {
      backgroundColor: C.surface, paddingHorizontal: 22, paddingTop: 20, paddingBottom: 32,
      borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl,
    },
    sheetTitle: { fontFamily: FONT.extrabold, fontSize: 19, color: C.text },
    editInput: {
      backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      fontFamily: FONT.regular, fontSize: 16, color: C.text, paddingVertical: 12, paddingHorizontal: 14, marginTop: 16,
    },
    editableRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 6 },
  });
}

export default function TaskDetailScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ key: string | string[]; taskId: string }>();
  const key = Array.isArray(params.key) ? params.key.join("/") : (params.key ?? "");
  const taskId = params.taskId;
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const {
    rec, getTask, setTaskStatus, addSubtask, toggleSubtask,
    addAttachment, removeAttachment, updateTask,
  } = useMeeting();

  const task = getTask(taskId);

  const [showSubtaskInput, setShowSubtaskInput] = useState(false);
  const [subtaskDraft, setSubtaskDraft] = useState("");
  const [attaching, setAttaching] = useState(false);
  const [showNotes, setShowNotes] = useState(false);
  const [notesDraft, setNotesDraft] = useState(task?.notes ?? "");
  const [statusPickerOpen, setStatusPickerOpen] = useState(false);
  const [priorityPickerOpen, setPriorityPickerOpen] = useState(false);
  const [editingField, setEditingField] = useState<"title" | "description" | null>(null);
  const [dueOpen, setDueOpen] = useState(false);
  const [editDraft, setEditDraft] = useState("");

  if (!task) {
    return (
      <View style={[st.container, { alignItems: "center", justifyContent: "center", padding: 20 }]}>
        <Stack.Screen options={{ title: "Task Detail" }} />
        <Text style={T.body}>This task couldn&apos;t be found.</Text>
        <Button label="Go back" variant="secondary" onPress={() => router.back()} style={{ marginTop: 16, alignSelf: "stretch" }} />
      </View>
    );
  }

  const done = task.status === "Completed";
  const stepIndex = STATUS_STEPS.indexOf(task.status);

  const toggleDone = () => setTaskStatus(task.id, done ? "Open" : "Completed");

  // Text fields only. The due date is PICKED (DueDatePicker) rather than typed
  // — dropping it from this union is what stops a future caller quietly
  // reintroducing a free-text date the calendar cannot plot.
  const openEdit = (field: "title" | "description") => {
    setEditDraft(field === "title" ? task.task : task.description);
    setEditingField(field);
  };
  const saveEdit = () => {
    if (!editingField) return;
    const trimmed = editDraft.trim();
    if (editingField === "title") updateTask(task.id, trimmed ? { task: trimmed } : {});
    else updateTask(task.id, { description: trimmed });
    setEditingField(null);
  };

  const setPriority = (priority: Task["priority"]) => {
    updateTask(task.id, { priority });
    setPriorityPickerOpen(false);
  };

  const saveNotes = () => {
    updateTask(task.id, { notes: notesDraft });
    setShowNotes(false);
  };

  const submitSubtask = () => {
    const trimmed = subtaskDraft.trim();
    if (!trimmed) return;
    addSubtask(task.id, trimmed);
    setSubtaskDraft("");
    setShowSubtaskInput(false);
  };

  // Picks a REAL file from the device. There's no backend endpoint to store
  // a task attachment anywhere (only audio recordings have an upload
  // pipeline — see lib/uploads.tsx), so the file's local URI is kept for
  // this session only: real name/size/type, but nothing is uploaded and it
  // won't survive a reload or reach anyone else. Confirmed with the user
  // this tradeoff is acceptable rather than faking a name-only attachment.
  const pickAttachment = async () => {
    setAttaching(true);
    try {
      const result = await DocumentPicker.getDocumentAsync({
        type: "*/*",
        multiple: false,
        copyToCacheDirectory: true,
      });
      if (result.canceled || !result.assets?.length) return;
      const file = result.assets[0];
      addAttachment(task.id, {
        id: nextId("att"),
        name: file.name,
        size: file.size ?? null,
        mimeType: file.mimeType ?? null,
        uri: file.uri,
      });
    } catch {
      Alert.alert("Couldn't add attachment", "Something went wrong picking that file.");
    } finally {
      setAttaching(false);
    }
  };

  const confirmRemoveAttachment = (attachmentId: string, name: string) => {
    Alert.alert("Remove attachment", `Remove "${name}" from this task?`, [
      { text: "Cancel", style: "cancel" },
      { text: "Remove", style: "destructive", onPress: () => removeAttachment(task.id, attachmentId) },
    ]);
  };

  const fmtSize = (bytes: number | null) => {
    if (bytes == null) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Task Detail" }} />
      <ScrollView contentContainerStyle={st.body} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled">
        <View style={st.titleRow}>
          <PressSpring onPress={toggleDone} hitSlop={8} style={st.checkbox} accessibilityLabel={done ? "Mark incomplete" : "Mark complete"}>
            <Icon name={done ? "checkmark.square.fill" : "square"} tintColor={done ? C.success : C.textFaint} size={26} />
          </PressSpring>
          <Pressable onPress={() => openEdit("title")} style={{ flex: 1 }} accessibilityLabel="Edit title">
            <Text style={[st.title, done && st.titleDone]}>{task.task}</Text>
          </Pressable>
          <Icon name="pencil" tintColor={C.textFaint} size={15} />
        </View>

        <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginTop: S.xl }}>
          <Text style={st.descLabel}>Description</Text>
        </View>
        <Pressable onPress={() => openEdit("description")} accessibilityLabel="Edit description">
          <Text style={st.descTxt}>{task.description || "Add a description…"}</Text>
        </Pressable>

        <View style={{ marginTop: S.xl }}>
          <View style={st.row}>
            <Text style={st.rowLabel}>Meeting</Text>
            <Pressable
              onPress={() => router.push({ pathname: "/recording/[key]", params: { key } })}
              style={st.linkChip}
              accessibilityLabel="Open meeting"
            >
              <Text style={[st.rowValue, { color: C.primary }]} numberOfLines={1}>{rec?.title || task.meetingTitle}</Text>
              <Icon name="chevron.right" tintColor={C.primary} size={13} />
            </Pressable>
          </View>

          <Pressable style={st.row} onPress={() => setDueOpen(true)} accessibilityLabel="Edit due date">
            <Text style={st.rowLabel}>Due Date</Text>
            <View style={st.linkChip}>
              <Icon name="calendar" tintColor={C.textFaint} size={14} />
              <Text style={st.rowValue}>{task.due || "No due date"}</Text>
              <Icon name="pencil" tintColor={C.textFaint} size={12} />
            </View>
          </Pressable>

          <Pressable style={st.row} onPress={() => setPriorityPickerOpen(true)} accessibilityLabel="Edit priority">
            <Text style={st.rowLabel}>Priority</Text>
            <View style={st.linkChip}>
              <StatusPill label={task.priority} color={PRIORITY_COLOR[task.priority](C)} />
              <Icon name="chevron.right" tintColor={C.textFaint} size={13} />
            </View>
          </Pressable>

          <View style={[st.row, { borderBottomWidth: 0 }]}>
            <Text style={st.rowLabel}>Assignee</Text>
            <Pressable
              onPress={() => router.push({ pathname: "/recording/[key]/task/[taskId]/assign", params: { key, taskId } })}
              style={st.linkChip}
              accessibilityLabel="Assign task"
            >
              {task.assignee ? (
                <>
                  <View style={{
                    width: 22, height: 22, borderRadius: 11, backgroundColor: task.assignee.avatarColor,
                    alignItems: "center", justifyContent: "center",
                  }}>
                    <Text style={{ fontFamily: FONT.bold, fontSize: 10, color: "#FFFFFF" }}>{initialsOf(task.assignee.name)}</Text>
                  </View>
                  <Text style={st.rowValue}>{task.assignee.name}</Text>
                </>
              ) : (
                <Text style={[st.rowValue, { color: C.primary }]}>Unassigned</Text>
              )}
              <Icon name="chevron.right" tintColor={C.textFaint} size={13} />
            </Pressable>
          </View>
        </View>

        <Button
          label={task.assignee ? "Reassign Task" : "Assign Task"}
          onPress={() => router.push({ pathname: "/recording/[key]/task/[taskId]/assign", params: { key, taskId } })}
          style={{ marginTop: S.xl }}
        />

        {/* ---- Status stepper ---- */}
        <Text style={[st.descLabel, { marginTop: S.xxl }]}>Status</Text>
        <PressSpring onPress={() => setStatusPickerOpen(true)} style={st.stepperTrack} accessibilityLabel="Change status">
          {STATUS_STEPS.map((s, i) => (
            <View key={s} style={{ flex: i === STATUS_STEPS.length - 1 ? 0 : 1, alignItems: "center" }}>
              <View style={{ flexDirection: "row", alignItems: "center", alignSelf: "stretch" }}>
                {i <= stepIndex ? (
                  <Pop key={`${s}-${stepIndex}`} style={st.stepDot}>
                    <View style={[st.stepDot, { backgroundColor: C.primary, position: "absolute" }]}>
                      {i < stepIndex ? <Icon name="checkmark" tintColor="#FFFFFF" size={12} /> : (
                        <Text style={{ fontFamily: FONT.bold, fontSize: 11, color: "#FFFFFF" }}>{i + 1}</Text>
                      )}
                    </View>
                  </Pop>
                ) : (
                  <View style={[st.stepDot, { backgroundColor: C.surface2 }]}>
                    <Text style={{ fontFamily: FONT.bold, fontSize: 11, color: C.textFaint }}>{i + 1}</Text>
                  </View>
                )}
                {i < STATUS_STEPS.length - 1 ? (
                  <View style={[st.stepLine, { backgroundColor: i < stepIndex ? C.primary : C.border }]} />
                ) : null}
              </View>
              <Text style={[st.stepLabel, { color: i <= stepIndex ? C.text : C.textFaint }]}>{s}</Text>
            </View>
          ))}
        </PressSpring>

        {/* ---- More Options ---- */}
        <Text style={[st.descLabel, { marginTop: S.xxl }]}>More Options</Text>
        <View style={{ marginTop: 4 }}>
          <Pressable style={st.moreRow} onPress={() => setShowSubtaskInput((v) => !v)} accessibilityLabel="Add subtask">
            <Icon name="plus" tintColor={C.textDim} size={17} />
            <Text style={st.moreTxt}>Add Subtask</Text>
            <Icon name={showSubtaskInput ? "chevron.up" : "chevron.right"} tintColor={C.textFaint} size={14} />
          </Pressable>
          {showSubtaskInput ? (
            <View style={{ flexDirection: "row", gap: S.sm, paddingVertical: 10 }}>
              <TextInput
                style={st.addInput}
                value={subtaskDraft}
                onChangeText={setSubtaskDraft}
                placeholder="Subtask title"
                placeholderTextColor={C.textFaint}
                autoFocus
                returnKeyType="done"
                onSubmitEditing={submitSubtask}
              />
              <Button label="Add" onPress={submitSubtask} style={{ paddingHorizontal: 18 }} disabled={!subtaskDraft.trim()} />
            </View>
          ) : null}
          {task.subtasks.map((s) => (
            <Pressable key={s.id} style={st.subtaskRow} onPress={() => toggleSubtask(task.id, s.id)}>
              <Icon name={s.done ? "checkmark.square.fill" : "square"} tintColor={s.done ? C.success : C.textFaint} size={17} />
              <Text style={[st.subtaskTxt, s.done && st.subtaskDone]}>{s.title}</Text>
            </Pressable>
          ))}

          <Pressable style={st.moreRow} onPress={pickAttachment} disabled={attaching} accessibilityLabel="Add attachment">
            <Icon name="doc.text.fill" tintColor={C.textDim} size={17} />
            <Text style={st.moreTxt}>{attaching ? "Choosing…" : "Add Attachment"}</Text>
            <Icon name="chevron.right" tintColor={C.textFaint} size={14} />
          </Pressable>
          {task.attachments.map((a) => (
            <Pressable key={a.id} style={st.subtaskRow} onLongPress={() => confirmRemoveAttachment(a.id, a.name)}>
              <Icon name="doc.richtext" tintColor={C.textFaint} size={16} />
              <View style={{ flex: 1 }}>
                <Text style={st.subtaskTxt} numberOfLines={1}>{a.name}</Text>
                {a.size != null ? <Text style={{ fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint }}>{fmtSize(a.size)}</Text> : null}
              </View>
              <Pressable onPress={() => confirmRemoveAttachment(a.id, a.name)} hitSlop={8} accessibilityLabel={`Remove ${a.name}`}>
                <Icon name="trash" tintColor={C.textFaint} size={15} />
              </Pressable>
            </Pressable>
          ))}

          <Pressable style={[st.moreRow, { borderBottomWidth: 0 }]} onPress={() => setShowNotes((v) => !v)} accessibilityLabel="Add notes">
            <Icon name="square.and.pencil" tintColor={C.textDim} size={17} />
            <Text style={st.moreTxt}>{task.notes ? "Edit Notes" : "Add Notes"}</Text>
            <Icon name={showNotes ? "chevron.up" : "chevron.right"} tintColor={C.textFaint} size={14} />
          </Pressable>
          {showNotes ? (
            <View>
              <TextInput
                style={st.notesInput}
                value={notesDraft}
                onChangeText={setNotesDraft}
                placeholder="Add notes about this task…"
                placeholderTextColor={C.textFaint}
                multiline
                autoFocus
              />
              <Button label="Save Notes" onPress={saveNotes} style={{ marginTop: 8 }} />
            </View>
          ) : task.notes ? (
            <Text style={[st.descTxt, { marginTop: 6 }]}>{task.notes}</Text>
          ) : null}
        </View>

        {/* ---- Activity timeline ---- */}
        <Text style={[st.descLabel, { marginTop: S.xxl }]}>Activity Timeline</Text>
        <View style={{ marginTop: 4 }}>
          {task.activity.map((a) => (
            <View key={a.id} style={st.activityRow}>
              <View style={{ width: 7, height: 7, borderRadius: 3.5, backgroundColor: C.primary, marginTop: 6 }} />
              <View style={{ flex: 1 }}>
                <Text style={{ fontFamily: FONT.medium, fontSize: 13.5, color: C.text }}>{a.text}</Text>
                <Text style={[T.caption, { marginTop: 2 }]}>
                  {new Date(a.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
                </Text>
              </View>
            </View>
          ))}
        </View>
      </ScrollView>

      <Modal visible={statusPickerOpen} transparent animationType="fade" onRequestClose={() => setStatusPickerOpen(false)}>
        <KeyboardAwareSheet>
          <Pressable style={st.sheetBackdrop} onPress={() => setStatusPickerOpen(false)}>
            <Pressable style={st.sheet} onPress={() => {}}>
              <Text style={{ fontFamily: FONT.extrabold, fontSize: 19, color: C.text }}>Change status</Text>
              {STATUS_STEPS.map((s) => (
                <Pressable
                  key={s}
                  onPress={() => { setTaskStatus(task.id, s); setStatusPickerOpen(false); }}
                  style={{
                    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
                    paddingVertical: 15, borderBottomWidth: s === "Completed" ? 0 : 1, borderBottomColor: C.border,
                  }}
                >
                  <Text style={{ fontFamily: FONT.semibold, fontSize: 15, color: C.text }}>{s}</Text>
                  {task.status === s ? <Icon name="checkmark" tintColor={C.primary} size={18} /> : null}
                </Pressable>
              ))}
            </Pressable>
          </Pressable>
        </KeyboardAwareSheet>
      </Modal>

      <Modal visible={priorityPickerOpen} transparent animationType="fade" onRequestClose={() => setPriorityPickerOpen(false)}>
        <Pressable style={st.sheetBackdrop} onPress={() => setPriorityPickerOpen(false)}>
          <Pressable style={st.sheet} onPress={() => {}}>
            <Text style={st.sheetTitle}>Change priority</Text>
            {(["High", "Medium", "Low"] as Task["priority"][]).map((p) => (
              <Pressable
                key={p}
                onPress={() => setPriority(p)}
                style={{
                  flexDirection: "row", alignItems: "center", justifyContent: "space-between",
                  paddingVertical: 15, borderBottomWidth: p === "Low" ? 0 : 1, borderBottomColor: C.border,
                }}
              >
                <StatusPill label={p} color={PRIORITY_COLOR[p](C)} />
                {task.priority === p ? <Icon name="checkmark" tintColor={C.primary} size={18} /> : null}
              </Pressable>
            ))}
          </Pressable>
        </Pressable>
      </Modal>

      {/* Due dates are PICKED, never typed. update_meeting_task stores
          due_date verbatim, so free text ("next Friday") would persist and
          then fail to plot on /calendar — see lib/due-date-picker.tsx. */}
      <DueDatePicker
        visible={dueOpen}
        onClose={() => setDueOpen(false)}
        value={isPlottableDue(task.due) ? task.due : ""}
        onChange={(dayKey) => updateTask(task.id, { due: dayKey })}
      />

      <Modal visible={editingField !== null} transparent animationType="fade" onRequestClose={() => setEditingField(null)}>
        <KeyboardAwareSheet>
          <Pressable style={st.sheetBackdrop} onPress={() => setEditingField(null)}>
            <Pressable style={st.sheet} onPress={() => {}}>
              <Text style={st.sheetTitle}>
                {editingField === "title" ? "Edit title" : "Edit description"}
              </Text>
              <TextInput
                style={st.editInput}
                value={editDraft}
                onChangeText={setEditDraft}
                autoFocus
                multiline={editingField === "description"}
                placeholderTextColor={C.textFaint}
                returnKeyType={editingField === "description" ? "default" : "done"}
                onSubmitEditing={editingField === "description" ? undefined : saveEdit}
              />
              <View style={{ flexDirection: "row", gap: S.sm, marginTop: 20 }}>
                <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={() => setEditingField(null)} />
                <Button label="Save" style={{ flex: 1 }} onPress={saveEdit} />
              </View>
            </Pressable>
          </Pressable>
        </KeyboardAwareSheet>
      </Modal>
    </View>
  );
}
