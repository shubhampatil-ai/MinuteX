// lib/waveform.tsx — the two audio visuals MinuteX draws, both honest.
//
//   <LevelWaveform>  the recorder's live waveform. Draws a history of REAL
//                    input levels (see lib/use-input-level.ts) — newest on the
//                    right, scrolling left. It has no idea what audio is; it
//                    renders the array it is handed, so it can be tested and
//                    previewed with any data.
//   <SeekTrack>      the player's scrub bar: elapsed / remaining with
//                    tap-to-seek.
//
// WHAT WAS REMOVED, AND WHY. This file used to export a `Waveform` component
// that drew bar heights from a hash of the recording's key, and a
// `LiveWaveform` that animated bars to random targets. Both looked like audio and were not.
// On a recorder that is the worst possible lie: a waveform that keeps dancing
// over a microphone that has silently stopped tells the user everything is
// fine at exactly the moment it is not. Playback has no decoded amplitude data
// to draw from (that would mean downloading and decoding the whole file on the
// phone), so the player shows a plain track rather than an invented shape.
import React, { useRef } from "react";
import { Pressable, StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";
import Animated, {
  useAnimatedStyle, withTiming, type SharedValue,
} from "react-native-reanimated";
import { MOTION } from "./motion";
import { useTheme } from "./theme";

// ---------------------------------------------------------------------------
// LevelWaveform
// ---------------------------------------------------------------------------
function Bar({
  levels, index, height, width, minHeight, color, emptyColor,
}: {
  levels: SharedValue<number[]>;
  index: number;
  height: number;
  width: number;
  minHeight: number;
  color: string;
  emptyColor: string;
}) {
  const style = useAnimatedStyle(() => {
    const v = levels.get()[index] ?? -1;
    // -1 = no sample yet (the field before recording has filled it): a dot in
    // the resting colour. 0 = recorded silence: a dot in the live colour.
    const empty = v < 0;
    const h = empty ? minHeight : Math.max(minHeight, v * height);
    return {
      height: withTiming(h, { duration: MOTION.waveformEaseMs }),
      backgroundColor: empty ? emptyColor : color,
    };
  });
  return <Animated.View style={[{ width, borderRadius: width / 2 }, style]} />;
}

export function LevelWaveform({
  levels, bars, height = 96, barWidth = 3, color, emptyColor, style, accessibilityLabel,
}: {
  /** Oldest → newest, each 0..1, or -1 for "no sample yet". */
  levels: SharedValue<number[]>;
  bars: number;
  height?: number;
  barWidth?: number;
  /** Colour of captured samples. Pass a dim colour while paused. */
  color: string;
  /** Colour of the not-yet-filled placeholder dots. */
  emptyColor: string;
  style?: StyleProp<ViewStyle>;
  accessibilityLabel?: string;
}) {
  const minHeight = barWidth; // a round dot
  return (
    <View
      style={[st.row, { height }, style]}
      accessible
      accessibilityRole="image"
      accessibilityLabel={accessibilityLabel ?? "Live input level"}
    >
      {Array.from({ length: bars }, (_, i) => (
        <Bar
          key={i}
          levels={levels}
          index={i}
          height={height}
          width={barWidth}
          minHeight={minHeight}
          color={color}
          emptyColor={emptyColor}
        />
      ))}
    </View>
  );
}

// ---------------------------------------------------------------------------
// SeekTrack
// ---------------------------------------------------------------------------
export function SeekTrack({
  progress, onSeek, color, trackColor, style, height = 28,
}: {
  /** 0..1 playback position. */
  progress: number;
  onSeek?: (ratio: number) => void;
  color?: string;
  trackColor?: string;
  style?: StyleProp<ViewStyle>;
  /** Touch height; the visible track is 4px centred inside it. */
  height?: number;
}) {
  const { C } = useTheme();
  const wRef = useRef(0);
  const p = Number.isFinite(progress) ? Math.max(0, Math.min(1, progress)) : 0;
  const fg = color ?? C.primary;
  return (
    <Pressable
      disabled={!onSeek}
      onPress={(e) => {
        const w = wRef.current;
        if (w > 0 && onSeek) onSeek(Math.max(0, Math.min(1, e.nativeEvent.locationX / w)));
      }}
      onLayout={(e) => { wRef.current = e.nativeEvent.layout.width; }}
      accessibilityRole="adjustable"
      accessibilityLabel="Playback position"
      accessibilityValue={{ min: 0, max: 100, now: Math.round(p * 100) }}
      // "adjustable" promises swipe-up/down to a screen-reader user; these
      // are what make that promise true (5% steps).
      accessibilityActions={[{ name: "increment" }, { name: "decrement" }]}
      onAccessibilityAction={(e) => {
        if (!onSeek) return;
        const step = e.nativeEvent.actionName === "increment" ? 0.05 : -0.05;
        onSeek(Math.max(0, Math.min(1, p + step)));
      }}
      style={[{ height, justifyContent: "center" }, style]}
    >
      <View style={[st.track, { backgroundColor: trackColor ?? C.border }]}>
        <View style={[st.fill, { width: `${p * 100}%`, backgroundColor: fg }]} />
      </View>
      <View
        pointerEvents="none"
        style={[st.thumb, { left: `${p * 100}%`, backgroundColor: fg, borderColor: C.surface }]}
      />
    </Pressable>
  );
}

const st = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  track: { height: 4, borderRadius: 2, overflow: "hidden" },
  fill: { height: 4, borderRadius: 2 },
  thumb: {
    position: "absolute", width: 14, height: 14, borderRadius: 7, marginLeft: -7,
    borderWidth: 2,
  },
});
