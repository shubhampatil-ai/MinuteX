// src/app/salesforce.tsx — connect / disconnect a Salesforce org.
//
// Phase 1 of the CRM integration: the connection itself, nothing about
// meetings yet. Deliberately mirrors device/[id].tsx (the other "one linked
// external thing" screen): hero card with status, spec rows, then the
// management action guarded by an Alert that explains the consequence.
//
// The OAuth browser round-trip lives in lib/salesforce.ts; this screen only
// reflects state and reports outcomes.
import { useCallback, useMemo, useState } from "react";
import { Alert, RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { S, CAPS, FONT, useTheme, ColorScale } from "../../lib/theme";
import {
  Button, Card, ErrorText, IconCircle, ListRow, Loading, Masthead, Row,
  SectionRule,
} from "../../lib/ui";
import {
  ApiError, SalesforceConfig, SalesforceStatus, clearToken, disconnectSalesforce,
  getSalesforceConfig, getSalesforceStatus, isSalesforceReconnect,
} from "../../lib/api";
import { connectSalesforce } from "../../lib/salesforce";

function dateLabel(v?: string): string {
  if (!v) return "—";
  const d = new Date(v);
  return isNaN(d.getTime()) ? v : d.toLocaleDateString();
}

// "https://acme.my.salesforce.com" -> "acme.my.salesforce.com"
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
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3,
      color: C.success,
    },
    statusTodo: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3,
      color: C.warn,
    },
  });
}

export default function SalesforceScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);

  const [info, setInfo] = useState<SalesforceStatus | null>(null);
  const [config, setConfig] = useState<SalesforceConfig | null>(null);
  // True when the org row still exists but its refresh token is dead. /status
  // only reports that a connection RECORD exists, so this is the only signal
  // that the connection is real-but-unusable — and the mapping screen can't
  // work until the user reconnects.
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const status = await getSalesforceStatus();
      setInfo(status);
      if (!status.connected) {
        setConfig(null);
        setStale(false);
        return;
      }
      // The mapping only exists once connected, and a failure to read it must
      // not break the connection screen — it degrades to "not configured".
      // The ONE failure worth distinguishing is a dead Salesforce credential:
      // the mapping screen would fail the same way, so surface it here instead
      // of letting the user walk into it.
      try {
        setConfig(await getSalesforceConfig());
        setStale(false);
      } catch (e) {
        setConfig(null);
        setStale(isSalesforceReconnect(e));
      }
    } catch (e) {
      // Only a REAL MinuteX 401 ends the session. A stale Salesforce
      // credential is reported as 409 + code and never reaches this branch.
      if (e instanceof ApiError && e.status === 401) {
        await clearToken(); router.replace("/login"); return;
      }
      setError(e instanceof ApiError ? e.message : "Could not load the Salesforce connection.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [router]);

  // Runs on mount AND on every return to this screen, so coming back from the
  // mapping screen shows the new state. Wrapped in a void arrow because
  // useFocusEffect treats a returned value as a cleanup function, and an async
  // fn returns a Promise.
  useFocusEffect(useCallback(() => { load(); }, [load]));

  const onConnect = async () => {
    setBusy("connect"); setError("");
    const outcome = await connectSalesforce();
    setBusy("");
    // A cancel is a normal thing to do — don't shout about it.
    if (!outcome.ok && !outcome.cancelled) setError(outcome.message);
    if (outcome.ok) await load();
  };

  const confirmDisconnect = () => {
    Alert.alert(
      "Disconnect Salesforce?",
      "MinuteX stops being able to push meetings to this org. Nothing already "
      + "pushed to Salesforce is removed, and your recordings stay in your account.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Disconnect", style: "destructive",
          onPress: async () => {
            setBusy("disconnect"); setError("");
            try {
              await disconnectSalesforce();
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

  if (loading) {
    return (
      <View style={[st.container, { paddingTop: insets.top + S.lg }]}>
        <Loading label="Checking Salesforce…" />
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
      <Masthead kicker={connected ? "Linked to your account" : "Not connected"}
                title="Salesforce" />
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
              {connected ? info?.sf_username || "—" : "Push meetings into your CRM"}
            </Text>
            <Text style={[st.statusCaps, { color: connected ? C.success : C.textFaint }]}>
              {connected ? "CONNECTED" : "NOT CONNECTED"}
            </Text>
          </View>
        </View>

        {connected ? (
          <View style={{ marginTop: S.lg, paddingTop: S.sm, borderTopWidth: 1, borderTopColor: C.border }}>
            <Row label="Org" value={hostOf(info?.instance_url)} />
            <Row label="Salesforce user" value={info?.sf_username || "—"} />
            <Row label="Connected" value={dateLabel(info?.connected_at)} />
          </View>
        ) : null}
      </Card>

      {connected ? (
        <>
          {/* The mapping is what makes the connection USABLE — an org is
              connected but inert until MinuteX knows which object and fields
              to write to, so this sits above Manage and states plainly
              whether it's done. */}
          <SectionRule>Configuration</SectionRule>
          <ListRow
            icon="slider.horizontal.3"
            label="Salesforce mapping"
            sub={
              stale
                ? "Reconnect Salesforce to change the mapping"
                : config?.mappings?.length
                  // The configured objects, in the org's own words — nothing here
                  // names a specific object.
                  ? config.mappings.map((m) => m.object_label || m.object).join(" · ")
                  : "Choose which records to link meetings to"
            }
            right={
              stale
                ? <Text style={st.statusTodo}>Reconnect</Text>
                : config?.mappings?.length
                  ? <Text style={st.statusOk}>{`${config.mappings.length} SET`}</Text>
                  : <Text style={st.statusTodo}>Needed</Text>
            }
            // Opening the mapping screen against a dead credential would only
            // bounce straight back here, so reconnect first.
            onPress={stale ? onConnect : () => router.push("/salesforce-config")}
          />
          {stale ? (
            <Text style={st.blurb}>
              Your Salesforce sign-in expired or was revoked in your org. Your
              MinuteX account and recordings are unaffected — reconnect to use
              the mapping again.
            </Text>
          ) : null}

          <SectionRule>Manage</SectionRule>
          <Text style={st.blurb}>
            Disconnecting only removes MinuteX’s access. Meetings already pushed
            into Salesforce stay there.
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
          <SectionRule>Connect</SectionRule>
          <Text style={st.blurb}>
            You’ll sign in on Salesforce’s own page — MinuteX never sees your
            Salesforce password. You can disconnect at any time.
          </Text>
          <Button
            label={busy === "connect" ? "Opening Salesforce…" : "Connect Salesforce"}
            loading={busy === "connect"}
            disabled={busy !== ""}
            onPress={onConnect}
          />
        </>
      )}
    </ScrollView>
  );
}
