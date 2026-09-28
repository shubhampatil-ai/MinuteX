// lib/motion.tsx — small, reusable spring microinteractions built on
// react-native-reanimated. Two primitives cover every "polish" moment the
// redesign asks for (tab switching already has its own spring in ui.tsx's
// SegmentedTabs via the RN Animated API — these two are for everywhere else):
//
//   <Pop>        mounts with a spring scale+fade — task rows appearing,
//                a newly generated document card, the Assistant opening.
//   <PressSpring> a spring "squish" on press — checkboxes, status steps,
//                the Assistant FAB, Assign/Notify buttons.
import { useEffect } from "react";
import { Pressable, View, type PressableProps, type StyleProp, type ViewStyle } from "react-native";
import Animated, {
  cancelAnimation, Easing, useAnimatedStyle, useSharedValue, withDelay,
  withRepeat, withSpring, withTiming,
} from "react-native-reanimated";

// ---------------------------------------------------------------------------
// MOTION — the one set of timing constants every animation draws from.
//
// Motion in MinuteX communicates a STATE CHANGE (recording started, a step
// completed, a sheet opened). Nothing loops for decoration: the only
// continuous animations are the live-recording pulse and the waveform, and
// both stop the moment capture stops.
// ---------------------------------------------------------------------------
export const MOTION = {
  /** Press feedback, toggles, checkmarks. */
  fast: 120,
  /** Sheets, tab indicator, fades between states. */
  standard: 220,
  /** One-off emphasis — a step completing, the recorder changing state. */
  slow: 360,
  /** How far a pressable scales on press-in. Small: this is feedback, not a bounce. */
  pressScale: 0.96,
  /** Distance a sheet or toast travels on enter. */
  slideDistance: 24,
  /** One full cycle of the live-recording indicator. */
  recordingPulse: 1400,
  /**
   * How often the waveform samples the input level. 70ms (~14 Hz) reads as
   * continuous movement for speech envelopes while costing one synchronous
   * native read per tick — well under a millisecond. Faster buys nothing the
   * eye can see and costs battery over a three-hour meeting.
   */
  waveformSampleMs: 70,
  /** Per-bar height transition; slightly under the sample period so a bar
   *  always finishes easing before the next value arrives. */
  waveformEaseMs: 60,
} as const;

const SPRING = { damping: 14, stiffness: 180, mass: 0.6 };

export function Pop({
  children, style, delay = 0,
}: { children: React.ReactNode; style?: StyleProp<ViewStyle>; delay?: number }) {
  const progress = useSharedValue(0);

  useEffect(() => {
    progress.set(withDelay(delay, withSpring(1, SPRING)));
  }, [progress, delay]);

  const animatedStyle = useAnimatedStyle(() => ({
    opacity: progress.get(),
    transform: [{ scale: 0.92 + progress.get() * 0.08 }],
  }));

  return <Animated.View style={[style, animatedStyle]}>{children}</Animated.View>;
}

export function PressSpring({
  children, style, onPress, disabled, accessibilityLabel, hitSlop,
  pressedScale = 0.92, accessibilityRole, accessibilityState,
}: {
  children: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  onPress?: PressableProps["onPress"];
  disabled?: boolean;
  accessibilityLabel?: string;
  hitSlop?: PressableProps["hitSlop"];
  /** Defaults to the historic 0.92 so existing call sites are unchanged;
   *  large controls pass MOTION.pressScale for a subtler press. */
  pressedScale?: number;
  accessibilityRole?: PressableProps["accessibilityRole"];
  accessibilityState?: PressableProps["accessibilityState"];
}) {
  const scale = useSharedValue(1);
  const animatedStyle = useAnimatedStyle(() => ({ transform: [{ scale: scale.get() }] }));

  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityLabel={accessibilityLabel}
      accessibilityRole={accessibilityRole ?? "button"}
      accessibilityState={accessibilityState ?? (disabled ? { disabled: true } : undefined)}
      hitSlop={hitSlop}
      onPressIn={() => { scale.set(withSpring(pressedScale, SPRING)); }}
      onPressOut={() => { scale.set(withSpring(1, SPRING)); }}
    >
      <Animated.View style={[style, animatedStyle]}>{children}</Animated.View>
    </Pressable>
  );
}

/**
 * The live-recording indicator: a solid dot with one soft ring breathing out
 * from it. It animates ONLY while `active` — a paused recorder shows the dot
 * still, which is itself the signal that capture has stopped.
 */
export function PulseDot({
  active, color, size = 8,
}: { active: boolean; color: string; size?: number }) {
  const t = useSharedValue(0);

  useEffect(() => {
    if (active) {
      t.set(0);
      t.set(withRepeat(
        withTiming(1, { duration: MOTION.recordingPulse, easing: Easing.out(Easing.quad) }),
        -1,
        false,
      ));
    } else {
      cancelAnimation(t);
      t.set(withTiming(0, { duration: MOTION.fast }));
    }
    return () => cancelAnimation(t);
  }, [active, t]);

  const ring = useAnimatedStyle(() => ({
    opacity: active ? 0.55 * (1 - t.get()) : 0,
    transform: [{ scale: 1 + t.get() * 1.6 }],
  }));

  return (
    <View
      style={{ width: size, height: size, alignItems: "center", justifyContent: "center" }}
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
    >
      <Animated.View
        style={[{
          position: "absolute", width: size, height: size, borderRadius: size / 2,
          backgroundColor: color,
        }, ring]}
      />
      <View style={{ width: size, height: size, borderRadius: size / 2, backgroundColor: color }} />
    </View>
  );
}
