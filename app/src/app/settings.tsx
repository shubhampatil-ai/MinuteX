// src/app/settings.tsx — grouped app settings (iOS-style).
// Appearance, sync & AI preferences, notifications, permissions, privacy,
// about — plus honest Coming Soon rows for planned features. Local-only
// toggles persist on-device via lib/storage (no server sync yet, and the
// footnote says so).
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Linking, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, CAPS, FONT, useTheme, ColorScale, ThemeMode } from "../../lib/theme";
import {
  ComingSoonRow, ListRow, Row, SectionRule, SwitchRow,
} from "../../lib/ui";
import { store } from "../../lib/storage";
import { useDevice } from "../../lib/device-context";
import {
  checkNotificationPermission, requestNotificationPermissionDetailed,
} from "../../lib/notifications";
import {
  openAppSettings, useOnForeground, openLocationSettings,
} from "../../lib/permissions";
import { getSalesforceStatus } from "../../lib/api";

// Small persisted-boolean hook — settings survive app restarts.
function useStoredBool(key: string, initial: boolean): [boolean, (v: boolean) => void] {
  const [val, setVal] = useState(initial);
  useEffect(() => {
    store.getItemAsync(key).then((s) => {
      if (s === "1") setVal(true);
      else if (s === "0") setVal(false);
    });
  }, [key]);
  const set = (v: boolean) => {
    setVal(v);
    store.setItemAsync(key, v ? "1" : "0").catch(() => {});
  };
  return [val, set];
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 26, paddingTop: S.lg },
    dim: { ...T.bodyDim, fontSize: 13 },
    action: { fontFamily: FONT.bold, fontSize: 13, color: C.primary },
    note: { ...T.caption, marginTop: S.sm, lineHeight: 17 },
    // Appearance: three ruled rectangles, the same Chip treatment used for
    // filters elsewhere. No filled slider, no shadow.
    segments: { flexDirection: "row" as const, gap: 7 },
    segment: {
      flex: 1, paddingVertical: 9, alignItems: "center" as const, gap: 4,
      borderWidth: 1, borderColor: C.border, borderRadius: R.sm,
    },
    segmentActive: { backgroundColor: C.text, borderColor: C.text },
    segmentTxt: { fontFamily: FONT.semibold, fontSize: 11, color: C.textDim },
    segmentTxtActive: { color: C.bg },
    granted: { ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1.3, color: C.success },
  });
}

const APPEARANCE_OPTIONS: { value: ThemeMode | "system"; label: string; icon: any }[] = [
  { value: "light", label: "Light", icon: "sun.max.fill" },
  { value: "dark", label: "Dark", icon: "moon.fill" },
  { value: "system", label: "System", icon: "circle.lefthalf.filled" },
];

export default function SettingsScreen() {
  const router = useRouter();
  const { C, T, mode, isSystemDefault, setMode } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const [bleOk, setBleOk] = useState<boolean | null>(null);
  const [bleBlocked, setBleBlocked] = useState(false);
  const [notif, setNotif] = useState(false);
  // Phone-wide GPS switch, not a MinuteX permission — so it can't be fixed
  // from this app's settings page, only from the OS location screen.
  const [locOn, setLocOn] = useState(true);
  const [sfConnected, setSfConnected] = useState<boolean | null>(null);
  const [autoSync, setAutoSync] = useStoredBool("minutex.pref.autosync", true);
  const { checkPermissions, requestPermissions, areScanPrerequisitesMet } = useDevice();
  const [autoTranscribe, setAutoTranscribe] = useStoredBool("minutex.pref.autotranscribe", true);
  const [autoSummarize, setAutoSummarize] = useStoredBool("minutex.pref.autosummarize", true);

  // Read the REAL OS permission state — on mount and every time the user
  // returns from the phone's Settings app, so this screen never lies.
  const refresh = useCallback(async () => {
    const ble = await checkPermissions();
    setBleOk(ble);
    if (ble) setBleBlocked(false);
    setNotif(await checkNotificationPermission());
    // Probing needs BLUETOOTH_SCAN to be meaningful; without it the scanner
    // reports a permission error and we'd wrongly flag Location as off.
    setLocOn(ble ? await areScanPrerequisitesMet() : true);
  }, [checkPermissions, areScanPrerequisitesMet]);
  useEffect(() => { refresh(); }, [refresh]);
  useOnForeground(refresh);

  // Connection state for the Integrations row. Null while unknown so the row
  // shows "Checking…" rather than claiming "Not connected" before we know.
  const refreshSalesforce = useCallback(async () => {
    try {
      const s = await getSalesforceStatus();
      setSfConnected(s.connected);
    } catch {
      setSfConnected(null);  // leave it unstated rather than wrong
    }
  }, []);
  // Wrapped in a void arrow, not passed directly: useFocusEffect treats a
  // returned value as a cleanup function, and an async fn returns a Promise.
  useFocusEffect(useCallback(() => { refreshSalesforce(); }, [refreshSalesforce]));

  const fixBle = async () => {
    if (bleBlocked) { openAppSettings(); return; }
    const outcome = await requestPermissions();
    setBleOk(outcome === "granted");
    if (outcome === "blocked") setBleBlocked(true);
  };

  const toggleNotif = async (v: boolean) => {
    if (!v) {
      // The OS has no "un-grant" API — that lives in system settings.
      Alert.alert(
        "Turn off notifications",
        "Notifications are managed by your phone. Turn them off for MinuteX in system settings.",
        [
          { text: "Cancel", style: "cancel" },
          { text: "Open settings", onPress: openAppSettings },
        ]
      );
      return;
    }
    const outcome = await requestNotificationPermissionDetailed();
    setNotif(outcome === "granted");
    if (outcome === "blocked") {
      Alert.alert(
        "Notifications are blocked",
        "Notifications were turned off for MinuteX at the system level. Enable them in your phone's settings.",
        [
          { text: "Not now", style: "cancel" },
          { text: "Open settings", onPress: openAppSettings },
        ]
      );
    }
  };

  const activeSegment: ThemeMode | "system" = isSystemDefault ? "system" : mode;

  return (
    <ScrollView style={st.container} contentContainerStyle={{ paddingBottom: 40 }} showsVerticalScrollIndicator={false}>
      {/* Appearance */}
      <SectionRule style={{ marginTop: 0 }}>Appearance</SectionRule>
      <View style={st.segments}>
        {APPEARANCE_OPTIONS.map((opt) => {
          const active = activeSegment === opt.value;
          return (
            <Pressable
              key={opt.value}
              onPress={() => setMode(opt.value)}
              style={[st.segment, active && st.segmentActive]}
              accessibilityRole="button"
              accessibilityState={{ selected: active }}
            >
              <Icon name={opt.icon} tintColor={active ? C.bg : C.textDim} size={15} />
              <Text style={[st.segmentTxt, active && st.segmentTxtActive]}>{opt.label}</Text>
            </Pressable>
          );
        })}
      </View>

      {/* Sync & AI */}
      <SectionRule>Sync & AI</SectionRule>
      <SwitchRow
        label="Auto-sync over Wi-Fi"
        sub="Upload recordings whenever Wi-Fi is available"
        value={autoSync} onValueChange={setAutoSync}
      />
      <SwitchRow
        label="Auto transcription"
        sub="Transcribe each recording as soon as it uploads"
        value={autoTranscribe} onValueChange={setAutoTranscribe}
      />
      <SwitchRow
        label="Auto-written briefs"
        sub="Write the brief as soon as the transcript lands"
        value={autoSummarize} onValueChange={setAutoSummarize}
      />
      <Text style={st.note}>Preferences are applied on-device (v1); server-side sync coming later.</Text>

      {/* Integrations */}
      <SectionRule>Integrations</SectionRule>
      <ListRow
        icon="cloud.fill"
        label="Salesforce"
        sub={
          sfConnected === null ? "Push meeting notes into your CRM"
          : sfConnected ? "Connected"
          : "Not connected"
        }
        right={sfConnected ? <Text style={st.granted}>Connected</Text> : undefined}
        onPress={() => router.push("/salesforce")}
      />

      {/* Notifications & permissions */}
      <SectionRule>Notifications & permissions</SectionRule>
      <SwitchRow
        label="Notifications"
        sub="Brief-ready and device alerts"
        value={notif} onValueChange={toggleNotif}
      />
      <Row
        label="Bluetooth & nearby devices"
        value={
          bleOk === null ? <Text style={st.dim}>Checking…</Text> :
          bleOk ? <Text style={st.granted}>Granted</Text> :
          <Pressable onPress={fixBle}>
            <Text style={st.action}>{bleBlocked ? "Open settings" : "Grant"}</Text>
          </Pressable>
        }
      />
      {/* Surfaced only when it's actually off — Location being on is the
          normal state and a permanent "On" row here would imply MinuteX
          tracks location, which it doesn't. */}
      {!locOn && (
        <Row
          label="Location"
          value={
            <Pressable onPress={openLocationSettings}>
              <Text style={st.action}>Turn on</Text>
            </Pressable>
          }
        />
      )}
      <ListRow icon="gearshape.fill" label="Manage in system settings"
        sub="All MinuteX permissions on this phone" onPress={openAppSettings} />

      {/* Privacy & storage */}
      <SectionRule>Privacy & data</SectionRule>
      {/* Trash is where a deleted brief actually goes — the Desk only ever
          soft-deletes, so this is the one place a recording can be restored
          or genuinely removed. It belongs under Privacy & data for that
          reason, not under a generic "storage" heading. */}
      <ListRow icon="trash" label="Trash"
        sub="Restore deleted briefs, or remove them for good"
        onPress={() => router.push("/trash")} />
      <ListRow icon="lock.shield" label="Your data"
        sub="Recordings are stored encrypted in the cloud" />
      <ListRow icon="externaldrive.fill" label="On-device cache"
        sub="Managed automatically" />
      <ListRow icon="hand.raised.fill" label="Privacy policy"
        onPress={() => Linking.openURL("https://exceller.tech")} />

      {/* Coming soon */}
      <SectionRule>Not yet</SectionRule>
      <ComingSoonRow icon="brain.head.profile" label="Model selection"
        sub="Choose the model that writes your briefs" />
      <ComingSoonRow icon="icloud.and.arrow.up" label="Backup & restore"
        sub="Export and restore your library" />
      <ComingSoonRow icon="bell.fill" label="Notification centre"
        sub="All your alerts in one place" />

      {/* About */}
      <SectionRule>About</SectionRule>
      <Row label="App version" value="1.0.0" numeric />
      <ListRow icon="globe" label="Visit Exceller Tech"
        onPress={() => Linking.openURL("https://exceller.tech")} />
    </ScrollView>
  );
}
