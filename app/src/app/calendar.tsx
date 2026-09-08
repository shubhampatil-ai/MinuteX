// src/app/calendar.tsx — the calendar: everything landing on a day.
//
// The Action Center answers "what should I do next?". This screen answers
// "what does my time actually look like?" — and on this product that question
// has two halves that belong on one surface:
//
//   MEETINGS   what happened, at a real clock time
//   TASKS      what is owed, on a day
//
// Showing only one of them would be the wrong calendar. MinuteX's whole claim
// is that meetings turn into work; a calendar where you can see the Acme
// review at 10:00 AM and the three tasks it generated due that Friday is that
// claim made visible. So the grid marks both, and a day opens into a single
// agenda listing both.
//
// WHY THE TWO ARE NOT MERGED INTO ONE SORTED TIMELINE. A meeting happened at
// an instant. A task is owed on a DAY and has no time at all. Interleaving
// them by clock would mean inventing an hour for every task — the kind of
// small fabrication this feature refuses everywhere else. So the agenda reads
// chronologically for meetings, then lists the day's tasks by urgency, each
// under its own heading. See agendaForDay() in lib/task-insights.ts.
//
// HOW IT LOADS. Tasks come through the one server-side range filter the
// backend exposes (`due_before`), following the cursor because that filter is
// applied after a recency-ordered index read — see the RANGE_ constants below.
// Meetings come from getRecordings(), which is already the app's one meeting
// list; it is small and cached-feeling, and the calendar filters it by day.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator, FlatList, Pressable, RefreshControl, StyleSheet, Text,
  View,
} from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, ELEV, CAPS, FONT, useTheme, ColorScale } from "../../lib/theme";
import { Button, EmptyState, ErrorText } from "../../lib/ui";
import {
  ApiError, ApiTask, RecordingSummary, getAllTasks,
  getMeetingHighlights, getRecordings, patchTaskById,
} from "../../lib/api";
import { TaskCard, SectionHeader } from "../../lib/task-action-center";
import {
  AgendaEntry, CalendarCell, GRID_DAY_LABELS, MeetingDeadline, addDays,
  addMonths, agendaForDay, clockLabel, dayHeading, dayLoad, deadlinesFromHighlights,
  describeDay, durationLabel, loadByDay, monthGrid, monthLabel, startOfMonth,
  toDayKey, unplacedDeadlines,
} from "../../lib/task-insights";
import { resolveSpokenDate } from "../../lib/spoken-dates";

// The backend caps a task page at 200 (TASKS_PAGE_MAX) and asking for more is
// silently clamped, so this asks for exactly that and FOLLOWS THE CURSOR
// instead of pretending one page is the whole range.
//
// Following it matters here in a way it does not on the dashboard.
// `due_before` is an upper bound applied AFTER the index read, and the index is
// ordered by recency, not by due date — so a single page of a busy account can
// be filled entirely by recent tasks while older ones due in the month being
// viewed sit unread behind the cursor. A calendar that quietly omits a day's
// work is worse than one that takes a moment longer to fill in.
const RANGE_PAGE = 200;
// A ceiling on that following, so an enormous account cannot spin here. If it
// is ever hit the screen says so rather than presenting a partial month as
// complete.
const RANGE_MAX_PAGES = 5;

// How many recent meetings to read highlights from, for the dates they
// mention. Highlights are STORED on the recording row and returned cached, so
// each call is a cheap read rather than an AI generation — but it is still one
// request per meeting, so this is deliberately bounded to the recent ones a
// calendar is actually likely to show. Older meetings' dates surface on the
// meeting itself, where they always have.
const HIGHLIGHT_MEETINGS = 12;

export default function TaskCalendarScreen() {
  const { C, T, mode } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();
  // Deep-linkable: the dashboard's day strip passes the day it was tapped on.
  const { day } = useLocalSearchParams<{ day?: string }>();

  const [now, setNow] = useState(() => new Date());
  const [month, setMonth] = useState(() =>
    startOfMonth(day ? new Date(`${day}T00:00:00`) : new Date())
  );
  const [selected, setSelected] = useState(() => day || toDayKey(new Date()));

  const [tasks, setTasks] = useState<ApiTask[]>([]);
  const [meetings, setMeetings] = useState<RecordingSummary[]>([]);
  const [deadlines, setDeadlines] = useState<MeetingDeadline[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState("");
  // True when the range read stopped at RANGE_MAX_PAGES with more still
  // behind the cursor — the grid is then a floor, not a total.
  const [truncated, setTruncated] = useState(false);

  // Same day-rollover discipline as the dashboard: everything here is
  // day-based, so a screen held past midnight must not keep calling yesterday
  // "today" in the grid.
  useEffect(() => {
    const midnight = new Date(now);
    midnight.setHours(24, 0, 0, 0);
    const t = setTimeout(
      () => setNow(new Date()),
      Math.max(1000, midnight.getTime() - now.getTime())
    );
    return () => clearTimeout(t);
  }, [now]);

  const load = useCallback(
    async (target: Date, isRefresh = false) => {
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        // The grid shows leading/trailing days from the adjacent months, so
        // the fetch has to cover them too — otherwise the first row of a month
        // would look empty when it is not. One week of slack past the grid's
        // end covers the trailing row.
        const gridEnd = addDays(addMonths(target, 1), 7);
        const dueBefore = toDayKey(gridEnd);

        const collected: ApiTask[] = [];
        let cursor = "";
        let pages = 0;
        let more = true;
        while (pages < RANGE_MAX_PAGES) {
          const res = await getAllTasks({
            due_before: dueBefore,
            limit: RANGE_PAGE,
            ...(cursor ? { cursor } : {}),
          });
          collected.push(...res.tasks);
          pages += 1;
          cursor = res.next_cursor;
          if (!cursor) {
            more = false;
            break;
          }
        }
        setTasks(collected);
        setTruncated(more && !!cursor);
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Couldn't load your calendar."
        );
        setTasks([]);
        setTruncated(false);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    []
  );

  // Meetings are a cheap, whole-list read and not month-scoped:
  // the recordings list is the app's one meeting collection, filtered by day
  // here. Best-effort — a failure costs the meeting markers, and must never
  // take the task grid down with it.
  const loadContext = useCallback(() => {
    getRecordings()
      .then(async (rs) => {
        setMeetings(rs);

        // The dates those meetings MENTIONED. Highlights are stored per
        // recording and returned cached (regenerate: false), so this is a
        // batch of cheap reads — but it is still N requests, hence the bound.
        // Newest first: a calendar is read forward from now.
        const recent = [...rs]
          .sort((a, b) =>
            String(b.recorded_at || b.created_at || "").localeCompare(
              String(a.recorded_at || a.created_at || "")
            )
          )
          .slice(0, HIGHLIGHT_MEETINGS);

        const results = await Promise.all(
          recent.map(async (r) => {
            try {
              const h = await getMeetingHighlights(r.audio_s3_key);
              return deadlinesFromHighlights(
                r.audio_s3_key,
                r.title || "Untitled meeting",
                String(r.recorded_at || r.created_at || ""),
                h.meeting_highlights?.deadlines ?? [],
                resolveSpokenDate
              );
            } catch {
              // A meeting whose highlights are missing or still generating
              // contributes nothing. It must not fail the calendar.
              return [];
            }
          })
        );
        setDeadlines(results.flat());
      })
      .catch(() => {
        setMeetings([]);
        setDeadlines([]);
      });
  }, []);

  useFocusEffect(
    useCallback(() => {
      setNow(new Date());
      load(month);
      loadContext();
    }, [load, loadContext, month])
  );

  // ---- Derived ------------------------------------------------------------

  const cells = useMemo(() => monthGrid(month, now), [month, now]);
  const loads = useMemo(
    () => loadByDay(tasks, now, meetings, deadlines),
    [tasks, now, meetings, deadlines]
  );
  const agenda = useMemo(
    () => agendaForDay(tasks, meetings, selected, now, deadlines),
    [tasks, meetings, selected, now, deadlines]
  );
  // Dates a meeting mentioned that are NOT calendar days ("end of Q3"). Listed
  // once under the grid rather than pinned to a guessed day.
  const unplaced = useMemo(() => unplacedDeadlines(deadlines), [deadlines]);
  const selectedLoad = useMemo(
    () => dayLoad(loads, selected),
    [loads, selected]
  );



  const monthTotals = useMemo(() => {
    let open = 0;
    let overdue = 0;
    let mtgs = 0;
    let dates = 0;
    for (const c of cells) {
      if (!c.inMonth) continue;
      const l = dayLoad(loads, c.dayKey);
      open += l.open;
      overdue += l.overdue;
      mtgs += l.meetings;
      dates += l.deadlines;
    }
    return { open, overdue, meetings: mtgs, dates };
  }, [cells, loads]);

  // ---- Actions ------------------------------------------------------------

  const goMonth = useCallback(
    (delta: number) => {
      const next = addMonths(month, delta);
      setMonth(next);
      // Land the selection on the same month so the list below is never
      // showing a day the grid no longer displays. The 1st, or today when
      // paging back to the current month.
      const today = toDayKey(now);
      const first = toDayKey(next);
      setSelected(today.slice(0, 7) === first.slice(0, 7) ? today : first);
    },
    [month, now]
  );

  const goToday = useCallback(() => {
    const today = new Date();
    setNow(today);
    setMonth(startOfMonth(today));
    setSelected(toDayKey(today));
  }, []);

  const openTask = useCallback(
    (t: ApiTask) => {
      router.push({ pathname: "/task/[id]", params: { id: t.id } } as never);
    },
    [router]
  );

  const openMeeting = useCallback(
    (key: string) => {
      router.push({
        pathname: "/recording/[key]",
        params: { key },
      } as never);
    },
    [router]
  );

  const toggleComplete = useCallback(
    async (t: ApiTask) => {
      const next = t.status === "Completed" ? "Open" : "Completed";
      setBusyId(t.id);
      setTasks((prev) =>
        prev.map((x) => (x.id === t.id ? { ...x, status: next } : x))
      );
      try {
        await patchTaskById(t.id, { status: next });
        await load(month, true);
      } catch (e) {
        setTasks((prev) =>
          prev.map((x) => (x.id === t.id ? { ...x, status: t.status } : x))
        );
        setError(
          e instanceof ApiError ? e.message : "Could not update that task."
        );
      } finally {
        setBusyId("");
      }
    },
    [load, month]
  );

  // ---- Grid ---------------------------------------------------------------

  const renderCell = (c: CalendarCell) => {
    const l = dayLoad(loads, c.dayKey);
    const on = c.dayKey === selected;
    // A day is "clear" when everything that was due on it is done — worth
    // showing, because a green day is the point of finishing things.
    const clear = l.open === 0 && l.completed > 0;
    const taskColor = l.overdue > 0 ? C.danger : clear ? C.success : C.primary;
    const hasTasks = l.open > 0 || l.completed > 0;
    const hasDates = l.deadlines > 0;

    return (
      <Pressable
        key={c.dayKey}
        onPress={() => setSelected(c.dayKey)}
        accessibilityRole="button"
        accessibilityState={{ selected: on }}
        accessibilityLabel={`${dayHeading(c.dayKey, now)}${
          describeDay(l) ? `, ${describeDay(l)}` : ", nothing"
        }`}
        style={({ pressed }) => [
          st.cell,
          on && { backgroundColor: C.text },
          !on && c.isToday && { borderColor: C.primary, borderWidth: 1.5 },
          pressed && !on && { opacity: 0.6 },
        ]}
      >
        <Text
          style={[
            st.cellDate,
            !c.inMonth && { color: C.textFaint, opacity: 0.55 },
            c.isPast && c.inMonth && !on && { color: C.textDim },
            on && { color: C.bg },
            !on && c.isToday && { color: C.primary, fontFamily: FONT.extrabold },
          ]}
        >
          {c.date}
        </Text>

        {/* Two distinct markers, because the two kinds of entry are distinct:
            a violet bar for meetings (the AI/meeting accent used across the
            app), a dot for task load. A day with neither shows neither — no
            zero, no placeholder. */}
        <View style={st.markers}>
          {l.meetings > 0 ? (
            <View
              style={[
                st.meetingBar,
                { backgroundColor: on ? C.bg : C.accent },
                l.meetings > 1 && st.meetingBarWide,
              ]}
            />
          ) : null}
          {hasTasks ? (
            <View
              style={[
                st.dot,
                { backgroundColor: on ? C.bg : taskColor },
                l.open > 2 && st.dotWide,
              ]}
            />
          ) : null}
          {/* A hollow ring for a date the meeting merely MENTIONED — visually
              lighter than a task dot, because nobody owes it. */}
          {hasDates ? (
            <View
              style={[
                st.ring,
                { borderColor: on ? C.bg : C.warn },
              ]}
            />
          ) : null}
        </View>
      </Pressable>
    );
  };

  // ---- Agenda rows --------------------------------------------------------

  const renderEntry = ({ item }: { item: AgendaEntry }) => {
    if (item.kind === "task") {
      return (
        <TaskCard
          task={item.task}
          now={now}
          busy={busyId === item.task.id}
          onPress={openTask}
          onToggleComplete={toggleComplete}
          onResolve={openTask}
        />
      );
    }

    if (item.kind === "deadline") {
      const d = item.deadline;
      return (
        <Pressable
          onPress={() => openMeeting(d.recordingKey)}
          accessibilityRole="button"
          accessibilityLabel={`${d.what || d.when}, mentioned in ${d.meetingTitle}`}
          style={({ pressed }) => [st.dateCard, pressed && { opacity: 0.85 }]}
        >
          <View style={st.dateHead}>
            <Icon name="calendar.badge.clock" size={13} tintColor={C.warn} />
            <Text style={st.dateWhat} numberOfLines={2}>
              {d.what || d.when}
            </Text>
          </View>
          {/* The speaker's OWN words always show. A resolved date never
              replaces what was actually said, and an inferred one is labelled
              as inferred so nobody plans around a guess as if it were a
              quotation. */}
          <Text style={st.dateMeta} numberOfLines={2}>
            &ldquo;{d.when}&rdquo;
            {d.confidence === "relative" ? "  ·  date inferred" : ""}
          </Text>
          <Text style={st.dateSource} numberOfLines={1}>
            Mentioned in {d.meetingTitle}
          </Text>
        </Pressable>
      );
    }

    const r = item.recording;
    const when = clockLabel(String(r.recorded_at || r.created_at || ""));
    const dur = durationLabel(r.duration);
    // Only the facts we have. A meeting still processing says so, because
    // opening it would show an empty transcript.
    const processing = r.status !== "complete" && r.status !== "transcribed";
    const meta = [when, dur].filter(Boolean).join("  ·  ");

    return (
      <Pressable
        onPress={() => openMeeting(r.audio_s3_key)}
        accessibilityRole="button"
        accessibilityLabel={`Open meeting ${r.title || "Untitled"}`}
        style={({ pressed }) => [st.meetingCard, pressed && { opacity: 0.85 }]}
      >
        {/* A colour spine, so a meeting reads as a different kind of thing
            from a task at a glance rather than by reading the label. */}
        <View style={[st.spine, { backgroundColor: C.primary }]} />
        <View style={st.meetingBody}>
          <View style={st.meetingHead}>
            <Icon
              name="waveform"
              size={13}
              tintColor={C.primary}
            />
            <Text style={st.meetingTitle} numberOfLines={1}>
              {r.title || "Untitled meeting"}
            </Text>
            <Icon name="chevron.right" size={13} tintColor={C.textFaint} />
          </View>
          {meta ? (
            <Text style={st.meetingMeta} numberOfLines={1}>
              {meta}
            </Text>
          ) : null}
          {processing ? (
            <Text style={st.processing}>Still processing</Text>
          ) : null}
        </View>
      </Pressable>
    );
  };

  const dayIsToday = selected === toDayKey(now);
  const daySummary = describeDay(selectedLoad);

  const header = (
    <View>
      {/* Month bar */}
      <View style={st.monthBar}>
        <Pressable
          onPress={() => goMonth(-1)}
          hitSlop={10}
          accessibilityRole="button"
          accessibilityLabel="Previous month"
          style={({ pressed }) => [st.navBtn, pressed && { opacity: 0.6 }]}
        >
          <Icon name="chevron.left" size={16} tintColor={C.textDim} />
        </Pressable>
        <View style={{ flex: 1, alignItems: "center" }}>
          <Text style={st.monthTxt}>{monthLabel(month)}</Text>
          {/* What this month holds, stated only when it holds something. */}
          {monthTotals.open > 0 ||
          monthTotals.meetings > 0 ||
          monthTotals.dates > 0 ? (
            <Text style={st.monthSub}>
              {/* "at least" only when the range read was cut short — the grid
                  is then a floor, and saying so beats a confident wrong
                  total. */}
              {truncated ? "at least " : ""}
              {[
                monthTotals.meetings
                  ? `${monthTotals.meetings} meeting${monthTotals.meetings === 1 ? "" : "s"}`
                  : "",
                monthTotals.open ? `${monthTotals.open} due` : "",
                monthTotals.overdue ? `${monthTotals.overdue} overdue` : "",
                monthTotals.dates
                  ? `${monthTotals.dates} date${monthTotals.dates === 1 ? "" : "s"}`
                  : "",
              ]
                .filter(Boolean)
                .join(" · ")}
            </Text>
          ) : null}
        </View>
        <Pressable
          onPress={() => goMonth(1)}
          hitSlop={10}
          accessibilityRole="button"
          accessibilityLabel="Next month"
          style={({ pressed }) => [st.navBtn, pressed && { opacity: 0.6 }]}
        >
          <Icon name="chevron.right" size={16} tintColor={C.textDim} />
        </Pressable>
      </View>

      {/* Weekday headers, Monday-first to match the grid. */}
      <View style={st.dowRow}>
        {GRID_DAY_LABELS.map((d, i) => (
          <Text key={`${d}${i}`} style={st.dow}>
            {d}
          </Text>
        ))}
      </View>

      {/* The grid. Fixed 6 rows so paging months never shifts what is below. */}
      <View style={st.grid}>{cells.map(renderCell)}</View>

      {/* What the two markers mean. Small, stated once, so a violet bar and a
          blue dot are not a puzzle. */}
      <View style={st.legend}>
        <View style={st.legendItem}>
          <View style={[st.meetingBar, { backgroundColor: C.accent }]} />
          <Text style={st.legendTxt}>Meeting</Text>
        </View>
        <View style={st.legendItem}>
          <View style={[st.dot, { backgroundColor: C.primary }]} />
          <Text style={st.legendTxt}>Due</Text>
        </View>
        <View style={st.legendItem}>
          <View style={[st.dot, { backgroundColor: C.danger }]} />
          <Text style={st.legendTxt}>Overdue</Text>
        </View>
        <View style={st.legendItem}>
          <View style={[st.dot, { backgroundColor: C.success }]} />
          <Text style={st.legendTxt}>Clear</Text>
        </View>
        <View style={st.legendItem}>
          <View style={[st.ring, { borderColor: C.warn }]} />
          <Text style={st.legendTxt}>Date mentioned</Text>
        </View>
      </View>

      {loading ? (
        <View style={st.loadingRow}>
          <ActivityIndicator color={C.primary} />
        </View>
      ) : null}

      {!!error && (
        <View style={{ gap: S.sm, marginTop: S.md }}>
          <ErrorText>{error}</ErrorText>
          <Button
            label="Retry"
            variant="secondary"
            onPress={() => load(month)}
          />
        </View>
      )}

      <SectionHeader
        title={dayHeading(selected, now)}
        actionLabel={dayIsToday ? undefined : "Today"}
        onAction={dayIsToday ? undefined : goToday}
      />
      {daySummary ? <Text style={st.daySummary}>{daySummary}</Text> : null}
    </View>
  );

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Calendar" }} />
      <FlatList
        data={agenda}
        keyExtractor={(e) => `${e.kind}:${e.id}`}
        renderItem={renderEntry}
        ListHeaderComponent={header}
        ListEmptyComponent={
          loading || error ? null : (
            <EmptyState
              icon="calendar"
              title="Nothing on this day."
              subtitle={
                // Past days and future days deserve different sentences: an
                // empty past day is a fact, an empty future day is capacity.
                selected < toDayKey(now)
                  ? "No meetings were recorded and nothing was due."
                  : "No meetings and nothing due — this day is clear."
              }
            />
          )
        }
        ListFooterComponent={
          <View>
            {/* Dates the meetings mentioned that are not calendar days
                ("end of Q3", "before the holidays"). They belong on this
                screen — they are things that were said to matter — but they
                cannot be pinned to a day without inventing one, so they are
                listed here instead of guessed onto the grid. */}
            {unplaced.length > 0 ? (
              <>
                <SectionHeader title="Mentioned, no fixed date" />
                <View style={{ gap: S.sm }}>
                  {unplaced.map((d) => (
                    <Pressable
                      key={d.id}
                      onPress={() => openMeeting(d.recordingKey)}
                      accessibilityRole="button"
                      accessibilityLabel={`${d.what || d.when}, mentioned in ${d.meetingTitle}`}
                      style={({ pressed }) => [
                        st.dateCard,
                        pressed && { opacity: 0.85 },
                      ]}
                    >
                      <View style={st.dateHead}>
                        <Icon
                          name="calendar.badge.clock"
                          size={13}
                          tintColor={C.textFaint}
                        />
                        <Text style={st.dateWhat} numberOfLines={2}>
                          {d.what || d.when}
                        </Text>
                      </View>
                      <Text style={st.dateMeta} numberOfLines={2}>
                        &ldquo;{d.when}&rdquo;
                      </Text>
                      <Text style={st.dateSource} numberOfLines={1}>
                        Mentioned in {d.meetingTitle}
                      </Text>
                    </Pressable>
                  ))}
                </View>
              </>
            ) : null}
            <View style={{ height: S.xxl }} />
          </View>
        }
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => {
              load(month, true);
              loadContext();
            }}
            tintColor={C.primary}
          />
        }
        showsVerticalScrollIndicator={false}
        contentContainerStyle={{ paddingHorizontal: 20, paddingTop: S.md }}
        initialNumToRender={8}
        windowSize={7}
        removeClippedSubviews
      />
    </View>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    monthBar: {
      flexDirection: "row",
      alignItems: "center",
      gap: S.sm,
      marginBottom: S.md,
    },
    navBtn: {
      width: 34,
      height: 34,
      borderRadius: 17,
      alignItems: "center",
      justifyContent: "center",
      backgroundColor: C.surface,
      borderWidth: 1,
      borderColor: C.border,
    },
    monthTxt: {
      fontFamily: FONT.bold,
      fontSize: 16.5,
      color: C.text,
      letterSpacing: -0.2,
    },
    monthSub: {
      fontFamily: FONT.medium,
      fontSize: 11.5,
      color: C.textFaint,
      marginTop: 2,
    },
    dowRow: { flexDirection: "row", marginBottom: 6 },
    dow: {
      ...CAPS,
      flex: 1,
      textAlign: "center",
      fontFamily: FONT.bold,
      fontSize: 9.5,
      letterSpacing: 0.8,
      color: C.textFaint,
    },
    grid: {
      flexDirection: "row",
      flexWrap: "wrap",
      backgroundColor: C.surface,
      borderRadius: R.card,
      borderWidth: 1,
      borderColor: C.border,
      paddingVertical: S.sm,
      paddingHorizontal: 4,
      shadowColor: C.shadow,
      ...ELEV.sm,
    },
    cell: {
      // Seven per row, sized by fraction rather than fixed points so the grid
      // fits a small Android phone and a tablet alike.
      width: `${100 / 7}%`,
      aspectRatio: 1,
      alignItems: "center",
      justifyContent: "center",
      borderRadius: R.md,
      borderWidth: 1.5,
      borderColor: "transparent",
      gap: 3,
    },
    cellDate: { fontFamily: FONT.semibold, fontSize: 13, color: C.text },
    markers: { height: 5, gap: 2, alignItems: "center", justifyContent: "center" },
    meetingBar: { width: 7, height: 2.5, borderRadius: 1.5 },
    meetingBarWide: { width: 12 },
    dot: { width: 4, height: 4, borderRadius: 2 },
    dotWide: { width: 12 },
    // Hollow, so a mentioned date reads as lighter than owed work.
    ring: { width: 5, height: 5, borderRadius: 2.5, borderWidth: 1 },
    legend: {
      flexDirection: "row",
      flexWrap: "wrap",
      gap: S.md,
      marginTop: S.sm,
      paddingHorizontal: 2,
    },
    legendItem: { flexDirection: "row", alignItems: "center", gap: 5 },
    legendTxt: { fontFamily: FONT.medium, fontSize: 10.5, color: C.textFaint },
    daySummary: {
      fontFamily: FONT.medium,
      fontSize: 12,
      color: C.textFaint,
      marginTop: -S.sm,
      marginBottom: S.md,
    },
    meetingCard: {
      flexDirection: "row",
      backgroundColor: C.surface,
      borderRadius: R.card,
      borderWidth: 1,
      borderColor: C.border,
      marginBottom: S.sm,
      overflow: "hidden",
      shadowColor: C.shadow,
      ...ELEV.sm,
    },
    spine: { width: 3 },
    meetingBody: { flex: 1, padding: S.md, gap: 5 },
    meetingHead: { flexDirection: "row", alignItems: "center", gap: 6 },
    meetingTitle: {
      flex: 1,
      fontFamily: FONT.semibold,
      fontSize: 13.5,
      color: C.text,
    },
    meetingMeta: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint },
    dateCard: {
      backgroundColor: C.surface,
      borderRadius: R.card,
      borderWidth: 1,
      borderColor: C.border,
      borderStyle: "dashed",
      padding: S.md,
      marginBottom: S.sm,
      gap: 5,
    },
    dateHead: { flexDirection: "row", alignItems: "center", gap: 6 },
    dateWhat: {
      flex: 1,
      fontFamily: FONT.semibold,
      fontSize: 13,
      color: C.text,
      lineHeight: 18,
    },
    dateMeta: {
      fontFamily: FONT.regular,
      fontSize: 11.5,
      color: C.textDim,
      fontStyle: "italic",
    },
    dateSource: { fontFamily: FONT.regular, fontSize: 11, color: C.textFaint },
    processing: { fontFamily: FONT.medium, fontSize: 11, color: C.warn },
    loadingRow: { paddingVertical: S.md, alignItems: "center" },
  });
}
