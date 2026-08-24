// lib/meeting-tasks.tsx — Overview/Assistant Tasks section: the top 3 tasks
// with a checkbox, assignee, due date and priority, plus "View all" to the
// full Tasks list. Tapping a row opens Task Detail — assignment, notes,
// subtasks, status and the activity timeline all live there now, not here.
//
// Task completion (via the checkbox) still works inline for a fast "done"
// tap, but it's the same setTaskStatus mutator Task Detail uses — see
// lib/meeting-context.tsx — so status stays consistent everywhere it's shown.
import { useMemo } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { FONT, S, useTheme, ColorScale } from "./theme";
import { Card, SectionRule, StatusPill } from "./ui";
import { Icon } from "./icons";
import { useMeeting } from "./meeting-context";
import { initialsOf, type Task } from "./task-model";
import { PressSpring } from "./motion";

const VISIBLE_CAP = 3;

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    // Empty-state card: a tappable row rather than a bare message, so the
    // section that says "nothing here" is also the way to fix that.
    emptyCard: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      backgroundColor: C.surface, borderRadius: 14, borderWidth: 1,
      borderColor: C.border, paddingHorizontal: 14, paddingVertical: 13,
    },
    emptyTitle: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.text },
    emptySub: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      marginTop: 2, lineHeight: 16,
    },
    emptyCta: { fontFamily: FONT.bold, fontSize: 12.5, color: C.primary },
    addRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      alignSelf: "flex-start" as const, paddingVertical: 10, paddingHorizontal: 2,
    },
    addTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary },
    row: {
      flexDirection: "row" as const, alignItems: "flex-start" as const, gap: S.md,
      paddingVertical: 12,
    },
    checkbox: { marginTop: 1 },
    title: { fontFamily: FONT.semibold, fontSize: 14.5, lineHeight: 20, color: C.text, flex: 1 },
    titleDone: { textDecorationLine: "line-through" as const, color: C.textFaint },
    metaRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm, marginTop: 5, flexWrap: "wrap" as const },
    ownerChip: { flexDirection: "row" as const, alignItems: "center" as const, gap: 5 },
    ownerAvatar: {
      width: 18, height: 18, borderRadius: 9, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    ownerTxt: { fontFamily: FONT.medium, fontSize: 12, color: C.textDim },
    dueTxt: { fontFamily: FONT.medium, fontSize: 12, color: C.textFaint },
  });
}

const PRIORITY_COLOR: Record<Task["priority"], (C: ColorScale) => string> = {
  High: (C) => C.danger,
  Medium: (C) => C.warn,
  Low: (C) => C.textFaint,
};

export function Tasks({
  tasks, onOpenTask, onViewAll, onAddTask,
}: {
  tasks: Task[];
  onOpenTask: (id: string) => void;
  onViewAll: () => void;
  /** Omitted on read-only surfaces; the add affordance is then hidden. */
  onAddTask?: () => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { setTaskStatus } = useMeeting();

  // An empty task list used to render NOTHING — the section simply vanished.
  // That is indistinguishable from a bug: on a meeting where the AI found no
  // commitments (a short call, or a recording with no usable transcript) the
  // user saw no Tasks section at all and reasonably concluded tasks were
  // broken. Say so instead, and offer the way forward.
  if (!tasks.length) {
    if (!onAddTask) return null;
    return (
      <View>
        <SectionRule>Tasks</SectionRule>
        <Pressable
          style={st.emptyCard}
          onPress={onAddTask}
          accessibilityRole="button"
          accessibilityLabel="Add a task"
        >
          <Icon name="checklist" size={18} tintColor={C.textFaint} />
          <View style={{ flex: 1 }}>
            <Text style={st.emptyTitle}>No tasks from this meeting</Text>
            <Text style={st.emptySub}>
              MinuteX never invents a task. If something was agreed, add it.
            </Text>
          </View>
          <Text style={st.emptyCta}>Add</Text>
        </Pressable>
      </View>
    );
  }
  const visible = tasks.slice(0, VISIBLE_CAP);

  const toggle = (t: Task) => {
    setTaskStatus(t.id, t.status === "Completed" ? "Open" : "Completed");
  };

  return (
    <View>
      <SectionRule
        right={tasks.length > VISIBLE_CAP ? (
          <Pressable onPress={onViewAll} hitSlop={6}>
            <Text style={{ fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary }}>View all</Text>
          </Pressable>
        ) : <Icon name="checkmark.circle" tintColor={C.success} size={16} />}
      >
        Tasks ({tasks.length})
      </SectionRule>
      <Card style={{ paddingVertical: S.xs }}>
        {visible.map((t, i) => {
          const done = t.status === "Completed";
          return (
            <Pressable
              key={t.id}
              onPress={() => onOpenTask(t.id)}
              style={({ pressed }) => [
                st.row, i > 0 && { borderTopWidth: 1, borderTopColor: C.border },
                pressed && { opacity: 0.7 },
              ]}
              accessibilityLabel={t.task}
            >
              <PressSpring onPress={() => toggle(t)} hitSlop={8} style={st.checkbox} accessibilityLabel={done ? "Mark incomplete" : "Mark complete"}>
                <Icon
                  name={done ? "checkmark.square.fill" : "square"}
                  tintColor={done ? C.success : C.textFaint}
                  size={21}
                />
              </PressSpring>
              <View style={{ flex: 1 }}>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
                  <Text style={[st.title, done && st.titleDone]} numberOfLines={1}>{t.task}</Text>
                  <Icon name="chevron.right" tintColor={C.textFaint} size={14} />
                </View>
                <View style={st.metaRow}>
                  {t.assignee ? (
                    <View style={st.ownerChip}>
                      <View style={[st.ownerAvatar, { backgroundColor: t.assignee.avatarColor }]}>
                        <Text style={{ fontFamily: FONT.bold, fontSize: 9.5, color: "#FFFFFF" }}>
                          {initialsOf(t.assignee.name)}
                        </Text>
                      </View>
                      <Text style={st.ownerTxt}>{t.assignee.name}</Text>
                    </View>
                  ) : (
                    <Text style={st.dueTxt}>Unassigned</Text>
                  )}
                  {t.due ? <Text style={st.dueTxt}>{t.due}</Text> : null}
                  {!done ? <StatusPill label={t.priority} color={PRIORITY_COLOR[t.priority](C)} /> : null}
                </View>
              </View>
            </Pressable>
          );
        })}
      </Card>
      {!!onAddTask && (
        <Pressable
          onPress={onAddTask}
          hitSlop={6}
          style={st.addRow}
          accessibilityRole="button"
          accessibilityLabel="Add a task the AI missed"
        >
          <Icon name="plus" size={13} tintColor={C.primary} />
          <Text style={st.addTxt}>Add a task</Text>
        </Pressable>
      )}
    </View>
  );
}
