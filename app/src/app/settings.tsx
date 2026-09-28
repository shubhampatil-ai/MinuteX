// src/app/settings.tsx — grouped app settings.
//
// One screen of short, titled groups on single surfaces (lib/kit.tsx), in the
// order people look for things: how it looks, how it records, what happens
// after, what it may notify about, what it is connected to, their data, the
// phone's permissions, and about.
//
// THE RULE FOR EVERY ROW: it either does something real, reports something
// real, or says plainly that it is not available yet. Three rows used to fail
// that. "Auto-sync over Wi-Fi", "Auto transcription" and "Auto-written briefs"
// were switches whose values were saved and never read by anything — turning
// them off changed nothing, because uploading, transcription and brief writing
// always happen. They are now stated as facts, not offered as choices. (The
// stored keys are left alone; nothing reads them.)
//
// Notifications has exactly ONE switch: "Brief-ready notifications", the only
// OS push MinuteX sends. It is this device's `enabled` flag on the backend's
// PushTokens row — the sender reads it — so turning it off genuinely stops
// delivery (lib/push.ts, cloud/shared/push_delivery.py). There are no task
// reminders, quiet hours or per-type switches, because nothing sends them.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Linking, Platform, ScrollView, StyleSheet, Text } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { S, FONT, GUTTER, COMPANY, useTheme, ColorScale, ThemeMode } from "../../lib/theme";
import { SegmentedControl, SettingGroup, SettingRow, ToggleRow } from "../../lib/kit";
import { APP_VERSION } from "../../lib/app-info";
import { useDevice } from "../../lib/device-context";
import {
  BRIEF_READY_PROMPT, cachedBriefReadyEnabled, getPushPermission, pushUnavailableReason,
  requestPushPermission, setBriefReadyEnabled,
} from "../../lib/push";
import { nextPermissionStep, type PushPermission } from "../../lib/push-logic";
import {
  openAppSettings, useOnForeground, openLocationSettings,
} from "../../lib/permissions";
import { useIntegrations } from "../../lib/integrations";
import { canSilenceRinger, openSilenceRingerSettings } from "../../lib/audio-focus";
import {
  canUseWavEngine, getRecEngine, loadRecEngine, setRecEngine,
} from "../../lib/rec-engine";
import { refreshPermission, requestPermission, type MicPermission } from "../../lib/rec-controller";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: GUTTER },
    action: { fontFamily: FONT.semibold, fontSize: 14, color: C.primary },
    granted: { fontFamily: FONT.medium, fontSize: 13.5, color: C.success },
    dim: { fontFamily: FONT.regular, fontSize: 13.5, color: C.textDim },
  });
}

type Appearance = ThemeMode | "system";

export default function SettingsScreen() {
  const router = useRouter();
  const { C, mode, isSystemDefault, setMode } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [bleOk, setBleOk] = useState<boolean | null>(null);
  const [bleBlocked, setBleBlocked] = useState(false);
  // Brief-ready push for THIS device: the OS permission, and the server-side
  // switch (null until known — the row renders "off" rather than guessing).
  const [pushPerm, setPushPerm] = useState<PushPermission>("unavailable");
  const [briefReady, setBriefReady] = useState<boolean | null>(null);
  const [pushBusy, setPushBusy] = useState(false);
  const [mic, setMic] = useState<MicPermission>("unknown");
  const [ringerOk, setRingerOk] = useState(false);
  // Phone-wide GPS switch, not a MinuteX permission — so it can't be fixed
  // from this app's settings page, only from the OS location screen.
  const [locOn, setLocOn] = useState(true);
  // Read from the app-wide integration context rather than fetching here:
  // one source of truth means this row cannot disagree with the Integrations
  // screen it links to. Null while the first load is in flight, so the row
  // says nothing rather than claiming "Nothing connected yet" prematurely.
  const {
    integrations, loading: integrationsLoading, refresh: refreshIntegrations,
  } = useIntegrations();
  const connectedCount = integrationsLoading && !integrations.length
    ? null
    : integrations.filter((i) => i.connected).length;
  const { checkPermissions, requestPermissions, areScanPrerequisitesMet } = useDevice();

  // Experimental recording engine. Android-only and only in a build that
  // bundled the native module, so the row is hidden entirely elsewhere rather
  // than shown disabled — a toggle that cannot do anything is worse than none.
  const [wavEngine, setWavEngine] = useState(false);
  useEffect(() => {
    void loadRecEngine().then(() => setWavEngine(getRecEngine() === "wav"));
  }, []);

  // Read the REAL OS permission state — on mount and every time the user
  // returns from the phone's Settings app, so this screen never lies.
  const refresh = useCallback(async () => {
    const ble = await checkPermissions();
    setBleOk(ble);
    if (ble) setBleBlocked(false);
    setPushPerm(await getPushPermission());
    setBriefReady(await cachedBriefReadyEnabled());
    setMic(await refreshPermission());
    setRingerOk(canSilenceRinger());
    // Probing needs BLUETOOTH_SCAN to be meaningful; without it the scanner
    // reports a permission error and we'd wrongly flag Location as off.
    setLocOn(ble ? await areScanPrerequisitesMet() : true);
  }, [checkPermissions, areScanPrerequisitesMet]);
  useEffect(() => { refresh(); }, [refresh]);
  useOnForeground(refresh);

  // Re-read integration status on every return to this screen. Wrapped in a
  // void arrow: useFocusEffect treats a returned value as a cleanup function,
  // and an async fn returns a Promise.
  useFocusEffect(useCallback(() => { refreshIntegrations(); }, [refreshIntegrations]));

  const fixBle = async () => {
    if (bleBlocked) { openAppSettings(); return; }
    const outcome = await requestPermissions();
    setBleOk(outcome === "granted");
    if (outcome === "blocked") setBleBlocked(true);
  };

  const fixMic = async () => {
    if (mic === "blocked") { openAppSettings(); return; }
    setMic(await requestPermission());
  };

  // Flip this device's brief-ready switch on the SERVER. Throws surface as an
  // alert and the switch snaps back — it never shows a state the backend did
  // not accept.
  const applyBriefReady = async (enabled: boolean) => {
    setPushBusy(true);
    try {
      const state = await setBriefReadyEnabled(enabled);
      setBriefReady(state ? state.enabled : enabled ? null : false);
      if (enabled && !state) {
        Alert.alert("Couldn't turn on notifications", "This device couldn't get a push token. Try again in a moment.");
      }
    } catch (e: any) {
      Alert.alert(
        enabled ? "Couldn't turn on notifications" : "Couldn't turn off notifications",
        e?.message || "Check your connection and try again."
      );
    } finally {
      setPushBusy(false);
      setPushPerm(await getPushPermission());
    }
  };

  const toggleBriefReady = async (v: boolean) => {
    if (pushBusy) return;
    if (!v) {
      // OFF needs no permission: the server simply stops sending here. The OS
      // permission is left alone — that is the phone's setting, not ours.
      if (pushPerm === "granted") await applyBriefReady(false);
      else setBriefReady(false);
      return;
    }
    const step = nextPermissionStep(pushPerm);
    if (step === "none") {
      Alert.alert("Not available", pushUnavailableReason() ?? "Push notifications aren't available here.");
    } else if (step === "settings") {
      // Refused for good: the OS will not show its dialog again. Only the
      // system Settings screen can change it, so that is the only offer.
      Alert.alert(
        "Notifications are off for MinuteX",
        "Turn them on for MinuteX in your phone's settings, then come back.",
        [{ text: "Not now", style: "cancel" }, { text: "Open settings", onPress: openAppSettings }]
      );
    } else if (step === "register") {
      await applyBriefReady(true);
    } else {
      // explain (first time) or request (a soft deny the OS will re-ask).
      const ask = async () => {
        const after = await requestPushPermission();
        setPushPerm(after);
        if (after === "granted") await applyBriefReady(true);
      };
      if (step === "request") { await ask(); return; }
      Alert.alert(BRIEF_READY_PROMPT.title, BRIEF_READY_PROMPT.body, [
        { text: BRIEF_READY_PROMPT.later, style: "cancel" },
        { text: BRIEF_READY_PROMPT.enable, onPress: () => { void ask(); } },
      ]);
    }
  };

  const openRinger = async () => {
    const opened = await openSilenceRingerSettings();
    if (!opened) {
      Alert.alert(
        "Could not open Settings",
        "Look for “Do Not Disturb access” in your phone's notification settings and enable it for MinuteX."
      );
    }
  };

  const appearance: Appearance = isSystemDefault ? "system" : mode;
  // The trailing answer for a permission row. The ROW is the button when the
  // permission can be fixed (see onPress below), so the target is full width.
  const permissionValue = (ok: boolean | null, blocked: boolean) =>
    ok === null ? <Text style={st.dim}>Checking…</Text>
      : ok ? <Text style={st.granted}>Allowed</Text>
        : <Text style={st.action}>{blocked ? "Open settings" : "Allow"}</Text>;

  return (
    <ScrollView
      style={st.container}
      contentContainerStyle={{ paddingTop: S.sm, paddingBottom: S.x4 }}
      showsVerticalScrollIndicator={false}
    >
      {/* ---- Appearance ---- */}
      <SettingGroup
        title="Appearance"
        footer={appearance === "system" ? "Follows your phone's light or dark setting." : undefined}
        style={{ marginTop: S.md }}
      >
        <SettingRow
          title="Theme"
          trailing={
            <SegmentedControl<Appearance>
              style={{ width: 216 }}
              value={appearance}
              onChange={setMode}
              options={[
                { key: "light", label: "Light" },
                { key: "dark", label: "Dark" },
                { key: "system", label: "System" },
              ]}
            />
          }
        />
      </SettingGroup>

      {/* ---- Recording ---- */}
      <SettingGroup
        title="Recording"
        footer={Platform.OS === "android"
          ? "Silencing the ringer needs Do Not Disturb access, which Android only grants from its own settings."
          : undefined}
      >
        {canUseWavEngine() ? (
          <ToggleRow
            title="High-quality WAV recording"
            description="Experimental · 16 kHz mono PCM, ~1.9 MB/min (AAC is ~1 MB/min). Applies to the next recording."
            value={wavEngine}
            onValueChange={(v) => {
              setWavEngine(v);
              void setRecEngine(v ? "wav" : "aac");
            }}
          />
        ) : null}
        <SettingRow
          title="Your MinuteX device"
          description="Pair, check battery and Wi-Fi"
          onPress={() => router.push("/devices")}
        />
        {Platform.OS === "android" ? (
          <SettingRow
            title="Silence ringer while recording"
            description="Keeps a ringtone out of the recording"
            trailing={ringerOk
              ? <Text style={st.granted}>Allowed</Text>
              : <Text style={st.action}>Set up</Text>}
            onPress={ringerOk ? undefined : () => { void openRinger(); }}
          />
        ) : null}
      </SettingGroup>

      {/* ---- Sync & AI — stated as facts, because they are not choices ---- */}
      <SettingGroup
        title="Sync & AI"
        footer="These happen for every recording and aren't configurable yet."
      >
        <SettingRow
          title="Uploads"
          value="Automatic"
          description="Phone recordings upload whenever you're online; your device uploads over its Wi-Fi."
        />
        <SettingRow
          title="Transcripts & briefs"
          value="Automatic"
          description="Every upload is transcribed and written up as soon as it lands."
        />
        <SettingRow title="AI model" description="Choose the model that writes your briefs" soon />
      </SettingGroup>

      {/* ---- Notifications ---- */}
      <SettingGroup title="Notifications">
        <ToggleRow
          title="Brief-ready notifications"
          description={
            pushUnavailableReason()
              ?? (pushPerm === "blocked" || pushPerm === "denied"
                ? "Notifications are off for MinuteX in your phone's settings"
                : "A notification on this device when a meeting brief is ready")
          }
          value={pushPerm === "granted" && briefReady === true}
          onValueChange={(v) => { void toggleBriefReady(v); }}
          disabled={pushBusy || pushPerm === "unavailable"}
        />
        <SettingRow
          title="Notification centre"
          description="Briefs ready, task assignments and due dates"
          onPress={() => router.push("/notifications" as never)}
        />
      </SettingGroup>

      {/* ---- Integrations ---- */}
      <SettingGroup title="Integrations">
        {/* ONE row, not one per application: Gmail and the Soon cards all
            live behind Connected apps, so there is a single place to
            look for "what is MinuteX linked to". */}
        <SettingRow
          title="Connected apps"
          value={
            connectedCount === null ? undefined
              : connectedCount === 0 ? "None"
                : `${connectedCount} connected`
          }
          description={connectedCount === null ? "Gmail and more" : undefined}
          onPress={() => router.push("/integrations")}
        />
      </SettingGroup>

      {/* ---- Privacy & data ---- */}
      <SettingGroup title="Privacy & data">
        {/* Trash is where a deleted brief actually goes — MinuteX only ever
            soft-deletes, so this is the one place a recording can be restored
            or genuinely removed. */}
        <SettingRow
          title="Trash"
          description="Restore deleted briefs, or remove them for good"
          onPress={() => router.push("/trash")}
        />
        <SettingRow title="Your data" description="Recordings are stored encrypted in the cloud" />
        <SettingRow title="On-device cache" value="Managed automatically" />
        <SettingRow title="Privacy policy" onPress={() => Linking.openURL("https://exceller.tech")} />
        <SettingRow title="Backup & restore" description="Export and restore your library" soon />
      </SettingGroup>

      {/* ---- Device & permissions — the REAL OS state, re-read on return ---- */}
      <SettingGroup title="Permissions">
        <SettingRow
          title="Microphone"
          description="Needed to record on this phone"
          trailing={permissionValue(mic === "unknown" ? null : mic === "granted", mic === "blocked")}
          onPress={mic === "denied" || mic === "blocked" ? () => { void fixMic(); } : undefined}
        />
        <SettingRow
          title="Bluetooth & nearby devices"
          description="Needed to find and control your MinuteX device"
          trailing={permissionValue(bleOk, bleBlocked)}
          onPress={bleOk === false ? () => { void fixBle(); } : undefined}
        />
        {/* Surfaced only when it's actually off — Location being on is the
            normal state, and a permanent "On" row would imply MinuteX tracks
            location, which it doesn't. */}
        {!locOn ? (
          <SettingRow
            title="Location"
            description="Android needs it switched on to scan for Bluetooth devices"
            trailing={<Text style={st.action}>Turn on</Text>}
            onPress={openLocationSettings}
          />
        ) : null}
        <SettingRow
          title="Manage in system settings"
          description="All MinuteX permissions on this phone"
          onPress={openAppSettings}
        />
      </SettingGroup>

      {/* ---- About ---- */}
      <SettingGroup title="About">
        <SettingRow title="Version" value={APP_VERSION} />
        <SettingRow title={COMPANY} description="Visit exceller.tech" onPress={() => Linking.openURL("https://exceller.tech")} />
        <SettingRow title="Help & support" soon />
      </SettingGroup>
    </ScrollView>
  );
}
