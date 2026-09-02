// src/app/pair-device.tsx — MinuteX: pair a device to this account.
//
// This is the OWNERSHIP flow (who the recordings belong to), not the BLE
// Wi-Fi setup in pair.tsx. Two steps, mirroring the backend:
//
//   1. Identify the device  -> POST /devices/pair-request  -> 6-digit code
//   2. Confirm the code     -> POST /devices/pair          -> PAIRED
//
// The device shows the same code, so entering it proves the user is holding
// the right hardware. A device can belong to exactly one account: the
// backend answers 409 if someone already owns it.
//
// QR SCANNING (prototype note): step 1 takes the device id by hand for now.
// A scanner would fill the same field — expo-camera is a native module and
// this build doesn't include it yet, so it's deliberately left out rather
// than shipped as a dead button. `applyScannedPayload` below is the seam:
// point a scanner's result at it and nothing else has to change.
import React, { useMemo, useState } from "react";
import {
  KeyboardAvoidingView, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { useRouter } from "expo-router";
import { S, FONT, TABULAR, useTheme, ColorScale } from "../../lib/theme";
import {
  Button, ErrorText, IconCircle, StepDots, SuccessText, TextField,
} from "../../lib/ui";
import {
  ApiError, clearToken, pairDevice, requestPairing,
} from "../../lib/api";

// The device's QR encodes {"device_id":...,"serial_number":...,...} — see
// tests/mock_device.py show_pairing_qr(). Accept a bare id too, so a typed
// id and a scanned payload go down the same path.
export function applyScannedPayload(raw: string): string {
  const s = (raw ?? "").trim();
  if (!s) return "";
  if (s.startsWith("{")) {
    try {
      const obj = JSON.parse(s);
      if (obj && typeof obj.device_id === "string") return obj.device_id;
    } catch {
      return "";
    }
    return "";
  }
  return s;
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    scroll: {
      flexGrow: 1, justifyContent: "center" as const,
      paddingHorizontal: S.xl, paddingVertical: 60,
    },
    h1: { ...T.h2, textAlign: "center" as const, marginTop: S.lg },
    sub: {
      ...T.bodyDim, lineHeight: 22, marginTop: S.sm, marginBottom: S.xl,
      textAlign: "center" as const,
    },
    hint: { ...T.caption, lineHeight: 18, marginTop: S.md },
    codeInput: {
      ...TABULAR, fontSize: 26, letterSpacing: 8, textAlign: "center" as const,
    },
    deviceLine: {
      fontFamily: FONT.semibold, fontSize: 13.5, color: C.text,
      textAlign: "center" as const, marginBottom: S.sm,
    },
  });
}

export default function PairDeviceScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);

  const [step, setStep] = useState<0 | 1>(0);
  const [deviceId, setDeviceId] = useState("");
  const [code, setCode] = useState("");
  const [expiresIn, setExpiresIn] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  const handle401 = async () => { await clearToken(); router.replace("/login"); };

  const onRequest = async () => {
    setError(""); setOk("");
    const id = applyScannedPayload(deviceId);
    if (!id) { setError("Enter the device ID shown on your device."); return; }
    setDeviceId(id);
    setBusy(true);
    try {
      const res = await requestPairing(id);
      setExpiresIn(res.expires_in);
      setStep(1);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return handle401();
      if (e instanceof ApiError && e.status === 409) {
        setError("That device is already paired to another account. Reset it first.");
      } else if (e instanceof ApiError && e.status === 404) {
        setError("No such device. Check the ID printed on it.");
      } else {
        setError(e instanceof ApiError ? e.message : "Could not start pairing.");
      }
    } finally {
      setBusy(false);
    }
  };

  const onConfirm = async () => {
    setError(""); setOk("");
    const c = code.trim();
    if (!/^\d{6}$/.test(c)) { setError("Enter the 6-digit code."); return; }
    setBusy(true);
    try {
      const dev = await pairDevice(deviceId, c);
      setOk(dev.name || dev.device_id);
      setTimeout(() => router.replace("/devices"), 900);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return handle401();
      if (e instanceof ApiError && e.status === 410) {
        setError("That code expired. Start again to get a new one.");
        setStep(0); setCode("");
      } else if (e instanceof ApiError && e.status === 403) {
        setError("That code doesn't match. Check the device and try again.");
      } else if (e instanceof ApiError && e.status === 409) {
        setError("That device was just paired to another account.");
      } else {
        setError(e instanceof ApiError ? e.message : "Could not pair the device.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: C.bg }}
      // "padding" on BOTH platforms: on Android 15+ edge-to-edge means the OS
      // no longer resizes the window, and RN's KeyboardAvoidingView ignores the
      // keyboard entirely when behavior is undefined. See lib/ui.tsx.
      behavior="padding"
    >
      <ScrollView
        contentContainerStyle={st.scroll}
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}
      >
        <View style={{ alignItems: "center" }}>
          <IconCircle name="checkmark.seal.fill" size={72} iconSize={30} />
        </View>
        <StepDots total={2} index={step} style={{ marginTop: S.lg }} />

        {step === 0 ? (
          <>
            <Text style={st.h1}>Pair your device</Text>
            <Text style={st.sub}>
              Enter the device ID printed on your MinuteX. We'll show a code to
              confirm it's really yours.
            </Text>
            <TextField
              value={deviceId}
              onChangeText={setDeviceId}
              placeholder="esp32-001"
              autoCapitalize="none"
              autoCorrect={false}
              returnKeyType="go"
              onSubmitEditing={onRequest}
            />
            {error ? <ErrorText>{error}</ErrorText> : null}
            <View style={{ height: S.lg }} />
            <Button
              label={busy ? "Starting…" : "Continue"}
              disabled={busy}
              onPress={onRequest}
            />
            <Text style={st.hint}>
              One device belongs to one account. Its recordings land in yours,
              and nobody else can see them.
            </Text>
          </>
        ) : (
          <>
            <Text style={st.h1}>Enter the code</Text>
            <Text style={st.sub}>
              Your device is showing a 6-digit code
              {expiresIn ? ` — it's good for ${Math.round(expiresIn / 60)} minutes` : ""}.
            </Text>
            <Text style={st.deviceLine}>{deviceId}</Text>
            <TextField
              value={code}
              onChangeText={(t) => setCode(t.replace(/\D/g, "").slice(0, 6))}
              placeholder="000000"
              keyboardType="number-pad"
              maxLength={6}
              style={st.codeInput}
              returnKeyType="go"
              onSubmitEditing={onConfirm}
            />
            {error ? <ErrorText>{error}</ErrorText> : null}
            {ok ? <SuccessText>Paired — {ok}</SuccessText> : null}
            <View style={{ height: S.lg }} />
            <Button
              label={busy ? "Pairing…" : "Pair device"}
              disabled={busy || !!ok}
              onPress={onConfirm}
            />
            <View style={{ height: S.sm }} />
            <Button
              label="Back"
              variant="ghost"
              disabled={busy}
              onPress={() => { setStep(0); setCode(""); setError(""); }}
            />
          </>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}
