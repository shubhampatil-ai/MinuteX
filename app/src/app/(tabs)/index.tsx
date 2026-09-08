// src/app/(tabs)/index.tsx — MinuteX: every brief, newest first, all sources.
//
// "Workspace" edition. MinuteX is a stack of soft, shadowed cards on a light
// gray canvas: a masthead, a stat strip, then meeting cards with a rounded
// source-icon bubble, a bold headline, a quiet summary, and a blue accent
// reserved for the single primary action per row. Recordings from every source
// (MinuteX device, phone, uploaded file) appear together. Every state is
// designed: skeletons while loading, a stated empty state, error with retry,
// pull-to-refresh, and a background poll while anything is still being
// written up.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, Animated, Easing, Pressable, RefreshControl, ScrollView, SectionList,
  StyleSheet, Text, View,
  type LayoutChangeEvent, type NativeScrollEvent, type NativeSyntheticEvent,
} from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Icon, type IconName } from "../../../lib/icons";
import { S, R, ELEV, CAPS, FONT, TABULAR, useTheme, ColorScale } from "../../../lib/theme";
import { NotificationBell } from "../../../lib/notification-bell";
import {
  Avatar, Button, Chip, EmptyState, ErrorText, IconCircle, SearchBar,
  SkeletonCard,
} from "../../../lib/ui";
import { Waveform } from "../../../lib/waveform";
import { useDevice } from "../../../lib/device-context";
import {
  getRecordings, clearToken, trashRecording, isStillUploading,
  RecordingSummary, ApiError, getAllTasks, getWorkspaceMembers, getMe,
  ApiMember,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { fmtDuration, sourceMeta, statusMeta } from "../../../lib/sources";
import {
  useUploads, dismissUpload, retryUpload, abandonUpload,
} from "../../../lib/uploads";

type Filter = "all" | "ready" | "processing";
type Section = { title: string; data: RecordingSummary[] };

// "Monday, 3 August" — the masthead dateline.
function fmtToday() {
  return new Date().toLocaleDateString(undefined, {
    weekday: "long", day: "numeric", month: "long",
  });
}

function fmtTime(iso: string) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

// "3 August" for this year, "3 August 2025" once the year differs — an old
// brief should never be ambiguous about which August it came from.
function fmtDay(d: Date) {
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, {
    day: "numeric", month: "long", ...(sameYear ? {} : { year: "numeric" }),
  });
}

// Today and yesterday read as words; anything older gets its actual date, so
// the row's time of day always has a day to hang off.
function dayLabel(iso: string): string {
  const d = new Date(iso);
  if (!iso || isNaN(d.getTime())) return "Undated";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  if (t >= today) return "Today";
  if (t >= today - 864e5) return "Yesterday";
  return fmtDay(d);
}

// The per-row stamp: "14:32" for today, "Yesterday · 14:32", and a real date
// once the brief is older than that. Rows carry their own day so the stamp
// still reads correctly away from its section header.
function fmtWhen(iso: string) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const day = dayLabel(iso);
  return day === "Today" ? fmtTime(iso) : `${day} · ${fmtTime(iso)}`;
}

// Group recordings into one section per day, newest first. Days older than
// yesterday are titled with their real date rather than pooled into a vague
// "This week" / "Earlier" bucket.
function groupByDate(items: RecordingSummary[]): Section[] {
  const sorted = [...items].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const sections: Section[] = [];
  for (const r of sorted) {
    const title = dayLabel(r.created_at);
    const last = sections[sections.length - 1];
    if (last && last.title === title) last.data.push(r);
    else sections.push({ title, data: [r] });
  }
  return sections;
}

// Total captured time this week, rendered "4h 12m" for the masthead.
// duration arrives as number | string | null — DynamoDB Decimals serialize as
// strings — so coerce before summing.
function weekCaptured(items: RecordingSummary[]) {
  const since = Date.now() - 7 * 864e5;
  const secs = items.reduce((acc, r) => {
    const t = new Date(r.created_at).getTime();
    if (isNaN(t) || t < since) return acc;
    const d = Number(r.duration);
    return acc + (isFinite(d) ? d : 0);
  }, 0);
  if (!secs) return null;
  const h = Math.floor(secs / 3600);
  const m = Math.round((secs % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

// Indeterminate progress — a hairline sweeping while the device uploads.
// Square ends, ink-weight: a rule that happens to move, not a candy bar.
function SweepBar({ color, track }: { color: string; track: string }) {
  const x = useRef(new Animated.Value(0)).current;
  const [w, setW] = useState(0);
  useEffect(() => {
    const loop = Animated.loop(
      Animated.timing(x, { toValue: 1, duration: 1300, easing: Easing.inOut(Easing.ease), useNativeDriver: true })
    );
    loop.start();
    return () => loop.stop();
  }, [x]);
  const translateX = x.interpolate({ inputRange: [0, 1], outputRange: [-80, Math.max(w, 80)] });
  return (
    <View
      style={{ height: 3, backgroundColor: track, overflow: "hidden", marginTop: 10 }}
      onLayout={(e) => setW(e.nativeEvent.layout.width)}
    >
      <Animated.View style={{ width: 80, height: 3, backgroundColor: color, transform: [{ translateX }] }} />
    </View>
  );
}

// Determinate progress — real bytes on the wire, reported by the native
// uploader. Same 3px rule as SweepBar so the two are interchangeable; this
// one just knows how far along it is.
function ProgressBar({ value, color, track }: { value: number; color: string; track: string }) {
  const pct = Math.max(0, Math.min(1, value));
  return (
    <View style={{ height: 3, backgroundColor: track, overflow: "hidden", marginTop: 10 }}>
      <View style={{ width: `${pct * 100}%`, height: 3, backgroundColor: color }} />
    </View>
  );
}

// "1.4 MB of 12.8 MB" — shown under an in-flight upload so a slow transfer
// looks like progress rather than a hang.
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// One cell of the stat strip under the masthead.
function Stat({
  value, label, tint, last, C,
}: { value: string; label: string; tint?: string; last?: boolean; C: ColorScale }) {
  return (
    <View style={{
      flex: 1, paddingHorizontal: 14, paddingVertical: 12,
      borderRightWidth: last ? 0 : 1, borderRightColor: C.border,
    }}>
      {/* TABULAR carries fontFamily (mono) — spread it FIRST so the sans
          family below wins. Stat figures are extrabold with tabular figures,
          not mono; mono is reserved for timecodes. */}
      <Text style={{ ...TABULAR, fontFamily: FONT.extrabold, fontSize: 20, color: tint ?? C.text }}>
        {value}
      </Text>
      <Text style={{ ...CAPS, fontSize: 10, letterSpacing: 1.2, color: C.textFaint, marginTop: 2 }}>
        {label}
      </Text>
    </View>
  );
}

type WorkspaceTile = {
  key: string; label: string; icon: IconName; path: string; badge?: string;
};

// The four workspace destinations, in one place. Both the full row and the
// collapsed strip in the sticky bar render from this, so the two can never
// drift apart on destinations, icons, or the open-task badge.
function workspaceTiles(openTasks: number | null): WorkspaceTile[] {
  return [
    {
      key: "tasks", label: "Tasks", icon: "checklist", path: "/tasks",
      // Capped at 99+: the badge is a glanceable "how much", and a real
      // account already carries 61 open tasks, so three digits would either
      // overflow the circle or shrink the type past legibility.
      badge: openTasks != null && openTasks > 0
        ? (openTasks > 99 ? "99+" : String(openTasks))
        : undefined,
    },
    { key: "calendar", label: "Calendar", icon: "calendar", path: "/calendar" },
    { key: "contacts", label: "People", icon: "person.2.fill", path: "/contacts" },
  ];
}

// The collapsed workspace strip — four icons that live in the sticky control
// bar once the full row has scrolled away. Icon-only, sized to sit beside the
// search field without out-shouting it; the labels are gone but the badge
// stays, because "4 open tasks" is the reason to tap.
function WorkspaceStrip({
  st, C, openTasks, onPress,
}: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  openTasks: number | null;
  onPress: (path: string) => void;
}) {
  return (
    <View style={st.wsStrip}>
      {workspaceTiles(openTasks).map((t) => (
        <Pressable
          key={t.key}
          onPress={() => onPress(t.path)}
          hitSlop={4}
          style={({ pressed }) => [st.wsStripBtn, pressed && { opacity: 0.6 }]}
          accessibilityRole="button"
          accessibilityLabel={t.badge ? `${t.label}, ${t.badge} open` : t.label}
        >
          <View style={st.wsIconWrap}>
            <Icon name={t.icon} tintColor={C.textDim} size={18} />
            {t.badge ? (
              <View style={st.wsStripBadge}>
                <Text style={st.wsStripBadgeTxt}>{t.badge}</Text>
              </View>
            ) : null}
          </View>
        </Pressable>
      ))}
    </View>
  );
}

// The Workspace row — Tasks / Calendar / Folders / People, directly on
// MinuteX.
//
// These were briefly buried under You › Organize, which was wrong: they are
// places you WORK, not preferences you set once. Tasks especially — "what do
// I owe" is a daily question, and it does not belong behind two taps in a
// settings list.
//
// Tasks leads and carries a live count, so the row reports state rather than
// just offering navigation. The count is omitted (not shown as 0) when it
// could not be fetched, because a wrong 0 reads as "nothing to do".
//
// This full row scrolls away with the masthead; WorkspaceStrip above keeps
// the same four destinations reachable from the sticky bar once it has gone.
function WorkspaceRow({
  st, C, openTasks, onPress,
}: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  openTasks: number | null;
  onPress: (path: string) => void;
}) {
  const tiles = workspaceTiles(openTasks);
  return (
    <View style={st.wsRow}>
      {tiles.map((t, i) => (
        <Pressable
          key={t.key}
          onPress={() => onPress(t.path)}
          style={({ pressed }) => [
            st.wsTile,
            i < tiles.length - 1 && {
              borderRightWidth: 1, borderRightColor: C.border,
            },
            pressed && { opacity: 0.6 },
          ]}
          accessibilityRole="button"
          accessibilityLabel={
            t.badge ? `${t.label}, ${t.badge} open` : t.label
          }
        >
          <View style={st.wsIconWrap}>
            <Icon name={t.icon} tintColor={C.primary} size={19} />
            {t.badge ? (
              <View style={st.wsBadge}>
                <Text style={st.wsBadgeTxt}>{t.badge}</Text>
              </View>
            ) : null}
          </View>
          <Text style={st.wsLabel}>{t.label}</Text>
        </Pressable>
      ))}
    </View>
  );
}

// MinuteX's top-left device pill — the hardware's entry point since the
// bottom bar went to three tabs (Desk · Record · You). It reports the live
// link in one glance and taps through to /devices for the full dashboard.
//
// Four states, because "connected or not" isn't the whole truth: mid-connect
// is its own state (the user just tapped Reconnect and deserves to see it),
// and never-paired is different from paired-but-away — the first needs a
// pairing trip, the second just needs the device switched on. Recording
// outranks all of them: if the hardware is capturing, that's the headline.
function DevicePill({
  st, C,
}: { st: ReturnType<typeof buildStyles>; C: ColorScale }) {
  const router = useRouter();
  const { device, status, connState } = useDevice();
  const connected = connState === "connected";
  const linking = connState === "connecting" || connState === "reconnecting";
  const recording = connected && status?.isRecording === true;

  // Danger red is reserved for recording — an idle-but-offline device is not
  // an error, so it gets the neutral faint treatment rather than a red alarm.
  const { tint, label } =
    recording ? { tint: C.danger, label: "Recording" }
      : connected ? { tint: C.success, label: device?.name ?? "Device" }
        : linking ? { tint: C.warn, label: "Connecting" }
          : device ? { tint: C.textFaint, label: "Device off" }
            : { tint: C.textFaint, label: "No device" };

  // Tinted background only when there's something to say; an absent or
  // sleeping device sits on the plain inset surface.
  const live = recording || connected || linking;

  return (
    <View style={st.deviceRow}>
      <Pressable
        onPress={() => router.push("/devices")}
        style={({ pressed }) => [
          st.devicePill,
          {
            backgroundColor: live ? tint + "1A" : C.surface2,
            borderColor: live ? tint + "40" : C.border,
          },
          pressed && { opacity: 0.7 },
        ]}
        accessibilityRole="button"
        accessibilityLabel={`Device: ${label}. Open device settings`}
      >
        <View style={[st.deviceDot, { backgroundColor: tint }]} />
        <Icon name="cpu.fill" tintColor={live ? tint : C.textFaint} size={13} />
        <Text style={[st.deviceLabel, { color: live ? tint : C.textDim }]} numberOfLines={1}>
          {label}
        </Text>
      </Pressable>
    </View>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    // No horizontal padding here any more. The list is full-bleed so the
    // sticky control layer can span edge to edge and paint an opaque band
    // across the whole width; the 20px gutter now sits on each block inside
    // (dashboard, controls, section heads, cards) via `gutter`.
    container: { flex: 1, backgroundColor: C.bg },
    gutter: { paddingHorizontal: 20 },
    // The pinned bar: search, plus the workspace icons once they collapse.
    // Sits above the list as a sibling, so it never scrolls. Opaque and
    // hairline-separated, because cards scroll beneath its lower edge.
    bar: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
      paddingHorizontal: 20, paddingTop: S.sm, paddingBottom: S.md,
      backgroundColor: C.bg,
      borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.border,
    },
    hidden: { display: "none" as const },
    // The collapsed workspace strip. Icon-only and tucked against the search
    // field: it must be reachable at a glance without competing with it.
    wsStrip: { flexDirection: "row" as const, alignItems: "center" as const, gap: 2 },
    wsStripBtn: { paddingHorizontal: 6, paddingVertical: 6 },
    // Smaller than the full row's badge (17px) to suit an 18px icon, and
    // bordered in the page background so it reads as sitting ON the icon
    // rather than colliding with the neighbouring one.
    wsStripBadge: {
      position: "absolute" as const, top: -5, right: -8,
      minWidth: 14, height: 14, borderRadius: 7, paddingHorizontal: 3,
      backgroundColor: C.primary, alignItems: "center" as const,
      justifyContent: "center" as const,
      borderWidth: 1.5, borderColor: C.bg,
    },
    wsStripBadgeTxt: {
      ...TABULAR, fontFamily: FONT.bold, fontSize: 8.5,
      color: C.textOnPrimary,
    },
    // Error / skeletons / empty state, under the sticky controls.
    listState: { paddingHorizontal: 20 },
    // Masthead: dateline over a bold display title, no rule — the page
    // background does the separating, cards carry their own edges.
    masthead: {
      flexDirection: "row" as const, alignItems: "flex-end" as const,
      justifyContent: "space-between" as const,
      paddingBottom: 4,
    },
    // Device pill — top-left of the page, above the dateline. This is where
    // the hardware lives now that the bottom bar is three tabs: connectivity
    // is a status you glance at, so it states the link and only then offers
    // the trip to /devices. Sized as a chip, not a button: it must not
    // out-shout "MinuteX" directly beneath it.
    deviceRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      marginBottom: 10,
    },
    devicePill: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 7,
      alignSelf: "flex-start" as const,
      paddingLeft: 9, paddingRight: 11, paddingVertical: 7,
      borderRadius: R.pill, borderWidth: 1,
    },
    // The state itself: a dot, coloured by link state, doing the work a
    // second line of text would otherwise do.
    deviceDot: { width: 7, height: 7, borderRadius: 4 },
    deviceLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.1,
    },
    dateline: { ...CAPS, fontSize: 11, color: C.textFaint },
    title: { ...T.display, marginTop: 6 },
    weekFig: { ...TABULAR, fontFamily: FONT.extrabold, fontSize: 22, lineHeight: 22, color: C.primary },
    weekLabel: { ...CAPS, fontSize: 10, letterSpacing: 1.4, color: C.textFaint },
    statStrip: {
      flexDirection: "row" as const, marginTop: 18,
      backgroundColor: C.surface, borderRadius: R.card, overflow: "hidden" as const,
      shadowColor: C.shadow, ...ELEV.sm,
    },
    // Workspace row: same card stock and hairline dividers as the stat strip
    // directly above it, so the two read as one block of "where things are"
    // rather than two competing widgets.
    wsRow: {
      flexDirection: "row" as const, marginTop: S.sm,
      backgroundColor: C.surface, borderRadius: R.card,
      overflow: "hidden" as const, shadowColor: C.shadow, ...ELEV.sm,
    },
    wsTile: {
      flex: 1, alignItems: "center" as const, justifyContent: "center" as const,
      paddingVertical: 13, gap: 6,
    },
    wsIconWrap: { position: "relative" as const },
    wsLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.1,
      color: C.textDim,
    },
    // Count badge on the Tasks tile. Sits on the icon rather than beside the
    // label so the three tiles keep identical text metrics.
    wsBadge: {
      position: "absolute" as const, top: -5, right: -11,
      minWidth: 17, height: 17, borderRadius: 9, paddingHorizontal: 4,
      backgroundColor: C.primary, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    wsBadgeTxt: {
      ...TABULAR, fontFamily: FONT.bold, fontSize: 10,
      color: C.textOnPrimary,
    },
    // Live bar: blue-accented card, the one thing on the page happening now.
    liveBar: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 10,
      marginTop: S.lg, backgroundColor: C.primary, borderRadius: R.card,
      paddingHorizontal: 16, paddingVertical: 14,
      shadowColor: C.shadow, ...ELEV.md,
    },
    liveDot: { width: 8, height: 8, borderRadius: 5, backgroundColor: C.textOnPrimary },
    liveTitle: { fontFamily: FONT.bold, fontSize: 13, color: C.textOnPrimary },
    liveSub: { fontFamily: FONT.regular, fontSize: 11, color: "rgba(255,255,255,0.8)", marginTop: 1 },
    // Transfer / upload notices — soft shadowed cards, matching the rest.
    notice: {
      marginTop: S.md, backgroundColor: C.surface, borderRadius: R.card, padding: 14,
      shadowColor: C.shadow, ...ELEV.sm,
    },
    noticeTitle: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.text },
    noticeSub: { ...T.caption, marginTop: 2 },
    filters: { flexDirection: "row" as const, gap: 7, marginTop: S.lg, marginBottom: 2 },
    // The "whose meetings" row. Gap matches `filters` so the two read as one
    // filter block; the padding is on the contentContainer because the row
    // scrolls horizontally.
    whoseRow: { flexDirection: "row" as const, gap: 7, marginTop: 9, paddingRight: S.lg },
    // "Recorded by" on another person's brief. Sits under the headline, at
    // caption weight — it is provenance, not the point of the card.
    recordedBy: {
      flexDirection: "row" as const, alignItems: "center" as const,
      gap: 6, marginTop: 7,
    },
    recordedByText: {
      fontFamily: FONT.medium, fontSize: 11.5, color: C.textDim, flexShrink: 1,
    },
    // Day header. Sticky now (RN sticks all section headers or none), so it
    // keeps its opaque background and carries the gutter itself. marginTop
    // became paddingTop: a sticky header's margin sits outside the pinned
    // box, which would leave a transparent strip above it with cards
    // visible through the gap.
    sectionHead: {
      flexDirection: "row" as const, alignItems: "baseline" as const,
      justifyContent: "space-between" as const,
      paddingHorizontal: 20,
      paddingTop: 16, paddingBottom: 8, marginBottom: 10,
      backgroundColor: C.bg,
    },
    // A brief card: rounded, shadowed surface — the primary visual unit.
    // Design unchanged; the 20px gutter it used to inherit from `container`
    // is now marginHorizontal, since the list itself is full-bleed.
    brief: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      marginHorizontal: 20,
      marginBottom: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    briefTop: { flexDirection: "row" as const, alignItems: "flex-start" as const, gap: S.md },
    kicker: { ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.4 },
    // Time + duration is a timecode — mono, per the type system's third job.
    briefMeta: { ...TABULAR, fontSize: 11, color: C.textFaint },
    headline: { ...T.headlineSm, marginTop: 5 },
    summary: { ...T.bodyDim, marginTop: 7 },
    metaRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 14, marginTop: 11 },
    inlineRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 5 },
    actionsTxt: { fontFamily: FONT.semibold, fontSize: 11.5, color: C.text },
    // Still-writing rows show progress instead of a summary.
    progressRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 8, marginTop: 9 },
    progressTrack: { flex: 1, height: 4, backgroundColor: C.surface2, borderRadius: 2, overflow: "hidden" as const },
    progressFill: { width: "62%", height: "100%", backgroundColor: C.primary, borderRadius: 2 },
    progressTxt: { fontFamily: FONT.semibold, fontSize: 11, color: C.primary },
  });
}

export default function DeskScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const { device, status, connState } = useDevice();
  const [items, setItems] = useState<RecordingSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  // WHOSE meetings — the "per user" filter, for OWNER and MANAGER only.
  //
  // WHY THOSE TWO AND NOBODY ELSE. A MEMBER cannot read a colleague's
  // meetings in the first place (_can_read_meeting gates on
  // CAP_VIEW_ALL_MEETINGS, which starts at MANAGER), so for them every
  // person-chip except their own would return an empty list. That is not a
  // filter, it is a row of dead ends that also advertises who else is in the
  // organisation as if their meetings were one tap away.
  //
  // `view_all_meetings` is read from the server's own capability map rather
  // than re-derived from the role here — one source of truth, and it stays
  // correct if the capability's minimum role ever changes.
  //
  // This is PRESENTATION ONLY. The server applies ?user_id= after its own
  // read check either way, so hiding the control is a UX decision, never the
  // security boundary (see TestUserFilterCannotWiden).
  const { isOrganisation, activeId, active } = useWorkspace();
  const canSeeEveryones = !!active?.capabilities?.view_all_meetings;
  const [whose, setWhose] = useState("");
  const [members, setMembers] = useState<ApiMember[]>([]);
  const [myUserId, setMyUserId] = useState("");
  // Open-task count for the Workspace row. A number is what makes that row
  // worth a place on MinuteX rather than being pure navigation chrome —
  // "3 open" is a reason to tap; a bare "Tasks" label is not.
  const [openTasks, setOpenTasks] = useState<number | null>(null);
  // Whether the workspace tiles have collapsed into the pinned bar.
  //
  // The tiles are NAVIGATION, not decoration: reaching Tasks or Calendar must
  // never require scrolling a long list back to offset 0. So the full row
  // scrolls away with the masthead and a compact icon strip takes its place
  // in the pinned bar.
  //
  // The threshold is MEASURED, never guessed. An earlier attempt hardcoded
  // 120px; the row actually clears the top around 300, so between the two the
  // full row and the strip were both on screen — the same four destinations
  // and the same badge, duplicated. `wsBottom` is the row's own bottom edge
  // reported by onLayout, so the strip can only appear once the row it
  // replaces has genuinely gone.
  const [collapsed, setCollapsed] = useState(false);
  const wsBottom = useRef(0);
  const onWorkspaceLayout = useCallback((e: LayoutChangeEvent) => {
    const { y, height } = e.nativeEvent.layout;
    // `y` is relative to the dashboard (the ListHeaderComponent), whose own
    // top sits at contentContainerStyle.paddingTop in scroll coordinates —
    // so add it to compare against contentOffset.y.
    wsBottom.current = S.lg + y + height;
  }, []);
  const onScroll = useCallback((e: NativeSyntheticEvent<NativeScrollEvent>) => {
    const y = e.nativeEvent.contentOffset.y;
    // Until onLayout has reported, collapse nothing — showing both is the
    // one outcome worth avoiding.
    const edge = wsBottom.current;
    if (!edge) return;
    // A 24px dead band: the strip appears once the row is fully past, and
    // only goes away again once the row is properly back, so a scroll resting
    // on the boundary cannot flicker between the two.
    setCollapsed((prev) => (prev ? y > edge - 24 : y > edge));
  }, []);

  const load = useCallback(async () => {
    setError("");
    try {
      // `whose` is only ever set on an organisation, and the server ignores
      // it on a personal workspace, so this is safe to pass unconditionally.
      setItems(await getRecordings({ userId: whose }));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { await clearToken(); router.replace("/login"); return; }
      setError(e instanceof ApiError ? e.message : "Could not load your briefs.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [router, whose]);

  useFocusEffect(useCallback(() => { load(); }, [load]));

  // Who the caller IS — so their own meetings are not labelled with their own
  // name on every row. One read, cached for the session by getMe's caller.
  useEffect(() => {
    let alive = true;
    getMe()
      .then((u) => { if (alive) setMyUserId(u.user_id); })
      .catch(() => { /* attribution just shows on every row; not fatal */ });
    return () => { alive = false; };
  }, []);

  // The people to offer in the "whose meetings" filter.
  //
  // Skipped entirely unless the caller may actually see everyone's meetings:
  // fetching a roster to populate a control a MEMBER never sees would be a
  // request per Desk visit for nothing. `whose` is cleared on the way out so
  // a demotion (or a workspace switch) cannot leave a stale filter pinned to
  // someone the caller can no longer query.
  useEffect(() => {
    if (!isOrganisation || !activeId || !canSeeEveryones) {
      setMembers([]);
      setWhose("");
      return;
    }
    let alive = true;
    getWorkspaceMembers(activeId)
      .then((r) => { if (alive) setMembers(r.members); })
      .catch(() => { if (alive) setMembers([]); });
    return () => { alive = false; };
  }, [isOrganisation, activeId, canSeeEveryones]);

  // The task count rides in its OWN request, deliberately not inside load():
  // MinuteX's job is to show briefs, and a task-service hiccup must not blank
  // the page or surface an error over it. A failure just leaves the badge off.
  useFocusEffect(useCallback(() => {
    let alive = true;
    getAllTasks({ status: "Open", limit: 200 })
      .then((r) => { if (alive) setOpenTasks(r.tasks.length); })
      .catch(() => { if (alive) setOpenTasks(null); });
    return () => { alive = false; };
  }, []));

  // Long-press a brief to move it to Trash. The gesture is the deliberate
  // choice here: react-native-gesture-handler is a dependency but has never
  // been mounted (there is no GestureHandlerRootView in the root layout), so
  // swipe-to-delete would mean introducing a whole gesture stack for one
  // row action. Long-press needs nothing the app isn't already doing, and
  // it can't be triggered by a scroll the way a swipe can.
  //
  // Nothing here destroys anything: the brief goes to Trash, where it can be
  // restored or permanently deleted. That is what makes clearing MinuteX in
  // a few presses safe — a mis-tap costs a trip to Trash, not a recording.
  const [deletingKey, setDeletingKey] = useState<string | null>(null);

  const confirmDelete = useCallback((item: RecordingSummary) => {
    const failed = statusMeta(item.status).kind === "failed";
    Alert.alert(
      failed ? "Move this failed brief to Trash?" : "Move this brief to Trash?",
      failed
        ? "Nothing was written up for this one. You can restore it later from Trash."
        : `“${item.title || "Untitled conversation"}” moves to Trash with its `
        + "transcript and everything written from it. You can restore it later.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Move to Trash",
          style: "destructive",
          onPress: async () => {
            const key = item.audio_s3_key;
            setDeletingKey(key);
            // Optimistic: drop the row immediately so a list of failures
            // clears at the speed the user taps, not the speed of the
            // network. A failure below puts it straight back via load().
            setItems((prev) => prev.filter((r) => r.audio_s3_key !== key));
            try {
              await trashRecording(key);
            } catch (e) {
              Alert.alert(
                "Couldn't move to Trash",
                isStillUploading(e)
                  ? "This recording is still being processed. Try again in a minute."
                  : e instanceof ApiError
                    ? e.message
                    : "Something went wrong. Please try again."
              );
              load();
            } finally {
              setDeletingKey(null);
            }
          },
        },
      ]
    );
  }, [load]);

  // In-flight phone/upload jobs from the shared UploadManager. Refresh the
  // list whenever a job lands so its row flips from "Uploading" without a
  // manual pull-to-refresh.
  const uploads = useUploads();
  const seenDone = useRef(new Set<string>());
  useEffect(() => {
    for (const j of uploads) {
      if (j.phase === "done" && !seenDone.current.has(j.id)) {
        seenDone.current.add(j.id);
        load();
      }
    }
  }, [uploads, load]);

  // The backend advances status (uploaded -> transcribing -> generating_ai
  // -> complete) without any push channel — poll gently while this screen is
  // focused AND something is actually still processing.
  const hasProcessing = items.some((r) => statusMeta(r.status).kind === "processing");
  useFocusEffect(useCallback(() => {
    if (!hasProcessing) return;
    const t = setInterval(load, 20_000);
    return () => clearInterval(t);
  }, [hasProcessing, load]));

  const sections = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = items.filter((r) => {
      const kind = statusMeta(r.status).kind;
      if (filter === "ready" && kind !== "ready") return false;
      if (filter === "processing" && kind === "ready") return false;
      if (!q) return true;
      return (
        (r.title || "").toLowerCase().includes(q) ||
        (r.summary || "").toLowerCase().includes(q) ||
        (r.language || "").toLowerCase().includes(q)
      );
    });
    return groupByDate(filtered);
  }, [items, query, filter]);

  const connected = connState === "connected";
  const isRecording = connected && status?.isRecording === true;
  const uploading = connected && !isRecording && (status?.pendingUploads ?? 0) > 0;

  // Stat strip. "Pending" counts briefs still being written — the Actions
  // tab that owns overdue promises isn't built yet, so the third cell reports
  // what this screen actually knows.
  const pendingCount = items.filter((r) => statusMeta(r.status).kind === "processing").length;
  const captured = weekCaptured(items);
  const filtering = !!query || filter !== "all";

  // The collapsing dashboard. This is the list's ListHeaderComponent rather
  // than a sibling View: as a sibling it permanently consumed ~450px of a
  // ~780px viewport, so the list it sat above could only ever show one and a
  // half cards. Inside the list it scrolls away under the sticky controls and
  // comes back on the way down — ordinary SectionList scrolling, with nothing
  // interpolated and nothing to snap.
  //
  // Only the masthead and stats truly leave: they are decoration, read on
  // arrival. The workspace tiles are navigation, so they reappear in the
  // sticky bar as WorkspaceStrip rather than making you scroll a long list
  // back to offset 0 to reach Tasks.
  const dashboard = (
    <View style={st.gutter}>
      {/* Device connectivity — top-left, above the masthead. Replaces the
          Device bottom tab. */}
      <DevicePill st={st} C={C} />

      {/* Masthead — today's date, the title, and the week's captured time */}
      <View style={st.masthead}>
        <View style={{ flex: 1 }}>
          <Text style={st.dateline}>{fmtToday()}</Text>
          <Text style={st.title}>MinuteX</Text>
        </View>
        <View style={{ flexDirection: "row", alignItems: "center", gap: S.sm }}>
          {captured ? (
            <View style={{ alignItems: "flex-end", gap: 3 }}>
              <Text style={st.weekFig}>{captured}</Text>
              <Text style={st.weekLabel}>captured this week</Text>
            </View>
          ) : null}
          {/* The notification centre's entry point. Reads its count from the
              shared context, so it stays in step with the centre itself. */}
          <NotificationBell />
        </View>
      </View>

      {/* Stat strip */}
      <View style={st.statStrip}>
        <Stat C={C} value={String(items.length)} label="Briefs" />
        <Stat C={C} value={device && status?.batteryLevel != null ? `${status.batteryLevel}%` : "—"} label="Device" />
        <Stat
          C={C}
          value={String(pendingCount)}
          label="In the works"
          tint={pendingCount ? C.primary : undefined}
          last
        />
      </View>

      {/* Workspace — Tasks / Calendar / Folders / People. On MinuteX itself,
          because these are daily destinations, not settings. onLayout reports
          where this row ends so the pinned strip knows exactly when it has
          scrolled past — see `collapsed`. */}
      <View onLayout={onWorkspaceLayout}>
        <WorkspaceRow
          st={st}
          C={C}
          openTasks={openTasks}
          onPress={(path) => router.push(path as any)}
        />
      </View>

      {/* Live recording bar */}
      {isRecording ? (
        <Pressable style={({ pressed }) => [st.liveBar, pressed && { opacity: 0.85 }]}
          onPress={() => router.navigate("/record")}>
          <View style={st.liveDot} />
          <View style={{ flex: 1 }}>
            <Text style={st.liveTitle}>On the record now</Text>
            <Text style={st.liveSub}>Your device is capturing — tap to view</Text>
          </View>
          <Icon name="arrow.right" tintColor={C.textOnPrimary} size={18} />
        </Pressable>
      ) : null}

      {/* Transfer notice — the device is pushing recordings up over Wi-Fi */}
      {uploading ? (
        <View style={st.notice}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: S.md }}>
            <Icon name="tray.and.arrow.up.fill" tintColor={C.primary} size={18} />
            <View style={{ flex: 1 }}>
              <Text style={st.noticeTitle}>
                Transferring {status!.pendingUploads} recording{(status!.pendingUploads ?? 0) > 1 ? "s" : ""}…
              </Text>
              <Text style={st.noticeSub}>They'll land on MinuteX once written up</Text>
            </View>
          </View>
          <SweepBar color={C.primary} track={C.border} />
        </View>
      ) : null}

      {/* Phone/upload jobs from the shared UploadManager — mirrors the
          transfer notice above so every source reports the same way */}
      {uploads.map((j) => (
        <View key={j.id} style={st.notice}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: S.md }}>
            <Icon
              name={j.phase === "failed" ? "exclamationmark.triangle.fill"
                : j.phase === "done" ? "checkmark.circle.fill"
                  // "waiting" is not a failure — the recording is safe on disk
                  // and the upload is queued. A warning triangle here would
                  // read as data loss, which is the opposite of the truth.
                  : j.phase === "waiting" ? "clock"
                    : "tray.and.arrow.up.fill"}
              tintColor={j.phase === "failed" ? C.danger
                : j.phase === "done" ? C.success
                  : j.phase === "waiting" ? C.warn
                    : C.primary}
              size={18}
            />
            <View style={{ flex: 1 }}>
              <Text style={st.noticeTitle} numberOfLines={1}>
                {j.phase === "failed" ? "Upload failed"
                  : j.phase === "done" ? "Uploaded"
                    : j.phase === "waiting" ? "Waiting to upload"
                      : `Uploading “${j.title}”…`}
              </Text>
              <Text style={st.noticeSub} numberOfLines={2}>
                {j.phase === "failed" ? (j.error || "Something went wrong.")
                  : j.phase === "done" ? "Writing it up now"
                    // Say plainly that the audio is safe. This banner is what a
                    // user sees after recording a meeting offline, and the one
                    // thing they need to know is that they haven't lost it.
                    : j.phase === "waiting" ? "Saved on this phone — it'll upload when you're back online"
                      : j.phase === "finalizing" ? "Finishing up…"
                        // Real byte counts while the transfer is live — a big file
                        // on a slow link should look busy, not stuck.
                        : j.progress != null && j.totalBytes
                          ? `${fmtBytes(j.bytesSent ?? 0)} of ${fmtBytes(j.totalBytes)}`
                          : j.source === "MOBILE" ? "Phone recording" : "Audio file"}
              </Text>
            </View>
            {/* Percentage sits opposite the title, in mono, so it lines up
                across stacked jobs. */}
            {j.phase === "uploading" && j.progress != null ? (
              <Text style={{ ...TABULAR, fontSize: 11.5, color: C.primary }}>
                {Math.round(j.progress * 100)}%
              </Text>
            ) : null}
            {/* Retry before dismiss: a failed upload is usually a dropped
                connection, and the file is still on disk. Only offered when
                the job kept its input (see retryUpload). */}
            {j.phase === "failed" || j.phase === "waiting" ? (
              <Pressable onPress={() => retryUpload(j.id)} accessibilityLabel="Retry upload"
                hitSlop={6}
                style={({ pressed }) => [
                  { flexDirection: "row", alignItems: "center", gap: 5 },
                  pressed && { opacity: 0.6 },
                ]}>
                <Icon name="arrow.clockwise" tintColor={C.primary} size={14} />
                <Text style={{ fontFamily: FONT.bold, fontSize: 12.5, color: C.primary }}>
                  {j.phase === "waiting" ? "Now" : "Retry"}
                </Text>
              </Pressable>
            ) : null}
            {/* Dismiss only hides the banner — it never deletes audio. A
                recording that genuinely can't be uploaded is removed through
                the explicit Discard action below, never by tidying a banner. */}
            {j.phase === "failed" || j.phase === "done" ? (
              <Pressable onPress={() => dismissUpload(j.id)} accessibilityLabel="Dismiss"
                style={({ pressed }) => pressed && { opacity: 0.6 }}>
                <Icon name="xmark" tintColor={C.textDim} size={16} />
              </Pressable>
            ) : null}
          </View>
          {/* Determinate whenever the uploader knows the total; the sweep is
              the honest fallback when it doesn't. A waiting job gets neither —
              nothing is moving, and an animated bar would imply it is. */}
          {j.phase !== "failed" && j.phase !== "done" && j.phase !== "waiting" ? (
            j.progress != null
              ? <ProgressBar value={j.progress} color={C.primary} track={C.border} />
              : <SweepBar color={C.primary} track={C.border} />
          ) : null}
          {/* The only path that deletes a local recording, and only for one
              that has genuinely failed. Kept behind a confirm because the
              audio is unrecoverable afterwards. */}
          {j.phase === "failed" ? (
            <Pressable
              onPress={() => Alert.alert(
                "Discard this recording?",
                "The audio is still on this phone. Discarding deletes it for good.",
                [
                  { text: "Keep it", style: "cancel" },
                  { text: "Discard", style: "destructive", onPress: () => abandonUpload(j.id) },
                ]
              )}
              accessibilityLabel="Discard recording"
              style={({ pressed }) => [{ marginTop: S.sm }, pressed && { opacity: 0.6 }]}
            >
              <Text style={{ fontFamily: FONT.semibold, fontSize: 12, color: C.textDim }}>
                Discard recording
              </Text>
            </Pressable>
          ) : null}
        </View>
      ))}

      {/* Filters. These scroll away with the dashboard rather than joining
          the pinned bar: a filter is set occasionally and then left alone,
          so it does not earn ~50px of permanent height the way search and
          navigation do. */}
      <View style={st.filters}>
        <Chip label="All" active={filter === "all"} onPress={() => setFilter("all")} />
        <Chip label="Filed" active={filter === "ready"} onPress={() => setFilter("ready")} />
        <Chip label="In the works" active={filter === "processing"} onPress={() => setFilter("processing")} />
      </View>

      {/* WHOSE meetings. Organisation only, and only once there is more than
          one person to choose between — a filter with a single option is
          chrome, not a control. Horizontally scrollable because an
          organisation can have more members than fit a row.

          A MEMBER sees this too, deliberately: the server intersects the
          filter with what they may already read, so for them it narrows
          their own + shared meetings rather than exposing anyone. */}
      {isOrganisation && canSeeEveryones && members.length > 1 ? (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          // This screen has a search field, so without persistTaps the first
          // tap on a person chip while the keyboard is up would be swallowed
          // dismissing it instead of applying the filter.
          keyboardShouldPersistTaps="handled"
          contentContainerStyle={st.whoseRow}
        >
          <Chip
            label="Everyone"
            active={whose === ""}
            onPress={() => setWhose("")}
          />
          {members.map((m) => (
            <Chip
              key={m.user_id}
              // Already a display name server-side (chosen -> derived ->
              // email), so this is never a raw uuid.
              label={m.user_id === myUserId ? "You" : (m.name || m.email || "")}
              active={whose === m.user_id}
              onPress={() => setWhose(
                whose === m.user_id ? "" : m.user_id)}
            />
          ))}
        </ScrollView>
      ) : null}
    </View>
  );

  return (
    <View style={[st.container, { paddingTop: insets.top }]}>
      {/* THE PINNED BAR — a sibling ABOVE the list, not inside it.

          It was briefly a sticky section header, which does not work: RN
          sticky headers are "the next header pushes the previous one off", so
          every date header displaced the search bar as it arrived and the
          controls vanished a screen or two down. Outside the list it simply
          never moves. One fixed bar plus one list is not a nested scroll
          view — the list below is still the only thing that scrolls. */}
      <View style={st.bar}>
        <SearchBar value={query} onChangeText={setQuery} placeholder="Search briefs"
          style={{ flex: 1 }} />
        {/* The workspace icons, revealed only once the full row has scrolled
            past. Hidden with `display: none` rather than unmounted so the
            bar's height never changes as it appears. */}
        <View style={collapsed ? undefined : st.hidden} pointerEvents={collapsed ? "auto" : "none"}>
          <WorkspaceStrip st={st} C={C} openTasks={openTasks}
            onPress={(path) => router.push(path as any)} />
        </View>
      </View>

      <SectionList
        sections={sections}
        keyExtractor={(i) => i.audio_s3_key}
        showsVerticalScrollIndicator={false}
        // Date headers stick within the list, so the day you are reading
        // stays labelled until the next one arrives.
        stickySectionHeadersEnabled
        // Drives the workspace collapse only. 64ms keeps the swap attached to
        // the gesture without re-rendering on every frame — and since
        // `collapsed` is a boolean behind a dead band, most of these events
        // change no state at all.
        onScroll={onScroll}
        scrollEventThrottle={64}
        ListHeaderComponent={dashboard}
        // paddingBottom clears the absolutely-positioned tab bar and the
        // floating record button, exactly as before.
        contentContainerStyle={{ paddingTop: S.lg, paddingBottom: 100 }}
        refreshControl={
          <RefreshControl refreshing={refreshing}
            onRefresh={() => { setRefreshing(true); load(); }} tintColor={C.primary} />
        }
        ListEmptyComponent={
          loading ? (
            <View style={st.listState}>
              <SkeletonCard />
              <SkeletonCard />
              <SkeletonCard />
            </View>
          ) : error ? (
            <View style={st.listState}>
              <ErrorText>{error}</ErrorText>
              <Button label="Try again" variant="secondary" onPress={() => { setLoading(true); load(); }}
                style={{ marginTop: S.md, alignSelf: "flex-start", paddingHorizontal: S.xl }} />
            </View>
          ) : (
            <View style={st.listState}>
              <EmptyState
                title={filtering ? "Nothing matches that" : "Nothing on MinuteX yet"}
                subtitle={filtering
                  ? "Try a different search or filter."
                  : "Record your next conversation and the first brief lands here in a couple of minutes."}
                action={filtering ? undefined : (
                  <Button label="Start recording" onPress={() => router.push("/new-recording")} />
                )}
              />
            </View>
          )
        }
        renderSectionHeader={({ section }) => {
          const s = section as Section;
          return (
            <View style={st.sectionHead}>
              <Text style={T.label}>{s.title}</Text>
              <Text style={{ ...T.caption }}>
                {s.data.length} brief{s.data.length === 1 ? "" : "s"}
              </Text>
            </View>
          );
        }}
        renderItem={({ item }) => {
          const status = statusMeta(item.status);
          const ready = status.kind === "ready";
          const failed = status.kind === "failed";
          const deleting = deletingKey === item.audio_s3_key;
          const source = sourceMeta(item);
          const duration = fmtDuration(item.duration);
          // Danger only for a genuinely failed brief; blue for the source
          // icon otherwise — "ready" gets a quiet success check, not a
          // loud color, since most rows are ready most of the time.
          const kickerColor = failed ? C.danger : C.textFaint;
          return (
            <Pressable
              style={({ pressed }) => [
                st.brief,
                pressed && { opacity: 0.7 },
                deleting && { opacity: 0.4 },
              ]}
              onPress={() => router.push({ pathname: "/recording/[key]", params: { key: item.audio_s3_key } })}
              onLongPress={() => confirmDelete(item)}
              delayLongPress={400}
              accessibilityHint="Long press to move this brief to Trash"
            >
              <View style={st.briefTop}>
                <IconCircle
                  name={failed ? "exclamationmark.triangle.fill" : source.icon}
                  tint={failed ? C.danger : C.primary}
                  bg={failed ? C.dangerSoft : C.primarySoft}
                  size={40}
                />
                <View style={{ flex: 1 }}>
                  {/* Kicker line: what kind of conversation, when, how long. */}
                  <View style={{ flexDirection: "row", alignItems: "baseline", gap: 8 }}>
                    <Text style={[st.kicker, { color: kickerColor }]}>
                      {failed ? "Couldn't finish" : source.label}
                    </Text>
                    <Text style={st.briefMeta}>
                      {fmtWhen(item.created_at)}{duration ? ` · ${duration}` : ""}
                    </Text>
                  </View>

                  {/* The headline is what the AI wrote */}
                  <Text style={st.headline} numberOfLines={3}>
                    {item.title || "Untitled conversation"}
                  </Text>

                  {/* WHO recorded it. Organisation only, and only for
                      someone ELSE's meeting: the server omits these fields
                      in a personal workspace, and "Recorded by you" on your
                      own row is noise on every row you own. */}
                  {item.recorded_by_name
                    && item.recorded_by !== myUserId ? (
                    <View style={st.recordedBy}>
                      <Avatar
                        name={item.recorded_by_name}
                        photoUri={item.recorded_by_avatar}
                        size={16}
                        fontSize={8}
                      />
                      <Text style={st.recordedByText} numberOfLines={1}>
                        {item.recorded_by_name}
                      </Text>
                    </View>
                  ) : null}
                </View>
                {ready ? (
                  <Icon name="checkmark.circle.fill" tintColor={C.success} size={18} />
                ) : null}
              </View>

              {ready ? (
                <>
                  {item.summary ? (
                    <Text style={st.summary} numberOfLines={3}>{item.summary}</Text>
                  ) : null}
                  {/* The list endpoint returns no participant data — that
                      only comes back on the detail fetch — so speaker
                      avatars live on the brief screen, not here. */}
                  <View style={st.metaRow}>
                    {item.language ? (
                      <Text style={{ ...T.caption }}>{item.language.toUpperCase()}</Text>
                    ) : null}
                    <View style={{ flex: 1, alignItems: "flex-end" }}>
                      <Waveform seed={item.audio_s3_key} bars={28} height={16}
                        color={C.border} dimColor={C.border} />
                    </View>
                  </View>
                </>
              ) : failed ? (
                <>
                  <Text style={st.summary} numberOfLines={2}>
                    We stopped rather than guess. Open it to see why.
                  </Text>
                  {/* Visible on failed rows only. Long-press covers every
                      brief, but it is invisible until someone discovers
                      it — and a desk full of failures is exactly when a
                      user needs the way out to be obvious. */}
                  <Pressable
                    onPress={() => confirmDelete(item)}
                    disabled={deleting}
                    hitSlop={8}
                    accessibilityLabel="Move this brief to Trash"
                    style={({ pressed }) => [
                      { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 12 },
                      pressed && { opacity: 0.6 },
                    ]}
                  >
                    <Icon name="trash" tintColor={C.danger} size={14} />
                    <Text style={{ fontFamily: FONT.bold, fontSize: 12.5, color: C.danger }}>
                      {deleting ? "Moving…" : "Move to Trash"}
                    </Text>
                  </Pressable>
                </>
              ) : (
                // Still being written — show the pipeline, not a summary.
                <View style={st.progressRow}>
                  <View style={st.progressTrack}><View style={st.progressFill} /></View>
                  <Text style={st.progressTxt}>{status.label}…</Text>
                </View>
              )}
            </Pressable>
          );
        }}
      />
    </View>
  );
}