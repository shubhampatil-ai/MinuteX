// src/app/recording/[key]/transcript.tsx — the transcript, full screen.
//
// The transcript used to be one of two tabs on Meeting Detail. It is now its
// own screen, reached from a "View Transcript" button under the player, and the
// four tabs there are all ANALYSIS (Overview / Speakers / Tasks / Documents).
// The reasoning: the transcript is the longest thing in the product and the one
// thing a user reads top to bottom, so sharing vertical space with a masthead,
// a player and a tab bar cost it the most. It is also the only surface with no
// AI on it at all, which makes it a poor fit beside three that are entirely AI.
//
// The reading experience itself is UNCHANGED and deliberately not
// reimplemented here: search, speaker colours, timestamps and tap-to-seek all
// come from lib/transcript-view.tsx, which the detail screen also uses for its
// speaker colours. One implementation, so a timestamp shown on one surface
// cannot seek somewhere else on another.
//
// MeetingProvider is mounted by the parent _layout.tsx, so this screen shares
// the same recording and the same speaker map as Meeting Detail — a rename made
// here is already applied when the user navigates back.
import { useMemo, useState } from "react";
import {
  ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Text,
  TextInput, View,
} from "react-native";
import { Stack } from "expo-router";
import { S, R, FONT, useTheme, ColorScale } from "../../../../lib/theme";
import { Button, KeyboardAwareSheet } from "../../../../lib/ui";
import { AudioPlayerProvider } from "../../../../lib/audio-player";
import { useMeeting } from "../../../../lib/meeting-context";
import { speakerName } from "../../../../lib/sources";
import {
  TranscriptView, buildSpeakerBlocks, buildSpeakerColors,
} from "../../../../lib/transcript-view";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    center: {
      alignItems: "center" as const, justifyContent: "center" as const,
      gap: S.md, flex: 1,
    },
    empty: {
      fontFamily: FONT.medium, fontSize: 14.5, lineHeight: 21, color: C.textDim,
      textAlign: "center" as const,
    },
    modalWrap: {
      flex: 1, backgroundColor: "rgba(0,0,0,0.45)",
      justifyContent: "center" as const, padding: 24,
    },
    modalCard: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      borderWidth: 1, borderColor: C.border,
    },
    modalTitle: {
      fontFamily: FONT.bold, fontSize: 16, color: C.text, marginBottom: S.sm,
    },
    input: {
      fontFamily: FONT.medium, fontSize: 15, color: C.text,
      backgroundColor: C.bg, borderRadius: R.md, borderWidth: 1,
      borderColor: C.border, paddingHorizontal: S.md, paddingVertical: 11,
      marginBottom: S.md,
    },
  });
}

export default function TranscriptScreen() {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { rec, loading, error, reload, renameSpeaker } = useMeeting();

  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  const speakerNames = rec?.speaker_names ?? undefined;
  const speakerBlocks = useMemo(
    () => buildSpeakerBlocks(rec?.timestamps), [rec?.timestamps]);
  const speakerColors = useMemo(
    () => buildSpeakerColors(rec?.timestamps, C.speakers),
    [rec?.timestamps, C.speakers]);
  const resolveName = (label: string) => speakerName(label, speakerNames);

  const openRename = (raw: string) => {
    setDraft(speakerNames?.[raw] ?? "");
    setEditing(raw);
  };

  const save = async () => {
    if (!editing) return;
    setSaving(true);
    try {
      await renameSpeaker(editing, draft);
      setEditing(null);
    } catch {
      // renameSpeaker already alerted
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <View style={[st.container, st.center]}>
        <Stack.Screen options={{ title: "Transcript" }} />
        <ActivityIndicator color={C.primary} />
      </View>
    );
  }

  if (error || !rec) {
    return (
      <View style={[st.container, st.center]}>
        <Stack.Screen options={{ title: "Transcript" }} />
        <Text style={st.empty}>{error || "Meeting unavailable."}</Text>
        <Button label="Try again" variant="secondary" onPress={reload}
                style={{ alignSelf: "stretch" }} />
      </View>
    );
  }

  if (!rec.transcript) {
    return (
      <View style={[st.container, st.center]}>
        <Stack.Screen options={{ title: "Transcript" }} />
        <Text style={st.empty}>
          There&apos;s no transcript for this meeting yet.
        </Text>
      </View>
    );
  }

  return (
    // The player is mounted here too, because tap-to-seek is the transcript's
    // most-used affordance and it needs a player to seek. Same provider
    // component as Meeting Detail; playback state is per-screen.
    <AudioPlayerProvider url={rec.audio_url}>
      <Stack.Screen options={{ title: "Transcript" }} />
      <ScrollView
        style={st.container}
        contentContainerStyle={{ paddingBottom: 40 }}
        showsVerticalScrollIndicator={false}
        keyboardDismissMode="on-drag"
        keyboardShouldPersistTaps="handled"
      >
        <TranscriptView
          transcript={rec.transcript}
          speakerBlocks={speakerBlocks}
          speakerColors={speakerColors}
          resolveName={resolveName}
          onRenameSpeaker={openRename}
          search={search}
          onSearchChange={setSearch}
        />
      </ScrollView>

      {/* Rename a speaker — the same mapping Meeting Detail's Speakers tab
          edits, so one rename updates every surface. */}
      <Modal visible={editing !== null} transparent animationType="fade"
             onRequestClose={() => setEditing(null)}>
        {/* Inside the Modal and around the backdrop — a Modal on Android is
            its own window and needs "height" behaviour, which is exactly what
            KeyboardAwareSheet encapsulates (see lib/ui.tsx). */}
        <KeyboardAwareSheet>
        <Pressable style={st.modalWrap} onPress={() => setEditing(null)}>
          <Pressable style={st.modalCard} onPress={(e) => e.stopPropagation()}>
            <Text style={st.modalTitle}>
              {editing != null ? speakerName(editing, speakerNames) : ""}
            </Text>
            <TextInput
              style={st.input}
              value={draft}
              onChangeText={setDraft}
              placeholder="Name this speaker"
              placeholderTextColor={C.textFaint}
              autoFocus
              editable={!saving}
              returnKeyType="done"
              onSubmitEditing={save}
            />
            <View style={{ flexDirection: "row", gap: S.sm }}>
              <Button label="Cancel" variant="secondary" style={{ flex: 1 }}
                      onPress={() => setEditing(null)} disabled={saving} />
              <Button label="Save" style={{ flex: 1 }} onPress={save}
                      loading={saving} />
            </View>
          </Pressable>
        </Pressable>
        </KeyboardAwareSheet>
      </Modal>
    </AudioPlayerProvider>
  );
}
