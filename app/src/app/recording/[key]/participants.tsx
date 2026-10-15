// src/app/recording/[key]/participants.tsx — map the voices in this meeting to
// real people.
//
// Diarization separates VOICES, not identities: the transcript contains "0",
// "1", "2" because the audio carries no names. This screen is where a human
// supplies the missing half.
//
// What it does NOT do is rewrite the transcript. The labels stay "0"/"1"
// forever — mapping writes a participant row beside the transcript and syncs
// the recording's speaker_names map, which is what the transcript view,
// documents and highlights already render from. That keeps the transcript the
// verbatim record and lets any mapping be corrected later without the original
// having been altered.
//
// The payoff worth surfacing: mapping a speaker can RESOLVE tasks. The AI
// records which speaker owed a task without guessing who they are, so naming
// speaker 0 is what finally lets "Send proposal" belong to Rahul Sharma — and
// to reach him, if he has a MinuteX account. We report that count after a
// mapping rather than leaving it invisible.
//
// MEETING ACCESS. A tagged person with a MinuteX account gets READ-ONLY
// access to this meeting — Notes by default — and the recorder picks per
// person: No access / Notes / Full (notes + transcript + audio). It is a
// per-USER setting, so someone who is a speaker and an attendee shows one
// value on both rows. None of it changes the recorder's own permissions, and
// "Full" is never a write permission: the backend enforces both.
//
// ONE TAGGING UI. The speaker cards, the contact picker, the name prompt and
// the access sheet all come from lib/speaker-tagging.tsx — the same pieces the
// meeting's People tab and the Transcript open — so identifying a speaker
// looks and behaves the same wherever it starts.
import { useCallback, useMemo, useState } from "react";
import {
  Pressable, RefreshControl, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { Icon } from "../../../../lib/icons";
import { S, R, ELEV, FONT, useTheme, ColorScale } from "../../../../lib/theme";
import {
  Button, EmptyState, ErrorText, Loading, SectionTitle, Toast,
} from "../../../../lib/ui";
import { ContactPicker } from "../../../../lib/contact-picker";
import { avatarColorFor, initialsOf } from "../../../../lib/task-model";
import {
  ParticipantAccess, SpeakerAccessSheet, SpeakerContactPicker, SpeakerNameSheet,
  SpeakerTagCard, useSpeakerTagging,
} from "../../../../lib/speaker-tagging";

export default function ParticipantsScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const { key } = useLocalSearchParams<{ key: string }>();
  const recordingKey = String(key || "");
  const tagging = useSpeakerTagging(recordingKey);
  const {
    speakers, attendees, loading, refreshing, error, stillProcessing,
    attendeeBusy, toast, load, addAttendee, removeAttendee, workspaceId,
  } = tagging;

  // Which speaker the contact picker / name prompt is open for.
  const [pickerFor, setPickerFor] = useState<string | null>(null);
  const [nameFor, setNameFor] = useState<string | null>(null);
  const [accessTarget, setAccessTarget] =
    useState<{ userId: string; name: string } | null>(null);
  // The picker is open to record ATTENDANCE (no speaker), not to map a voice.
  const [attendeePicker, setAttendeePicker] = useState(false);

  useFocusEffect(useCallback(() => { load(); }, [load]));

  const openAccess = useCallback(
    (userId: string, name: string) => setAccessTarget({ userId, name }), []);

  if (loading) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title: "Participants" }} />
        <Loading label="Loading participants" />
      </View>
    );
  }

  return (
    <ScrollView
      style={st.container}
      contentContainerStyle={{ paddingBottom: S.xxl * 2 }}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={() => load(true)} tintColor={C.primary} />
      }
      showsVerticalScrollIndicator={false}
    >
      <Stack.Screen options={{ title: "Participants" }} />

      <Text style={st.intro}>
        Match each voice to a person. The transcript keeps its original speaker
        labels — this only changes how they are shown, and lets tasks find the
        right owner.
      </Text>

      {!!error && (
        <View style={{ gap: S.sm, marginBottom: S.md }}>
          <ErrorText>{error}</ErrorText>
          <Button label="Retry" variant="secondary" onPress={() => load()} />
        </View>
      )}

      {speakers.length === 0 ? (
        // TWO DIFFERENT SITUATIONS: still processing (voices may yet appear)
        // versus finished with none found (they will not). Only the first
        // makes the "once it finishes" promise.
        stillProcessing ? (
          <EmptyState
            icon="person.fill"
            title="No speakers detected yet"
            subtitle="This meeting is still being processed. Once it finishes transcribing, each voice will appear here."
          />
        ) : (
          <View style={st.card}>
            <Text style={st.noSpeakersTitle}>
              No speakers were identified for this recording.
            </Text>
            <Text style={st.noSpeakersBody}>
              You can still record who was there. This marks attendance only —
              it does not attribute anything said in the meeting.
            </Text>
            <Button
              label="I was in this meeting"
              variant="secondary"
              onPress={() => setAttendeePicker(true)}
              loading={attendeeBusy}
            />
          </View>
        )
      ) : (
        <>
          <SectionTitle>Speakers ({speakers.length})</SectionTitle>
          {speakers.map((sid) => (
            <SpeakerTagCard
              key={sid}
              tagging={tagging}
              speakerId={sid}
              onSelectContact={setPickerFor}
              onSetName={setNameFor}
              onOpenAccess={openAccess}
            />
          ))}
        </>
      )}

      {/* ATTENDANCE — people who were there but are not voices in the
          transcript. Kept visually separate from the speaker cards so
          "spoke" and "was present" stay distinct. */}
      {attendees.length > 0 && (
        <>
          <SectionTitle>Also attended ({attendees.length})</SectionTitle>
          {attendees.map((p) => (
            <View key={p.speaker_id} style={st.attendeeRow}>
              <View style={[st.attendeeAvatar, { backgroundColor: avatarColorFor(p.contact?.name || "?") }]}>
                <Text style={st.attendeeInitials}>{initialsOf(p.contact?.name || "?")}</Text>
              </View>
              <View style={{ flex: 1, minWidth: 0 }}>
                <Text style={st.attendeeName} numberOfLines={1}>{p.contact?.name || "Unknown"}</Text>
                <Text style={st.attendeeSub}>Attended — did not speak</Text>
                {p.access !== undefined && (
                  <View style={{ marginTop: 6, alignItems: "flex-start" }}>
                    <ParticipantAccess tagging={tagging} participant={p} onOpenAccess={openAccess} />
                  </View>
                )}
              </View>
              <Pressable
                onPress={() => removeAttendee(p)}
                disabled={attendeeBusy}
                hitSlop={8}
                accessibilityRole="button"
                accessibilityLabel={`Remove ${p.contact?.name || "attendee"}`}
              >
                <Icon name="xmark" size={13} tintColor={C.textDim} />
              </Pressable>
            </View>
          ))}
        </>
      )}

      {/* Attending without speaking is not only a no-diarization case. */}
      {speakers.length > 0 && (
        <Button
          label="I was in this meeting"
          variant="ghost"
          onPress={() => setAttendeePicker(true)}
          loading={attendeeBusy}
          style={{ marginTop: S.md }}
        />
      )}

      <SpeakerContactPicker tagging={tagging} speakerId={pickerFor} onClose={() => setPickerFor(null)} />
      <SpeakerNameSheet tagging={tagging} speakerId={nameFor} onClose={() => setNameFor(null)} />
      <SpeakerAccessSheet tagging={tagging} target={accessTarget} onClose={() => setAccessTarget(null)} />

      {/* A SECOND picker, for attendance rather than speaker mapping — the
          two answer different questions ("who is this voice?" versus "who
          else was here?"). */}
      <ContactPicker
        visible={attendeePicker}
        onClose={() => setAttendeePicker(false)}
        onPick={addAttendee}
        allowSelf
        salesforceWorkspaceId={workspaceId}
        meetingKey={recordingKey}
        title="Who was in this meeting?"
      />

      <Toast visible={!!toast} label={toast} />
    </ScrollView>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    noSpeakersTitle: { fontFamily: FONT.bold, fontSize: 15, color: C.text, marginBottom: 6 },
    noSpeakersBody: {
      fontFamily: FONT.regular, fontSize: 13, color: C.textFaint, lineHeight: 19, marginBottom: S.md,
    },
    attendeeRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      backgroundColor: C.surface, borderRadius: R.card, padding: S.md,
      marginBottom: S.sm, shadowColor: C.shadow, ...ELEV.sm,
    },
    attendeeAvatar: {
      width: 34, height: 34, borderRadius: 17,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    attendeeInitials: { fontFamily: FONT.bold, fontSize: 12, color: "#FFFFFF" },
    attendeeName: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text, flexShrink: 1 },
    attendeeSub: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 2 },
    intro: { ...T.caption, marginTop: 2, marginBottom: S.lg },
    card: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      marginBottom: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
  });
}
