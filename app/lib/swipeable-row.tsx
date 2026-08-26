// lib/swipeable-row.tsx — the swipe affordance behind the Action Center's
// task cards (§9).
//
// WHY PanResponder AND NOT react-native-gesture-handler's Swipeable.
// gesture-handler IS a dependency, but its components require a
// <GestureHandlerRootView> at the app root, and this app does not mount one
// (see src/app/_layout.tsx). Adding one to satisfy a swipe on one screen means
// re-parenting every screen's gesture handling — a far larger change than the
// affordance justifies, and one that can silently alter modal and navigator
// behaviour elsewhere. RN's built-in PanResponder + Animated needs no root, no
// new dependency, and runs the translation on the native driver.
//
// WHAT A SWIPE MAY DO. Only actions with real persistence are offered:
//
//   right → Complete   PATCH status:"Completed"  (update_meeting_task stamps
//                      completed_at server-side)
//   left  → Snooze     PATCH due:<+1 day>        (due_date is patchable)
//   left  → Reassign   opens the existing contact-resolution flow
//
// An action whose backend does not exist is passed as `disabled`, and this
// component renders it dimmed and non-tappable rather than accepting the tap
// and pretending it worked (§24).
import React, { memo, useCallback, useMemo, useRef } from "react";
import {
  Animated, PanResponder, Pressable, StyleSheet, Text, View,
} from "react-native";
import { Icon } from "./icons";
import { S, R, FONT, useTheme, ColorScale } from "./theme";

export type SwipeAction = {
  key: string;
  label: string;
  icon: string;
  color: string;
  onPress: () => void;
  /** True when no backend supports this action. Rendered dimmed and inert. */
  disabled?: boolean;
};

// How far the finger must travel before the row starts following it. Above the
// ~10px slop a vertical FlatList scroll uses, so a scroll never opens a row.
const ACTIVATE_DX = 14;
// Where the left tray rests when open — wide enough for two 68pt actions.
const TRAY_WIDTH = 152;
// How far right the row must go before a release counts as "complete".
const COMPLETE_DX = 96;

export const SwipeableRow = memo(function SwipeableRow({
  children, onComplete, actions, enabled = true,
}: {
  children: React.ReactNode;
  /** Right-swipe. Omit to disable that direction entirely. */
  onComplete?: () => void;
  /** Left-swipe tray. Empty disables that direction. */
  actions?: SwipeAction[];
  enabled?: boolean;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const x = useRef(new Animated.Value(0)).current;
  // Read inside the responder without re-creating it — a responder rebuilt
  // mid-gesture drops the gesture.
  const openRef = useRef(false);

  const tray = actions ?? [];
  const canLeft = tray.length > 0;
  const canRight = !!onComplete;

  const settle = useCallback(
    (to: number) => {
      openRef.current = to !== 0;
      Animated.spring(x, {
        toValue: to,
        useNativeDriver: true,
        bounciness: 0,
        speed: 18,
      }).start();
    },
    [x]
  );

  const responder = useMemo(
    () =>
      PanResponder.create({
        // Never claim the gesture on touch-down: a tap must still reach the
        // card underneath, and a vertical drag must still scroll the list.
        onStartShouldSetPanResponder: () => false,
        onMoveShouldSetPanResponder: (_e, g) => {
          if (!enabled) return false;
          if (Math.abs(g.dx) < ACTIVATE_DX) return false;
          // Horizontal intent only — 2:1 keeps a diagonal flick scrolling.
          if (Math.abs(g.dx) < Math.abs(g.dy) * 2) return false;
          return g.dx < 0 ? canLeft || openRef.current : canRight || openRef.current;
        },
        onPanResponderMove: (_e, g) => {
          const base = openRef.current ? -TRAY_WIDTH : 0;
          let next = base + g.dx;
          // Clamp to what each direction actually supports, with a little
          // resistance past the stop so the row never flies off.
          if (next < -TRAY_WIDTH) next = -TRAY_WIDTH + (next + TRAY_WIDTH) * 0.2;
          if (!canLeft && next < 0) next = next * 0.15;
          if (!canRight && next > 0) next = next * 0.15;
          if (next > COMPLETE_DX * 1.5) {
            next = COMPLETE_DX * 1.5 + (next - COMPLETE_DX * 1.5) * 0.2;
          }
          x.setValue(next);
        },
        onPanResponderRelease: (_e, g) => {
          const base = openRef.current ? -TRAY_WIDTH : 0;
          const end = base + g.dx;
          if (canRight && end > COMPLETE_DX) {
            // Fire, then snap back: the list re-sorts on the refetch, so the
            // row should not sit open waiting for it.
            settle(0);
            onComplete?.();
            return;
          }
          if (canLeft && end < -TRAY_WIDTH / 2) {
            settle(-TRAY_WIDTH);
            return;
          }
          settle(0);
        },
        onPanResponderTerminationRequest: () => !openRef.current,
        onPanResponderTerminate: () => settle(0),
      }),
    // canLeft/canRight/enabled are captured; onComplete is read through the
    // closure at release time, which is fine because the screen's handler is
    // itself a stable useCallback.
    [enabled, canLeft, canRight, onComplete, settle, x]
  );

  const completeOpacity = x.interpolate({
    inputRange: [0, COMPLETE_DX],
    outputRange: [0, 1],
    extrapolate: "clamp",
  });
  const trayOpacity = x.interpolate({
    inputRange: [-TRAY_WIDTH, -TRAY_WIDTH / 3, 0],
    outputRange: [1, 0.4, 0],
    extrapolate: "clamp",
  });

  return (
    <View style={st.wrap}>
      {/* Right-swipe backdrop: complete. */}
      {canRight ? (
        <Animated.View
          style={[st.behind, st.completeBehind, { opacity: completeOpacity }]}
          pointerEvents="none"
        >
          <Icon name="checkmark" size={16} tintColor={C.success} />
          <Text style={[st.behindTxt, { color: C.success }]}>Complete</Text>
        </Animated.View>
      ) : null}

      {/* Left-swipe tray: snooze / reassign. */}
      {canLeft ? (
        <Animated.View style={[st.behind, st.trayBehind, { opacity: trayOpacity }]}>
          {tray.map((a) => (
            <Pressable
              key={a.key}
              onPress={() => {
                if (a.disabled) return;
                settle(0);
                a.onPress();
              }}
              disabled={a.disabled}
              accessibilityRole="button"
              accessibilityState={{ disabled: !!a.disabled }}
              accessibilityLabel={
                a.disabled ? `${a.label} — not available` : a.label
              }
              style={({ pressed }) => [
                st.trayBtn,
                a.disabled && { opacity: 0.35 },
                pressed && !a.disabled && { opacity: 0.6 },
              ]}
            >
              <Icon name={a.icon as never} size={15} tintColor={a.color} />
              <Text style={[st.trayTxt, { color: a.color }]}>{a.label}</Text>
            </Pressable>
          ))}
        </Animated.View>
      ) : null}

      <Animated.View
        style={{ transform: [{ translateX: x }] }}
        {...responder.panHandlers}
      >
        {children}
      </Animated.View>
    </View>
  );
});

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    wrap: { position: "relative" },
    behind: {
      position: "absolute",
      top: 0,
      bottom: S.sm, // matches TaskCard's marginBottom so the tray isn't taller
      borderRadius: R.card,
      flexDirection: "row",
      alignItems: "center",
    },
    completeBehind: {
      left: 0,
      right: 0,
      backgroundColor: C.successSoft,
      paddingLeft: S.lg,
      gap: 6,
    },
    behindTxt: { fontFamily: FONT.bold, fontSize: 12.5 },
    trayBehind: {
      right: 0,
      width: TRAY_WIDTH,
      backgroundColor: C.surface2,
      justifyContent: "space-evenly",
    },
    trayBtn: { alignItems: "center", gap: 4, width: 68 },
    trayTxt: { fontFamily: FONT.semibold, fontSize: 11 },
  });
}
