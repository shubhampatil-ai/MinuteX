// src/app/integrations-connected.tsx — the OAuth redirect landing route.
//
// WHY THIS FILE EXISTS. Exactly the reason crm-connected.tsx exists, and the
// bug it fixes is the same one: openAuthSessionAsync does NOT intercept the
// redirect itself. Per the Expo SDK 57 docs the OS delivers the deep link back
// to the app, so the browser's final hop to
//
//     recorderapp://integrations-connected?provider=gmail&connected=1
//
// arrives as an ordinary deep link that expo-router tries to ROUTE. With no
// file matching that path the user lands on "Unmatched Route — Page could not
// be found" with the raw URL underneath, instead of back in the app.
//
// In the happy case openAuthSessionAsync resolves first and this screen is
// never seen. It exists for the cases where the OS wins the race, or the link
// is opened cold (the browser handed off after the app was backgrounded, the
// user tapped the link from history).
//
// This screen deliberately does NOT re-run the exchange or write any state:
// the backend already finished (or failed) it before issuing the redirect, and
// `connected=1` here is only a REPORT of that. It reads the params, says what
// happened, and sends the user somewhere real — the screen it lands on
// re-fetches authoritative status from the backend.
import { useEffect } from "react";
import { StyleSheet, Text, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { FONT, S, useTheme, ColorScale } from "../../lib/theme";
import { Loading } from "../../lib/ui";

// Mirrors REASON_COPY in lib/integrations.tsx — the same backend ?reason=
// codes. Kept in sync deliberately rather than shared, following the same
// split crm-connected.tsx makes: that module owns the in-session browser
// flow, this one owns the cold-start fallback.
const REASON_COPY: Record<string, string> = {
  denied: "Access was declined.",
  missing_params: "The provider didn’t return a valid response.",
  expired: "That sign-in link expired.",
  exchange_failed: "Couldn’t complete the connection.",
  no_refresh_token:
    "Google didn’t return a lasting connection. Try again and approve access "
    + "when the consent screen appears.",
  failed: "Couldn’t complete the connection.",
};

// The provider ids that have a screen of their own to land on. Anything else
// goes to the Integrations list, which is always correct if less specific.
const PROVIDER_ROUTE: Record<string, "/integrations/gmail"> = {
  gmail: "/integrations/gmail",
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

export default function IntegrationConnectedScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const params = useLocalSearchParams<{
    connected?: string; reason?: string; provider?: string;
  }>();
  const st = buildStyles(C);

  const ok = params.connected === "1";
  const reason = typeof params.reason === "string" ? params.reason : "";
  const provider = typeof params.provider === "string" ? params.provider : "";
  const target = PROVIDER_ROUTE[provider] ?? "/integrations";

  // replace(), not push(), so this transient hop doesn't sit in the back stack.
  useEffect(() => {
    const t = setTimeout(() => router.replace(target), ok ? 700 : 1600);
    return () => clearTimeout(t);
  }, [router, ok, target]);

  return (
    <View style={st.container}>
      {ok ? (
        <>
          <Loading label="Finishing up…" />
          <Text style={st.body}>Connected. Taking you back…</Text>
        </>
      ) : (
        <>
          <Text style={st.title}>Couldn’t connect</Text>
          <Text style={st.body}>
            {REASON_COPY[reason] ?? "Couldn’t complete the connection."}
            {"\n"}Taking you back so you can try again…
          </Text>
        </>
      )}
    </View>
  );
}
