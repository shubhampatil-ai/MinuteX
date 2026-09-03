// src/app/assistant.tsx — the workspace Task AI. "Ask MinuteX about my work."
//
// The counterpart to recording/[key]/assistant.tsx, and deliberately built to
// the same pattern: the same bubbles, the same thinking dots, the same source
// rows, the same composer keyboard handling. Two AI surfaces that felt
// different would read as two products.
//
// WHAT IS DIFFERENT, and why:
//
//   * SCOPE. That screen answers about ONE meeting's transcript. This one
//     answers about the user's whole task list, and the backend answers it by
//     calling task tools (POST /ai/chat) rather than by being handed a
//     transcript. So there is no meeting in context here, which is why a
//     source row has to name its own meeting — see AssistantSource.
//
//   * SESSIONS, NOT A LOCAL THREAD. /ai/chat now persists the conversation
//     and returns a `session_id`; this screen holds turns for RENDERING but
//     the backend is the source of truth. So leaving the screen no longer
//     ends the conversation, and reopening resumes it. What is kept on the
//     device is one id (which conversation was last open) — never the
//     messages.
//
//     Two consequences worth knowing before editing this file:
//       - `history` is sent ONLY when there is no session yet. Once one
//         exists the server's copy wins; replaying the client's would be
//         redundant at best and stale at worst.
//       - a REOPENED conversation has no sources or proposals on its turns.
//         Neither is persisted server-side (a stale proposal would be an
//         offer the user may no longer be allowed to accept), which is why
//         those fields on Turn are optional rather than always present.
//
//   * PROPOSALS. This assistant can draft a task change. It cannot make one.
//     A proposal arrives already validated and authorized by the backend, and
//     is rendered as a card with an explicit Confirm — the mutation only
//     happens when the user taps it, through the same PATCH /tasks/{id} the
//     rest of the app uses. See ProposalCard.
//
//   * TASK SCOPE. Opened with a `taskId` param (from Task Details), the
//     screen asks about that ONE task: the suggested prompts become the
//     per-task set and the first message carries the task id so the agent can
//     call get_task / get_meeting_context on it. The backend still authorizes
//     that id on every tool call — passing it here is addressing, not access.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, Alert, Animated, Keyboard, Platform, Pressable,
  ScrollView, StyleSheet, Text, TextInput, View,
} from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ELEV, FONT, R, S, useTheme, type ColorScale } from "../../lib/theme";
import { Icon, type IconName } from "../../lib/icons";
import { RawGradient } from "../../lib/ui";
import { Markdown } from "../../lib/document-renderer";
import {
  ApiError, isRetryable, sendAIMessage, getAISuggestions, applyTaskProposal,
  listChatSessions, getChatSession, deleteChatSession,
  type AssistantSource, type ChatSessionSummary, type ChatTurn,
  type TaskProposal,
} from "../../lib/api";
// Remembers WHICH conversation was last open, and nothing else. The
// conversation itself lives on the backend (spec section 20: AsyncStorage /
// SecureStore may only be a UI cache, never the source of truth) — this
// stores a 16-char id so reopening the screen lands where the user left off.
import { store } from "../../lib/storage";

// Shown until the backend's catalogue arrives (and if it fails). Every one is
// answerable by the task tools — a suggestion the assistant cannot act on
// would be the app itself setting the user up to fail.
const FALLBACK_PROMPTS = [
  "What needs my attention?",
  "What's overdue?",
  "What should I work on this week?",
  "What did I commit to in my last meeting?",
];

const PROMPT_ICONS: IconName[] = [
  "bell.badge", "exclamationmark.triangle.fill", "calendar", "waveform",
];

// Which conversation was last open. A UI convenience only — see the storage
// import above. Losing it costs the user one tap in History, never a message.
const LAST_SESSION_KEY = "minutex.ai.lastSession";

// How many conversations the History sheet lists. The backend caps this too;
// asking for a bounded page is the client half of spec section 13.
const SESSION_LIST_LIMIT = 20;

// A turn plus the two things a workspace answer can carry that a plain
// ChatTurn cannot. Both are per-ANSWER, so they live on the turn rather than
// in screen state: scrolling back must show the evidence for the answer it
// sits under, not for the most recent one.
type Turn = ChatTurn & {
  sources?: AssistantSource[];
  proposals?: TaskProposal[];
};

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    scrollBody: { paddingHorizontal: 20, paddingTop: S.md, paddingBottom: 24 },
    intro: { fontFamily: FONT.bold, fontSize: 19, lineHeight: 26, color: C.text, marginBottom: 6 },
    introSub: { fontFamily: FONT.regular, fontSize: 13, lineHeight: 19, color: C.textDim, marginBottom: S.lg },
    label: {
      fontFamily: FONT.semibold, fontSize: 10.5, letterSpacing: 0.3,
      color: C.textFaint, textTransform: "uppercase", marginBottom: 9,
    },
    suggestGrid: { flexDirection: "row", flexWrap: "wrap", gap: S.sm },
    suggestCard: {
      flexBasis: "48%", flexGrow: 1, backgroundColor: C.surface,
      borderWidth: 1, borderColor: C.border, borderRadius: R.card,
      padding: 13, gap: 8, shadowColor: C.shadow, ...ELEV.sm,
    },
    suggestIcon: {
      width: 30, height: 30, borderRadius: 10, backgroundColor: C.primarySoft,
      alignItems: "center", justifyContent: "center",
    },
    suggestTxt: { fontFamily: FONT.semibold, fontSize: 12.5, lineHeight: 17, color: C.text },
    bubbleUser: {
      alignSelf: "flex-end", maxWidth: "86%", backgroundColor: C.primarySoft,
      borderRadius: R.card, borderBottomRightRadius: 4,
      paddingHorizontal: 14, paddingVertical: 11, marginTop: 14,
    },
    bubbleUserTxt: { fontFamily: FONT.medium, fontSize: 14.5, lineHeight: 20, color: C.primaryStrong },
    bubbleAiRow: { flexDirection: "row", gap: 10, marginTop: 14, alignItems: "flex-start" },
    aiAvatar: {
      width: 26, height: 26, borderRadius: 13, alignItems: "center",
      justifyContent: "center", marginTop: 2,
    },
    bubbleAi: {
      flex: 1, backgroundColor: C.surface, borderWidth: 1, borderColor: C.border,
      borderRadius: R.card, borderTopLeftRadius: 4, padding: 13,
    },
    sourcesWrap: {
      marginTop: 11, paddingTop: 9,
      borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.border,
    },
    sourceRow: { flexDirection: "row", alignItems: "center", gap: 6, paddingVertical: 4 },
    sourceTxt: { fontFamily: FONT.medium, fontSize: 12, color: C.primary, flexShrink: 1 },
    // The proposal card. Bordered in warn rather than primary so it reads as
    // "waiting on you" rather than as another answer.
    propCard: {
      marginTop: 11, backgroundColor: C.warnSoft, borderRadius: R.md,
      borderWidth: 1, borderColor: C.warn, padding: 12,
    },
    propTitle: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.text },
    propChange: { flexDirection: "row", alignItems: "center", gap: 7, marginTop: 8, flexWrap: "wrap" },
    propFrom: { fontFamily: FONT.medium, fontSize: 12, color: C.textDim, textDecorationLine: "line-through" },
    propTo: { fontFamily: FONT.bold, fontSize: 12, color: C.text },
    propField: { fontFamily: FONT.regular, fontSize: 11, color: C.textFaint },
    propReason: { fontFamily: FONT.regular, fontSize: 12, lineHeight: 17, color: C.textDim, marginTop: 8 },
    propBtnRow: { flexDirection: "row", gap: S.sm, marginTop: 11 },
    propConfirm: {
      flex: 1, backgroundColor: C.primary, borderRadius: R.sm,
      paddingVertical: 10, alignItems: "center",
    },
    propConfirmTxt: { fontFamily: FONT.bold, fontSize: 12.5, color: "#FFFFFF" },
    propDismiss: {
      flex: 1, backgroundColor: C.surface, borderWidth: 1, borderColor: C.border,
      borderRadius: R.sm, paddingVertical: 10, alignItems: "center",
    },
    propDismissTxt: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.textDim },
    propDone: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 10 },
    propDoneTxt: { fontFamily: FONT.semibold, fontSize: 12, color: C.success },
    working: { flexDirection: "row", alignItems: "center", gap: S.sm, paddingVertical: 14, marginLeft: 36 },
    dotRow: { flexDirection: "row", alignItems: "center", gap: 4 },
    dot: { width: 6, height: 6, borderRadius: 3, backgroundColor: C.primary },
    workingTxt: { fontFamily: FONT.regular, fontSize: 13, color: C.textDim },
    headerActions: { flexDirection: "row", alignItems: "center", gap: S.lg },
    // The conversations overlay. Anchored to the top so it reads as pulled
    // down from the header action that opened it.
    sheetWrap: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, zIndex: 20 },
    sheetScrim: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, backgroundColor: "rgba(0,0,0,0.28)" },
    sheet: {
      backgroundColor: C.surface, borderBottomLeftRadius: R.lg,
      borderBottomRightRadius: R.lg, paddingHorizontal: 18,
      paddingTop: S.md, paddingBottom: S.lg,
      borderBottomWidth: 1, borderBottomColor: C.border,
      shadowColor: C.shadow, ...ELEV.lg,
    },
    sheetHead: {
      flexDirection: "row", alignItems: "center",
      justifyContent: "space-between", marginBottom: S.sm,
    },
    sheetTitle: { fontFamily: FONT.bold, fontSize: 15, color: C.text },
    sheetNew: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary },
    sheetEmpty: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18,
      color: C.textDim, paddingVertical: S.sm,
    },
    sessionRow: {
      flexDirection: "row", alignItems: "center", gap: S.md,
      paddingVertical: 11,
      borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.border,
    },
    sessionTitle: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.text },
    sessionMeta: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint, marginTop: 2,
    },
    unsaved: {
      flexDirection: "row", alignItems: "center", gap: 6,
      backgroundColor: C.warnSoft, borderRadius: R.sm,
      paddingHorizontal: 10, paddingVertical: 7, marginTop: 12,
    },
    unsavedTxt: {
      flex: 1, fontFamily: FONT.medium, fontSize: 11.5, lineHeight: 16,
      color: C.text,
    },
    errBox: { backgroundColor: C.dangerSoft, borderRadius: R.md, padding: S.md, marginTop: 12 },
    errTxt: { fontFamily: FONT.medium, fontSize: 13, lineHeight: 19, color: C.danger },
    retry: { fontFamily: FONT.bold, fontSize: 13, color: C.primary, marginTop: 8 },
    composerWrap: {
      borderTopWidth: 1, borderTopColor: C.border, backgroundColor: C.surface,
      paddingHorizontal: 14, paddingTop: 10,
    },
    composerRow: { flexDirection: "row", alignItems: "flex-end", gap: S.sm },
    input: {
      flex: 1, fontFamily: FONT.regular, fontSize: 15, color: C.text,
      maxHeight: 110, backgroundColor: C.surface2, borderRadius: R.pill,
      paddingVertical: 11, paddingHorizontal: 16,
    },
    send: {
      width: 40, height: 40, borderRadius: 20, backgroundColor: C.primary,
      alignItems: "center", justifyContent: "center",
    },
  });
}

/** A source timestamp in the same M:SS / H:MM:SS form the transcript and the
 *  player use — a different format here would read as a different number. */
function clock(seconds: number) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`;
}

const FIELD_LABELS: Record<string, string> = {
  status: "Status",
  due_date: "Deadline",
  priority: "Priority",
};

/** Three dots breathing in sequence while the assistant works.
 *
 * useNativeDriver so it keeps moving on the UI thread while JS parses a long
 * response — exactly when a JS-driven animation would stutter and undo the
 * reassurance it exists to give. Mirrors the meeting assistant's.
 */
function ThinkingDots({ st }: { st: ReturnType<typeof buildStyles> }) {
  // useMemo rather than useRef().current: the values are READ during render
  // (they drive the opacity style), and reading a ref while rendering is what
  // the refs lint rule forbids. An empty dep list makes them just as stable.
  const dots = useMemo(
    () => [new Animated.Value(0.3), new Animated.Value(0.3), new Animated.Value(0.3)],
    []
  );

  useEffect(() => {
    const loops = dots.map((d, i) =>
      Animated.loop(
        Animated.sequence([
          Animated.delay(i * 160),
          Animated.timing(d, { toValue: 1, duration: 360, useNativeDriver: true }),
          Animated.timing(d, { toValue: 0.3, duration: 360, useNativeDriver: true }),
          Animated.delay((2 - i) * 160),
        ])
      )
    );
    loops.forEach((l) => l.start());
    return () => loops.forEach((l) => l.stop());
  }, [dots]);

  return (
    <View style={st.dotRow}>
      {dots.map((d, i) => (
        <Animated.View key={i} style={[st.dot, { opacity: d }]} />
      ))}
    </View>
  );
}

/** Where an answer came from, as tappable rows under it.
 *
 * REUSES THE EXISTING NAVIGATION. Tapping a source pushes
 * /recording/[key]/transcript?evidence=seg_N — the identical route, param and
 * highlight behaviour a task's "View in transcript" and the meeting
 * assistant's sources already use. The only difference is where the key comes
 * from: `meeting_id` on the source, because this screen has no meeting in
 * context and an answer may cite more than one.
 *
 * Renders NOTHING when empty. An answer from task rows alone has no
 * transcript evidence, and that is the ordinary case here — most task
 * questions never touch a meeting.
 */
function Sources({ sources, C, st }: {
  sources?: AssistantSource[];
  C: ColorScale;
  st: ReturnType<typeof buildStyles>;
}) {
  const router = useRouter();
  const usable = (sources ?? []).filter((s) => s.segment_id && s.meeting_id);
  if (!usable.length) return null;
  return (
    <View style={st.sourcesWrap}>
      <Text style={st.label}>Sources</Text>
      {usable.map((src) => (
        <Pressable
          key={`${src.meeting_id}:${src.segment_id}`}
          onPress={() =>
            router.push({
              pathname: "/recording/[key]/transcript",
              params: { key: src.meeting_id, evidence: src.segment_id },
            } as never)
          }
          hitSlop={6}
          style={({ pressed }) => [st.sourceRow, pressed && { opacity: 0.6 }]}
          accessibilityRole="button"
          accessibilityLabel={`View this source in the transcript at ${clock(src.start_time)}`}
        >
          <Icon name="waveform" size={12} tintColor={C.primary} />
          <Text style={st.sourceTxt} numberOfLines={1}>
            Transcript — {clock(src.start_time)}
          </Text>
        </Pressable>
      ))}
    </View>
  );
}

/** A task change the AI drafted, awaiting an explicit confirmation.
 *
 * THE WHOLE POINT OF THIS COMPONENT is that the mutation happens HERE, on a
 * press, and nowhere else. The AI's turn produced a validated patch; until
 * someone taps Confirm, nothing has been written and the card says so.
 *
 * `current` -> `proposed` is rendered as a real before/after because a bare
 * "set to Completed" gives the user nothing to check the AI against. Once
 * applied, the card becomes a static confirmation rather than disappearing:
 * a control that vanishes leaves the user unsure whether it fired.
 */
function ProposalCard({ proposal, C, st, onApplied }: {
  proposal: TaskProposal;
  C: ColorScale;
  st: ReturnType<typeof buildStyles>;
  onApplied: () => void;
}) {
  const [state, setState] = useState<"pending" | "busy" | "done" | "dismissed">("pending");

  const fields = Object.keys(proposal.proposed) as (keyof TaskProposal["proposed"])[];

  const confirm = useCallback(async () => {
    setState("busy");
    try {
      await applyTaskProposal(proposal);
      setState("done");
      onApplied();
    } catch (e) {
      setState("pending");
      Alert.alert(
        "Could not apply",
        e instanceof ApiError ? e.message : "Please try again."
      );
    }
  }, [proposal, onApplied]);

  if (state === "dismissed") return null;

  return (
    <View style={st.propCard}>
      <Text style={st.propTitle} numberOfLines={2}>
        {proposal.task.title || "This task"}
      </Text>

      {fields.map((f) => (
        <View key={String(f)} style={st.propChange}>
          <Text style={st.propField}>{FIELD_LABELS[String(f)] ?? String(f)}</Text>
          {proposal.current[f] ? (
            <Text style={st.propFrom}>{proposal.current[f]}</Text>
          ) : null}
          <Icon name="arrow.right" size={11} tintColor={C.textFaint} />
          <Text style={st.propTo}>{proposal.proposed[f] || "—"}</Text>
        </View>
      ))}

      {proposal.reason ? <Text style={st.propReason}>{proposal.reason}</Text> : null}

      {state === "done" ? (
        <View style={st.propDone}>
          <Icon name="checkmark.circle.fill" size={14} tintColor={C.success} />
          <Text style={st.propDoneTxt}>Applied</Text>
        </View>
      ) : (
        <View style={st.propBtnRow}>
          <Pressable
            onPress={confirm}
            disabled={state === "busy"}
            style={({ pressed }) => [st.propConfirm, pressed && { opacity: 0.8 }]}
            accessibilityRole="button"
            accessibilityLabel="Confirm this change"
          >
            {state === "busy" ? (
              <ActivityIndicator color="#FFFFFF" size="small" />
            ) : (
              <Text style={st.propConfirmTxt}>Confirm</Text>
            )}
          </Pressable>
          <Pressable
            onPress={() => setState("dismissed")}
            disabled={state === "busy"}
            style={({ pressed }) => [st.propDismiss, pressed && { opacity: 0.7 }]}
            accessibilityRole="button"
            accessibilityLabel="Dismiss this suggestion"
          >
            <Text style={st.propDismissTxt}>Not now</Text>
          </Pressable>
        </View>
      )}
    </View>
  );
}

export default function WorkspaceAssistantScreen() {
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);
  // `taskId` scopes the conversation to one task (opened from Task Details);
  // `q` pre-sends a first question (tapped from a dashboard chip).
  const { taskId, q } = useLocalSearchParams<{ taskId?: string; q?: string }>();
  const scopedTaskId = String(taskId ?? "");

  const [turns, setTurns] = useState<Turn[]>([]);
  const [prompts, setPrompts] = useState<string[]>(FALLBACK_PROMPTS);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [kbHeight, setKbHeight] = useState(0);
  // THE CONVERSATION THIS SCREEN IS IN. "" means "a new one, not yet
  // created" — the backend mints the id on the first successful turn and
  // returns it. Never invented client-side: an id the server does not know
  // would 404 on the next turn.
  const [sessionId, setSessionId] = useState("");
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [loadingSession, setLoadingSession] = useState(false);
  // True when the last answer could not be stored. Shown, because the user
  // will not find that turn when they come back.
  const [unsaved, setUnsaved] = useState(false);
  const lastAsked = useRef("");
  const scrollRef = useRef<ScrollView>(null);
  // Guards the auto-send of `q` so a re-render cannot fire it twice.
  const autoSent = useRef(false);
  // Guards the resume-on-open effect for the same reason.
  const resumed = useRef(false);

  // Suggested prompts, from the backend so the catalogue lives in one place.
  // A failure is non-fatal: FALLBACK_PROMPTS still gives the user somewhere
  // to start, and the composer works regardless.
  useEffect(() => {
    let alive = true;
    getAISuggestions(scopedTaskId || undefined)
      .then((s) => {
        if (alive && s.length) setPrompts(s);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [scopedTaskId]);

  /** Reload the conversation list. Non-fatal: the composer works without it,
   *  so a failure leaves History empty rather than blocking the screen. */
  const refreshSessions = useCallback(async () => {
    try {
      const res = await listChatSessions(SESSION_LIST_LIMIT);
      setSessions(res.sessions);
      return res.sessions;
    } catch {
      return [];
    }
  }, []);

  /** Open one conversation, replacing what is on screen.
   *
   * The turns come from the BACKEND, which is the source of truth. Note what
   * is not restored: sources and proposals. Neither is persisted server-side
   * (a stale proposal would be an offer the user may no longer be allowed to
   * accept), so a reopened conversation shows its text without them — which
   * is why Turn's extra fields are optional.
   */
  const openSession = useCallback(async (id: string) => {
    setHistoryOpen(false);
    setLoadingSession(true);
    setError("");
    setUnsaved(false);
    try {
      const detail = await getChatSession(id);
      setSessionId(detail.session_id);
      setTurns(detail.turns.map((t) => ({ role: t.role, content: t.content, at: t.at })));
      void store.setItemAsync(LAST_SESSION_KEY, detail.session_id).catch(() => {});
      requestAnimationFrame(() => scrollRef.current?.scrollToEnd({ animated: false }));
    } catch (e) {
      // A 404 here means the conversation is gone (deleted on another
      // device, or never ours). Fall back to a NEW conversation rather than
      // stranding the screen — and forget the stale id.
      if (e instanceof ApiError && e.status === 404) {
        setSessionId("");
        setTurns([]);
        void store.deleteItemAsync(LAST_SESSION_KEY).catch(() => {});
      } else {
        setError(
          e instanceof ApiError ? e.message : "Could not load that conversation."
        );
      }
    } finally {
      setLoadingSession(false);
    }
  }, []);

  /** Start a fresh conversation. Creates nothing server-side: the backend
   *  mints a session on the first answered turn, so an abandoned "new chat"
   *  leaves no empty row behind. */
  const newSession = useCallback(() => {
    setHistoryOpen(false);
    setSessionId("");
    setTurns([]);
    setError("");
    setUnsaved(false);
    void store.deleteItemAsync(LAST_SESSION_KEY).catch(() => {});
  }, []);

  /** Delete a conversation, with a confirmation — it is destructive and
   *  irreversible (hard delete server-side). */
  const removeSession = useCallback((s: ChatSessionSummary) => {
    Alert.alert(
      "Delete conversation?",
      `"${s.title}" will be permanently deleted.`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete",
          style: "destructive",
          onPress: async () => {
            try {
              await deleteChatSession(s.session_id);
              setSessions((prev) =>
                prev.filter((x) => x.session_id !== s.session_id));
              // Deleting the conversation you are IN leaves the screen on a
              // thread that no longer exists — start a fresh one instead.
              if (s.session_id === sessionId) newSession();
            } catch (e) {
              Alert.alert(
                "Could not delete",
                e instanceof ApiError ? e.message : "Please try again."
              );
            }
          },
        },
      ]
    );
  }, [sessionId, newSession]);

  // RESUME ON OPEN. Load the list, and reopen the last conversation if there
  // is one. A task-scoped visit always starts fresh: "ask about this task" is
  // a new question, not a continuation of whatever was open before.
  useEffect(() => {
    if (resumed.current) return;
    resumed.current = true;
    (async () => {
      const list = await refreshSessions();
      if (scopedTaskId || q) return;
      let last = "";
      try {
        last = (await store.getItemAsync(LAST_SESSION_KEY)) ?? "";
      } catch {
        last = "";
      }
      // Only reopen an id the list confirms still exists — otherwise the
      // screen would flash an error for a conversation deleted elsewhere.
      if (last && list.some((s) => s.session_id === last)) {
        void openSession(last);
      }
    })();
  }, [refreshSessions, openSession, scopedTaskId, q]);

  // KEYBOARD. Measured directly rather than via KeyboardAvoidingView, for the
  // same reason the meeting assistant does it: under react-native-screens a
  // modal-presented screen reports a container-relative frame while the
  // keyboard reports window-relative coordinates, and the subtraction silently
  // collapses to zero. Applied as marginBottom on the composer.
  useEffect(() => {
    const showEvt = Platform.OS === "ios" ? "keyboardWillShow" : "keyboardDidShow";
    const hideEvt = Platform.OS === "ios" ? "keyboardWillHide" : "keyboardDidHide";
    const onShow = Keyboard.addListener(showEvt, (e) => {
      setKbHeight(e.endCoordinates?.height ?? 0);
      requestAnimationFrame(() => scrollRef.current?.scrollToEnd({ animated: true }));
    });
    const onHide = Keyboard.addListener(hideEvt, () => setKbHeight(0));
    return () => {
      onShow.remove();
      onHide.remove();
    };
  }, []);

  const ask = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || sending) return;
      lastAsked.current = message;
      setError("");
      setDraft("");
      setSending(true);

      // The thread BEFORE this turn — what gets sent as history, and what we
      // roll back to if the request fails.
      const prior = turns;
      const optimistic: Turn[] = [
        ...prior,
        { role: "user", content: message, at: new Date().toISOString() },
      ];
      setTurns(optimistic);
      requestAnimationFrame(() => scrollRef.current?.scrollToEnd({ animated: true }));

      try {
        // Scoped conversations prefix the task id ONCE, on the first turn:
        // after that it is in the history and the agent has the id from its
        // own earlier tool results. The backend authorizes the id on every
        // tool call regardless — this is addressing, not access.
        const outbound =
          scopedTaskId && !prior.length
            ? `About task ${scopedTaskId}: ${message}`
            : message;
        // `sessionId` continues the conversation server-side. History is
        // sent ONLY when there is no session yet — it is the backend's
        // fallback, and once a session exists the server's stored copy is
        // authoritative (sending it too would be bytes the backend ignores).
        const res = await sendAIMessage(
          outbound,
          sessionId || undefined,
          sessionId ? undefined : prior.map((t) => ({ role: t.role, content: t.content }))
        );
        // ADOPT THE RETURNED ID. On the first turn this is how the screen
        // learns which conversation it is in; on later turns it is the same
        // id back. Empty means the backend could not persist — see `unsaved`.
        if (res.session_id && res.session_id !== sessionId) {
          setSessionId(res.session_id);
          void store.setItemAsync(LAST_SESSION_KEY, res.session_id).catch(() => {});
          // Refresh the list so the new conversation appears in History
          // with its generated title.
          void refreshSessions();
        }
        setUnsaved(!res.persisted || !res.session_id);
        setTurns([
          ...optimistic,
          {
            role: "assistant",
            content: res.reply,
            at: new Date().toISOString(),
            sources: res.sources,
            proposals: res.proposals,
          },
        ]);
      } catch (e) {
        setTurns(prior);
        setError(
          isRetryable(e)
            ? "The assistant is busy. Try that again."
            : e instanceof ApiError
              ? e.message
              : "Something went wrong."
        );
      } finally {
        setSending(false);
        requestAnimationFrame(() => scrollRef.current?.scrollToEnd({ animated: true }));
      }
    },
    [sending, turns, scopedTaskId, sessionId, refreshSessions]
  );

  // A question handed in by the route (a dashboard chip) is asked once, on
  // mount, so the user lands on the answer rather than on a composer they
  // have to re-type into.
  useEffect(() => {
    const seed = String(q ?? "").trim();
    if (seed && !autoSent.current) {
      autoSent.current = true;
      ask(seed);
    }
    // `ask` changes identity every render (it closes over turns); depending on
    // it here would re-fire the seed. The autoSent guard is the real control.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const composerLift = kbHeight > 0 ? Math.max(kbHeight - insets.bottom, 0) : 0;

  return (
    <View style={st.container}>
      <Stack.Screen
        options={{
          title: scopedTaskId ? "Ask about this task" : "MinuteX AI",
          // Two header actions, and only for workspace conversations: a
          // task-scoped visit is a single question, so a conversation list
          // there would offer to switch away from the thing being asked
          // about. Deliberately plain icons rather than a menu — spec
          // section 10 asks for a simple entry point, not a chrome surface.
          headerRight: scopedTaskId
            ? undefined
            : () => (
                <View style={st.headerActions}>
                  <Pressable
                    onPress={() => { void refreshSessions(); setHistoryOpen(true); }}
                    hitSlop={8}
                    accessibilityRole="button"
                    accessibilityLabel="Recent conversations"
                    style={({ pressed }) => pressed && { opacity: 0.6 }}
                  >
                    <Icon name="clock" size={19} tintColor={C.text} />
                  </Pressable>
                  <Pressable
                    onPress={newSession}
                    hitSlop={8}
                    accessibilityRole="button"
                    accessibilityLabel="New conversation"
                    style={({ pressed }) => pressed && { opacity: 0.6 }}
                  >
                    <Icon name="plus" size={19} tintColor={C.text} />
                  </Pressable>
                </View>
              ),
        }}
      />

      {/* RECENT CONVERSATIONS. A plain overlay list rather than a modal
          route: it is a switcher, and routing to it would put a screen
          between the user and the thread they are reading. */}
      {historyOpen ? (
        <View style={st.sheetWrap}>
          <Pressable
            style={st.sheetScrim}
            onPress={() => setHistoryOpen(false)}
            accessibilityRole="button"
            accessibilityLabel="Close conversations"
          />
          <View style={st.sheet}>
            <View style={st.sheetHead}>
              <Text style={st.sheetTitle}>Recent conversations</Text>
              <Pressable onPress={newSession} hitSlop={8}
                         accessibilityRole="button"
                         accessibilityLabel="Start a new conversation">
                <Text style={st.sheetNew}>New</Text>
              </Pressable>
            </View>
            {sessions.length ? (
              <ScrollView style={{ maxHeight: 340 }}>
                {sessions.map((sess) => (
                  <View key={sess.session_id} style={st.sessionRow}>
                    <Pressable
                      onPress={() => void openSession(sess.session_id)}
                      style={({ pressed }) => [{ flex: 1 }, pressed && { opacity: 0.6 }]}
                      accessibilityRole="button"
                      accessibilityLabel={`Open conversation: ${sess.title}`}
                    >
                      <Text
                        style={[
                          st.sessionTitle,
                          sess.session_id === sessionId && { color: C.primary },
                        ]}
                        numberOfLines={1}
                      >
                        {sess.title || "Untitled"}
                      </Text>
                      <Text style={st.sessionMeta} numberOfLines={1}>
                        {sess.message_count} message
                        {sess.message_count === 1 ? "" : "s"}
                        {sess.last_message_preview
                          ? ` · ${sess.last_message_preview}`
                          : ""}
                      </Text>
                    </Pressable>
                    <Pressable
                      onPress={() => removeSession(sess)}
                      hitSlop={8}
                      accessibilityRole="button"
                      accessibilityLabel={`Delete conversation: ${sess.title}`}
                      style={({ pressed }) => pressed && { opacity: 0.5 }}
                    >
                      <Icon name="trash" size={15} tintColor={C.textFaint} />
                    </Pressable>
                  </View>
                ))}
              </ScrollView>
            ) : (
              <Text style={st.sheetEmpty}>
                No saved conversations yet. Ask something and it will be kept
                here.
              </Text>
            )}
          </View>
        </View>
      ) : null}

      <ScrollView
        ref={scrollRef}
        style={{ flex: 1 }}
        contentContainerStyle={st.scrollBody}
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}
      >
        {!turns.length ? (
          <>
            <Text style={st.intro}>
              {scopedTaskId ? "Ask about this task" : "What do you want to get done?"}
            </Text>
            <Text style={st.introSub}>
              {scopedTaskId
                ? "I can explain why this task exists and what was said about it in the meeting it came from."
                : "I can look through your tasks and the meetings they came from. Ask me anything about your work."}
            </Text>
            <Text style={st.label}>Suggested</Text>
            <View style={st.suggestGrid}>
              {prompts.map((p, i) => (
                <Pressable
                  key={p}
                  onPress={() => ask(p)}
                  disabled={sending}
                  style={({ pressed }) => [st.suggestCard, pressed && { opacity: 0.75 }]}
                  accessibilityRole="button"
                  accessibilityLabel={p}
                >
                  <View style={st.suggestIcon}>
                    <Icon
                      name={PROMPT_ICONS[i % PROMPT_ICONS.length]}
                      size={15}
                      tintColor={C.primary}
                    />
                  </View>
                  <Text style={st.suggestTxt}>{p}</Text>
                </Pressable>
              ))}
            </View>
          </>
        ) : null}

        {turns.map((t, i) =>
          t.role === "user" ? (
            <View key={i} style={st.bubbleUser}>
              <Text style={st.bubbleUserTxt}>{t.content}</Text>
            </View>
          ) : (
            <View key={i} style={st.bubbleAiRow}>
              <RawGradient colors={[C.primary, C.accent]} style={st.aiAvatar}>
                <Icon name="sparkles" size={13} tintColor="#FFFFFF" />
              </RawGradient>
              <View style={st.bubbleAi}>
                <Markdown text={t.content} />
                <Sources sources={t.sources} C={C} st={st} />
                {(t.proposals ?? []).map((p, j) => (
                  <ProposalCard
                    key={`${p.task.id}:${j}`}
                    proposal={p}
                    C={C}
                    st={st}
                    // A confirmed change makes any task list behind this
                    // screen stale. Rather than reaching into another
                    // screen's state, the dashboard re-reads on focus — so
                    // there is nothing to do here but let the card settle.
                    onApplied={() => {}}
                  />
                ))}
              </View>
            </View>
          )
        )}

        {sending ? (
          <View style={st.working} accessibilityRole="progressbar" accessibilityLabel="Thinking">
            <ThinkingDots st={st} />
            <Text style={st.workingTxt}>Looking through your tasks…</Text>
          </View>
        ) : null}

        {loadingSession ? (
          <View style={st.working} accessibilityRole="progressbar"
                accessibilityLabel="Loading conversation">
            <ActivityIndicator color={C.primary} size="small" />
            <Text style={st.workingTxt}>Loading conversation…</Text>
          </View>
        ) : null}

        {/* SAID PLAINLY when a turn could not be stored. The answer above is
            real and correct — the backend returned it and only the write
            failed — but it will not be here when the user comes back, and
            silently losing it would be the worse outcome. */}
        {unsaved && turns.length ? (
          <View style={st.unsaved}>
            <Icon name="exclamationmark.triangle.fill" size={12} tintColor={C.warn} />
            <Text style={st.unsavedTxt}>
              This answer could not be saved, so it won&apos;t appear in your
              conversation history.
            </Text>
          </View>
        ) : null}

        {error ? (
          <View style={st.errBox}>
            <Text style={st.errTxt}>{error}</Text>
            <Pressable onPress={() => ask(lastAsked.current)} hitSlop={8}>
              <Text style={st.retry}>Retry</Text>
            </Pressable>
          </View>
        ) : null}
      </ScrollView>

      <View
        style={[
          st.composerWrap,
          { marginBottom: composerLift, paddingBottom: Math.max(insets.bottom, 10) },
        ]}
      >
        <View style={st.composerRow}>
          <TextInput
            style={st.input}
            value={draft}
            onChangeText={setDraft}
            placeholder={scopedTaskId ? "Ask about this task…" : "Ask about your tasks…"}
            placeholderTextColor={C.textFaint}
            multiline
            editable={!sending}
            onSubmitEditing={() => ask(draft)}
            accessibilityLabel="Message"
          />
          <Pressable
            onPress={() => ask(draft)}
            disabled={!draft.trim() || sending}
            style={({ pressed }) => [
              st.send,
              (!draft.trim() || sending) && { opacity: 0.4 },
              pressed && { opacity: 0.8 },
            ]}
            accessibilityRole="button"
            accessibilityLabel="Send"
          >
            <Icon name="arrow.up" size={17} tintColor="#FFFFFF" />
          </Pressable>
        </View>
      </View>
    </View>
  );
}
