// src/app/org-salesforce.tsx — connect / disconnect the ORGANISATION's
// Salesforce connection (Phase 2D.1/2D.2). The workspace-scoped twin of
// salesforce.tsx (Personal) — deliberately a SEPARATE screen rather than
// branching logic inside that one, so Personal Salesforce stays exactly as
// it was: same file, same behaviour, zero risk of a workspace conditional
// leaking into a path Personal already relies on.
//
// SCOPE. This screen only ever acts on the CURRENTLY ACTIVE workspace (via
// useWorkspace().activeId) and only renders when that workspace is an
// organisation — see the guard right after the hooks. It never reads or
// writes Personal's CrmConnections; every call here is one of the
// workspace-scoped functions in lib/api.ts (getOrgSalesforceStatus etc.),
// which address `/workspaces/{id}/crm/salesforce/...` explicitly.
//
// RBAC IS DISPLAY-ONLY HERE, ENFORCED SERVER-SIDE THERE. `can("manage_
// integrations")` decides whether the Connect/Disconnect/Configure actions
// render as usable; the backend re-checks CAP_MANAGE_INTEGRATIONS on every
// one of those requests regardless of what this screen shows. A Member who
// somehow triggers the action anyway gets the same 403 the backend gives
// anyone else — this screen just avoids showing them a button that would
// only fail.
import { useCallback, useMemo, useState } from "react";
import { Alert, RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import * as Linking from "expo-linking";
import * as WebBrowser from "expo-web-browser";
import { S, CAPS, FONT, useTheme, ColorScale } from "../../lib/theme";
import {
  Button, Card, ErrorText, IconCircle, ListRow, Loading, Masthead, Row,
  SectionRule,
} from "../../lib/ui";
import {
  ApiError, OrgSalesforceStatus, SalesforceConfig, clearToken,
  disconnectOrgSalesforce, getOrgSalesforceAuthorizeUrl, getOrgSalesforceConfig,
  getOrgSalesforceStatus, isSalesforceReconnect,
} from "../../lib/api";
import { useWorkspace, workspaceLabel } from "../../lib/workspace-context";

// Same deep link Personal uses (crm-connected.tsx branches on the `scope`
// query param the backend's org callback adds) — one landing route serves
// both flows rather than a second copy of the same redirect-handling logic.
const RETURN_PATH = "crm-connected";

type ConnectOutcome =
  | { ok: true }
  | { ok: false; cancelled: true }
  | { ok: false; cancelled?: false; message: string };

const REASON_COPY: Record<string, string> = {
  denied: "Salesforce access was declined.",
  missing_params: "Salesforce didn’t return a valid response. Try again.",
  exchange_failed: "Couldn’t complete the Salesforce connection. Try again.",
  pkce_missing: "That Salesforce sign-in link expired. Try again.",
  not_authorized: "You no longer have permission to connect Salesforce for this workspace.",
  expired: "That Salesforce sign-in link expired. Try again.",
};

async function connectOrgSalesforce(workspaceId: string): Promise<ConnectOutcome> {
  const returnUrl = Linking.createURL(RETURN_PATH);
  let authorizeUrl: string;
  try {
    authorizeUrl = await getOrgSalesforceAuthorizeUrl(workspaceId);
  } catch (e: any) {
    return { ok: false, message: e?.message ?? "Couldn’t start the Salesforce connection." };
  }
  const result = await WebBrowser.openAuthSessionAsync(authorizeUrl, returnUrl);
  if (result.type !== "success") return { ok: false, cancelled: true };

  const { queryParams } = Linking.parse(result.url);
  if (queryParams?.connected === "1") return { ok: true };
  const reason = typeof queryParams?.reason === "string" ? queryParams.reason : "";
  return {
    ok: false,
    message: REASON_COPY[reason] ?? "Couldn’t complete the Salesforce connection. Try again.",
  };
}

function dateLabel(v?: string): string {
  if (!v) return "—";
  const d = new Date(v);
  return isNaN(d.getTime()) ? v : d.toLocaleDateString();
}

function hostOf(url?: string): string {
  if (!url) return "—";
  return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    hero: { flexDirection: "row" as const, alignItems: "center" as const, gap: 18 },
    heroInfo: { flex: 1 },
    name: { fontFamily: FONT.extrabold, fontSize: 22, color: C.text },
    org: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 5 },
    statusCaps: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3, marginTop: 9,
    },
    blurb: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint,
      marginTop: 4, marginBottom: 14, lineHeight: 18,
    },
    statusOk: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3, color: C.success,
    },
    statusTodo: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3, color: C.warn,
    },
    noticeCard: {
      borderWidth: 1, borderColor: C.warn, borderRadius: 12, padding: 12,
      marginTop: S.md, backgroundColor: C.warnSoft,
    },
    noticeText: {
      fontFamily: FONT.medium, fontSize: 12.5, color: C.text, lineHeight: 18,
    },
  });
}

export default function OrgSalesforceScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);
  const { active, activeId, isOrganisation, can, loading: wsLoading } = useWorkspace();

  const [info, setInfo] = useState<OrgSalesforceStatus | null>(null);
  const [config, setConfig] = useState<SalesforceConfig | null>(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState("");

  const canManage = can("manage_integrations");

  const load = useCallback(async () => {
    if (!activeId || !isOrganisation) { setLoading(false); return; }
    setError("");
    try {
      const status = await getOrgSalesforceStatus(activeId);
      setInfo(status);
      if (!status.connected) {
        setConfig(null);
        setStale(false);
        return;
      }
      try {
        setConfig(await getOrgSalesforceConfig(activeId));
        setStale(false);
      } catch (e) {
        setConfig(null);
        setStale(isSalesforceReconnect(e));
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        await clearToken(); router.replace("/login"); return;
      }
      setError(e instanceof ApiError ? e.message : "Could not load the Salesforce connection.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [router, activeId, isOrganisation]);

  useFocusEffect(useCallback(() => { load(); }, [load]));

  const onConnect = async () => {
    if (!activeId) return;
    setBusy("connect"); setError("");
    const outcome = await connectOrgSalesforce(activeId);
    setBusy("");
    if (!outcome.ok && !outcome.cancelled) setError(outcome.message);
    if (outcome.ok) await load();
  };

  const confirmDisconnect = () => {
    Alert.alert(
      `Disconnect Salesforce for ${workspaceLabel(active)}?`,
      "MinuteX stops being able to push this organisation's meetings to "
      + "this org. Nothing already pushed to Salesforce is removed, and "
      + "your Personal Salesforce connection (if any) is unaffected.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Disconnect", style: "destructive",
          onPress: async () => {
            if (!activeId) return;
            setBusy("disconnect"); setError("");
            try {
              await disconnectOrgSalesforce(activeId);
              await load();
            } catch (e) {
              setError(e instanceof ApiError ? e.message : "Could not disconnect Salesforce.");
            } finally {
              setBusy("");
            }
          },
        },
      ]
    );
  };

  // Not an organisation workspace (or workspace still loading) — this
  // screen has nothing to show. Personal Salesforce lives at /salesforce;
  // routing here for a personal workspace would be a caller bug, not a
  // state this screen should try to render.
  if (wsLoading || loading) {
    return (
      <View style={[st.container, { paddingTop: insets.top + S.lg }]}>
        <Loading label="Checking Salesforce…" />
      </View>
    );
  }
  if (!isOrganisation) {
    return (
      <View style={[st.container, { paddingTop: insets.top + S.lg }]}>
        <Masthead kicker="Organisation" title="Salesforce" />
        <ErrorText>
          Switch to an organisation workspace to manage its Salesforce connection.
        </ErrorText>
      </View>
    );
  }

  const connected = info?.connected === true;

  return (
    <ScrollView
      style={[st.container, { paddingTop: insets.top + S.lg }]}
      contentContainerStyle={{ paddingBottom: 100 }}
      showsVerticalScrollIndicator={false}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={() => { setRefreshing(true); load(); }}
          tintColor={C.primary}
        />
      }
    >
      <Masthead
        kicker={connected ? `Linked to ${workspaceLabel(active)}` : `Not connected · ${workspaceLabel(active)}`}
        title="Salesforce"
      />
      {error ? <ErrorText>{error}</ErrorText> : null}

      <Card style={{ marginTop: S.lg }}>
        <View style={st.hero}>
          <IconCircle
            name="cloud.fill"
            size={62}
            tint={connected ? C.primary : C.textFaint}
            bg={connected ? C.primarySoft : C.surface2}
          />
          <View style={st.heroInfo}>
            <Text style={st.name} numberOfLines={2}>
              {connected ? hostOf(info?.instance_url) : "Salesforce"}
            </Text>
            <Text style={st.org} numberOfLines={1}>
              {connected
                ? info?.sf_username || "—"
                : `Push ${workspaceLabel(active)}'s meetings into your CRM`}
            </Text>
            <Text style={[st.statusCaps, { color: connected ? C.success : C.textFaint }]}>
              {connected ? "CONNECTED" : "NOT CONNECTED"}
            </Text>
          </View>
        </View>

        {connected ? (
          <View style={{ marginTop: S.lg, paddingTop: S.sm, borderTopWidth: 1, borderTopColor: C.border }}>
            <Row label="Organisation" value={workspaceLabel(active)} />
            <Row label="Salesforce org" value={hostOf(info?.instance_url)} />
            <Row label="Salesforce user" value={info?.sf_username || "—"} />
            <Row label="Connected" value={dateLabel(info?.connected_at)} />
          </View>
        ) : null}
      </Card>

      {connected && info?.config_cleared_reason ? (
        <View style={st.noticeCard}>
          <Text style={st.noticeText}>{info.config_cleared_reason}</Text>
        </View>
      ) : null}

      {connected ? (
        <>
          <SectionRule>Configuration</SectionRule>
          <ListRow
            icon="slider.horizontal.3"
            label="Salesforce mapping"
            sub={
              stale
                ? "Reconnect Salesforce to change the mapping"
                : config?.mappings?.length
                  ? config.mappings.map((m) => m.object_label || m.object).join(" · ")
                  : canManage
                    ? "Choose which records to link meetings to"
                    : "Not configured yet — ask an owner or manager"
            }
            right={
              stale
                ? <Text style={st.statusTodo}>Reconnect</Text>
                : config?.mappings?.length
                  ? <Text style={st.statusOk}>{`${config.mappings.length} SET`}</Text>
                  : <Text style={st.statusTodo}>Needed</Text>
            }
            onPress={
              stale
                ? (canManage ? onConnect : undefined)
                : (canManage ? () => router.push("/org-salesforce-config") : undefined)
            }
          />
          {stale ? (
            <Text style={st.blurb}>
              This organisation’s Salesforce sign-in expired or was revoked.
              {canManage
                ? " Reconnect to use the mapping again."
                : " Ask an owner or manager to reconnect."}
            </Text>
          ) : null}

          {canManage ? (
            <>
              <SectionRule>Manage</SectionRule>
              <Text style={st.blurb}>
                Disconnecting only removes MinuteX’s access for {workspaceLabel(active)}.
                Meetings already pushed into Salesforce stay there, and your
                own Personal Salesforce connection (if any) is unaffected.
              </Text>
              <Button
                label={busy === "disconnect" ? "Disconnecting…" : "Disconnect Salesforce"}
                variant="secondary"
                disabled={busy !== ""}
                onPress={confirmDisconnect}
              />
            </>
          ) : (
            <>
              <SectionRule>Manage</SectionRule>
              <Text style={st.blurb}>
                Only an owner or manager can disconnect or reconfigure this
                organisation’s Salesforce connection.
              </Text>
            </>
          )}
        </>
      ) : canManage ? (
        <>
          <SectionRule>Connect</SectionRule>
          <Text style={st.blurb}>
            You’ll sign in on Salesforce’s own page — MinuteX never sees your
            Salesforce password. This connects Salesforce for the whole
            {" "}{workspaceLabel(active)} organisation, not just your account.
          </Text>
          <Button
            label={busy === "connect" ? "Opening Salesforce…" : "Connect Salesforce"}
            loading={busy === "connect"}
            disabled={busy !== ""}
            onPress={onConnect}
          />
        </>
      ) : (
        <>
          <SectionRule>Connect</SectionRule>
          <Text style={st.blurb}>
            Only an owner or manager can connect Salesforce for this organisation.
          </Text>
        </>
      )}
    </ScrollView>
  );
}
