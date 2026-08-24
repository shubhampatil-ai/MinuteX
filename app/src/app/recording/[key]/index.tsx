// src/app/recording/[key]/index.tsx — Meeting Detail: Overview + Transcript.
//
// MinuteX is an AI Meeting Workspace, not a printed brief and not a scroll of
// every AI surface the backend can produce. Two tabs, an animated segmented
// control, and exactly one entry point per capability:
//
//   Overview tab:    Summary (one AI note, Read more, Share)
//                       -> Highlights (4-5 icon-assisted rows)
//                       -> Tasks (top 3 + View all -> Task Detail)
//                       -> Documents (list + Create Document)
//   Transcript tab:  Search, speaker labels/colors, timestamps, tap-to-seek.
//                       No AI features here at all.
//
// Shared: title (tap to rename), a large Apple-Music-style audio player,
// overflow menu (share/rename), speaker rename from the transcript, and a
// floating Assistant button that opens the dedicated workspace at
// recording/[key]/assistant — chat, generated documents and tasks all live
// there now, not inline on this screen.
//
// MeetingProvider is mounted by the parent recording/[key]/_layout.tsx, not
// here — this screen, Assistant, Task Detail, Assign To and Notify all share
// that one instance so task/document state survives navigating between them.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator, Alert, KeyboardAvoidingView, Modal, Platform, Pressable,
  ScrollView, Share, StyleSheet, Text, TextInput, View,
} from "react-native";
import { Stack, useRouter } from "expo-router";
import { Icon } from "../../../../lib/icons";
import { S, R, FONT, TABULAR, useTheme, ColorScale } from "../../../../lib/theme";
import { Button, SegmentedTabs, Skeleton } from "../../../../lib/ui";
import { AudioPlayer, AudioPlayerProvider, useAudioSeek } from "../../../../lib/audio-player";
import { useMeeting } from "../../../../lib/meeting-context";
import { sourceMeta, statusMeta, speakerName, normalizeSpeakerLabel, fmtDuration } from "../../../../lib/sources";
import { MeetingSummary, Highlights, Participants } from "../../../../lib/meeting-summary";
import { CrmRecordsBlock } from "../../../../lib/meeting-crm-records";
import { Tasks } from "../../../../lib/meeting-tasks";
import { AddTaskSheet } from "../../../../lib/add-task-sheet";
import { DocumentsList, CreateDocumentSheet } from "../../../../lib/meeting-documents";
import { AssistantButton } from "../../../../lib/assistant-button";
import { ContactPicker } from "../../../../lib/contact-picker";
import { avatarColorFor, initialsOf } from "../../../../lib/task-model";
import {
  ApiError, isAlreadyRunning, isStillUploading, reprocessRecording, trashRecording,
  ApiFolder, getFolders, moveRecordingToFolder,
  ApiContact, getParticipants, setParticipant,
} from "../../../../lib/api";

type Tab = "overview" | "transcript";

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
    // ---- transcript ----
    searchBar: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 9,
      backgroundColor: C.surface2, borderRadius: R.pill,
      paddingHorizontal: S.md, paddingVertical: 10, marginTop: S.lg,
    },
    searchInput: { flex: 1, fontFamily: FONT.regular, fontSize: 14.5, color: C.text, paddingVertical: 4 },
    empty: { fontFamily: FONT.regular, fontSize: 13.5, color: C.textFaint, fontStyle: "italic" as const, marginTop: 16 },
  });
}

// A recording is "probably stuck", not merely slow, once it has sat in a
// non-terminal status for this long. The pipeline normally finishes in ~2
// minutes; the stranded rows that motivated scripts/26_reprocess_stuck.py sat
// at "transcribing"/"generating_ai" forever, and the app polled them with no
// way out. Generous on purpose — offering "try again" to a run that is merely
// slow would spend money re-doing work that was about to succeed.
const LIKELY_STUCK_AFTER_MS = 15 * 60 * 1000;

function StillWriting({
  st, C, stage, stuck, retrying, onRetry, retryNote,
}: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  stage: number;
  stuck: boolean;
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
          ? "This is taking much longer than usual. It may have stalled — you can start it over."
          : "Usually done in about two minutes. You can leave — it'll be here when it's ready."}
      </Text>
      {stuck ? (
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

function fmtTs(sec: number) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

const SPEAKER_LABEL_HIT = 6;

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
  // Folder filing. The list is fetched when the picker opens rather than on
  // mount: most visits to a meeting never touch it, and it is one more request
  // on a screen that already makes several.
  const [folderPickerOpen, setFolderPickerOpen] = useState(false);
  const [folderOptions, setFolderOptions] = useState<ApiFolder[]>([]);
  const [movingFolder, setMovingFolder] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);

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
  // no longer on the Desk, and re-fetching would only show a meeting the user
  // just filed away. The Desk re-queries on focus, so it is already gone by
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
  // The folder this meeting is filed under, resolved for display. Falls back to
  // "" (General) when unset or when the folder list has not been fetched yet.
  const currentFolderName = useMemo(() => {
    const fid = (rec as any)?.folder_id || "";
    if (!fid) return "";
    return folderOptions.find((f) => f.id === fid)?.name || "";
  }, [rec, folderOptions]);

  // Fetch the folder list only when the picker is actually opened.
  useEffect(() => {
    if (!folderPickerOpen) return;
    getFolders()
      .then((r) => setFolderOptions(r.folders))
      .catch(() => setFolderOptions([]));
  }, [folderPickerOpen]);

  const moveToFolder = useCallback(
    async (folderId: string | null) => {
      setMovingFolder(true);
      try {
        await moveRecordingToFolder(key, folderId);
        setFolderPickerOpen(false);
        // Re-read the meeting so the menu label and any folder-derived UI
        // reflect the move without a manual refresh.
        reload();
      } catch (e) {
        Alert.alert(
          "Could not move",
          e instanceof ApiError ? e.message : "Please try again."
        );
      } finally {
        setMovingFolder(false);
      }
    },
    [key, reload]
  );

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

  const shareMeeting = async () => {
    setMenuOpen(false);
    try {
      await Share.share({ message: rec?.title || "Meeting", title: rec?.title || "Meeting" });
    } catch {
      // user cancelled
    }
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
  const [speakerFolderContacts, setSpeakerFolderContacts] = useState<ApiContact[]>([]);
  const [taggedContact, setTaggedContact] = useState<ApiContact | null>(null);

  const openRenameSpeaker = (label: string) => {
    const raw = normalizeSpeakerLabel(label);
    setEditingSpeaker(raw);
    setSpeakerDraft(speakerNames?.[raw] ?? "");
    setTaggedContact(null);
    // Load who is already tagged in this meeting (and its folder) so the
    // picker can rank them first. Best-effort: a failure only affects
    // ORDERING, never whether the sheet works.
    void getParticipants(String(key))
      .then((p) => {
        setSpeakerContacts(
          p.participants
            .map((x) => x.contact)
            .filter((c): c is ApiContact => !!c)
        );
        setSpeakerFolderContacts(p.folder_contacts);
        // Show what this speaker is CURRENTLY tagged as, so the sheet reflects
        // reality rather than looking untagged every time it opens.
        const mine = p.participants.find((x) => x.speaker_id === raw);
        if (mine?.contact) setTaggedContact(mine.contact);
      })
      .catch(() => {
        setSpeakerContacts([]);
        setSpeakerFolderContacts([]);
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

  const speakerColors = useMemo(() => {
    const map = new Map<string, string>();
    (rec?.timestamps ?? []).forEach((seg) => {
      if (!map.has(seg.speaker)) map.set(seg.speaker, C.speakers[map.size % C.speakers.length]);
    });
    return map;
  }, [rec, C]);

  const speakerBlocks = useMemo(() => {
    const segs = rec?.timestamps ?? [];
    const blocks: { speaker: string; start: number; end: number; texts: string[] }[] = [];
    for (const s of segs) {
      const last = blocks[blocks.length - 1];
      if (last && last.speaker === s.speaker) {
        last.texts.push(s.text);
        last.end = Number(s.end) || last.end;
      } else {
        blocks.push({ speaker: s.speaker, start: Number(s.start) || 0, end: Number(s.end) || 0, texts: [s.text] });
      }
    }
    return blocks;
  }, [rec]);

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

  const stage = rec.transcript ? (rec.summary ? 2 : 1) : 0;

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
          </View>

          {showTabs ? (
            <View style={{ backgroundColor: C.bg, paddingTop: S.lg, paddingBottom: S.sm }}>
              <SegmentedTabs
                value={tab}
                onChange={(v) => setTab(v as Tab)}
                tabs={[
                  { key: "overview", label: "Overview" },
                  { key: "transcript", label: "Transcript" },
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
                  only be left on the desk forever. Trash keeps the audio, so
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
              retrying={retrying}
              onRetry={retryProcessing}
              retryNote={retryNote}
            />
          ) : null}

          {/* ================= OVERVIEW TAB ================= */}
          {!failed && !stillProcessing && hasTranscript && tab === "overview" ? (
            rec.summary ? (
              <View style={st.section}>
                <MeetingSummary summary={rec.summary} />
                <CrmRecordsBlock
                  mappings={crmMappings}
                  records={rec.crm_records}
                  ambiguity={crmAmbiguity}
                  onSave={setCrmRecord}
                  onChoose={chooseCrmCandidate}
                  onConfirm={confirmCrmRecord}
                  onSync={syncCrmRecordNow}
                />
                <Highlights highlights={rec.highlights} legacyHighlights={rec.meeting_highlights} />
                <Participants
                  participants={rec.participants}
                  resolveName={resolveName}
                  onRenameSpeaker={openRenameSpeaker}
                />
                <Tasks
                  tasks={tasks}
                  onOpenTask={(id) => router.push({ pathname: "/recording/[key]/task/[taskId]", params: { key, taskId: id } })}
                  onViewAll={() => router.push({ pathname: "/recording/[key]/task", params: { key } })}
                  onAddTask={() => setAddTaskOpen(true)}
                />
                <DocumentsList
                  recordingKey={key}
                  meetingTitle={rec.title || "Meeting"}
                  documents={documents}
                  onChange={setDocuments}
                  onCreatePress={() => setCreateOpen(true)}
                  documentsNeedingUpdate={documentsNeedingUpdate}
                  onUpdateAll={updateAllDocuments}
                />
              </View>
            ) : (
              // Backend has genuinely finished (status is "ready") but Groq's
              // summarization came back empty — a real, terminal state, not a
              // "still working" one. The transcript is safe either way; only
              // the AI summary is missing.
              <View style={st.section}>
                <View style={st.noticeCard}>
                  <Text style={{ fontFamily: FONT.bold, fontSize: 16, lineHeight: 22, color: C.text }}>
                    No summary for this meeting yet
                  </Text>
                  <Text style={{ fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim, marginTop: 10 }}>
                    The transcript finished, but the AI summary didn&apos;t come through. Nothing was lost —
                    check the Transcript tab, or try generating a document from it below.
                  </Text>
                  {/* Re-runs the whole pipeline, including transcription. The
                      transcript here is already fine, so this is the heavier
                      option — the cheaper one is generating a document from
                      the existing transcript, which the text above points at
                      first and which costs no STT. */}
                  <Button
                    label="Write the summary again"
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
                {/* Still offered when the summary failed: the record
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
                <Participants
                  participants={rec.participants}
                  resolveName={resolveName}
                  onRenameSpeaker={openRenameSpeaker}
                />
                <Tasks
                  tasks={tasks}
                  onOpenTask={(id) => router.push({ pathname: "/recording/[key]/task/[taskId]", params: { key, taskId: id } })}
                  onViewAll={() => router.push({ pathname: "/recording/[key]/task", params: { key } })}
                  onAddTask={() => setAddTaskOpen(true)}
                />
                <DocumentsList
                  recordingKey={key}
                  meetingTitle={rec.title || "Meeting"}
                  documents={documents}
                  onChange={setDocuments}
                  onCreatePress={() => setCreateOpen(true)}
                  documentsNeedingUpdate={documentsNeedingUpdate}
                  onUpdateAll={updateAllDocuments}
                />
              </View>
            )
          ) : null}

          {/* ================= TRANSCRIPT TAB ================= */}
          {!failed && hasTranscript && tab === "transcript" ? (
            <TranscriptTab
              st={st} C={C}
              transcript={rec.transcript}
              speakerBlocks={speakerBlocks}
              speakerColors={speakerColors}
              resolveName={resolveName}
              onRenameSpeaker={openRenameSpeaker}
              search={search}
              onSearchChange={setSearch}
            />
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
        />

        {/* ---- Overflow menu: Rename, Share, Move to Trash ---- */}
        <Modal visible={menuOpen} transparent animationType="fade" onRequestClose={() => setMenuOpen(false)}>
          <Pressable style={st.sheetBackdrop} onPress={() => setMenuOpen(false)}>
            <Pressable style={st.sheet} onPress={() => {}}>
              <Pressable style={st.menuRow} onPress={openRenameTitle} accessibilityLabel="Rename meeting">
                <Icon name="pencil" tintColor={C.text} size={18} />
                <Text style={st.menuTxt}>Rename meeting</Text>
              </Pressable>
              <Pressable style={st.menuRow} onPress={shareMeeting} accessibilityLabel="Share meeting">
                <Icon name="square.and.arrow.up" tintColor={C.text} size={18} />
                <Text style={st.menuTxt}>Share meeting</Text>
              </Pressable>
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
              {/* Filing the meeting. Never a copy — this rewrites one field. */}
              <Pressable
                style={st.menuRow}
                onPress={() => {
                  setMenuOpen(false);
                  setFolderPickerOpen(true);
                }}
                accessibilityLabel="Move meeting to a folder"
              >
                <Icon name="folder" tintColor={C.text} size={18} />
                <Text style={st.menuTxt}>
                  {currentFolderName ? `Folder: ${currentFolderName}` : "Move to folder"}
                </Text>
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

        {/* ---- Move to folder ----
            One list, one tap. "General" is offered as the first row rather
            than as a separate "remove" action, because to the user it IS a
            destination — it just happens to be represented server-side by the
            absence of a folder. */}
        <Modal
          visible={folderPickerOpen}
          transparent
          animationType="fade"
          onRequestClose={() => setFolderPickerOpen(false)}
        >
          <Pressable
            style={st.sheetBackdrop}
            onPress={() => setFolderPickerOpen(false)}
          >
            <Pressable style={st.sheet} onPress={() => {}}>
              <Text style={{
                fontFamily: FONT.semibold, fontSize: 11, color: C.textFaint,
                textTransform: "uppercase", letterSpacing: 0.6,
              }}>
                Organize
              </Text>
              <Text style={st.sheetTitle}>Move to folder</Text>
              <Text style={{
                fontFamily: FONT.regular, fontSize: 13, color: C.textDim,
                marginTop: 4, marginBottom: 10, lineHeight: 19,
              }}>
                The meeting moves — it is never copied. Its tasks follow it.
              </Text>

              <Pressable
                style={st.menuRow}
                onPress={() => moveToFolder(null)}
                disabled={movingFolder}
                accessibilityLabel="Move to General"
              >
                <Icon name="tray.fill" tintColor={C.text} size={18} />
                <Text style={[st.menuTxt, { flex: 1 }]}>General (no folder)</Text>
                {!(rec as any)?.folder_id && (
                  <Icon name="checkmark" tintColor={C.primary} size={16} />
                )}
              </Pressable>

              {folderOptions.map((f, i) => {
                const active = (rec as any)?.folder_id === f.id;
                return (
                  <Pressable
                    key={f.id}
                    style={[
                      st.menuRow,
                      i === folderOptions.length - 1 && { borderBottomWidth: 0 },
                    ]}
                    onPress={() => moveToFolder(f.id)}
                    disabled={movingFolder || active}
                    accessibilityLabel={`Move to ${f.name}`}
                  >
                    <Icon name="folder" tintColor={C.text} size={18} />
                    <Text style={[st.menuTxt, { flex: 1 }]}>{f.name}</Text>
                    {active && (
                      <Icon name="checkmark" tintColor={C.primary} size={16} />
                    )}
                  </Pressable>
                );
              })}

              {folderOptions.length === 0 && (
                <Text style={{
                  fontFamily: FONT.regular, fontSize: 13, color: C.textFaint,
                  paddingVertical: 14,
                }}>
                  No folders yet. Create one from You › Folders.
                </Text>
              )}
            </Pressable>
          </Pressable>
        </Modal>

        {/* ---- Rename meeting title ---- */}
        <Modal visible={renaming} transparent animationType="fade" onRequestClose={() => setRenaming(false)}>
          <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : "height"}>
            <Pressable style={st.sheetBackdrop} onPress={() => setRenaming(false)}>
              <Pressable style={st.sheet} onPress={() => {}}>
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
              <Pressable style={st.sheet} onPress={() => {}}>
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
            Ordering: people already tagged in this meeting, then the folder's
            contacts, then everyone — plus create-new and phone import. */}
        <AddTaskSheet
          visible={addTaskOpen}
          onClose={() => setAddTaskOpen(false)}
          recordingKey={key}
          onSubmit={addTask}
        />

        <ContactPicker
          visible={speakerPickerOpen}
          onClose={() => setSpeakerPickerOpen(false)}
          onPick={(c) => {
            setSpeakerPickerOpen(false);
            void tagSpeakerContact(c);
          }}
          meetingContacts={speakerContacts}
          folderContacts={speakerFolderContacts}
          folderId={rec?.folder_id || undefined}
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

type SpeakerBlock = { speaker: string; start: number; end: number; texts: string[] };

function TranscriptTab({
  st, C, transcript, speakerBlocks, speakerColors, resolveName, onRenameSpeaker,
  search, onSearchChange,
}: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  transcript: string;
  speakerBlocks: SpeakerBlock[];
  speakerColors: Map<string, string>;
  resolveName: (label: string) => string;
  onRenameSpeaker: (label: string) => void;
  search: string;
  onSearchChange: (v: string) => void;
}) {
  const { seekTo, currentTime, playing } = useAudioSeek();
  const q = search.trim().toLowerCase();
  const filteredBlocks = q
    ? speakerBlocks.filter((b) =>
        b.texts.join(" ").toLowerCase().includes(q) ||
        resolveName(b.speaker).toLowerCase().includes(q))
    : speakerBlocks;

  return (
    <View>
      {speakerBlocks.length ? (
        <View style={st.searchBar}>
          <Icon name="magnifyingglass" tintColor={C.textFaint} size={16} />
          <TextInput
            style={st.searchInput}
            value={search}
            onChangeText={onSearchChange}
            placeholder="Search transcript"
            placeholderTextColor={C.textFaint}
            autoCapitalize="none"
            autoCorrect={false}
            returnKeyType="search"
          />
          {search ? (
            <Pressable onPress={() => onSearchChange("")} hitSlop={8} accessibilityLabel="Clear search">
              <Icon name="xmark.circle.fill" tintColor={C.textFaint} size={16} />
            </Pressable>
          ) : null}
        </View>
      ) : null}

      {q && !filteredBlocks.length ? (
        <Text style={st.empty}>Nothing in the transcript matches &quot;{search}&quot;.</Text>
      ) : null}

      {speakerBlocks.length ? (
        filteredBlocks.map((b, i) => {
          const color = speakerColors.get(b.speaker) ?? C.speakers[0];
          const name = resolveName(b.speaker);
          const isNamed = name !== `Speaker ${b.speaker}`;
          const active = playing && currentTime >= b.start && currentTime < b.end;
          return (
            <View key={i} style={{ flexDirection: "row", gap: 12, marginTop: 18 }}>
              <View style={{ width: 44, flexShrink: 0, alignItems: "flex-end" }}>
                {seekTo ? (
                  <Pressable onPress={() => seekTo(b.start)} hitSlop={8} accessibilityLabel={`Play from ${fmtTs(b.start)}`}>
                    <Text style={{ ...TABULAR, fontSize: 10.5, color: C.primary }}>
                      {fmtTs(b.start)}
                    </Text>
                  </Pressable>
                ) : (
                  <Text style={{ ...TABULAR, fontSize: 10.5, color: C.textFaint }}>{fmtTs(b.start)}</Text>
                )}
              </View>
              <View style={{
                flex: 1, backgroundColor: active ? C.primarySoft : "transparent",
                borderRadius: R.md, padding: active ? 10 : 0,
              }}>
                <Pressable
                  onPress={() => onRenameSpeaker(b.speaker)}
                  hitSlop={SPEAKER_LABEL_HIT}
                  style={{ flexDirection: "row", alignItems: "center", gap: 6, alignSelf: "flex-start" }}
                  accessibilityLabel={`Rename ${name}`}
                >
                  <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: color }} />
                  <Text style={{ fontFamily: FONT.bold, fontSize: 12.5, color }}>
                    {name}
                  </Text>
                  {!isNamed ? <Icon name="pencil" tintColor={C.textFaint} size={11} /> : null}
                </Pressable>
                <Text style={{ fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: C.text, marginTop: 5 }}>
                  {b.texts.join(" ")}
                </Text>
              </View>
            </View>
          );
        })
      ) : (
        <Text style={{ fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: C.text, marginTop: S.lg }}>
          {transcript}
        </Text>
      )}
    </View>
  );
}
