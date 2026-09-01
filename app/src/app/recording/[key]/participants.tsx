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
import { useCallback, useMemo, useState } from "react";
import {
  Pressable, RefreshControl, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { Icon } from "../../../../lib/icons";
import {
  S, R, ELEV, CAPS, FONT, useTheme, ColorScale,
} from "../../../../lib/theme";
import {
  Button, EmptyState, ErrorText, Loading, SectionTitle, Toast,
} from "../../../../lib/ui";
import {
  ApiContact, ApiError, ApiParticipant, getParticipants, setParticipant,
} from "../../../../lib/api";
import { ContactPicker } from "../../../../lib/contact-picker";
import { avatarColorFor, initialsOf } from "../../../../lib/task-model";

export default function ParticipantsScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const { key } = useLocalSearchParams<{ key: string }>();
  const recordingKey = String(key || "");

  const [speakers, setSpeakers] = useState<string[]>([]);
  const [participants, setParticipants] = useState<ApiParticipant[]>([]);
  const [folderContacts, setFolderContacts] = useState<ApiContact[]>([]);
  const [folderId, setFolderId] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [busySpeaker, setBusySpeaker] = useState("");
  const [toast, setToast] = useState("");

  // Which speaker the picker is currently choosing for.
  const [pickerFor, setPickerFor] = useState<string | null>(null);

  const load = useCallback(
    async (isRefresh = false) => {
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        const res = await getParticipants(recordingKey);
        setSpeakers(res.speakers);
        setParticipants(res.participants);
        setFolderContacts(res.folder_contacts);
        setFolderId(res.folder_id);
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Could not load participants."
        );
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [recordingKey]
  );

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  const bySpeaker = useMemo(() => {
    const map: Record<string, ApiParticipant> = {};
    participants.forEach((p) => {
      map[p.speaker_id] = p;
    });
    return map;
  }, [participants]);

  const assign = useCallback(
    async (speakerId: string, contact: ApiContact | null) => {
      setBusySpeaker(speakerId);
      try {
        const res = await setParticipant(
          recordingKey,
          speakerId,
          contact ? contact.id : null
        );
        await load();
        // One mapping can resolve several tasks — saying so is the difference
        // between this feeling like bookkeeping and feeling like progress.
        const n = res.tasks_resolved ?? 0;
        if (n > 0) {
          setToast(
            `${n} task${n === 1 ? "" : "s"} now assigned to ${contact?.name}`
          );
          setTimeout(() => setToast(""), 3200);
        }
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Could not save that mapping."
        );
      } finally {
        setBusySpeaker("");
      }
    },
    [recordingKey, load]
  );

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
        <RefreshControl
          refreshing={refreshing}
          onRefresh={() => load(true)}
          tintColor={C.primary}
        />
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
        <EmptyState
          icon="person.fill"
          title="No speakers detected"
          subtitle="This meeting has no separated speakers yet. Once it finishes transcribing, each voice will appear here."
        />
      ) : (
        <>
          <SectionTitle>Speakers ({speakers.length})</SectionTitle>
          {speakers.map((sid) => {
            const mapped = bySpeaker[sid];
            const contact = mapped?.contact;
            const busy = busySpeaker === sid;
            return (
              <View key={sid} style={st.card}>
                <View style={st.cardTop}>
                  <View style={st.speakerBadge}>
                    <Text style={st.speakerBadgeTxt}>{sid}</Text>
                  </View>
                  <View style={{ flex: 1 }}>
                    <Text style={st.speakerLabel}>Speaker {sid}</Text>
                    {contact ? (
                      <Text style={st.mappedTo} numberOfLines={1}>
                        {contact.name}
                        {contact.email ? ` · ${contact.email}` : ""}
                      </Text>
                    ) : (
                      <Text style={st.unmapped}>Not yet identified</Text>
                    )}
                  </View>
                  {contact && (
                    <View
                      style={[
                        st.avatar,
                        { backgroundColor: avatarColorFor(contact.name) },
                      ]}
                    >
                      <Text style={st.avatarTxt}>
                        {initialsOf(contact.name)}
                      </Text>
                    </View>
                  )}
                </View>

                <View style={st.cardActions}>
                  <Pressable
                    style={[st.action, { backgroundColor: C.primarySoft }]}
                    onPress={() => setPickerFor(sid)}
                    disabled={busy}
                    accessibilityRole="button"
                    accessibilityLabel={
                      contact
                        ? `Change contact for speaker ${sid}`
                        : `Select contact for speaker ${sid}`
                    }
                  >
                    <Icon
                      name={contact ? "pencil" : "plus"}
                      size={13}
                      tintColor={C.primary}
                    />
                    <Text style={[st.actionTxt, { color: C.primary }]}>
                      {busy
                        ? "Saving…"
                        : contact
                          ? "Change Contact"
                          : "Select Contact"}
                    </Text>
                  </Pressable>
                  {!!contact && (
                    <Pressable
                      style={st.action}
                      onPress={() => assign(sid, null)}
                      disabled={busy}
                      accessibilityRole="button"
                      accessibilityLabel={`Clear mapping for speaker ${sid}`}
                    >
                      <Icon name="xmark" size={13} tintColor={C.textDim} />
                      <Text style={[st.actionTxt, { color: C.textDim }]}>
                        Clear
                      </Text>
                    </Pressable>
                  )}
                </View>
              </View>
            );
          })}
        </>
      )}

      {!folderId && (
        <View style={st.hintBox}>
          <Icon name="folder" size={14} tintColor={C.textFaint} />
          <Text style={st.hintTxt}>
            This meeting is not in a folder. Filing it lets that folder&apos;s
            contacts be offered first here.
          </Text>
        </View>
      )}

      <ContactPicker
        visible={pickerFor !== null}
        onClose={() => setPickerFor(null)}
        onPick={(c) => {
          if (pickerFor) assign(pickerFor, c);
        }}
        onClear={
          pickerFor && bySpeaker[pickerFor]?.contact_id
            ? () => {
                if (pickerFor) assign(pickerFor, null);
              }
            : undefined
        }
        folderContacts={folderContacts}
        folderId={folderId}
        // You are almost always IN your own meeting, and "Speaker 0" is very
        // often you. Without this the only way to say so was to type your own
        // name and email in by hand — and a typo in that email silently broke
        // the link to your account, which is what every task notification
        // depends on.
        allowSelf
        title={pickerFor ? `Who is Speaker ${pickerFor}?` : "Select Contact"}
      />

      <Toast visible={!!toast} label={toast} />
    </ScrollView>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    intro: { ...T.caption, marginTop: 2, marginBottom: S.lg },
    card: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      marginBottom: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    cardTop: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
    },
    speakerBadge: {
      width: 34, height: 34, borderRadius: R.md,
      backgroundColor: C.surface2, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    speakerBadgeTxt: { fontFamily: FONT.bold, fontSize: 14, color: C.textDim },
    speakerLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.3,
      color: C.textFaint,
    },
    mappedTo: {
      fontFamily: FONT.bold, fontSize: 14.5, color: C.text, marginTop: 3,
    },
    unmapped: {
      fontFamily: FONT.medium, fontSize: 13.5, color: C.textFaint, marginTop: 3,
    },
    avatar: {
      width: 36, height: 36, borderRadius: 18, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 13, color: "#fff" },
    cardActions: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
      marginTop: 14, paddingTop: 12, borderTopWidth: 1,
      borderTopColor: C.border,
    },
    action: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      paddingVertical: 7, paddingHorizontal: 12, borderRadius: R.pill,
    },
    actionTxt: { fontFamily: FONT.bold, fontSize: 12.5 },
    hintBox: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.sm, padding: S.md, borderRadius: R.md,
      backgroundColor: C.surface2, marginTop: S.md,
    },
    hintTxt: { ...T.caption, flex: 1 },
  });
}
