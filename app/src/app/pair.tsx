// src/app/pair.tsx — device onboarding: welcome → permissions → scan →
// connect → Wi-Fi → success, with step progress. Logic is unchanged from the
// working flow (permission CHECK without prompting on mount; Wi-Fi submit
// watches the device's real Wi-Fi state instead of a fire-and-forget timeout)
// — this pass is the premium visual layer: device illustration, progress
// dots, permission cards, and an explicit success moment.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, Animated, Easing, Pressable, ScrollView, StyleSheet, Text, TextInput, View,
} from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, FONT, useTheme, ColorScale } from "../../lib/theme";
import { Button, Card, StatusPill, ErrorText, IconCircle, StepDots, KeyboardAware } from "../../lib/ui";
import { DeviceArt } from "../../lib/device-art";
import { useDevice } from "../../lib/device-context";
import type { DiscoveredDevice } from "../../lib/device";
import {
  requestNotificationPermissionDetailed, checkNotificationPermission,
} from "../../lib/notifications";
import {
  openAppSettings, useOnForeground, openLocationSettings,
} from "../../lib/permissions";

type Step = "welcome" | "perms" | "scan" | "found" | "connecting" | "wifi" | "done";

// Progress position for the dots — scan/found/connecting share one slot.
const STEP_INDEX: Record<Step, number> = {
  welcome: 0, perms: 1, scan: 2, found: 2, connecting: 2, wifi: 3, done: 4,
};

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    scroll: { flexGrow: 1, paddingHorizontal: S.xl, paddingTop: S.lg, paddingBottom: 40 },
    center: { alignItems: "center" as const, justifyContent: "center" as const, gap: S.md, paddingTop: 60 },
    h1: { ...T.h1 },
    h1Center: { ...T.h1, textAlign: "center" as const },
    h2: { ...T.h2, textAlign: "center" as const, marginTop: S.md },
    sub: { ...T.bodyDim, lineHeight: 22 },
    subCenter: { ...T.bodyDim, lineHeight: 22, textAlign: "center" as const },
    link: { color: C.primary, fontSize: 15, fontFamily: FONT.semibold, marginTop: S.lg },
    // welcome
    welcomeWrap: { flex: 1, alignItems: "center" as const, justifyContent: "center" as const, gap: S.lg },
    featureRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md, alignSelf: "stretch" as const },
    featureTxt: { ...T.body, flex: 1, lineHeight: 21 },
    // perms
    permRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md, paddingVertical: S.md },
    permBody: { flex: 1 },
    permLabel: { ...T.body, fontFamily: FONT.bold },
    permSub: { ...T.caption, marginTop: 2, lineHeight: 17 },
    // radar
    radarWrap: { width: 170, height: 170, alignItems: "center" as const, justifyContent: "center" as const },
    radarRing: { position: "absolute" as const, borderRadius: 999, borderWidth: 1.5, borderColor: C.primary },
    radarRing1: { width: 84, height: 84, opacity: 0.5 },
    radarRing2: { width: 128, height: 128, opacity: 0.3 },
    radarPulse: { width: 170, height: 170 },
    radarCore: {
      width: 64, height: 64, borderRadius: 32, backgroundColor: C.primarySoft,
      alignItems: "center" as const, justifyContent: "center" as const, borderWidth: 1, borderColor: C.primary,
    },
    // found list
    deviceRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md },
    deviceName: { ...T.h3, fontSize: 16 },
    deviceId: { ...T.caption, marginTop: 2 },
    // wifi
    // Wi-Fi fields are ruled lines, not boxes — matches login.
    fieldWrap: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 10,
      borderBottomWidth: 1, borderBottomColor: C.border,
      paddingVertical: 15, paddingHorizontal: 2,
    },
    fieldInput: { flex: 1, color: C.text, fontFamily: FONT.regular, fontSize: 15.5, paddingVertical: 0 },
    wifiStatus: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md },
    wifiStatusTxt: { ...T.bodyDim },
    // success — flat, no coloured glow
    checkWrap: {
      width: 110, height: 110, borderRadius: 55,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    successRing: { position: "absolute" as const, borderRadius: 999, borderWidth: 1.5, borderColor: C.success },
  });
}

export default function PairScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const params = useLocalSearchParams<{ step?: string }>();
  const {
    device, status, attach, sendWifi, scan,
    checkPermissions, requestPermissions, enableTransport, areScanPrerequisitesMet, btOn,
  } = useDevice();

  // If deep-linked to the wifi step and already connected, jump straight there.
  const [step, setStep] = useState<Step>(params.step === "wifi" && device ? "wifi" : "welcome");
  const [permsOk, setPermsOk] = useState(false);
  // Android said "never ask again" — the request dialog is dead; the only fix
  // is the OS settings page, so the UI switches to an "Open settings" path.
  const [permsBlocked, setPermsBlocked] = useState(false);
  const [notifBlocked, setNotifBlocked] = useState(false);
  // Android's master GPS toggle. Off => BLE scans silently return nothing,
  // so we surface it as its own fixable row instead of a mystery empty scan.
  const [locOn, setLocOn] = useState(true);
  // Set when we hand the user off to the OS location screen, so coming back
  // with it fixed resumes the scan instead of silently waiting for a re-tap.
  const awaitingLoc = useRef(false);
  // startScan is declared below refreshPerms; a ref breaks the cycle without
  // reordering the hooks or making refreshPerms depend on it.
  const startScanRef = useRef<(() => void) | null>(null);
  const [notifOk, setNotifOk] = useState<boolean | null>(null);
  const [found, setFound] = useState<DiscoveredDevice[]>([]);
  const [error, setError] = useState("");
  const stopScan = useRef<(() => void) | null>(null);

  // Wi-Fi form
  const [ssid, setSsid] = useState("");
  const [pass, setPass] = useState("");
  const [passHidden, setPassHidden] = useState(true);
  const [wifiSubmitting, setWifiSubmitting] = useState(false);

  // Radar pulse while scanning — reflects what BLE scanning actually is
  // (radiating out to find nearby advertisers).
  const pulse = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    // Only pulse while a scan is actually running (not on the timed-out state).
    if (step !== "scan" || error) { pulse.setValue(0); return; }
    const loop = Animated.loop(
      Animated.timing(pulse, { toValue: 1, duration: 1800, easing: Easing.out(Easing.ease), useNativeDriver: true })
    );
    loop.start();
    return () => loop.stop();
  }, [step, error, pulse]);
  const ringScale = pulse.interpolate({ inputRange: [0, 1], outputRange: [0.5, 1] });
  const ringOpacity = pulse.interpolate({ inputRange: [0, 0.7, 1], outputRange: [0.6, 0.15, 0] });

  // Success pop-in
  const successScale = useRef(new Animated.Value(0.5)).current;
  useEffect(() => {
    if (step !== "done") { successScale.setValue(0.5); return; }
    Animated.spring(successScale, { toValue: 1, useNativeDriver: true, friction: 5, tension: 80 }).start();
  }, [step, successScale]);

  // CHECK permissions silently (no prompt). Runs on mount AND every time the
  // app returns to foreground — so when the user flips the permission in the
  // phone's Settings and comes back, the pills update instantly.
  const refreshPerms = useCallback(async () => {
    const ok = await checkPermissions();
    const notif = await checkNotificationPermission();
    setPermsOk(ok);
    // Only meaningful once the permission is actually granted — probing the
    // scanner without BLUETOOTH_SCAN reports a permission error, not a
    // location one, which would show a misleading "Location off" row.
    const loc = ok ? await areScanPrerequisitesMet() : true;
    setLocOn(loc);
    // The user just came back from the OS location screen with it switched on
    // — pick the flow up where it left off rather than making them tap again.
    if (awaitingLoc.current && ok && btOn && loc) {
      awaitingLoc.current = false;
      setError("");
      startScanRef.current?.();
    }
    if (notif) setNotifOk(true);
    if (ok) { setPermsBlocked(false); setError(""); }
  }, [checkPermissions, areScanPrerequisitesMet, btOn]);
  useEffect(() => { refreshPerms(); }, [refreshPerms]);
  useOnForeground(refreshPerms);

  // Watch real Wi-Fi state once we've submitted creds (device read-back).
  const wifiState = status?.wifiState;
  useEffect(() => {
    if (!wifiSubmitting) return;
    if (wifiState === "connected") {
      setWifiSubmitting(false);
      setStep("done");
    } else if (wifiState === "failed") {
      setWifiSubmitting(false);
      setError("Couldn't connect to that network. Check the name and password.");
    }
  }, [wifiSubmitting, wifiState]);

  const begin = () => {
    setError("");
    // Skip the permission step entirely when everything is already granted.
    // locOn is part of the gate: with GPS off the scan finds nothing, so
    // jumping straight to it would strand the user on an empty radar.
    if (permsOk && btOn && locOn) startScan();
    else setStep("perms");
  };

  const askPerms = async () => {
    setError("");
    // Already blocked at the OS level — the dialog won't show again, so this
    // button's job is to take the user to the phone's Settings instead.
    if (permsBlocked) { openAppSettings(); return; }
    // Permissions FIRST — enableBluetooth() needs BLUETOOTH_CONNECT already
    // granted (not just requested) or the system enable prompt can silently
    // no-op on some Android builds.
    const outcome = await requestPermissions();
    setPermsOk(outcome === "granted");
    if (outcome === "blocked") {
      setPermsBlocked(true);
      setError("Permission was turned off for MinuteX. Enable Bluetooth & Nearby devices in your phone's settings — we'll pick it up automatically when you come back.");
      return;
    }
    if (outcome === "denied") {
      setError("Bluetooth & nearby-devices permission is needed to find your recorder.");
      return;
    }
    if (!btOn) {
      const on = await enableTransport();
      if (!on) {
        setError("Turn on Bluetooth to continue, then tap Allow & continue again.");
        return;
      }
    }
    // Last gate: the phone's location services. Unlike Bluetooth there is no
    // in-app "turn this on" dialog Android will show us, so we take the user
    // straight to the OS location screen — one tap there, swipe back, and
    // useOnForeground's refreshPerms picks it up and continues automatically.
    const loc = await areScanPrerequisitesMet();
    setLocOn(loc);
    if (!loc) {
      setError("Turn on Location to let Android scan for nearby devices. We'll continue as soon as you come back.");
      awaitingLoc.current = true;
      openLocationSettings();
      return;
    }
    startScan();
  };

  const askNotif = async () => {
    if (notifBlocked) { openAppSettings(); return; }
    const outcome = await requestNotificationPermissionDetailed();
    setNotifOk(outcome === "granted");
    if (outcome === "blocked") setNotifBlocked(true);
  };

  // Always tear down any previous scan before starting a new one — overlapping
  // native scans orphan the older one, which Android can punish by cancelling
  // a later connectToDevice() outright.
  const startScan = useCallback(() => {
    stopScan.current?.();
    setError(""); setFound([]); setStep("scan");
    const handle = scan(
      (d) => { setFound((p) => (p.some((x) => x.id === d.id) ? p : [...p, d])); setStep("found"); },
      {
        timeoutMs: 15000,
        onTimeout: () => { setStep("scan"); setError("No recorder found. Make sure it's on and nearby."); },
      },
    );
    // Call remove() ON the handle rather than storing a bare reference to it —
    // an adapter is free to implement remove() as a method that needs `this`.
    stopScan.current = () => handle.remove();
  }, [scan]);

  // Keep the ref in sync in an effect, not during render (writing a ref in the
  // render body is what react-hooks/refs flags, and it's unsafe under
  // concurrent rendering).
  useEffect(() => { startScanRef.current = startScan; }, [startScan]);

  useEffect(() => () => stopScan.current?.(), []);

  const connect = async (d: DiscoveredDevice) => {
    stopScan.current?.();
    setStep("connecting"); setError("");
    try {
      await attach(d);
      setStep("wifi");
    } catch (e: any) {
      setError("Connection failed: " + (e?.message ?? "unknown"));
      setStep("found");
    }
  };

  const submitWifi = async () => {
    if (!ssid.trim()) { setError("Enter a Wi-Fi name."); return; }
    setError(""); setWifiSubmitting(true);
    try {
      await sendWifi(ssid.trim(), pass);
      // Now we WAIT for the device to report its Wi-Fi state via the effect above.
    } catch (e: any) {
      setWifiSubmitting(false);
      setError("Failed to send Wi-Fi: " + (e?.message ?? "unknown"));
    }
  };

  return (
    <KeyboardAware style={st.container}>
      <StepDots total={5} index={STEP_INDEX[step]} style={{ marginTop: S.md, paddingHorizontal: 26 }} />

      <ScrollView contentContainerStyle={st.scroll} keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}>
        {error ? <ErrorText>{error}</ErrorText> : null}

        {/* ---- 1 · Welcome ---- */}
        {step === "welcome" && (
          <View style={st.welcomeWrap}>
            <DeviceArt size={150} />
            <Text style={st.h1Center}>Meet your recorder</Text>
            <Text style={st.subCenter}>
              Clip it on, press record, and every meeting lands here — transcribed
              and summarized by AI.
            </Text>
            <View style={{ gap: S.sm, alignSelf: "stretch", marginTop: S.sm }}>
              <View style={st.featureRow}>
                <IconCircle name="dot.radiowaves.left.and.right" size={36} />
                <Text style={st.featureTxt}>Pairs over Bluetooth in seconds</Text>
              </View>
              <View style={st.featureRow}>
                <IconCircle name="wifi" size={36} />
                <Text style={st.featureTxt}>Uploads recordings over your Wi-Fi</Text>
              </View>
              <View style={st.featureRow}>
                <IconCircle name="sparkles" size={36} />
                <Text style={st.featureTxt}>AI summaries ready when you are</Text>
              </View>
            </View>
            <Button label="Get started" onPress={begin} style={{ alignSelf: "stretch", marginTop: S.md }} />
            <Pressable onPress={() => router.back()}><Text style={st.link}>Not now</Text></Pressable>
          </View>
        )}

        {/* ---- 2 · Permissions ---- */}
        {step === "perms" && (
          <View style={{ gap: S.md, paddingTop: S.lg }}>
            <Text style={st.h1}>A couple of permissions</Text>
            <Text style={st.sub}>
              MinuteX talks to your recorder over Bluetooth. We only ask once.
            </Text>
            <Card>
              <View style={st.permRow}>
                <IconCircle name="dot.radiowaves.left.and.right" />
                <View style={st.permBody}>
                  <Text style={st.permLabel}>Bluetooth</Text>
                  <Text style={st.permSub}>Connects to your recorder nearby</Text>
                </View>
                <StatusPill label={btOn ? "Ready" : "Needed"} color={btOn ? C.success : C.warn} />
              </View>
              <View style={st.permRow}>
                <IconCircle name="location" />
                <View style={st.permBody}>
                  <Text style={st.permLabel}>Nearby devices</Text>
                  <Text style={st.permSub}>Android requires this to scan for devices</Text>
                </View>
                <StatusPill label={permsOk ? "Ready" : "Needed"} color={permsOk ? C.success : C.warn} />
              </View>
              {/* Android's master GPS switch. Shown only when it's actually
                  off, since on a normal phone it already is on and an extra
                  "Ready" row here would just be noise. Tapping it opens the
                  OS location screen directly — no Settings spelunking. */}
              {!locOn && (
                <View style={st.permRow}>
                  <IconCircle name="location" />
                  <View style={st.permBody}>
                    <Text style={st.permLabel}>Location</Text>
                    <Text style={st.permSub}>Android can&apos;t scan for devices while Location is off</Text>
                  </View>
                  <Pressable onPress={openLocationSettings} hitSlop={8}>
                    <Text style={{ color: C.primary, fontSize: 14, fontFamily: FONT.bold }}>Turn on</Text>
                  </Pressable>
                </View>
              )}
              <View style={st.permRow}>
                <IconCircle name="bell.badge" />
                <View style={st.permBody}>
                  <Text style={st.permLabel}>Notifications <Text style={st.permSub}>(optional)</Text></Text>
                  <Text style={st.permSub}>Get notified when summaries are ready</Text>
                </View>
                {notifOk ? (
                  <StatusPill label="Ready" color={C.success} />
                ) : (
                  <Pressable onPress={askNotif} hitSlop={8}>
                    <Text style={{ color: C.primary, fontSize: 14, fontFamily: FONT.bold }}>
                      {notifBlocked ? "Settings" : "Enable"}
                    </Text>
                  </Pressable>
                )}
              </View>
            </Card>
            <Button
              label={
                permsBlocked ? "Open phone settings"
                  : permsOk && !locOn ? "Turn on Location"
                  : "Allow & continue"
              }
              icon={permsBlocked || (permsOk && !locOn)
                ? <Icon name="gearshape.fill" tintColor="#FFFFFF" size={16} /> : undefined}
              onPress={askPerms}
            />
            <Text style={{ ...T.caption, textAlign: "center", lineHeight: 17 }}>
              Recording happens on the MinuteX device — the app never uses your
              phone's microphone.
            </Text>
          </View>
        )}

        {/* ---- 3 · Scanning ---- */}
        {step === "scan" && (
          <View style={st.center}>
            <View style={st.radarWrap}>
              <Animated.View style={[st.radarRing, st.radarPulse, { transform: [{ scale: ringScale }], opacity: ringOpacity }]} />
              <View style={[st.radarRing, st.radarRing2]} />
              <View style={[st.radarRing, st.radarRing1]} />
              <View style={st.radarCore}><Icon name="dot.radiowaves.left.and.right" tintColor={C.primary} size={28} /></View>
            </View>
            <Text style={st.h2}>{error ? "No recorder found" : "Looking for your recorder…"}</Text>
            <Text style={st.subCenter}>
              {error ? "Make sure it's powered on and within reach." : "Keep the device close to your phone."}
            </Text>
            {error ? (
              <Button label="Scan again" onPress={startScan} style={{ alignSelf: "stretch", marginTop: S.md }} />
            ) : null}
            <Pressable onPress={() => { stopScan.current?.(); router.back(); }}><Text style={st.link}>Cancel</Text></Pressable>
          </View>
        )}

        {/* ---- 3b · Found ---- */}
        {step === "found" && (
          <View style={{ gap: S.md, paddingTop: S.lg }}>
            <Text style={st.h1}>Select your recorder</Text>
            <Text style={st.sub}>We found {found.length === 1 ? "a device" : `${found.length} devices`} nearby.</Text>
            {found.map((d) => (
              <Card key={d.id} onPress={() => connect(d)}>
                <View style={st.deviceRow}>
                  <IconCircle name="waveform" />
                  <View style={{ flex: 1 }}>
                    <Text style={st.deviceName}>{d.name ?? "Recorder"}</Text>
                    <Text style={st.deviceId}>{d.id}</Text>
                  </View>
                  <Icon name="chevron.right" tintColor={C.primary} size={18} />
                </View>
              </Card>
            ))}
            <Button label="Scan again" variant="ghost" onPress={startScan} />
          </View>
        )}

        {/* ---- 3c · Connecting ---- */}
        {step === "connecting" && (
          <View style={st.center}>
            {/* Amber means pairing — the device's own signal language. */}
            <DeviceArt size={110} pairing />
            <ActivityIndicator color={C.primary} size="large" />
            <Text style={st.h2}>Connecting…</Text>
            <Text style={st.subCenter}>Hold tight — establishing a secure link.</Text>
          </View>
        )}

        {/* ---- 4 · Wi-Fi ---- */}
        {step === "wifi" && (
          <View style={{ gap: S.md, paddingTop: S.lg }}>
            <Text style={st.h1}>Connect to Wi-Fi</Text>
            <Text style={st.sub}>
              Your recorder uploads over Wi-Fi, so recordings appear here even
              when your phone is away.
            </Text>
            <View style={st.fieldWrap}>
              <Icon name="wifi" tintColor={C.textFaint} size={18} />
              <TextInput
                style={st.fieldInput} placeholder="Wi-Fi name (SSID)" placeholderTextColor={C.textFaint}
                autoCapitalize="none" autoCorrect={false}
                value={ssid} onChangeText={setSsid} editable={!wifiSubmitting}
              />
            </View>
            <View style={st.fieldWrap}>
              <Icon name="lock" tintColor={C.textFaint} size={18} />
              <TextInput
                style={st.fieldInput} placeholder="Password" placeholderTextColor={C.textFaint}
                autoCapitalize="none" secureTextEntry={passHidden}
                value={pass} onChangeText={setPass} editable={!wifiSubmitting}
              />
              <Pressable onPress={() => setPassHidden(!passHidden)} hitSlop={10}
                accessibilityLabel={passHidden ? "Show password" : "Hide password"}>
                <Icon name={passHidden ? "eye.slash" : "eye"} tintColor={C.textFaint} size={18} />
              </Pressable>
            </View>

            {/* Real-time Wi-Fi state from the device. */}
            {wifiSubmitting ? (
              <View style={st.wifiStatus}>
                <ActivityIndicator color={C.warn} />
                <Text style={st.wifiStatusTxt}>
                  {wifiState === "connecting" ? `Connecting to ${status?.wifiSsid || ssid}…` : "Sending to device…"}
                </Text>
              </View>
            ) : wifiState === "connected" ? (
              <StatusPill label={`Connected: ${status?.wifiSsid || "Wi-Fi"}`} color={C.success} />
            ) : null}

            <Button label={wifiSubmitting ? "Connecting…" : "Save & connect"} onPress={submitWifi}
              loading={wifiSubmitting} />
            <Button label="Skip for now" variant="ghost" onPress={() => router.replace("/")} />
          </View>
        )}

        {/* ---- 5 · Success ---- */}
        {step === "done" && (
          <View style={st.center}>
            <Animated.View style={{ transform: [{ scale: successScale }], alignItems: "center", justifyContent: "center" }}>
              <View style={[st.successRing, { width: 150, height: 150, opacity: 0.25 }]} />
              <View style={[st.successRing, { position: "absolute", width: 190, height: 190, opacity: 0.12 }]} />
              {/* Settled green — success state for "this is resolved". */}
              <View style={[st.checkWrap, { backgroundColor: C.success }]}>
                <Icon name="checkmark" tintColor="#FFFFFF" size={46} />
              </View>
            </Animated.View>
            <Text style={st.h2}>You're all set</Text>
            <Text style={st.subCenter}>
              {device?.name ?? "Your recorder"} is connected{wifiState === "connected" ? " and on Wi-Fi" : ""}.
              New recordings will appear in your files automatically.
            </Text>
            <Button label="Go to my files" onPress={() => router.replace("/")} style={{ alignSelf: "stretch", marginTop: S.md }} />
          </View>
        )}
      </ScrollView>
    </KeyboardAware>
  );
}
