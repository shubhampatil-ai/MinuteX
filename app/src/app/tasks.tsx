// src/app/tasks.tsx — the Task Tracker: every commitment, across every meeting.
//
// This is the screen the first-class Task table exists for. Tasks used to live
// inside a recording row, which meant "what do I owe this week" was
// unanswerable without opening meetings one at a time.
//
// Every filter here is applied SERVER-side (see getAllTasks) and the list is
// paged. That is not premature optimization: a task list grows without bound,
// and the alternative — download everything, filter on the phone — degrades
// exactly as an account becomes valuable.
//
// The one piece of judgement in this screen is how it treats an UNRESOLVED
// assignee. The AI hears "Rahul, send the proposal" and records the name and
// the speaker, but never guesses which Rahul. So those tasks are shown with a
// clear "Needs assignee" affordance instead of a name that looks confirmed —
// because a task that silently belongs to the wrong person is worse than one
// that visibly belongs to nobody yet.
import { useCallback, useMemo, useState } from "react";
import {
  ActivityIndicator, FlatList, Pressable, RefreshControl, StyleSheet, Text,
  View,
} from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import {
  S, R, ELEV, CAPS, FONT, useTheme, ColorScale,
} from "../../lib/theme";
import {
  Button, Chip, EmptyState, ErrorText, SkeletonCard,
} from "../../lib/ui";
import {
  ApiError, ApiFolder, ApiTask, TaskFilters, TaskStatusV2, assigneeLabel,
  getAllTasks, getFolders, needsAssigneeResolution,
} from "../../lib/api";
import { avatarColorFor, initialsOf } from "../../lib/task-model";

const PAGE_SIZE = 50;

// The filter presets, kept as data so adding one is a single entry rather than
// another branch in the render.
type Preset = {
  key: string;
  label: string;
  filters: Omit<TaskFilters, "limit" | "cursor">;
};

const PRESETS: Preset[] = [
  { key: "open", label: "Open", filters: { status: "Open" } },
  { key: "overdue", label: "Overdue", filters: { overdue: true } },
  { key: "mine", label: "Assigned to me", filters: { assigned_to_me: true } },
  {
    key: "progress",
    label: "In Progress",
    filters: { status: "In Progress" },
  },
  { key: "done", label: "Completed", filters: { status: "Completed" } },
  { key: "all", label: "All", filters: {} },
];

export default function TasksScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();

  const [preset, setPreset] = useState("open");
  const [folderId, setFolderId] = useState("");
  const [folders, setFolders] = useState<ApiFolder[]>([]);
  const [tasks, setTasks] = useState<ApiTask[]>([]);
  const [cursor, setCursor] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const activeFilters = useMemo<TaskFilters>(() => {
    const p = PRESETS.find((x) => x.key === preset) ?? PRESETS[0];
    return { ...p.filters, ...(folderId ? { folder_id: folderId } : {}) };
  }, [preset, folderId]);

  const load = useCallback(
    async (filters: TaskFilters, isRefresh = false) => {
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        const res = await getAllTasks({ ...filters, limit: PAGE_SIZE });
        setTasks(res.tasks);
        setCursor(res.next_cursor);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Could not load tasks.");
        setTasks([]);
        setCursor("");
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    []
  );

  useFocusEffect(
    useCallback(() => {
      load(activeFilters);
      // The folder chips are a stable, cheap list — fetched alongside so the
      // filter row is populated without a second visible loading state.
      getFolders()
        .then((r) => setFolders(r.folders))
        .catch(() => setFolders([]));
    }, [load, activeFilters])
  );

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await getAllTasks({
        ...activeFilters,
        limit: PAGE_SIZE,
        cursor,
      });
      setTasks((prev) => [...prev, ...res.tasks]);
      setCursor(res.next_cursor);
    } catch {
      // Keep the cursor so scrolling again retries rather than dead-ending.
    } finally {
      setLoadingMore(false);
    }
  }, [activeFilters, cursor, loadingMore]);

  const renderTask = ({ item }: { item: ApiTask }) => {
    const { name, confirmed } = assigneeLabel(item);
    const needsPerson = needsAssigneeResolution(item);
    const done = item.status === "Completed";
    const cancelled = item.status === "Cancelled";
    return (
      <Pressable
        style={st.card}
        onPress={() =>
          router.push({
            pathname: "/task/[id]",
            params: { id: item.id },
          } as any)
        }
        accessibilityRole="button"
        accessibilityLabel={`Open task ${item.task}`}
      >
        <View style={st.cardTop}>
          <View style={{ flex: 1 }}>
            <Text
              style={[
                st.title,
                (done || cancelled) && {
                  textDecorationLine: "line-through",
                  color: C.textFaint,
                },
              ]}
              numberOfLines={2}
            >
              {item.task}
            </Text>

            <View style={st.metaRow}>
              <View
                style={[
                  st.statusPill,
                  { backgroundColor: statusTint(item.status, C).bg },
                ]}
              >
                <Text
                  style={[
                    st.statusTxt,
                    { color: statusTint(item.status, C).fg },
                  ]}
                >
                  {item.status}
                </Text>
              </View>
              {/* Overdue is computed server-side from due date + status, so it
                  is accurate right now rather than as of the last write. */}
              {item.is_overdue && (
                <View style={[st.statusPill, { backgroundColor: C.dangerSoft }]}>
                  <Text style={[st.statusTxt, { color: C.danger }]}>
                    Overdue
                  </Text>
                </View>
              )}
              {!!item.due && <Text style={st.due}>Due {item.due}</Text>}
            </View>
          </View>

          {confirmed ? (
            <View style={[st.avatar, { backgroundColor: avatarColorFor(name) }]}>
              <Text style={st.avatarTxt}>{initialsOf(name)}</Text>
            </View>
          ) : null}
        </View>

        {/* The identity story, stated honestly in each of its three shapes. */}
        {needsPerson ? (
          <View style={st.needsRow}>
            <Icon
              name="exclamationmark.triangle"
              size={12}
              tintColor={C.warn}
            />
            <Text style={st.needsTxt}>
              {name ? `"${name}" — needs a contact` : "Needs an assignee"}
            </Text>
            <Text style={st.needsCta}>Resolve</Text>
          </View>
        ) : confirmed ? (
          <View style={st.assigneeRow}>
            <Text style={st.assigneeTxt}>{name}</Text>
            {!!item.assignee_user_id && (
              <View style={st.appBadge}>
                <Icon name="checkmark" size={9} tintColor={C.success} />
                <Text style={st.appBadgeTxt}>App</Text>
              </View>
            )}
          </View>
        ) : (
          <View style={st.assigneeRow}>
            <Text style={[st.assigneeTxt, { color: C.textFaint }]}>
              Unassigned
            </Text>
          </View>
        )}
      </Pressable>
    );
  };

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Tasks" }} />
      <Text style={st.intro}>
        Every commitment from every meeting, in one place.
      </Text>

      <FlatList
        data={tasks}
        keyExtractor={(t) => t.id}
        renderItem={renderTask}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => load(activeFilters, true)}
            tintColor={C.primary}
          />
        }
        onEndReached={loadMore}
        onEndReachedThreshold={0.4}
        ListHeaderComponent={
          <View style={{ marginBottom: S.md }}>
            <Text style={st.filterLabel}>Status</Text>
            <View style={st.chipRow}>
              {PRESETS.map((p) => (
                <Chip
                  key={p.key}
                  label={p.label}
                  active={preset === p.key}
                  onPress={() => setPreset(p.key)}
                />
              ))}
            </View>
            {folders.length > 0 && (
              <>
                <Text style={st.filterLabel}>Folder</Text>
                <View style={st.chipRow}>
                  <Chip
                    label="Any"
                    active={!folderId}
                    onPress={() => setFolderId("")}
                  />
                  {folders.map((f) => (
                    <Chip
                      key={f.id}
                      label={f.name}
                      active={folderId === f.id}
                      onPress={() => setFolderId(f.id)}
                    />
                  ))}
                </View>
              </>
            )}
            {loading && (
              <>
                <SkeletonCard />
                <SkeletonCard />
              </>
            )}
            {!!error && (
              <View style={{ gap: S.sm, marginTop: S.sm }}>
                <ErrorText>{error}</ErrorText>
                <Button
                  label="Retry"
                  variant="secondary"
                  onPress={() => load(activeFilters)}
                />
              </View>
            )}
          </View>
        }
        ListEmptyComponent={
          loading || error ? null : (
            <EmptyState
              icon="checklist"
              title="Nothing here"
              subtitle={
                preset === "open"
                  ? "No open tasks. Tasks appear automatically from what people commit to in your meetings."
                  : "No tasks match these filters."
              }
            />
          )
        }
        ListFooterComponent={
          loadingMore ? (
            <View style={st.footer}>
              <ActivityIndicator color={C.primary} />
            </View>
          ) : (
            <View style={{ height: S.xxl }} />
          )
        }
        contentContainerStyle={{ paddingTop: S.sm }}
        showsVerticalScrollIndicator={false}
      />
    </View>
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
    intro: { ...T.caption, marginTop: 2, marginBottom: S.md },
    filterLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.3,
      color: C.textFaint, marginTop: S.sm, marginBottom: 6,
    },
    chipRow: {
      flexDirection: "row" as const, flexWrap: "wrap" as const, gap: S.sm,
    },
    card: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      marginBottom: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    cardTop: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.md,
    },
    title: { fontFamily: FONT.bold, fontSize: 14.5, color: C.text, lineHeight: 20 },
    metaRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      flexWrap: "wrap" as const, gap: 6, marginTop: 8,
    },
    statusPill: {
      paddingHorizontal: 8, paddingVertical: 3, borderRadius: R.pill,
    },
    statusTxt: { fontFamily: FONT.bold, fontSize: 10.5 },
    due: { fontFamily: FONT.medium, fontSize: 11.5, color: C.textFaint },
    avatar: {
      width: 34, height: 34, borderRadius: 17, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 12.5, color: "#fff" },
    assigneeRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      marginTop: 12, paddingTop: 10, borderTopWidth: 1,
      borderTopColor: C.border,
    },
    assigneeTxt: { fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim },
    appBadge: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 3,
      paddingHorizontal: 6, paddingVertical: 2, borderRadius: R.pill,
      backgroundColor: C.successSoft,
    },
    appBadgeTxt: { fontFamily: FONT.bold, fontSize: 9.5, color: C.success },
    needsRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      marginTop: 12, paddingTop: 10, borderTopWidth: 1,
      borderTopColor: C.border,
    },
    needsTxt: {
      fontFamily: FONT.medium, fontSize: 12.5, color: C.warn, flex: 1,
    },
    needsCta: { fontFamily: FONT.bold, fontSize: 12, color: C.primary },
    footer: { paddingVertical: S.lg, alignItems: "center" as const },
  });
}
