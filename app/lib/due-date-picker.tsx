// lib/due-date-picker.tsx — choosing a task's due date on a calendar.
//
// WHY THIS EXISTS. Due dates were entered as FREE TEXT ("e.g. Friday, or
// 2026-08-29"), and the backend stores `due_date` verbatim — it does not parse
// or validate the string. So "Friday", "next week" and a typo like "2026-08-3"
// all persisted happily and then failed to place on the calendar, which only
// plots a real YYYY-MM-DD (see dueKeyOf / isDayKey in lib/task-insights.ts).
// A task with a due date the user could see in the form but not on the
// calendar is the exact "why isn't it showing up?" trap this component closes.
//
// So: pick a day, get a valid day. Every value this emits is a real
// YYYY-MM-DD, which means every task dated through it lands on the calendar.
//
// It is shared by the Add Task sheet, Quick Add and the Task Detail screen, so
// the three cannot drift into three different notions of what a due date is.
import { useMemo, useState } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { Icon } from "./icons";
import { S, R, CAPS, FONT, useTheme, ColorScale } from "./theme";
import { Button } from "./ui";
import {
  GRID_DAY_LABELS, addDays, addMonths, dayHeading, monthGrid, monthLabel,
  shortDate, startOfMonth, toDayKey,
} from "./task-insights";

/** The shortcuts most tasks actually use. Each is a real calendar day
 * computed from `now` — never a phrase like "Friday" that would be stored
 * unparsed. */
function quickChoices(now: Date) {
  return [
    { key: "today", label: "Today", value: toDayKey(now) },
    { key: "tomorrow", label: "Tomorrow", value: toDayKey(addDays(now, 1)) },
    // The coming Friday. On a Friday or weekend this is next week's, which is
    // what "end of week" means to someone setting a deadline today.
    { key: "friday", label: "Friday", value: toDayKey(nextWeekday(now, 5)) },
    { key: "week", label: "Next week", value: toDayKey(addDays(now, 7)) },
  ];
}

/** The next occurrence of a JS weekday (0=Sun … 6=Sat), never today. */
function nextWeekday(from: Date, weekday: number): Date {
  const delta = (weekday - from.getDay() + 7) % 7;
  return addDays(from, delta === 0 ? 7 : delta);
}

export type DueDatePickerProps = {
  visible: boolean;
  onClose: () => void;
  /** Current value as YYYY-MM-DD, or "" for none. */
  value: string;
  /** Emits a YYYY-MM-DD, or "" when the date is cleared. */
  onChange: (dayKey: string) => void;
  title?: string;
};

export function DueDatePicker({
  visible, onClose, value, onChange, title = "Due date",
}: DueDatePickerProps) {
  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={onClose}
    >
      {/* Mounted only while open, so the viewed month always opens on the
          current value rather than wherever it was left last time. */}
      {visible ? (
        <PickerBody
          value={value}
          onChange={onChange}
          onClose={onClose}
          title={title}
        />
      ) : null}
    </Modal>
  );
}

function PickerBody({
  value, onChange, onClose, title,
}: Omit<DueDatePickerProps, "visible">) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);

  const now = useMemo(() => new Date(), []);
  // Open on the month of the current value, or this month when unset.
  const [month, setMonth] = useState(() =>
    startOfMonth(value ? new Date(`${value}T00:00:00`) : now)
  );
  const [picked, setPicked] = useState(value);
  // Measured grid width -> cell size in points. Percentage widths combined
  // with aspectRatio inside a ScrollView depend on the parent having resolved
  // its own width, which is not guaranteed on the first pass; measuring makes
  // the cell size explicit and the grid impossible to collapse.
  const [gridW, setGridW] = useState(0);
  const cellSize = gridW > 0 ? Math.floor(gridW / 7) : 0;

  const cells = useMemo(() => monthGrid(month, now), [month, now]);
  const choices = useMemo(() => quickChoices(now), [now]);

  const commit = (dayKey: string) => {
    onChange(dayKey);
    onClose();
  };

  return (
    <View style={st.backdrop}>
      {/* The tap-to-dismiss area is a FLEXING SIBLING above the sheet, not a
          flex:1 wrapper around it — a scrim that fills the modal leaves the
          sheet no room to lay out, which is how this rendered as a dim screen
          with no calendar on it. Same structure as lib/add-task-sheet.tsx. */}
      <Pressable
        style={st.backdropTap}
        onPress={onClose}
        accessibilityRole="button"
        accessibilityLabel="Close date picker"
      />
      <View style={st.sheet}>
        <View style={st.grabber} />
        <View style={st.head}>
          <Text style={st.title}>{title}</Text>
          <Pressable
            onPress={onClose}
            hitSlop={10}
            accessibilityRole="button"
            accessibilityLabel="Close"
          >
            <Icon name="xmark" size={16} tintColor={C.textFaint} />
          </Pressable>
        </View>

        <ScrollView showsVerticalScrollIndicator={false}>
          {/* Shortcuts. Each writes a real day, so the common case never
              touches the grid. */}
          <View style={st.chipRow}>
            {choices.map((c) => {
              const on = picked === c.value;
              return (
                <Pressable
                  key={c.key}
                  onPress={() => commit(c.value)}
                  accessibilityRole="button"
                  accessibilityState={{ selected: on }}
                  accessibilityLabel={`${c.label}, ${shortDate(c.value)}`}
                  style={({ pressed }) => [
                    st.chip,
                    on && { backgroundColor: C.primarySoft, borderColor: C.primary },
                    pressed && { opacity: 0.7 },
                  ]}
                >
                  <Text style={[st.chipTxt, on && { color: C.primary }]}>
                    {c.label}
                  </Text>
                </Pressable>
              );
            })}
          </View>

          {/* Month bar */}
          <View style={st.monthBar}>
            <Pressable
              onPress={() => setMonth(addMonths(month, -1))}
              hitSlop={10}
              accessibilityRole="button"
              accessibilityLabel="Previous month"
              style={({ pressed }) => [st.navBtn, pressed && { opacity: 0.6 }]}
            >
              <Icon name="chevron.left" size={15} tintColor={C.textDim} />
            </Pressable>
            <Text style={st.monthTxt}>{monthLabel(month)}</Text>
            <Pressable
              onPress={() => setMonth(addMonths(month, 1))}
              hitSlop={10}
              accessibilityRole="button"
              accessibilityLabel="Next month"
              style={({ pressed }) => [st.navBtn, pressed && { opacity: 0.6 }]}
            >
              <Icon name="chevron.right" size={15} tintColor={C.textDim} />
            </Pressable>
          </View>

          <View style={st.dowRow}>
            {GRID_DAY_LABELS.map((d, i) => (
              <Text key={`${d}${i}`} style={st.dow}>
                {d}
              </Text>
            ))}
          </View>

          {/* The same Monday-first 6x7 grid the calendar screen uses, so a
              date means the same thing wherever it is chosen. */}
          <View
            style={st.grid}
            onLayout={(e) => setGridW(e.nativeEvent.layout.width - 8)}
          >
            {cells.map((c) => {
              const on = c.dayKey === picked;
              return (
                <Pressable
                  key={c.dayKey}
                  onPress={() => {
                    setPicked(c.dayKey);
                    commit(c.dayKey);
                  }}
                  accessibilityRole="button"
                  accessibilityState={{ selected: on }}
                  accessibilityLabel={dayHeading(c.dayKey, now)}
                  style={({ pressed }) => [
                    st.cell,
                    cellSize > 0 && { width: cellSize, height: cellSize },
                    on && { backgroundColor: C.primary },
                    !on && c.isToday && {
                      borderColor: C.primary,
                      borderWidth: 1.5,
                    },
                    pressed && !on && { opacity: 0.6 },
                  ]}
                >
                  <Text
                    style={[
                      st.cellTxt,
                      !c.inMonth && { color: C.textFaint, opacity: 0.5 },
                      // A past day stays selectable — backdating a task that
                      // was already owed is legitimate, and blocking it would
                      // just push people back to guessing.
                      c.isPast && c.inMonth && !on && { color: C.textDim },
                      on && { color: C.textOnPrimary },
                      !on && c.isToday && {
                        color: C.primary,
                        fontFamily: FONT.extrabold,
                      },
                    ]}
                  >
                    {c.date}
                  </Text>
                </Pressable>
              );
            })}
          </View>

          {/* Clearing is a real choice: a task with no deadline is normal. */}
          {picked ? (
            <View style={{ marginTop: S.md }}>
              <Button
                label="Clear due date"
                variant="ghost"
                onPress={() => commit("")}
              />
            </View>
          ) : null}
          <View style={{ height: S.lg }} />
        </ScrollView>
      </View>
    </View>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.35)" },
    backdropTap: { flex: 1 },
    sheet: {
      backgroundColor: C.surface,
      borderTopLeftRadius: R.xl,
      borderTopRightRadius: R.xl,
      paddingHorizontal: 20,
      paddingTop: S.sm,
      paddingBottom: S.xl,
      maxHeight: "86%",
    },
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
      marginBottom: S.md,
    },
    title: { ...T.h1 },
    chipRow: {
      flexDirection: "row",
      flexWrap: "wrap",
      gap: S.sm,
      marginBottom: S.lg,
    },
    chip: {
      borderRadius: R.pill,
      borderWidth: 1,
      borderColor: C.border,
      backgroundColor: C.surface,
      paddingHorizontal: 12,
      paddingVertical: 7,
    },
    chipTxt: { fontFamily: FONT.semibold, fontSize: 12, color: C.textDim },
    monthBar: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      marginBottom: S.sm,
    },
    navBtn: {
      width: 32,
      height: 32,
      borderRadius: 16,
      alignItems: "center",
      justifyContent: "center",
      backgroundColor: C.surface2,
    },
    monthTxt: { fontFamily: FONT.bold, fontSize: 15, color: C.text },
    dowRow: { flexDirection: "row", marginBottom: 4 },
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
      backgroundColor: C.surface2,
      borderRadius: R.card,
      paddingVertical: S.sm,
      paddingHorizontal: 4,
    },
    cell: {
      // Fallback only; the real size is measured and applied inline above.
      width: `${100 / 7}%`,
      aspectRatio: 1,
      alignItems: "center",
      justifyContent: "center",
      borderRadius: R.md,
      borderWidth: 1.5,
      borderColor: "transparent",
    },
    cellTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.text },
  });
}

/** The label for a due-date row: "Aug 29", "Today", "Tomorrow", or a prompt
 * when unset. Shared so every screen's row reads identically. */
export function dueDateLabel(dayKey: string, now: Date): string {
  if (!dayKey) return "No due date";
  const heading = dayHeading(dayKey, now);
  // dayHeading returns "Today"/"Tomorrow"/"Yesterday" for the near days and a
  // dated form otherwise; both are already what this row wants.
  return heading;
}

/** True when a stored due value is a real calendar day the calendar can plot.
 * Legacy tasks carry free text like "Friday" — those need re-picking, and the
 * UI says so rather than silently showing them as dateless. */
export function isPlottableDue(due: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(String(due || "").trim());
}
