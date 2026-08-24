// lib/waveform.tsx — MinuteX waveform visuals.
// Two flavors:
//   <Waveform>     — static bars, deterministic per `seed` (a recording key),
//                    with an optional progress split for playback position.
//   <LiveWaveform> — organically animating bars for the live-recording state.
// Purely decorative (we don't have real amplitude data from the hardware),
// but seeded so the same recording always shows the same "fingerprint" —
// which makes it feel like real audio rather than random confetti.
import React, { useEffect, useMemo, useRef } from "react";
import { Animated, Easing, Pressable, StyleSheet, View, ViewStyle } from "react-native";
import { useTheme } from "./theme";

// FNV-ish hash → stable pseudo-random heights in [0.18, 1].
function seededHeights(seed: string, count: number): number[] {
  let h = 2166136261 >>> 0;
  const src = seed.length > 0 ? seed : "minutex";
  const out: number[] = [];
  for (let i = 0; i < count; i++) {
    h = Math.imul(h ^ (src.charCodeAt(i % src.length) + i * 31), 16777619) >>> 0;
    const r = ((h >>> 8) % 1000) / 1000; // 0..1
    // Blend a gentle sine envelope with the hash so bars cluster like speech.
    const envelope = 0.55 + 0.45 * Math.sin((i / count) * Math.PI * 3 + r * 2);
    out.push(Math.max(0.18, Math.min(1, 0.25 + 0.75 * r * envelope)));
  }
  return out;
}

export function Waveform({
  seed, bars = 56, height = 64, progress, style, color, dimColor, barWidth = 3,
  onSeek,
}: {
  seed: string;
  bars?: number;
  height?: number;
  /** 0..1 playback position — bars left of it use `color`, right use `dimColor`. */
  progress?: number;
  style?: ViewStyle;
  color?: string;
  dimColor?: string;
  barWidth?: number;
  /** Tap-to-seek callback with the tapped ratio (0..1). */
  onSeek?: (ratio: number) => void;
}) {
  const { C } = useTheme();
  const heights = useMemo(() => seededHeights(seed, bars), [seed, bars]);
  const played = color ?? C.primary;
  const rest = dimColor ?? C.border;
  const cut = progress == null ? bars : Math.round(progress * bars);
  const wRef = useRef(0);

  const body = (
    <View
      style={[st.row, { height }, style]}
      onLayout={(e) => { wRef.current = e.nativeEvent.layout.width; }}
    >
      {heights.map((hRatio, i) => (
        <View
          key={i}
          style={{
            width: barWidth,
            borderRadius: barWidth / 2,
            height: Math.max(3, hRatio * height),
            backgroundColor: i < cut ? played : rest,
          }}
        />
      ))}
    </View>
  );

  if (!onSeek) return body;
  return (
    <Pressable
      onPress={(e) => {
        const w = wRef.current;
        if (w > 0) onSeek(Math.max(0, Math.min(1, e.nativeEvent.locationX / w)));
      }}
    >
      {body}
    </Pressable>
  );
}

// One animated bar of the live waveform. Each bar loops between fresh random
// targets with its own duration, so the field never repeats in lockstep.
function LiveBar({
  active, height, width, color, delay,
}: { active: boolean; height: number; width: number; color: string; delay: number }) {
  const v = useRef(new Animated.Value(0.15)).current;

  useEffect(() => {
    let alive = true;
    if (!active) {
      Animated.timing(v, { toValue: 0.12, duration: 300, easing: Easing.out(Easing.ease), useNativeDriver: true }).start();
      return;
    }
    const step = () => {
      if (!alive) return;
      Animated.timing(v, {
        toValue: 0.15 + Math.random() * 0.85,
        duration: 260 + Math.random() * 340,
        easing: Easing.inOut(Easing.ease),
        useNativeDriver: true,
      }).start(({ finished }) => finished && step());
    };
    const t = setTimeout(step, delay);
    return () => { alive = false; clearTimeout(t); };
  }, [active, v, delay]);

  return (
    <Animated.View
      style={{
        width, height, borderRadius: width / 2, backgroundColor: color,
        transform: [{ scaleY: v }],
      }}
    />
  );
}

export function LiveWaveform({
  active, bars = 36, height = 56, color, style, barWidth = 3,
}: {
  active: boolean;
  bars?: number;
  height?: number;
  color?: string;
  style?: ViewStyle;
  barWidth?: number;
}) {
  const { C } = useTheme();
  return (
    <View style={[st.row, { height }, style]}>
      {Array.from({ length: bars }, (_, i) => (
        <LiveBar
          key={i}
          active={active}
          height={height}
          width={barWidth}
          color={color ?? C.primary}
          delay={(i % 7) * 60}
        />
      ))}
    </View>
  );
}

const st = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 2,
  },
});
