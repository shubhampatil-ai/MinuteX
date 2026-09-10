// src/app/recording/[key]/assistant.tsx — the Assistant workspace.
//
// A ChatGPT-with-meeting-context screen: suggested actions, a persistent
// conversation, every generated document, AI-detected tasks, and a fixed
// chat input. This is the ONE place chat lives now — Overview has no inline
// "Ask MinuteX" anymore, just the floating Assistant button that opens here.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, Alert, Animated, Easing, Keyboard, Modal,
  Platform, Pressable, ScrollView, Share, StyleSheet, Text, TextInput, View,
} from "react-native";
import { Stack, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ELEV, FONT, R, S, useTheme, ColorScale } from "../../../../lib/theme";
import { RawGradient } from "../../../../lib/ui";
import { Icon } from "../../../../lib/icons";
import { Markdown } from "../../../../lib/document-renderer";
import { speakerName } from "../../../../lib/sources";
import { useMeeting } from "../../../../lib/meeting-context";
import { Tasks } from "../../../../lib/meeting-tasks";
import { CreateDocumentSheet, type GeneratedDoc } from "../../../../lib/meeting-documents";
import { MomEditorScreen } from "../../../../lib/mom-editor";
import {
  ApiError, ChatSource, ChatTurn, getAiChat, isNotReady, isRetryable, sendAiChat,
} from "../../../../lib/api";
import { canExportPdf, copyDocument, exportPdf } from "../../../../lib/export-doc";
import { exportDocx } from "../../../../lib/docx-export";

const SUGGESTED = [
  { icon: "checkmark.circle", label: "What are my action items?" },
  { icon: "envelope.badge", label: "Draft a follow-up email." },
  { icon: "doc.richtext", label: "Generate Minutes of Meeting." },
  { icon: "sparkles", label: "Explain this meeting." },
];

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    scrollBody: { paddingHorizontal: 20, paddingTop: S.md, paddingBottom: 24 },
    suggestGrid: { flexDirection: "row" as const, flexWrap: "wrap" as const, gap: S.sm },
    suggestCard: {
      flexBasis: "48%" as const, flexGrow: 1, backgroundColor: C.surface,
      borderWidth: 1, borderColor: C.border, borderRadius: R.card,
      padding: 13, gap: 8, shadowColor: C.shadow, ...ELEV.sm,
    },
    suggestIcon: {
      width: 30, height: 30, borderRadius: 10, backgroundColor: C.primarySoft,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    suggestTxt: { fontFamily: FONT.semibold, fontSize: 12.5, lineHeight: 17, color: C.text },
    bubbleUser: {
      alignSelf: "flex-end" as const, maxWidth: "86%" as const, backgroundColor: C.primarySoft,
      borderRadius: R.card, borderBottomRightRadius: 4,
      paddingHorizontal: 14, paddingVertical: 11, marginTop: 14,
    },
    bubbleUserTxt: { fontFamily: FONT.medium, fontSize: 14.5, lineHeight: 20, color: C.primaryStrong },
    bubbleTimestamp: { fontFamily: FONT.regular, fontSize: 10.5, color: C.textFaint, marginTop: 4, alignSelf: "flex-end" as const },
    bubbleAiRow: { flexDirection: "row" as const, gap: 10, marginTop: 14, alignItems: "flex-start" as const },
    aiAvatar: {
      width: 26, height: 26, borderRadius: 13, alignItems: "center" as const,
      justifyContent: "center" as const, marginTop: 2,
    },
    bubbleAi: { flex: 1, backgroundColor: C.surface, borderWidth: 1, borderColor: C.border, borderRadius: R.card, borderTopLeftRadius: 4, padding: 13 },
    // Sources sit INSIDE the answer bubble, under a hairline. Attached to the
    // claim they support rather than floating beside it, which is what makes
    // them read as evidence for this answer and not as a separate suggestion.
    sourcesWrap: { marginTop: 11, paddingTop: 9, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.border },
    sourcesLabel: { fontFamily: FONT.semibold, fontSize: 10.5, letterSpacing: 0.3, color: C.textFaint, textTransform: "uppercase" as const, marginBottom: 7 },
    sourceRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 6, paddingVertical: 4 },
    sourceTxt: { fontFamily: FONT.medium, fontSize: 12, color: C.primary },
    composerWrap: {
      borderTopWidth: 1, borderTopColor: C.border, backgroundColor: C.surface,
      paddingHorizontal: 14, paddingTop: 10,
    },
    composerRow: {
      flexDirection: "row" as const, alignItems: "flex-end" as const, gap: S.sm,
    },
    input: {
      flex: 1, fontFamily: FONT.regular, fontSize: 15, color: C.text,
      maxHeight: 110, backgroundColor: C.surface2, borderRadius: R.pill,
      paddingVertical: 11, paddingHorizontal: 16,
    },
    send: {
      width: 40, height: 40, borderRadius: 20, backgroundColor: C.primary,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    errBox: { backgroundColor: C.dangerSoft, borderRadius: R.md, padding: S.md, marginTop: 12 },
    working: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm, paddingVertical: 14, marginLeft: 36 },
    // The three pulsing dots. Sized and spaced to sit on the same optical line
    // as the label beside them rather than reading as punctuation.
    dotRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 4 },
    dot: { width: 6, height: 6, borderRadius: 3, backgroundColor: C.primary },
    docCard: {
      width: 148, backgroundColor: C.surface, borderWidth: 1, borderColor: C.border,
      borderRadius: R.card, padding: 13, marginRight: S.sm, shadowColor: C.shadow, ...ELEV.sm,
    },
    docIcon: { width: 32, height: 32, borderRadius: 10, alignItems: "center" as const, justifyContent: "center" as const },
    docLabel: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.text, marginTop: 9, lineHeight: 17 },
    docMeta: { fontFamily: FONT.regular, fontSize: 10.5, color: C.textFaint, marginTop: 4 },
  });
}

const DOC_VISUAL: Record<string, { icon: string; color: string }> = {
  minutes_of_meeting: { icon: "list.number", color: "#3E6BFF" },
  executive_summary: { icon: "doc.richtext", color: "#1FA972" },
  follow_up_email: { icon: "envelope.badge", color: "#F5A623" },
  whatsapp_update: { icon: "message.fill", color: "#1FA972" },
};
function docVisual(type: string) {
  return DOC_VISUAL[type] ?? { icon: "doc.richtext", color: "#7C5CFF" };
}

// A source timestamp, in the same M:SS / H:MM:SS form the transcript and the
// player already use — a different format here would read as a different kind
// of number.
function clock(seconds: number) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`;
}

/** Three dots breathing in sequence, while the assistant works.
 *
 * WHY AN ANIMATION AT ALL. A grounded answer on a long meeting takes two Groq
 * round-trips (retrieve, then answer) where it used to take one, so the wait
 * got longer at exactly the moment the screen had nothing to say about it. A
 * static spinner reads as "possibly stuck"; something that moves reads as
 * "working". The dots are the cheap, conventional signal for that.
 *
 * useNativeDriver so the animation runs on the UI thread — it must keep moving
 * while JS is busy parsing a long response, which is precisely when a
 * JS-driven animation would stutter and undo the reassurance.
 */
function ThinkingDots({ st }: { st: ReturnType<typeof buildStyles> }) {
  // useRef, not useState: these are animation handles, and re-creating them on
  // every render would restart the loop and produce a visible stutter.
  const dots = useRef([new Animated.Value(0.35), new Animated.Value(0.35),
                       new Animated.Value(0.35)]).current;

  useEffect(() => {
    const loops = dots.map((v, i) =>
      Animated.loop(Animated.sequence([
        // Staggered start, so the three read as a wave rather than a blink.
        Animated.delay(i * 160),
        Animated.timing(v, { toValue: 1, duration: 380, useNativeDriver: true,
                             easing: Easing.out(Easing.quad) }),
        Animated.timing(v, { toValue: 0.35, duration: 380, useNativeDriver: true,
                             easing: Easing.in(Easing.quad) }),
        Animated.delay((2 - i) * 160),
      ])));
    loops.forEach((l) => l.start());
    // Stopped on unmount: a loop left running holds the component alive and
    // keeps burning frames after the answer has arrived.
    return () => loops.forEach((l) => l.stop());
  }, [dots]);

  return (
    <View style={st.dotRow}>
      {dots.map((v, i) => (
        <Animated.View key={i} style={[st.dot, { opacity: v, transform: [{ scale: v }] }]} />
      ))}
    </View>
  );
}

/** What the assistant says it is doing, and when.
 *
 * The phases are NOT a fake progress bar — they mirror the two calls the
 * backend actually makes. A meeting whose transcript fits the context budget
 * is answered in ONE call with no retrieval step, so claiming to "search the
 * transcript" there would be theatre. `willRetrieve` decides which script runs,
 * from the same size test the backend uses.
 *
 * Timings are deliberately shorter than the real steps: a label that advances
 * slightly early reads as progress, whereas one that lags behind reality reads
 * as stuck. The last phase has no timer — it stays until the answer lands,
 * however long that takes.
 */
const RETRIEVE_PHASES = [
  { at: 0, label: "Searching the meeting…" },
  { at: 2600, label: "Reading the relevant sections…" },
  { at: 6000, label: "Writing the answer…" },
];
// The COMMON path in production: one Groq call with the whole transcript.
// Paced for that single call rather than for the two-call retrieval wait —
// "Writing the answer" at 3s on a one-call request would still be true, but
// arriving at 2s reads as more responsive on the request people actually make.
const DIRECT_PHASES = [
  { at: 0, label: "Reading the meeting…" },
  { at: 2000, label: "Writing the answer…" },
];

function useThinkingLabel(sending: boolean, willRetrieve: boolean) {
  const phases = willRetrieve ? RETRIEVE_PHASES : DIRECT_PHASES;
  const [label, setLabel] = useState(phases[0].label);

  useEffect(() => {
    if (!sending) return;
    setLabel(phases[0].label);
    const timers = phases.slice(1).map((p) =>
      setTimeout(() => setLabel(p.label), p.at));
    return () => timers.forEach(clearTimeout);
    // `phases` is derived from willRetrieve, so depending on that is enough and
    // avoids re-running this on every render from a fresh array identity.
  }, [sending, willRetrieve]);  // eslint-disable-line react-hooks/exhaustive-deps

  return label;
}

/** Where an answer came from, as tappable rows under it.
 *
 * REUSES THE EXISTING NAVIGATION, deliberately. Tapping a source pushes
 * /recording/[key]/transcript?evidence=seg_N — the identical route, param and
 * highlight/seek behaviour a task's "View in transcript" already uses. A second
 * mechanism for the same job would be two things to keep working, and the
 * transcript screen already does everything needed here.
 *
 * Renders NOTHING when there are no sources: an ungrounded answer, an older
 * stored turn and an older backend all produce that case, and it is ordinary
 * rather than an error worth reporting to the user.
 */
function Sources({ sources, recordingKey, speakerNames, C, st }: {
  sources?: ChatSource[];
  recordingKey: string;
  /** The meeting's speaker_id -> name map, for attributing each source. */
  speakerNames?: Record<string, string> | null;
  C: ColorScale;
  st: ReturnType<typeof buildStyles>;
}) {
  const router = useRouter();
  if (!sources?.length) return null;
  return (
    <View style={st.sourcesWrap}>
      <Text style={st.sourcesLabel}>Sources</Text>
      {sources.map((src) => {
        // WHO said it, resolved live from the speaker map — the API already
        // sends `speaker_id` on every source and it was being discarded, so
        // a row could only say when, never who. Resolving (rather than
        // storing) means a rename retitles every past answer's sources too.
        const who = src.speaker_id
          ? speakerName(src.speaker_id, speakerNames)
          : "";
        const label = who
          ? `${who} — ${clock(src.start_time)}`
          : `Transcript — ${clock(src.start_time)}`;
        return (
          <Pressable
            key={src.segment_id}
            onPress={() =>
              router.push({
                pathname: "/recording/[key]/transcript",
                params: { key: recordingKey, evidence: src.segment_id },
              } as never)
            }
            hitSlop={6}
            style={({ pressed }) => [st.sourceRow, pressed && { opacity: 0.6 }]}
            accessibilityRole="button"
            accessibilityLabel={`View this source in the transcript at ${clock(src.start_time)}`}
          >
            <Icon name="waveform" size={12} tintColor={C.primary} />
            <Text style={st.sourceTxt}>{label}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

export default function AssistantScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { key, rec, documents, addDocument, setDocuments, tasks, activity, logActivity } = useMeeting();
  const [historyOpen, setHistoryOpen] = useState(false);

  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [momOpen, setMomOpen] = useState(false);
  const [openDocIndex, setOpenDocIndex] = useState<number | null>(null);
  const lastAsked = useRef("");
  const scrollRef = useRef<ScrollView>(null);

  // Will this question need the two-call retrieval path?
  //
  // Mirrors the backend's own test (userApi's retrieve_meeting_context: does
  // the labelled transcript fit the context budget?) closely enough to pick
  // the right wording. It is ONLY used to choose a label, so being wrong on a
  // borderline meeting costs nothing — the phases still advance and the answer
  // is unaffected. Deliberately not exposed by the API: a round-trip to find
  // out what to say while waiting for a round-trip would be absurd.
  //
  // THE THRESHOLD. Measured against the DEPLOYED config, not the code default.
  // With GROQ_TPM_LIMIT=300000 in production the binding constraint is the
  // model's 131k context window, not the TPM quota, which puts the budget near
  // 369k chars — so retrieval engages only past roughly eight hours of talk,
  // and virtually every real meeting takes the one-call path. 300k is set
  // deliberately BELOW that budget: overshooting means promising a search that
  // does not happen, while undershooting only costs a slightly generic label on
  // a meeting nobody records.
  //
  // If the Groq plan is ever downgraded this becomes wrong in the harmless
  // direction (it under-predicts retrieval), which is why it is a constant here
  // and not a second budget calculation to keep in sync.
  const willRetrieve = (rec?.transcript?.length ?? 0) > 300000;
  const thinkingLabel = useThinkingLabel(sending, willRetrieve);

  // ---- Keyboard height, measured rather than inferred ---------------------
  //
  // The composer is pinned to the bottom of the screen, which is exactly where
  // the keyboard appears, so something has to lift it. KeyboardAvoidingView is
  // the usual answer and it does NOT work on this screen.
  //
  // WHY NOT KeyboardAvoidingView. It derives its inset from
  //   frame.y + frame.height - (keyboardFrame.screenY - keyboardVerticalOffset)
  // where `frame` comes from its own onLayout. That subtraction only means
  // anything if both terms are in the same coordinate space. This screen is
  // registered with presentation: "modal" (see _layout.tsx), so under
  // react-native-screens it lives in its own native container and its onLayout
  // frame is container-relative while the keyboard's screenY is window-relative.
  // The two disagree, the difference collapses toward zero, and you get no
  // padding — silently, with no error and nothing visibly wrong in the code.
  // That is why every other screen in the app is fine with the shared wrapper
  // and this one was not.
  //
  // Tracking endCoordinates.height directly skips the measurement entirely:
  // the OS tells us how tall the keyboard is, and we pad by that. Nothing here
  // depends on window resizing either, so it is also correct under the enforced
  // edge-to-edge of Android 15+, where the OS no longer resizes the window.
  //
  // ANDROID USES keyboardDidShow, NOT keyboardWillShow — the "will" events are
  // iOS-only, so listening for them on Android silently never fires.
  const [kbHeight, setKbHeight] = useState(0);
  useEffect(() => {
    const showEvent = Platform.OS === "ios" ? "keyboardWillShow" : "keyboardDidShow";
    const hideEvent = Platform.OS === "ios" ? "keyboardWillHide" : "keyboardDidHide";
    const show = Keyboard.addListener(showEvent, (e) => {
      setKbHeight(e.endCoordinates?.height ?? 0);
      // Lifting the composer shortens the scroll view, which would otherwise
      // leave the newest message hidden behind the keyboard in an existing
      // conversation. Runs after the lift has laid out.
      requestAnimationFrame(() => scrollRef.current?.scrollToEnd({ animated: true }));
    });
    const hide = Keyboard.addListener(hideEvent, () => setKbHeight(0));
    return () => { show.remove(); hide.remove(); };
  }, []);

  // The keyboard's reported height spans from the bottom of the SCREEN, so on a
  // device with a gesture bar it already covers the area `insets.bottom` also
  // accounts for. Subtracting it prevents padding by that strip twice; clamped
  // at 0 so a device without one is unaffected.
  const composerLift = kbHeight > 0 ? Math.max(kbHeight - insets.bottom, 0) : 0;

  useEffect(() => {
    if (!key) return;
    (async () => {
      try {
        const res = await getAiChat(key);
        setTurns(res.chat_history);
      } catch {
        // Non-fatal — the composer still works without prior history.
      }
    })();
  }, [key]);

  const ask = useCallback(async (message: string) => {
    const text = message.trim();
    if (!text || sending) return;
    lastAsked.current = text;
    setError("");
    setDraft("");
    const optimistic: ChatTurn[] = [...turns, { role: "user", content: text }];
    setTurns(optimistic);
    setSending(true);
    try {
      const res = await sendAiChat(key, text, turns);
      setTurns(res.chat_history);
      logActivity(`Asked the Assistant: "${text.length > 60 ? `${text.slice(0, 60)}…` : text}"`);
    } catch (e) {
      setTurns(turns);
      if (isNotReady(e)) setError("The transcript isn't ready yet.");
      else if (isRetryable(e)) setError("Unable to generate AI output.");
      else setError(e instanceof ApiError ? e.message : "Something went wrong.");
    } finally {
      setSending(false);
      requestAnimationFrame(() => scrollRef.current?.scrollToEnd({ animated: true }));
    }
  }, [key, turns, sending, logActivity]);

  const openDoc = openDocIndex != null ? documents[openDocIndex] : null;

  return (
    <View style={st.container}>
      <Stack.Screen
        options={{
          title: "Assistant",
          headerLeft: () => (
            <Pressable onPress={() => router.back()} hitSlop={10} accessibilityLabel="Close">
              <Icon name="xmark" tintColor={C.text} size={20} />
            </Pressable>
          ),
          headerRight: () => (
            <Pressable onPress={() => setHistoryOpen(true)} hitSlop={10} accessibilityLabel="Activity history">
              <Icon name="clock.arrow.circlepath" tintColor={C.textFaint} size={20} />
            </Pressable>
          ),
        }}
      />

      {/* A plain View, deliberately — see the composerLift note above for why
          KeyboardAvoidingView cannot measure this screen correctly. The lift is
          applied to the composer itself at the bottom of this tree. */}
      <View style={{ flex: 1 }}>
        <ScrollView
          ref={scrollRef}
          contentContainerStyle={st.scrollBody}
          showsVerticalScrollIndicator={false}
          keyboardDismissMode="on-drag"
         keyboardShouldPersistTaps="handled">
          {!turns.length ? (
            <View>
              <Text style={T.label}>Suggested</Text>
              <View style={[st.suggestGrid, { marginTop: 9 }]}>
                {SUGGESTED.map((s) => (
                  <Pressable
                    key={s.label}
                    onPress={() => ask(s.label)}
                    disabled={sending}
                    style={({ pressed }) => [st.suggestCard, pressed && { opacity: 0.75 }]}
                    accessibilityLabel={s.label}
                  >
                    <View style={st.suggestIcon}>
                      <Icon name={s.icon as any} tintColor={C.primary} size={15} />
                    </View>
                    <Text style={st.suggestTxt}>{s.label}</Text>
                  </Pressable>
                ))}
              </View>
            </View>
          ) : null}

          {turns.map((t, i) => (
            t.role === "user" ? (
              <View key={i} style={{ alignItems: "flex-end" }}>
                <View style={st.bubbleUser}>
                  <Text style={st.bubbleUserTxt}>{t.content}</Text>
                </View>
                {t.at ? <Text style={st.bubbleTimestamp}>{new Date(t.at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}</Text> : null}
              </View>
            ) : (
              <View key={i} style={st.bubbleAiRow}>
                <RawGradient colors={[C.primary, C.accent]} style={st.aiAvatar}>
                  <Icon name="sparkles" tintColor="#FFFFFF" size={13} />
                </RawGradient>
                <View style={st.bubbleAi}>
                  <Markdown text={t.content} />
                  <Sources sources={t.sources} recordingKey={key}
                    speakerNames={rec?.speaker_names} C={C} st={st} />
                </View>
              </View>
            )
          ))}

          {sending ? (
            <View style={st.working} accessibilityRole="progressbar"
                  accessibilityLabel={thinkingLabel}>
              <ThinkingDots st={st} />
              <Text style={T.bodyDim}>{thinkingLabel}</Text>
            </View>
          ) : null}

          {error ? (
            <View style={st.errBox}>
              <Text style={[T.body, { color: C.danger }]}>{error}</Text>
              <Pressable onPress={() => ask(lastAsked.current)} hitSlop={6} style={{ marginTop: 8 }} accessibilityLabel="Retry">
                <Text style={{ fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary }}>Retry</Text>
              </Pressable>
            </View>
          ) : null}

          {documents.length ? (
            <View style={{ marginTop: S.xl }}>
              <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
                <Text style={T.label}>Generated Documents</Text>
                <Pressable onPress={() => setCreateOpen(true)} hitSlop={6}>
                  <Text style={{ fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary }}>View all</Text>
                </Pressable>
              </View>
              <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ marginTop: 10 }} keyboardShouldPersistTaps="handled">
                {documents.map((doc, i) => {
                  const v = docVisual(doc.type);
                  return (
                    <Pressable key={`${doc.type}-${doc.generated_at}`} onPress={() => setOpenDocIndex(i)} style={st.docCard}>
                      <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
                        <View style={[st.docIcon, { backgroundColor: v.color + "22" }]}>
                          <Icon name={v.icon as any} tintColor={v.color} size={16} />
                        </View>
                        <Icon name="ellipsis" tintColor={C.textFaint} size={14} />
                      </View>
                      <Text style={st.docLabel} numberOfLines={2}>{doc.label}</Text>
                      <Text style={st.docMeta}>
                        {new Date(doc.generated_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                      </Text>
                    </Pressable>
                  );
                })}
              </ScrollView>
            </View>
          ) : null}

          {tasks.length ? (
            <View style={{ marginTop: S.xl }}>
              <Tasks
                tasks={tasks}
                onOpenTask={(id) => router.push({ pathname: "/recording/[key]/task/[taskId]", params: { key, taskId: id } })}
                onViewAll={() => router.push({ pathname: "/recording/[key]/task", params: { key } })}
              />
            </View>
          ) : null}

          {activity.length ? (
            <View style={{ marginTop: S.xl }}>
              <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
                <Text style={T.label}>History</Text>
                <Pressable onPress={() => setHistoryOpen(true)} hitSlop={6}>
                  <Text style={{ fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary }}>View all</Text>
                </Pressable>
              </View>
              <View style={{ marginTop: 10 }}>
                {activity.slice(0, 3).map((a) => (
                  <View key={a.id} style={{ flexDirection: "row", gap: 10, marginTop: 10, alignItems: "flex-start" }}>
                    <View style={{ width: 6, height: 6, borderRadius: 3, backgroundColor: C.primary, marginTop: 6 }} />
                    <View style={{ flex: 1 }}>
                      <Text style={{ fontFamily: FONT.medium, fontSize: 13, color: C.text }}>{a.text}</Text>
                      <Text style={[T.caption, { marginTop: 2 }]}>
                        {new Date(a.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
                      </Text>
                    </View>
                  </View>
                ))}
              </View>
            </View>
          ) : null}
        </ScrollView>

        {/* ---- Fixed chat input ----
            marginBottom (not paddingBottom) so composerWrap's own border and
            background stop at the top of the keyboard instead of the surface
            stretching down behind it. */}
        <View style={[st.composerWrap, { marginBottom: composerLift }]}>
          <View style={st.composerRow}>
            <Pressable onPress={() => setCreateOpen(true)} hitSlop={8} accessibilityLabel="Create document">
              <Icon name="plus" tintColor={C.textDim} size={22} />
            </Pressable>
            <TextInput
              style={st.input}
              value={draft}
              onChangeText={setDraft}
              placeholder="Ask anything about this meeting…"
              placeholderTextColor={C.textFaint}
              multiline
              returnKeyType="send"
              onSubmitEditing={() => ask(draft)}
              editable={!sending}
            />
            <Pressable
              onPress={() => ask(draft)}
              disabled={!draft.trim() || sending}
              style={({ pressed }) => [
                st.send,
                (!draft.trim() || sending) && { opacity: 0.4 },
                pressed && { opacity: 0.8 },
              ]}
              accessibilityLabel="Send"
            >
              <Icon name="arrow.up" tintColor="#FFFFFF" size={17} />
            </Pressable>
          </View>
          <View style={{ height: S.sm }} />
        </View>
      </View>

      <CreateDocumentSheet
        visible={createOpen}
        recordingKey={key}
        onClose={() => setCreateOpen(false)}
        onGenerated={addDocument}
        onOpenMomEditor={() => setMomOpen(true)}
      />

      {/* Minutes of Meeting is structured, so it opens its own editor here
          exactly as it does from Overview — one MoM, one editor, whichever
          surface the user reached it from. */}
      <Modal
        visible={momOpen}
        animationType="slide"
        onRequestClose={() => setMomOpen(false)}
      >
        <MomEditorScreen
          recordingKey={key}
          meetingTitle={rec?.title || "Meeting"}
          onClose={() => setMomOpen(false)}
          onDocumentChange={addDocument}
        />
      </Modal>

      {openDoc ? (
        <AssistantDocSheet
          doc={openDoc}
          recordingKey={key}
          meetingTitle={rec?.title || "Meeting"}
          onClose={() => setOpenDocIndex(null)}
          onSave={(d) => {
            const next = documents.slice();
            next[openDocIndex!] = d;
            setDocuments(next);
          }}
          onDelete={() => {
            setDocuments(documents.filter((_, i) => i !== openDocIndex));
            setOpenDocIndex(null);
          }}
        />
      ) : null}

      <Modal visible={historyOpen} animationType="slide" onRequestClose={() => setHistoryOpen(false)}>
        <View style={{ flex: 1, backgroundColor: C.bg }}>
          <View style={{
            flexDirection: "row", alignItems: "center", gap: S.md,
            paddingHorizontal: 20, paddingTop: 60, paddingBottom: 14,
            borderBottomWidth: 1, borderBottomColor: C.border,
          }}>
            <Pressable onPress={() => setHistoryOpen(false)} hitSlop={8} accessibilityLabel="Close">
              <Icon name="xmark" tintColor={C.text} size={20} />
            </Pressable>
            <Text style={{ fontFamily: FONT.extrabold, fontSize: 18, color: C.text, flex: 1 }}>Activity</Text>
          </View>
          <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 60 }} keyboardShouldPersistTaps="handled">
            {activity.length ? activity.map((a) => (
              <View key={a.id} style={{ flexDirection: "row", gap: 12, marginBottom: 18 }}>
                <View style={{ alignItems: "center" }}>
                  <View style={{ width: 9, height: 9, borderRadius: 4.5, backgroundColor: C.primary }} />
                  <View style={{ width: 1.5, flex: 1, backgroundColor: C.border, marginTop: 4 }} />
                </View>
                <View style={{ flex: 1, paddingBottom: 4 }}>
                  <Text style={{ fontFamily: FONT.medium, fontSize: 14, color: C.text }}>{a.text}</Text>
                  <Text style={[T.caption, { marginTop: 3 }]}>
                    {new Date(a.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
                  </Text>
                </View>
              </View>
            )) : (
              <Text style={T.bodyDim}>Nothing has happened in this meeting yet.</Text>
            )}
          </ScrollView>
        </View>
      </Modal>
    </View>
  );
}

// A lightweight, view-only-first sheet for a document opened from this
// screen's horizontal document rail — reuses the same export/share plumbing
// as the Overview document sheet without importing its private component.
function AssistantDocSheet({
  doc, recordingKey, meetingTitle, onClose, onSave, onDelete,
}: {
  doc: GeneratedDoc;
  recordingKey: string;
  meetingTitle: string;
  onClose: () => void;
  onSave: (doc: GeneratedDoc) => void;
  onDelete: () => void;
}) {
  const { C, T } = useTheme();
  const [busy, setBusy] = useState<"pdf" | "docx" | "copy" | null>(null);
  const [toast, setToast] = useState("");

  const flash = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(""), 1800);
  };

  const doCopy = async () => {
    setBusy("copy");
    const r = await copyDocument(doc.content);
    setBusy(null);
    flash(r === "copied" ? "Copied" : r === "shared" ? "Shared" : "Couldn't copy");
  };
  const doShare = async () => {
    try { await Share.share({ message: doc.content, title: doc.label }); } catch { /* cancelled */ }
  };
  const doPdf = async () => {
    setBusy("pdf");
    const r = await exportPdf(doc.content, doc.label, meetingTitle);
    setBusy(null);
    flash(r === "shared" ? "PDF shared" : r === "saved" ? "PDF saved" : "Couldn't make the PDF");
  };
  const doDocx = async () => {
    setBusy("docx");
    const r = await exportDocx(doc.content, doc.label, meetingTitle);
    setBusy(null);
    flash(r === "shared" ? "Document shared" : r === "saved" ? "Document saved" : "Couldn't export");
  };
  const confirmDelete = () => {
    Alert.alert("Delete document", `Delete "${doc.label}"?`, [
      { text: "Cancel", style: "cancel" },
      { text: "Delete", style: "destructive", onPress: onDelete },
    ]);
  };

  return (
    <View style={{
      position: "absolute", left: 0, right: 0, top: 0, bottom: 0,
      backgroundColor: C.bg,
    }}>
      <View style={{
        flexDirection: "row", alignItems: "center", gap: S.md,
        paddingHorizontal: 20, paddingTop: 60, paddingBottom: 14,
        borderBottomWidth: 1, borderBottomColor: C.border,
      }}>
        <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
          <Icon name="xmark" tintColor={C.text} size={20} />
        </Pressable>
        <Text style={{ fontFamily: FONT.extrabold, fontSize: 18, color: C.text, flex: 1 }} numberOfLines={1}>
          {doc.label}
        </Text>
      </View>
      <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 100 }} keyboardShouldPersistTaps="handled">
        {toast ? <Text style={[T.caption, { marginBottom: 10, color: C.primary }]}>{toast}</Text> : null}
        <Markdown text={doc.content} />
      </ScrollView>
      <View style={{
        flexDirection: "row", flexWrap: "wrap", gap: S.sm,
        paddingHorizontal: 20, paddingVertical: 14, borderTopWidth: 1, borderTopColor: C.border,
      }}>
        <DocChip icon="square.and.arrow.up" label="Share" onPress={doShare} />
        <DocChip icon="doc.on.doc" label="Copy" onPress={doCopy} busy={busy === "copy"} />
        <DocChip icon="arrow.down.doc" label="PDF" onPress={doPdf} busy={busy === "pdf"} disabled={!canExportPdf()} />
        <DocChip icon="arrow.down.doc" label="DOCX" onPress={doDocx} busy={busy === "docx"} />
        <DocChip icon="trash" label="Delete" onPress={confirmDelete} tint={C.danger} />
      </View>
    </View>
  );
}

function DocChip({
  icon, label, onPress, busy, disabled, tint,
}: {
  icon: Parameters<typeof Icon>[0]["name"]; label: string; onPress: () => void;
  busy?: boolean; disabled?: boolean; tint?: string;
}) {
  const { C } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || busy}
      style={({ pressed }) => [{
        flexDirection: "row", alignItems: "center", gap: 6,
        backgroundColor: C.surface2, borderRadius: R.pill,
        paddingHorizontal: 13, paddingVertical: 9,
      }, disabled && { opacity: 0.4 }, pressed && { opacity: 0.7 }]}
    >
      {busy ? <ActivityIndicator color={tint ?? C.textDim} size="small" /> : <Icon name={icon} tintColor={tint ?? C.textDim} size={14} />}
      <Text style={{ fontFamily: FONT.semibold, fontSize: 12.5, color: tint ?? C.text }}>{label}</Text>
    </Pressable>
  );
}
