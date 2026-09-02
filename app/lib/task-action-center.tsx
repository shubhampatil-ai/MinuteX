// lib/task-action-center.tsx — the presentational pieces of the Task Action
// Center (src/app/tasks.tsx).
//
// Split out of the screen so the screen file stays about DATA — loading,
// filtering, paging, mutation — and these stay about pixels. Everything here
// is memoized: the attention list re-renders on every page append, and an
// unmemoized card would re-render every previously-loaded row with it.
//
// The one piece of judgement that lives in this file rather than the screen is
// the assignee row on TaskCard. It has three shapes, and they are not
// cosmetic: an UNRESOLVED name (the AI heard "Rahul") must never be dressed as
// a confirmed person, because a task that silently belongs to the wrong person
// is worse than one that visibly belongs to nobody yet. That rule is enforced
// here, once, from assigneeLabel()/needsAssigneeResolution() — the same two
// functions the task detail screen resolves through.
import React, { memo, useEffect, useMemo, useRef } from "react";
import {
  Animated, Easing, Image, Pressable, ScrollView, StyleSheet, Text, TextInput,
  View,
} from "react-native";
import { Icon } from "./icons";
import {
  S, R, ELEV, CAPS, FONT, useTheme, ColorScale,
} from "./theme";
import { Card, Skeleton } from "./ui";
import {
  ApiTask, TaskStatusV2, assigneeLabel, needsAssigneeResolution,
} from "./api";
import { avatarColorFor, initialsOf } from "./task-model";
import {
  Deadline, MeetingInsight, TaskCounts, WeeklyProgress, describeInsight,
  dueKeyOf, isOverdue, needsAssignment, shortDate,
} from "./task-insights";

// ---------------------------------------------------------------------------
// Status colour. One mapping, shared by every surface that shows a status, so
// the pill on a card and the pill on the detail screen can never drift.
// ---------------------------------------------------------------------------
export function statusTint(status: TaskStatusV2 | string, C: ColorScale) {
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

// ---------------------------------------------------------------------------
// Pill — the small status/urgency badge used across the screen.
// ---------------------------------------------------------------------------
export const Pill = memo(function Pill({
  label, bg, fg,
}: { label: string; bg: string; fg: string }) {
  return (
    <View style={[pillStyles.wrap, { backgroundColor: bg }]}>
      <Text style={[pillStyles.txt, { color: fg }]} numberOfLines={1}>
        {label.toUpperCase()}
      </Text>
    </View>
  );
});

const pillStyles = StyleSheet.create({
  wrap: { paddingHorizontal: 7, paddingVertical: 3, borderRadius: R.pill },
  txt: { fontFamily: FONT.bold, fontSize: 9, letterSpacing: 0.5 },
});

// ---------------------------------------------------------------------------
// SectionHeader — "Needs your attention        See all"
// ---------------------------------------------------------------------------
export const SectionHeader = memo(function SectionHeader({
  title, actionLabel, onAction,
}: { title: string; actionLabel?: string; onAction?: () => void }) {
  const { C } = useTheme();
  return (
    <View style={sectionStyles.row}>
      <Text style={[sectionStyles.title, { color: C.text }]}>{title}</Text>
      {actionLabel && onAction ? (
        <Pressable
          onPress={onAction}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel={`${actionLabel}: ${title}`}
        >
          {({ pressed }) => (
            <Text
              style={[
                sectionStyles.action,
                { color: C.primary },
                pressed && { opacity: 0.6 },
              ]}
            >
              {actionLabel}
            </Text>
          )}
        </Pressable>
      ) : null}
    </View>
  );
});

const sectionStyles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginTop: S.xl,
    marginBottom: S.md,
  },
  title: { fontFamily: FONT.bold, fontSize: 16.5, letterSpacing: -0.2 },
  action: { fontFamily: FONT.semibold, fontSize: 12.5 },
});

// ---------------------------------------------------------------------------
// TaskHeader — greeting + "Your actions" + avatar (§3)
// ---------------------------------------------------------------------------
export const TaskHeader = memo(function TaskHeader({
  greeting, name, avatarUrl, onAvatarPress, onBack,
}: {
  greeting: string;
  name: string;
  avatarUrl?: string;
  onAvatarPress?: () => void;
  /** Back out of the screen. The Action Center hides the navigator header to
   * own its own layout, so it has to carry the Back affordance itself —
   * /tasks is a PUSHED stack route, and without this there is no way off it
   * on Android's gesture-nav or iOS beyond the edge swipe. */
  onBack?: () => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildHeaderStyles(C), [C]);
  return (
    <View style={st.wrap}>
      {onBack ? (
        <Pressable
          onPress={onBack}
          hitSlop={10}
          accessibilityRole="button"
          accessibilityLabel="Go back"
          style={({ pressed }) => [st.back, pressed && { opacity: 0.6 }]}
        >
          <Icon name="chevron.left" size={18} tintColor={C.textDim} />
        </Pressable>
      ) : null}
      <View style={{ flex: 1 }}>
        <Text style={st.greeting}>{greeting}</Text>
        <Text style={st.title}>Your actions</Text>
      </View>
      <Pressable
        onPress={onAvatarPress}
        hitSlop={8}
        accessibilityRole="button"
        accessibilityLabel="Open your profile"
        style={({ pressed }) => pressed && { opacity: 0.7 }}
      >
        <View style={st.avatar}>
          {avatarUrl ? (
            <Image source={{ uri: avatarUrl }} style={st.avatarImg} />
          ) : (
            <Text style={st.avatarTxt}>{initialsOf(name || "?")}</Text>
          )}
        </View>
      </Pressable>
    </View>
  );
});

function buildHeaderStyles(C: ColorScale) {
  return StyleSheet.create({
    wrap: {
      flexDirection: "row",
      alignItems: "flex-start",
      gap: S.md,
      paddingTop: S.sm,
    },
    back: {
      width: 34,
      height: 34,
      borderRadius: 17,
      alignItems: "center",
      justifyContent: "center",
      backgroundColor: C.surface,
      borderWidth: 1,
      borderColor: C.border,
      marginTop: 2,
      marginLeft: -4,
    },
    greeting: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary },
    title: {
      fontFamily: FONT.extrabold,
      fontSize: 27,
      lineHeight: 33,
      letterSpacing: -0.5,
      color: C.text,
      marginTop: 3,
    },
    avatar: {
      width: 40,
      height: 40,
      borderRadius: 20,
      backgroundColor: C.text,
      alignItems: "center",
      justifyContent: "center",
      overflow: "hidden",
    },
    avatarImg: { width: 40, height: 40 },
    avatarTxt: {
      fontFamily: FONT.bold,
      fontSize: 14,
      color: C.bg,
      letterSpacing: 0.3,
    },
  });
}

// ---------------------------------------------------------------------------
// MinuteXAIActionCard (§4)
//
// The dark card is the one high-contrast surface on the screen — it reads as a
// core capability rather than a banner. The chips are real controls, but there
// is NO task-agent backend: no endpoint accepts "prioritize my tasks". So a
// tap applies the honest local equivalent (a filter / a scroll) and the card
// says what it did. It never fabricates an AI reply or calls an endpoint that
// does not exist.
// ---------------------------------------------------------------------------
export type AIPromptKey = "prioritize" | "overdue" | "week";

export const AI_PROMPTS: { key: AIPromptKey; label: string }[] = [
  { key: "prioritize", label: "Prioritize my tasks" },
  { key: "overdue", label: "What's overdue?" },
  { key: "week", label: "Summarize my week" },
];

export const MinuteXAIActionCard = memo(function MinuteXAIActionCard({
  onPrompt, note,
}: { onPrompt: (key: AIPromptKey) => void; note?: string }) {
  const { C } = useTheme();
  const st = useMemo(() => buildAIStyles(C), [C]);
  return (
    <View style={st.card}>
      {/* A single soft bloom rather than a gradient wash — depth without
          decoration. */}
      <View style={st.bloom} pointerEvents="none" />
      <View style={st.brandRow}>
        <Icon name="sparkles" size={12} tintColor="#A9BEFF" />
        <Text style={st.brand}>MinuteX AI</Text>
      </View>
      <Text style={st.headline}>What do you want to get done?</Text>
      <View style={st.chipRow}>
        {AI_PROMPTS.map((p) => (
          <Pressable
            key={p.key}
            onPress={() => onPrompt(p.key)}
            accessibilityRole="button"
            accessibilityLabel={p.label}
            style={({ pressed }) => [st.chip, pressed && { opacity: 0.65 }]}
          >
            <Text style={st.chipTxt}>{p.label}</Text>
          </Pressable>
        ))}
      </View>
      {note ? <Text style={st.note}>{note}</Text> : null}
    </View>
  );
});

function buildAIStyles(C: ColorScale) {
  // Deliberately fixed dark, in both themes: this card is the product's AI
  // surface and should look the same everywhere, like the assistant does.
  return StyleSheet.create({
    card: {
      backgroundColor: "#15182A",
      borderRadius: R.lg,
      padding: S.lg,
      marginTop: S.lg,
      overflow: "hidden",
      shadowColor: C.shadow,
      ...ELEV.md,
    },
    bloom: {
      position: "absolute",
      top: -70,
      right: -50,
      width: 170,
      height: 170,
      borderRadius: 85,
      backgroundColor: "rgba(124,92,255,0.20)",
    },
    brandRow: { flexDirection: "row", alignItems: "center", gap: 5 },
    brand: {
      ...CAPS,
      fontFamily: FONT.bold,
      fontSize: 9.5,
      letterSpacing: 1.2,
      color: "#A9BEFF",
    },
    headline: {
      fontFamily: FONT.bold,
      fontSize: 17,
      lineHeight: 24,
      color: "#FFFFFF",
      marginTop: 10,
      letterSpacing: -0.2,
    },
    chipRow: {
      flexDirection: "row",
      flexWrap: "wrap",
      gap: S.sm,
      marginTop: S.md,
    },
    chip: {
      backgroundColor: "rgba(255,255,255,0.10)",
      borderWidth: 1,
      borderColor: "rgba(255,255,255,0.14)",
      borderRadius: R.pill,
      paddingHorizontal: 12,
      paddingVertical: 7,
    },
    chipTxt: { fontFamily: FONT.semibold, fontSize: 12, color: "#E8EBF7" },
    note: {
      fontFamily: FONT.regular,
      fontSize: 11.5,
      lineHeight: 17,
      color: "#9AA2C0",
      marginTop: S.md,
    },
  });
}

// ---------------------------------------------------------------------------
// TaskHealthCards (§5)
// ---------------------------------------------------------------------------
export type HealthKey = "overdue" | "week" | "done" | "review";

export const TaskHealthCards = memo(function TaskHealthCards({
  counts, onSelect,
}: { counts: TaskCounts; onSelect: (key: HealthKey) => void }) {
  const { C } = useTheme();
  const st = useMemo(() => buildHealthStyles(C), [C]);
  const cells: {
    key: HealthKey;
    value: number;
    label: string;
    color: string;
  }[] = [
    { key: "overdue", value: counts.overdue, label: "Overdue", color: C.danger },
    // Needs Review earns a cell ONLY when there is something to review. A
    // permanent "0 needs review" would take a quarter of the row to say
    // nothing, and — worse — would train people to read past the one number
    // that means "the AI is unsure who owes this".
    ...(counts.needsAssignment
      ? [{
        key: "review" as const,
        value: counts.needsAssignment,
        label: "Needs review",
        color: C.warn,
      }]
      : []),
    { key: "week", value: counts.dueThisWeek, label: "Due this week", color: C.warn },
    { key: "done", value: counts.completed, label: "Completed", color: C.success },
  ];
  return (
    <View style={st.row}>
      {cells.map((c) => (
        <Pressable
          key={c.key}
          onPress={() => onSelect(c.key)}
          accessibilityRole="button"
          accessibilityLabel={`${c.value} ${c.label}`}
          style={({ pressed }) => [st.cell, pressed && { opacity: 0.7 }]}
        >
          <Text style={[st.value, { color: c.color }]}>{c.value}</Text>
          <Text style={st.label} numberOfLines={1}>
            {c.label}
          </Text>
        </Pressable>
      ))}
    </View>
  );
});

function buildHealthStyles(C: ColorScale) {
  return StyleSheet.create({
    row: { flexDirection: "row", gap: S.sm, marginTop: S.lg },
    cell: {
      flex: 1,
      backgroundColor: C.surface,
      borderRadius: R.card,
      borderWidth: 1,
      borderColor: C.border,
      paddingVertical: S.md,
      paddingHorizontal: 10,
      shadowColor: C.shadow,
      ...ELEV.sm,
    },
    value: { fontFamily: FONT.extrabold, fontSize: 23, letterSpacing: -0.5 },
    label: {
      fontFamily: FONT.medium,
      fontSize: 10.5,
      color: C.textFaint,
      marginTop: 2,
    },
  });
}

// ---------------------------------------------------------------------------
// AttentionFilters (§7) — horizontally scrollable, compact.
// ---------------------------------------------------------------------------
export const AttentionFilters = memo(function AttentionFilters<
  K extends string,
>({
  options, value, onChange,
}: {
  options: { key: K; label: string }[];
  value: K;
  onChange: (key: K) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildFilterStyles(C), [C]);
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={st.row}
      // A short horizontal strip inside a vertical list is fine — it holds
      // four chips, not a task list (§22).
      keyboardShouldPersistTaps="handled"
    >
      {options.map((o) => {
        const on = o.key === value;
        return (
          <Pressable
            key={o.key}
            onPress={() => onChange(o.key)}
            accessibilityRole="button"
            accessibilityState={{ selected: on }}
            accessibilityLabel={`Filter: ${o.label}`}
            style={({ pressed }) => [
              st.chip,
              on && { backgroundColor: C.text, borderColor: C.text },
              pressed && { opacity: 0.7 },
            ]}
          >
            <Text style={[st.txt, on && { color: C.bg }]}>{o.label}</Text>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}) as <K extends string>(props: {
  options: { key: K; label: string }[];
  value: K;
  onChange: (key: K) => void;
}) => React.ReactElement;

function buildFilterStyles(C: ColorScale) {
  return StyleSheet.create({
    row: { flexDirection: "row", gap: S.sm, paddingRight: S.lg },
    chip: {
      borderRadius: R.pill,
      borderWidth: 1,
      borderColor: C.border,
      backgroundColor: C.surface,
      paddingHorizontal: 13,
      paddingVertical: 7,
    },
    txt: { fontFamily: FONT.semibold, fontSize: 12, color: C.textDim },
  });
}

// ---------------------------------------------------------------------------
// TaskCard (§8)
// ---------------------------------------------------------------------------
export type TaskCardProps = {
  task: ApiTask;
  now: Date;
  folderName?: string;
  /** The source meeting's title, resolved by the screen from the recordings it
   *  already loads — so the card costs no extra fetch. Absent for a manual
   *  task, which correctly shows no source. */
  meetingTitle?: string;
  busy?: boolean;
  onPress: (task: ApiTask) => void;
  onToggleComplete: (task: ApiTask) => void;
  onResolve: (task: ApiTask) => void;
  /** Open the meeting this task came from. Optional: when absent the source
   *  still renders, just not as a link. */
  onOpenMeeting?: (task: ApiTask) => void;
};

function taskCardsEqual(a: TaskCardProps, b: TaskCardProps) {
  // Identity of the row plus everything that changes its pixels. `now` is
  // compared by day, not by instant, so a re-render of the parent does not
  // invalidate every card (§22).
  return (
    a.task === b.task &&
    a.folderName === b.folderName &&
    a.meetingTitle === b.meetingTitle &&
    a.onOpenMeeting === b.onOpenMeeting &&
    a.busy === b.busy &&
    a.now.getDate() === b.now.getDate() &&
    a.onPress === b.onPress &&
    a.onToggleComplete === b.onToggleComplete &&
    a.onResolve === b.onResolve
  );
}

export const TaskCard = memo(function TaskCard({
  task, now, folderName, meetingTitle, busy, onPress, onToggleComplete,
  onResolve, onOpenMeeting,
}: TaskCardProps) {
  const { C } = useTheme();
  const st = useMemo(() => buildTaskCardStyles(C), [C]);

  const { name, confirmed } = assigneeLabel(task);
  // The SERVER's verdict, via the shared helper. Calling
  // needsAssigneeResolution directly here would miss a task whose assignment
  // the confidence gate withheld, and the card would then show a confident
  // assignee for a row the detail screen flags as needing review.
  const needsPerson = needsAssignment(task);
  const overdue = isOverdue(task, now);
  const done = task.status === "Completed";
  const cancelled = task.status === "Cancelled";
  const tint = statusTint(task.status, C);

  // "Aug 22 · Sales · High priority" — only the parts that exist.
  const meta = [
    shortDate(dueKeyOf(task)),
    folderName || "",
    task.priority && task.priority !== "Medium" ? `${task.priority} priority` : "",
  ].filter(Boolean);

  return (
    <Pressable
      onPress={() => onPress(task)}
      accessibilityRole="button"
      accessibilityLabel={`Open task ${task.task}`}
      style={({ pressed }) => [
        st.card,
        overdue && { borderColor: C.dangerSoft },
        needsPerson && { backgroundColor: C.warnSoft, borderColor: C.warnSoft },
        pressed && { opacity: 0.85 },
      ]}
    >
      <View style={st.top}>
        {/* The checkbox is a real mutation (PATCH status) — the one write this
            card performs directly. */}
        <Pressable
          onPress={() => onToggleComplete(task)}
          disabled={busy || cancelled}
          hitSlop={10}
          accessibilityRole="checkbox"
          accessibilityState={{ checked: done, disabled: busy || cancelled }}
          accessibilityLabel={
            done ? `Reopen ${task.task}` : `Complete ${task.task}`
          }
          style={({ pressed }) => [
            st.checkbox,
            done && { backgroundColor: C.success, borderColor: C.success },
            (busy || cancelled) && { opacity: 0.4 },
            pressed && { opacity: 0.6 },
          ]}
        >
          {done ? <Icon name="checkmark" size={11} tintColor="#fff" /> : null}
        </Pressable>

        <View style={st.body}>
          <View style={st.titleRow}>
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
              {task.task}
            </Text>
            {/* Overdue outranks status: it is the thing to act on. Status
                still shows for anything not overdue, so a card always states
                where the task stands. */}
            {overdue ? (
              <Pill label="Overdue" bg={C.dangerSoft} fg={C.danger} />
            ) : (
              <Pill label={task.status} bg={tint.bg} fg={tint.fg} />
            )}
            {/* AI provenance at a glance. The card deliberately does NOT show
                the confidence band — the "needs a contact" row below already
                carries the only consequence a low-confidence reading has, and
                a second badge on every AI row would be noise on a list whose
                job is triage. The band is on the detail screen, where the
                user is actually deciding. */}
            {task.source_type === "AI" ? (
              <Pill label="AI" bg={C.accentSoft} fg={C.accent} />
            ) : null}
          </View>

          {meta.length ? (
            <Text style={st.meta} numberOfLines={1}>
              {meta.join("  ·  ")}
            </Text>
          ) : null}

          {/* The identity story, in its three honest shapes. */}
          {needsPerson ? (
            <Pressable
              onPress={() => onResolve(task)}
              accessibilityRole="button"
              accessibilityLabel={
                name ? `Resolve who ${name} is` : "Assign this task"
              }
              style={({ pressed }) => [st.needsRow, pressed && { opacity: 0.7 }]}
            >
              <Icon name="exclamationmark.triangle" size={11} tintColor={C.warn} />
              <Text style={st.needsTxt} numberOfLines={1}>
                {name ? `"${name}" needs a contact` : "Needs an assignee"}
              </Text>
              <Text style={st.resolve}>Resolve</Text>
            </Pressable>
          ) : confirmed ? (
            <View style={st.assigneeRow}>
              <View
                style={[st.dot, { backgroundColor: avatarColorFor(name) }]}
              >
                <Text style={st.dotTxt}>{initialsOf(name)}</Text>
              </View>
              <Text style={st.assigneeTxt} numberOfLines={1}>
                {name}
              </Text>
              {/* Only when a MinuteX account actually backs this person. */}
              {task.assignee_user_id ? (
                <>
                  <Text style={st.sep}>·</Text>
                  <Text style={st.appTxt}>App</Text>
                </>
              ) : null}
            </View>
          ) : (
            <View style={st.assigneeRow}>
              <Text style={[st.assigneeTxt, { color: C.textFaint }]}>
                Unassigned
              </Text>
            </View>
          )}

          {/* WHERE this came from. Only for tasks that actually have a source
              meeting — a manually typed task has none and must not be given a
              fake provenance line. */}
          {meetingTitle ? (
            <Pressable
              onPress={
                onOpenMeeting ? () => onOpenMeeting(task) : undefined
              }
              disabled={!onOpenMeeting}
              hitSlop={6}
              style={({ pressed }) => [st.sourceRow, pressed && { opacity: 0.6 }]}
              accessibilityRole={onOpenMeeting ? "button" : undefined}
              accessibilityLabel={
                onOpenMeeting ? `Open meeting ${meetingTitle}` : undefined
              }
            >
              <Icon name="waveform" size={11} tintColor={C.textFaint} />
              <Text style={st.sourceTxt} numberOfLines={1}>
                {meetingTitle}
              </Text>
            </Pressable>
          ) : null}
        </View>
      </View>
    </Pressable>
  );
},
taskCardsEqual);

function buildTaskCardStyles(C: ColorScale) {
  return StyleSheet.create({
    sourceRow: {
      flexDirection: "row", alignItems: "center", gap: 5, marginTop: 6,
      alignSelf: "flex-start",
    },
    sourceTxt: {
      fontFamily: FONT.medium, fontSize: 11.5, color: C.textFaint,
      flexShrink: 1,
    },
    card: {
      backgroundColor: C.surface,
      borderRadius: R.card,
      borderWidth: 1,
      borderColor: C.border,
      padding: S.md,
      marginBottom: S.sm,
      shadowColor: C.shadow,
      ...ELEV.sm,
    },
    top: { flexDirection: "row", gap: S.md, alignItems: "flex-start" },
    checkbox: {
      width: 19,
      height: 19,
      borderRadius: 6,
      borderWidth: 1.5,
      borderColor: C.borderStrong,
      alignItems: "center",
      justifyContent: "center",
      marginTop: 2,
    },
    body: { flex: 1, gap: 6 },
    titleRow: { flexDirection: "row", alignItems: "flex-start", gap: S.sm },
    title: {
      flex: 1,
      fontFamily: FONT.semibold,
      fontSize: 14,
      lineHeight: 19.5,
      color: C.text,
      letterSpacing: -0.1,
    },
    meta: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint },
    assigneeRow: { flexDirection: "row", alignItems: "center", gap: 6 },
    dot: {
      width: 18,
      height: 18,
      borderRadius: 9,
      alignItems: "center",
      justifyContent: "center",
    },
    dotTxt: { fontFamily: FONT.bold, fontSize: 8.5, color: "#fff" },
    assigneeTxt: {
      fontFamily: FONT.medium,
      fontSize: 12,
      color: C.textDim,
      flexShrink: 1,
    },
    sep: { fontFamily: FONT.medium, fontSize: 12, color: C.textFaint },
    appTxt: { fontFamily: FONT.semibold, fontSize: 11.5, color: C.success },
    needsRow: { flexDirection: "row", alignItems: "center", gap: 5 },
    needsTxt: {
      fontFamily: FONT.medium,
      fontSize: 11.5,
      color: C.warn,
      flexShrink: 1,
    },
    resolve: { fontFamily: FONT.bold, fontSize: 11.5, color: C.primary },
  });
}

// ---------------------------------------------------------------------------
// WeeklyProgressCard (§10)
// ---------------------------------------------------------------------------
export const WeeklyProgressCard = memo(function WeeklyProgressCard({
  week, onDayPress,
}: {
  week: WeeklyProgress;
  /** Tapping a day opens the calendar on it. Optional so the card still
   * renders as a plain summary wherever there is nowhere to go. */
  onDayPress?: (dayKey: string) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildWeekStyles(C), [C]);
  const pct = Math.round(week.ratio * 100);
  // A week with no work in it is a real state, not a broken statistic. The
  // card still renders — it carries the day strip and the calendar link — but
  // "0 / 0" and "0%" would read as a failure rather than an empty week, so the
  // figures give way to a sentence.
  const empty = week.total === 0;
  return (
    <Card style={st.card}>
      <View style={st.top}>
        <View style={{ flex: 1 }}>
          <Text style={st.label}>Task completion</Text>
          {empty ? (
            <Text style={st.emptyTxt}>Nothing due or completed this week.</Text>
          ) : (
            <Text style={st.figure}>
              {week.completed} <Text style={st.figureDim}>/ {week.total}</Text>
            </Text>
          )}
          {/* Only when a real prior week exists to compare against. */}
          {week.deltaPct !== null ? (
            <View style={st.deltaRow}>
              <Icon
                name={week.deltaPct >= 0 ? "arrow.up" : "arrow.down"}
                size={10}
                tintColor={week.deltaPct >= 0 ? C.success : C.danger}
              />
              <Text
                style={[
                  st.delta,
                  { color: week.deltaPct >= 0 ? C.success : C.danger },
                ]}
              >
                {Math.abs(week.deltaPct)}% vs last week
              </Text>
            </View>
          ) : null}
        </View>
        {empty ? null : <ProgressRing ratio={week.ratio} label={`${pct}%`} />}
      </View>

      <View style={st.strip}>
        {week.days.map((d) => (
          <Pressable
            key={d.dayKey}
            onPress={onDayPress ? () => onDayPress(d.dayKey) : undefined}
            disabled={!onDayPress}
            accessibilityRole={onDayPress ? "button" : undefined}
            accessibilityLabel={
              onDayPress ? `Open ${d.label} ${d.date} in the calendar` : undefined
            }
            style={({ pressed }) => [
              st.day,
              d.isToday && { backgroundColor: C.text, borderColor: C.text },
              pressed && { opacity: 0.6 },
            ]}
          >
            <Text style={[st.dayLabel, d.isToday && { color: C.bg }]}>
              {d.label}
            </Text>
            <Text style={[st.dayDate, d.isToday && { color: C.bg }]}>
              {d.date}
            </Text>
          </Pressable>
        ))}
      </View>
    </Card>
  );
});

/** A ring drawn from two rotated half-discs — no SVG dependency, and it
 * animates on the native driver so it stays smooth while the list scrolls. */
const ProgressRing = memo(function ProgressRing({
  ratio, label, size = 56,
}: { ratio: number; label: string; size?: number }) {
  const { C } = useTheme();
  const clamped = Math.max(0, Math.min(1, ratio));
  const anim = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    Animated.timing(anim, {
      toValue: clamped,
      duration: 700,
      easing: Easing.out(Easing.cubic),
      useNativeDriver: true,
    }).start();
  }, [anim, clamped]);

  const thickness = 5;
  // Right half sweeps 0-180°, then the left half takes over for 180-360°.
  const rightRotate = anim.interpolate({
    inputRange: [0, 0.5, 1],
    outputRange: ["0deg", "180deg", "180deg"],
  });
  const leftRotate = anim.interpolate({
    inputRange: [0, 0.5, 1],
    outputRange: ["0deg", "0deg", "180deg"],
  });
  const half = {
    position: "absolute" as const,
    width: size / 2,
    height: size,
    overflow: "hidden" as const,
  };
  const fill = {
    width: size / 2,
    height: size,
    borderTopWidth: thickness,
    borderBottomWidth: thickness,
    borderColor: C.primary,
  };

  return (
    <View
      style={{ width: size, height: size, alignItems: "center", justifyContent: "center" }}
      accessibilityLabel={`${label} complete`}
    >
      <View
        style={{
          position: "absolute",
          width: size,
          height: size,
          borderRadius: size / 2,
          borderWidth: thickness,
          borderColor: C.surface2,
        }}
      />
      <View style={[half, { left: 0 }]}>
        <Animated.View
          style={{
            width: size,
            height: size,
            transform: [{ rotate: leftRotate }],
          }}
        >
          <View
            style={[
              fill,
              {
                borderLeftWidth: thickness,
                borderTopLeftRadius: size / 2,
                borderBottomLeftRadius: size / 2,
              },
            ]}
          />
        </Animated.View>
      </View>
      <View style={[half, { right: 0 }]}>
        <Animated.View
          style={{
            width: size,
            height: size,
            marginLeft: -size / 2,
            transform: [{ rotate: rightRotate }],
          }}
        >
          <View
            style={[
              fill,
              {
                marginLeft: size / 2,
                borderRightWidth: thickness,
                borderTopRightRadius: size / 2,
                borderBottomRightRadius: size / 2,
              },
            ]}
          />
        </Animated.View>
      </View>
      <Text
        style={{
          fontFamily: FONT.bold,
          fontSize: 13,
          color: C.text,
          letterSpacing: -0.3,
        }}
      >
        {label}
      </Text>
    </View>
  );
});

function buildWeekStyles(C: ColorScale) {
  return StyleSheet.create({
    card: { padding: S.lg, gap: S.lg },
    top: { flexDirection: "row", alignItems: "center", gap: S.lg },
    label: {
      ...CAPS,
      fontFamily: FONT.bold,
      fontSize: 9.5,
      letterSpacing: 1.2,
      color: C.textFaint,
    },
    figure: {
      fontFamily: FONT.extrabold,
      fontSize: 25,
      color: C.text,
      marginTop: 5,
      letterSpacing: -0.6,
    },
    figureDim: { fontFamily: FONT.semibold, fontSize: 17, color: C.textFaint },
    emptyTxt: {
      fontFamily: FONT.medium,
      fontSize: 13,
      lineHeight: 19,
      color: C.textFaint,
      marginTop: 6,
    },
    deltaRow: { flexDirection: "row", alignItems: "center", gap: 3, marginTop: 4 },
    delta: { fontFamily: FONT.semibold, fontSize: 11.5 },
    strip: { flexDirection: "row", gap: 6 },
    day: {
      flex: 1,
      alignItems: "center",
      paddingVertical: 8,
      borderRadius: R.md,
      borderWidth: 1,
      borderColor: C.border,
      backgroundColor: C.surface2,
    },
    dayLabel: {
      fontFamily: FONT.semibold,
      fontSize: 9,
      letterSpacing: 0.6,
      color: C.textFaint,
    },
    dayDate: {
      fontFamily: FONT.bold,
      fontSize: 13,
      color: C.text,
      marginTop: 2,
    },
  });
}

// ---------------------------------------------------------------------------
// UpcomingDeadlines (§11)
// ---------------------------------------------------------------------------
export const UpcomingDeadlines = memo(function UpcomingDeadlines({
  items, folderNames, onPress,
}: {
  items: Deadline[];
  folderNames: Map<string, string>;
  onPress: (task: ApiTask) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildDeadlineStyles(C), [C]);
  return (
    <View style={{ gap: S.sm }}>
      {items.map((d) => {
        const folder = folderNames.get(String(d.task.folder_id || "")) || "";
        const sub = [d.label, folder].filter(Boolean).join("  ·  ");
        const urgent = d.days <= 1;
        return (
          <Pressable
            key={d.task.id}
            onPress={() => onPress(d.task)}
            accessibilityRole="button"
            accessibilityLabel={`${d.task.task}, due ${d.label}`}
            style={({ pressed }) => [st.card, pressed && { opacity: 0.85 }]}
          >
            <View style={st.row}>
              <Text style={st.title} numberOfLines={1}>
                {d.task.task}
              </Text>
              <Pill
                label={
                  d.days === 0
                    ? "Today"
                    : `${d.days} day${d.days === 1 ? "" : "s"}`
                }
                bg={urgent ? C.dangerSoft : C.surface2}
                fg={urgent ? C.danger : C.textDim}
              />
            </View>
            <Text style={st.sub}>{sub}</Text>
            <View style={st.track}>
              <View
                style={[
                  st.fill,
                  {
                    width: `${Math.round(d.progress * 100)}%`,
                    backgroundColor: urgent ? C.danger : C.primary,
                  },
                ]}
              />
            </View>
          </Pressable>
        );
      })}
    </View>
  );
});

function buildDeadlineStyles(C: ColorScale) {
  return StyleSheet.create({
    card: {
      backgroundColor: C.surface,
      borderRadius: R.card,
      borderWidth: 1,
      borderColor: C.border,
      padding: S.md,
      gap: 7,
      shadowColor: C.shadow,
      ...ELEV.sm,
    },
    row: { flexDirection: "row", alignItems: "center", gap: S.sm },
    title: {
      flex: 1,
      fontFamily: FONT.semibold,
      fontSize: 13.5,
      color: C.text,
    },
    sub: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint },
    track: {
      height: 4,
      borderRadius: 2,
      backgroundColor: C.surface2,
      overflow: "hidden",
    },
    fill: { height: 4, borderRadius: 2 },
  });
}

// ---------------------------------------------------------------------------
// MeetingTaskInsight (§12) — the product's differentiation, stated as a fact.
// ---------------------------------------------------------------------------
export const MeetingTaskInsight = memo(function MeetingTaskInsight({
  insight, onPress,
}: { insight: MeetingInsight; onPress: (recordingKey: string) => void }) {
  const { C } = useTheme();
  const st = useMemo(() => buildInsightStyles(C), [C]);
  return (
    <Pressable
      onPress={() => onPress(insight.recordingKey)}
      accessibilityRole="button"
      accessibilityLabel={`Open meeting ${insight.title}`}
      style={({ pressed }) => [st.card, pressed && { opacity: 0.85 }]}
    >
      <View style={st.head}>
        <Icon name="waveform" size={13} tintColor={C.accent} />
        <Text style={st.title} numberOfLines={1}>
          {insight.title}
        </Text>
        <Icon name="chevron.right" size={13} tintColor={C.textFaint} />
      </View>
      <Text style={st.body}>
        <Text style={st.bodyStrong}>generated </Text>
        {describeInsight(insight)}
      </Text>
    </Pressable>
  );
});

function buildInsightStyles(C: ColorScale) {
  return StyleSheet.create({
    card: {
      backgroundColor: C.accentSoft,
      borderRadius: R.card,
      borderWidth: 1,
      borderColor: C.accentSoft,
      padding: S.md,
      gap: 6,
    },
    head: { flexDirection: "row", alignItems: "center", gap: 6 },
    title: {
      flex: 1,
      fontFamily: FONT.bold,
      fontSize: 13.5,
      color: C.text,
    },
    body: {
      fontFamily: FONT.regular,
      fontSize: 12.5,
      lineHeight: 18.5,
      color: C.textDim,
    },
    bodyStrong: { fontFamily: FONT.semibold, color: C.textDim },
  });
}

// ---------------------------------------------------------------------------
// QuickAddTaskButton (§13) — floating action button.
// ---------------------------------------------------------------------------
export const QuickAddTaskButton = memo(function QuickAddTaskButton({
  onPress, bottom,
}: { onPress: () => void; bottom: number }) {
  const { C } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel="Quick add a task"
      style={({ pressed }) => [
        {
          position: "absolute",
          right: 20,
          bottom,
          flexDirection: "row",
          alignItems: "center",
          gap: 6,
          paddingHorizontal: 16,
          paddingVertical: 12,
          borderRadius: R.pill,
          backgroundColor: C.primary,
          shadowColor: C.shadow,
          ...ELEV.lg,
        },
        pressed && { opacity: 0.85 },
      ]}
    >
      <Icon name="plus" size={15} tintColor={C.textOnPrimary} />
      <Text
        style={{
          fontFamily: FONT.bold,
          fontSize: 13,
          color: C.textOnPrimary,
        }}
      >
        Quick Add
      </Text>
    </Pressable>
  );
});

// ---------------------------------------------------------------------------
// Skeletons (§18) — one per section, shaped like the thing it stands in for.
// ---------------------------------------------------------------------------
export const ActionCenterSkeleton = memo(function ActionCenterSkeleton() {
  const { C } = useTheme();
  const block = (style: object) => (
    <View
      style={[
        {
          backgroundColor: C.surface,
          borderRadius: R.card,
          borderWidth: 1,
          borderColor: C.border,
          padding: S.md,
        },
        style,
      ]}
    />
  );
  return (
    <View style={{ gap: S.md }}>
      {/* AI card */}
      <View
        style={{
          height: 148,
          borderRadius: R.lg,
          backgroundColor: C.surface2,
          marginTop: S.lg,
        }}
      />
      {/* Health cards */}
      <View style={{ flexDirection: "row", gap: S.sm }}>
        {[0, 1, 2].map((i) => (
          <View key={i} style={{ flex: 1 }}>
            {block({ height: 68, justifyContent: "center", gap: 8 })}
          </View>
        ))}
      </View>
      {/* Task cards */}
      <View style={{ gap: S.sm, marginTop: S.md }}>
        {[0, 1, 2].map((i) => (
          <View
            key={i}
            style={{
              backgroundColor: C.surface,
              borderRadius: R.card,
              borderWidth: 1,
              borderColor: C.border,
              padding: S.md,
              gap: 9,
            }}
          >
            <Skeleton style={{ width: "72%", height: 13 }} />
            <Skeleton style={{ width: "44%" }} />
            <Skeleton style={{ width: "36%" }} />
          </View>
        ))}
      </View>
      {/* Weekly + deadlines */}
      <View style={{ height: 132, borderRadius: R.card, backgroundColor: C.surface2 }} />
      <View style={{ height: 74, borderRadius: R.card, backgroundColor: C.surface2 }} />
    </View>
  );
});

// ---------------------------------------------------------------------------
// TaskSearchBar — narrowing the attention list by text.
//
// SCOPE IS STATED, NOT IMPLIED. There is no server-side text search on
// GET /tasks (the endpoint takes status/folder/assignee/overdue/due_before and
// nothing else), so this filters the tasks already LOADED. That is a real
// limitation and the caller renders a note saying which set was searched —
// the same honesty the health cards already apply to their counts. A box that
// silently searched one page while looking account-wide would quietly tell
// people a task does not exist.
//
// Uncontrolled-feeling but controlled: `value` is owned by the screen so
// clearing a filter chip can clear the query too.
// ---------------------------------------------------------------------------
export const TaskSearchBar = memo(function TaskSearchBar({
  value, onChange, placeholder = "Search tasks",
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
}) {
  const { C } = useTheme();
  return (
    <View
      style={[
        searchStyles.wrap,
        { backgroundColor: C.surface2, borderColor: C.border },
      ]}
    >
      <Icon name="magnifyingglass" size={17} tintColor={C.textFaint} />
      <TextInput
        value={value}
        onChangeText={onChange}
        placeholder={placeholder}
        placeholderTextColor={C.textFaint}
        style={[searchStyles.input, { color: C.text }]}
        returnKeyType="search"
        autoCapitalize="none"
        autoCorrect={false}
        // The list updates as you type; there is nothing to submit.
        blurOnSubmit
        accessibilityLabel="Search tasks"
      />
      {value ? (
        <Pressable
          onPress={() => onChange("")}
          hitSlop={10}
          accessibilityRole="button"
          accessibilityLabel="Clear search"
        >
          {({ pressed }) => (
            <Icon
              name="xmark.circle.fill"
              size={17}
              tintColor={pressed ? C.textDim : C.textFaint}
            />
          )}
        </Pressable>
      ) : null}
    </View>
  );
});

const searchStyles = StyleSheet.create({
  wrap: {
    flexDirection: "row",
    alignItems: "center",
    gap: S.sm,
    borderRadius: R.pill,
    borderWidth: 1,
    paddingHorizontal: 14,
    height: 42,
  },
  input: {
    flex: 1,
    fontFamily: FONT.regular,
    fontSize: 15,
    // Kill the default vertical padding so the text centres in a 42pt pill.
    paddingVertical: 0,
  },
});

// ---------------------------------------------------------------------------
// ShowMoreRow — the collapse control under a truncated list.
//
// WHY A ROW AND NOT JUST "See all". The attention list is the tallest thing on
// this screen, and everything genuinely useful below it ("This week", upcoming
// deadlines, meeting insight) was being pushed off-screen by however many
// tasks happened to load. Truncating needs an affordance that says BOTH how
// much is hidden and how to get it back, in one tap, without leaving the
// screen — a "See all" that only re-filters is a dead end when the filter is
// already All.
// ---------------------------------------------------------------------------
export const ShowMoreRow = memo(function ShowMoreRow({
  expanded, hiddenCount, totalCount, onToggle,
}: {
  expanded: boolean;
  /** How many rows are currently hidden (0 when expanded). */
  hiddenCount: number;
  /** The full size of the list, for the collapsed-again label. */
  totalCount: number;
  onToggle: () => void;
}) {
  const { C } = useTheme();
  const label = expanded
    ? `Show less`
    : `Show all ${totalCount}`;
  const hint = expanded
    ? ""
    : `${hiddenCount} more`;
  return (
    <Pressable
      onPress={onToggle}
      accessibilityRole="button"
      accessibilityState={{ expanded }}
      accessibilityLabel={
        expanded ? "Show fewer tasks" : `Show all ${totalCount} tasks`
      }
      style={({ pressed }) => [
        moreStyles.row,
        {
          backgroundColor: C.surface,
          borderColor: C.border,
        },
        pressed && { opacity: 0.65 },
      ]}
    >
      <Text style={[moreStyles.label, { color: C.primary }]}>{label}</Text>
      {hint ? (
        <Text style={[moreStyles.hint, { color: C.textFaint }]}>{hint}</Text>
      ) : null}
      <Icon
        name={expanded ? "chevron.up" : "chevron.down"}
        size={15}
        tintColor={C.primary}
      />
    </Pressable>
  );
});

const moreStyles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: S.sm,
    borderRadius: R.md,
    borderWidth: 1,
    paddingVertical: 12,
    marginTop: S.sm,
  },
  label: { fontFamily: FONT.semibold, fontSize: 13.5 },
  hint: { fontFamily: FONT.regular, fontSize: 12.5 },
});
