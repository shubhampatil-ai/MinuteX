// lib/speaker-tagging.tsx — ONE way to say who a speaker is, everywhere.
//
// A speaker can be identified from three places: the meeting's People tab,
// a tap on a speaker in the Transcript, and the Participants screen. They used
// to be three different UIs — a "Who is this?" sheet, a bare name prompt with
// no contact tagging at all, and the Participants cards — so what a user could
// do depended on where they happened to tap. Only the Participants cards
// offered "That's me" and meeting access.
//
// Everything below is shared by all three:
//
//   useSpeakerTagging  the state and writes (participants API + meeting
//                      context), so every surface saves the same way and the
//                      transcript, summary and documents follow immediately;
//   SpeakerTagCard     the Participants card — contact, name and meeting
//                      access — rendered identically on every surface;
//   SpeakerTagSheet    that card in a sheet, for the People tab and Transcript;
//   SpeakerAccessSheet / SpeakerNameSheet / SpeakerContactPicker
//                      the three follow-up choices the card opens.
//
// The transcript itself is never rewritten: mapping writes a participant row
// and syncs speaker_names, exactly as before (see participants.tsx).
//
// MEETING ACCESS is per USER and read-only at every level (see the backend's
// PARTICIPANT MEETING ACCESS block): the recorder's own permissions never
// change, and "Full" is never a write permission.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View,
} from "react-native";
import { Icon } from "./icons";
import { S, R, ELEV, CAPS, FONT, useTheme, ColorScale } from "./theme";
import { Button, KeyboardAwareSheet } from "./ui";
import { BottomSheet, SheetAction } from "./kit";
import {
  ApiContact, ApiError, ApiParticipant, MeetingAccessLevel,
  getParticipants, setMeetingAccess, setParticipant, tagAttendee,
} from "./api";
import { ContactPicker } from "./contact-picker";
import { speakerName } from "./sources";
import { useMeeting } from "./meeting-context";
import { avatarColorFor, initialsOf } from "./task-model";
// Tagging a synced Salesforce Contact relates the meeting to it: tell the
// meeting's Salesforce card to reload.
import { invalidateSalesforce } from "./salesforce-events";

// ===========================================================================
// Access vocabulary.
// ===========================================================================
export const ACCESS_LABEL: Record<MeetingAccessLevel, string> = {
  none: "No access",
  notes: "Notes",
  full: "Full access",
};

const ACCESS_OPTIONS: {
  level: MeetingAccessLevel;
  label: string;
  description: string;
  icon: "eye.slash" | "doc.text.fill" | "waveform";
}[] = [
  { level: "notes", label: "Notes",
    description: "Read-only notes: overview, highlights, decisions and tasks.",
    icon: "doc.text.fill" },
  { level: "full", label: "Full access",
    description: "Read-only notes, transcript and recording. They can’t edit anything.",
    icon: "waveform" },
  { level: "none", label: "No access",
    description: "They stay tagged, but can’t open this meeting.",
    icon: "eye.slash" },
];

// ===========================================================================
// State + writes.
// ===========================================================================
export type SpeakerTagging = ReturnType<typeof useSpeakerTagging>;

export function useSpeakerTagging(recordingKey: string) {
  // speaker_names lives on the meeting in context, which every surface reads.
  // Writes go through the participants API and are then pulled back into the
  // context, so the transcript, summary and documents follow at once rather
  // than on the next remount.
  const { rec, syncSpeakerNames, renameSpeaker } = useMeeting();

  const [speakers, setSpeakers] = useState<string[]>([]);
  const [participants, setParticipants] = useState<ApiParticipant[]>([]);
  const [apiNames, setApiNames] = useState<Record<string, string>>({});
  const [recordingStatus, setRecordingStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [busySpeaker, setBusySpeaker] = useState("");
  const [attendeeBusy, setAttendeeBusy] = useState(false);
  // Optimistic access changes, keyed by USER (one person, one state), cleared
  // whenever a fresh load makes the server's value authoritative again.
  const [accessOverride, setAccessOverride] =
    useState<Record<string, MeetingAccessLevel>>({});

  const [toast, setToastText] = useState("");
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showToast = useCallback((text: string) => {
    setToastText(text);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToastText(""), 3200);
  }, []);
  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
  }, []);

  const load = useCallback(async (isRefresh = false) => {
    if (!recordingKey) return;
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      const res = await getParticipants(recordingKey);
      setSpeakers(res.speakers);
      setParticipants(res.participants);
      setApiNames(res.speaker_names ?? {});
      setRecordingStatus(String(res.recording_status || ""));
      setAccessOverride({});
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load participants.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [recordingKey]);

  // The meeting's map is authoritative (renames land there first); the
  // participants API copy covers the moment before the meeting has loaded.
  const speakerNames = rec?.speaker_names ?? apiNames;

  const bySpeaker = useMemo(() => {
    const map: Record<string, ApiParticipant> = {};
    // Attendance rows are not speakers and must never occupy a speaker slot.
    participants.forEach((p) => { if (!p.attendance_only) map[p.speaker_id] = p; });
    return map;
  }, [participants]);

  const attendees = useMemo(
    () => participants.filter((p) => p.attendance_only), [participants]);

  /** People already tagged in this meeting — ranked first in the picker. */
  const meetingContacts = useMemo(
    () => participants.map((p) => p.contact).filter((c): c is ApiContact => !!c),
    [participants]);

  /** Map a speaker to a contact (or clear it with null). One mapping can
   *  resolve several AI tasks — said. */
  const assign = useCallback(async (
    speakerId: string, contact: ApiContact | null,
  ): Promise<boolean> => {
    setBusySpeaker(speakerId);
    setError("");
    try {
      const res = await setParticipant(
        recordingKey, speakerId, contact ? contact.id : null);
      invalidateSalesforce("relation");
      await load(true);
      await syncSpeakerNames();
      const n = res.tasks_resolved ?? 0;
      if (n > 0 && contact) {
        showToast(`${n} task${n === 1 ? "" : "s"} now assigned to ${contact.name}`);
      } else if (contact) {
        showToast(`${speakerName(speakerId, speakerNames)} tagged as ${contact.name}`);
      }
      return true;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not save that mapping.");
      return false;
    } finally {
      setBusySpeaker("");
    }
  }, [recordingKey, load, syncSpeakerNames, showToast, speakerNames]);

  /** A display name without a contact — for someone who genuinely isn't one
   *  (a receptionist, a one-off guest). Empty clears it. */
  const setName = useCallback(async (speakerId: string, name: string): Promise<boolean> => {
    setBusySpeaker(speakerId);
    try {
      await renameSpeaker(speakerId, name);
      await load(true);
      return true;
    } catch {
      return false; // renameSpeaker already alerted
    } finally {
      setBusySpeaker("");
    }
  }, [renameSpeaker, load]);

  /** Record that a contact attended without speaking. Idempotent server-side. */
  const addAttendee = useCallback(async (contact: ApiContact) => {
    setAttendeeBusy(true);
    setError("");
    try {
      await tagAttendee(recordingKey, contact.id);
      invalidateSalesforce("relation");
      await load(true);
      showToast(`${contact.name} marked as a participant`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not save that.");
    } finally {
      setAttendeeBusy(false);
    }
  }, [recordingKey, load, showToast]);

  /** Remove an attendance record, by the row key the API handed us. */
  const removeAttendee = useCallback(async (p: ApiParticipant) => {
    setAttendeeBusy(true);
    try {
      await setParticipant(recordingKey, p.speaker_id, null);
      invalidateSalesforce("relation");
      await load(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not remove that.");
    } finally {
      setAttendeeBusy(false);
    }
  }, [recordingKey, load]);

  /** Current level for a person — optimistic while a change is in flight. */
  const levelFor = useCallback((p: ApiParticipant | undefined): MeetingAccessLevel | null =>
    p?.access ? accessOverride[p.access.user_id] ?? p.access.level : null,
  [accessOverride]);

  const changeAccess = useCallback(async (
    userId: string, name: string, level: MeetingAccessLevel,
    previous: MeetingAccessLevel,
  ) => {
    if (level === previous) return;
    setError("");
    setAccessOverride((m) => ({ ...m, [userId]: level }));
    try {
      await setMeetingAccess(recordingKey, userId, level);
      showToast(level === "none"
        ? `${name} can no longer open this meeting`
        : `${name} now has ${ACCESS_LABEL[level].toLowerCase()}`);
    } catch (e) {
      // Roll back to what the server last confirmed.
      setAccessOverride((m) => ({ ...m, [userId]: previous }));
      setError(e instanceof ApiError ? e.message : "Could not change access.");
    }
  }, [recordingKey, showToast]);

  /** Is the pipeline still working on this recording? Unknown = still
   *  processing, the behaviour an older backend always had. */
  const stillProcessing = !recordingStatus
    || !["complete", "transcribed", "failed"].includes(recordingStatus);

  return {
    recordingKey, workspaceId: rec?.workspace_id,
    speakers, participants, bySpeaker, attendees, meetingContacts,
    speakerNames, stillProcessing,
    loading, refreshing, error, setError, busySpeaker, attendeeBusy, toast,
    load, assign, setName, addAttendee, removeAttendee, levelFor, changeAccess,
  };
}

// ===========================================================================
// The card — the Participants card, identical on every surface.
// ===========================================================================
export function AccessChip({
  level, name, onPress,
}: { level: MeetingAccessLevel; name: string; onPress: () => void }) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const on = level !== "none";
  return (
    <Pressable
      style={[st.accessChip, on && { backgroundColor: C.primarySoft, borderColor: C.primarySoft }]}
      onPress={onPress}
      hitSlop={6}
      accessibilityRole="button"
      accessibilityLabel={`Meeting access for ${name}: ${ACCESS_LABEL[level]}. Change`}
    >
      <Icon name={on ? "eye" : "eye.slash"} size={12} tintColor={on ? C.primary : C.textDim} />
      <Text style={[st.accessChipTxt, on && { color: C.primary }]}>{ACCESS_LABEL[level]}</Text>
      <Icon name="chevron.down" size={10} tintColor={on ? C.primary : C.textDim} />
    </Pressable>
  );
}

/** "Not on MinuteX" for a contact with no account; the chip otherwise.
 *  `access` undefined = an older backend, which renders nothing. */
export function ParticipantAccess({
  tagging, participant, onOpenAccess,
}: {
  tagging: SpeakerTagging;
  participant: ApiParticipant;
  onOpenAccess: (userId: string, name: string) => void;
}) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  if (participant.access === undefined) return null;
  if (participant.access === null) {
    return <Text style={st.accessNone}>Not on MinuteX · can&rsquo;t be given access</Text>;
  }
  const name = participant.contact?.name || "This person";
  const userId = participant.access.user_id;
  return (
    <AccessChip
      level={tagging.levelFor(participant) ?? "none"}
      name={name}
      onPress={() => onOpenAccess(userId, name)}
    />
  );
}

export function SpeakerTagCard({
  tagging, speakerId, onSelectContact, onSetName, onOpenAccess, flat,
}: {
  tagging: SpeakerTagging;
  speakerId: string;
  onSelectContact: (speakerId: string) => void;
  onSetName: (speakerId: string) => void;
  onOpenAccess: (userId: string, name: string) => void;
  /** Drop the card chrome when the card is the body of a sheet. */
  flat?: boolean;
}) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const sid = speakerId;
  const mapped = tagging.bySpeaker[sid];
  const contact = mapped?.contact;
  const busy = tagging.busySpeaker === sid;
  const shown = speakerName(sid, tagging.speakerNames);
  const named = shown !== `Speaker ${sid}`;

  return (
    <View style={flat ? undefined : st.card}>
      <View style={st.cardTop}>
        <View style={st.speakerBadge}>
          <Text style={st.speakerBadgeTxt}>{sid}</Text>
        </View>
        <View style={{ flex: 1, minWidth: 0 }}>
          {/* The CURRENT display name, resolved through the helper the
              transcript uses, so every surface agrees. */}
          <Text style={st.speakerLabel}>{shown}</Text>
          {contact ? (
            <Text style={st.mappedTo} numberOfLines={1}>
              {contact.name}
              {contact.email ? ` · ${contact.email}` : ""}
            </Text>
          ) : named ? (
            // Named, but not tied to a Contact — what is missing is the link,
            // so say that rather than "Not yet identified" under their name.
            <Text style={st.unmapped}>Speaker {sid} · not linked to a contact</Text>
          ) : (
            <Text style={st.unmapped}>Not yet identified</Text>
          )}
        </View>
        {contact && (
          <View style={[st.avatar, { backgroundColor: avatarColorFor(contact.name) }]}>
            <Text style={st.avatarTxt}>{initialsOf(contact.name)}</Text>
          </View>
        )}
      </View>

      <View style={st.cardActions}>
        {/* TAG A CONTACT — the preferred path, offered first: a contact is a
            real person tasks can be assigned to and who can be notified. */}
        <Pressable
          style={[st.action, { backgroundColor: C.primarySoft }]}
          onPress={() => onSelectContact(sid)}
          disabled={busy}
          hitSlop={6}
          accessibilityRole="button"
          accessibilityLabel={contact
            ? `Change contact for speaker ${sid}` : `Select contact for speaker ${sid}`}
        >
          <Icon name={contact ? "pencil" : "plus"} size={13} tintColor={C.primary} />
          <Text style={[st.actionTxt, { color: C.primary }]}>
            {busy ? "Saving…" : contact ? "Change Contact" : "Select Contact"}
          </Text>
        </Pressable>
        {/* A plain display name, for someone who isn't a contact. Secondary,
            and hidden once a contact is attached — the contact's name wins. */}
        {!contact && (
          <Pressable
            style={st.action}
            onPress={() => onSetName(sid)}
            disabled={busy}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel={named ? `Rename speaker ${sid}` : `Set a name for speaker ${sid}`}
          >
            <Icon name="text.bubble" size={13} tintColor={C.textDim} />
            <Text style={[st.actionTxt, { color: C.textDim }]}>
              {named ? "Rename" : "Set name"}
            </Text>
          </Pressable>
        )}
        {!!contact && (
          <Pressable
            style={st.action}
            onPress={() => { void tagging.assign(sid, null); }}
            disabled={busy}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel={`Clear mapping for speaker ${sid}`}
          >
            <Icon name="xmark" size={13} tintColor={C.textDim} />
            <Text style={[st.actionTxt, { color: C.textDim }]}>Clear</Text>
          </Pressable>
        )}
      </View>

      {/* MEETING ACCESS — who else can open this meeting, read-only. */}
      {!!contact && !!mapped && mapped.access !== undefined && (
        <View style={st.accessRow}>
          <Text style={st.rowLabel}>Meeting access</Text>
          <ParticipantAccess tagging={tagging} participant={mapped} onOpenAccess={onOpenAccess} />
        </View>
      )}
    </View>
  );
}

// ===========================================================================
// The three follow-ups the card opens.
// ===========================================================================
function AccessOptions({
  tagging, userId, name, onDone,
}: { tagging: SpeakerTagging; userId: string; name: string; onDone: () => void }) {
  // Any row for that user carries the one shared state.
  const p = tagging.participants.find((x) => x.access?.user_id === userId);
  const current = tagging.levelFor(p);
  return (
    <>
      {ACCESS_OPTIONS.map((o) => (
        <SheetAction
          key={o.level}
          label={o.label}
          description={o.description}
          icon={o.icon}
          selected={current === o.level}
          onPress={() => {
            onDone();
            if (current) void tagging.changeAccess(userId, name, o.level, current);
          }}
        />
      ))}
    </>
  );
}

const ACCESS_SUBTITLE =
  "What they can see in MinuteX. Everything is read-only; only you can edit this meeting.";

/** Access choice as its own sheet (the Participants list). */
export function SpeakerAccessSheet({
  tagging, target, onClose,
}: {
  tagging: SpeakerTagging;
  target: { userId: string; name: string } | null;
  onClose: () => void;
}) {
  return (
    <BottomSheet
      visible={!!target}
      onClose={onClose}
      title={target ? `${target.name}’s access` : "Access"}
      subtitle={ACCESS_SUBTITLE}
    >
      {target && (
        <AccessOptions tagging={tagging} userId={target.userId} name={target.name} onDone={onClose} />
      )}
    </BottomSheet>
  );
}

function NameEditor({
  tagging, speakerId, onDone,
}: { tagging: SpeakerTagging; speakerId: string; onDone: () => void }) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const [draft, setDraft] = useState(tagging.speakerNames?.[speakerId] ?? "");
  const saving = tagging.busySpeaker === speakerId;
  const save = async () => { if (await tagging.setName(speakerId, draft)) onDone(); };
  return (
    <View>
      <TextInput
        style={st.nameInput}
        value={draft}
        onChangeText={setDraft}
        placeholder="Their name"
        placeholderTextColor={C.textFaint}
        autoCapitalize="words"
        autoFocus
        maxLength={60}
        editable={!saving}
        returnKeyType="done"
        onSubmitEditing={save}
      />
      <Text style={st.nameHint}>
        A name only — no contact, so no tasks or access. Updates the transcript and
        participants immediately; generated documents that mention this speaker will
        be flagged for an update.
      </Text>
      <View style={{ flexDirection: "row", gap: S.sm, marginTop: S.lg }}>
        <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={onDone} disabled={saving} />
        <Button label="Save" style={{ flex: 1 }} onPress={save} loading={saving} />
      </View>
    </View>
  );
}

/** The contact picker, configured the same way on every surface: people in
 *  this meeting first, "That's me", Salesforce people, and Clear. */
export function SpeakerContactPicker({
  tagging, speakerId, onClose, onPicked,
}: {
  tagging: SpeakerTagging;
  speakerId: string | null;
  onClose: () => void;
  onPicked?: () => void;
}) {
  const mapped = speakerId ? tagging.bySpeaker[speakerId] : undefined;
  return (
    <ContactPicker
      visible={speakerId !== null}
      onClose={onClose}
      onPick={(c) => {
        onClose();
        if (speakerId) void tagging.assign(speakerId, c).then((ok) => { if (ok) onPicked?.(); });
      }}
      onClear={mapped?.contact_id
        ? () => { onClose(); if (speakerId) void tagging.assign(speakerId, null); }
        : undefined}
      meetingContacts={tagging.meetingContacts}
      // You are almost always IN your own meeting, and "Speaker 0" is very
      // often you — without this you had to type your own email by hand.
      allowSelf
      salesforceWorkspaceId={tagging.workspaceId}
      meetingKey={tagging.recordingKey}
      title={speakerId ? `Who is ${speakerName(speakerId, tagging.speakerNames)}?` : "Select Contact"}
    />
  );
}

/** A name prompt as its own modal (the Participants list). */
export function SpeakerNameSheet({
  tagging, speakerId, onClose,
}: { tagging: SpeakerTagging; speakerId: string | null; onClose: () => void }) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  return (
    <Modal visible={speakerId !== null} transparent animationType="fade" onRequestClose={onClose}>
      <KeyboardAwareSheet>
        <Pressable style={st.backdrop} onPress={onClose}>
          <Pressable style={st.sheet} onPress={(e) => e.stopPropagation()}>
            <Text style={st.sheetEyebrow}>
              {speakerId ? speakerName(speakerId, tagging.speakerNames) : ""}
            </Text>
            <Text style={st.sheetTitle}>Set a name</Text>
            {speakerId && <NameEditor tagging={tagging} speakerId={speakerId} onDone={onClose} />}
          </Pressable>
        </Pressable>
      </KeyboardAwareSheet>
    </Modal>
  );
}

// ===========================================================================
// The card in a sheet — the People tab and the Transcript.
//
// One Modal with three modes (card / name / access) rather than modals opened
// from a modal: on Android a Modal presented from another can render behind
// it. The contact picker is the one exception, mounted as a SIBLING so this
// sheet stays open underneath it and cancelling returns here.
// ===========================================================================
export function SpeakerTagSheet({
  tagging, speakerId, onClose,
}: { tagging: SpeakerTagging; speakerId: string | null; onClose: () => void }) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const [mode, setMode] = useState<"card" | "name" | "access">("card");
  const [accessTarget, setAccessTarget] = useState<{ userId: string; name: string } | null>(null);
  const [pickerFor, setPickerFor] = useState<string | null>(null);
  const { load } = tagging;

  // Every open starts on the card. A render-phase reset (as BottomSheet does)
  // rather than an effect, so the first frame never shows a stale mode.
  const [openedFor, setOpenedFor] = useState<string | null>(null);
  if (speakerId !== openedFor) {
    setOpenedFor(speakerId);
    setMode("card");
    setAccessTarget(null);
  }
  // Fresh mapping every time the sheet opens on a speaker: another surface
  // may have changed it since this one last loaded.
  useEffect(() => {
    if (speakerId !== null) void load(true);
  }, [speakerId, load]);

  const title = mode === "name" ? "Set a name"
    : mode === "access" && accessTarget ? `${accessTarget.name}’s access`
      : "Who is this?";

  return (
    <>
      <Modal visible={speakerId !== null} transparent
             animationType="fade" onRequestClose={onClose}>
        <KeyboardAwareSheet>
          <Pressable style={st.backdrop} onPress={onClose}>
            <Pressable style={st.sheet} onPress={(e) => e.stopPropagation()}>
              <ScrollView keyboardShouldPersistTaps="handled" bounces={false}
                          showsVerticalScrollIndicator={false}>
                <Text style={st.sheetEyebrow}>
                  {speakerId ? speakerName(speakerId, tagging.speakerNames) : ""}
                </Text>
                <Text style={st.sheetTitle}>{title}</Text>
                {mode === "access" && <Text style={st.nameHint}>{ACCESS_SUBTITLE}</Text>}

                {!!tagging.error && <Text style={st.errorTxt}>{tagging.error}</Text>}

                {speakerId !== null && mode === "card" && (
                  <SpeakerTagCard
                    flat
                    tagging={tagging}
                    speakerId={speakerId}
                    onSelectContact={(sid) => setPickerFor(sid)}
                    onSetName={() => setMode("name")}
                    onOpenAccess={(userId, name) => { setAccessTarget({ userId, name }); setMode("access"); }}
                  />
                )}
                {speakerId !== null && mode === "name" && (
                  <NameEditor tagging={tagging} speakerId={speakerId} onDone={() => setMode("card")} />
                )}
                {mode === "access" && accessTarget && (
                  <AccessOptions tagging={tagging} userId={accessTarget.userId}
                                 name={accessTarget.name} onDone={() => setMode("card")} />
                )}

                {mode === "card" && (
                  <Button label="Done" variant="secondary" onPress={onClose}
                          style={{ marginTop: S.lg }} />
                )}
              </ScrollView>
            </Pressable>
          </Pressable>
        </KeyboardAwareSheet>
      </Modal>

      <SpeakerContactPicker
        tagging={tagging}
        speakerId={pickerFor}
        onClose={() => setPickerFor(null)}
      />
    </>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    card: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      marginBottom: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    cardTop: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md },
    speakerBadge: {
      width: 34, height: 34, borderRadius: R.md, backgroundColor: C.surface2,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    speakerBadgeTxt: { fontFamily: FONT.bold, fontSize: 14, color: C.textDim },
    speakerLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.3, color: C.textFaint,
    },
    mappedTo: { fontFamily: FONT.bold, fontSize: 14.5, color: C.text, marginTop: 3 },
    unmapped: { fontFamily: FONT.medium, fontSize: 13.5, color: C.textFaint, marginTop: 3 },
    avatar: {
      width: 36, height: 36, borderRadius: 18,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 13, color: "#fff" },
    cardActions: {
      flexDirection: "row" as const, flexWrap: "wrap" as const,
      alignItems: "center" as const, gap: S.sm,
      marginTop: 14, paddingTop: 12, borderTopWidth: 1, borderTopColor: C.border,
    },
    action: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      paddingVertical: 7, paddingHorizontal: 12, borderRadius: R.pill, minHeight: 32,
    },
    actionTxt: { fontFamily: FONT.bold, fontSize: 12.5 },
    rowLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1,
      color: C.textFaint, marginBottom: 8,
    },
    accessRow: {
      marginTop: 12, paddingTop: 12, borderTopWidth: 1, borderTopColor: C.border,
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const, gap: S.sm,
    },
    accessChip: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 5,
      paddingVertical: 6, paddingHorizontal: 10, borderRadius: R.pill,
      borderWidth: 1, borderColor: C.border, minHeight: 32,
    },
    accessChipTxt: { fontFamily: FONT.bold, fontSize: 12, color: C.textDim },
    accessNone: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, flexShrink: 1 },
    backdrop: {
      flex: 1, backgroundColor: "rgba(0,0,0,0.45)",
      justifyContent: "flex-end" as const,
    },
    sheet: {
      backgroundColor: C.surface, borderTopLeftRadius: R.card, borderTopRightRadius: R.card,
      padding: S.lg, paddingBottom: S.xl, maxHeight: "85%",
    },
    sheetEyebrow: {
      fontFamily: FONT.semibold, fontSize: 11, color: C.textFaint,
      textTransform: "uppercase" as const, letterSpacing: 0.6,
    },
    sheetTitle: {
      fontFamily: FONT.bold, fontSize: 18, color: C.text, marginTop: 2, marginBottom: S.md,
    },
    nameInput: {
      fontFamily: FONT.medium, fontSize: 15, color: C.text, backgroundColor: C.bg,
      borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      paddingHorizontal: S.md, paddingVertical: 11,
    },
    nameHint: { ...T.caption, marginTop: 8, marginBottom: S.sm },
    errorTxt: { fontFamily: FONT.medium, fontSize: 13, color: C.danger, marginBottom: S.sm },
  });
}
