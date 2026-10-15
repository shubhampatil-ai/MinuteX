// src/app/(tabs)/index.tsx — the Desk: find a brief, see what's recent, record.
//
// The hierarchy is deliberate and short:
//
//   MinuteX                            tasks · notifications · you
//   [ Search briefs ]                        ┐ pinned, never scroll away
//   [ All | Filed | In the works ]  Filter   ┘
//   Today
//     brief
//     brief
//   Yesterday …
//                                     (record button in the tab bar)
//
// What it is NOT is a dashboard. It is a brief INBOX: the list is the
// content, and everything above it exists to find something in it. The row of
// Tasks / Calendar / People cards is gone from here — Tasks (the one that is
// only reachable from this screen) is a badged icon in the header; Calendar
// stays one tap inside Tasks; People stays under You › Contacts. The stat
// strip and the row of equal-weight icon tiles are gone, as are the decorative waveform on every row (it was
// drawn from a hash of the key, not the audio) and the progress bar that was
// always 62% full whatever the backend said. A brief in the works now shows
// the stage it is actually at.
//
// Every state is designed: skeletons while loading, a stated empty state,
// error with retry, pull-to-refresh, and a background poll while anything is
// still being written up. Recordings from every source (MinuteX device, phone,
// uploaded file) appear together.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, AppState, Pressable, RefreshControl, ScrollView, SectionList,
  StyleSheet, Text, View,
} from "react-native";
import Reanimated, {
  cancelAnimation, Easing, useAnimatedStyle, useSharedValue, withRepeat, withTiming,
} from "react-native-reanimated";
import { useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Icon } from "../../../lib/icons";
import { S, R, FONT, GUTTER, useTheme, ColorScale } from "../../../lib/theme";
import { PulseDot } from "../../../lib/motion";
import { NotificationBell } from "../../../lib/notification-bell";
import {
  Avatar, Button, EmptyState, ErrorText, SearchBar, SkeletonCard,
} from "../../../lib/ui";
import {
  AppHeader, BottomSheet, BriefCard, IconButton, SegmentedControl, SectionHeader,
  SheetAction, type GroupPosition,
} from "../../../lib/kit";
import { useDevice } from "../../../lib/device-context";
import { useRecorder } from "../../../lib/use-recorder";
import { isPaused } from "../../../lib/rec-store";
import {
  trashRecording, isStillUploading,
  RecordingSummary, ApiError, getAllTasks, getWorkspaceMembers,
  ApiMember, getSharedWithMe, type SharedRecordingSummary,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { useQueryClient } from "@tanstack/react-query";
import { savedAtLabel } from "../../../lib/offline-cache-rules";
import { dropMeetingFromLists, useMe, useRecordings } from "../../../lib/shared-data";
import { fmtDuration, sourceMeta, statusMeta } from "../../../lib/sources";
import { stageLabel } from "../../../lib/processing";
import { offerBriefReadyPrompt } from "../../../lib/push";
import {
  useUploads, dismissUpload, retryUpload, abandonUpload,
} from "../../../lib/uploads";

type Filter = "all" | "ready" | "processing";

// One source for the status tabs, so the width measurement below measures
// exactly the labels the control renders.
const STATUS_OPTIONS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "ready", label: "Filed" },
  { key: "processing", label: "In the works" },
];

// SegmentedControl geometry (lib/kit.tsx): 2pt track padding each side, and
// each segment pads its label by S.xs each side. BREATHING is the minimum
// clear space either side of the longest label so it never kisses the thumb.
const SEG_TRACK_PAD = 4;
const SEG_LABEL_PAD = 8;
const SEG_BREATHING = 8;
type Section = { title: string; data: RecordingSummary[] };

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

function positionIn(index: number, count: number): GroupPosition {
  if (count === 1) return "single";
  if (index === 0) return "first";
  if (index === count - 1) return "last";
  return "middle";
}

// Indeterminate progress — a hairline sweeping while bytes move and the
// uploader cannot report a total. It claims only "something is moving".
function SweepBar({ color, track }: { color: string; track: string }) {
  const x = useSharedValue(0);
  const [w, setW] = useState(0);
  useEffect(() => {
    x.set(withRepeat(withTiming(1, { duration: 1300, easing: Easing.inOut(Easing.ease) }), -1, false));
    return () => cancelAnimation(x);
  }, [x]);
  const sweep = useAnimatedStyle(() => ({
    transform: [{ translateX: -80 + x.get() * (Math.max(w, 80) + 80) }],
  }));
  return (
    <View
      style={{ height: 3, backgroundColor: track, overflow: "hidden", marginTop: 10, borderRadius: 2 }}
      onLayout={(e) => setW(e.nativeEvent.layout.width)}
    >
      <Reanimated.View style={[{ width: 80, height: 3, backgroundColor: color }, sweep]} />
    </View>
  );
}

// Determinate progress — REAL bytes on the wire, reported by the native
// uploader. The only bar on this screen that shows a fraction, because it is
// the only thing here that genuinely knows one.
function ProgressBar({ value, color, track }: { value: number; color: string; track: string }) {
  const pct = Math.max(0, Math.min(1, value));
  return (
    <View style={{ height: 3, backgroundColor: track, overflow: "hidden", marginTop: 10, borderRadius: 2 }}>
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

// The open-task count on the header's Tasks button. Capped at 99+ (a real
// account already carries 61 open tasks), and omitted — never "0" — when it
// could not be fetched, because a wrong 0 reads as "nothing to do".
function tasksBadge(openTasks: number | null): string | undefined {
  if (openTasks == null || openTasks <= 0) return undefined;
  return openTasks > 99 ? "99+" : String(openTasks);
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    // The pinned header: title + actions, then search. Opaque and hairline-
    // separated, because the list scrolls beneath its lower edge.
    pinned: {
      paddingHorizontal: GUTTER, paddingBottom: S.md, backgroundColor: C.bg,
      borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.border,
    },
    // Search, then the filters, both pinned: finding a brief never needs a
    // scroll back to the top.
    filterRow: { flexDirection: "row", alignItems: "center", gap: S.sm, marginTop: S.sm },
    searchRow: { flexDirection: "row", alignItems: "center", gap: S.sm, marginTop: S.sm },
    // Off-screen, invisible copies of the tab labels — measured, never seen.
    // A fixed wide box so each label lays out at its natural single-line
    // width; alignSelf on each Text stops the column stretching it.
    measure: { position: "absolute", opacity: 0, left: 0, top: 0, width: 1000 },
    // The people filter's entry point. Deliberately small: it is a
    // refinement, and must not read as a fourth segment.
    filterBtn: {
      flexDirection: "row", alignItems: "center", gap: 6, flexShrink: 0,
      minHeight: 40, maxWidth: 124, paddingHorizontal: S.md, borderRadius: R.pill,
      borderWidth: StyleSheet.hairlineWidth,
    },
    filterTxt: { fontFamily: FONT.semibold, fontSize: 13.5, flexShrink: 1 },
    gutter: { paddingHorizontal: GUTTER },
    listState: { paddingHorizontal: GUTTER },

    // A live state that outranks the list: something is being recorded now.
    liveBar: {
      flexDirection: "row", alignItems: "center", gap: S.md,
      marginTop: S.lg, backgroundColor: C.surface, borderRadius: R.card,
      borderWidth: 1, borderColor: C.recording,
      paddingHorizontal: S.lg, minHeight: 56,
    },
    liveTitle: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    liveSub: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 1 },

    // Transfer / upload notices — flat, bordered, one per job.
    notice: {
      marginTop: S.md, backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      borderWidth: StyleSheet.hairlineWidth, borderColor: C.border,
    },
    noticeTitle: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    noticeSub: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 17, color: C.textDim, marginTop: 2 },

    recordedBy: { flexDirection: "row", alignItems: "center", gap: 6 },
    recordedByText: { fontFamily: FONT.medium, fontSize: 12, color: C.textDim, flexShrink: 1 },

    // Day header. Sticky (RN sticks all section headers or none), so it keeps
    // an opaque background and carries the gutter itself.
    sectionHead: { paddingHorizontal: GUTTER, paddingTop: S.lg, backgroundColor: C.bg },
    itemWrap: { paddingHorizontal: GUTTER },
  });
}

export default function DeskScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);
  const { device, status, connState } = useDevice();
  const phone = useRecorder();
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

  // THE MEETINGS LIST, through the on-device cache (lib/query-client.tsx,
  // local-first phase 1): the last good list paints at once — also offline —
  // and the server is asked again in the background. Keyed on the account,
  // the workspace and the filter, so one account never sees another's list;
  // wiped on sign-out.
  const queryClient = useQueryClient();
  // Shared with Profile (lib/shared-data.ts): one request, one saved copy.
  const { query: list, key: listKey } = useRecordings(whose);
  const items = useMemo(() => list.data ?? [], [list.data]);
  // Offline with nothing saved: the request waits ("paused") — say so
  // rather than showing skeletons forever.
  const offlineEmpty = !list.data && list.fetchStatus === "paused";
  const loading = !list.data && !list.isError && !offlineEmpty;
  const error = list.data ? "" : offlineEmpty
    ? "You're offline. Your briefs will appear when you're back online."
    : list.isError
      ? (list.error instanceof ApiError ? list.error.message : "Could not load your briefs.")
      : "";
  // Showing the saved list because the server couldn't be reached.
  const savedNote = list.data && (list.isError || list.fetchStatus === "paused")
    ? `Offline · ${savedAtLabel(list.dataUpdatedAt)}` : "";
  const [refreshing, setRefreshing] = useState(false);
  const [members, setMembers] = useState<ApiMember[]>([]);
  // Who the caller IS — for the header avatar, and so their own meetings are
  // not labelled with their own name on every row. Shared, cached (/me once).
  const me = useMe().data ?? null;
  const myUserId = me?.user_id ?? "";
  const [openTasks, setOpenTasks] = useState<number | null>(null);
  // Meetings shared with this user by other recorders. Loaded in its own
  // request: a failure leaves the section off and never blanks the Desk.
  const [shared, setShared] = useState<SharedRecordingSummary[]>([]);

  // The people filter lives in a sheet rather than a permanent chip row.
  const [whoseOpen, setWhoseOpen] = useState(false);

  // RESPONSIVE FILTER PLACEMENT — measured, not a breakpoint.
  //
  // The status tabs and the Filter control share one row only when the tabs
  // still get enough width for their LONGEST label in full. At 360pt that is
  // not the case (each segment would get ~68pt for "In the works", which needs
  // ~80pt), so there the Filter control moves up beside Search and the tabs
  // take the full width. Measuring the real rendered widths (rather than
  // assuming a font size) keeps this right under the OS text-size setting too.
  // Until everything is measured, the safe placement (beside Search) is used,
  // so the first frame can never truncate a tab.
  const [rowW, setRowW] = useState(0);
  const [labelW, setLabelW] = useState<Record<string, number>>({});
  const [filterW, setFilterW] = useState(0);

  // Ask the server again. Joins a request already in flight instead of
  // starting a second one (the screen's first fetch and its focus refresh
  // land together).
  const load = useCallback(async () => {
    try {
      if (listKey) await queryClient.refetchQueries({ queryKey: listKey, exact: true }, { cancelRefetch: false });
    } finally {
      setRefreshing(false);
    }
  }, [queryClient, listKey]);

  // ON FOCUS, only a STALE list is re-read. Every change made in the app now
  // updates the saved list itself once the server confirms it (rename, Trash,
  // restore, reprocess — lib/shared-data.ts), and a finished brief marks it
  // stale; so a list read in the last 30 s is what the server would say.
  // Pull-to-refresh, the processing poll and "Try again" still always ask.
  const refreshIfStale = useCallback(() => {
    if (listKey) void queryClient.refetchQueries({ queryKey: listKey, exact: true, stale: true }, { cancelRefetch: false });
  }, [queryClient, listKey]);
  useFocusEffect(useCallback(() => { refreshIfStale(); }, [refreshIfStale]));

  const loadShared = useCallback(async () => {
    try {
      setShared(await getSharedWithMe());
    } catch {
      // Silent by design: the Desk's own list is unaffected, and a missing
      // "Shared with you" section is the honest state when it can't load.
      setShared([]);
    }
  }, []);

  useFocusEffect(useCallback(() => { loadShared(); }, [loadShared]));

  // The people to offer in the "whose meetings" filter. Skipped entirely
  // unless the caller may actually see everyone's meetings; `whose` is cleared
  // on the way out so a demotion (or a workspace switch) cannot leave a stale
  // filter pinned to someone the caller can no longer query.
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
  // the Desk's job is to show briefs, and a task-service hiccup must not blank
  // the page or surface an error over it. A failure just leaves the count off.
  useFocusEffect(useCallback(() => {
    let alive = true;
    getAllTasks({ status: "Open", limit: 200 })
      .then((r) => { if (alive) setOpenTasks(r.tasks.length); })
      .catch(() => { if (alive) setOpenTasks(null); });
    return () => { alive = false; };
  }, []));

  // Long-press a brief to move it to Trash. Nothing here destroys anything:
  // the brief goes to Trash, where it can be restored or permanently deleted.
  const [deletingKey, setDeletingKey] = useState<string | null>(null);

  const confirmDelete = useCallback((item: RecordingSummary) => {
    const failed = statusMeta(item.status).kind === "failed";
    Alert.alert(
      failed ? "Move this failed brief to Trash?" : "Move this brief to Trash?",
      failed
        ? "Nothing was written up for this one. You can restore it later from Trash."
        : `“${item.title || "Untitled meeting"}” moves to Trash with its `
        + "transcript and everything written from it. You can restore it later.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Move to Trash",
          style: "destructive",
          onPress: async () => {
            const key = item.audio_s3_key;
            setDeletingKey(key);
            // NOT optimistic: the row stays (with its spinner) until the
            // server confirms, then leaves every saved list. Removing it
            // first left it missing from the saved list whenever the request
            // failed offline — the cache must only ever say what AWS said.
            try {
              await trashRecording(key);
              dropMeetingFromLists(key);
            } catch (e) {
              Alert.alert(
                "Couldn't move to Trash",
                isStillUploading(e)
                  ? "This recording is still being processed. Try again in a minute."
                  : e instanceof ApiError
                    ? e.message
                    : "Something went wrong. Please try again."
              );
            } finally {
              setDeletingKey(null);
            }
          },
        },
      ]
    );
  }, []);

  // In-flight phone/upload jobs from the shared UploadManager. Refresh the
  // list whenever a job lands so its row flips from "Uploading" on its own.
  const uploads = useUploads();
  const seenDone = useRef(new Set<string>());
  useEffect(() => {
    for (const j of uploads) {
      if (j.phase === "done" && !seenDone.current.has(j.id)) {
        seenDone.current.add(j.id);
        load();
        // A recording just uploaded and its brief is minutes away — the one
        // moment a "notify me" offer makes sense. Shown once ever (push.ts).
        void offerBriefReadyPrompt().catch(() => { /* never block the Desk */ });
      }
    }
  }, [uploads, load]);

  // The backend advances status (uploaded -> transcribing -> generating_ai
  // -> complete) without any push channel — poll gently while this screen is
  // focused AND something is actually still processing.
  const hasProcessing = items.some((r) => statusMeta(r.status).kind === "processing");
  useFocusEffect(useCallback(() => {
    if (!hasProcessing) return;
    // Only while the app is in the foreground: a phone left in a pocket on
    // this screen must not keep asking the server.
    const t = setInterval(() => { if (AppState.currentState === "active") load(); }, 20_000);
    return () => clearInterval(t);
  }, [hasProcessing, load]));

  const { sections, sharedKeys } = useMemo(() => {
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
    // SHARED WITH YOU — meetings another recorder gave this user READ-ONLY
    // access to. They sit in the same day groups as the user's own briefs
    // (tagged "Shared" on the card, not pinned in a section of their own), with
    // the same search and status filter, deduplicated against the main list (an
    // org manager may already see the meeting there), and hidden while
    // filtering by person, which is a filter over this workspace's recorders
    // only.
    let sharedRows: SharedRecordingSummary[] = [];
    if (!whose) {
      const own = new Set(items.map((r) => r.audio_s3_key));
      sharedRows = shared.filter((r) => {
        if (own.has(r.audio_s3_key)) return false;
        const kind = statusMeta(r.status).kind;
        if (filter === "ready" && kind !== "ready") return false;
        if (filter === "processing" && kind === "ready") return false;
        if (!q) return true;
        return (
          (r.title || "").toLowerCase().includes(q) ||
          (r.summary || "").toLowerCase().includes(q) ||
          (r.recorded_by_name || "").toLowerCase().includes(q)
        );
      });
    }
    return {
      sections: groupByDate([...filtered, ...sharedRows]),
      sharedKeys: new Set(sharedRows.map((r) => r.audio_s3_key)),
    };
  }, [items, shared, query, filter, whose]);

  const connected = connState === "connected";
  const deviceRecording = connected && status?.isRecording === true;
  const deviceUploading = connected && !deviceRecording && (status?.pendingUploads ?? 0) > 0;
  // A phone recording keeps running when the user leaves the recorder; the
  // Desk is where they land, so it has to say so and offer the way back.
  const phoneLive = phone.state === "RECORDING" || isPaused(phone.state);
  const filtering = !!query || filter !== "all" || !!whose;
  // The per-person filter exists only where the server would honour it: an
  // organisation, a caller with view_all_meetings, and more than one person to
  // choose between. Anywhere else there is no Filter control at all.
  const canFilterWhose = isOrganisation && canSeeEveryones && members.length > 1;
  const whoseLabel = (userId: string) => {
    if (!userId) return "Everyone";
    if (userId === myUserId) return "You";
    const m = members.find((x) => x.user_id === userId);
    // Already a display name server-side (chosen -> derived -> email).
    return m?.name || m?.email || "Filter";
  };
  const taskCount = tasksBadge(openTasks);

  const longestLabel = Math.max(0, ...STATUS_OPTIONS.map((o) => labelW[o.key] ?? 0));
  const measured = rowW > 0 && filterW > 0
    && STATUS_OPTIONS.every((o) => (labelW[o.key] ?? 0) > 0);
  const tabsNeed = SEG_TRACK_PAD
    + STATUS_OPTIONS.length * (longestLabel + SEG_LABEL_PAD + 2 * SEG_BREATHING);
  const filterInline = canFilterWhose && measured && tabsNeed + S.sm + filterW <= rowW;

  const filterButton = canFilterWhose ? (
    <Pressable
      onPress={() => setWhoseOpen(true)}
      onLayout={(e) => setFilterW(Math.ceil(e.nativeEvent.layout.width))}
      accessibilityRole="button"
      accessibilityLabel={`Filter by person. Showing ${whoseLabel(whose)}`}
      style={({ pressed }) => [
        st.filterBtn,
        whose
          ? { backgroundColor: C.primarySoft, borderColor: C.primary }
          : { backgroundColor: C.surface, borderColor: C.border },
        pressed && { opacity: 0.75 },
      ]}
    >
      <Icon name="slider.horizontal.3" tintColor={whose ? C.primary : C.textDim} size={15} />
      <Text style={[st.filterTxt, { color: whose ? C.primary : C.text }]} numberOfLines={1}>
        {whose ? whoseLabel(whose) : "Filter"}
      </Text>
    </Pressable>
  ) : null;

  const header = (
    <View style={st.gutter}>
      {savedNote ? (
        <Text style={{ fontFamily: FONT.medium, fontSize: 12, color: C.textFaint, marginBottom: S.sm }}
          accessibilityRole="text">{savedNote}</Text>
      ) : null}
      {/* A recording in progress outranks everything below it. */}
      {phoneLive ? (
        <Pressable
          style={({ pressed }) => [st.liveBar, pressed && { opacity: 0.85 }]}
          onPress={() => router.navigate("/record-phone")}
          accessibilityRole="button"
          accessibilityLabel={phone.state === "RECORDING" ? "Recording on this phone. Open recorder" : "Recording paused. Open recorder"}
        >
          <PulseDot active={phone.state === "RECORDING"} color={phone.state === "RECORDING" ? C.recording : C.warn} size={9} />
          <View style={{ flex: 1, minWidth: 0 }}>
            <Text style={st.liveTitle}>{phone.state === "RECORDING" ? "Recording on this phone" : "Recording paused"}</Text>
            <Text style={st.liveSub} numberOfLines={1}>Tap to return to the recorder</Text>
          </View>
          <Icon name="chevron.right" tintColor={C.textFaint} size={16} />
        </Pressable>
      ) : null}
      {deviceRecording ? (
        <Pressable
          style={({ pressed }) => [st.liveBar, pressed && { opacity: 0.85 }]}
          onPress={() => router.navigate("/record")}
          accessibilityRole="button"
          accessibilityLabel="Your device is recording. Open"
        >
          <PulseDot active color={C.recording} size={9} />
          <View style={{ flex: 1, minWidth: 0 }}>
            <Text style={st.liveTitle}>Your device is recording</Text>
            <Text style={st.liveSub} numberOfLines={1}>Tap to view or stop</Text>
          </View>
          <Icon name="chevron.right" tintColor={C.textFaint} size={16} />
        </Pressable>
      ) : null}

      {/* The device pushing recordings up over Wi-Fi. It reports a count, not
          bytes, so the bar is the indeterminate sweep. */}
      {deviceUploading ? (
        <View style={st.notice}>
          <Text style={st.noticeTitle}>
            Transferring {status!.pendingUploads} recording{(status!.pendingUploads ?? 0) > 1 ? "s" : ""} from your device
          </Text>
          <Text style={st.noticeSub}>They&apos;ll appear here once written up.</Text>
          <SweepBar color={C.primary} track={C.border} />
        </View>
      ) : null}

      {/* Phone/upload jobs from the shared UploadManager. */}
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
            <View style={{ flex: 1, minWidth: 0 }}>
              <Text style={st.noticeTitle} numberOfLines={1}>
                {j.phase === "failed" ? "Upload failed"
                  : j.phase === "done" ? "Uploaded"
                    : j.phase === "waiting" ? "Waiting to upload"
                      : `Uploading “${j.title}”`}
              </Text>
              <Text style={st.noticeSub} numberOfLines={2}>
                {j.phase === "failed" ? (j.error || "Something went wrong.")
                  : j.phase === "done" ? "Transcribing and writing the brief now"
                    // Say plainly that the audio is safe. This banner is what a
                    // user sees after recording a meeting offline, and the one
                    // thing they need to know is that they haven't lost it.
                    : j.phase === "waiting" ? "Saved on this phone — it'll upload when you're back online"
                      : j.phase === "finalizing" ? "Finishing up…"
                        // Real byte counts while the transfer is live.
                        : j.progress != null && j.totalBytes
                          ? `${fmtBytes(j.bytesSent ?? 0)} of ${fmtBytes(j.totalBytes)}`
                          : j.source === "MOBILE" ? "Phone recording" : "Audio file"}
              </Text>
            </View>
            {/* Retry before dismiss: a failed upload is usually a dropped
                connection, and the file is still on disk. */}
            {j.phase === "failed" || j.phase === "waiting" ? (
              <Pressable
                onPress={() => retryUpload(j.id)}
                accessibilityRole="button"
                accessibilityLabel={j.phase === "waiting" ? "Upload now" : "Retry upload"}
                hitSlop={10}
                style={({ pressed }) => [{ minHeight: 44, justifyContent: "center", paddingHorizontal: S.xs }, pressed && { opacity: 0.6 }]}
              >
                <Text style={{ fontFamily: FONT.semibold, fontSize: 13.5, color: C.primary }}>
                  {j.phase === "waiting" ? "Upload now" : "Retry"}
                </Text>
              </Pressable>
            ) : null}
            {/* Dismiss only hides the banner — it never deletes audio. */}
            {j.phase === "failed" || j.phase === "done" ? (
              <IconButton icon="xmark" iconSize={16} size={36} onPress={() => dismissUpload(j.id)} accessibilityLabel="Dismiss" />
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
              that has genuinely failed. Behind a confirm: it is unrecoverable. */}
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
              accessibilityRole="button"
              accessibilityLabel="Discard recording"
              style={({ pressed }) => [{ marginTop: S.sm, minHeight: 32, justifyContent: "center" }, pressed && { opacity: 0.6 }]}
            >
              <Text style={{ fontFamily: FONT.semibold, fontSize: 12.5, color: C.textDim }}>Discard recording</Text>
            </Pressable>
          ) : null}
        </View>
      ))}

    </View>
  );

  return (
    <View style={[st.container, { paddingTop: insets.top }]}>
      {/* THE PINNED BAR — a sibling ABOVE the list, not inside it, so it never
          scrolls away (a sticky section header would be displaced by the next
          date header). */}
      <View style={st.pinned}>
        <AppHeader
          title="MinuteX"
          safeTop={false}
          style={{ paddingHorizontal: 0, paddingTop: S.xs, paddingBottom: 0 }}
          right={
            <>
              {/* Device status, only when there is a device to report on. It
                  is a status you glance at, so it states the link and then
                  offers the trip to /devices; with no device it says nothing
                  (pairing lives in You › Your device and in the recorder's
                  source sheet). */}
              {device ? (
                <IconButton
                  icon="cpu.fill"
                  iconSize={19}
                  onPress={() => router.push("/devices")}
                  tint={deviceRecording ? C.recording : connected ? C.success : C.textFaint}
                  accessibilityLabel={
                    deviceRecording ? "Device recording. Open device"
                      : connected ? `Device connected${status?.batteryLevel != null ? `, ${status.batteryLevel}% battery` : ""}. Open device`
                        : "Device not connected. Open device"
                  }
                />
              ) : null}
              {/* Tasks is the one workspace destination reachable only from
                  here, so it keeps a place in the header — as a badged icon
                  with the REAL open count, not a card. Calendar is one tap
                  inside Tasks; People lives under You › Contacts. */}
              <IconButton
                icon="checklist"
                iconSize={20}
                onPress={() => router.push("/tasks")}
                badge={taskCount}
                accessibilityLabel={taskCount ? `Tasks, ${taskCount} open` : "Tasks"}
              />
              {/* Real unread count from the notification centre — see
                  lib/notification-bell.tsx. No badge when there is none. */}
              <NotificationBell />
              <Pressable
                onPress={() => router.navigate("/profile")}
                accessibilityRole="button"
                accessibilityLabel="You — profile and settings"
                hitSlop={6}
                style={({ pressed }) => [{ width: 44, height: 44, alignItems: "center", justifyContent: "center" }, pressed && { opacity: 0.7 }]}
              >
                <Avatar name={me?.name || me?.email || ""} photoUri={me?.avatar_view_url} size={32} />
              </Pressable>
            </>
          }
        />
        <View style={st.searchRow}>
          <SearchBar
            value={query}
            onChangeText={setQuery}
            placeholder="Search briefs"
            style={{ flex: 1 }}
          />
          {filterInline ? null : filterButton}
        </View>
        <View
          style={st.filterRow}
          onLayout={(e) => setRowW(Math.floor(e.nativeEvent.layout.width))}
        >
          <SegmentedControl<Filter>
            style={{ flex: 1 }}
            value={filter}
            onChange={setFilter}
            options={STATUS_OPTIONS}
          />
          {filterInline ? filterButton : null}
          {/* The measuring copies. Same font as the ACTIVE label (semibold,
              the wider of the two weights the control uses). */}
          <View
            style={st.measure}
            pointerEvents="none"
            accessibilityElementsHidden
            importantForAccessibility="no-hide-descendants"
          >
            {STATUS_OPTIONS.map((o) => (
              <Text
                key={o.key}
                numberOfLines={1}
                style={{ fontFamily: FONT.semibold, fontSize: 13.5, alignSelf: "flex-start" }}
                onLayout={(e) => {
                  const w = Math.ceil(e.nativeEvent.layout.width);
                  setLabelW((prev) => (prev[o.key] === w ? prev : { ...prev, [o.key]: w }));
                }}
              >
                {o.label}
              </Text>
            ))}
          </View>
        </View>
      </View>

      <SectionList
        sections={sections}
        keyExtractor={(i) => i.audio_s3_key}
        showsVerticalScrollIndicator={false}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="on-drag"
        stickySectionHeadersEnabled
        ListHeaderComponent={header}
        // Clearance derived from the real tab bar: (tabs)/_layout sizes it
        // 60 + max(inset, 8), and the extra 40 clears the raised record
        // button, which overhangs the bar.
        contentContainerStyle={{ paddingBottom: 60 + Math.max(insets.bottom, 8) + 40 }}
        refreshControl={
          <RefreshControl refreshing={refreshing}
            onRefresh={() => { setRefreshing(true); load(); loadShared(); }} tintColor={C.primary} />
        }
        ListEmptyComponent={
          loading ? (
            <View style={[st.listState, { marginTop: S.lg }]}>
              <SkeletonCard />
              <SkeletonCard />
              <SkeletonCard />
            </View>
          ) : error ? (
            <View style={[st.listState, { marginTop: S.xl }]}>
              <ErrorText>{error}</ErrorText>
              <Button label="Try again" variant="secondary" onPress={() => { void load(); }}
                style={{ marginTop: S.md, alignSelf: "flex-start", paddingHorizontal: S.xl }} />
            </View>
          ) : (
            <View style={st.listState}>
              <EmptyState
                title={filtering ? "Nothing matches that" : "No briefs yet"}
                subtitle={filtering
                  ? "Try a different search or filter."
                  : "Record your next conversation and the first brief lands here a few minutes after it uploads."}
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
              <SectionHeader
                title={s.title}
                detail={`${s.data.length} brief${s.data.length === 1 ? "" : "s"}`}
              />
            </View>
          );
        }}
        renderItem={({ item, index, section }) => {
          if (sharedKeys.has(item.audio_s3_key)) {
            // Someone else's meeting, shared with this user. The "Shared" tag
            // is what sets it apart from their own briefs in the same day group;
            // the access level rides in the meta line.
            const row = item as SharedRecordingSummary;
            return (
              <View style={st.itemWrap}>
                <BriefCard
                  title={row.title || "Untitled meeting"}
                  meta={[
                    fmtTime(row.created_at), fmtDuration(row.duration),
                    row.access_level === "full" ? "Full access" : "Notes only",
                  ]}
                  preview={statusMeta(row.status).kind === "ready"
                    ? row.summary || undefined : undefined}
                  status={{ label: "Shared", tone: "info" }}
                  byline={row.recorded_by_name ? (
                    <View style={st.recordedBy}>
                      <Avatar name={row.recorded_by_name} photoUri={row.recorded_by_avatar} size={16} fontSize={8} />
                      <Text style={st.recordedByText} numberOfLines={1}>{row.recorded_by_name}</Text>
                    </View>
                  ) : undefined}
                  position={positionIn(index, (section as Section).data.length)}
                  // READ-ONLY view. The owner screen would 404 for a grantee,
                  // and there is deliberately no Trash action here.
                  onPress={() => router.push({ pathname: "/meeting/[key]/shared", params: { key: row.audio_s3_key } })}
                  accessibilityHint="Opens a read-only view shared with you"
                />
              </View>
            );
          }
          const meta = statusMeta(item.status);
          const failed = meta.kind === "failed";
          const processing = meta.kind === "processing";
          const deleting = deletingKey === item.audio_s3_key;
          const duration = fmtDuration(item.duration);
          const when = fmtTime(item.created_at);
          const byline = item.recorded_by_name && item.recorded_by !== myUserId ? (
            // WHO recorded it. Organisation only, and only for someone ELSE's
            // meeting: the server omits these fields in a personal workspace.
            <View style={st.recordedBy}>
              <Avatar name={item.recorded_by_name} photoUri={item.recorded_by_avatar} size={16} fontSize={8} />
              <Text style={st.recordedByText} numberOfLines={1}>{item.recorded_by_name}</Text>
            </View>
          ) : undefined;
          return (
            <View style={st.itemWrap}>
              <BriefCard
                title={item.title || "Untitled meeting"}
                meta={[when, duration, sourceMeta(item).label]}
                // A ready brief previews its summary; a failed one says why in
                // one line; one in the works shows nothing it doesn't have.
                preview={failed ? "We stopped rather than guess. Open it to see why." : processing ? undefined : item.summary || undefined}
                status={
                  failed ? { label: stageLabel(item.status), tone: "danger" }
                    : processing ? { label: stageLabel(item.status), tone: "info" }
                      : undefined
                }
                byline={byline}
                position={positionIn(index, (section as Section).data.length)}
                dimmed={deleting}
                onPress={() => router.push({ pathname: "/recording/[key]", params: { key: item.audio_s3_key } })}
                onLongPress={() => confirmDelete(item)}
                accessibilityHint="Long press to move this brief to Trash"
                footer={failed ? (
                  // Visible on failed rows only: long-press covers every brief,
                  // but a desk full of failures is exactly when the way out
                  // needs to be obvious.
                  <Pressable
                    onPress={() => confirmDelete(item)}
                    disabled={deleting}
                    accessibilityRole="button"
                    accessibilityLabel="Move this brief to Trash"
                    style={({ pressed }) => [{ flexDirection: "row", alignItems: "center", gap: 6, minHeight: 32 }, pressed && { opacity: 0.6 }]}
                  >
                    <Icon name="trash" tintColor={C.danger} size={14} />
                    <Text style={{ fontFamily: FONT.semibold, fontSize: 13, color: C.danger }}>
                      {deleting ? "Moving…" : "Move to Trash"}
                    </Text>
                  </Pressable>
                ) : undefined}
              />
            </View>
          );
        }}
      />

      {/* WHOSE meetings — the same filter the chip row used to offer, in a
          sheet. Scrolls, because an organisation can have more members than
          fit on screen. Choosing reloads through the same getRecordings
          ?user_id= path as before. */}
      <BottomSheet
        visible={whoseOpen}
        onClose={() => setWhoseOpen(false)}
        title="Show meetings from"
      >
        <ScrollView style={{ maxHeight: 380 }} keyboardShouldPersistTaps="handled">
          <SheetAction
            label="Everyone"
            selected={whose === ""}
            onPress={() => { setWhose(""); setWhoseOpen(false); }}
          />
          {members.map((m) => (
            <SheetAction
              key={m.user_id}
              label={whoseLabel(m.user_id)}
              selected={whose === m.user_id}
              onPress={() => { setWhose(m.user_id); setWhoseOpen(false); }}
            />
          ))}
        </ScrollView>
      </BottomSheet>
    </View>
  );
}
