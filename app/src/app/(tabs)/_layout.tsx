// src/app/(tabs)/_layout.tsx — bottom navigation, "Workspace" edition.
// Desk · Record (large raised center) · You.
//
// Three destinations, not four: the device is a piece of hardware you check on,
// not a place you work, so it moved off the bar to the Desk's top-left status
// pill (src/app/(tabs)/index.tsx) where its live connection state is visible
// without a trip. It still lives at /devices, also reachable from You.
//
// The bar sits on a white/light surface, separated from the page by a
// hairline and a soft shadow. Labels are caps micro-labels. The active tab
// is confident blue — the one raised element is the mic FAB, filled with the
// same blue so it reads as the app's primary action, not a neutral utility.
import { useMemo } from "react";
import { Tabs, useRouter } from "expo-router";
import { Pressable, StyleSheet, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Icon } from "../../../lib/icons";
import { useTheme, ColorScale, ELEV, FONT } from "../../../lib/theme";
import { useDevice } from "../../../lib/device-context";

function buildStyles(C: ColorScale, bottomInset: number) {
  return StyleSheet.create({
    bar: {
      // Raised white surface above the page background, closed by a hairline
      // and a soft shadow — matches every other card-like surface in the app.
      backgroundColor: C.surface,
      borderTopColor: C.border,
      borderTopWidth: 1,
      shadowColor: C.shadow,
      ...ELEV.md,
      // Respect the home-indicator / gesture-nav area instead of a fixed 66 —
      // on gesture phones the labels used to sit half-under the indicator.
      height: 60 + Math.max(bottomInset, 8),
      paddingBottom: Math.max(bottomInset, 8),
      paddingTop: 8,
    },
    recordBtnWrap: {
      top: -26,
      alignSelf: "center",
    },
    recordBtn: {
      width: 58,
      height: 58,
      borderRadius: 29,
      alignItems: "center",
      justifyContent: "center",
      backgroundColor: C.primary,
      // The 5px ring is the bar colour, so the circle reads as punched
      // through the bar rather than floating above it.
      borderWidth: 5,
      borderColor: C.surface,
      shadowColor: C.shadow,
      ...ELEV.md,
    },
    // Live badge on the FAB while the hardware is recording — danger red,
    // the one colour that outranks primary blue.
    recBadge: {
      position: "absolute",
      top: 2,
      right: 2,
      width: 15,
      height: 15,
      borderRadius: 8,
      backgroundColor: C.danger,
      borderWidth: 2.5,
      borderColor: C.surface,
    },
  });
}

// Raised center button — the "+ New Recording" entry point. Opens the source
// chooser (phone / upload / MinuteX device), not a specific recorder: the
// hardware is one source among three, so the entry point can't assume it.
// If the hardware IS mid-recording, skip the chooser and jump straight to its
// live screen (that's what the badge is advertising).
// Resting state is solid primary blue — recording-red is reserved for the
// active recording badge, so the entry point to recording isn't red before
// you've even started.
function RecordButton({ tb }: { tb: ReturnType<typeof buildStyles> }) {
  const router = useRouter();
  const { C } = useTheme();
  const { status, connState } = useDevice();
  const recording = connState === "connected" && status?.isRecording === true;
  return (
    <Pressable
      onPress={() => router.navigate(recording ? "/record" : "/new-recording")}
      style={({ pressed }) => [tb.recordBtnWrap, pressed && { opacity: 0.7 }]}
      accessibilityLabel={recording ? "Recording in progress" : "New recording"}
    >
      <View style={tb.recordBtn}>
        <Icon name="mic.fill" tintColor={C.textOnPrimary} size={23} />
      </View>
      {recording ? <View style={tb.recBadge} pointerEvents="none" /> : null}
    </Pressable>
  );
}

export default function TabsLayout() {
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const tb = useMemo(() => buildStyles(C, insets.bottom), [C, insets.bottom]);

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarStyle: tb.bar,
        // Active tab is the single confident blue accent, matching every
        // other primary action / active state in the app.
        tabBarActiveTintColor: C.primary,
        tabBarInactiveTintColor: C.textFaint,
        tabBarShowLabel: true,
        tabBarLabelStyle: {
          fontFamily: FONT.semibold,
          fontSize: 10,
          letterSpacing: 0.6,
          textTransform: "uppercase",
        },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "Desk",
          tabBarIcon: ({ color }) => <Icon name="newspaper.fill" tintColor={color} size={21} />,
        }}
      />
      <Tabs.Screen
        name="record-tab"
        options={{
          title: "",
          tabBarButton: () => <RecordButton tb={tb} />,
        }}
      />
      <Tabs.Screen
        name="profile"
        options={{
          title: "You",
          tabBarIcon: ({ color }) => <Icon name="person.fill" tintColor={color} size={21} />,
        }}
      />
    </Tabs>
  );
}
