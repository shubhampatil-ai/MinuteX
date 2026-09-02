// src/app/integrations/gmail.tsx — manage the Gmail connection.
//
// Settings -> Integrations -> Gmail -> Manage.
//
// Deliberately mirrors src/app/salesforce.tsx, the other "one linked external
// thing" screen: hero with status, spec rows, then the management action
// guarded by an Alert that explains the consequence. Following that shape
// rather than inventing a new one means a user who has connected Salesforce
// already knows how this screen works.
//
// WHAT THIS SCREEN MUST BE HONEST ABOUT, because the whole feature's
// trustworthiness rests on it:
//   * WHICH account is connected — an integration that can send mail as you
//     without naming the address is a scary integration.
//   * WHAT it can do — the granted scopes in plain words. MinuteX asks only
//     for send permission, and saying so is the point of asking for so little.
//   * WHAT disconnecting does, and what it does NOT do. Nothing the user
//     created is removed; only the ability to send.
import { useCallback, useMemo, useState } from "react";
import {
  Alert, RefreshControl, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { useFocusEffect } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { CAPS, FONT, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, Card, ErrorText, IconCircle, Loading, Masthead, Row, SectionRule,
} from "../../../lib/ui";
import { ApiError } from "../../../lib/api";
import { useGmail, useIntegrations } from "../../../lib/integrations";

const GMAIL_RED = "#EA4335";

function dateLabel(v?: string): string {
  if (!v) return "—";
  const d = new Date(v);
  return isNaN(d.getTime()) ? v : d.toLocaleDateString();
}

/** Granted OAuth scopes, in words. Deliberately describes the CAPABILITY
 *  rather than echoing the URL: "gmail.send" means nothing to a user, and the
 *  reassuring part — that reading mail is not included — is only visible when
 *  it is spelled out. Unknown scopes fall through to the raw leaf so a future
 *  scope is never silently unlabelled. */
function scopeLabel(scope: string): string {
  if (scope.endsWith("/gmail.send")) return "Send email on your behalf";
  if (scope.endsWith("/userinfo.email")) return "See your email address";
  if (scope === "openid") return "Confirm your identity";
  if (scope.endsWith("/userinfo.profile")) return "See your basic profile";
  return scope.split("/").pop() || scope;
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    hero: { flexDirection: "row" as const, alignItems: "center" as const, gap: 18 },
    heroInfo: { flex: 1 },
    name: { fontFamily: FONT.extrabold, fontSize: 22, color: C.text },
    account: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint, marginTop: 5,
    },
    statusCaps: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3, marginTop: 9,
    },
    blurb: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint,
      marginTop: 12, marginBottom: 14, lineHeight: 18,
    },
    scopeRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 10,
      paddingVertical: 9,
    },
    scopeTxt: { fontFamily: FONT.regular, fontSize: 13, color: C.text, flex: 1 },
    warnNote: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.warn,
      marginTop: 10, lineHeight: 18,
    },
    footnote: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      lineHeight: 17, marginTop: 14, marginBottom: 30,
    },
  });
}

export default function GmailIntegrationScreen() {
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);

  const { refresh, connect, disconnect } = useIntegrations();
  const gmail = useGmail();
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  useFocusEffect(useCallback(() => { refresh(); }, [refresh]));

  const onRefresh = async () => {
    setRefreshing(true);
    await refresh();
    setRefreshing(false);
  };

  const onConnect = async () => {
    setBusy("connect"); setError("");
    const outcome = await connect("gmail");
    setBusy("");
    if (!outcome.ok && !outcome.cancelled) setError(outcome.message);
  };

  const confirmDisconnect = () => {
    Alert.alert(
      "Disconnect Gmail?",
      "MinuteX stops being able to send email from this account. Nothing "
      + "already sent is affected, and your meetings, contacts, tasks and "
      + "documents stay exactly as they are.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Disconnect", style: "destructive",
          onPress: async () => {
            setBusy("disconnect"); setError("");
            try {
              await disconnect("gmail");
            } catch (e) {
              setError(e instanceof ApiError
                ? e.message : "Couldn’t disconnect Gmail. Try again.");
            } finally {
              setBusy("");
            }
          },
        },
      ]
    );
  };

  const integration = gmail.integration;

  if (gmail.loading && !integration) {
    return (
      <View style={st.container}>
        <Loading label="Checking Gmail…" />
      </View>
    );
  }

  const connected = gmail.usable;
  const statusColor = connected ? C.success
    : gmail.needsReauth ? C.warn
      : gmail.status === "ERROR" ? C.danger : C.textFaint;
  const statusText = connected ? "Connected"
    : gmail.needsReauth ? "Reconnect required"
      : gmail.status === "ERROR" ? "Needs attention" : "Not connected";

  const scopes = integration?.scopes ?? [];

  return (
    <ScrollView
      style={st.container}
      contentContainerStyle={{ paddingBottom: insets.bottom + 30 }}
      showsVerticalScrollIndicator={false}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.textFaint} />
      }
    >
      <Masthead
        kicker={connected ? "Linked to your account" : "Not connected"}
        title="Gmail"
      />

      <Card>
        <View style={st.hero}>
          <IconCircle name="envelope.badge" size={54} tint={GMAIL_RED}
            bg={`${GMAIL_RED}1A`} />
          <View style={st.heroInfo}>
            <Text style={st.name}>Gmail</Text>
            {integration?.account_identifier ? (
              <Text style={st.account}>{integration.account_identifier}</Text>
            ) : null}
            <Text style={[st.statusCaps, { color: statusColor }]}>{statusText}</Text>
          </View>
        </View>

        <Text style={st.blurb}>
          Send Minutes of Meeting, summaries, action items and follow-ups to
          participants from your own Gmail address.
        </Text>

        {/* Why an action the user had yesterday is gone today. */}
        {gmail.needsReauth || gmail.status === "ERROR" ? (
          <Text style={st.warnNote}>
            {integration?.message
              || "Reconnect Gmail to keep sending from this account."}
          </Text>
        ) : null}

        {error ? <ErrorText>{error}</ErrorText> : null}

        {connected ? (
          <Button
            label="Disconnect Gmail"
            variant="danger"
            loading={busy === "disconnect"}
            onPress={confirmDisconnect}
            style={{ marginTop: 6 }}
          />
        ) : (
          <Button
            label={gmail.needsReauth ? "Reconnect Gmail" : "Connect Gmail"}
            loading={busy === "connect"}
            onPress={onConnect}
            style={{ marginTop: 6 }}
          />
        )}
      </Card>

      {connected ? (
        <>
          <SectionRule>Connection</SectionRule>
          <Row label="Account" value={integration?.account_identifier || "—"} />
          {integration?.account_name ? (
            <Row label="Name" value={integration.account_name} />
          ) : null}
          <Row label="Connected" value={dateLabel(integration?.connected_at)} numeric />

          <SectionRule>What MinuteX can do</SectionRule>
          {scopes.length ? scopes.map((scope) => (
            <View key={scope} style={st.scopeRow}>
              <IconCircle name="checkmark" size={22} iconSize={12}
                tint={C.success} bg={C.successSoft} />
              <Text style={st.scopeTxt}>{scopeLabel(scope)}</Text>
            </View>
          )) : (
            <Text style={st.footnote}>
              MinuteX can send email on your behalf.
            </Text>
          )}
          <Text style={st.footnote}>
            MinuteX cannot read, search or open your inbox — it never asks for
            permission to. Disconnecting removes MinuteX&apos;s access and
            deletes the stored credential; nothing you created in MinuteX is
            removed.
          </Text>
        </>
      ) : (
        <Text style={st.footnote}>
          Connecting opens Google&apos;s own sign-in page. MinuteX never sees
          your Google password, and requests only permission to send email and
          to read which address you connected.
        </Text>
      )}
    </ScrollView>
  );
}
