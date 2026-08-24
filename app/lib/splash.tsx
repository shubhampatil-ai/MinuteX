// lib/splash.tsx — branded in-app splash, "Workspace" edition.
// Shown while the root layout loads fonts and resolves the stored session (and
// for a short minimum hold so the entrance reads as intentional, not a flash).
//
// A clean masthead: a caps kicker, the wordmark in bold sans, a rule that
// draws itself, and the tagline. No gradient, no glow, no bouncing dots.
//
// IMPORTANT: this renders in two places — inside the themed tree, and ABOVE
// ThemeProvider as the font gate in src/app/_layout.tsx. It therefore must not
// call useTheme(). The palette is inlined from theme.tsx's LIGHT/DARK scales
// and picked off the OS scheme directly.
import { useEffect, useRef } from "react";
import { Animated, Easing, StyleSheet, Text, View, useColorScheme } from "react-native";
import { APP_NAME, COMPANY, TAGLINE, FONT, CAPS, S } from "./theme";

// Mirrors LIGHT/DARK in theme.tsx. Duplicated on purpose — see the note above.
const PAPER = { bg: "#F6F7FB", text: "#12131A", dim: "#5B6072", faint: "#9297A8" };
const INK = { bg: "#0B0D14", text: "#F3F4F8", dim: "#9BA0B4", faint: "#666C82" };

export function Splash() {
  const dark = useColorScheme() === "dark";
  const c = dark ? INK : PAPER;

  const titleRise = useRef(new Animated.Value(10)).current;
  const titleFade = useRef(new Animated.Value(0)).current;
  const ruleGrow = useRef(new Animated.Value(0)).current;
  const tagFade = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    Animated.sequence([
      Animated.parallel([
        Animated.timing(titleRise, {
          toValue: 0, duration: 460, easing: Easing.out(Easing.cubic), useNativeDriver: true,
        }),
        Animated.timing(titleFade, { toValue: 1, duration: 460, useNativeDriver: true }),
      ]),
      // The rule under the masthead draws left-to-right. scaleX can't use the
      // native driver together with a left-anchored transform origin, so it
      // runs on the JS driver — it's one short animation on an idle screen.
      Animated.timing(ruleGrow, {
        toValue: 1, duration: 520, easing: Easing.inOut(Easing.cubic), useNativeDriver: false,
      }),
      Animated.timing(tagFade, { toValue: 1, duration: 360, useNativeDriver: true }),
    ]).start();
  }, [titleRise, titleFade, ruleGrow, tagFade]);

  const ruleWidth = ruleGrow.interpolate({ inputRange: [0, 1], outputRange: ["0%", "100%"] });

  return (
    <View style={[st.container, { backgroundColor: c.bg }]}>
      <View style={st.masthead}>
        <Animated.View style={{ opacity: titleFade, transform: [{ translateY: titleRise }] }}>
          <Text style={[st.kicker, { color: c.faint }]}>{COMPANY}</Text>
          <Text style={[st.brand, { color: c.text }]}>{APP_NAME}</Text>
        </Animated.View>

        <Animated.View style={[st.rule, { width: ruleWidth, backgroundColor: c.text }]} />

        <Animated.View style={{ opacity: tagFade }}>
          <Text style={[st.tagline, { color: c.dim }]}>{TAGLINE}</Text>
        </Animated.View>
      </View>
    </View>
  );
}

const st = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", paddingHorizontal: 26 },
  masthead: { alignSelf: "stretch" },
  kicker: { ...CAPS, letterSpacing: 2.6 },
  brand: { fontFamily: FONT.extrabold, fontSize: 62, lineHeight: 64, marginTop: S.sm },
  rule: { height: 2, marginTop: S.md },
  tagline: { fontFamily: FONT.regular, fontSize: 14, lineHeight: 20, marginTop: S.md },
});