// src/app/site-salesforce/index.tsx — Salesforce (Site) connection.
//
// delivery_type = salesforce_site: the workspace's Salesforce Site + Apex
// REST receiver is the only Salesforce connection.
//
//   Site URL -> Connect -> Test Connection -> Discover objects
//   -> Sync Users & Contacts -> (a meeting) Push / Create Salesforce Task
//
// The Salesforce Site is the only API boundary. Connect saves the workspace's
// Site endpoint (PUT .../site-delivery, auth mode "none" for development, or
// whatever mode the endpoint already has) and then pins the org id the Site
// reports (PUT .../site-salesforce/connect). No Salesforce credential is
// entered, sent or stored by the app.
import { useCallback, useMemo, useRef, useState } from "react";
import { Alert, RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { FONT, S, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, Card, ErrorText, KeyboardAware, ListRow, Loading, Row, SectionRule, SuccessText, TextField,
} from "../../../lib/ui";
import {
  ApiError, SiteEndpoint, SiteHealth, SiteSfConnection, checkSiteDeliveryHealth,
  connectSiteSalesforce, disconnectSiteSalesforce, getSiteDelivery, getSiteSalesforce,
  saveSiteDelivery, startSitePeopleSync, startSiteSalesforceDiscovery,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { Flag, FlagRow, siteSfError, syncLabel, usePolling } from "../../../lib/site-salesforce-ui";
import {
  STAGE_LABELS, displaySiteUrl, healthLine, isInFlight, normalizeSiteUrl, peopleSummary, siteStage,
} from "../../../lib/site-salesforce-flow";
import { completedRuns, invalidateSiteSalesforce } from "../../../lib/site-salesforce-events";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.x4 },
    intro: { fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim },
    label: { fontFamily: FONT.bold, fontSize: 13, color: C.text, marginBottom: S.sm },
    help: { fontFamily: FONT.regular, fontSize: 12, lineHeight: 18, color: C.textFaint, marginTop: S.sm },
    status: { fontFamily: FONT.bold, fontSize: 15, color: C.text },
    stage: { fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim, marginTop: 4 },
    counts: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 4 },
    ok: { fontFamily: FONT.bold, fontSize: 13.5, color: C.success, marginTop: S.sm },
    bad: { fontFamily: FONT.bold, fontSize: 13.5, color: C.danger, marginTop: S.sm },
    badDetail: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim, marginTop: 2 },
  });
}

export default function SiteSalesforceScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId, can, loading: wsLoading } = useWorkspace();
  const canManage = can("manage_integrations");
  const canSyncPeople = can("manage_contacts");

  const [conn, setConn] = useState<SiteSfConnection | null>(null);
  const [endpoint, setEndpoint] = useState<SiteEndpoint | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"" | "connect" | "health" | "discover" | "people" | "disconnect">("");
  const [urlDraft, setUrlDraft] = useState("");
  const [editingUrl, setEditingUrl] = useState(false);
  const [health, setHealth] = useState<SiteHealth | null>(null);

  // The last statuses seen, so a sync or discovery that has just FINISHED is
  // announced once to every other screen (People, pickers, the meeting card)
  // and they reload immediately instead of showing a stale list.
  const lastRun = useRef<{ people?: string; discovery?: string }>({});

  const load = useCallback(async () => {
    if (!activeId) return;
    try {
      const [c, e] = await Promise.all([getSiteSalesforce(activeId), getSiteDelivery(activeId)]);
      const next = { people: e.people_sync?.status, discovery: c.discovery?.status };
      const done = completedRuns(lastRun.current, next);
      lastRun.current = next;
      if (done.length) invalidateSiteSalesforce(...done);
      setConn(c);
      setEndpoint(e);
      setUrlDraft((d) => d || displaySiteUrl(e.url));
      setError("");
    } catch (e) {
      setError(siteSfError(e));
    } finally {
      setLoading(false);
    }
  }, [activeId]);

  useFocusEffect(useCallback(() => { void load(); }, [load]));

  const discovering = isInFlight(conn?.discovery?.status);
  const syncingPeople = isInFlight(endpoint?.people_sync?.status);
  usePolling(discovering || syncingPeople, () => { void load(); });

  const connect = async (allowOrgChange = false) => {
    const { url, error: urlError } = normalizeSiteUrl(urlDraft);
    if (urlError) { setError(urlError); return; }
    setBusy("connect"); setError(""); setNotice(""); setHealth(null);
    try {
      // Save the endpoint only when it changed. An endpoint already set to a
      // signed auth mode keeps that mode (and its server-side secret).
      if (!endpoint?.configured || !endpoint.enabled || endpoint.url !== url) {
        const saved = await saveSiteDelivery(activeId, {
          url, auth_mode: endpoint?.auth_mode || "none", enabled: true,
        });
        setEndpoint(saved.endpoint);
      }
      const before = conn?.org_id;
      const c = await connectSiteSalesforce(activeId, { allow_org_change: allowOrgChange });
      setConn(c);
      setEditingUrl(false);
      setNotice(c.org_id !== before
        ? `Connected to ${c.org_name || "Salesforce"}.`
        : "Salesforce Site connected.");
      invalidateSiteSalesforce("connection", "objects", "relation");
    } catch (e) {
      if (e instanceof ApiError && e.code === "org_mismatch" && conn?.connected && !allowOrgChange) {
        Alert.alert("Switch Salesforce org?",
          `${e.message}\n\nSwitching clears the discovered objects and all Task mappings.`,
          [{ text: "Cancel", style: "cancel" },
           { text: "Switch org", style: "destructive", onPress: () => { void connect(true); } }]);
      } else {
        setError(siteSfError(e));
      }
    } finally {
      setBusy("");
      void load();
    }
  };

  const testConnection = async () => {
    setBusy("health"); setError(""); setNotice(""); setHealth(null);
    try {
      setHealth(await checkSiteDeliveryHealth(activeId));
    } catch (e) {
      setHealth({ ok: false, code: e instanceof ApiError ? e.code : "", message: siteSfError(e) });
    } finally {
      setBusy("");
    }
  };

  const run = async (kind: "discover" | "people") => {
    setBusy(kind); setError(""); setNotice("");
    try {
      if (kind === "discover") await startSiteSalesforceDiscovery(activeId);
      else await startSitePeopleSync(activeId);
      // Mark the run as started so its completion is announced even when the
      // worker finishes before the first poll sees it in flight.
      lastRun.current = { ...lastRun.current, [kind === "discover" ? "discovery" : "people"]: "PENDING" };
      await load();
    } catch (e) {
      setError(siteSfError(e));
    } finally {
      setBusy("");
    }
  };

  const disconnect = () => {
    Alert.alert("Disconnect Salesforce?",
      "MinuteX forgets the connected org and the discovered objects. Task mappings, Tasks already sent and the Site URL are kept.",
      [{ text: "Cancel", style: "cancel" },
       { text: "Disconnect", style: "destructive", onPress: async () => {
         setBusy("disconnect");
         try {
           await disconnectSiteSalesforce(activeId);
           invalidateSiteSalesforce("connection", "objects", "people", "relation");
           await load();
         }
         catch (e) { setError(siteSfError(e)); }
         finally { setBusy(""); }
       } }]);
  };

  if (wsLoading || (loading && !conn)) return <Loading />;

  const connected = conn?.connected === true;
  const stage = siteStage({
    connected, connecting: busy === "connect",
    discoveryStatus: conn?.discovery?.status, peopleStatus: endpoint?.people_sync?.status,
  });
  const anyBusy = busy !== "";
  const counts = conn?.discovery?.counts || {};
  const people = peopleSummary(endpoint?.people_sync?.counts);
  const healthView = healthLine(health);
  const showUrlForm = canManage && (!connected || editingUrl);

  return (
    <KeyboardAware style={st.screen}>
      <ScrollView style={st.screen} contentContainerStyle={st.body} keyboardShouldPersistTaps="handled"
        refreshControl={<RefreshControl refreshing={false} onRefresh={() => { void load(); }} />}>
        <Stack.Screen options={{ title: "Salesforce (Site)" }} />
        <Text style={st.intro}>
          Connects this workspace to Salesforce through your org&apos;s MinuteX Site. MinuteX
          sees and does only what the org&apos;s configured integration user may in Salesforce.
        </Text>
        {error ? <ErrorText>{error}</ErrorText> : null}
        {notice ? <SuccessText>{notice}</SuccessText> : null}

        <SectionRule>Connection</SectionRule>
        <Card>
          <Text style={[st.status, { color: connected ? C.success : C.textFaint }]}>
            {connected ? "✓ Salesforce Site Connected" : "Not connected"}
          </Text>
          <Text style={st.stage}>{STAGE_LABELS[stage]}</Text>
          {endpoint?.configured ? <Row label="Site URL" value={displaySiteUrl(endpoint.url)} /> : null}
          {connected ? (
            <>
              <Row label="Org" value={`${conn?.org_name || "Salesforce"}${conn?.is_sandbox ? " (sandbox)" : ""}`} />
              <Row label="Integration user" value={conn?.integration_user?.name || conn?.integration_user?.username || ""} />
              {endpoint?.auth_mode === "none" ? <Row label="Auth" value="None (development)" /> : null}
              <FlagRow>
                <Flag label="Task: create" on={!!conn?.task_permissions?.createable} />
                <Flag label="Task: update" on={!!conn?.task_permissions?.updateable} />
              </FlagRow>
              {!conn?.task_permissions?.createable ? (
                <Text style={st.help}>
                  Salesforce permissions do not allow the integration user to create Tasks.
                  Grant it in Salesforce, then connect again.
                </Text>
              ) : null}
            </>
          ) : null}

          {canManage && endpoint?.configured ? (
            <Button label="Test Connection" variant="secondary" onPress={() => { void testConnection(); }}
              loading={busy === "health"} disabled={anyBusy} style={{ marginTop: S.md }} />
          ) : null}
          {healthView ? (
            <View>
              <Text style={healthView.ok ? st.ok : st.bad}>
                {healthView.ok ? "✓ Connected" : healthView.title}
              </Text>
              {healthView.detail ? <Text style={st.badDetail}>{healthView.detail}</Text> : null}
            </View>
          ) : null}
        </Card>

        {showUrlForm ? (
          <Card style={{ marginTop: S.md }}>
            <Text style={st.label}>Salesforce Site URL</Text>
            <TextField value={urlDraft} onChangeText={setUrlDraft}
              placeholder="https://your-site.my.salesforce-sites.com"
              autoCapitalize="none" autoCorrect={false} keyboardType="url"
              editable={!anyBusy} accessibilityLabel="Salesforce Site URL" />
            <Text style={st.help}>
              The public Site the MinuteX receiver is installed on (Setup › Sites). MinuteX adds
              {" "}/services/apexrest/minutex/v1/ if you leave it off. In Salesforce, set Custom
              Settings › MinuteX Site Settings › Integration Username first.
            </Text>
            <Button label={connected ? "Save and connect" : "Connect"} onPress={() => { void connect(); }}
              loading={busy === "connect"} disabled={anyBusy || !urlDraft.trim()} style={{ marginTop: S.lg }} />
            {editingUrl ? (
              <Button label="Cancel" variant="ghost" disabled={anyBusy}
                onPress={() => { setEditingUrl(false); setUrlDraft(displaySiteUrl(endpoint?.url)); }}
                style={{ marginTop: S.sm }} />
            ) : null}
          </Card>
        ) : null}
        {!canManage && !connected ? (
          <Text style={st.help}>An owner or manager connects Salesforce for this workspace.</Text>
        ) : null}

        {connected ? (
          <>
            <SectionRule>Salesforce metadata</SectionRule>
            <Card>
              <Text style={st.status}>Objects: {syncLabel(conn?.discovery?.status)}</Text>
              {counts.objects ? (
                <Text style={st.counts}>
                  {counts.objects} objects meetings and Tasks can be related to
                  {counts.objects_inaccessible ? ` (${counts.objects_inaccessible} not accessible to the integration user)` : ""}
                </Text>
              ) : null}
              {conn?.discovery?.completed_at ? (
                <Text style={st.counts}>Last completed {conn.discovery.completed_at.slice(0, 16).replace("T", " ")}</Text>
              ) : null}
              {conn?.discovery?.status === "FAILED" && conn.discovery.error_code ? (
                <Text style={st.badDetail}>{siteSfError(new ApiError(0, "Discovery failed.", conn.discovery.error_code))}</Text>
              ) : null}
              {canManage ? (
                <Button label={discovering ? "Discovering…" : "Discover Salesforce Objects"}
                  variant="secondary" loading={busy === "discover"} disabled={anyBusy || discovering}
                  onPress={() => { void run("discover"); }} style={{ marginTop: S.md }} />
              ) : null}
            </Card>

            <Card style={{ marginTop: S.md }}>
              <Text style={st.status}>Users & Contacts: {syncLabel(endpoint?.people_sync?.status)}</Text>
              {endpoint?.people_sync?.status === "SYNCED" && people.total !== null ? (
                <>
                  <Text style={st.ok}>✓ {people.total} people synced</Text>
                  {people.users !== null ? <Text style={st.counts}>Users: {people.users}</Text> : null}
                  {people.contacts !== null ? <Text style={st.counts}>Contacts: {people.contacts}</Text> : null}
                </>
              ) : null}
              <Text style={st.help}>
                Contacts are the people you meet — tag them on a meeting&apos;s People tab. Users are
                your Salesforce colleagues who can be assigned Tasks. MinuteX uses this local copy and
                does not search Salesforce each time.
              </Text>
              {people.total ? (
                <Button label="View synced people" variant="ghost" disabled={anyBusy}
                  onPress={() => router.push("/contacts?tab=salesforce" as never)} />
              ) : null}
              {canSyncPeople ? (
                <Button label={syncingPeople ? "Syncing…" : "Sync Users & Contacts"}
                  variant="secondary" loading={busy === "people"} disabled={anyBusy || syncingPeople}
                  onPress={() => { void run("people"); }} style={{ marginTop: S.md }} />
              ) : null}
            </Card>

            <SectionRule>Use</SectionRule>
            <ListRow icon="square.grid.2x2" label="Salesforce objects"
              sub="The records meetings and Tasks can be related to"
              onPress={() => router.push("/site-salesforce/objects")} />
            <ListRow icon="slider.horizontal.3" label="Task mappings"
              sub="Reusable defaults: object, subject, assignee, due rule"
              onPress={() => router.push("/site-salesforce/mappings")} />
            <Text style={st.help}>
              Tag a synced Salesforce Contact on a meeting and the meeting — and its Tasks — are
              related to that Contact automatically. Open the meeting&apos;s Salesforce section to push
              it or change the record.
            </Text>

            {canManage ? (
              <>
                <SectionRule>Manage</SectionRule>
                {!editingUrl ? (
                  <Button label="Change Site URL" variant="secondary" disabled={anyBusy}
                    onPress={() => { setEditingUrl(true); setHealth(null); }} />
                ) : null}
                <Button label="Disconnect" variant="destructive" onPress={disconnect}
                  loading={busy === "disconnect"} disabled={anyBusy} style={{ marginTop: S.sm }} />
              </>
            ) : null}
          </>
        ) : null}
      </ScrollView>
    </KeyboardAware>
  );
}
