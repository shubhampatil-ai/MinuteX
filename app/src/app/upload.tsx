// src/app/upload.tsx — import an audio file (source: UPLOAD).
// Pick from the system document UI (Files / Downloads / Drive / iCloud —
// whatever providers the OS exposes), validate format + size, optionally
// title it, then hand the file to the shared UploadManager: the exact same
// pipeline as phone and device recordings. No upload code lives here.
import { useMemo, useRef, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useRouter, useLocalSearchParams } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, FONT, useTheme, ColorScale } from "../../lib/theme";
import {
  Button, Card, ErrorText, IconCircle, KeyboardAware, TextField,
  scrollFormProps,
} from "../../lib/ui";
import {
  MAX_UPLOAD_BYTES, UPLOAD_FORMATS, startUpload, validateAudioFile,
} from "../../lib/uploads";

type Picked = {
  uri: string;
  name: string;
  size: number | null;
  format: string; // validated extension
};

function fmtBytes(n: number | null): string {
  if (n == null) return "";
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    content: { paddingHorizontal: 26, paddingBottom: 60 },
    title: { ...T.headlineSm, marginTop: 10 },
    sub: { ...T.bodyDim, marginTop: 10 },
    // dashed drop-zone-style picker target
    pickZone: {
      marginTop: S.xl, borderRadius: R.md, borderWidth: 1, borderStyle: "dashed" as const,
      borderColor: C.border,
      alignItems: "center" as const, paddingVertical: S.xxl, gap: S.md,
    },
    pickTitle: { fontFamily: FONT.semibold, fontSize: 16, color: C.text },
    pickSub: { ...T.caption, textAlign: "center" as const, paddingHorizontal: S.xl },
    fileRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md },
    fileName: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    fileMeta: { ...T.caption, marginTop: 2 },
    label: { ...T.label, marginTop: S.xl, marginBottom: S.sm },
  });
}

export default function UploadScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);

  const [file, setFile] = useState<Picked | null>(null);
  const [title, setTitle] = useState("");
  const [error, setError] = useState("");
  // Synchronous re-entrancy guard rather than state: submit() navigates away
  // immediately, so a state flag would never re-render to disable the button
  // — a fast double-tap would start two uploads of the same file.
  const submittedRef = useRef(false);

  const pick = async () => {
    setError("");
    // Loaded lazily: expo-document-picker throws at import time when the
    // installed dev-client binary predates the dependency (native module
    // missing). Lazy-loading keeps the route alive and turns that into a
    // readable message instead of a bundle-wide crash.
    let DocumentPicker: typeof import("expo-document-picker");
    try {
      DocumentPicker = require("expo-document-picker");
    } catch {
      setError(
        "File picking isn't available in this build yet — rebuild the app " +
        "(npx expo run:android / run:ios) to include the document picker."
      );
      return;
    }
    const res = await DocumentPicker.getDocumentAsync({
      // The picker filters by MIME; extension is validated again below
      // (Android providers sometimes report generic types). audio/* covers
      // anything a provider types generically as audio.
      type: ["audio/*", ...new Set(Object.values(UPLOAD_FORMATS))],
      copyToCacheDirectory: true, // local copy -> uploadable file:// URI
      multiple: false,
    });
    if (res.canceled || !res.assets?.length) return;
    const a = res.assets[0];
    try {
      const format = validateAudioFile({ name: a.name, size: a.size ?? null });
      setFile({ uri: a.uri, name: a.name ?? "audio", size: a.size ?? null, format });
    } catch (e: any) {
      setFile(null);
      setError(e?.message ?? "That file can't be used.");
    }
  };

  // Hand off to the shared UploadManager and return to Files — the upload
  // banner there tracks progress, and the recording row appears immediately
  // (the backend writes the timeline stub at presign time).
  const submit = () => {
    if (!file || submittedRef.current) return;
    submittedRef.current = true;
    setError("");
    startUpload({
      source: "UPLOAD",
      fileUri: file.uri,
      format: file.format,
      // Send a title ONLY when the user actually typed one. The picked
      // file's own name used to fill this in when left blank — but phone/OS
      // voice-recorder apps commonly name exports as a raw date/time string
      // (e.g. "04-08-2026 11.35.m4a"), and once that landed as `title` here
      // it looked identical to a real user-typed title to the backend,
      // permanently blocking the AI-generated title from ever being saved
      // (see lambda-transcribe-live's title_source handling). Leaving this
      // blank lets the AI title win, exactly as it does for phone recordings
      // (record-phone.tsx never sends a title either).
      title: title.trim() || undefined,
      size: file.size ?? undefined,
    }).catch(() => { /* surfaced by the Files-screen upload banner */ });
    router.back();
  };

  return (
    <KeyboardAware style={st.container}>
      <Stack.Screen options={{ title: "Upload audio" }} />
      <ScrollView contentContainerStyle={st.content} {...scrollFormProps}>
        <Text style={st.title}>A file you already have</Text>
        <Text style={st.sub}>
          Import a recording from your files. It comes back as the same
          one-page brief as everything else.
        </Text>

        {/* Picker target / picked-file card */}
        {!file ? (
          <Pressable style={({ pressed }) => [st.pickZone, pressed && { opacity: 0.8 }]}
            onPress={pick} accessibilityLabel="Choose an audio file">
            <IconCircle name="folder" size={56} iconSize={24} />
            <Text style={st.pickTitle}>Choose an audio file</Text>
            <Text style={st.pickSub}>
              Any audio format ({Object.keys(UPLOAD_FORMATS).slice(0, 6).join(" · ")} …)
              — up to {MAX_UPLOAD_BYTES / 1e9} GB
            </Text>
          </Pressable>
        ) : (
          <Card style={{ marginTop: S.xl }}>
            <View style={st.fileRow}>
              <IconCircle name="waveform" size={40} iconSize={18} />
              <View style={{ flex: 1 }}>
                <Text style={st.fileName} numberOfLines={1}>{file.name}</Text>
                <Text style={st.fileMeta}>
                  {file.format.toUpperCase()}
                  {file.size != null ? ` · ${fmtBytes(file.size)}` : ""}
                </Text>
              </View>
              <Pressable onPress={pick} accessibilityLabel="Choose a different file"
                style={({ pressed }) => pressed && { opacity: 0.6 }}>
                <Icon name="pencil" tintColor={C.primary} size={18} />
              </Pressable>
            </View>
          </Card>
        )}

        {error ? <ErrorText>{error}</ErrorText> : null}

        {/* Optional title — the AI fills one in later if left blank */}
        <Text style={st.label}>Title (optional)</Text>
        <TextField
          value={title}
          onChangeText={setTitle}
          placeholder="e.g. Interview with Priya"
          maxLength={200}
        />

        <Button
          label="Write it up"
          onPress={submit}
          disabled={!file}
          style={{ marginTop: S.xl }}
        />
      </ScrollView>
    </KeyboardAware>
  );
}
