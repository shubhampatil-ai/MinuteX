// lib/assistant-button.tsx — the one floating entry point into the Assistant
// workspace. Visible on Overview and Transcript; never embedded inline on
// either page. A soft violet-to-blue pill with a sparkle mark, matching the
// gradient used for every other "AI" surface in the app. Pops in on mount and
// gives a spring squish on press, per the redesign's microinteraction spec.
import { StyleSheet, Text } from "react-native";
import { useRouter } from "expo-router";
import { ELEV, FONT, R, useTheme } from "./theme";
import { Icon } from "./icons";
import { RawGradient } from "./ui";
import { Pop, PressSpring } from "./motion";

export function AssistantButton({ recordingKey }: { recordingKey: string }) {
  const router = useRouter();
  const { C } = useTheme();

  return (
    <Pop style={st.wrap} delay={150}>
      <PressSpring
        onPress={() => router.push({ pathname: "/recording/[key]/assistant", params: { key: recordingKey } })}
        style={[st.wrapInner, { shadowColor: C.shadow }]}
        accessibilityLabel="Open Assistant"
      >
        <RawGradient colors={[C.primary, C.accent]} style={st.pill}>
          <Icon name="sparkles" tintColor="#FFFFFF" size={17} />
          <Text style={st.txt}>Assistant</Text>
        </RawGradient>
      </PressSpring>
    </Pop>
  );
}

const st = StyleSheet.create({
  wrap: { position: "absolute", right: 20, bottom: 24 },
  wrapInner: { borderRadius: R.pill, ...ELEV.lg },
  pill: {
    flexDirection: "row", alignItems: "center", gap: 8,
    borderRadius: R.pill, paddingHorizontal: 18, paddingVertical: 14,
  },
  txt: { fontFamily: FONT.bold, fontSize: 14.5, color: "#FFFFFF" },
});
