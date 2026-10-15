// src/app/salesforce/index.tsx — Settings -> Integrations -> Salesforce.
// THE Salesforce connection (Webhook Contract v2 underneath; the app never
// says "webhook" — to a person this is simply "Salesforce").
//
//   Salesforce login   the integration user (lib/salesforce-login-card.tsx);
//                      "Finish setup" there configures the receiver
//   Signing secret    shown ONCE after a rotation
//   [Test connection]  a signed configuration fetch; shows its version
//   Records            what users can link a meeting to, as Salesforce sent it,
//                      with its configuration version (meeting types, the
//                      older approach, are no longer shown)
//   Disconnect Salesforce
//
// The manual "Salesforce address" field and the People section were removed
// (user request, 2026-10-15): the login's Finish setup finds the receiver.
//
// (The retired MinuteX Salesforce package, site-salesforce/*, is not shown.)
// The secret lives in this screen's memory only — never stored on the
// device, never shown again — and the server never returns it after the
// response that created it.
import { useCallback, useMemo, useState } from "react";
import { Alert, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import * as Clipboard from "expo-clipboard";
import { FONT, R, S, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, Card, ErrorText, KeyboardAware, Loading, SectionRule, SuccessText,
} from "../../../lib/ui";
import {
  deleteWebhook, getWebhook, refreshWebhookConfiguration, rotateWebhookSecret, testWebhook,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { WebhookEndpoint, configStatusLine, testLine } from "../../../lib/webhook-flow";
import { SALESFORCE_COMING_SOON, webhookError } from "../../../lib/webhook-ui";
import { SALESFORCE_OAUTH_ENABLED, SalesforceLoginCard } from "../../../lib/salesforce-login-card";
import { linkableObjectSummary } from "../../../lib/salesforce-link-flow";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.x4 },
    help: { fontFamily: FONT.regular, fontSize: 12, lineHeight: 18, color: C.textFaint, marginTop: S.sm },
    line: { fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim },
    strong: { fontFamily: FONT.bold, fontSize: 15, color: C.text },
    secretBox: { borderRadius: R.md, borderWidth: 1, borderColor: C.warn, padding: S.md,
                 backgroundColor: C.warnSoft, marginBottom: S.md },
    secret: { fontFamily: FONT.mono, fontSize: 13, color: C.text, marginVertical: S.sm },
    warn: { fontFamily: FONT.bold, fontSize: 12.5, color: C.text },
    type: { paddingVertical: 8, borderTopWidth: 1, borderTopColor: C.border },
    typeName: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    typeMeta: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
  });
}

export default function SalesforceScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId, can } = useWorkspace();
  const admin = can("manage_integrations");
  const [endpoint, setEndpoint] = useState<WebhookEndpoint | null>(null);
  // In memory only, for this visit: the one time the secret exists in the app.
  const [secret, setSecret] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    if (!activeId || SALESFORCE_COMING_SOON) { setLoading(false); return; }
    try {
      const r = await getWebhook(activeId);
      setEndpoint(r.endpoint);
      setError("");
    } catch (e) {
      setError(webhookError(e));
    } finally {
      setLoading(false);
    }
  }, [activeId]);

  useFocusEffect(useCallback(() => { void load(); }, [load]));

  async function run(name: string, fn: () => Promise<void>) {
    setBusy(name);
    setNotice("");
    try {
      await fn();
      setError("");
    } catch (e) {
      setError(webhookError(e));
    } finally {
      setBusy("");
    }
  }

  function onRotate() {
    Alert.alert(
      "Create a new signing secret?",
      "MinuteX signs with both the old and new secret for 24 hours, then only the new one. Update Salesforce before then.",
      [{ text: "Cancel", style: "cancel" },
       { text: "Create", onPress: () => void run("rotate", async () => {
         const r = await rotateWebhookSecret(activeId!);
         setEndpoint(r.endpoint);
         setSecret(r.secret);
       }) }]);
  }

  function onRemove() {
    Alert.alert("Disconnect Salesforce?",
      "Meetings stop going to Salesforce, and audio links already sent stop working.",
      [{ text: "Cancel", style: "cancel" },
       { text: "Disconnect", style: "destructive", onPress: () => void run("remove", async () => {
         await deleteWebhook(activeId!);
         setSecret("");
         router.back();
       }) }]);
  }

  // Coming soon (internal testing builds): even a direct link to this screen
  // offers nothing to press.
  if (SALESFORCE_COMING_SOON) {
    return (
      <View style={[st.screen, st.body]}>
        <Stack.Screen options={{ title: "Salesforce" }} />
        <Card>
          <Text style={st.strong}>Salesforce is coming soon</Text>
          <Text style={st.help}>Connecting Salesforce isn&apos;t available in this build yet.</Text>
        </Card>
      </View>
    );
  }
  if (loading) return <Loading />;
  const cfg = endpoint?.configuration;

  return (
    <KeyboardAware style={st.screen}>
      <ScrollView style={st.screen} contentContainerStyle={st.body} keyboardShouldPersistTaps="handled">
        <Stack.Screen options={{ title: "Salesforce" }} />
        {error ? <ErrorText>{error}</ErrorText> : null}
        {notice ? <SuccessText>{notice}</SuccessText> : null}

        {/* Behind EXPO_PUBLIC_SALESFORCE_OAUTH: the integration-user login
            the MinuteX package uses; its Finish setup configures the
            receiver the sections below describe. */}
        {SALESFORCE_OAUTH_ENABLED && activeId ? (
          <View style={{ marginBottom: S.lg }}>
            <SalesforceLoginCard workspaceId={activeId} admin={admin}
              receiverReady={endpoint ? endpoint.configured && endpoint.enabled : undefined}
              onChanged={() => { void load(); }} />
          </View>
        ) : null}

        {secret ? (
          <View style={st.secretBox}>
            <Text style={st.warn}>Signing secret — you won&apos;t see this again</Text>
            <Text style={st.secret} selectable>{secret}</Text>
            <Button label="Copy secret" variant="secondary"
              onPress={() => { void Clipboard.setStringAsync(secret); setNotice("Secret copied."); }} />
          </View>
        ) : null}

        {endpoint?.configured ? (
          <>
            <SectionRule>Connection</SectionRule>
            <Card>
              <Text style={st.line}>{testLine(endpoint.last_test)}</Text>
              {admin ? (
                <Button label="Test connection" variant="secondary" style={{ marginTop: S.md }}
                  loading={busy === "test"} disabled={!!busy}
                  onPress={() => void run("test", async () => {
                    await testWebhook(activeId!);
                    await load();
                  })} />
              ) : null}
            </Card>

            <SectionRule>Records users can link to</SectionRule>
            <Card>
              <Text style={st.strong}>{configStatusLine(cfg)}</Text>
              {/* The objects users can link meetings to, and their outputs.
                  Meeting types (the older approach) are not shown. */}
              {cfg?.fetched_at && !Array.isArray(cfg.linkable_objects) ? (
                <Text style={st.typeMeta}>
                  This Salesforce setup doesn&apos;t support linking yet. Your Salesforce admin
                  updates the MinuteX receiver, then refreshes here.
                </Text>
              ) : null}
              {Array.isArray(cfg?.linkable_objects) && cfg.linkable_objects.length === 0 ? (
                  <Text style={st.typeMeta}>Salesforce hasn&apos;t enabled any records to link to.</Text>
                ) : null}
              {(cfg?.linkable_objects ?? []).map((o) => {
                const sum = linkableObjectSummary(o);
                return (
                  <View key={o.object} style={st.type}>
                    <Text style={st.typeName}>{sum.title}</Text>
                    <Text style={st.typeMeta}>{sum.role}</Text>
                    <Text style={st.typeMeta}>Sends: {sum.sends}</Text>
                    <Text style={st.typeMeta}>On by default: {sum.onByDefault}</Text>
                  </View>
                );
              })}
              <Button label="Refresh from Salesforce" variant="secondary" style={{ marginTop: S.md }}
                loading={busy === "refresh"} disabled={!!busy}
                onPress={() => void run("refresh", async () => {
                  const r = await refreshWebhookConfiguration(activeId!);
                  if (r.rate_limited) setNotice("Refreshed moments ago — try again shortly.");
                  await load();
                })} />
            </Card>

            {admin ? (
              <>
                <SectionRule>Security</SectionRule>
                <Button label="Create new signing secret" variant="secondary" onPress={onRotate}
                  loading={busy === "rotate"} disabled={!!busy} />
                <Button label="Disconnect Salesforce" variant="destructive" onPress={onRemove}
                  loading={busy === "remove"} disabled={!!busy} style={{ marginTop: S.md }} />
              </>
            ) : null}
          </>
        ) : null}
      </ScrollView>
    </KeyboardAware>
  );
}
