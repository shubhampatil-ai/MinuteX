// src/app/devices.tsx — Your device: the hardware dashboard.
//
// "Workspace" edition. The device gets a masthead, then a rounded, softly
// shadowed Card holding its portrait beside the one number that matters
// (battery, set in a bold extrabold numeral), then a spec list inside the
// same card. Claimed devices below are their own cards, not a ruled list.
import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, ELEV, CAPS, FONT, TABULAR, useTheme, ColorScale } from "../../lib/theme";
import {
  Button, Card, ComingSoonRow, EmptyState, ErrorText, Masthead, Row,
  SectionRule,
} from "../../lib/ui";
import { DeviceArt } from "../../lib/device-art";
import { useDevice } from "../../lib/device-context";
import type { DeviceStatus } from "../../lib/device";
import { lastSeenLabel } from "../../lib/sources";
import { getDevices, clearToken, ApiError, DeviceInfo } from "../../lib/api";

function wifiLabel(s: DeviceStatus | null): string {
  if (s?.wifiState === "connected") return s.wifiSsid ? s.wifiSsid : "Connected";
  if (s?.wifiState === "connecting") return "Connecting…";
  if (s?.wifiState === "failed") return "Failed";
  return "Off";
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20, paddingTop: S.lg },
    // Portrait of the device beside its battery reading, inside a Card.
    hero: { flexDirection: "row" as const, alignItems: "center" as const, gap: 20, marginTop: S.lg },
    heroInfo: { flex: 1 },
    battLabel: { ...CAPS, fontSize: 11, letterSpacing: 1.2, color: C.textFaint },
    battRow: { flexDirection: "row" as const, alignItems: "baseline" as const, gap: 6, marginTop: 5 },
    // TABULAR carries fontFamily (mono) — override with extrabold for the
    // big numeral treatment (matches the new headline scale, no more serif).
    battNum: { ...TABULAR, fontFamily: FONT.extrabold, fontSize: 44, lineHeight: 44, color: C.text },
    battPct: { fontFamily: FONT.semibold, fontSize: 15, color: C.textDim },
    battTrack: { height: 6, borderRadius: R.pill, backgroundColor: C.surface2, marginTop: 10, overflow: "hidden" as const },
    battFill: { height: "100%" as const, borderRadius: R.pill },
    battNote: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 7 },
    deviceName: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    actions: { flexDirection: "row" as const, gap: S.sm, marginTop: S.lg },
    actionBtn: { flex: 1 },
    claimedCard: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      marginBottom: S.md,
    },
    claimedId: { ...TABULAR, fontSize: 12.5, color: C.text, flex: 1 },
    claimedName: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.text },
    claimedSub: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint, marginTop: 2 },
  });
}

export default function DevicesScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const { device, status, connState, reconnectById, refreshStatus } = useDevice();
  const [claimed, setClaimed] = useState<DeviceInfo[]>([]);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      setClaimed(await getDevices());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { await clearToken(); router.replace("/login"); return; }
      setError(e instanceof ApiError ? e.message : "Could not load devices.");
    } finally {
      setRefreshing(false);
    }
  }, [router]);

  useEffect(() => { load(); }, [load]);

  const onRefresh = () => { setRefreshing(true); refreshStatus(); load(); };

  const connected = connState === "connected";
  const batt = status?.batteryLevel ?? 0;
  // Only a genuinely low battery is coloured — "fine" is the absence of a
  // colour in this system, so a healthy device gets settled green and nothing
  // else on the screen competes with it.
  const battColor = batt <= 20 ? C.danger : C.success;
  const synced = (status?.pendingUploads ?? 0) === 0;
  // Rough runtime estimate from the design's "About 9 hours of listening
  // left" line, scaled off a ~12h full-charge budget.
  const hoursLeft = Math.round((batt / 100) * 12);

  return (
    <ScrollView
      style={st.container}
      contentContainerStyle={{ paddingBottom: 100 }}
      showsVerticalScrollIndicator={false}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.primary} />}
    >
      <Masthead
        kicker={
          connected ? "Connected · just now"
            : connState === "connecting" || connState === "reconnecting" ? "Connecting…"
            : device ? "Not connected" : "Nothing paired"
        }
        title="Your device"
      />
      {error ? <ErrorText>{error}</ErrorText> : null}

      {/* Live connected device */}
      {device ? (
        <Card style={{ marginTop: S.lg }}>
          <View style={[st.hero, { marginTop: 0 }]}>
            <DeviceArt size={92} live={connected} />
            <View style={st.heroInfo}>
              {status ? (
                <>
                  <Text style={st.battLabel}>Battery</Text>
                  <View style={st.battRow}>
                    <Text style={st.battNum}>{batt}</Text>
                    <Text style={st.battPct}>%</Text>
                  </View>
                  <View style={st.battTrack}>
                    <View style={[st.battFill, {
                      width: `${Math.max(2, Math.min(100, batt))}%`, backgroundColor: battColor,
                    }]} />
                  </View>
                  <Text style={st.battNote}>
                    {hoursLeft > 0 ? `About ${hoursLeft} hours of listening left` : "Charge it soon"}
                  </Text>
                </>
              ) : (
                <>
                  <Text style={st.deviceName}>{device.name ?? "MinuteX device"}</Text>
                  <Text style={st.battNote} numberOfLines={1}>{device.id}</Text>
                </>
              )}
            </View>
          </View>

          <View style={{ marginTop: S.lg, paddingTop: S.sm, borderTopWidth: 1, borderTopColor: C.border }}>
            <Row label="Wi-Fi" value={wifiLabel(status)} />
            <Row
              label="Waiting to upload"
              value={
                status ? (
                  <Text style={{
                    fontFamily: FONT.semibold, fontSize: 13.5,
                    color: synced ? C.success : C.primary,
                  }}>
                    {synced ? "Nothing — all filed" : `${status.pendingUploads} waiting`}
                  </Text>
                ) : "—"
              }
            />
            <Row label="Firmware" value={status?.firmwareVersion ? `v${status.firmwareVersion}` : "—"} numeric />
            <Row label="IP address" value={status?.ipAddress || "—"} numeric />
            <Row label="Serial" value={device.id} numeric />
            <Row label="State" value={status?.recordingState ?? "—"} />
          </View>

          <View style={st.actions}>
            <Button label="Wi-Fi setup" variant="secondary" style={st.actionBtn}
              onPress={() => router.push("/pair?step=wifi")} />
            <Button label="Reconnect" variant="secondary" style={st.actionBtn}
              onPress={() => reconnectById(device.id)} />
          </View>
        </Card>
      ) : (
        <EmptyState
          title="No device paired yet"
          subtitle="Pair your device over Bluetooth to see its battery, its Wi-Fi, and what it's still holding."
          action={<Button label="Pair your device" onPress={() => router.push("/pair")} />}
        />
      )}

      {/* Firmware updates — honest Coming Soon, not a dead button */}
      <SectionRule>Not yet</SectionRule>
      <ComingSoonRow icon="arrow.down.circle" label="Wireless firmware updates"
        sub="Update the device from the app, without a cable" />

      {/* Claimed devices (account-linked, from backend) */}
      <SectionRule>Linked to your account</SectionRule>
      {claimed.length === 0 ? (
        <EmptyState
          title="Nothing linked"
          subtitle="Link a device to your account so its recordings land on your desk."
          action={<Button label="Pair a device" variant="secondary" onPress={() => router.push("/pair-device")} />}
        />
      ) : (
        // Tapping through to /device/{id} is where rename, remove and
        // factory reset live — each device gets its own Card row.
        claimed.map((d) => (
          <Card
            key={d.device_id}
            style={st.claimedCard}
            onPress={() => router.push({ pathname: "/device/[id]", params: { id: d.device_id } })}
          >
            <Icon
              name="checkmark.seal.fill"
              tintColor={device?.id === d.device_id && connected ? C.success : C.textFaint}
              size={18}
            />
            <View style={{ flex: 1 }}>
              <Text style={st.claimedName} numberOfLines={1}>{d.name}</Text>
              <Text style={st.claimedSub} numberOfLines={1}>
                {d.firmware_version ? `v${d.firmware_version} · ` : ""}
                {lastSeenLabel(d.last_seen)}
              </Text>
            </View>
            {device?.id === d.device_id && connected ? (
              <Text style={{
                ...CAPS, fontFamily: FONT.bold, fontSize: 9.5,
                letterSpacing: 1.3, color: C.success,
              }}>
                Live
              </Text>
            ) : (
              <Icon name="chevron.right" tintColor={C.textFaint} size={16} />
            )}
          </Card>
        ))
      )}
      <Button label="Pair another device" variant="ghost" onPress={() => router.push("/pair-device")} />
      {/* The old API-key flow still works for devices provisioned before
          pairing existed — kept as the quiet fallback, not the main path. */}
      <Button label="Use a device key instead" variant="ghost" onPress={() => router.push("/claim")} />
    </ScrollView>
  );
}
