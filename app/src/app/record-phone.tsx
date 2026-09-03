// src/app/record-phone.tsx — in-app phone recording (source: MOBILE).
// The phone-microphone sibling of record.tsx (which remote-controls the
// hardware). Record / Pause / Resume / Stop / Cancel with a live timer.
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
// Stop finalizes the audio into the app's document directory and hands the
// session to the shared UploadManager, which uploads it when there is a
// network and keeps it safe when there isn't. The user lands back on Files,
// where the upload banner tracks progress.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Platform, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Icon } from "../../lib/icons";
import { S, R, CAPS, FONT, TABULAR } from "../../lib/theme";
import { LiveWaveform } from "../../lib/waveform";
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

function fmt(sec: number) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// "On the record" is the one screen that is always ink, in either theme — it
// is a state, not a surface, and the dark ground is what makes it feel live.
// INK/PAPER are the DARK scale's values, inlined so this screen doesn't flip
// to paper in light mode.
const INK = {
  bg: "#16130F", text: "#F7F5F0", dim: "#C9C2B4",
  faint: "#9C9488", rule: "#3A342B", rec: "#E5484D", ok: "#7FBFA8",
  warn: "#E8B44A",
};

function buildStyles() {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: INK.bg, paddingHorizontal: 26 },
    close: { padding: 4 },
    topRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const,
    },
    // Source chip: a soft pill, matching the rounded chip treatment.
    sourceChip: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 7,
      borderWidth: 1, borderColor: INK.rule, borderRadius: R.pill,
      paddingHorizontal: 12, paddingVertical: 6,
    },
    sourceDot: { width: 6, height: 6, borderRadius: 3 },
    sourceTxt: { fontFamily: FONT.semibold, fontSize: 10.5, color: INK.dim, letterSpacing: 1 },

    center: { flex: 1, alignItems: "center" as const, justifyContent: "center" as const, gap: 22 },
    kicker: { ...CAPS, fontSize: 10.5, letterSpacing: 2.6, color: INK.faint },
    // The timer is the AI-facing numeral: extrabold sans, not a thin weight.
    // TABULAR carries fontFamily (mono) — spread it FIRST so extrabold wins.
    timer: {
      ...TABULAR,
      fontFamily: FONT.extrabold, fontSize: 84, lineHeight: 88,
      color: INK.text, letterSpacing: -1,
    },
    state: { fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: INK.dim, textAlign: "center" as const },
    error: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20,
      color: INK.text, borderLeftWidth: 3, borderLeftColor: INK.rec,
      paddingLeft: 13, marginBottom: S.md,
    },
    warnBand: {
      fontFamily: FONT.regular, fontSize: 13, lineHeight: 19,
      color: INK.text, borderLeftWidth: 3, borderLeftColor: INK.warn,
      paddingLeft: 13, marginBottom: S.md,
    },

    // Diagnostics panel — deliberately plain. It is a debugging surface, not
    // part of the product's visual language, and it should read as such.
    logPanel: {
      borderWidth: 1, borderColor: INK.rule, borderRadius: R.sm,
      padding: 10, marginBottom: S.md,
    },
    logHead: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const, marginBottom: 6,
    },
    logTitle: {
      fontFamily: FONT.medium, fontSize: 11, color: INK.faint,
      letterSpacing: CAPS.letterSpacing, textTransform: "uppercase" as const,
    },
    logCopy: { fontFamily: FONT.medium, fontSize: 12, color: INK.ok },
    logBody: {
      fontFamily: FONT.mono, fontSize: 10, lineHeight: 15, color: INK.dim,
    },

    // The live status band. Distinct from `state` prose: this is the one line
    // that must never be ambiguous about whether audio is being captured, so
    // it gets a dot whose colour IS the answer.
    statusRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 9,
      alignSelf: "center" as const,
      borderWidth: 1, borderRadius: R.pill,
      paddingHorizontal: 14, paddingVertical: 7,
    },
    statusDot: { width: 8, height: 8, borderRadius: 4 },
    statusTxt: { fontFamily: FONT.semibold, fontSize: 12.5 },

    routeRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "center" as const, gap: 6,
    },
    routeTxt: { fontFamily: FONT.regular, fontSize: 11.5, color: INK.faint },

    controls: { alignItems: "center" as const, gap: 12 },
    controlsRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "center" as const, gap: S.xxl,
    },
    // Secondary controls: outlined circles on ink, no fill, no shadow.
    sideBtn: {
      width: 52, height: 52, borderRadius: 26,
      borderWidth: 1, borderColor: INK.rule,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    sideBtnLabel: { ...CAPS, fontSize: 9.5, letterSpacing: 1.4, color: INK.faint, marginTop: 6, textAlign: "center" as const },
    sideWrap: { alignItems: "center" as const, width: 72 },
    // The record control: a thin ring holding a red square/disc.
    recBtn: {
      width: 104, height: 104, borderRadius: 52,
      borderWidth: 1, borderColor: INK.rule,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    recRing: {
      position: "absolute" as const, top: -1, left: -1, right: -1, bottom: -1,
      borderRadius: 52, borderWidth: 1, borderColor: INK.rec,
    },
    recDot: { width: 60, height: 60, borderRadius: 30, backgroundColor: INK.rec },
    stopSquare: { width: 44, height: 44, borderRadius: R.sm, backgroundColor: INK.rec },
    hint: { ...CAPS, fontSize: 10.5, letterSpacing: 2.2, color: INK.faint },

    inputSheet: {
      borderTopWidth: 1, borderTopColor: INK.rule, marginTop: S.md, paddingTop: S.md,
    },
    inputRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      paddingVertical: 12,
    },
    inputName: { fontFamily: FONT.regular, fontSize: 13.5, color: INK.text, flex: 1 },
  });
}

/**
 * The one line the user must be able to trust. Colour carries the meaning:
 * red = capturing, amber = paused for a reason outside the user's control,
 * dim = the user's own pause, green = saved.
 */
function statusChrome(state: string): { color: string; label: string } {
  switch (state) {
    case "RECORDING": return { color: INK.rec, label: "Recording" };
    case "PAUSED_BY_USER": return { color: INK.faint, label: "Paused" };
    case "PAUSED_BY_CALL": return { color: INK.warn, label: "Paused because of a call" };
    case "PAUSED_BY_AUDIO_INTERRUPTION": return { color: INK.warn, label: "Paused — audio interrupted" };
    case "PAUSED_BY_MICROPHONE": return { color: INK.warn, label: "Microphone unavailable" };
    case "FINALIZING": return { color: INK.dim, label: "Saving…" };
    case "COMPLETED": return { color: INK.ok, label: "Saved" };
    case "ERROR": return { color: INK.rec, label: "Couldn't record" };
    default: return { color: INK.faint, label: "Ready" };
  }
}

export default function RecordPhoneScreen() {
  const router = useRouter();
  // Set when Record was opened from inside a folder (see the folder detail
  // screen's record button). Threaded to startUpload so the recording is filed
  // at presign time rather than landing in General and being moved after.
  const { folderId: folderParam } = useLocalSearchParams<{ folderId?: string }>();
  const folderId = String(folderParam || "");
  // This screen is always ink (see INK above), so it takes no colours from
  // the theme — only the safe-area insets and the controller's snapshot.
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(), []);

  const rec = useRecorder();
  const [showInputs, setShowInputs] = useState(false);
  const [showLog, setShowLog] = useState(false);
  const [logText, setLogText] = useState("");
  const [copied, setCopied] = useState(false);

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
  // event at all, so focus is the only reliable moment to notice.
  useFocusEffect(
    useCallback(() => {
      void refreshPermission();
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
  const paused = isPaused(rec.state as any);
  const live = recording || paused;
  const denied = rec.permission === "denied" || rec.permission === "blocked";
  const chrome = statusChrome(rec.state);

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
  // retries and offline waiting, then return to Files where the banner takes
  // over. The recording is on disk in the document directory before this
  // function returns, so navigating away cannot lose it.
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
        // Set when the user hit Record from inside a folder — the recording is
        // filed at presign time, so it never appears in General first.
        folderId: folderId || undefined,
      }).catch(() => { /* surfaced by the Files-screen upload banner */ });
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

  const inputs = live ? listInputs() : [];

  return (
    <View style={[st.container, { paddingTop: insets.top + S.lg, paddingBottom: Math.max(insets.bottom, S.lg) + S.xl }]}>
      <View style={st.topRow}>
        <Pressable style={st.close} onPress={cancel} accessibilityLabel="Close" hitSlop={8}>
          <Icon name="chevron.down" tintColor={INK.faint} size={24} />
        </Pressable>
        <View style={st.sourceChip}>
          <View style={[st.sourceDot, { backgroundColor: denied ? INK.rec : INK.ok }]} />
          <Text style={st.sourceTxt}>THIS PHONE</Text>
        </View>
      </View>

      <View style={st.center}>
        <Text style={st.kicker}>
          {recording ? "On the record" : paused ? "Held" : "Ready when you are"}
        </Text>
        <Pressable
          onLongPress={() => setShowLog((v) => !v)}
          delayLongPress={700}
          accessibilityLabel="Show recording diagnostics"
        >
          <Text style={st.timer}>{fmt(rec.seconds)}</Text>
        </Pressable>

        {/* The unambiguous answer to "is it recording?" — always shown once a
            session exists, in every state, including the ones the user didn't
            ask for. */}
        {rec.state !== "IDLE" ? (
          <View
            style={[st.statusRow, { borderColor: chrome.color }]}
            accessibilityLabel={`Status: ${chrome.label}`}
            accessibilityLiveRegion="polite"
          >
            <View style={[st.statusDot, { backgroundColor: chrome.color }]} />
            <Text style={[st.statusTxt, { color: chrome.color }]}>{chrome.label}</Text>
          </View>
        ) : null}

        <LiveWaveform
          // Only animate while audio is genuinely being captured. A moving
          // waveform over a paused recorder is the exact lie this audit set
          // out to remove.
          active={recording}
          bars={46}
          height={80}
          color={recording ? INK.rec : INK.rule}
          style={{ alignSelf: "stretch" }}
        />

        <Text style={st.state}>
          {denied ? "Microphone access is required to record."
            : recording ? "Keep going — locking the screen or switching apps won't stop it."
            : rec.state === "PAUSED_BY_CALL" ? "We'll pick up again automatically when the call ends."
            : rec.state === "PAUSED_BY_AUDIO_INTERRUPTION" ? "Another app took the microphone. We'll resume when it's free."
            : rec.state === "PAUSED_BY_MICROPHONE" ? "The microphone isn't available right now. Everything recorded so far is safe."
            : rec.state === "PAUSED_BY_USER" ? "Held. Resume, or stop to file the brief."
            : rec.state === "COMPLETED" ? "Saved. It'll upload on its own."
            : "Tap to start. You'll get a one-page brief when it's done."}
        </Text>

        {/* Which microphone is live. Shown while recording because a silent
            switch to a Bluetooth headset that then disconnects is the classic
            way to end up with an empty recording. */}
        {live && rec.inputName ? (
          <Pressable
            style={st.routeRow}
            onPress={() => setShowInputs((v) => !v)}
            accessibilityLabel="Change microphone"
          >
            <Icon name="mic.fill" tintColor={INK.faint} size={12} />
            <Text style={st.routeTxt}>{rec.inputName}</Text>
            {inputs.length > 1 ? (
              <Icon name="chevron.right" tintColor={INK.faint} size={12} />
            ) : null}
          </Pressable>
        ) : null}
      </View>

      {rec.lowStorage ? (
        <Text style={st.warnBand}>
          Storage is running low. The recording will be saved automatically if
          space runs out.
        </Text>
      ) : null}

      {rec.error ? (
        <Pressable onPress={clearError} accessibilityLabel="Dismiss error">
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

      {/* Input picker — only when there's a real choice to make. */}
      {showInputs && inputs.length > 1 ? (
        <ScrollView style={{ maxHeight: 180 }} contentContainerStyle={st.inputSheet}>
          {inputs.map((inp) => (
            <Pressable
              key={inp.uid}
              style={({ pressed }) => [st.inputRow, pressed && { opacity: 0.6 }]}
              onPress={async () => { await selectInput(inp.uid); setShowInputs(false); }}
              accessibilityLabel={`Use ${inp.name}`}
            >
              <Icon
                name={inp.name === rec.inputName ? "checkmark" : "mic.fill"}
                tintColor={inp.name === rec.inputName ? INK.ok : INK.faint}
                size={15}
              />
              <Text style={st.inputName}>{inp.name}</Text>
            </Pressable>
          ))}
        </ScrollView>
      ) : null}

      <View style={st.controls}>
        <View style={st.controlsRow}>
          {/* Pause / Resume — only meaningful mid-recording */}
          <View style={st.sideWrap}>
            {live ? (
              <>
                <Pressable
                  onPress={recording ? pauseByUser : () => void resumeByUser()}
                  disabled={rec.busy}
                  style={({ pressed }) => [
                    st.sideBtn,
                    rec.busy && { opacity: 0.4 },
                    pressed && { opacity: 0.6 },
                  ]}
                  accessibilityLabel={recording ? "Pause recording" : "Resume recording"}
                >
                  <Icon name={recording ? "pause.fill" : "play.fill"} tintColor={INK.text} size={20} />
                </Pressable>
                <Text style={st.sideBtnLabel}>{recording ? "Hold" : "Resume"}</Text>
              </>
            ) : null}
          </View>

          {/* Record / Stop */}
          <Pressable
            onPress={live ? stopAndSave : begin}
            disabled={denied || rec.busy || rec.state === "FINALIZING"}
            style={({ pressed }) => [
              st.recBtn,
              (denied || rec.busy || rec.state === "FINALIZING") && { opacity: 0.4 },
              pressed && { opacity: 0.7 },
            ]}
            accessibilityLabel={live ? "Stop and file the brief" : "Start recording"}
          >
            {/* A second ring, drawn in recording-red, marks the live state. */}
            {recording ? <View style={st.recRing} /> : null}
            <View style={live ? st.stopSquare : st.recDot} />
          </Pressable>

          {/* Cancel — discard without uploading */}
          <View style={st.sideWrap}>
            {live ? (
              <>
                <Pressable
                  onPress={cancel}
                  style={({ pressed }) => [st.sideBtn, pressed && { opacity: 0.6 }]}
                  accessibilityLabel="Discard recording"
                >
                  <Icon name="trash" tintColor={INK.rec} size={19} />
                </Pressable>
                <Text style={st.sideBtnLabel}>Discard</Text>
              </>
            ) : null}
          </View>
        </View>
        <Text style={st.hint}>
          {live ? "Stop & file the brief" : "Tap to record"}
        </Text>
      </View>

      {/* Not <Button variant="secondary"> — that outlines itself in C.text,
          which is near-black in light mode and would vanish on this ink
          ground. Drawn locally against the INK palette instead. */}
      {denied ? (
        <Pressable
          onPress={rec.permission === "blocked" ? openAppSettings : () => void requestPermission()}
          style={({ pressed }) => [{
            borderWidth: 1, borderColor: INK.text, borderRadius: R.pill,
            paddingVertical: 14, alignItems: "center", marginTop: S.lg,
          }, pressed && { opacity: 0.7 }]}
        >
          <Text style={{ fontFamily: FONT.bold, fontSize: 14, color: INK.text }}>
            {rec.permission === "blocked" ? "Open settings" : "Allow microphone"}
          </Text>
        </Pressable>
      ) : null}
    </View>
  );
}
