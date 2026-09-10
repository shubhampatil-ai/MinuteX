// src/app/integrations/index.tsx — Settings -> Connected Apps, AND
// Organisation -> Organisation Integrations. One screen, two entry points
// (settings.tsx and organisation/index.tsx both router.push("/integrations")
// — see those files), rendering two DELIBERATELY DIFFERENT views depending
// on the active workspace, so the same route never reads as "the same
// screen twice" no matter which way the user arrived:
//
//   PERSONAL (useWorkspace().isOrganisation === false): unchanged from
//   before this file gained organisation-awareness — the full personal
//   catalog (Gmail, Salesforce, and every server-listed provider), because
//   this IS "my personal/user-level connected applications."
//
//   ORGANISATION: shows ONLY what is relevant to the active organisation —
//   the Organisation Salesforce connection (workspace-owned, see
//   OrgSalesforceCard) and Gmail (the SAME per-user connection Personal
//   already shows, re-labelled here as "Your Gmail account" / "used for
//   organisation actions" — never a second OAuth connection, never a
//   shared organisation mailbox). Every other personal-only provider
//   (Google Calendar, Google Tasks, WhatsApp, Outlook, ...) is hidden here:
//   an organisation member switching into a workspace should see what THIS
//   organisation uses, not their own unrelated personal connections mixed
//   in — that mixing is exactly the confusion this split exists to remove.
//
// GMAIL IS USER-OWNED, NOT ORGANISATION-OWNED. There is exactly one Gmail
// connection per MinuteX user (Integrations table, keyed by user_id only —
// see cloud/shared/integrations.py). Showing it here is the SAME
// useIntegrations() catalog Personal reads, not a second fetch and not a
// second connection — switching workspace changes what this screen shows,
// never what is connected. Sending email from an organisation meeting/task
// already only ever used the sending user's own Gmail token (see
// gmail_send_meeting/gmail_send_task in lambda_function.py) — this screen
// now just says that honestly instead of leaving it unlabelled.
//
// SALESFORCE IS ORGANISATION-OWNED. OrgCrmConnections is keyed by
// workspace_id, entirely separate from CrmConnections (Personal, keyed by
// user_id) — see org-salesforce.tsx and shared/integrations.py. The
// Personal Salesforce card is never shown in Organisation mode, and the
// Organisation Salesforce card is never shown in Personal mode: showing
// both together (as this screen briefly did) was itself the source of the
// "which Salesforce is this" confusion.
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
import { ErrorText, Masthead, SectionRule, SoonBadge } from "../../../lib/ui";
import { IntegrationLogo } from "../../../lib/integration-logos";
import { Integration, OrgSalesforceStatus, getOrgSalesforceStatus } from "../../../lib/api";
import { useGmail, useIntegrations } from "../../../lib/integrations";
import { useWorkspace, workspaceLabel } from "../../../lib/workspace-context";

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
    scopeBadge: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9, letterSpacing: 1,
      color: C.textFaint, marginTop: 2,
    },
  });
}

// Category -> section title, in the order sections render. Mirrors
// shared/integrations.py's PROVIDERS categories ("crm", "communication",
// "productivity") — a new category the backend introduces falls into
// OTHER_CATEGORY below rather than disappearing.
const SECTIONS: { category: string; title: string }[] = [
  { category: "crm", title: "CRM" },
  { category: "communication", title: "Email" },
  { category: "productivity", title: "Calendar" },
];
const OTHER_CATEGORY = "Other";

export default function IntegrationsScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);

  const { integrations, loading, error, refresh, connect } = useIntegrations();
  const { active, isOrganisation, loading: wsLoading } = useWorkspace();
  const [busy, setBusy] = useState("");
  const [connectError, setConnectError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  // The ORGANISATION Salesforce card — a second, independent status fetched
  // alongside the personal catalog above rather than merged into it (see the
  // module note in shared/integrations.py's public_org_salesforce_status).
  // Only fetched when the active workspace IS an organisation: a personal
  // workspace has no organisation connection to show, and calling the route
  // for one would only ever answer "not connected" for a card this screen
  // should not render at all.
  const [orgSalesforce, setOrgSalesforce] = useState<OrgSalesforceStatus | null>(null);
  const [orgSalesforceLoading, setOrgSalesforceLoading] = useState(false);

  const loadOrgSalesforce = useCallback(async () => {
    if (!isOrganisation || !active) { setOrgSalesforce(null); return; }
    setOrgSalesforceLoading(true);
    try {
      setOrgSalesforce(await getOrgSalesforceStatus(active.workspace_id));
    } catch {
      // Degrade to "not shown" rather than breaking the whole screen — the
      // Personal cards above must still render.
      setOrgSalesforce(null);
    } finally {
      setOrgSalesforceLoading(false);
    }
  }, [isOrganisation, active]);

  // Re-read on every return to this screen — coming back from the Gmail
  // Manage screen (or org-salesforce.tsx) must show the new state, and a
  // connection can also change on another device.
  useFocusEffect(useCallback(() => { refresh(); void loadOrgSalesforce(); }, [refresh, loadOrgSalesforce]));

  const onRefresh = async () => {
    setRefreshing(true);
    await Promise.all([refresh(), loadOrgSalesforce()]);
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

  const grouped = useMemo(() => {
    const byCategory = new Map<string, Integration[]>();
    for (const it of integrations) {
      const list = byCategory.get(it.category) ?? [];
      list.push(it);
      byCategory.set(it.category, list);
    }
    return byCategory;
  }, [integrations]);

  const knownCategories = new Set(SECTIONS.map((s) => s.category));
  const other = integrations.filter((it) => !knownCategories.has(it.category));

  const gmail = useGmail();

  const loadingBody = (loading && !integrations.length) || wsLoading;

  return (
    <ScrollView
      style={st.container}
      contentContainerStyle={{ paddingBottom: insets.bottom + 30 }}
      showsVerticalScrollIndicator={false}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.textFaint} />
      }
    >
      {isOrganisation ? (
        <>
          <Masthead kicker={workspaceLabel(active)} title="Organisation Integrations" />
          <Text style={st.blurb}>Apps and services used with this organisation.</Text>

          {connectError ? <ErrorText>{connectError}</ErrorText> : null}
          {error && !integrations.length ? <ErrorText>{error}</ErrorText> : null}

          {loadingBody ? (
            <View style={{ paddingVertical: 40, alignItems: "center" }}>
              <ActivityIndicator color={C.textFaint} />
            </View>
          ) : (
            <>
              <SectionRule>CRM</SectionRule>
              <OrgSalesforceCard
                workspaceName={workspaceLabel(active)}
                status={orgSalesforce}
                loading={orgSalesforceLoading}
                onPress={() => router.push("/org-salesforce")}
              />

              <SectionRule>Communication</SectionRule>
              <OrgGmailCard
                integration={gmail.integration}
                loading={gmail.loading}
                busy={busy === "gmail"}
                onConnect={() => onConnect("gmail")}
                onManage={() => openManage("gmail")}
              />
            </>
          )}

          <Text style={st.footnote}>
            Salesforce here is this organisation&apos;s own connection, managed by an
            owner or manager. Gmail uses your own personal connection — the same one
            shown in Settings — so email always sends from your address, never a
            shared organisation mailbox.
          </Text>
        </>
      ) : (
        <>
          <Masthead kicker="Personal" title="Connected Apps" />
          <Text style={st.blurb}>
            Apps connected to your personal MinuteX account.
          </Text>

          {connectError ? <ErrorText>{connectError}</ErrorText> : null}
          {error && !integrations.length ? <ErrorText>{error}</ErrorText> : null}

          {loadingBody ? (
            <View style={{ paddingVertical: 40, alignItems: "center" }}>
              <ActivityIndicator color={C.textFaint} />
            </View>
          ) : (
            <>
              {SECTIONS.map(({ category, title }) => {
                const items = grouped.get(category) ?? [];
                if (!items.length) return null;
                return (
                  <View key={category}>
                    <SectionRule>{title}</SectionRule>
                    {items.map((it) => (
                      <IntegrationCard
                        key={it.provider}
                        integration={it}
                        busy={busy === it.provider}
                        onConnect={() => onConnect(it.provider)}
                        onManage={() => openManage(it.provider)}
                      />
                    ))}
                  </View>
                );
              })}

              {other.length ? (
                <View>
                  <SectionRule>{OTHER_CATEGORY}</SectionRule>
                  {other.map((it) => (
                    <IntegrationCard
                      key={it.provider}
                      integration={it}
                      busy={busy === it.provider}
                      onConnect={() => onConnect(it.provider)}
                      onManage={() => openManage(it.provider)}
                    />
                  ))}
                </View>
              ) : null}
            </>
          )}

          <Text style={st.footnote}>
            MinuteX never stores your password for a connected application, and
            your recordings, contacts and tasks stay in your account whether an
            integration is connected or not.
          </Text>
        </>
      )}
    </ScrollView>
  );
}

/** The Organisation Salesforce card — same visual language as
 *  IntegrationCard below, but reading OrgSalesforceStatus (a second,
 *  independent connection) rather than the personal Integration catalog.
 *  Kept as its own small component rather than forcing OrgSalesforceStatus
 *  into the Integration shape: the two have genuinely different fields
 *  (`configured`, `config_cleared_reason`, `connected_by_user_id` have no
 *  Personal equivalent), and coercing one into the other would be the kind
 *  of union-type nobody can reason about that shared/integrations.py's own
 *  docstring warns against. */
function OrgSalesforceCard({
  workspaceName, status, loading, onPress,
}: {
  workspaceName: string;
  status: OrgSalesforceStatus | null;
  loading: boolean;
  onPress: () => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const connected = status?.connected === true;
  const label = loading ? "Checking…" : connected ? "Connected" : "Not connected";
  const color = loading ? C.textFaint : connected ? C.success : C.textFaint;

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [st.card, pressed && { opacity: 0.85 }]}
    >
      <View style={st.cardHead}>
        <View style={st.logoTile}>
          <IntegrationLogo provider="salesforce" size={26} />
        </View>
        <View style={{ flex: 1 }}>
          <Text style={st.name}>Salesforce</Text>
          <Text style={st.scopeBadge}>Organisation connection</Text>
          <Text style={[st.statusCaps, { color, marginTop: 5 }]}>{label}</Text>
        </View>
      </View>

      <Text style={st.desc}>
        Push {workspaceName}&apos;s meeting notes onto your organisation&apos;s CRM records.
      </Text>

      {connected && status?.sf_username ? (
        <Text style={st.account}>{status.sf_username}</Text>
      ) : null}

      {/* "Managed by organisation" — always true for this card (an
          Owner/Manager connects/configures/disconnects it, never a
          per-member action), so it is shown regardless of who is looking,
          the same way the label on the card itself never changes by role. */}
      {connected ? <Text style={st.scopeBadge}>Managed by organisation</Text> : null}

      {connected && status?.config_cleared_reason ? (
        <Text style={st.warnNote}>{status.config_cleared_reason}</Text>
      ) : null}

      <View style={st.statusRow}>
        <View />
        <View style={[st.action, connected ? st.actionQuiet : st.actionPrimary]}>
          <Text style={connected ? st.actionTxtQuiet : st.actionTxtPrimary}>
            {connected ? "Manage" : "Connect Salesforce"}
          </Text>
        </View>
      </View>
    </Pressable>
  );
}

/** The Organisation Integrations screen's Gmail card. Reads the SAME
 *  useGmail()/Integration status Personal's IntegrationCard reads — this is
 *  not a second connection or a second fetch, only different copy, because
 *  Gmail is user-owned even when the context is an organisation (see the
 *  module docstring). Tapping it goes to the exact same Manage screen
 *  Personal uses (/integrations/gmail) — there is nothing organisation-
 *  specific to configure, since there is no organisation-level Gmail
 *  connection to configure. */
function OrgGmailCard({
  integration, loading, busy, onConnect, onManage,
}: {
  integration: Integration | null;
  loading: boolean;
  busy: boolean;
  onConnect: () => void;
  onManage: () => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const connected = integration?.status === "CONNECTED";
  const needsReauth = integration?.status === "REAUTH_REQUIRED"
    || integration?.status === "ERROR";
  const label = loading ? "Checking…"
    : connected ? "Connected"
      : needsReauth ? "Reconnect required" : "Not connected";
  const color = loading ? C.textFaint
    : connected ? C.success
      : needsReauth ? C.warn : C.textFaint;

  return (
    <View style={st.card}>
      <View style={st.cardHead}>
        <View style={st.logoTile}>
          <IntegrationLogo provider="gmail" size={26} />
        </View>
        <View style={{ flex: 1 }}>
          <Text style={st.name}>Gmail</Text>
          <Text style={st.scopeBadge}>Your Gmail account</Text>
          <Text style={[st.statusCaps, { color, marginTop: 5 }]}>{label}</Text>
        </View>
      </View>

      <Text style={st.desc}>
        Used for sending from your own account — never a shared organisation
        mailbox. Only you can manage this connection.
      </Text>

      {connected && integration?.account_identifier ? (
        <Text style={st.account}>Sending as {integration.account_identifier}</Text>
      ) : null}

      {needsReauth ? (
        <Text style={st.warnNote}>
          {integration?.message || "Reconnect Gmail to keep sending from your account."}
        </Text>
      ) : null}

      <View style={st.statusRow}>
        <View />
        <Pressable
          onPress={connected ? onManage : onConnect}
          disabled={busy}
          accessibilityRole="button"
          accessibilityState={{ disabled: busy }}
          style={({ pressed }) => [
            st.action, connected ? st.actionQuiet : st.actionPrimary,
            (pressed || busy) && { opacity: 0.7 },
          ]}
        >
          {busy
            ? <ActivityIndicator size="small" color={connected ? C.text : C.textOnPrimary} />
            : (
              <Text style={connected ? st.actionTxtQuiet : st.actionTxtPrimary}>
                {connected ? "Manage" : needsReauth ? "Reconnect Gmail" : "Connect Gmail"}
              </Text>
            )}
        </Pressable>
      </View>
    </View>
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
