// src/app/recording/[key]/index.tsx — Meeting Detail: four analysis tabs.
//
// MinuteX is an AI Meeting Workspace, not a printed brief and not a scroll of
// every AI surface the backend can produce. FOUR tabs, an animated segmented
// control, and exactly one entry point per capability:
//
//   Overview   The AI's own sections for THIS meeting — dynamic titles,
//              dynamic count, chosen by the model (lib/meeting-overview.tsx).
//   Speakers   Who spoke, their talk time, rename / map to a contact.
//   Tasks      The meeting's tasks: add, open, view all.
//   Documents  Generated documents, the MoM editor, Create Document.
//
// The TRANSCRIPT is NOT a tab. It is its own screen
// (recording/[key]/transcript), reached from "View Transcript" under the
// player. Two reasons: it is the longest thing in the product and the one
// thing read top-to-bottom, so a masthead + player + tab bar cost it the most
// vertical space; and it is the only surface with no AI on it, which made it a
// poor fit beside three that are entirely AI.
//
// Each tab was previously a BLOCK stacked in one scrolling Overview. Splitting
// them changed no behaviour inside any block — the same components, the same
// props, the same handlers. What changed is that only one is mounted at a time.
//
// Shared: title (tap to rename), a large Apple-Music-style audio player,
// overflow menu (share/rename), speaker rename (from the Speakers tab or the
// transcript screen — one mapping drives both), and a floating Assistant
// button that opens the dedicated workspace at recording/[key]/assistant.
//
// MeetingProvider is mounted by the parent recording/[key]/_layout.tsx, not
// here — this screen, Assistant, Task Detail, Assign To and Notify all share
// that one instance so task/document state survives navigating between them.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator, Alert, KeyboardAvoidingView, Modal, Platform, Pressable,
  ScrollView, StyleSheet, Text, TextInput, View,
} from "react-native";
import { Stack, useRouter } from "expo-router";
import { Icon } from "../../../../lib/icons";
import { S, R, FONT, useTheme, ColorScale } from "../../../../lib/theme";
import { Button, SegmentedTabs, Skeleton } from "../../../../lib/ui";
import { AudioPlayer, AudioPlayerProvider } from "../../../../lib/audio-player";
import { useMeeting } from "../../../../lib/meeting-context";
import { sourceMeta, statusMeta, speakerName, normalizeSpeakerLabel, fmtDuration } from "../../../../lib/sources";
import { MeetingSummary, Highlights, Participants } from "../../../../lib/meeting-summary";
import { MeetingOverviewView, hasOverview } from "../../../../lib/meeting-overview";
import { buildSpeakerBlocks, buildSpeakerColors } from "../../../../lib/transcript-view";
import { CrmRecordsBlock } from "../../../../lib/meeting-crm-records";
import { Tasks } from "../../../../lib/meeting-tasks";
import { AddTaskSheet } from "../../../../lib/add-task-sheet";
import { DocumentsList, CreateDocumentSheet } from "../../../../lib/meeting-documents";
import { ShareMeetingSheet } from "../../../../lib/meeting-share";
import { GmailShareRow, GmailShareSheet } from "../../../../lib/gmail-share";
import { useGmail } from "../../../../lib/integrations";
import { MomEditorScreen } from "../../../../lib/mom-editor";
import { AssistantButton } from "../../../../lib/assistant-button";
import { ContactPicker } from "../../../../lib/contact-picker";
import { avatarColorFor, initialsOf } from "../../../../lib/task-model";
import {
  ApiError, isAlreadyRunning, isStillUploading, reprocessRecording, trashRecording,
  ApiContact, getParticipants, setParticipant,
} from "../../../../lib/api";

// The four ANALYSIS tabs. "transcript" is deliberately NOT among them — it is
// a screen (see the header). Adding a tab here means adding a case to the tab
// body below and an entry to the SegmentedTabs list; nothing else.
type Tab = "overview" | "speakers" | "tasks" | "documents";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    center: { alignItems: "center" as const, justifyContent: "center" as const, gap: S.md },
    masthead: { paddingHorizontal: 20, paddingTop: 6 },
    titleRow: { flexDirection: "row" as const, alignItems: "flex-start" as const, gap: 10 },
    headline: { fontFamily: FONT.extrabold, fontSize: 21, lineHeight: 26, color: C.text, flex: 1, letterSpacing: -0.2 },
    dateline: { fontFamily: FONT.medium, fontSize: 12.5, color: C.textFaint, marginTop: 4 },
    section: { gap: S.xl, marginTop: S.lg },
    stepRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 12,
      paddingVertical: 14, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    stepDot: {
      width: 24, height: 24, borderRadius: 12, flexShrink: 0,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    stepLabel: { flex: 1, fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    progressTrack: { height: 6, backgroundColor: C.border, borderRadius: 3, overflow: "hidden" as const, marginTop: 20 },
    failCard: { backgroundColor: C.dangerSoft, borderRadius: R.card, padding: S.lg, marginTop: 20 },
    noticeCard: { backgroundColor: C.surface2, borderRadius: R.card, padding: S.lg },
    errorBig: { fontFamily: FONT.extrabold, fontSize: 20, lineHeight: 26, color: C.text, textAlign: "center" as const },
    sheetBackdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" as const },
    sheet: {
      backgroundColor: C.surface, paddingHorizontal: 22, paddingTop: 20, paddingBottom: 32,
      borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl,
    },
    sheetTitle: { fontFamily: FONT.extrabold, fontSize: 20, lineHeight: 26, color: C.text, marginTop: 6 },
    nameInput: {
      backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      fontFamily: FONT.regular, fontSize: 17, color: C.text, paddingVertical: 12, paddingHorizontal: 14, marginTop: 18,
    },
    // Tag-a-contact row inside the rename sheet. Reads as a tappable list row
    // rather than a button, because it leads to a picker rather than acting.
    tagRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      marginTop: 14, padding: 12, borderRadius: R.md,
      borderWidth: 1, borderColor: C.border, backgroundColor: C.surface2,
    },
    tagAvatar: {
      width: 36, height: 36, borderRadius: 18,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    tagAvatarTxt: { fontFamily: FONT.bold, fontSize: 13, color: "#fff" },
    tagName: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    tagHint: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      marginTop: 2,
    },
    tagAction: { fontFamily: FONT.bold, fontSize: 12.5, color: C.primary },
    // Separates the two ways of answering "who is this?" so neither looks like
    // a sub-field of the other.
    orRule: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      textAlign: "center" as const, marginTop: 14, marginBottom: 2,
    },
    menuRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 12,
      paddingVertical: 15, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    menuTxt: { fontFamily: FONT.medium, fontSize: 15, color: C.text },
  });
}

// A recording is "probably stuck", not merely slow, once it has sat in a
// non-terminal status for this long. The pipeline normally finishes in ~2
// minutes; the stranded rows that motivated scripts/26_reprocess_stuck.py sat
// at "transcribing"/"generating_ai" forever, and the app polled them with no
// way out. Generous on purpose — offering "try again" to a run that is merely
// slow would spend money re-doing work that was about to succeed.
const LIKELY_STUCK_AFTER_MS = 15 * 60 * 1000;

// Statuses the backend will actually replay — REPROCESSABLE_STATUSES in
// cloud/functions/userapi/lambda_function.py. "uploading"/"uploaded" are
// excluded there because the S3 trigger may still be about to fire for them,
// so POST /recordings/ai/reprocess answers 409 for those two.
//
// The button is gated on this list because offering an action the server
// always refuses is worse than offering none: a recording whose bytes never
// landed sat at "Uploading" forever, showed the stalled banner, and every
// press of "Start over" returned a 409 the user could do nothing about. They
// still see the honest "this has stalled" copy — it just no longer promises a
// recovery that does not exist for that state. Trash remains the way out.
const REPROCESSABLE_STATUSES = new Set([
  "failed", "transcribed", "transcribing", "generating_ai",
]);

function StillWriting({
  st, C, stage, stuck, canRetry, retrying, onRetry, retryNote,
}: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  stage: number;
  stuck: boolean;
  /** Whether the backend will accept a reprocess for this status. */
  canRetry: boolean;
  retrying: boolean;
  onRetry: () => void;
  retryNote: string;
}) {
  const steps = ["Audio received", "Transcribed", "Summary written"];
  return (
    <View style={{ marginTop: 22 }}>
      {steps.map((label, i) => {
        const done = i < stage;
        const active = i === stage;
        return (
          <View key={label} style={[st.stepRow, i > stage && { opacity: 0.4 }]}>
            <View style={[
              st.stepDot,
              done ? { backgroundColor: C.success } : active ? { backgroundColor: C.primarySoft } : { backgroundColor: C.surface2 },
            ]}>
              {done ? <Icon name="checkmark" tintColor="#FFFFFF" size={13} /> : null}
            </View>
            <Text style={st.stepLabel}>{label}</Text>
          </View>
        );
      })}
      <View style={st.progressTrack}>
        <View style={{ width: `${Math.round((stage / steps.length) * 100)}%`, height: "100%", backgroundColor: C.primary, borderRadius: 3 }} />
      </View>
      <Text style={{ fontFamily: FONT.regular, fontSize: 13.5, color: C.textDim, marginTop: 14 }}>
        {stuck
          ? canRetry
            ? "This is taking much longer than usual. It may have stalled — you can start it over."
            : "This is taking much longer than usual. The audio may not have finished uploading — you can remove this recording and record again."
          : "Usually done in about two minutes. You can leave — it'll be here when it's ready."}
      </Text>
      {stuck && canRetry ? (
        <>
          <Button
            label="Start over"
            variant="secondary"
            loading={retrying}
            onPress={onRetry}
            style={{ marginTop: 16 }}
          />
          {retryNote ? (
            <Text style={{ fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim, marginTop: 10 }}>
              {retryNote}
            </Text>
          ) : null}
        </>
      ) : null}
    </View>
  );
}

function fmtDate(iso: string) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export default function MeetingDetailScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const {
    key, rec, loading, error, reload, documents, addDocument, setDocuments,
    renameMeeting, renameSpeaker, setCrmRecord, chooseCrmCandidate,
    confirmCrmRecord, syncCrmRecordNow, crmMappings, crmAmbiguity, tasks,
    documentsNeedingUpdate, updateAllDocuments, addTask,
  } = useMeeting();
  const [tab, setTab] = useState<Tab>("overview");

  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [savingTitle, setSavingTitle] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  // The structured MoM editor. Full-screen rather than a route so the
  // Overview state underneath (documents, tasks) survives closing it.
  const [momOpen, setMomOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [gmailShareOpen, setGmailShareOpen] = useState(false);
  // The ONE Gmail check this screen makes. Reading the shared context rather
  // than fetching status here is what keeps this menu in step with the
  // Integrations screen: disconnecting Gmail there removes the action here
  // without this screen being remounted.
  const gmail = useGmail();

  const [editingSpeaker, setEditingSpeaker] = useState<string | null>(null);
  const [speakerDraft, setSpeakerDraft] = useState("");
  const [savingSpeaker, setSavingSpeaker] = useState(false);

  const [search, setSearch] = useState("");

  // Reprocess — re-run transcription + AI for a recording that failed or
  // stalled. Confirmed first because each run costs real API spend, and the
  // backend refuses a repeat inside its cooldown (429 -> isAlreadyRunning).
  const [retrying, setRetrying] = useState(false);
  const [retryNote, setRetryNote] = useState("");

  const retryProcessing = useCallback(() => {
    Alert.alert(
      "Process this recording again?",
      "We'll re-run transcription and rewrite the summary. This can take a few minutes.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Try again",
          onPress: async () => {
            setRetrying(true);
            setRetryNote("");
            try {
              await reprocessRecording(key);
              setRetryNote("Started — this can take a few minutes.");
              // The row is now "transcribing"; reload so the screen switches to
              // the progress UI it already shows for a first-time upload.
              await reload();
            } catch (e) {
              setRetryNote(
                isAlreadyRunning(e)
                  ? (e as ApiError).message
                  : e instanceof ApiError
                    ? e.message
                    : "Couldn't start. Please try again."
              );
            } finally {
              setRetrying(false);
            }
          },
        },
      ]
    );
  }, [key, reload]);

  // Move to Trash. NOTHING is destroyed here: the audio, the transcript and
  // every AI artifact stay on the row, and Trash offers Restore. That is why
  // the dialog promises a way back instead of warning about a permanent loss —
  // only the Trash screen can actually delete a recording.
  //
  // On success we navigate back rather than reload: this screen's recording is
  // no longer on MinuteX, and re-fetching would only show a meeting the user
  // just filed away. MinuteX re-queries on focus, so it is already gone by
  // the time they land there.
  const [deleting, setDeleting] = useState(false);

  const confirmDelete = useCallback(() => {
    setMenuOpen(false);
    Alert.alert(
      "Move this meeting to Trash?",
      "Its transcript and everything written from it — summary, documents, "
      + "tasks and chat — move with it. You can restore it later from Trash.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Move to Trash",
          style: "destructive",
          onPress: async () => {
            setDeleting(true);
            try {
              await trashRecording(key);
              router.back();
            } catch (e) {
              setDeleting(false);
              Alert.alert(
                "Couldn't move to Trash",
                isStillUploading(e)
                  ? "This recording is still being processed. Try again in a minute."
                  : e instanceof ApiError
                    ? e.message
                    : "Something went wrong. Please try again."
              );
            }
          },
        },
      ]
    );
  }, [key, router]);

  const speakerNames = rec?.speaker_names ?? undefined;

  const openRenameTitle = () => {
    setTitleDraft(rec?.title || "");
    setRenaming(true);
    setMenuOpen(false);
  };
  const saveTitle = async () => {
    setSavingTitle(true);
    try {
      await renameMeeting(titleDraft);
      setRenaming(false);
    } catch {
      // renameMeeting already alerted
    } finally {
      setSavingTitle(false);
    }
  };

  // Opens the share-link sheet. This used to Share.share() the meeting TITLE
  // as plain text — the recipient got a sentence, not the meeting. A link
  // gives them the actual notes on their phone with no MinuteX account, and
  // unlike an exported PDF it can be narrowed or revoked afterwards.
  // See lib/meeting-share.tsx.
  const shareMeeting = () => {
    setMenuOpen(false);
    setShareOpen(true);
  };

  // Contact tagging inside the rename sheet.
  //
  // Renaming a speaker used to write a free-text NAME only, which is a display
  // label and nothing more: no contact, so no task could be assigned to that
  // person and nothing could notify them. Tagging a real contact here does both
  // at once — the backend's setParticipant writes the participant row AND syncs
  // speaker_names, so the transcript reads the same either way.
  //
  // Typing a name is still allowed. Some speakers genuinely aren't contacts
  // (a receptionist, a one-off attendee), and forcing a contact record for them
  // would be worse than a plain label.
  // Manual task entry, for what the AI did not extract.
  const [addTaskOpen, setAddTaskOpen] = useState(false);
  const [speakerPickerOpen, setSpeakerPickerOpen] = useState(false);
  const [speakerContacts, setSpeakerContacts] = useState<ApiContact[]>([]);
  const [taggedContact, setTaggedContact] = useState<ApiContact | null>(null);

  const openRenameSpeaker = (label: string) => {
    const raw = normalizeSpeakerLabel(label);
    setEditingSpeaker(raw);
    setSpeakerDraft(speakerNames?.[raw] ?? "");
    setTaggedContact(null);
    // Load who is already tagged in this meeting so the picker can rank
    // them first. Best-effort: a failure only affects ORDERING, never
    // whether the sheet works.
    void getParticipants(String(key))
      .then((p) => {
        setSpeakerContacts(
          p.participants
            .map((x) => x.contact)
            .filter((c): c is ApiContact => !!c)
        );
        // Show what this speaker is CURRENTLY tagged as, so the sheet reflects
        // reality rather than looking untagged every time it opens.
        const mine = p.participants.find((x) => x.speaker_id === raw);
        if (mine?.contact) setTaggedContact(mine.contact);
      })
      .catch(() => {
        setSpeakerContacts([]);
      });
  };

  /** Tag this speaker as a real contact. */
  const tagSpeakerContact = async (contact: ApiContact) => {
    if (!editingSpeaker) return;
    setSavingSpeaker(true);
    try {
      const res = await setParticipant(String(key), editingSpeaker, contact.id);
      setTaggedContact(contact);
      setSpeakerDraft(contact.name);
      // Re-read the meeting so the transcript and Participants list pick up the
      // speaker_names the backend just synced.
      await reload();
      setEditingSpeaker(null);
      const n = res.tasks_resolved ?? 0;
      if (n > 0) {
        Alert.alert(
          "Speaker tagged",
          `${contact.name} is now Speaker ${editingSpeaker}. ` +
          `${n} task${n === 1 ? "" : "s"} from this meeting ${n === 1 ? "is" : "are"} ` +
          `now assigned to them.`
        );
      }
    } catch (e) {
      Alert.alert(
        "Couldn't tag that contact",
        e instanceof ApiError ? e.message : "Something went wrong."
      );
    } finally {
      setSavingSpeaker(false);
    }
  };
  const saveSpeaker = async () => {
    if (!editingSpeaker) return;
    setSavingSpeaker(true);
    try {
      await renameSpeaker(editingSpeaker, speakerDraft);
      setEditingSpeaker(null);
    } catch {
      // renameSpeaker already alerted
    } finally {
      setSavingSpeaker(false);
    }
  };

  const speakerColors = useMemo(
    () => buildSpeakerColors(rec?.timestamps, C.speakers),
    [rec?.timestamps, C.speakers]);

  const speakerBlocks = useMemo(
    () => buildSpeakerBlocks(rec?.timestamps), [rec?.timestamps]);

  // Talk time per speaker, summed from the segments the transcript already
  // carries — sum(end - start). Derived on the client precisely because the
  // data is already here: asking the backend for it would be a new field, a
  // new write and a migration for something computable in one pass.
  const talkTime = useMemo(() => {
    const totals = new Map<string, number>();
    for (const seg of rec?.timestamps ?? []) {
      const span = (Number(seg.end) || 0) - (Number(seg.start) || 0);
      if (span > 0) {
        totals.set(seg.speaker, (totals.get(seg.speaker) ?? 0) + span);
      }
    }
    return totals;
  }, [rec?.timestamps]);

  const resolveName = useCallback(
    (label: string) => speakerName(label, speakerNames),
    [speakerNames]
  );

  // ---------- loading ----------
  if (loading) {
    return (
      <ScrollView style={st.container} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled">
        <Stack.Screen options={{ title: "Meeting" }} />
        <Skeleton style={{ width: 92, marginTop: S.lg }} />
        <Skeleton style={{ width: "100%", height: 26, marginTop: 12 }} />
        <Skeleton style={{ width: "72%", height: 26, marginTop: 6 }} />
        <Skeleton style={{ width: "45%", marginTop: 14 }} />
      </ScrollView>
    );
  }

  // ---------- error ----------
  if (error || !rec) {
    return (
      <View style={[st.container, st.center, { flex: 1 }]}>
        <Stack.Screen options={{ title: "Meeting" }} />
        <Text style={st.errorBig}>{error || "Meeting unavailable."}</Text>
        <Button label="Try again" variant="secondary" onPress={reload} style={{ alignSelf: "stretch" }} />
      </View>
    );
  }

  const status = statusMeta(rec.status);
  const failed = status.kind === "failed";
  // The backend's own status is the source of truth for "still working" —
  // NOT whether transcript/summary happen to be non-empty strings. A
  // recording can legitimately finish with status="complete" (or the legacy
  // "transcribed") and an empty summary (Groq's map-reduce came back blank
  // without throwing — a backend-side bug, tracked separately in
  // lambda-transcribe-live). Gating on rec.transcript/rec.summary alone made
  // that case show "still writing the summary" forever, since a done
  // recording with no summary looked identical to one still in progress.
  const stillProcessing = status.kind === "processing";
  // Age is measured from created_at, the one timestamp every row has
  // regardless of source. An unparseable date reads as "not stuck" so a bad
  // timestamp can never nag the user to re-spend on a healthy recording.
  const createdMs = rec.created_at ? new Date(rec.created_at).getTime() : NaN;
  const likelyStuck =
    stillProcessing && !isNaN(createdMs) &&
    Date.now() - createdMs > LIKELY_STUCK_AFTER_MS;
  const srcMeta = sourceMeta(rec);
  const duration = fmtDuration(rec.duration);
  const hasTranscript = !!rec.transcript;
  const showTabs = hasTranscript;

  // "Is there an analysis to show?" — the overview is the primary output, and
  // a recording analysed before it shipped has only the legacy summary. Either
  // one counts; neither means the AI stage produced nothing.
  const hasAnalysis = hasOverview(rec.overview) || !!rec.summary;

  const stage = rec.transcript ? (hasAnalysis ? 2 : 1) : 0;

  const datelineParts = [fmtDate(rec.created_at), duration, srcMeta.label].filter(Boolean);

  return (
    <AudioPlayerProvider url={rec.audio_url}>
      <View style={{ flex: 1, backgroundColor: C.bg }}>
        <Stack.Screen
          options={{
            title: rec.title || "Meeting",
            headerRight: () => (
              <Pressable onPress={() => setMenuOpen(true)} hitSlop={10} accessibilityLabel="More">
                <Icon name="ellipsis" tintColor={C.text} size={20} />
              </Pressable>
            ),
          }}
        />

        <ScrollView
          style={st.container}
          contentContainerStyle={{ paddingBottom: rec.audio_url ? 130 : 40 }}
          showsVerticalScrollIndicator={false}
          keyboardDismissMode="on-drag"
          stickyHeaderIndices={showTabs ? [1] : undefined}
          keyboardShouldPersistTaps="handled">
          {/* ---- Shared masthead: title (tap to rename), dateline, player ---- */}
          <View>
            <Pressable onPress={openRenameTitle} style={st.titleRow} accessibilityLabel="Rename meeting">
              <Text style={st.headline}>{rec.title || "Untitled meeting"}</Text>
            </Pressable>
            <Text style={st.dateline}>{datelineParts.join(" · ")}</Text>

            {rec.audio_url ? (
              <View style={{ marginTop: S.lg }}>
                <AudioPlayer url={rec.audio_url} seed={key} variant="hero" />
              </View>
            ) : null}

            {/* The transcript's one entry point. Directly under the player
                because the two are used together (tap a timestamp, hear it),
                and ABOVE the tab bar so it reads as a peer of the meeting
                itself rather than as one more analysis view. */}
            {hasTranscript ? (
              <Button
                label="View Transcript"
                variant="secondary"
                onPress={() => router.push({
                  pathname: "/recording/[key]/transcript", params: { key },
                })}
                style={{ marginTop: S.lg }}
              />
            ) : null}
          </View>

          {showTabs ? (
            <View style={{ backgroundColor: C.bg, paddingTop: S.lg, paddingBottom: S.sm }}>
              <SegmentedTabs
                value={tab}
                onChange={(v) => setTab(v as Tab)}
                tabs={[
                  { key: "overview", label: "Overview" },
                  { key: "speakers", label: "Speakers" },
                  { key: "tasks", label: "Tasks" },
                  { key: "documents", label: "Documents" },
                ]}
              />
            </View>
          ) : null}

          {/* ---- Failed ---- */}
          {failed ? (
            <View style={st.failCard}>
              <Text style={{ fontFamily: FONT.bold, fontSize: 16, lineHeight: 22, color: C.danger }}>
                We couldn&apos;t write this one up, so we stopped rather than guess.
              </Text>
              <Text style={{ fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim, marginTop: 10 }}>
                The recording finished, but transcription didn&apos;t return usable text —
                most often that means the audio was too quiet or too short. The
                audio itself is still here; nothing was thrown away.
              </Text>
              {/* The audio is still in S3, so this is genuinely recoverable —
                  a transient STT/Groq failure usually succeeds on a second
                  pass. (Audio that is truly too quiet will fail again, which
                  is why the confirm dialog names the cost in time.) */}
              {/* Two ways out of a dead end, side by side: replay it, or file
                  it away. Before this, a recording that failed twice could
                  only be left on MinuteX forever. Trash keeps the audio, so
                  a user who bins it can still change their mind. */}
              <View style={{ flexDirection: "row", gap: S.sm, marginTop: 16 }}>
                <Button
                  label="Try again"
                  variant="secondary"
                  loading={retrying}
                  onPress={retryProcessing}
                  style={{ flex: 1 }}
                />
                <Button
                  label="Move to Trash"
                  variant="danger"
                  loading={deleting}
                  onPress={confirmDelete}
                  style={{ flex: 1 }}
                />
              </View>
              {retryNote ? (
                <Text style={{ fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim, marginTop: 10 }}>
                  {retryNote}
                </Text>
              ) : null}
            </View>
          ) : null}

          {/* ---- Still processing (real backend status, not string-length guessing) ---- */}
          {!failed && stillProcessing ? (
            <StillWriting
              st={st} C={C} stage={stage}
              stuck={likelyStuck}
              canRetry={REPROCESSABLE_STATUSES.has(String(rec.status ?? ""))}
              retrying={retrying}
              onRetry={retryProcessing}
              retryNote={retryNote}
            />
          ) : null}

          {/* ================= OVERVIEW TAB =================
              The AI's own sections. No hardcoded Summary/Highlights/Decisions
              headings — MeetingOverviewView renders whatever the model
              produced for THIS meeting. MeetingSummary/Highlights below are
              the LEGACY path only, for a recording analysed before the
              dynamic overview shipped. */}
          {!failed && !stillProcessing && hasTranscript && tab === "overview" ? (
            hasAnalysis ? (
              <View style={st.section}>
                {hasOverview(rec.overview) ? (
                  <MeetingOverviewView overview={rec.overview} />
                ) : (
                  <>
                    <MeetingSummary summary={rec.summary} />
                    <Highlights
                      highlights={rec.highlights}
                      legacyHighlights={rec.meeting_highlights}
                    />
                  </>
                )}
                <CrmRecordsBlock
                  mappings={crmMappings}
                  records={rec.crm_records}
                  ambiguity={crmAmbiguity}
                  onSave={setCrmRecord}
                  onChoose={chooseCrmCandidate}
                  onConfirm={confirmCrmRecord}
                  onSync={syncCrmRecordNow}
                />
              </View>
            ) : (
              // The backend has genuinely FINISHED (status is terminal) but the
              // analysis came back empty — a real end state, not "still
              // working". The transcript is safe either way; only the AI
              // analysis is missing.
              <View style={st.section}>
                <View style={st.noticeCard}>
                  <Text style={{ fontFamily: FONT.bold, fontSize: 16, lineHeight: 22, color: C.text }}>
                    No overview for this meeting yet
                  </Text>
                  <Text style={{ fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim, marginTop: 10 }}>
                    The transcript finished, but the AI analysis didn&apos;t come through.
                    Nothing was lost — read the transcript above, or generate a
                    document from it in Documents.
                  </Text>
                  {/* Re-runs the whole pipeline, transcription included. The
                      transcript here is already fine, so this is the heavier
                      option — the cheaper one is generating a document from the
                      existing transcript, which the text above points at first
                      and which costs no STT. */}
                  <Button
                    label="Write the overview again"
                    variant="secondary"
                    loading={retrying}
                    onPress={retryProcessing}
                    style={{ marginTop: 16 }}
                  />
                  {retryNote ? (
                    <Text style={{ fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim, marginTop: 10 }}>
                      {retryNote}
                    </Text>
                  ) : null}
                </View>
                {/* Still offered when the analysis failed: the record
                    identifier is independent of it, and the user may well want
                    to link this meeting to Salesforce anyway. Renders nothing
                    when no mappings are configured. */}
                <CrmRecordsBlock
                  mappings={crmMappings}
                  records={rec.crm_records}
                  ambiguity={crmAmbiguity}
                  onSave={setCrmRecord}
                  onChoose={chooseCrmCandidate}
                  onConfirm={confirmCrmRecord}
                  onSync={syncCrmRecordNow}
                />
              </View>
            )
          ) : null}

          {/* ================= SPEAKERS TAB =================
              The existing Participants block, unchanged, plus talk time
              derived from the transcript's own segments. Renaming still routes
              through the same openRenameSpeaker the transcript screen uses, so
              one mapping drives every surface. */}
          {!failed && !stillProcessing && hasTranscript && tab === "speakers" ? (
            <View style={st.section}>
              <Participants
                participants={rec.participants}
                resolveName={resolveName}
                onRenameSpeaker={openRenameSpeaker}
                talkTime={talkTime}
                onOpenTranscript={() => router.push({
                  pathname: "/recording/[key]/transcript", params: { key },
                })}
              />
            </View>
          ) : null}

          {/* ================= TASKS TAB =================
              The existing Tasks block and its existing routes. Nothing about
              task CRUD changed — only where the block is mounted. */}
          {!failed && !stillProcessing && hasTranscript && tab === "tasks" ? (
            <View style={st.section}>
              <Tasks
                tasks={tasks}
                onOpenTask={(id) => router.push({ pathname: "/recording/[key]/task/[taskId]", params: { key, taskId: id } })}
                onViewAll={() => router.push({ pathname: "/recording/[key]/task", params: { key } })}
                onAddTask={() => setAddTaskOpen(true)}
              />
            </View>
          ) : null}

          {/* ================= DOCUMENTS TAB =================
              The existing DocumentsList and MoM editor entry point, unchanged. */}
          {!failed && !stillProcessing && hasTranscript && tab === "documents" ? (
            <View style={st.section}>
              <DocumentsList
                recordingKey={key}
                meetingTitle={rec.title || "Meeting"}
                documents={documents}
                onChange={setDocuments}
                onCreatePress={() => setCreateOpen(true)}
                documentsNeedingUpdate={documentsNeedingUpdate}
                onUpdateAll={updateAllDocuments}
                onOpenMomEditor={() => setMomOpen(true)}
              />
            </View>
          ) : null}
        </ScrollView>

        {/* ---- Floating Assistant button — visible on both tabs ---- */}
        <AssistantButton recordingKey={key} />

        {/* ---- Create Document sheet, shared entry point ---- */}
        <CreateDocumentSheet
          visible={createOpen}
          recordingKey={key}
          onClose={() => setCreateOpen(false)}
          onGenerated={addDocument}
          onOpenMomEditor={() => setMomOpen(true)}
        />

        {/* ---- Structured Minutes of Meeting editor ----
            addDocument also UPDATES an existing entry by type, so the mirrored
            minutes_of_meeting row in Documents(N) refreshes on every save
            without a re-fetch. */}
        <Modal
          visible={momOpen}
          animationType="slide"
          onRequestClose={() => setMomOpen(false)}
        >
          <MomEditorScreen
            recordingKey={key}
            meetingTitle={rec?.title || "Meeting"}
            onClose={() => setMomOpen(false)}
            onDocumentChange={addDocument}
          />
        </Modal>

        {/* ---- Overflow menu: Rename, Share, Move to Trash ---- */}
        <Modal visible={menuOpen} transparent animationType="fade" onRequestClose={() => setMenuOpen(false)}>
          <Pressable style={st.sheetBackdrop} onPress={() => setMenuOpen(false)}>
            <Pressable style={st.sheet} onPress={() => { }}>
              <Pressable style={st.menuRow} onPress={openRenameTitle} accessibilityLabel="Rename meeting">
                <Icon name="pencil" tintColor={C.text} size={18} />
                <Text style={st.menuTxt}>Rename meeting</Text>
              </Pressable>
              <Pressable style={st.menuRow} onPress={shareMeeting} accessibilityLabel="Share meeting">
                <Icon name="square.and.arrow.up" tintColor={C.text} size={18} />
                <Text style={st.menuTxt}>Share meeting</Text>
              </Pressable>
              {/* Email the minutes through the user's own Gmail. GmailShareRow
                  owns the visibility rule: when Gmail is not connected (or the
                  connection went stale) it renders as an explanatory prompt
                  routing to Settings, never as an action that would fail. */}
              <GmailShareRow
                usable={gmail.usable}
                needsReauth={gmail.needsReauth}
                onPress={() => { setMenuOpen(false); setGmailShareOpen(true); }}
                style={st.menuRow}
              />
              {/* Identify the voices. Mapping a speaker to a contact is also
                  what lets this meeting's AI tasks find a real owner, which is
                  why it sits in the primary menu rather than buried in the
                  transcript. */}
              <Pressable
                style={st.menuRow}
                onPress={() => {
                  setMenuOpen(false);
                  router.push({
                    pathname: "/recording/[key]/participants",
                    params: { key },
                  });
                }}
                accessibilityLabel="Manage participants"
              >
                <Icon name="person.2.fill" tintColor={C.text} size={18} />
                <Text style={st.menuTxt}>Participants &amp; speakers</Text>
              </Pressable>
              {/* Destructive last and coloured, so it is never the row a
                  thumb lands on by accident reaching for Share. */}
              <Pressable
                style={[st.menuRow, { borderBottomWidth: 0 }]}
                onPress={confirmDelete}
                disabled={deleting}
                accessibilityLabel="Move meeting to Trash"
              >
                <Icon name="trash" tintColor={C.danger} size={18} />
                <Text style={[st.menuTxt, { color: C.danger }]}>
                  {deleting ? "Moving…" : "Move to Trash"}
                </Text>
              </Pressable>
            </Pressable>
          </Pressable>
        </Modal>

        {/* ---- Rename meeting title ---- */}
        <Modal visible={renaming} transparent animationType="fade" onRequestClose={() => setRenaming(false)}>
          <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : "height"}>
            <Pressable style={st.sheetBackdrop} onPress={() => setRenaming(false)}>
              <Pressable style={st.sheet} onPress={() => { }}>
                <Text style={{ fontFamily: FONT.semibold, fontSize: 11, color: C.textFaint, textTransform: "uppercase", letterSpacing: 0.6 }}>
                  Meeting title
                </Text>
                <Text style={st.sheetTitle}>Rename this meeting</Text>
                <TextInput
                  style={st.nameInput}
                  value={titleDraft}
                  onChangeText={setTitleDraft}
                  autoFocus
                  maxLength={120}
                  returnKeyType="done"
                  onSubmitEditing={saveTitle}
                  editable={!savingTitle}
                />
                <View style={{ flexDirection: "row", gap: S.sm, marginTop: 22 }}>
                  <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={() => setRenaming(false)} disabled={savingTitle} />
                  <Button label="Save" style={{ flex: 1 }} onPress={saveTitle} loading={savingTitle} />
                </View>
              </Pressable>
            </Pressable>
          </KeyboardAvoidingView>
        </Modal>

        {/* ---- Rename a speaker (opened from the transcript or the
             Participants list — both feed the same openRenameSpeaker) ---- */}
        <Modal visible={editingSpeaker !== null} transparent animationType="fade" onRequestClose={() => setEditingSpeaker(null)}>
          <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : "height"}>
            <Pressable style={st.sheetBackdrop} onPress={() => setEditingSpeaker(null)}>
              <Pressable style={st.sheet} onPress={() => { }}>
                <Text style={{ fontFamily: FONT.semibold, fontSize: 11, color: C.textFaint, textTransform: "uppercase", letterSpacing: 0.6 }}>
                  {editingSpeaker != null ? speakerName(editingSpeaker, speakerNames) : ""}
                </Text>
                <Text style={st.sheetTitle}>Who is this?</Text>

                {/* TAG A CONTACT — the preferred path, offered first.
                    A tagged contact is a real person the system can assign
                    tasks to and notify; a typed name is only a display label.
                    Both are legitimate, so both are here, with the one that
                    does more listed above. */}
                <Pressable
                  style={st.tagRow}
                  onPress={() => setSpeakerPickerOpen(true)}
                  disabled={savingSpeaker}
                  accessibilityRole="button"
                  accessibilityLabel={
                    taggedContact
                      ? `Tagged as ${taggedContact.name}. Change contact`
                      : "Tag a contact"
                  }
                >
                  {taggedContact ? (
                    <>
                      <View style={[st.tagAvatar, { backgroundColor: avatarColorFor(taggedContact.name) }]}>
                        <Text style={st.tagAvatarTxt}>{initialsOf(taggedContact.name)}</Text>
                      </View>
                      <View style={{ flex: 1 }}>
                        <Text style={st.tagName}>{taggedContact.name}</Text>
                        <Text style={st.tagHint}>
                          Tagged — tasks can be assigned to them
                        </Text>
                      </View>
                      <Text style={st.tagAction}>Change</Text>
                    </>
                  ) : (
                    <>
                      <View style={[st.tagAvatar, { backgroundColor: C.primarySoft }]}>
                        <Icon name="person.2.fill" size={17} tintColor={C.primary} />
                      </View>
                      <View style={{ flex: 1 }}>
                        <Text style={st.tagName}>Tag a contact</Text>
                        <Text style={st.tagHint}>
                          So tasks can be assigned to this person
                        </Text>
                      </View>
                      <Icon name="chevron.right" size={15} tintColor={C.textFaint} />
                    </>
                  )}
                </Pressable>

                <Text style={st.orRule}>or just set a name</Text>

                <TextInput
                  style={st.nameInput}
                  value={speakerDraft}
                  onChangeText={setSpeakerDraft}
                  placeholder="Their name"
                  placeholderTextColor={C.textFaint}
                  autoCapitalize="words"
                  maxLength={60}
                  returnKeyType="done"
                  onSubmitEditing={saveSpeaker}
                />
                <Text style={{ fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 8 }}>
                  Updates the transcript and participants immediately. Generated
                  documents that mention this speaker will be flagged for an update.
                </Text>
                <View style={{ flexDirection: "row", gap: S.sm, marginTop: 22 }}>
                  <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={() => setEditingSpeaker(null)} disabled={savingSpeaker} />
                  <Button label="Save" style={{ flex: 1 }} onPress={saveSpeaker} loading={savingSpeaker} />
                </View>
              </Pressable>
            </Pressable>
          </KeyboardAvoidingView>
        </Modal>

        {/* The contact picker for speaker tagging. Mounted as its own Modal
            OUTSIDE the rename sheet: nesting one Modal inside another is
            unreliable on Android (the inner one can render behind), and this
            way the rename sheet stays open underneath so cancelling the picker
            returns to it rather than losing the user's place.
            Ordering: people already tagged in this meeting, then everyone
            — plus create-new and phone import. */}
        <AddTaskSheet
          visible={addTaskOpen}
          onClose={() => setAddTaskOpen(false)}
          recordingKey={key}
          onSubmit={addTask}
        />

        {/* Share as a public read-only link. Mounted out here, as a sibling of
            the overflow menu rather than inside it, for the same Android
            reason the contact picker is: a Modal nested in a Modal can render
            behind its parent. */}
        <ShareMeetingSheet
          visible={shareOpen}
          onClose={() => setShareOpen(false)}
          recordingKey={key}
          meetingTitle={rec?.title || "Meeting"}
        />

        {/* Gmail send. A sibling of the overflow menu for the same Android
            reason as the share sheet above: a Modal nested in a Modal can
            render behind its parent. */}
        <GmailShareSheet
          visible={gmailShareOpen}
          onClose={() => setGmailShareOpen(false)}
          recordingKey={key}
          meetingTitle={rec?.title || "Meeting"}
          // MeetingProvider already loaded these for the Documents tab, so
          // passing them saves the sheet a redundant round trip — and means
          // the share list matches the Documents tab exactly.
          documents={documents}
          onSent={(count) => Alert.alert(
            "Sent",
            `Your email was sent to ${count} ${count === 1 ? "person" : "people"}.`)}
        />

        <ContactPicker
          visible={speakerPickerOpen}
          onClose={() => setSpeakerPickerOpen(false)}
          onPick={(c) => {
            setSpeakerPickerOpen(false);
            void tagSpeakerContact(c);
          }}
          meetingContacts={speakerContacts}
          title={
            editingSpeaker != null
              ? `Who is Speaker ${editingSpeaker}?`
              : "Select Contact"
          }
        />
      </View>
    </AudioPlayerProvider>
  );
}
