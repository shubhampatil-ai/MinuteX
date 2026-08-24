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
import { Pressable, type PressableProps, type StyleProp, type ViewStyle } from "react-native";
import Animated, {
  useAnimatedStyle, useSharedValue, withSpring, withDelay,
} from "react-native-reanimated";

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
}: {
  children: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  onPress?: PressableProps["onPress"];
  disabled?: boolean;
  accessibilityLabel?: string;
  hitSlop?: PressableProps["hitSlop"];
}) {
  const scale = useSharedValue(1);
  const animatedStyle = useAnimatedStyle(() => ({ transform: [{ scale: scale.get() }] }));

  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityLabel={accessibilityLabel}
      hitSlop={hitSlop}
      onPressIn={() => { scale.set(withSpring(0.92, SPRING)); }}
      onPressOut={() => { scale.set(withSpring(1, SPRING)); }}
    >
      <Animated.View style={[style, animatedStyle]}>{children}</Animated.View>
    </Pressable>
  );
}
