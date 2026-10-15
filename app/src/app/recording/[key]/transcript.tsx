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
//
// Tapping a speaker opens the SAME tagging sheet as the People tab and the
// Participants screen (lib/speaker-tagging.tsx): tag a contact, "That's me",
// meeting access, or just a name. It used to offer a name only.
import { useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Stack, useLocalSearchParams } from "expo-router";
import { S, FONT, useTheme, ColorScale } from "../../../../lib/theme";
import { Button, Toast } from "../../../../lib/ui";
import { AudioPlayerProvider } from "../../../../lib/audio-player";
import { useMeeting } from "../../../../lib/meeting-context";
import { speakerName } from "../../../../lib/sources";
import {
  TranscriptView, buildSpeakerBlocks, buildSpeakerColors,
} from "../../../../lib/transcript-view";
import { SpeakerTagSheet, useSpeakerTagging } from "../../../../lib/speaker-tagging";

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
  });
}

export default function TranscriptScreen() {
  const { C } = useTheme();
  // Segment ids arrive as a comma-joined param from a task's "View evidence".
  // A param rather than context because the transcript is reached by a normal
  // route push and must keep working when opened with no evidence at all.
  const { evidence } = useLocalSearchParams<{ evidence?: string }>();
  const evidenceIds = useMemo(
    () => String(evidence ?? "").split(",").map((x) => x.trim()).filter(Boolean),
    [evidence]);
  const scrollRef = useRef<ScrollView>(null);
  // Guards against re-scrolling on every layout pass — the user must be able
  // to scroll away and stay there.
  const scrolled = useRef(false);
  const st = useMemo(() => buildStyles(C), [C]);
  const { key, rec, loading, error, reload } = useMeeting();

  const [search, setSearch] = useState("");
  // The speaker whose tagging sheet is open.
  const [editing, setEditing] = useState<string | null>(null);
  const tagging = useSpeakerTagging(String(key));

  const speakerNames = rec?.speaker_names ?? undefined;
  const speakerBlocks = useMemo(
    () => buildSpeakerBlocks(rec?.timestamps), [rec?.timestamps]);
  const speakerColors = useMemo(
    () => buildSpeakerColors(rec?.timestamps, C.speakers),
    [rec?.timestamps, C.speakers]);
  const resolveName = (label: string) => speakerName(label, speakerNames);

  const openRename = (raw: string) => setEditing(raw);

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
          {rec.transcript_withheld
            ? "The transcript isn't saved on this phone. Connect to the internet to read it."
            : "There’s no transcript for this meeting yet."}
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
        ref={scrollRef}
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
          highlightIds={evidenceIds}
          onHighlightLayout={(y) => {
            if (scrolled.current) return;
            scrolled.current = true;
            // A little above the block, so the reader sees the line BEFORE it
            // too — evidence read without its lead-in is easy to misjudge.
            scrollRef.current?.scrollTo({ y: Math.max(0, y - 80), animated: true });
          }}
        />
      </ScrollView>

      {/* Who is this speaker — the same sheet the People tab opens, so one
          mapping updates every surface. */}
      <SpeakerTagSheet
        tagging={tagging}
        speakerId={editing}
        onClose={() => setEditing(null)}
      />
      <Toast visible={!!tagging.toast} label={tagging.toast} />
    </AudioPlayerProvider>
  );
}
