// src/app/crm-connected.tsx — the OAuth redirect landing route.
//
// WHY THIS FILE EXISTS. openAuthSessionAsync does NOT intercept the redirect
// itself: per the Expo SDK 57 docs, "the OS delivers the deep link back to your
// app", and on Android the whole thing is a polyfill over AppState + Linking.
// So the browser's final hop to
//
//     recorderapp://crm-connected?connected=0&reason=…
//
// arrives as a normal deep link that expo-router tries to ROUTE. With no file
// matching that path, the user saw "Unmatched Route — Page could not be found"
// with the raw URL printed underneath, instead of landing back in the app.
//
// In the happy case openAuthSessionAsync resolves first and this screen is
// never seen. It exists for the cases where the OS wins the race, or where the
// link is opened cold (browser handed off after the app was backgrounded, the
// user tapped the link from history, etc.). Either way the outcome must not be
// a dead end.
//
// This screen deliberately does NOT re-run the OAuth exchange or write any
// state: the backend already finished (or failed) the exchange before issuing
// this redirect, and `connected=1` here is only a report of that. It reads the
// query param, tells the user, and sends them somewhere real — the Salesforce
// settings screen, which re-fetches the authoritative status from the backend.
import { useEffect } from "react";
import { StyleSheet, Text, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { FONT, S, useTheme, ColorScale } from "../../lib/theme";
import { Loading } from "../../lib/ui";

// Mirrors REASON_COPY in lib/salesforce.ts — the same backend ?reason= codes.
// Kept in sync deliberately rather than shared, because that module owns the
// in-session browser flow and this one owns the cold-start fallback.
const REASON_COPY: Record<string, string> = {
  denied: "Salesforce access was declined.",
  missing_params: "Salesforce didn’t return a valid response.",
  exchange_failed: "Couldn’t complete the Salesforce connection.",
  pkce_missing: "That Salesforce sign-in link expired.",
};

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: {
      flex: 1, backgroundColor: C.bg, alignItems: "center" as const,
      justifyContent: "center" as const, paddingHorizontal: 32, gap: S.md,
    },
    title: {
      fontFamily: FONT.extrabold, fontSize: 19, color: C.text,
      textAlign: "center" as const,
    },
    body: {
      fontFamily: FONT.regular, fontSize: 14, lineHeight: 21, color: C.textDim,
      textAlign: "center" as const,
    },
  });
}

export default function CrmConnectedScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const params = useLocalSearchParams<{ connected?: string; reason?: string }>();
  const st = buildStyles(C);

  const ok = params.connected === "1";
  const reason = typeof params.reason === "string" ? params.reason : "";

  // Hand the user back to the Salesforce screen, which reads the real
  // connection status from the backend rather than trusting this query param.
  // replace(), not push(), so the deep link doesn't sit in the back stack.
  useEffect(() => {
    const t = setTimeout(() => router.replace("/salesforce"), ok ? 700 : 1600);
    return () => clearTimeout(t);
  }, [router, ok]);

  return (
    <View style={st.container}>
      {ok ? (
        <>
          <Loading label="Finishing up…" />
          <Text style={st.body}>Salesforce connected. Taking you back…</Text>
        </>
      ) : (
        <>
          <Text style={st.title}>Salesforce didn’t connect</Text>
          <Text style={st.body}>
            {REASON_COPY[reason] ?? "Couldn’t complete the Salesforce connection."}
            {"\n"}Taking you back so you can try again…
          </Text>
        </>
      )}
    </View>
  );
}
