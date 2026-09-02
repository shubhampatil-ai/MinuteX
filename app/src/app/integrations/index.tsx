// src/app/integrations.tsx — Settings -> Integrations.
//
// The central place a user connects MinuteX to other applications. Gmail is
// the only one that can be connected in this phase; the rest are honest
// "Coming Soon" cards, which is the same convention settings.tsx already uses
// for planned features (ComingSoonRow / SoonBadge).
//
// THE CARD LIST COMES FROM THE SERVER. It is not a constant in this file. The
// backend's PROVIDERS registry decides which integrations exist and which are
// available, so the day WhatsApp ships every installed build shows it as
// connectable — and, more importantly, an old build can never offer a Connect
// button for something the backend cannot yet honour.
//
// STATUS IS READ, NEVER INFERRED. Each card renders the backend's status
// verbatim through useIntegrations(), including REAUTH_REQUIRED, which is a
// real state and not "connected with a warning": those actions are unavailable
// until the user reconnects.
//
// LOGOS are the vendors' own marks, rendered as SVG by
// lib/integration-logos.tsx. A brand is recognised by its exact artwork, so an
// approximation drawn from primitives would be both uglier and a redrawing of
// someone else's trademark. That module carries the licensing note; this one
// just asks it for a mark by provider id and lets it handle a provider this
// build has never heard of (the catalog is server-driven, so that happens).
import { useCallback, useMemo, useState } from "react";
import {
  ActivityIndicator, Pressable, RefreshControl, ScrollView,
  StyleSheet, Text, View,
} from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { S, R, CAPS, FONT, useTheme, ColorScale } from "../../../lib/theme";
import { ErrorText, Masthead, SoonBadge } from "../../../lib/ui";
import { IntegrationLogo } from "../../../lib/integration-logos";
import { Integration } from "../../../lib/api";
import { useIntegrations } from "../../../lib/integrations";

// The logo tile sits on a neutral surface rather than a per-brand tinted one.
// With real multi-colour marks a tinted backdrop fights the artwork — Gmail's
// red wash behind a red-and-blue envelope reads as a rendering bug — and a
// single quiet tile is also what lets five different brands sit in one list
// without the page looking like a paint chart.

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    blurb: {
      fontFamily: FONT.regular, fontSize: 13, color: C.textDim,
      lineHeight: 19, marginBottom: S.lg,
    },
    card: {
      borderWidth: 1, borderColor: C.border, borderRadius: R.md,
      backgroundColor: C.surface, padding: 16, marginBottom: 12,
    },
    cardHead: { flexDirection: "row" as const, alignItems: "center" as const, gap: 13 },
    logoTile: {
      width: 44, height: 44, borderRadius: 12, backgroundColor: C.surface2,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    name: { fontFamily: FONT.bold, fontSize: 15.5, color: C.text },
    desc: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim,
      lineHeight: 18, marginTop: 10,
    },
    account: {
      fontFamily: FONT.medium, fontSize: 12.5, color: C.text, marginTop: 8,
    },
    statusRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const, marginTop: 14,
    },
    statusCaps: { ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3 },
    action: {
      borderRadius: R.pill, paddingVertical: 9, paddingHorizontal: 18,
      alignItems: "center" as const, justifyContent: "center" as const,
      minWidth: 104,
    },
    actionPrimary: { backgroundColor: C.primary },
    actionQuiet: { borderWidth: 1, borderColor: C.border, backgroundColor: C.surface },
    actionTxtPrimary: { fontFamily: FONT.bold, fontSize: 13, color: C.textOnPrimary },
    actionTxtQuiet: { fontFamily: FONT.bold, fontSize: 13, color: C.text },
    warnNote: {
      fontFamily: FONT.regular, fontSize: 12, color: C.warn,
      marginTop: 8, lineHeight: 17,
    },
    soon: { opacity: 0.55 },
    footnote: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      lineHeight: 17, marginTop: 6, marginBottom: 30,
    },
  });
}

export default function IntegrationsScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);

  const { integrations, loading, error, refresh, connect } = useIntegrations();
  const [busy, setBusy] = useState("");
  const [connectError, setConnectError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  // Re-read on every return to this screen — coming back from the Gmail
  // Manage screen must show the new state, and a connection can also change
  // on another device.
  useFocusEffect(useCallback(() => { refresh(); }, [refresh]));

  const onRefresh = async () => {
    setRefreshing(true);
    await refresh();
    setRefreshing(false);
  };

  const onConnect = async (provider: string) => {
    setBusy(provider);
    setConnectError("");
    const outcome = await connect(provider);
    setBusy("");
    // Backing out of the browser is a normal choice — don't shout about it.
    if (!outcome.ok && !outcome.cancelled) setConnectError(outcome.message);
  };

  const openManage = (provider: string) => {
    if (provider === "gmail") { router.push("/integrations/gmail"); return; }
    // Salesforce predates this system and keeps its own screen (see
    // managed_elsewhere in shared/integrations.py). Routing there rather than
    // duplicating a manage screen is the whole reason that flag exists.
    if (provider === "salesforce") { router.push("/salesforce"); return; }
  };

  return (
    <ScrollView
      style={st.container}
      contentContainerStyle={{ paddingBottom: insets.bottom + 30 }}
      showsVerticalScrollIndicator={false}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.textFaint} />
      }
    >
      <Masthead kicker="Settings" title="Integrations" />
      <Text style={st.blurb}>
        Connect MinuteX with your favourite applications.
      </Text>

      {connectError ? <ErrorText>{connectError}</ErrorText> : null}
      {error && !integrations.length ? <ErrorText>{error}</ErrorText> : null}

      {loading && !integrations.length ? (
        <View style={{ paddingVertical: 40, alignItems: "center" }}>
          <ActivityIndicator color={C.textFaint} />
        </View>
      ) : null}

      {integrations.map((it) => (
        <IntegrationCard
          key={it.provider}
          integration={it}
          busy={busy === it.provider}
          onConnect={() => onConnect(it.provider)}
          onManage={() => openManage(it.provider)}
        />
      ))}

      <Text style={st.footnote}>
        MinuteX never stores your password for a connected application, and
        your recordings, contacts and tasks stay in your account whether an
        integration is connected or not.
      </Text>
    </ScrollView>
  );
}

function IntegrationCard({
  integration, busy, onConnect, onManage,
}: {
  integration: Integration;
  busy: boolean;
  onConnect: () => void;
  onManage: () => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);

  // "Coming soon" is about AVAILABILITY, not about which flow connects it.
  // Salesforce is available and managed_elsewhere, so it is a working card
  // that happens to open its own screen — never a dimmed placeholder.
  const soon = !integration.available;

  const { label, color } = statusLabel(integration, C);

  return (
    <View style={[st.card, soon && st.soon]}>
      <View style={st.cardHead}>
        <View style={st.logoTile}>
          <IntegrationLogo provider={integration.provider} size={26} muted={soon} />
        </View>
        <View style={{ flex: 1 }}>
          <Text style={st.name}>{integration.name}</Text>
          <Text style={[st.statusCaps, { color, marginTop: 5 }]}>{label}</Text>
        </View>
        {soon ? <SoonBadge /> : null}
      </View>

      <Text style={st.desc}>{integration.description}</Text>

      {/* The connected account, where there is one. Showing WHICH account is
          linked is what makes the integration trustworthy rather than opaque. */}
      {integration.connected && integration.account_identifier ? (
        <Text style={st.account}>{integration.account_identifier}</Text>
      ) : null}

      {/* A reauth reason is worth its own line: it explains why an action the
          user had yesterday is gone today. */}
      {integration.status === "REAUTH_REQUIRED" || integration.status === "ERROR" ? (
        <Text style={st.warnNote}>
          {integration.message || "Reconnect to continue using this integration."}
        </Text>
      ) : null}

      {soon ? null : (
        <View style={st.statusRow}>
          <View />
          <CardAction
            integration={integration}
            busy={busy}
            onConnect={onConnect}
            onManage={onManage}
          />
        </View>
      )}
    </View>
  );
}

function CardAction({
  integration, busy, onConnect, onManage,
}: {
  integration: Integration; busy: boolean;
  onConnect: () => void; onManage: () => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);

  // Salesforce: its own screen owns BOTH connecting and managing, so the tap
  // goes there either way. The LABEL still tracks real status rather than
  // saying a generic "Open" — a card that reads "Not connected" next to a
  // button that says "Open" makes the user guess what the button will do.
  if (integration.managed_elsewhere) {
    return (
      <Pressable onPress={onManage} style={({ pressed }) => [
        st.action, integration.connected ? st.actionQuiet : st.actionPrimary,
        pressed && { opacity: 0.6 },
      ]}>
        <Text style={integration.connected ? st.actionTxtQuiet : st.actionTxtPrimary}>
          {integration.connected ? "Manage" : `Connect ${integration.name}`}
        </Text>
      </Pressable>
    );
  }

  if (integration.connected) {
    return (
      <Pressable onPress={onManage} style={({ pressed }) => [
        st.action, st.actionQuiet, pressed && { opacity: 0.6 },
      ]}>
        <Text style={st.actionTxtQuiet}>Manage</Text>
      </Pressable>
    );
  }

  // NOT_CONNECTED, REAUTH_REQUIRED and ERROR all lead to the same place — the
  // consent flow — but the WORD matters: "Reconnect" tells a user who already
  // set this up that nothing they did was lost.
  const verb = integration.status === "NOT_CONNECTED"
    ? `Connect ${integration.name}`
    : "Reconnect";

  return (
    <Pressable
      onPress={onConnect}
      disabled={busy}
      accessibilityRole="button"
      accessibilityState={{ disabled: busy }}
      style={({ pressed }) => [
        st.action, st.actionPrimary,
        (pressed || busy) && { opacity: 0.7 },
      ]}
    >
      {busy
        ? <ActivityIndicator size="small" color={C.textOnPrimary} />
        : <Text style={st.actionTxtPrimary}>{verb}</Text>}
    </Pressable>
  );
}

/** The one place a status becomes words and a colour, so every card — and the
 *  Manage screen — says the same thing about the same state. */
export function statusLabel(
  integration: Integration, C: ColorScale
): { label: string; color: string } {
  if (!integration.available) {
    return { label: "Coming soon", color: C.textFaint };
  }
  switch (integration.status) {
    case "CONNECTED":
      return { label: "Connected", color: C.success };
    case "REAUTH_REQUIRED":
      return { label: "Reconnect required", color: C.warn };
    case "ERROR":
      return { label: "Needs attention", color: C.danger };
    default:
      return { label: "Not connected", color: C.textFaint };
  }
}
