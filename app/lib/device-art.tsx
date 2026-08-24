// lib/device-art.tsx — code-drawn portrait of the MinuteX device.
//
// The device is a small dark object, drawn flat: an ink body with an 8px
// radius, a status LED, a mini waveform where the mic sits, and the button
// ring at the bottom. The one shadow it keeps is the soft ground contact that
// makes it read as an object resting on the page. No image assets, so it
// stays crisp at any size.
import { View } from "react-native";
import { useTheme } from "./theme";

// Deterministic bar heights for the mini waveform — a fixed pattern, not
// random, so the device looks identical on every render.
const MINI = [5, 9, 14, 8, 17, 11, 20, 13, 22, 12, 18, 9, 13, 6];

export function DeviceArt({
  size = 150, live = false, pairing = false,
}: {
  size?: number;
  /** LED green (connected) vs dim (offline) */
  live?: boolean;
  /** LED amber — mid-pairing. Amber outranks green. */
  pairing?: boolean;
}) {
  const { C } = useTheme();
  const w = size * 0.46;
  const h = size * 0.95;
  // The body is always ink so the device looks like itself in both themes; its
  // details are drawn in the dim/faint tones from the dark scale.
  const BODY = "#16130F";
  const DETAIL = "#6B6459";
  const led = pairing ? "#F5A623" : live ? "#3DCB93" : DETAIL;

  return (
    <View style={{ width: size * 0.8, height: h + 16, alignItems: "center", justifyContent: "center" }}>
      <View
        style={{
          width: w, height: h, borderRadius: 8, backgroundColor: BODY,
          alignItems: "center", justifyContent: "space-between",
          paddingVertical: h * 0.11,
          // Contact shadow only — no coloured glow.
          shadowColor: "#16130F", shadowOpacity: 0.22,
          shadowRadius: 26, shadowOffset: { width: 0, height: 12 },
          elevation: 8,
        }}
      >
        {/* Status LED */}
        <View style={{
          width: 6, height: 6, borderRadius: 3, backgroundColor: led,
          shadowColor: led, shadowOpacity: pairing || live ? 0.9 : 0,
          shadowRadius: 8, shadowOffset: { width: 0, height: 0 },
        }} />

        {/* Mic — a mini waveform, the same mark the app uses everywhere */}
        <View style={{ flexDirection: "row", alignItems: "center", gap: 2, height: h * 0.18 }}>
          {MINI.map((bar, i) => (
            <View
              key={i}
              style={{
                width: 2,
                height: Math.max(2, (bar / 22) * h * 0.18),
                backgroundColor: DETAIL,
              }}
            />
          ))}
        </View>

        {/* Button ring */}
        <View style={{
          width: w * 0.29, height: w * 0.29, borderRadius: w * 0.145,
          borderWidth: 1.5, borderColor: DETAIL,
        }} />
      </View>

      {/* Ground contact */}
      <View style={{
        position: "absolute", bottom: 0, width: w * 1.05, height: 8,
        borderRadius: 4, backgroundColor: C.text, opacity: 0.06,
      }} />
    </View>
  );
}

