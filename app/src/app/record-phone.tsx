// src/app/record-phone.tsx — in-app phone recording (source: MOBILE).
// The phone-microphone sibling of record.tsx (which remote-controls the
// hardware). Three distinct states — READY, RECORDING, PAUSED — plus the
// transient ones the controller reports (finalizing, completed, error).
//
// THE SCREEN OWNS NO RECORDING STATE.
// It reads lib/rec-controller.ts through useRecorder() and sends it commands.
// That inversion is the point: the recorder used to live in this component via
// useAudioRecorder(), which meant leaving the screen released it — the
// recording ended when the user navigated away — and the displayed state was
// React's idea of what it had asked for rather than what the microphone was
// actually doing. Now the controller survives navigation, reconciles itself
// against the native recorder, and this file is a view of it. Coming back to
// this screen mid-recording paints the live state on the first frame.
//
// THE WAVEFORM IS REAL. It is drawn from the controller's input level
// (lib/use-input-level.ts → lib/audio-level.ts → lib/waveform.tsx), sampled
// only while audio is genuinely being captured. Silence draws flat; a paused
// recorder shows its history frozen and dimmed. Nothing here animates for
// decoration — see lib/waveform.tsx for what was removed and why.
//
// Stop finalizes the audio into the app's document directory and hands the
// session to the shared UploadManager, which uploads it when there is a
// network and keeps it safe when there isn't. The user lands back where they
// came from, where the upload banner tracks progress.
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Platform, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import Animated, { FadeIn, FadeOut } from "react-native-reanimated";
import { Icon } from "../../lib/icons";
import { FONT, GUTTER, R, S, STUDIO } from "../../lib/theme";
import { MOTION, PulseDot, PressSpring } from "../../lib/motion";
import { BottomSheet, IconButton, RoundControl, SheetAction } from "../../lib/kit";
import { LevelWaveform } from "../../lib/waveform";
import { useWaveformLevels } from "../../lib/use-input-level";
import { openAppSettings } from "../../lib/permissions";
import { startUpload } from "../../lib/uploads";
import { canSilenceRinger, openSilenceRingerSettings } from "../../lib/audio-focus";
import { store } from "../../lib/storage";

import { useRecorder } from "../../lib/use-recorder";
import {
  REC_FORMAT,
  clearError,
  discard,
  getSession,
  listInputs,
  pauseByUser,
  refreshPermission,
  requestPermission,
  reset,
  resumeByUser,
  selectInput,
  start,
  stop,
} from "../../lib/rec-controller";
import { isPaused } from "../../lib/rec-store";
import { formatRecLog, getRecLog, subscribeRecLog } from "../../lib/rec-log";
import { copyText } from "../../lib/clipboard";

// Whether we have already offered to silence the ringer. Persisted, because
// "ask once" has to mean once ever, not once per app launch.
const SILENCE_ASK_KEY = "minutex.silenceRingerAsked";

// Bars in the live waveform. At 70ms per sample this is ~3.4s of history —
// long enough to see the shape of a sentence, short enough to feel live.
const WAVE_BARS = 48;

function fmt(sec: number) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/**
 * The one line the user must be able to trust. Colour carries the meaning:
 * red = capturing, amber = paused for a reason outside the user's control,
 * dim = the user's own pause, green = saved.
 */
function statusChrome(state: string): { color: string; label: string } {
  switch (state) {
    case "RECORDING": return { color: STUDIO.recording, label: "Recording" };
    case "PAUSED_BY_USER": return { color: STUDIO.textDim, label: "Paused" };
    case "PAUSED_BY_CALL": return { color: STUDIO.warn, label: "Paused because of a call" };
    case "PAUSED_BY_AUDIO_INTERRUPTION": return { color: STUDIO.warn, label: "Paused — audio interrupted" };
    case "PAUSED_BY_MICROPHONE": return { color: STUDIO.warn, label: "Microphone unavailable" };
    case "FINALIZING": return { color: STUDIO.textDim, label: "Saving…" };
    case "COMPLETED": return { color: STUDIO.ok, label: "Saved" };
    case "ERROR": return { color: STUDIO.recording, label: "Couldn't record" };
    default: return { color: STUDIO.textFaint, label: "Ready" };
  }
}

/** The explanatory line under the timer, per state. */
function stateCopy(state: string, denied: boolean): string {
  if (denied) return "Microphone access is required to record.";
  switch (state) {
    case "RECORDING": return "Locking the screen or switching apps won't stop it.";
    case "PAUSED_BY_CALL": return "We'll pick up again automatically when the call ends.";
    case "PAUSED_BY_AUDIO_INTERRUPTION": return "Another app took the microphone. We'll resume when it's free.";
    case "PAUSED_BY_MICROPHONE": return "The microphone isn't available right now. Everything recorded so far is safe.";
    case "PAUSED_BY_USER": return "Resume when you're ready, or stop to file the brief.";
    case "FINALIZING": return "Saving the recording to this phone…";
    case "COMPLETED": return "Saved. It'll upload on its own.";
    default: return "Record, transcribe and get a concise brief.";
  }
}

export default function RecordPhoneScreen() {
  const router = useRouter();
  // This screen is always on the STUDIO ground (lib/theme.tsx), in either
  // theme, so it takes no colours from useTheme — only the safe-area insets
  // and the controller's snapshot.
  const insets = useSafeAreaInsets();

  const rec = useRecorder();
  const [sourceOpen, setSourceOpen] = useState(false);
  const [inputsOpen, setInputsOpen] = useState(false);
  const [showLog, setShowLog] = useState(false);
  const [logText, setLogText] = useState("");
  const [copied, setCopied] = useState(false);
  const [focused, setFocused] = useState(true);

  // Refresh the log while the panel is open. Cheap (a formatted read of a
  // bounded in-memory ring buffer) and only runs while the panel is visible,
  // so it costs nothing during a normal recording.
  useEffect(() => {
    if (!showLog) return;
    const read = () => setLogText(formatRecLog(getRecLog().slice(-60)));
    read();
    const unsub = subscribeRecLog(read);
    return unsub;
  }, [showLog]);

  const copyDiagnostics = useCallback(async () => {
    await copyText(formatRecLog(getRecLog()));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, []);
  // Same synchronous re-entrancy guard as record.tsx: the controller's `busy`
  // flag is authoritative, but it round-trips through a subscription, so a
  // fast double-tap can still re-enter before the re-render lands.
  const inFlightRef = useRef(false);

  // Read the real permission whenever this screen becomes visible. A
  // permission revoked in Settings while we were backgrounded arrives with no
  // event at all, so focus is the only reliable moment to notice. Focus also
  // gates the waveform sampler: a screen underneath another draws nothing.
  useFocusEffect(
    useCallback(() => {
      void refreshPermission();
      setFocused(true);
      return () => setFocused(false);
    }, [])
  );

  // Clear a finished/errored session on the way out so returning to the screen
  // starts clean — but never touch a LIVE one, which is what lets the user
  // navigate away mid-recording and come back to it.
  useEffect(() => {
    return () => {
      const s = getSession();
      if (s && (s.state === "COMPLETED" || s.state === "ERROR")) reset();
    };
  }, []);

  const recording = rec.state === "RECORDING";
  const paused = isPaused(rec.state);
  const live = recording || paused;
  const denied = rec.permission === "denied" || rec.permission === "blocked";
  const chrome = statusChrome(rec.state);
  const controlsBusy = rec.busy || rec.state === "FINALIZING";

  // The waveform's data. Sampled only while capture is live AND this screen
  // is the one on top; a new session starts from an empty field.
  const levels = useWaveformLevels({
    active: recording && focused,
    bars: WAVE_BARS,
    resetKey: rec.sessionId,
  });

  /**
   * Offer to silence the ringer, ONCE, ever — and RESOLVE before recording.
   *
   * Do Not Disturb access cannot be requested from a runtime dialog — it only
   * exists as a system Settings screen — so the honest flow is to explain why
   * it helps and hand the user a button that takes them there.
   *
   * WHY THIS AWAITS
   * This used to be fired off after start(), which meant the recorder was
   * already capturing while the dialog sat on screen, and tapping "Open
   * Settings" backgrounded the app — recording the walk through Settings and
   * whatever was said during it. The user asked to make a decision about
   * silence and was recorded while making it. Resolving first means nothing is
   * captured until the choice is made.
   *
   * The promise settles on dismissal, not on the outcome of the Settings trip:
   * we deliberately do NOT wait for the user to come back and grant access.
   * Blocking a meeting recording on a system settings screen would be worse
   * than a ringtone in the file. If they do grant it, it applies to the next
   * recording.
   *
   * Asked once and never again: a recorder that nags before every meeting is
   * worse than one that occasionally records a ringtone.
   */
  const maybeOfferSilence = useCallback(async (): Promise<void> => {
    if (Platform.OS !== "android") return;       // DND access is Android-only
    if (canSilenceRinger()) return;              // already granted
    const asked = await store.getItemAsync(SILENCE_ASK_KEY);
    if (asked) return;                            // asked before; respect that
    // Written BEFORE showing the alert so a crash or a force-quit at the dialog
    // cannot turn "ask once" into "ask every launch".
    await store.setItemAsync(SILENCE_ASK_KEY, "1");

    return new Promise<void>((resolve) => {
      // Guarantees resolve() runs exactly once however the alert is dismissed —
      // a second call from an OEM that fires both onPress and onDismiss would
      // otherwise be a silently swallowed no-op, and a path that never resolved
      // would hang the record button forever.
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        resolve();
      };

      Alert.alert(
        "Silence your ringer while recording?",
        "If a call comes in, the ringtone and vibration get picked up by the " +
        "microphone. MinuteX can mute the ringer while you record and put it " +
        "back afterwards.\n\nThis needs Do Not Disturb access, which " +
        "Android only lets you grant from Settings. Recording starts as soon " +
        "as you choose.",
        [
          { text: "Not now", style: "cancel", onPress: done },
          {
            text: "Open Settings",
            onPress: () => {
              void openSilenceRingerSettings().then((opened) => {
                if (!opened) {
                  Alert.alert(
                    "Could not open Settings",
                    "Look for “Do Not Disturb access” in your phone's " +
                    "notification settings and enable it for MinuteX."
                  );
                }
              });
              // Resolve immediately rather than waiting for the Settings round
              // trip: the user tapped Record, and the recording should be
              // running when they return.
              done();
            },
          },
        ],
        { onDismiss: done }        // Android back button / tap-outside
      );
    });
  }, []);

  const begin = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      // Resolve the ringer question BEFORE any audio is captured, so the user
      // is never recorded while deciding whether to be recorded, and so a trip
      // to Settings cannot happen mid-recording. Never fatal: any failure here
      // must not stop the user from recording a meeting.
      try {
        await maybeOfferSilence();
      } catch {
        // Storage unavailable, or an alert that could not be presented.
      }

      const res = await start();
      if (!res.ok && res.needsPermission && !res.needsSettings) {
        // The dialog was shown and declined. requestPermission already ran
        // inside start(); nothing more to do here but let the error render.
        await requestPermission();
      }
    } finally {
      inFlightRef.current = false;
    }
  };

  // Stop -> the controller finalizes the file (verifying it holds real audio)
  // and returns the session. Hand it to the shared UploadManager, which owns
  // retries and offline waiting, then return to where the user came from,
  // where the banner takes over. The recording is on disk in the document
  // directory before this function returns, so navigating away cannot lose it.
  const stopAndSave = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const done = await stop();
      if (!done) return;
      if (done.state === "ERROR" || !done.fileUri) {
        // finalize() already put the reason on the session; the banner on this
        // screen shows it. Deliberately NOT navigating away — the user should
        // see that nothing was captured rather than find a missing recording.
        return;
      }
      startUpload({
        source: "MOBILE",
        fileUri: done.fileUri,
        format: done.format || REC_FORMAT,
        duration: done.duration || undefined,
        size: done.size ?? undefined,
        sessionId: done.id,
      }).catch(() => { /* surfaced by the Desk upload banner */ });
      reset();
      router.back();
    } finally {
      inFlightRef.current = false;
    }
  };

  const cancel = () => {
    if (!live) { reset(); router.back(); return; }
    Alert.alert(
      "Discard recording?",
      "This recording will be deleted and can't be recovered.",
      [
        { text: "Keep recording", style: "cancel" },
        {
          text: "Discard", style: "destructive",
          onPress: async () => {
            await discard();
            router.back();
          },
        },
      ]
    );
  };

  // Leaving the phone recorder for another source. Only offered while nothing
  // is being captured — switching source mid-recording would abandon it.
  const switchSource = (href: "/record" | "/upload") => {
    setSourceOpen(false);
    reset();
    router.replace(href);
  };

  const inputs = live ? listInputs() : [];

  return (
    <View style={[st.container, { paddingTop: insets.top + S.sm, paddingBottom: Math.max(insets.bottom, S.lg) + S.lg }]}>
      {/* ---- Top bar: close · source ---- */}
      <View style={st.topRow}>
        <IconButton
          icon="xmark"
          onPress={cancel}
          accessibilityLabel={live ? "Close — asks before discarding" : "Close"}
          tint={STUDIO.textDim}
        />
        <Pressable
          onPress={() => (live ? (inputs.length > 1 ? setInputsOpen(true) : undefined) : setSourceOpen(true))}
          disabled={live && inputs.length <= 1}
          accessibilityRole="button"
          accessibilityLabel={
            live
              ? `Recording with ${rec.inputName ?? "this phone"}${inputs.length > 1 ? ". Change microphone" : ""}`
              : "Recording source: this phone. Change source"
          }
          style={({ pressed }) => [st.sourceChip, pressed && { opacity: 0.7 }]}
        >
          <View style={[st.sourceDot, { backgroundColor: denied ? STUDIO.recording : STUDIO.ok }]} />
          <Text style={st.sourceTxt} numberOfLines={1}>
            {live && rec.inputName ? rec.inputName : "This phone"}
          </Text>
          {!live || inputs.length > 1 ? (
            <Icon name="chevron.down" tintColor={STUDIO.textFaint} size={14} />
          ) : null}
        </Pressable>
      </View>

      {/* ---- Centre: ready | live ---- */}
      <View style={st.center}>
        {live || rec.state === "FINALIZING" ? (
          <Animated.View
            key="live"
            entering={FadeIn.duration(MOTION.standard)}
            exiting={FadeOut.duration(MOTION.fast)}
            style={st.liveBlock}
          >
            {/* The unambiguous answer to "is it recording?" — every state,
                including the ones the user didn't ask for. */}
            <View
              style={st.statusRow}
              accessibilityLabel={`Status: ${chrome.label}`}
              accessibilityLiveRegion="polite"
            >
              <PulseDot active={recording} color={chrome.color} size={9} />
              <Text style={[st.statusTxt, { color: chrome.color }]}>{chrome.label}</Text>
            </View>

            <Pressable
              onLongPress={() => setShowLog((v) => !v)}
              delayLongPress={700}
              accessibilityRole="text"
              accessibilityLabel={`Elapsed ${fmt(rec.seconds)}`}
              accessibilityHint="Long press to show recording diagnostics"
            >
              <Text style={[st.timer, paused && { color: STUDIO.textDim }]}>{fmt(rec.seconds)}</Text>
            </Pressable>

            <LevelWaveform
              levels={levels}
              bars={WAVE_BARS}
              height={112}
              barWidth={3}
              color={recording ? STUDIO.primary : STUDIO.waveIdle}
              emptyColor={STUDIO.border}
              style={st.wave}
              accessibilityLabel={recording ? "Live input level" : "Input level, paused"}
            />

            <Text style={st.state}>{stateCopy(rec.state, denied)}</Text>
          </Animated.View>
        ) : (
          <Animated.View
            key="ready"
            entering={FadeIn.duration(MOTION.standard)}
            exiting={FadeOut.duration(MOTION.fast)}
            style={st.readyBlock}
          >
            <Text style={st.readyTitle} accessibilityRole="header">
              {rec.state === "COMPLETED" ? "Saved" : "Ready when you are"}
            </Text>
            <Text style={st.readySub}>{stateCopy(rec.state, denied)}</Text>

            <View style={st.micHalo}>
              <PressSpring
                onPress={begin}
                disabled={denied || controlsBusy}
                accessibilityLabel="Start recording"
                pressedScale={MOTION.pressScale}
                style={[st.micBtn, (denied || controlsBusy) && { opacity: 0.4 }]}
              >
                <Icon name="mic.fill" tintColor="#FFFFFF" size={34} />
              </PressSpring>
            </View>
            <Text style={st.readyHint}>{denied ? "Microphone blocked" : "Tap to start"}</Text>
          </Animated.View>
        )}
      </View>

      {/* ---- Warnings / errors — above the controls, never over them ---- */}
      {rec.lowStorage ? (
        <Text style={st.warnBand}>
          Storage is running low. The recording will be saved automatically if
          space runs out.
        </Text>
      ) : null}

      {rec.error ? (
        <Pressable onPress={clearError} accessibilityRole="button" accessibilityLabel={`${rec.error}. Dismiss`}>
          <Text style={st.error}>{rec.error}</Text>
        </Pressable>
      ) : null}

      {/* Diagnostics — hidden behind a long-press on the timer.
          Interruption bugs (a call that doesn't pause, audio that goes silent
          after one) only reproduce on a real handset, where there is no
          debugger attached. This makes the event log readable and copyable on
          the device itself, which is the difference between "it didn't work"
          and knowing whether the focus event even arrived. */}
      {showLog ? (
        <View style={st.logPanel}>
          <View style={st.logHead}>
            <Text style={st.logTitle}>Diagnostics</Text>
            <Pressable
              onPress={() => { void copyDiagnostics(); }}
              hitSlop={12}
              accessibilityRole="button"
              accessibilityLabel="Copy diagnostics"
            >
              <Text style={st.logCopy}>{copied ? "Copied" : "Copy"}</Text>
            </Pressable>
          </View>
          <ScrollView style={{ maxHeight: 200 }}>
            <Text style={st.logBody}>{logText || "No events yet."}</Text>
          </ScrollView>
        </View>
      ) : null}

      {/* ---- Controls ---- */}
      {live || rec.state === "FINALIZING" ? (
        <View style={st.controlsRow}>
          <RoundControl
            onPress={recording ? pauseByUser : () => void resumeByUser()}
            disabled={controlsBusy}
            accessibilityLabel={recording ? "Pause recording" : "Resume recording"}
            bg={STUDIO.surfaceRaised}
            border={STUDIO.border}
            caption={recording ? "Pause" : "Resume"}
            captionColor={STUDIO.textDim}
          >
            <Icon name={recording ? "pause.fill" : "play.fill"} tintColor={STUDIO.text} size={22} />
          </RoundControl>

          {/* Stop is the dominant control: largest, and the only one in
              recording red. The square glyph is the universal "stop". */}
          <RoundControl
            onPress={stopAndSave}
            disabled={controlsBusy}
            accessibilityLabel="Stop and file the brief"
            size={80}
            bg={STUDIO.recording}
            caption="Stop"
            captionColor={STUDIO.text}
          >
            <View style={st.stopSquare} />
          </RoundControl>

          <RoundControl
            onPress={cancel}
            disabled={controlsBusy}
            accessibilityLabel="Discard recording"
            bg={STUDIO.surfaceRaised}
            border={STUDIO.border}
            caption="Discard"
            captionColor={STUDIO.textDim}
          >
            <Icon name="trash" tintColor={STUDIO.recording} size={20} />
          </RoundControl>
        </View>
      ) : denied ? (
        // Drawn locally against STUDIO rather than <Button variant="secondary">,
        // which outlines itself in the THEME's text colour — near-black in
        // light mode, and invisible on this ground.
        <Pressable
          onPress={rec.permission === "blocked" ? openAppSettings : () => void requestPermission()}
          accessibilityRole="button"
          style={({ pressed }) => [st.pillBtn, pressed && { opacity: 0.7 }]}
        >
          <Text style={st.pillBtnTxt}>
            {rec.permission === "blocked" ? "Open settings" : "Allow microphone"}
          </Text>
        </Pressable>
      ) : (
        <Pressable
          onPress={() => setSourceOpen(true)}
          accessibilityRole="button"
          accessibilityLabel="Choose a different recording source"
          style={({ pressed }) => [st.sourcePill, pressed && { opacity: 0.7 }]}
        >
          <Icon name="dot.radiowaves.left.and.right" tintColor={STUDIO.textDim} size={16} />
          <Text style={st.sourcePillTxt}>Select source</Text>
          <Icon name="chevron.right" tintColor={STUDIO.textFaint} size={14} />
        </Pressable>
      )}

      {/* ---- Source sheet (before recording) ---- */}
      <BottomSheet
        visible={sourceOpen}
        onClose={() => setSourceOpen(false)}
        title="Record from"
        dark={{ bg: STUDIO.surface, text: STUDIO.text, textDim: STUDIO.textDim, handle: STUDIO.border }}
      >
        <SheetAction
          label="This phone"
          description="Best on a table, in a quiet room"
          icon="mic.fill"
          selected
          onPress={() => setSourceOpen(false)}
          color={STUDIO.text}
          dimColor={STUDIO.textDim}
        />
        <SheetAction
          label="Your MinuteX device"
          description="Records on the device; handles rooms and corridors"
          icon="dot.radiowaves.left.and.right"
          onPress={() => switchSource("/record")}
          color={STUDIO.text}
          dimColor={STUDIO.textDim}
        />
        <SheetAction
          label="An audio file"
          description="wav, mp3, m4a, flac"
          icon="doc.text.fill"
          onPress={() => switchSource("/upload")}
          color={STUDIO.text}
          dimColor={STUDIO.textDim}
        />
      </BottomSheet>

      {/* ---- Microphone sheet (while recording, only with a real choice) ---- */}
      <BottomSheet
        visible={inputsOpen && inputs.length > 1}
        onClose={() => setInputsOpen(false)}
        title="Microphone"
        subtitle="Switching keeps the same recording going."
        dark={{ bg: STUDIO.surface, text: STUDIO.text, textDim: STUDIO.textDim, handle: STUDIO.border }}
      >
        {inputs.map((inp) => (
          <SheetAction
            key={inp.uid}
            label={inp.name}
            icon="mic.fill"
            selected={inp.name === rec.inputName}
            onPress={async () => { await selectInput(inp.uid); setInputsOpen(false); }}
            color={STUDIO.text}
            dimColor={STUDIO.textDim}
          />
        ))}
      </BottomSheet>
    </View>
  );
}

const st = StyleSheet.create({
  container: { flex: 1, backgroundColor: STUDIO.bg, paddingHorizontal: GUTTER },
  topRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    marginLeft: -S.md, gap: S.md,
  },
  // flexShrink so a long OS-supplied route name ("Jabra Evolve2 65 Hands-Free
  // AG Audio") yields instead of pushing the chip off the row.
  sourceChip: {
    flexDirection: "row", alignItems: "center", gap: S.sm, flexShrink: 1,
    borderWidth: 1, borderColor: STUDIO.border, borderRadius: R.pill,
    paddingHorizontal: S.md, minHeight: 36,
  },
  sourceDot: { width: 7, height: 7, borderRadius: 4 },
  sourceTxt: { fontFamily: FONT.medium, fontSize: 13, color: STUDIO.text, flexShrink: 1 },

  center: { flex: 1, justifyContent: "center" },

  readyBlock: { alignItems: "center" },
  readyTitle: {
    fontFamily: FONT.bold, fontSize: 26, lineHeight: 32, color: STUDIO.text,
    textAlign: "center", letterSpacing: -0.3,
  },
  readySub: {
    fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: STUDIO.textDim,
    textAlign: "center", marginTop: S.sm, maxWidth: 300,
  },
  // A still, concentric halo — the mic's resting state. It does not pulse:
  // nothing is being captured yet, and motion here would say otherwise.
  micHalo: {
    marginTop: S.x4, width: 176, height: 176, borderRadius: 88,
    borderWidth: 1, borderColor: STUDIO.border, backgroundColor: STUDIO.surface,
    alignItems: "center", justifyContent: "center",
  },
  micBtn: {
    width: 112, height: 112, borderRadius: 56, backgroundColor: STUDIO.primary,
    alignItems: "center", justifyContent: "center",
  },
  readyHint: { fontFamily: FONT.medium, fontSize: 14, color: STUDIO.textDim, marginTop: S.lg },

  liveBlock: { alignItems: "center", alignSelf: "stretch" },
  statusRow: { flexDirection: "row", alignItems: "center", gap: S.sm, minHeight: 24 },
  statusTxt: { fontFamily: FONT.semibold, fontSize: 14 },
  timer: {
    fontFamily: FONT.bold, fontSize: 64, lineHeight: 74, color: STUDIO.text,
    fontVariant: ["tabular-nums"], letterSpacing: -1, marginTop: S.md,
  },
  wave: { alignSelf: "stretch", marginTop: S.xl },
  state: {
    fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: STUDIO.textDim,
    textAlign: "center", marginTop: S.xl, maxWidth: 320,
  },

  error: {
    fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20,
    color: STUDIO.text, borderLeftWidth: 3, borderLeftColor: STUDIO.recording,
    paddingLeft: 13, marginBottom: S.md,
  },
  warnBand: {
    fontFamily: FONT.regular, fontSize: 13, lineHeight: 19,
    color: STUDIO.text, borderLeftWidth: 3, borderLeftColor: STUDIO.warn,
    paddingLeft: 13, marginBottom: S.md,
  },

  // Diagnostics panel — deliberately plain. It is a debugging surface, not
  // part of the product's visual language, and it should read as such.
  logPanel: {
    borderWidth: 1, borderColor: STUDIO.border, borderRadius: R.sm,
    padding: 10, marginBottom: S.md,
  },
  logHead: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginBottom: 6,
  },
  logTitle: { fontFamily: FONT.medium, fontSize: 11, color: STUDIO.textFaint, letterSpacing: 0.6, textTransform: "uppercase" },
  logCopy: { fontFamily: FONT.medium, fontSize: 12, color: STUDIO.ok },
  logBody: { fontFamily: FONT.mono, fontSize: 10, lineHeight: 15, color: STUDIO.textDim },

  controlsRow: {
    flexDirection: "row", alignItems: "flex-start", justifyContent: "space-evenly",
    paddingTop: S.md,
  },
  stopSquare: { width: 26, height: 26, borderRadius: 6, backgroundColor: "#FFFFFF" },

  sourcePill: {
    flexDirection: "row", alignItems: "center", gap: S.sm, alignSelf: "center",
    borderWidth: 1, borderColor: STUDIO.border, borderRadius: R.pill,
    paddingHorizontal: S.lg, minHeight: 48,
  },
  sourcePillTxt: { fontFamily: FONT.semibold, fontSize: 14, color: STUDIO.text },
  pillBtn: {
    borderWidth: 1, borderColor: STUDIO.text, borderRadius: R.pill,
    minHeight: 48, alignItems: "center", justifyContent: "center",
  },
  pillBtnTxt: { fontFamily: FONT.semibold, fontSize: 15, color: STUDIO.text },
});
