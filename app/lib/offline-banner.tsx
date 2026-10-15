// lib/offline-banner.tsx — the app-wide "you're offline" note.
//
// Mounted ONCE in the root layout. While the phone is known to be offline
// (NetInfo, via lib/query-client.tsx useIsOnline) it says what that means in
// this app: screens show their saved data, and changes need a connection —
// lib/api.ts refuses writes at once with the same wording rather than
// queueing them (docs/LOCAL_FIRST_AUDIT.md §3: AWS is the source of truth).
//
// A small pill above the tab bar, never over a header, and pointerEvents
// "none": it can never block a tap on what is underneath.
import { StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useIsOnline } from "./query-client";
import { FONT, R, S, useTheme } from "./theme";

export const OFFLINE_BANNER_TEXT = "You're offline. Showing saved data; changes need a connection.";

export function OfflineBanner() {
  const online = useIsOnline();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  if (online) return null;
  return (
    <View pointerEvents="none" style={[st.wrap, { bottom: insets.bottom + 72 }]}
      accessibilityLiveRegion="polite">
      <View style={[st.pill, { backgroundColor: C.warnSoft, borderColor: C.warn }]}>
        <Text style={[st.text, { color: C.text }]}>{OFFLINE_BANNER_TEXT}</Text>
      </View>
    </View>
  );
}

const st = StyleSheet.create({
  wrap: { position: "absolute", left: S.lg, right: S.lg, alignItems: "center" },
  pill: { borderWidth: 1, borderRadius: R.md, paddingHorizontal: S.md, paddingVertical: 8 },
  text: { fontFamily: FONT.semibold, fontSize: 12.5, textAlign: "center" },
});
