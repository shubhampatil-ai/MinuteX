// src/app/device/[id].tsx — one device: its specs and its management.
//
// The dashboard tab shows the LIVE (BLE-connected) device; this is the
// account's record of a device — what the backend knows about it, plus the
// three destructive-ish actions that need room to explain themselves.
//
// "Workspace" edition: the same rounded Card treatment as the devices tab —
// the hero and spec rows live inside one card, danger reserved for the
// destructive actions.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert, RefreshControl, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { S, CAPS, FONT, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, Card, ErrorText, KeyboardAware, Loading, Masthead, Row, SectionRule,
  TextField, scrollFormProps,
} from "../../../lib/ui";
import { DeviceArt } from "../../../lib/device-art";
import { useDevice } from "../../../lib/device-context";
import { lastSeenLabel } from "../../../lib/sources";
import {
  ApiError, DeviceInfo, clearToken, factoryResetDevice, getDevice,
  renameDevice, unpairDevice,
} from "../../../lib/api";

function dateLabel(v: string | null): string {
  if (!v) return "—";
  const d = new Date(v);
  return isNaN(d.getTime()) ? v : d.toLocaleDateString();
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    hero: {
      flexDirection: "row" as const, alignItems: "center" as const,
      gap: 20, marginTop: 0,
    },
    heroInfo: { flex: 1 },
    name: { fontFamily: FONT.extrabold, fontSize: 22, color: C.text },
    id: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 5 },
    statusCaps: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3,
      marginTop: 9,
    },
    danger: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint,
      marginTop: 4, marginBottom: 14, lineHeight: 18,
    },
    renameRow: { flexDirection: "row" as const, gap: S.sm, alignItems: "center" as const },
  });
}

export default function DeviceDetailScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);
  const { id } = useLocalSearchParams<{ id: string }>();
  const deviceId = String(id ?? "");
  const { device: live, status, connState } = useDevice();

  const [info, setInfo] = useState<DeviceInfo | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const d = await getDevice(deviceId);
      setInfo(d);
      setName(d.name === d.device_id ? "" : d.name);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        await clearToken(); router.replace("/login"); return;
      }
      setError(e instanceof ApiError ? e.message : "Could not load this device.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [deviceId, router]);

  useEffect(() => { load(); }, [load]);

  const onSaveName = async () => {
    setBusy("rename"); setError("");
    try {
      const d = await renameDevice(deviceId, name.trim());
      setInfo(d);
      setName(d.name === d.device_id ? "" : d.name);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not rename this device.");
    } finally {
      setBusy("");
    }
  };

  // Both destructive actions keep the recordings; say so in the prompt, or
  // people reasonably assume "remove"/"reset" means "lose my meetings".
  const confirmUnpair = () => {
    Alert.alert(
      "Remove this device?",
      "It stops uploading to your account and can be paired to someone else. "
      + "Your recordings and notes stay in your account.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Remove", style: "destructive",
          onPress: async () => {
            setBusy("unpair"); setError("");
            try {
              await unpairDevice(deviceId);
              router.back();
            } catch (e) {
              setError(e instanceof ApiError ? e.message : "Could not remove this device.");
              setBusy("");
            }
          },
        },
      ]
    );
  };

  const confirmFactoryReset = () => {
    Alert.alert(
      "Factory reset?",
      "The device erases its settings and anything it hasn't uploaded yet, "
      + "then leaves your account. Recordings already uploaded are kept.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Reset device", style: "destructive",
          onPress: async () => {
            setBusy("reset"); setError("");
            try {
              await factoryResetDevice(deviceId);
              router.back();
            } catch (e) {
              setError(e instanceof ApiError ? e.message : "Could not reset this device.");
              setBusy("");
            }
          },
        },
      ]
    );
  };

  const isLive = live?.id === deviceId && connState === "connected";
  const statusColor =
    info?.status === "PAIRED" ? C.success
    : info?.status === "PAIRING" ? C.textDim
    : C.textFaint;

  if (loading) {
    return (
      <View style={[st.container, { paddingTop: insets.top + S.lg }]}>
        <Loading label="Loading device…" />
      </View>
    );
  }

  return (
    <KeyboardAware>
    <ScrollView
      style={[st.container, { paddingTop: insets.top + S.lg }]}
      contentContainerStyle={{ paddingBottom: 100 }}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={() => { setRefreshing(true); load(); }}
          tintColor={C.primary}
        />
      }
      {...scrollFormProps}
    >
      <Masthead kicker={isLive ? "Connected · just now" : "Linked to your account"}
                title="Device" />
      {error ? <ErrorText>{error}</ErrorText> : null}

      {info ? (
        <>
          <Card style={{ marginTop: S.lg }}>
            <View style={st.hero}>
              <DeviceArt size={82} live={isLive} />
              <View style={st.heroInfo}>
                <Text style={st.name} numberOfLines={2}>{info.name}</Text>
                <Text style={st.id} numberOfLines={1}>{info.device_id}</Text>
                <Text style={[st.statusCaps, { color: statusColor }]}>
                  {info.status}
                </Text>
              </View>
            </View>

            <View style={{ marginTop: S.lg, paddingTop: S.sm, borderTopWidth: 1, borderTopColor: C.border }}>
              <Row label="Firmware"
                   value={info.firmware_version ? `v${info.firmware_version}` : "—"} numeric />
              <Row label="Serial" value={info.serial_number || "—"} numeric />
              <Row label="Last seen" value={lastSeenLabel(info.last_seen)} />
              <Row label="Paired" value={dateLabel(info.paired_at)} />
              {/* Battery/storage come from the live BLE link when there is one;
                  the backend still reports null (the firmware doesn't send
                  them yet), so don't invent a number when nothing is connected. */}
              <Row label="Battery"
                   value={isLive && status?.batteryLevel != null ? `${status.batteryLevel}%` : "—"} numeric />
              <Row label="Waiting to upload"
                   value={isLive && status?.pendingUploads != null ? `${status.pendingUploads}` : "—"} numeric />
            </View>
          </Card>

          <SectionRule>Name</SectionRule>
          <View style={st.renameRow}>
            <TextField
              style={{ flex: 1 }}
              value={name}
              onChangeText={setName}
              placeholder={info.device_id}
              autoCapitalize="words"
              maxLength={64}
              returnKeyType="done"
              onSubmitEditing={onSaveName}
            />
            <Button
              label={busy === "rename" ? "Saving…" : "Save"}
              variant="secondary"
              disabled={busy !== ""}
              onPress={onSaveName}
            />
          </View>

          <SectionRule>Manage</SectionRule>
          <Text style={st.danger}>
            Removing or resetting keeps every recording and note already in
            your account — it only lets the hardware go.
          </Text>
          <Button
            label={busy === "unpair" ? "Removing…" : "Remove from account"}
            variant="secondary"
            disabled={busy !== ""}
            onPress={confirmUnpair}
          />
          <View style={{ height: S.sm }} />
          <Button
            label={busy === "reset" ? "Resetting…" : "Factory reset"}
            variant="ghost"
            disabled={busy !== ""}
            onPress={confirmFactoryReset}
          />
        </>
      ) : (
        <ErrorText>This device isn’t linked to your account.</ErrorText>
      )}
    </ScrollView>
    </KeyboardAware>
  );
}
