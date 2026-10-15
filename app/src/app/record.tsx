// src/app/record.tsx — the MinuteX device's remote (Start / Stop).
// Pause/Resume intentionally omitted for v1 (firmware has no pause). Recording
// state is driven by the REAL device status (two-way binding), not local guess.
// Recording happens on the hardware device — this screen is the remote.
//
// NO WAVEFORM, ON PURPOSE. The device captures to its own SD card and uploads
// over Wi-Fi; no audio reaches the phone, and the BLE status carries no input
// level. A waveform here could only be invented — which is exactly what this
// screen used to do (random bars). The live state is carried instead by the
// things the phone genuinely knows: the device says it is recording, and its
// battery and upload queue.
import { useEffect, useRef, useState } from "react";
import { StyleSheet, Text, View, Pressable } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import Animated, { FadeIn } from "react-native-reanimated";
import { Icon } from "../../lib/icons";
import { FONT, GUTTER, R, S, STUDIO } from "../../lib/theme";
import { MOTION, PulseDot, PressSpring } from "../../lib/motion";
import { IconButton, RoundControl } from "../../lib/kit";
import { useDevice } from "../../lib/device-context";

function fmt(sec: number) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export default function RecordScreen() {
  const router = useRouter();
  // Always on the STUDIO ground (lib/theme.tsx) — no theme colours.
  const insets = useSafeAreaInsets();
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

  // Timer driven by real recording state. It counts from when THIS screen saw
  // the device start, which is what the firmware exposes — the device reports
  // "recording", not "recording since".
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
  const battery = status?.batteryLevel;
  const chipLabel =
    device && status
      ? `${device.name ?? "Device"} · ${battery ?? "--"}%`
      : device ? (connState === "connecting" || connState === "reconnecting" ? "Connecting…" : "Not connected")
        : "No device";

  return (
    <View style={[st.container, { paddingTop: insets.top + S.sm, paddingBottom: Math.max(insets.bottom, S.lg) + S.lg }]}>
      <View style={st.topRow}>
        <IconButton icon="xmark" onPress={() => router.back()} accessibilityLabel="Close" tint={STUDIO.textDim} />
        <View style={st.sourceChip} accessibilityLabel={`Device: ${chipLabel}`}>
          <View style={[st.sourceDot, {
            backgroundColor: noDevice ? STUDIO.textFaint : isRecording ? STUDIO.recording : STUDIO.ok,
          }]} />
          <Text style={st.sourceTxt} numberOfLines={1}>{chipLabel}</Text>
        </View>
      </View>

      <View style={st.center}>
        {isRecording ? (
          <Animated.View key="live" entering={FadeIn.duration(MOTION.standard)} style={{ alignItems: "center" }}>
            <View style={st.statusRow} accessibilityLiveRegion="polite" accessibilityLabel="Status: Recording on your device">
              <PulseDot active color={STUDIO.recording} size={9} />
              <Text style={[st.statusTxt, { color: STUDIO.recording }]}>Recording on your device</Text>
            </View>
            <Text style={st.timer} accessibilityLabel={`Elapsed ${fmt(seconds)}`}>{fmt(seconds)}</Text>
            <Text style={st.state}>
              The device is capturing and will upload over Wi-Fi.
              We&apos;ll write up the brief once it lands.
            </Text>
          </Animated.View>
        ) : (
          <Animated.View key="ready" entering={FadeIn.duration(MOTION.standard)} style={{ alignItems: "center" }}>
            <Text style={st.readyTitle} accessibilityRole="header">
              {noDevice ? "Device not connected" : "Ready when you are"}
            </Text>
            <Text style={st.readySub}>
              {noDevice
                ? "Switch your MinuteX device on and keep it nearby, or pair one."
                : "Start recording on your device. You'll get a brief when it's done."}
            </Text>
            <View style={st.micHalo}>
              <PressSpring
                onPress={onToggle}
                disabled={busy || noDevice}
                accessibilityLabel="Start recording on your device"
                pressedScale={MOTION.pressScale}
                style={[st.micBtn, (busy || noDevice) && { opacity: 0.4 }]}
              >
                <Icon name="mic.fill" tintColor="#FFFFFF" size={34} />
              </PressSpring>
            </View>
            <Text style={st.readyHint}>{noDevice ? "Not available" : busy ? "Starting…" : "Tap to start"}</Text>
          </Animated.View>
        )}

        {/* Device vitals — battery and anything still waiting to upload. Real
            values from the BLE status, shown only when the device reports them. */}
        {status ? (
          <View style={st.vitals}>
            <View style={st.vitalRow}>
              <Text style={st.vitalName}>Battery</Text>
              <View style={st.vitalTrack}>
                <View style={{
                  width: `${Math.max(0, Math.min(100, battery ?? 0))}%`,
                  height: "100%",
                  backgroundColor: (battery ?? 100) <= 20 ? STUDIO.recording : STUDIO.ok,
                }} />
              </View>
              <Text style={st.vitalValue}>{battery ?? "—"}%</Text>
            </View>
            <View style={st.vitalRow}>
              <Text style={st.vitalName}>Wi-Fi</Text>
              <Text style={[st.vitalValue, { flex: 1 }]}>
                {status.wifiState === "connected" ? "Connected" : "Offline — uploads wait for Wi-Fi"}
              </Text>
            </View>
            {(status.pendingUploads ?? 0) > 0 ? (
              <View style={st.vitalRow}>
                <Text style={st.vitalName}>Waiting</Text>
                <Text style={[st.vitalValue, { flex: 1, color: STUDIO.warn }]}>
                  {status.pendingUploads} to upload
                </Text>
              </View>
            ) : null}
          </View>
        ) : null}
      </View>

      {error ? <Text style={st.error}>{error}</Text> : null}

      {isRecording ? (
        <View style={{ alignItems: "center" }}>
          <RoundControl
            onPress={onToggle}
            disabled={busy}
            accessibilityLabel="Stop recording on your device"
            size={80}
            bg={STUDIO.recording}
            caption={busy ? "Stopping…" : "Stop"}
            captionColor={STUDIO.text}
          >
            <View style={st.stopSquare} />
          </RoundControl>
        </View>
      ) : noDevice ? (
        // Drawn locally rather than <Button variant="secondary">, which
        // outlines itself in the theme's text colour — invisible on STUDIO.
        <Pressable
          onPress={() => router.push("/pair")}
          accessibilityRole="button"
          style={({ pressed }) => [st.pillBtn, pressed && { opacity: 0.7 }]}
        >
          <Text style={st.pillBtnTxt}>Pair your device</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

const st = StyleSheet.create({
  container: { flex: 1, backgroundColor: STUDIO.bg, paddingHorizontal: GUTTER },
  topRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    marginLeft: -S.md, gap: S.md,
  },
  sourceChip: {
    flexDirection: "row", alignItems: "center", gap: S.sm, flexShrink: 1,
    borderWidth: 1, borderColor: STUDIO.border, borderRadius: R.pill,
    paddingHorizontal: S.md, minHeight: 36,
  },
  sourceDot: { width: 7, height: 7, borderRadius: 4 },
  sourceTxt: { fontFamily: FONT.medium, fontSize: 13, color: STUDIO.text, flexShrink: 1 },

  center: { flex: 1, justifyContent: "center", gap: S.xxl },

  readyTitle: {
    fontFamily: FONT.bold, fontSize: 26, lineHeight: 32, color: STUDIO.text,
    textAlign: "center", letterSpacing: -0.3,
  },
  readySub: {
    fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: STUDIO.textDim,
    textAlign: "center", marginTop: S.sm, maxWidth: 300,
  },
  micHalo: {
    marginTop: S.x3, width: 176, height: 176, borderRadius: 88,
    borderWidth: 1, borderColor: STUDIO.border, backgroundColor: STUDIO.surface,
    alignItems: "center", justifyContent: "center",
  },
  micBtn: {
    width: 112, height: 112, borderRadius: 56, backgroundColor: STUDIO.primary,
    alignItems: "center", justifyContent: "center",
  },
  readyHint: { fontFamily: FONT.medium, fontSize: 14, color: STUDIO.textDim, marginTop: S.lg },

  statusRow: { flexDirection: "row", alignItems: "center", gap: S.sm },
  statusTxt: { fontFamily: FONT.semibold, fontSize: 14 },
  timer: {
    fontFamily: FONT.bold, fontSize: 64, lineHeight: 74, color: STUDIO.text,
    fontVariant: ["tabular-nums"], letterSpacing: -1, marginTop: S.md,
  },
  state: {
    fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: STUDIO.textDim,
    textAlign: "center", marginTop: S.lg, maxWidth: 320,
  },
  error: {
    fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20,
    color: STUDIO.text, borderLeftWidth: 3, borderLeftColor: STUDIO.recording,
    paddingLeft: 13, marginBottom: S.md,
  },

  vitals: {
    alignSelf: "stretch", backgroundColor: STUDIO.surface, borderRadius: R.card,
    borderWidth: 1, borderColor: STUDIO.border, padding: S.lg, gap: S.md,
  },
  vitalRow: { flexDirection: "row", alignItems: "center", gap: S.md },
  vitalName: { fontFamily: FONT.medium, fontSize: 13, color: STUDIO.textDim, width: 64 },
  vitalTrack: { flex: 1, height: 6, backgroundColor: STUDIO.surfaceRaised, borderRadius: R.pill, overflow: "hidden" },
  vitalValue: { fontFamily: FONT.medium, fontSize: 13, color: STUDIO.text, fontVariant: ["tabular-nums"] },

  stopSquare: { width: 26, height: 26, borderRadius: 6, backgroundColor: "#FFFFFF" },
  pillBtn: {
    borderWidth: 1, borderColor: STUDIO.text, borderRadius: R.pill,
    minHeight: 48, alignItems: "center", justifyContent: "center",
  },
  pillBtnTxt: { fontFamily: FONT.semibold, fontSize: 15, color: STUDIO.text },
});
