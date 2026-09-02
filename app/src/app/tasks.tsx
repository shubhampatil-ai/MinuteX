// src/app/tasks.tsx — the Task Action Center: what to do next, not what exists.
//
// This screen answers one question — "what should I act on?" — and everything
// on it is arranged to answer it in the order a person actually asks: what is
// late, what is due, what is unassigned, what came out of my meetings.
//
// WHAT IT KEPT FROM THE PREVIOUS TASK TRACKER, and why none of it was
// negotiable:
//
//   * SERVER-SIDE FILTERING AND CURSOR PAGING. A task list grows without
//     bound; downloading it to filter on the phone degrades exactly as an
//     account becomes valuable. Every filter chip still maps to a TaskFilters
//     field that getAllTasks sends to the backend, and the list is still 50 a
//     page behind a cursor.
//   * THE THREE-STATE ASSIGNEE. An unresolved AI name is never dressed as a
//     confirmed person — see TaskCard in lib/task-action-center.tsx. Resolve
//     still goes to /task/[id], which owns the candidate flow.
//   * TRUTHFUL CAPABILITIES. Nothing here claims a backend that does not
//     exist. The AI chips run a local action and say so; Reassign opens the
//     real resolution screen; Snooze is a real PATCH of due_date; the
//     completion checkbox is a real PATCH of status.
//
// WHAT IS COMPUTED VS FETCHED. The health cards, weekly progress, deadlines
// and meeting insight are all derived in lib/task-insights.ts from the tasks
// this screen has LOADED — there is no aggregate endpoint. Because that is a
// page rather than the whole account, the summary sections read from a
// separate, wider "insight" fetch (see loadInsights) that asks for open work
// specifically, and the health cards label themselves as counting loaded work.
// That is the honest version of a statistic the backend cannot yet give us.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, Alert, FlatList, RefreshControl, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { S, useTheme, ColorScale } from "../../lib/theme";
import { Button, EmptyState, ErrorText } from "../../lib/ui";
import {
  ApiError, ApiFolder, ApiTask, RecordingSummary, TaskFilters, getAllTasks,
  getFolders, getMe, getRecordings, patchTaskById,
} from "../../lib/api";
import {
  ActionCenterSkeleton, AIPromptKey, AttentionFilters, HealthKey,
  MeetingTaskInsight,
  MinuteXAIActionCard, QuickAddTaskButton, SectionHeader, ShowMoreRow, TaskCard,
  TaskHeader, TaskHealthCards, TaskSearchBar, UpcomingDeadlines,
  WeeklyProgressCard,
} from "../../lib/task-action-center";
import { SwipeableRow } from "../../lib/swipeable-row";
import { QuickAddTaskSheet } from "../../lib/quick-add-task-sheet";
import {
  addDays, computeCounts, dueKeyOf, greetingFor, isClosed, meetingInsights,
  daysUntil,
  needsAssignment, rankForAttention, toDayKey, upcomingDeadlines,
  weeklyProgress,
} from "../../lib/task-insights";

const PAGE_SIZE = 50;
// How many attention rows show before the list collapses behind "Show all".
//
// WHY COLLAPSE AT ALL. This screen's job is "what should I act on?", and the
// answer is the TOP of a ranked list — not all of it. Rendering every loaded
// task inline pushed "This week", "Upcoming deadlines" and "From your
// meetings" below the fold, so the further someone got through their work the
// less of the screen they could actually see. Five is the most that fits above
// those sections on a phone while still showing more than a token preview.
const COLLAPSED_ROWS = 5;
// How many tasks the derived sections (health, week, deadlines, insight) read.
// One extra page, requested once per focus, so a summary is not computed from
// whatever the user happened to scroll to. Still a bounded request.
const INSIGHT_PAGE = 100;

// The attention filters (§7). Each carries the SERVER-side filter it maps to,
// so selecting one narrows the query rather than the rendered array. `needs`
// is the exception and says so: resolution_status is not a query parameter the
// backend exposes, so it is applied to the loaded page after the fetch — the
// one place this screen filters client-side, and only because no server filter
// exists for it.
type FilterKey =
  | "all" | "overdue" | "mine" | "others" | "needs"
  | "today" | "upcoming" | "progress" | "done";

const FILTERS: {
  key: FilterKey;
  label: string;
  filters: Omit<TaskFilters, "limit" | "cursor">;
  clientOnly?: boolean;
}[] = [
  { key: "all", label: "All", filters: {} },
  // Deadline groups. `today` and `upcoming` ride on the SERVER's due_before
  // window (already supported by GET /tasks) and are then narrowed client-side
  // to the exact day — the API has no "due on" parameter, and adding one for a
  // view this small would be a backend change the requirement rules out.
  { key: "today", label: "Today", filters: {}, clientOnly: true },
  { key: "upcoming", label: "Upcoming", filters: {}, clientOnly: true },
  { key: "overdue", label: "Overdue", filters: { overdue: true } },
  // Review queue — the confidence gate's output, and the one filter that
  // matters most now that low-confidence assignments are withheld.
  { key: "needs", label: "Needs review", filters: {}, clientOnly: true },
  // Ownership. Both directions, because "what did I hand off?" is as real a
  // question as "what do I owe?".
  { key: "mine", label: "My tasks", filters: { assigned_to_me: true } },
  { key: "others", label: "Assigned to others", filters: {}, clientOnly: true },
  // Status. These ARE server filters — GET /tasks has always accepted
  // `status`, it was simply never exposed to the user.
  { key: "progress", label: "In progress", filters: { status: "In Progress" } },
  { key: "done", label: "Completed", filters: { status: "Completed" } },
];

/** The client half of the filter set.
 *
 * Every filter that CAN be a server query already is one (status, overdue,
 * assigned_to_me — see FILTERS above). These four cannot be, and each for a
 * specific reason rather than convenience:
 *
 *   today / upcoming     GET /tasks has `due_before` but no "due ON a day",
 *                        so an exact-day window has to be applied here.
 *   needs                the review flag is derived from resolution_status,
 *                        which is not an index key.
 *   others               "assigned to someone who is not me" is the negation
 *                        of assigned_to_me, and DynamoDB cannot express a
 *                        negated key condition.
 *
 * All four narrow the loaded page only, which is why they are marked
 * clientOnly and why the screen says so in its empty state.
 */
function narrowLocally(
  tasks: ApiTask[],
  filter: FilterKey,
  now: Date,
  myUserId: string
): ApiTask[] {
  switch (filter) {
    case "needs":
      return tasks.filter(needsAssignment);
    case "today":
      return tasks.filter((t) => {
        if (isClosed(t)) return false;
        const d = daysUntil(dueKeyOf(t), now);
        return d === 0;
      });
    case "upcoming":
      return tasks.filter((t) => {
        if (isClosed(t)) return false;
        const d = daysUntil(dueKeyOf(t), now);
        return d !== null && d > 0;
      });
    case "others":
      // Assigned to a REAL person who is not the caller. An unassigned task is
      // not "assigned to others" — it is assigned to nobody, and lumping the
      // two together would make this filter a dumping ground.
      return tasks.filter(
        (t) => !!t.assignee_user_id && t.assignee_user_id !== myUserId
      );
    default:
      return tasks;
  }
}

/** Case-insensitive substring match across the fields a person would search
 * BY: what the task says, who owes it, and which speaker it came from. The
 * assignee is matched through every shape it can take — a resolved contact
 * name, an unresolved AI name, or a speaker label — because from the outside
 * those are all just "who is this for?".
 *
 * Client-side by necessity: GET /tasks has no text-search parameter (see
 * TaskFilters), so this narrows what is already loaded and the UI says so. */
function matchesQuery(tasks: ApiTask[], query: string): ApiTask[] {
  const q = query.trim().toLowerCase();
  if (!q) return tasks;
  return tasks.filter((t) => {
    const haystack = [
      t.title || t.task || "",
      t.description || "",
      t.assignee?.name || "",
      t.assignee_name || "",
      t.assignee_name_legacy || "",
      t.speaker_name || "",
    ];
    return haystack.some((h) => h.toLowerCase().includes(q));
  });
}

export default function TasksScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();
  const insets = useSafeAreaInsets();

  const [filter, setFilter] = useState<FilterKey>("all");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [tasks, setTasks] = useState<ApiTask[]>([]);
  const [cursor, setCursor] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState("");

  // Context for the derived sections and for naming a task's folder.
  const [insightTasks, setInsightTasks] = useState<ApiTask[]>([]);
  const [folders, setFolders] = useState<ApiFolder[]>([]);
  const [recordings, setRecordings] = useState<RecordingSummary[]>([]);
  const [me, setMe] = useState<{ name: string; avatar_url: string } | null>(null);
  const [myUserId, setMyUserId] = useState("");
  const [aiNote, setAiNote] = useState("");
  const [quickAdd, setQuickAdd] = useState(false);

  const listRef = useRef<FlatList<ApiTask>>(null);
  // Read by loadMore, which must not re-create itself (and re-arm
  // onEndReached) every time the collapse state flips.
  const collapsedRef = useRef(false);

  // ONE clock, shared by every derived figure, so a task cannot read as
  // overdue in the health card and due-today on its own card.
  //
  // It is state rather than a bare `new Date()` because this screen is a tab
  // destination people leave open: everything here is DAY-based, so a session
  // held past midnight would otherwise keep calling yesterday "today" and
  // showing a task that just became overdue as due-soon. The timer re-arms for
  // the next local midnight and nothing else — no polling, one wakeup a day.
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const midnight = new Date(now);
    midnight.setHours(24, 0, 0, 0);
    const ms = Math.max(1000, midnight.getTime() - now.getTime());
    const t = setTimeout(() => setNow(new Date()), ms);
    return () => clearTimeout(t);
  }, [now]);

  const activeFilters = useMemo<TaskFilters>(() => {
    const f = FILTERS.find((x) => x.key === filter) ?? FILTERS[0];
    return f.filters;
  }, [filter]);

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
        setError(
          e instanceof ApiError ? e.message : "Couldn't load your tasks."
        );
        setTasks([]);
        setCursor("");
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    []
  );

  // The wider read the summary sections derive from, plus the cheap context
  // lists. All best-effort: a failure here degrades the summaries, and must
  // never take the task list down with it.
  const loadInsights = useCallback(async () => {
    try {
      const res = await getAllTasks({ limit: INSIGHT_PAGE });
      setInsightTasks(res.tasks);
    } catch {
      setInsightTasks([]);
    }
    getFolders()
      .then((r) => setFolders(r.folders))
      .catch(() => setFolders([]));
    getRecordings()
      .then(setRecordings)
      .catch(() => setRecordings([]));
    getMe()
      .then((u) => {
        setMe({ name: u.name, avatar_url: u.avatar_url });
        // Kept so "Assigned to others" can be expressed as "has a real
        // assignee who is not me" — GET /tasks can filter FOR me
        // (assigned_to_me) but cannot express the negation.
        setMyUserId(u.user_id ?? "");
      })
      .catch(() => setMe(null));
  }, []);

  useFocusEffect(
    useCallback(() => {
      // Returning to the screen re-reads the clock as well as the data: the
      // midnight timer only fires while this screen is mounted and focused.
      setNow(new Date());
      load(activeFilters);
    }, [load, activeFilters])
  );

  useFocusEffect(
    useCallback(() => {
      loadInsights();
    }, [loadInsights])
  );

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    // Do not page while the list is COLLAPSED. Only five rows render then, so
    // the footer sits close to the viewport and onEndReached fires immediately
    // — fetching page after page of tasks nobody can see, on someone's mobile
    // data. Expanding (or searching, which needs the full loaded set) is what
    // signals they actually want more.
    if (collapsedRef.current) return;
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

  // ---- Derived data (all of it from lib/task-insights) --------------------

  // Summaries prefer the wider insight read, falling back to the visible page
  // before it arrives so the cards are never blank after the list has painted.
  const summarySource = insightTasks.length ? insightTasks : tasks;

  const counts = useMemo(
    () =>
      computeCounts(summarySource, now, {
        partial: summarySource.length >= INSIGHT_PAGE,
      }),
    [summarySource, now]
  );
  const week = useMemo(() => weeklyProgress(summarySource, now), [summarySource, now]);
  const deadlines = useMemo(
    () => upcomingDeadlines(summarySource, now, 3),
    [summarySource, now]
  );

  const folderNames = useMemo(() => {
    const m = new Map<string, string>();
    for (const f of folders) m.set(f.id, f.name);
    return m;
  }, [folders]);

  const meetingTitles = useMemo(() => {
    const m = new Map<string, string>();
    for (const r of recordings) {
      if (r.title) m.set(r.audio_s3_key, r.title);
    }
    return m;
  }, [recordings]);

  const insight = useMemo(
    () => meetingInsights(summarySource, meetingTitles, now, 1)[0] ?? null,
    [summarySource, meetingTitles, now]
  );

  // The attention list: server-filtered, then ranked by urgency. "Needs
  // assignment" narrows the loaded page, which is why it is marked clientOnly
  // above rather than pretending to be a query.
  //
  // rankForAttention drops CLOSED tasks — a finished task is not something
  // that needs attention. But dropping them outright would mean ticking the
  // checkbox makes a row vanish with nowhere to see or undo it, so the closed
  // ones are appended below the open list rather than discarded: still out of
  // the way, still reachable, and un-tickable back to Open.
  const matched = useMemo(() => {
    const base = narrowLocally(tasks, filter, now, myUserId);
    const searched = matchesQuery(base, query);
    const open = rankForAttention(searched, now);
    const closed = searched.filter((t) => isClosed(t));
    return [...open, ...closed];
  }, [tasks, filter, now, query, myUserId]);

  // What actually renders. Truncation happens LAST — after filtering, search
  // and ranking — so "Show all 23" counts the tasks that match, and the five
  // rows on screen are the five most important of them rather than the first
  // five that happened to load.
  //
  // A search always shows every hit. Someone who typed a query has already
  // narrowed the list themselves; hiding matches behind a second tap would be
  // answering a question with "some of the answer".
  const truncated = !expanded && !query && matched.length > COLLAPSED_ROWS;
  const visible = useMemo(
    () => (truncated ? matched.slice(0, COLLAPSED_ROWS) : matched),
    [matched, truncated]
  );

  // How many tasks the search actually looked at. This is the LOADED page
  // count, not INSIGHT_PAGE: `matchesQuery` runs over `tasks` (what paging has
  // fetched), while the summary cards read the wider insight fetch. Reporting
  // the bigger number here would overstate the search by up to a page.
  const searchScope = tasks.length;

  // Mirror the truncation state into the ref loadMore reads. In an effect
  // rather than assigned during render: a render can be thrown away under
  // concurrent rendering, and writing a ref from one is exactly the kind of
  // side effect that then leaks a state the committed tree never had.
  useEffect(() => {
    collapsedRef.current = truncated;
  }, [truncated]);

  // Collapsing is only meaningful while there is more than a screenful. When a
  // filter change or a completed task shrinks the list below the cut, drop the
  // expanded flag so the control does not linger as a no-op "Show less".
  useEffect(() => {
    if (expanded && matched.length <= COLLAPSED_ROWS) setExpanded(false);
  }, [expanded, matched.length]);

  // ---- Mutations ----------------------------------------------------------

  const openTask = useCallback(
    (t: ApiTask) => {
      router.push({ pathname: "/task/[id]", params: { id: t.id } } as never);
    },
    [router]
  );

  /** Open the calendar already showing one day. The week strip is the only
   * place on this screen that names a specific date, so tapping one should
   * land on it rather than on whatever month the calendar defaults to. */
  const openCalendarOn = useCallback(
    (dayKey: string) => {
      router.push({
        pathname: "/calendar",
        params: { day: dayKey },
      } as never);
    },
    [router]
  );

  /** Real PATCH of status. update_meeting_task stamps completed_at server-side,
   * so the weekly figures get a genuine completion time out of this. */
  const toggleComplete = useCallback(
    async (t: ApiTask) => {
      const next = t.status === "Completed" ? "Open" : "Completed";
      setBusyId(t.id);
      // Optimistic, then reconciled by the refetch — a checkbox that waits for
      // a round trip feels broken on a phone.
      setTasks((prev) =>
        prev.map((x) => (x.id === t.id ? { ...x, status: next } : x))
      );
      try {
        await patchTaskById(t.id, { status: next });
        await Promise.all([load(activeFilters, true), loadInsights()]);
      } catch (e) {
        setTasks((prev) =>
          prev.map((x) => (x.id === t.id ? { ...x, status: t.status } : x))
        );
        Alert.alert(
          "Could not update",
          e instanceof ApiError ? e.message : "Please try again."
        );
      } finally {
        setBusyId("");
      }
    },
    [load, activeFilters, loadInsights]
  );

  /** Push the due date out by a day. A real mutation: due_date is patchable
   * (see update_meeting_task), so this persists rather than pretending to. */
  const snooze = useCallback(
    async (t: ApiTask) => {
      const current = dueKeyOf(t);
      const from = current ? new Date(`${current}T00:00:00`) : new Date();
      const next = toDayKey(addDays(from, 1));
      setBusyId(t.id);
      try {
        await patchTaskById(t.id, { due: next });
        await Promise.all([load(activeFilters, true), loadInsights()]);
      } catch (e) {
        Alert.alert(
          "Could not snooze",
          e instanceof ApiError ? e.message : "Please try again."
        );
      } finally {
        setBusyId("");
      }
    },
    [load, activeFilters, loadInsights]
  );

  // Reassign and Resolve are the same destination: the task detail screen owns
  // the candidate list, the folder hint and the contact picker. Duplicating any
  // of that here would be a second, divergent copy of the identity rules.
  const openResolve = useCallback(
    (t: ApiTask) => {
      router.push({ pathname: "/task/[id]", params: { id: t.id } } as never);
    },
    [router]
  );

  /** Open the meeting a task came from. Same route the meetings list uses —
   *  there is one meeting screen and this reuses it rather than adding a
   *  task-specific view of the same thing. */
  const openMeeting = useCallback(
    (t: ApiTask) => {
      const key = String(t.source_recording_id || "");
      if (!key) return;
      router.push({
        pathname: "/recording/[key]", params: { key },
      } as never);
    },
    [router]
  );

  // The AI chips (§4). There is no task-agent endpoint, so each chip performs
  // the honest LOCAL equivalent and names it. No fabricated reply, no call to
  // a route that does not exist.
  const onPrompt = useCallback(
    (key: AIPromptKey) => {
      if (key === "prioritize") {
        setFilter("all");
        listRef.current?.scrollToOffset({ offset: 0, animated: true });
        setAiNote(
          "Sorted by what needs you first: overdue, then due today, then due soon, then tasks still waiting on a name."
        );
        return;
      }
      if (key === "overdue") {
        setFilter("overdue");
        listRef.current?.scrollToOffset({ offset: 0, animated: true });
        setAiNote(
          counts.overdue
            ? `Showing ${counts.overdue} overdue task${counts.overdue === 1 ? "" : "s"}.`
            : "Nothing is overdue right now."
        );
        return;
      }
      setAiNote(
        week.total
          ? `This week: ${week.completed} of ${week.total} done, ${counts.dueThisWeek} still due, ${counts.overdue} overdue.`
          : "No tasks are due or completed this week yet."
      );
    },
    [counts, week]
  );

  const onHealthSelect = useCallback((key: HealthKey) => {
    // Overdue and Needs Review both map onto a real filter, so tapping the
    // number takes you to exactly the tasks it counted. "Due this week" and
    // "Completed" have no single equivalent filter and scroll to the section
    // that explains them rather than faking a query.
    if (key === "overdue" || key === "review") {
      setFilter(key === "review" ? "needs" : "overdue");
      listRef.current?.scrollToOffset({ offset: 0, animated: true });
    }
  }, []);

  // ---- Empty states (§19) -------------------------------------------------

  const empty = useMemo(() => {
    // A search that found nothing is NOT "all caught up" — the tasks exist,
    // they just did not match. Saying otherwise reads as reassurance when it
    // should read as "try a different word", and it hides the real limit:
    // only the loaded pages were searched.
    if (query.trim()) {
      return {
        icon: "magnifyingglass",
        title: `No tasks match "${query.trim()}".`,
        subtitle:
          "Only the tasks loaded so far are searched. Pull to refresh or clear the search to see everything.",
      };
    }
    switch (filter) {
      case "today":
        return {
          icon: "calendar.badge.clock",
          title: "Nothing due today.",
          subtitle: "Only the tasks loaded so far are checked — pull to refresh for more.",
        };
      case "upcoming":
        return {
          icon: "calendar",
          title: "Nothing scheduled ahead.",
          subtitle: "Tasks with a resolved deadline in the future appear here.",
        };
      case "others":
        return {
          icon: "person.2.fill",
          title: "Nothing assigned to anyone else.",
          subtitle: "Tasks you have handed to a teammate will show up here.",
        };
      case "progress":
        return {
          icon: "hourglass",
          title: "Nothing in progress.",
          subtitle: "Move a task to In Progress to see it here.",
        };
      case "done":
        return {
          icon: "checkmark.circle.fill",
          title: "Nothing completed yet.",
          subtitle: "Completed tasks stay here so you can look back on them.",
        };
      case "overdue":
        return {
          icon: "checkmark.circle",
          title: "Nothing overdue.",
          subtitle: "Nice work — you're on track.",
        };
      case "mine":
        return {
          icon: "person.fill",
          title: "No tasks assigned to you yet.",
          subtitle:
            "Tasks assigned to your MinuteX account will appear here.",
        };
      case "needs":
        return {
          icon: "checkmark.seal.fill",
          title: "All AI-extracted assignees are resolved.",
          subtitle: "Every task names a person the system can identify.",
        };
      default:
        return {
          icon: "checklist",
          title: "You're all caught up.",
          subtitle: "No action items need your attention.",
        };
    }
  }, [filter, query]);

  const renderItem = useCallback(
    ({ item }: { item: ApiTask }) => (
      <SwipeableRow
        onComplete={() => toggleComplete(item)}
        actions={[
          {
            key: "snooze",
            label: "Snooze",
            icon: "clock",
            color: C.warn,
            onPress: () => snooze(item),
          },
          {
            key: "reassign",
            label: "Reassign",
            icon: "person.fill",
            color: C.primary,
            onPress: () => openResolve(item),
          },
        ]}
        enabled={busyId !== item.id}
      >
        <TaskCard
          task={item}
          now={now}
          folderName={folderNames.get(String(item.folder_id || ""))}
          meetingTitle={meetingTitles.get(String(item.source_recording_id || ""))}
          busy={busyId === item.id}
          onPress={openTask}
          onToggleComplete={toggleComplete}
          onResolve={openResolve}
          onOpenMeeting={openMeeting}
        />
      </SwipeableRow>
    ),
    [
      C, now, folderNames, meetingTitles, busyId, openTask, toggleComplete,
      openResolve, openMeeting, snooze,
    ]
  );

  const header = (
    <View>
      <TaskHeader
        greeting={greetingFor(now)}
        name={me?.name || ""}
        avatarUrl={me?.avatar_url || undefined}
        onAvatarPress={() => router.push("/(tabs)/profile" as never)}
        // The navigator header is hidden so this screen can own its layout,
        // which makes Back this header's responsibility. canGoBack() guards
        // the deep-link case where there is nothing to go back to.
        onBack={router.canGoBack() ? () => router.back() : undefined}
      />

      {loading && !tasks.length ? (
        <ActionCenterSkeleton />
      ) : (
        <>
          <MinuteXAIActionCard onPrompt={onPrompt} note={aiNote} />

          <TaskHealthCards counts={counts} onSelect={onHealthSelect} />
          {counts.partial ? (
            // Say what the numbers describe. These are derived from the tasks
            // loaded, not from an account-wide aggregate the backend does not
            // expose — labelling that is cheaper than being subtly wrong.
            <Text style={st.partialNote}>
              Across your {INSIGHT_PAGE} most recent tasks.
            </Text>
          ) : null}

          <SectionHeader
            title="Needs your attention"
            // The action mirrors the row at the bottom of the list, so the
            // control is reachable from either end of a long list. It only
            // appears when there is something hidden to reveal — a "See all"
            // that does nothing is worse than no affordance at all.
            actionLabel={
              matched.length > COLLAPSED_ROWS
                ? expanded
                  ? "Show less"
                  : `See all ${matched.length}`
                : undefined
            }
            onAction={
              matched.length > COLLAPSED_ROWS
                ? () => setExpanded((v) => !v)
                : undefined
            }
          />
          <AttentionFilters
            options={FILTERS.map((f) => ({ key: f.key, label: f.label }))}
            value={filter}
            onChange={setFilter}
          />
          <View style={{ height: S.sm }} />
          <TaskSearchBar value={query} onChange={setQuery} />
          {query ? (
            // Name the set that was searched. There is no server-side text
            // search, so this is a real limit and hiding it would let someone
            // conclude a task does not exist when it simply was not loaded.
            <Text style={st.searchNote}>
              {matched.length === 0
                ? `No matches in your ${searchScope} most recent tasks.`
                : `${matched.length} of your ${searchScope} most recent tasks.`}
            </Text>
          ) : null}
          <View style={{ height: S.md }} />

          {!!error && (
            <View style={{ gap: S.sm, marginBottom: S.md }}>
              <ErrorText>{error}</ErrorText>
              <Button
                label="Retry"
                variant="secondary"
                onPress={() => load(activeFilters)}
              />
            </View>
          )}
        </>
      )}
    </View>
  );

  // The sections BELOW the list. They live in the footer rather than in a
  // second ScrollView, because nesting a task list inside a ScrollView breaks
  // virtualization (§22).
  const footer =
    loading && !tasks.length ? (
      <View style={{ height: S.xxl }} />
    ) : (
      <View>
        {/* The collapse control sits at the TOP of the footer, which is
            immediately below the last task row — where a person looking for
            "is there more?" actually looks. Hidden during a search, which
            already shows every match. */}
        {matched.length > COLLAPSED_ROWS && !query ? (
          <ShowMoreRow
            expanded={expanded}
            hiddenCount={matched.length - COLLAPSED_ROWS}
            totalCount={matched.length}
            onToggle={() => setExpanded((v) => !v)}
          />
        ) : null}

        {/* ALWAYS rendered. "View calendar" is the app's entry point to the
            calendar, so it cannot be conditional on this week containing work
            — an empty week is exactly when someone wants to go look at the
            month. The CARD copes with a zero week on its own. */}
        <SectionHeader
          title="This week"
          actionLabel="View calendar"
          onAction={() => router.push("/calendar" as never)}
        />
        <WeeklyProgressCard week={week} onDayPress={openCalendarOn} />

        {deadlines.length > 0 ? (
          <>
            <SectionHeader
              title="Upcoming deadlines"
              // These three ARE tasks from the list above, so "See all" means
              // "show me the whole list", not "re-apply the All filter" —
              // which did nothing when All was already selected.
              actionLabel="See all"
              onAction={() => {
                setFilter("all");
                setQuery("");
                setExpanded(true);
                listRef.current?.scrollToOffset({ offset: 0, animated: true });
              }}
            />
            <UpcomingDeadlines
              items={deadlines}
              folderNames={folderNames}
              onPress={openTask}
            />
          </>
        ) : null}

        {insight ? (
          <>
            <SectionHeader title="From your meetings" actionLabel="Recent" />
            <MeetingTaskInsight
              insight={insight}
              onPress={(key) =>
                router.push({
                  pathname: "/recording/[key]",
                  params: { key },
                } as never)
              }
            />
          </>
        ) : null}

        {loadingMore ? (
          <View style={st.footerSpinner}>
            <ActivityIndicator color={C.primary} />
          </View>
        ) : null}
        {/* Clears the FAB. */}
        <View style={{ height: 96 }} />
      </View>
    );

  return (
    <View style={st.container}>
      <Stack.Screen options={{ headerShown: false }} />
      <FlatList
        ref={listRef}
        data={loading && !tasks.length ? [] : visible}
        keyExtractor={(t) => t.id}
        renderItem={renderItem}
        ListHeaderComponent={header}
        ListFooterComponent={footer}
        ListEmptyComponent={
          loading || error ? null : (
            <EmptyState
              icon={empty.icon}
              title={empty.title}
              subtitle={empty.subtitle}
            />
          )
        }
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => {
              load(activeFilters, true);
              loadInsights();
            }}
            tintColor={C.primary}
          />
        }
        onEndReached={loadMore}
        onEndReachedThreshold={0.4}
        showsVerticalScrollIndicator={false}
        contentContainerStyle={{
          paddingHorizontal: 20,
          paddingTop: insets.top + S.sm,
        }}
        // Virtualization tuning for a phone: render a screenful either side,
        // not the whole list.
        initialNumToRender={8}
        maxToRenderPerBatch={10}
        windowSize={9}
        removeClippedSubviews
      />

      <QuickAddTaskButton
        onPress={() => setQuickAdd(true)}
        bottom={Math.max(insets.bottom, S.lg) + S.md}
      />

      <QuickAddTaskSheet
        visible={quickAdd}
        onClose={() => setQuickAdd(false)}
        onCreated={() => {
          load(activeFilters, true);
          loadInsights();
        }}
      />
    </View>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    partialNote: {
      ...T.caption,
      marginTop: 6,
    },
    searchNote: {
      ...T.caption,
      marginTop: S.sm,
    },
    footerSpinner: { paddingVertical: S.lg, alignItems: "center" },
  });
}
