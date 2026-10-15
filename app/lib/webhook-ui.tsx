// lib/webhook-ui.tsx — shared pieces for THE Salesforce connection (Webhook
// Contract v2 underneath — never called "webhook" on screen): the build flag,
// error wording, and the card on the Integrations screen. The server's `mode`
// says whether a workspace is connected — the app never guesses.
import { useCallback, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { ApiError, getSalesforceOrgConnection, getWebhook } from "./api";
import { IntegrationLogo } from "./integration-logos";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { useWorkspace } from "./workspace-context";
import {
  SalesforceCardStatus, WebhookEndpoint, WebhookMode, configStatusLine, salesforceCardStatus,
  webhookErrorMessage,
} from "./webhook-flow";

/** The integration-user Salesforce login (lib/salesforce-login-card.tsx),
 *  off unless the build sets EXPO_PUBLIC_SALESFORCE_OAUTH=1. Defined here,
 *  not in the login card, because the card already imports this file:
 *  reading it from there would make the two files import each other. The
 *  card re-exports it, so its importers are unchanged. */
export const SALESFORCE_OAUTH_ENABLED = process.env.EXPO_PUBLIC_SALESFORCE_OAUTH === "1";

/** The Salesforce webhook is THE Salesforce integration, so it is on in
 *  every build unless one sets EXPO_PUBLIC_WEBHOOK_V2=0 (a kill switch, not
 *  an opt-in). The server's WEBHOOK_ENABLED is the other half: with it off
 *  every route answers webhook_disabled, the card says "unavailable" and the
 *  meeting screen shows no Salesforce section — nothing is offered that the
 *  backend cannot do. The MinuteX Salesforce PACKAGE UI is hidden (its flag,
 *  EXPO_PUBLIC_SITE_SALESFORCE, is set by no build). */
export const WEBHOOK_V2_ENABLED = process.env.EXPO_PUBLIC_WEBHOOK_V2 !== "0";

/** Internal testing builds (eas.json "preview" / "sideload") set
 *  EXPO_PUBLIC_SALESFORCE_COMING_SOON=1: Salesforce is SHOWN as coming soon
 *  but nothing can be pressed — no connect, no setup screen, no meeting
 *  push. Development and production builds are unaffected. */
export const SALESFORCE_COMING_SOON = process.env.EXPO_PUBLIC_SALESFORCE_COMING_SOON === "1";

export function webhookError(e: unknown): string {
  if (e instanceof ApiError) return webhookErrorMessage(e.code, e.message);
  return e instanceof Error ? e.message : "Something went wrong.";
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    card: { backgroundColor: C.surface, borderRadius: R.card, borderWidth: 1,
            borderColor: C.border, padding: S.lg, marginBottom: S.md },
    head: { flexDirection: "row", alignItems: "center", gap: S.md },
    logo: { width: 44, height: 44, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
            alignItems: "center", justifyContent: "center", backgroundColor: C.bg },
    name: { fontFamily: FONT.bold, fontSize: 15.5, color: C.text },
    status: { fontFamily: FONT.bold, fontSize: 11, letterSpacing: 0.6, marginTop: 5 },
    desc: { fontFamily: FONT.regular, fontSize: 13, lineHeight: 19, color: C.textDim, marginTop: S.sm },
    note: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint, marginTop: S.sm },
    row: { flexDirection: "row", justifyContent: "flex-end", marginTop: S.md },
    action: { borderRadius: R.pill, paddingHorizontal: 16, minHeight: 40, justifyContent: "center" },
    actionTxt: { fontFamily: FONT.bold, fontSize: 13.5 },
  });
}

/** Integrations -> CRM -> "Salesforce". Opens the connection screen
 *  (src/app/salesforce). Shows the configuration version so an admin
 *  can tell which Salesforce setup MinuteX is reading. */
export function WebhookIntegrationCard() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId, can } = useWorkspace();
  const [state, setState] = useState<{ mode: WebhookMode; endpoint: WebhookEndpoint } | null>(null);
  const [status, setStatus] = useState<SalesforceCardStatus>("not_connected");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useFocusEffect(useCallback(() => {
    let live = true;
    // Coming soon: nothing to check — the card makes no request at all.
    if (!activeId || SALESFORCE_COMING_SOON) { setLoading(false); return undefined; }
    setLoading(true);
    // With the login in this build, BOTH halves decide the status: the
    // receiver row outlives a login disconnect (salesforceCardStatus).
    Promise.all([
      getWebhook(activeId),
      SALESFORCE_OAUTH_ENABLED ? getSalesforceOrgConnection(activeId) : Promise.resolve("not_used" as const),
    ])
      .then(([r, login]) => {
        if (!live) return;
        setState(r);
        setStatus(salesforceCardStatus(r, login));
        setError("");
      })
      .catch((e) => { if (live) setError(webhookError(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [activeId]));

  const connected = status === "connected" || status === "paused";
  const needsAction = status === "setup_needed" || status === "reconnect";
  const admin = can("manage_integrations");
  const label = SALESFORCE_COMING_SOON ? "COMING SOON" : loading ? "CHECKING…" : error ? "UNAVAILABLE"
    : status === "connected" ? "CONNECTED" : status === "paused" ? "PAUSED"
      : status === "setup_needed" ? "SETUP NEEDED" : status === "reconnect" ? "RECONNECT REQUIRED"
        : "NOT CONNECTED";
  const color = status === "connected" && !error ? C.success : error || needsAction ? C.warn : C.textFaint;

  return (
    <View style={st.card}>
      <View style={st.head}>
        <View style={st.logo}><IntegrationLogo provider="salesforce" size={26} /></View>
        <View style={{ flex: 1 }}>
          <Text style={st.name}>Salesforce</Text>
          <Text style={[st.status, { color }]}>{label}</Text>
        </View>
      </View>
      <Text style={st.desc}>
        Send meetings to your Salesforce org. Your org decides the meeting types and what
        each one creates there.
      </Text>
      {SALESFORCE_COMING_SOON ? (
        // Not a button: a plain label, so there is nothing to press.
        <View style={st.row}>
          <View style={[st.action, { backgroundColor: C.surface, borderWidth: 1, borderColor: C.border,
                                     opacity: 0.6 }]}
            accessibilityRole="text" accessibilityState={{ disabled: true }}>
            <Text style={[st.actionTxt, { color: C.textFaint }]}>Coming soon</Text>
          </View>
        </View>
      ) : connected ? (
        <Text style={st.note}>{configStatusLine(state?.endpoint.configuration)}</Text>
      ) : error ? <Text style={st.note}>{error}</Text>
        : status === "setup_needed" ? (
          <Text style={st.note}>Signed in to Salesforce, but setup isn&apos;t finished.</Text>
        ) : status === "reconnect" ? (
          <Text style={st.note}>Salesforce no longer accepts MinuteX&apos;s sign-in.</Text>
        ) : state?.mode === "site" ? (
          <Text style={st.note}>Salesforce needs to be connected again for this workspace.</Text>
        ) : null}
      {!SALESFORCE_COMING_SOON && (connected || admin) && !error ? (
        <View style={st.row}>
          <Pressable onPress={() => router.push("/salesforce" as never)}
            disabled={loading} accessibilityRole="button"
            style={({ pressed }) => [st.action, {
              backgroundColor: connected ? C.surface : C.primary,
              borderWidth: connected ? 1 : 0, borderColor: C.border,
              opacity: pressed || loading ? 0.7 : 1 }]}>
            <Text style={[st.actionTxt, { color: connected ? C.text : C.textOnPrimary }]}>
              {connected ? "Manage" : status === "setup_needed" ? "Finish setup"
                : status === "reconnect" ? "Connect again" : "Connect Salesforce"}
            </Text>
          </Pressable>
        </View>
      ) : null}
    </View>
  );
}
