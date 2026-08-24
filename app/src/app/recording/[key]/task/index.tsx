// src/app/recording/[key]/task/index.tsx — the full Tasks list ("View all"
// from Overview/Assistant's top-3 preview). Same row treatment as the
// preview, just every task and grouped by status so a long list stays
// scannable.
import { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { FONT, S, useTheme, ColorScale } from "../../../../../lib/theme";
import { Button, Card, EmptyState, StatusPill } from "../../../../../lib/ui";
import { Icon } from "../../../../../lib/icons";
import { useMeeting } from "../../../../../lib/meeting-context";
import { initialsOf, type Task } from "../../../../../lib/task-model";
import { AddTaskSheet } from "../../../../../lib/add-task-sheet";

const PRIORITY_COLOR: Record<Task["priority"], (C: ColorScale) => string> = {
  High: (C) => C.danger,
  Medium: (C) => C.warn,
  Low: (C) => C.textFaint,
};

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    body: { paddingHorizontal: 20, paddingTop: S.lg, paddingBottom: 60 },
    groupLabel: {
      fontFamily: FONT.semibold, fontSize: 11, letterSpacing: 0.6, textTransform: "uppercase" as const,
      color: C.textFaint, marginTop: S.xl, marginBottom: 8,
    },
    row: {
      flexDirection: "row" as const, alignItems: "flex-start" as const, gap: S.md,
      paddingVertical: 12,
    },
    title: { fontFamily: FONT.semibold, fontSize: 14.5, lineHeight: 20, color: C.text, flex: 1 },
    titleDone: { textDecorationLine: "line-through" as const, color: C.textFaint },
    metaRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm, marginTop: 5, flexWrap: "wrap" as const },
    ownerChip: { flexDirection: "row" as const, alignItems: "center" as const, gap: 5 },
    ownerAvatar: { width: 18, height: 18, borderRadius: 9, alignItems: "center" as const, justifyContent: "center" as const },
    ownerTxt: { fontFamily: FONT.medium, fontSize: 12, color: C.textDim },
    dueTxt: { fontFamily: FONT.medium, fontSize: 12, color: C.textFaint },
  });
}

export default function TaskListScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ key: string | string[] }>();
  const key = Array.isArray(params.key) ? params.key.join("/") : (params.key ?? "");
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { tasks, setTaskStatus, addTask } = useMeeting();
  const [addOpen, setAddOpen] = useState(false);

  const groups: { label: string; items: Task[] }[] = [
    { label: "Open", items: tasks.filter((t) => t.status === "Open") },
    { label: "In Progress", items: tasks.filter((t) => t.status === "In Progress") },
    { label: "Completed", items: tasks.filter((t) => t.status === "Completed") },
  ].filter((g) => g.items.length);

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Tasks" }} />
      <ScrollView contentContainerStyle={st.body} showsVerticalScrollIndicator={false}>
        {!tasks.length ? (
          <EmptyState
            icon="checkmark.circle"
            title="No tasks yet"
            subtitle="MinuteX adds tasks it hears in the meeting. It never invents one, so if something was agreed and isn't here, add it yourself."
            action={<Button label="Add task" onPress={() => setAddOpen(true)} />}
          />
        ) : groups.map((g) => (
          <View key={g.label}>
            <Text style={st.groupLabel}>{g.label} ({g.items.length})</Text>
            <Card style={{ paddingVertical: S.xs }}>
              {g.items.map((t, i) => {
                const done = t.status === "Completed";
                return (
                  <Pressable
                    key={t.id}
                    onPress={() => router.push({ pathname: "/recording/[key]/task/[taskId]", params: { key, taskId: t.id } })}
                    style={({ pressed }) => [
                      st.row, i > 0 && { borderTopWidth: 1, borderTopColor: C.border },
                      pressed && { opacity: 0.7 },
                    ]}
                    accessibilityLabel={t.task}
                  >
                    <Pressable
                      onPress={() => setTaskStatus(t.id, done ? "Open" : "Completed")}
                      hitSlop={8}
                      style={{ marginTop: 1 }}
                      accessibilityLabel={done ? "Mark incomplete" : "Mark complete"}
                    >
                      <Icon name={done ? "checkmark.square.fill" : "square"} tintColor={done ? C.success : C.textFaint} size={21} />
                    </Pressable>
                    <View style={{ flex: 1 }}>
                      <Text style={[st.title, done && st.titleDone]} numberOfLines={1}>{t.task}</Text>
                      <View style={st.metaRow}>
                        {t.assignee ? (
                          <View style={st.ownerChip}>
                            <View style={[st.ownerAvatar, { backgroundColor: t.assignee.avatarColor }]}>
                              <Text style={{ fontFamily: FONT.bold, fontSize: 9.5, color: "#FFFFFF" }}>{initialsOf(t.assignee.name)}</Text>
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
                    <Icon name="chevron.right" tintColor={C.textFaint} size={15} />
                  </Pressable>
                );
              })}
            </Card>
          </View>
        ))}
        {/* Always reachable at the end of the list too, not only from the
            empty state: the AI missing ONE task in an otherwise-populated
            meeting is the common case, and that user never sees the empty
            state at all. */}
        {!!tasks.length && (
          <View style={{ marginTop: S.xl }}>
            <Button
              label="+ Add task"
              variant="secondary"
              onPress={() => setAddOpen(true)}
            />
          </View>
        )}
      </ScrollView>

      <AddTaskSheet
        visible={addOpen}
        onClose={() => setAddOpen(false)}
        recordingKey={key}
        onSubmit={addTask}
      />
    </View>
  );
}
