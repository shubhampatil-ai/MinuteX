// src/app/record.tsx — full-screen recording interface (Start / Stop).
// Pause/Resume intentionally omitted for v1 (firmware has no pause). Recording
// state is driven by the REAL device status (two-way binding), not local guess.
// Recording happens on the hardware device — this screen is the remote.
import { useEffect, useMemo, useRef, useState } from "react";
import { Animated, Easing, Pressable, StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Icon } from "../../lib/icons";
import { S, R, CAPS, FONT, TABULAR } from "../../lib/theme";
import { LiveWaveform } from "../../lib/waveform";
import { useDevice } from "../../lib/device-context";

function fmt(sec: number) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// "On the record" is always ink, in either theme — it is a state, not a
// surface, and the dark ground is what makes it feel live. Mirrors the INK
// constants in record-phone.tsx.
const INK = {
  bg: "#16130F", text: "#F7F5F0", dim: "#C9C2B4",
  faint: "#9C9488", rule: "#3A342B", rec: "#E5484D", ok: "#7FBFA8", warn: "#E08B4F",
};

function buildStyles() {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: INK.bg, paddingHorizontal: 26 },
    close: { padding: 4 },
    topRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const,
    },
    // Device chip: a soft pill carrying the device's vitals in one line.
    sourceChip: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 7,
      borderWidth: 1, borderColor: INK.rule, borderRadius: R.pill,
      paddingHorizontal: 12, paddingVertical: 6,
    },
    sourceDot: { width: 6, height: 6, borderRadius: 3 },
    sourceTxt: { fontFamily: FONT.semibold, fontSize: 10.5, color: INK.dim, letterSpacing: 1 },

    center: { flex: 1, alignItems: "center" as const, justifyContent: "center" as const, gap: 22 },
    kicker: { ...CAPS, fontSize: 10.5, letterSpacing: 2.6, color: INK.faint },
    // TABULAR carries fontFamily (mono) — spread FIRST so extrabold wins.
    timer: {
      ...TABULAR,
      fontFamily: FONT.extrabold, fontSize: 84, lineHeight: 88,
      color: INK.text, letterSpacing: -1,
    },
    state: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20,
      color: INK.dim, textAlign: "center" as const,
    },
    error: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20,
      color: INK.text, borderLeftWidth: 3, borderLeftColor: INK.rec,
      paddingLeft: 13, marginBottom: S.md,
    },

    // Vitals panel: label / bar / value rows above a hairline.
    vitals: { alignSelf: "stretch" as const, borderTopWidth: 1, borderTopColor: INK.rule, paddingTop: 16 },
    vitalsLabel: { ...CAPS, fontSize: 10, letterSpacing: 2, color: INK.faint, marginBottom: 9 },
    vitalRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 10, marginBottom: 8 },
    vitalName: { fontFamily: FONT.semibold, fontSize: 12, color: INK.text, width: 62 },
    vitalTrack: { flex: 1, height: 6, backgroundColor: "#26221C", borderRadius: R.pill, overflow: "hidden" as const },
    vitalValue: { ...TABULAR, fontSize: 11, color: INK.faint },

    controls: { alignItems: "center" as const, gap: 12 },
    ringWrap: { alignItems: "center" as const, justifyContent: "center" as const },
    pulseRing: {
      position: "absolute" as const, width: 104, height: 104, borderRadius: 52,
      borderWidth: 1, borderColor: INK.rec,
    },
    recBtn: {
      width: 104, height: 104, borderRadius: 52,
      borderWidth: 1, borderColor: INK.rule,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    recDot: { width: 60, height: 60, borderRadius: 30, backgroundColor: INK.rec },
    stopSquare: { width: 44, height: 44, borderRadius: R.sm, backgroundColor: INK.rec },
    hint: { ...CAPS, fontSize: 10.5, letterSpacing: 2.2, color: INK.faint },
  });
}

// One expanding pulse ring — loops scale 1→1.7 while fading out.
function PulseRing({ active, delay, st }: { active: boolean; delay: number; st: ReturnType<typeof buildStyles> }) {
  const v = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    if (!active) { v.setValue(0); return; }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.delay(delay),
        Animated.timing(v, { toValue: 1, duration: 1600, easing: Easing.out(Easing.ease), useNativeDriver: true }),
      ])
    );
    loop.start();
    return () => loop.stop();
  }, [active, v, delay]);
  if (!active) return null;
  const scale = v.interpolate({ inputRange: [0, 1], outputRange: [1, 1.75] });
  const opacity = v.interpolate({ inputRange: [0, 0.15, 1], outputRange: [0, 0.5, 0] });
  return <Animated.View style={[st.pulseRing, { opacity, transform: [{ scale }] }]} />;
}

export default function RecordScreen() {
  const router = useRouter();
  // Always ink (see INK above) — this screen takes no colours from the theme.
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(), []);
  const { device, status, connState, start, stop } = useDevice();
  // Only trust `status` while actually connected — during a reconnect the
  // poll is paused (see device-context) but `status` itself still holds its
  // last known value, which can be stale ("recording" from before a drop).
  const isRecording = connState === "connected" && status?.isRecording === true;
  const [seconds, setSeconds] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Synchronous re-entrancy guard. `busy` (state) is checked by the button's
  // `disabled` prop, but state updates are async — a fast double-tap (or
  // Android's touch/press event sometimes firing onPress twice) can call
  // onToggle again before the re-render with disabled=true lands, sending the
  // BLE command multiple times. A ref updates immediately, so it blocks the
  // second call within the same tick.
  const inFlightRef = useRef(false);

  // Timer driven by real recording state.
  useEffect(() => {
    if (!isRecording) { setSeconds(0); return; }
    const t = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [isRecording]);

  const onToggle = async () => {
    if (inFlightRef.current) return;   // block re-entrant taps immediately
    if (!device) { setError("No device connected. Pair a device first."); return; }
    inFlightRef.current = true;
    setError(""); setBusy(true);
    try {
      if (isRecording) await stop();
      else await start();
    } catch (e: any) {
      setError((isRecording ? "Stop" : "Start") + " failed: " + (e?.message ?? "unknown"));
    } finally {
      inFlightRef.current = false;
      setBusy(false);
    }
  };

  const noDevice = !device || connState !== "connected";

  return (
    <View style={[st.container, { paddingTop: insets.top + S.lg, paddingBottom: Math.max(insets.bottom, S.lg) + S.xl }]}>
      <View style={st.topRow}>
        <Pressable style={st.close} onPress={() => router.back()} accessibilityLabel="Close" hitSlop={8}>
          <Icon name="chevron.down" tintColor={INK.faint} size={24} />
        </Pressable>
        <View style={st.sourceChip}>
          <View style={[st.sourceDot, {
            backgroundColor: noDevice ? INK.faint : isRecording ? INK.rec : INK.ok,
          }]} />
          <Text style={st.sourceTxt}>
            {device && status
              ? `DEVICE · ${status.batteryLevel ?? "--"}% · ${status.wifiState === "connected" ? "WI-FI" : "OFFLINE"}`
              : device ? String(connState).toUpperCase() : "NO DEVICE"}
          </Text>
        </View>
      </View>

      {/* Timer + live waveform */}
      <View style={st.center}>
        <Text style={st.kicker}>
          {isRecording ? "On the record" : noDevice ? "Not connected" : "Ready when you are"}
        </Text>
        <Text style={st.timer}>{fmt(seconds)}</Text>
        <LiveWaveform
          active={isRecording}
          bars={46}
          height={80}
          color={isRecording ? INK.rec : INK.rule}
          style={{ alignSelf: "stretch" }}
        />
        <Text style={st.state}>
          {isRecording ? "Your device is capturing — we'll write it up after." :
           noDevice ? "Connect your device to begin." : "Tap to start. You'll get a one-page brief when it's done."}
        </Text>

        {/* Device vitals — battery and anything still waiting to upload */}
        {status ? (
          <View style={st.vitals}>
            <Text style={st.vitalsLabel}>Your device</Text>
            <View style={st.vitalRow}>
              <Text style={st.vitalName}>Battery</Text>
              <View style={st.vitalTrack}>
                <View style={{
                  width: `${Math.max(0, Math.min(100, status.batteryLevel ?? 0))}%`,
                  height: "100%",
                  backgroundColor: (status.batteryLevel ?? 100) <= 20 ? INK.rec : INK.ok,
                }} />
              </View>
              <Text style={st.vitalValue}>{status.batteryLevel ?? "—"}%</Text>
            </View>
            {(status.pendingUploads ?? 0) > 0 ? (
              <View style={st.vitalRow}>
                <Text style={st.vitalName}>Waiting</Text>
                <View style={st.vitalTrack} />
                <Text style={[st.vitalValue, { color: INK.warn }]}>
                  {status.pendingUploads} to upload
                </Text>
              </View>
            ) : null}
          </View>
        ) : null}
      </View>

      {error ? <Text style={st.error}>{error}</Text> : null}

      {/* Record / Stop button with radiating pulse */}
      <View style={st.controls}>
        <View style={st.ringWrap}>
          <PulseRing active={isRecording} delay={0} st={st} />
          <PulseRing active={isRecording} delay={550} st={st} />
          <Pressable
            onPress={onToggle}
            disabled={busy || noDevice}
            style={({ pressed }) => [
              st.recBtn,
              (busy || noDevice) && { opacity: 0.4 },
              pressed && { opacity: 0.7 },
            ]}
            accessibilityLabel={isRecording ? "Stop recording" : "Start recording"}
          >
            <View style={isRecording ? st.stopSquare : st.recDot} />
          </Pressable>
        </View>
        <Text style={st.hint}>{isRecording ? "Stop & file the brief" : "Tap to record"}</Text>
      </View>

      {/* Drawn locally rather than <Button variant="secondary">, which
          outlines itself in C.text — near-black, invisible on this ink. */}
      {noDevice ? (
        <Pressable
          onPress={() => router.push("/pair")}
          style={({ pressed }) => [{
            borderWidth: 1, borderColor: INK.text, borderRadius: R.pill,
            paddingVertical: 14, alignItems: "center", marginTop: S.lg,
          }, pressed && { opacity: 0.7 }]}
        >
          <Text style={{ fontFamily: FONT.bold, fontSize: 14, color: INK.text }}>
            Pair your device
          </Text>
        </Pressable>
      ) : null}
    </View>
  );
}
