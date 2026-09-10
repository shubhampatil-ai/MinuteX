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
  ApiContact, ApiError, ApiParticipant, SpeakerIdentityRole, getParticipants,
  setParticipant, tagAttendee,
} from "../../../../lib/api";
import { ContactPicker } from "../../../../lib/contact-picker";
import { speakerName } from "../../../../lib/sources";
import { useMeeting } from "../../../../lib/meeting-context";
import { useWorkspace } from "../../../../lib/workspace-context";
import { avatarColorFor, initialsOf } from "../../../../lib/task-model";

export default function ParticipantsScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const { key } = useLocalSearchParams<{ key: string }>();
  const recordingKey = String(key || "");
  // Mapping a speaker rewrites the meeting's `speaker_names` SERVER-SIDE, and
  // the transcript, the overview summary and every generated document render
  // from that map through the meeting context. This screen writes through the
  // participants API rather than the context, so without telling it, those
  // surfaces keep showing the old name until the provider remounts — which is
  // why the change only appeared after navigating away and back.
  const { syncSpeakerNames, rec } = useMeeting();
  // CRM identity tagging (Phase 2D.3) only makes sense for an ORGANISATION
  // meeting — a personal meeting has no Salesforce Contact/User resolution
  // to feed, and showing the control there would be a control with nothing
  // behind it. `workspaces` carries `is_personal` per entry; a meeting's
  // workspace_id resolves against it rather than against a guessed prefix,
  // so this stays correct even if the id format ever changes server-side.
  const { workspaces } = useWorkspace();
  const isOrgMeeting = !!rec?.workspace_id
    && workspaces.some((w) => w.workspace_id === rec.workspace_id && !w.is_personal);

  const [speakers, setSpeakers] = useState<string[]>([]);
  const [participants, setParticipants] = useState<ApiParticipant[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [busySpeaker, setBusySpeaker] = useState("");
  const [toast, setToast] = useState("");

  // Which speaker the picker is currently choosing for.
  const [pickerFor, setPickerFor] = useState<string | null>(null);
  // The picker is open to record ATTENDANCE (no speaker), not to map a voice.
  const [attendeePicker, setAttendeePicker] = useState(false);
  const [attendeeBusy, setAttendeeBusy] = useState(false);
  const [recordingStatus, setRecordingStatus] = useState("");
  // The meeting's speaker_id -> name map, straight from the participants API.
  // This screen used to render the raw "Speaker 0" for every voice, so a
  // speaker the user had already named on the Transcript tab still read
  // "Not yet identified" here — the one screen whose whole job is identity.
  const [speakerNames, setSpeakerNames] =
    useState<Record<string, string>>({});

  const load = useCallback(
    async (isRefresh = false) => {
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        const res = await getParticipants(recordingKey);
        setSpeakers(res.speakers);
        setParticipants(res.participants);
        setSpeakerNames(res.speaker_names ?? {});
        setRecordingStatus(String(res.recording_status || ""));
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
      // Attendance rows are not speakers and must never occupy a speaker slot
      // in this map — doing so would render one as if it had a voice.
      if (!p.attendance_only) map[p.speaker_id] = p;
    });
    return map;
  }, [participants]);

  /** People recorded as PRESENT without speaking. */
  const attendees = useMemo(
    () => participants.filter((p) => p.attendance_only),
    [participants]
  );

  /** Is the pipeline still working on this recording?
   *
   * This is the difference between "no speakers yet" and "no speakers, ever".
   * An unknown status (an older backend that does not send one) is treated as
   * STILL PROCESSING, because the previous copy said exactly that — so a
   * backend that predates this change keeps the behaviour it already had
   * rather than gaining a new claim it cannot support. */
  const stillProcessing = useMemo(() => {
    if (!recordingStatus) return true;
    return !["complete", "transcribed", "failed"].includes(recordingStatus);
  }, [recordingStatus]);

  /** Record that a contact attended without speaking.
   *
   * Reuses the ordinary participant route. Idempotent server-side (the row is
   * keyed by the contact), so a double tap cannot add the same person twice. */
  const addAttendee = useCallback(
    async (contact: ApiContact) => {
      setAttendeeBusy(true);
      setError("");
      try {
        await tagAttendee(recordingKey, contact.id);
        await load();
        setToast(`${contact.name} marked as a participant`);
        setTimeout(() => setToast(""), 3200);
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Could not save that."
        );
      } finally {
        setAttendeeBusy(false);
      }
    },
    [recordingKey, load]
  );

  /** Remove an attendance record. Clearing needs the row's own key, which the
   *  API returns — the app never constructs it. */
  const removeAttendee = useCallback(
    async (p: ApiParticipant) => {
      setAttendeeBusy(true);
      try {
        await setParticipant(recordingKey, p.speaker_id, null);
        await load();
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Could not remove that."
        );
      } finally {
        setAttendeeBusy(false);
      }
    },
    [recordingKey, load]
  );

  const assign = useCallback(
    async (speakerId: string, contact: ApiContact | null,
          identityRole?: SpeakerIdentityRole) => {
      setBusySpeaker(speakerId);
      try {
        const res = await setParticipant(
          recordingKey,
          speakerId,
          contact ? contact.id : null,
          undefined,
          identityRole
        );
        await load();
        // The write changed speaker_names; pull it into the context so the
        // transcript and summary follow immediately rather than on remount.
        await syncSpeakerNames();
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
    [recordingKey, load, syncSpeakerNames]
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
        // TWO DIFFERENT SITUATIONS, and the old copy told both the same story.
        // "Once it finishes transcribing, each voice will appear here" is a
        // promise, and on a recording that HAS finished it is one the app
        // cannot keep — leaving the user waiting for something that will never
        // arrive. Only the processing case makes that promise now; the
        // finished case says so plainly and offers the way forward.
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
                    {/* The CURRENT display name, resolved through the shared
                        helper the transcript uses, so both screens agree.
                        Falls back to "Speaker 0" when nobody has named this
                        voice — which is also what marks it unidentified. */}
                    <Text style={st.speakerLabel}>
                      {speakerName(sid, speakerNames)}
                    </Text>
                    {contact ? (
                      <Text style={st.mappedTo} numberOfLines={1}>
                        {contact.name}
                        {contact.email ? ` · ${contact.email}` : ""}
                      </Text>
                    ) : speakerName(sid, speakerNames) !== `Speaker ${sid}` ? (
                      // Named, but not yet tied to a Contact. Saying "Not yet
                      // identified" under their own name reads as a
                      // contradiction; what is actually missing is the CRM
                      // link, so say that instead.
                      <Text style={st.unmapped}>Speaker {sid} · not linked to a contact</Text>
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

                {/* CRM identity (Phase 2D.3) — organisation meetings only,
                    and only once a Contact is attached: classifying WHO a
                    voice is comes first, classifying WHAT they are to the
                    CRM comes second. Re-tagging Internal/External never
                    disturbs the contact mapping above (setParticipant keeps
                    contact_id unless the caller changes it). */}
                {isOrgMeeting && !!contact && (
                  <View style={st.identityRow}>
                    <Text style={st.identityLabel}>CRM identity</Text>
                    <View style={st.identityToggle}>
                      <Pressable
                        style={[
                          st.identityOption,
                          mapped?.identity_role === "internal" && {
                            backgroundColor: C.primarySoft,
                          },
                        ]}
                        disabled={busy}
                        onPress={() => assign(sid, contact, "internal")}
                        accessibilityRole="button"
                        accessibilityLabel={`Tag speaker ${sid} as Internal / SM`}
                      >
                        <Text
                          style={[
                            st.identityOptionTxt,
                            mapped?.identity_role === "internal" && {
                              color: C.primary,
                            },
                          ]}
                        >
                          Internal / SM
                        </Text>
                      </Pressable>
                      <Pressable
                        style={[
                          st.identityOption,
                          mapped?.identity_role === "external" && {
                            backgroundColor: C.primarySoft,
                          },
                        ]}
                        disabled={busy}
                        onPress={() => assign(sid, contact, "external")}
                        accessibilityRole="button"
                        accessibilityLabel={`Tag speaker ${sid} as Client / External`}
                      >
                        <Text
                          style={[
                            st.identityOptionTxt,
                            mapped?.identity_role === "external" && {
                              color: C.primary,
                            },
                          ]}
                        >
                          Client / External
                        </Text>
                      </Pressable>
                    </View>
                  </View>
                )}
              </View>
            );
          })}
        </>
      )}

      {/* ATTENDANCE — people who were there but are not voices in the
          transcript. Rendered in BOTH cases (speakers or none), because
          someone can attend a well-diarized meeting without saying a word.
          Kept visually separate from the speaker cards so the distinction
          between "spoke" and "was present" stays legible. */}
      {attendees.length > 0 && (
        <>
          <SectionTitle>Also attended ({attendees.length})</SectionTitle>
          {attendees.map((p) => (
            <View key={p.speaker_id} style={st.attendeeRow}>
              <View
                style={[
                  st.attendeeAvatar,
                  {
                    backgroundColor: avatarColorFor(p.contact?.name || "?"),
                  },
                ]}
              >
                <Text style={st.attendeeInitials}>
                  {initialsOf(p.contact?.name || "?")}
                </Text>
              </View>
              <View style={{ flex: 1 }}>
                <Text style={st.attendeeName}>
                  {p.contact?.name || "Unknown"}
                </Text>
                <Text style={st.attendeeSub}>Attended — did not speak</Text>
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

      {/* The same action when speakers DO exist — attending without speaking
          is not only a no-diarization case. */}
      {speakers.length > 0 && (
        <Button
          label="I was in this meeting"
          variant="ghost"
          onPress={() => setAttendeePicker(true)}
          loading={attendeeBusy}
          style={{ marginTop: S.md }}
        />
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
        // You are almost always IN your own meeting, and "Speaker 0" is very
        // often you. Without this the only way to say so was to type your own
        // name and email in by hand — and a typo in that email silently broke
        // the link to your account, which is what every task notification
        // depends on.
        allowSelf
        title={pickerFor
          ? `Who is ${speakerName(pickerFor, speakerNames)}?`
          : "Select Contact"}
      />

      {/* A SECOND picker instance, for attendance rather than speaker mapping.
          Separate from the one above because the two answer different
          questions — "who is this voice?" versus "who else was here?" — and
          sharing one would mean tracking which mode it is in. `allowSelf` is
          the point: the common case is the user themselves, and it reuses
          their existing contact rather than making them type an email. */}
      <ContactPicker
        visible={attendeePicker}
        onClose={() => setAttendeePicker(false)}
        onPick={addAttendee}
        allowSelf
        title="Who was in this meeting?"
      />

      <Toast visible={!!toast} label={toast} />
    </ScrollView>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    noSpeakersTitle: {
      fontFamily: FONT.bold, fontSize: 15, color: C.text, marginBottom: 6,
    },
    noSpeakersBody: {
      fontFamily: FONT.regular, fontSize: 13, color: C.textFaint,
      lineHeight: 19, marginBottom: S.md,
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
    attendeeInitials: {
      fontFamily: FONT.bold, fontSize: 12, color: "#FFFFFF",
    },
    attendeeName: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    attendeeSub: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 2,
    },
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
    identityRow: {
      marginTop: 12, paddingTop: 12, borderTopWidth: 1, borderTopColor: C.border,
    },
    identityLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1,
      color: C.textFaint, marginBottom: 8,
    },
    identityToggle: {
      flexDirection: "row" as const, gap: S.sm,
    },
    identityOption: {
      flex: 1, paddingVertical: 8, borderRadius: R.pill,
      borderWidth: 1, borderColor: C.border, alignItems: "center" as const,
    },
    identityOptionTxt: { fontFamily: FONT.bold, fontSize: 12, color: C.textDim },
    hintBox: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.sm, padding: S.md, borderRadius: R.md,
      backgroundColor: C.surface2, marginTop: S.md,
    },
    hintTxt: { ...T.caption, flex: 1 },
  });
}
