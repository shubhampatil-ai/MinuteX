// src/app/claim.tsx — MinuteX: link a device to the logged-in account.
// The user enters their device API key (dk_live_...); we POST /devices/claim,
// which verifies it against DeviceKeys and links that deviceId to the account.
// After a successful claim its recordings show up on the files list.
import React, { useMemo, useState } from "react";
import {
  KeyboardAvoidingView, Pressable, ScrollView, StyleSheet, Text, TextInput, View,
} from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, FONT, useTheme, ColorScale } from "../../lib/theme";
import { Button, ErrorText, SuccessText, IconCircle, Card } from "../../lib/ui";
import { claimDevice, clearToken, ApiError } from "../../lib/api";

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    scroll: { flexGrow: 1, justifyContent: "center" as const, paddingHorizontal: S.xl, paddingVertical: 60 },
    h1: { ...T.h2, textAlign: "center" as const, marginTop: S.lg },
    sub: { ...T.bodyDim, lineHeight: 22, marginTop: S.sm, marginBottom: S.xl, textAlign: "center" as const },
    fieldWrap: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
      backgroundColor: C.surface, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      paddingHorizontal: S.md, minHeight: 52,
    },
    fieldInput: { flex: 1, color: C.text, fontSize: 16, paddingVertical: 14 },
    hint: { ...T.caption, lineHeight: 18, marginTop: S.md },
    link: { color: C.primary, fontSize: 15, fontFamily: FONT.semibold, textAlign: "center" as const },
  });
}

export default function ClaimScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  // Optional ?apiKey= prefill (e.g. handed over from the BLE pairing flow).
  const params = useLocalSearchParams<{ apiKey?: string }>();
  const [apiKey, setApiKey] = useState(params.apiKey ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState<string>("");

  const submit = async () => {
    setError(""); setOk("");
    const key = apiKey.trim();
    if (!key) { setError("Enter your device key."); return; }
    setBusy(true);
    try {
      const res = await claimDevice(key);
      setOk(res.device_id);
      // Brief confirmation, then go to the files list.
      setTimeout(() => router.replace("/"), 900);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        await clearToken(); router.replace("/login"); return;
      }
      if (e instanceof ApiError && e.status === 403) {
        setError("That device key is not valid. Check it and try again.");
      } else {
        setError(e instanceof ApiError ? e.message : "Could not link the device.");
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
      <ScrollView contentContainerStyle={st.scroll} keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}>
        <View style={{ alignItems: "center" }}>
          <IconCircle name="link" size={72} iconSize={30} />
        </View>
        <Text style={st.h1}>Link your recorder</Text>
        <Text style={st.sub}>
          Enter the device key from your recorder to see its meetings here.
        </Text>

        <Card style={{ padding: S.md }}>
          <View style={[st.fieldWrap, { borderWidth: 0, backgroundColor: "transparent" }]}>
            <Icon name="key" tintColor={C.textFaint} size={18} />
            <TextInput
              style={st.fieldInput}
              placeholder="dk_live_…"
              placeholderTextColor={C.textFaint}
              autoCapitalize="none"
              autoCorrect={false}
              value={apiKey}
              onChangeText={setApiKey}
            />
          </View>
        </Card>
        <Text style={st.hint}>
          You'll find the key in the device's setup card — it starts with "dk_live_".
        </Text>

        {error ? <ErrorText>{error}</ErrorText> : null}
        {ok ? <SuccessText>✓ Linked {ok}. Taking you to your files…</SuccessText> : null}

        <Button label="Link device" onPress={submit} loading={busy} style={{ marginTop: S.xl }} />

        <Pressable onPress={() => router.replace("/")} disabled={busy} style={{ marginTop: S.lg }}>
          <Text style={st.link}>Skip — go to my files</Text>
        </Pressable>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}
