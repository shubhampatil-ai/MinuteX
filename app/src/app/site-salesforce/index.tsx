// src/app/site-salesforce/index.tsx — Settings -> Integrations -> Salesforce
// -> Manage.
//
// A short setup checklist for the workspace's Salesforce connection:
//
//   ✓ Connected to <org>
//   People          Contacts and Users synced from Salesforce  [Sync people]
//   Where meetings go   the destinations the org's Salesforce admin set up
//                       (MinuteX Destinations, in Salesforce: object, field
//                       mappings, who may use it) — listed here as the
//                       signed-in person may use them
//   Salesforce data  the object catalog (refresh)
//   Disconnect Salesforce (owner/manager, always visible)
//   Advanced (collapsed): test, reinstall or reconnect, Task templates
//
// Nothing technical is shown until Advanced is opened. Not connected -> the
// Connect Salesforce screen (site-salesforce/connect).
import { useCallback, useMemo, useRef, useState } from "react";
import { Alert, Pressable, RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { FONT, S, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, Card, ErrorText, KeyboardAware, Loading, SectionRule, SuccessText,
} from "../../../lib/ui";
import {
  ApiError, SiteEndpoint, SiteHealth, SiteSfConnection, SiteSfDestination, checkSiteDeliveryHealth,
  disconnectSiteSalesforce, getSiteDelivery, getSiteSalesforce,
  listSiteSalesforceDestinations, startSitePeopleSync, startSiteSalesforceDiscovery,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { siteSfError, syncLabel, usePolling } from "../../../lib/site-salesforce-ui";
import {
  healthLine, isInFlight, orgDisplayName, peopleSummary,
} from "../../../lib/site-salesforce-flow";
import { completedRuns, invalidateSiteSalesforce } from "../../../lib/site-salesforce-events";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.x4 },
    label: { fontFamily: FONT.bold, fontSize: 13, color: C.text, marginBottom: S.sm },
    help: { fontFamily: FONT.regular, fontSize: 12, lineHeight: 18, color: C.textFaint, marginTop: S.sm },
    status: { fontFamily: FONT.bold, fontSize: 15, color: C.text },
    counts: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 4 },
    ok: { fontFamily: FONT.bold, fontSize: 15, color: C.success },
    bad: { fontFamily: FONT.bold, fontSize: 13.5, color: C.danger, marginTop: S.sm },
    badDetail: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim, marginTop: 2 },
    dest: { paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.border },
    destName: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    destMeta: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
    toggle: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.primary, marginTop: S.xl },
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
  const [destinations, setDestinations] = useState<SiteSfDestination[] | null>(null);
  const [destSupported, setDestSupported] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"" | "connect" | "health" | "discover" | "people" | "disconnect">("");
  const [advanced, setAdvanced] = useState(false);
  const [health, setHealth] = useState<SiteHealth | null>(null);

  // The last statuses seen, so a sync or discovery that has just FINISHED is
  // announced once to every other screen and they reload immediately.
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

      if (c.connected) {
        const d = await listSiteSalesforceDestinations(activeId).catch(() => null);
        setDestinations(d ? d.destinations : []);
        setDestSupported(d ? d.supported : true);
      }
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
      "MinuteX stops syncing with this Salesforce org. Meetings and Tasks already in Salesforce stay there.",
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
  const anyBusy = busy !== "";
  const people = peopleSummary(endpoint?.people_sync?.counts);
  const healthView = healthLine(health);

  if (!connected) {
    return (
      <View style={[st.screen, st.body]}>
        <Stack.Screen options={{ title: "Salesforce" }} />
        {error ? <ErrorText>{error}</ErrorText> : null}
        <Card>
          <Text style={st.status}>Salesforce isn&apos;t connected</Text>
          <Text style={st.counts}>Connect Salesforce to sync people and push meetings.</Text>
          {canManage ? (
            <Button label="Install MinuteX in Salesforce" onPress={() => router.replace("/site-salesforce/connect" as never)}
              disabled={anyBusy} style={{ marginTop: S.md }} />
          ) : (
            <Text style={st.help}>An owner or manager of this workspace connects Salesforce.</Text>
          )}
        </Card>
      </View>
    );
  }

  return (
    <KeyboardAware style={st.screen}>
      <ScrollView style={st.screen} contentContainerStyle={st.body} keyboardShouldPersistTaps="handled"
        refreshControl={<RefreshControl refreshing={false} onRefresh={() => { void load(); }} />}>
        <Stack.Screen options={{ title: "Salesforce" }} />
        {error ? <ErrorText>{error}</ErrorText> : null}
        {notice ? <SuccessText>{notice}</SuccessText> : null}

        <Card>
          <Text style={st.ok}>✓ Connected</Text>
          <Text style={st.counts}>
            {orgDisplayName(conn?.org_name) ? `Connected to: ${orgDisplayName(conn?.org_name)}` : "Connected to Salesforce"}
            {conn?.is_sandbox ? " (sandbox)" : ""}
          </Text>
        </Card>

        <SectionRule>People</SectionRule>
        <Card>
          <Text style={st.status}>
            {syncingPeople ? "Syncing people…"
              : people.total !== null && endpoint?.people_sync?.status === "SYNCED"
                ? `${people.contacts ?? 0} Contacts · ${people.users ?? 0} Users`
                : syncLabel(endpoint?.people_sync?.status) === "Not run" ? "Not synced yet" : syncLabel(endpoint?.people_sync?.status)}
          </Text>
          <Text style={st.help}>
            Contacts are people you meet — tag them on a meeting. Users can be assigned Tasks.
          </Text>
          {canSyncPeople ? (
            <Button label={syncingPeople ? "Syncing…" : "Sync people"} variant="secondary"
              loading={busy === "people"} disabled={anyBusy || syncingPeople}
              onPress={() => { void run("people"); }} style={{ marginTop: S.md }} />
          ) : null}
          {people.total ? (
            <Button label="View people" variant="ghost" disabled={anyBusy}
              onPress={() => router.push("/contacts?tab=salesforce" as never)} />
          ) : null}
        </Card>

        <SectionRule>Where meetings go</SectionRule>
        <Card>
          {destinations === null ? <Text style={st.counts}>Loading…</Text>
            : !destSupported ? (
              <Text style={st.counts}>Update MinuteX in Salesforce to choose where meetings go.</Text>
            ) : destinations.length === 0 ? (
              <>
                <Text style={st.status}>No destinations yet</Text>
                <Text style={st.help}>
                  {canManage
                    ? "Your Salesforce admin sets these up in Salesforce: App Launcher → MinuteX Destinations. Each one names a Salesforce object (for example Site Visit), the fields a meeting fills, and who can use it."
                    : "Your Salesforce admin chooses where meetings can go."}
                </Text>
              </>
            ) : (
              <>
                <Text style={st.counts}>You can push meetings to:</Text>
                {destinations.map((d) => (
                  <View key={d.destination_id} style={st.dest}>
                    <Text style={st.destName}>{d.name}</Text>
                    {d.fields.length ? (
                      <Text style={st.destMeta} numberOfLines={2}>Fills {d.fields.join(", ")}</Text>
                    ) : null}
                  </View>
                ))}
                {canManage ? (
                  <Text style={st.help}>Destinations, their fields and who can use them are managed in Salesforce (MinuteX Destinations).</Text>
                ) : null}
              </>
            )}
        </Card>

        {canManage ? (
          <>
            <SectionRule>Connection</SectionRule>
            <Button label="Disconnect Salesforce" variant="destructive" onPress={disconnect}
              loading={busy === "disconnect"} disabled={anyBusy} />
            <Text style={st.help}>
              MinuteX stops syncing with this Salesforce org. Meetings and Tasks already in Salesforce stay there.
            </Text>
            <Pressable onPress={() => setAdvanced((v) => !v)} accessibilityRole="button" hitSlop={8}>
              <Text style={st.toggle}>{advanced ? "Hide advanced" : "Advanced"}</Text>
            </Pressable>
            {advanced ? (
              <Card style={{ marginTop: S.sm }}>
                <Button label="Test connection" variant="secondary" onPress={() => { void testConnection(); }}
                  loading={busy === "health"} disabled={anyBusy} />
                {healthView ? (
                  <View>
                    <Text style={healthView.ok ? [st.ok, { fontSize: 13.5, marginTop: S.sm }] : st.bad}>
                      {healthView.ok ? "✓ Working" : healthView.title}
                    </Text>
                    {healthView.detail ? <Text style={st.badDetail}>{healthView.detail}</Text> : null}
                  </View>
                ) : null}
                {/* New Salesforce fields and picklist values (Task Status,
                    Priority) are picked up here; MinuteX also refreshes on connect. */}
                <Button label={discovering ? "Refreshing…" : "Refresh from Salesforce"} variant="ghost"
                  loading={busy === "discover"} disabled={anyBusy || discovering}
                  onPress={() => { void run("discover"); }} />
                <Button label="Reinstall or reconnect" variant="ghost" disabled={anyBusy}
                  onPress={() => router.push("/site-salesforce/connect" as never)} />
              </Card>
            ) : null}
          </>
        ) : null}
      </ScrollView>
    </KeyboardAware>
  );
}
