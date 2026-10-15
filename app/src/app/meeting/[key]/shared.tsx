// src/app/meeting/[key]/shared.tsx — a meeting, READ-ONLY, for someone who
// is not its recorder: a participant the recorder gave access to, or a task
// assignee.
//
// WHAT EACH VIEWER GETS is decided by the backend, never here:
//   participant, level "notes" -> notes only
//   participant, level "full"  -> notes + transcript + audio (still read-only)
//   assignee                   -> notes only
// "Full" is never a write permission. This screen renders what it is sent.
//
// WHY THIS IS A SEPARATE SCREEN AND NOT A FLAG ON THE REAL ONE.
//
// recording/[key]/index.tsx is the owner's meeting: it mounts MeetingProvider,
// fetches the full recording, presents an audio player, four AI tabs, a
// floating Assistant button, rename, speaker mapping, task
// creation and share-link management. Threading an `isReadOnly` flag through
// all of that would leave every one of those write paths one bad conditional
// away from an assignee — and the conditional that matters would be in the
// component that someone adds NEXT year, not in the ones reviewed today.
//
// This screen instead has no way to write. It calls exactly one endpoint,
// which is itself read-only, and imports no mutation helper. The security
// property is structural rather than conditional: there is no branch to get
// wrong, because there is no code here that could mutate anything.
//
// It lives under /meeting/ rather than /recording/ for the same reason — the
// recording/[key]/ directory has a _layout.tsx that mounts MeetingProvider for
// every screen beneath it, and that provider fetches the owner-only recording
// route. A read-only screen nested there would fire a request guaranteed to
// 404 for the very users this screen exists for.
//
// THE BACKEND IS THE ENFORCEMENT POINT. Everything here assumes the response
// is already filtered — no transcript or audio below "full", never an S3 key.
// This file never decides what a viewer may see; it renders what it is given.
import { useCallback, useEffect, useState } from "react";
import { RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useLocalSearchParams } from "expo-router";
import { S, R, FONT, useTheme, ColorScale } from "../../../../lib/theme";
import { Button, Card, Loading } from "../../../../lib/ui";
import { Icon } from "../../../../lib/icons";
import { MeetingOverviewView, hasOverview } from "../../../../lib/meeting-overview";
import { fmtDuration } from "../../../../lib/sources";
import { AudioPlayer } from "../../../../lib/audio-player";
import { fmtTs } from "../../../../lib/transcript-view";
import {
  getSharedMeeting, ApiError,
  type SharedMeeting, type SharedMeetingSection, type SharedMeetingResponse,
} from "../../../../lib/api";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    content: { padding: 20, paddingBottom: 48, gap: S.lg },
    center: {
      flex: 1, alignItems: "center" as const, justifyContent: "center" as const,
      gap: S.md, padding: 32,
    },
    title: {
      fontFamily: FONT.bold, fontSize: 22, lineHeight: 29, color: C.text,
    },
    metaRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      flexWrap: "wrap" as const, gap: S.sm, marginTop: S.xs,
    },
    meta: { fontFamily: FONT.medium, fontSize: 13, color: C.textDim },
    dot: { fontFamily: FONT.medium, fontSize: 13, color: C.textFaint },
    // The read-only banner. Deliberately stated rather than implied by the
    // absence of buttons: an assignee who does not know this is a limited view
    // reads "no transcript here" as the product being broken, and goes and
    // asks the owner — which is the exact interruption the feature removes.
    banner: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.md, padding: S.lg, borderRadius: R.lg,
      backgroundColor: C.primarySoft ?? C.surface,
      borderWidth: StyleSheet.hairlineWidth, borderColor: C.border,
    },
    bannerTitle: {
      fontFamily: FONT.semibold, fontSize: 13.5, color: C.text, marginBottom: 2,
    },
    bannerBody: {
      fontFamily: FONT.medium, fontSize: 12.5, lineHeight: 18, color: C.textDim,
    },
    sectionTitle: {
      fontFamily: FONT.semibold, fontSize: 15, color: C.text, marginBottom: S.sm,
    },
    text: {
      fontFamily: FONT.medium, fontSize: 14.5, lineHeight: 21, color: C.text,
    },
    itemRow: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.md, paddingVertical: 7,
    },
    bullet: {
      width: 6, height: 6, borderRadius: 3, backgroundColor: C.primary,
      marginTop: 8,
    },
    itemTxt: {
      fontFamily: FONT.medium, fontSize: 14.5, lineHeight: 21, color: C.text,
      flex: 1,
    },
    fieldRow: {
      flexDirection: "row" as const, gap: S.md, paddingVertical: 6,
      alignItems: "flex-start" as const,
    },
    // A RANGE, not a hard width. `f.label` is a server-supplied MoM field
    // label: at a fixed 110 a long one wrapped to four or five lines inside
    // its box while the value beside it stayed on one, so the row's height
    // was driven by the label rather than the content. The range keeps the
    // labels optically aligned in the common case and lets an unusual one
    // take the room it needs.
    fieldLabel: {
      fontFamily: FONT.semibold, fontSize: 13.5, color: C.textDim,
      minWidth: 96, maxWidth: 140, flexShrink: 1,
    },
    fieldValue: {
      fontFamily: FONT.medium, fontSize: 14, lineHeight: 20, color: C.text,
      flex: 1,
    },
    // Tables scroll horizontally rather than squeezing columns to nothing —
    // a MoM table can carry five columns and a phone is 390pt wide.
    tableRow: { flexDirection: "row" as const, gap: S.lg },
    tableCell: {
      fontFamily: FONT.medium, fontSize: 13.5, lineHeight: 19, color: C.text,
      minWidth: 110,
    },
    tableHead: { fontFamily: FONT.semibold, color: C.textDim },
    empty: {
      fontFamily: FONT.medium, fontSize: 14, lineHeight: 21, color: C.textDim,
      textAlign: "center" as const,
    },
    // Read-only transcript: speaker, timecode, text. No rename, no seek — the
    // owner's TranscriptView carries a rename affordance this screen must not.
    block: { paddingVertical: S.sm, gap: 3 },
    blockHead: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
    },
    speakerDot: { width: 8, height: 8, borderRadius: 4 },
    speakerName: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.text },
    timecode: { fontFamily: FONT.medium, fontSize: 12, color: C.textFaint },
  });
}

/** Banner copy per access. Stated rather than implied by missing buttons, so
 *  a notes-only viewer does not read "no transcript" as a broken product. */
function bannerFor(res: Pick<SharedMeetingResponse, "access" | "level" | "shared_by_name">) {
  const by = res.shared_by_name?.trim();
  const title = by ? `Shared with you by ${by}` : "Shared with you";
  if (res.access === "participant" && res.level === "full") {
    return {
      title,
      body: "You can read this meeting\u2019s notes and transcript and play the recording. It\u2019s read-only: only the recorder can make changes.",
    };
  }
  if (res.access === "participant") {
    return {
      title,
      body: "You were tagged in this meeting, so you can read its notes. The recording and full transcript stay with the recorder.",
    };
  }
  return {
    title,
    body: "You can read this meeting\u2019s notes because a task from it is assigned to you. The recording and full transcript stay with the meeting\u2019s owner.",
  };
}

// ---------------------------------------------------------------------------
// MoM sections. The backend sends the AI Overview when it has one and the MoM
// structure otherwise (never both — the payload builder guarantees that, so
// the same content cannot appear twice under two sets of headings). Overview
// rendering is reused from lib/meeting-overview.tsx; the four MoM kinds are
// rendered below, because that shape has no existing read-only renderer in
// the app — the MoM editor's components are all editable.
// ---------------------------------------------------------------------------
function MomSection({
  section, st, C,
}: {
  section: SharedMeetingSection;
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
}) {
  const body = () => {
    switch (section.kind) {
      case "text":
        return section.text?.trim()
          ? <Text style={st.text}>{section.text.trim()}</Text>
          : null;

      case "list":
        return (section.items ?? []).length ? (
          <View>
            {(section.items ?? []).map((item, i) => (
              <View key={i} style={st.itemRow}>
                <View style={st.bullet} />
                <Text style={st.itemTxt}>{item}</Text>
              </View>
            ))}
          </View>
        ) : null;

      case "fields":
        return (section.fields ?? []).length ? (
          <View>
            {(section.fields ?? []).map((f, i) => (
              <View key={i} style={st.fieldRow}>
                <Text style={st.fieldLabel}>{f.label}</Text>
                <Text style={st.fieldValue}>{f.value}</Text>
              </View>
            ))}
          </View>
        ) : null;

      case "table": {
        const columns = section.columns ?? [];
        const rows = section.rows ?? [];
        if (!rows.length) return null;
        return (
          <ScrollView horizontal showsHorizontalScrollIndicator={false}>
            <View style={{ gap: 6 }}>
              {!!columns.length && (
                <View style={st.tableRow}>
                  {columns.map((c, i) => (
                    <Text key={i} style={[st.tableCell, st.tableHead]}>{c}</Text>
                  ))}
                </View>
              )}
              {rows.map((row, ri) => (
                <View key={ri} style={st.tableRow}>
                  {row.map((cell, ci) => (
                    <Text key={ci} style={st.tableCell}>{cell}</Text>
                  ))}
                </View>
              ))}
            </View>
          </ScrollView>
        );
      }

      default:
        // An unrecognised kind renders NOTHING rather than a raw dump. A new
        // MoM kind added server-side must get a branch here deliberately.
        return null;
    }
  };

  const content = body();
  if (!content) return null;
  return (
    <Card>
      {!!section.title && <Text style={st.sectionTitle}>{section.title}</Text>}
      {content}
    </Card>
  );
}

export default function SharedMeetingScreen() {
  const { C } = useTheme();
  const st = buildStyles(C);
  const params = useLocalSearchParams<{ key: string | string[] }>();
  // The greedy key arrives split on "/" when it contains slashes, exactly as
  // the recording screens handle it.
  const key = Array.isArray(params.key) ? params.key.join("/") : (params.key ?? "");

  const [meeting, setMeeting] = useState<SharedMeeting | null>(null);
  const [viewer, setViewer] = useState<
    Pick<SharedMeetingResponse, "access" | "level" | "shared_by_name">
  >({ access: "assignee" });
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setError("");
      const res = await getSharedMeeting(key);
      setMeeting(res.meeting);
      setViewer({ access: res.access, level: res.level,
                  shared_by_name: res.shared_by_name });
    } catch (e) {
      // A 404 here is the ordinary consequence of losing access — the
      // recorder removed it, the task was reassigned, or the meeting went to
      // Trash — not a fault. Said plainly, because "Something went wrong"
      // would send the user to support for a system working as designed.
      setError(
        e instanceof ApiError && e.status === 404
          ? "This meeting is no longer shared with you. The recorder may have removed your access, or the meeting was deleted."
          : "Couldn't load this meeting. Please try again."
      );
      setMeeting(null);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [key]);

  useEffect(() => { load(); }, [load]);

  const onRefresh = useCallback(() => {
    setRefreshing(true);
    load();
  }, [load]);

  if (loading) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title: "Meeting" }} />
        <Loading label="Loading meeting" />
      </View>
    );
  }

  if (error || !meeting) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title: "Meeting" }} />
        <View style={st.center}>
          <Icon name="waveform" size={28} tintColor={C.textFaint} />
          <Text style={st.empty}>{error || "Meeting not available."}</Text>
          <Button label="Retry" variant="secondary" onPress={() => load()} />
        </View>
      </View>
    );
  }

  const date = meeting.recorded_at
    ? new Date(meeting.recorded_at).toLocaleDateString(undefined, {
        day: "numeric", month: "short", year: "numeric",
      })
    : "";
  // fmtDuration already maps null/undefined to "", so the null the payload
  // carries for a recording with no known duration needs no special case.
  const duration = fmtDuration(meeting.duration);
  const showOverview = hasOverview({ sections: meeting.overview ?? [] });
  const momSections = meeting.sections ?? [];
  const hasBody = showOverview || momSections.length > 0;
  const banner = bannerFor(viewer);
  // Present only when the server sent them — i.e. at level "full".
  const blocks = meeting.speaker_blocks ?? [];
  const plainTranscript = !blocks.length ? (meeting.transcript ?? "").trim() : "";

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Meeting" }} />
      <ScrollView
        contentContainerStyle={st.content}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={onRefresh}
                          tintColor={C.textDim} />
        }
      >
        <View>
          <Text style={st.title}>{meeting.title || "Untitled meeting"}</Text>
          <View style={st.metaRow}>
            {!!date && <Text style={st.meta}>{date}</Text>}
            {!!date && !!duration && <Text style={st.dot}>·</Text>}
            {!!duration && <Text style={st.meta}>{duration}</Text>}
          </View>
        </View>

        <View style={st.banner}>
          <Icon name="lock" size={15} tintColor={C.primary} />
          <View style={{ flex: 1 }}>
            <Text style={st.bannerTitle}>{banner.title}</Text>
            <Text style={st.bannerBody}>{banner.body}</Text>
          </View>
        </View>

        {showOverview ? (
          <MeetingOverviewView overview={{ sections: meeting.overview }} />
        ) : (
          momSections.map((section, i) => (
            <MomSection key={`${section.title}_${i}`} section={section}
                        st={st} C={C} />
          ))
        )}

        {!hasBody && (
          <Card>
            <Text style={st.empty}>
              No notes have been generated for this meeting yet.
            </Text>
          </Card>
        )}

        {!!meeting.audio_url && (
          <AudioPlayer url={meeting.audio_url} variant="card" />
        )}

        {(blocks.length > 0 || !!plainTranscript) && (
          <Card>
            <Text style={st.sectionTitle}>Transcript</Text>
            {blocks.length > 0 ? (
              blocks.map((b, i) => (
                <View key={i} style={st.block}>
                  <View style={st.blockHead}>
                    <View style={[st.speakerDot, {
                      backgroundColor: C.speakers[b.colour % C.speakers.length],
                    }]} />
                    <Text style={st.speakerName}>{b.speaker}</Text>
                    <Text style={st.timecode}>{fmtTs(b.start)}</Text>
                  </View>
                  <Text style={st.text} selectable>{b.text}</Text>
                </View>
              ))
            ) : (
              <Text style={st.text} selectable>{plainTranscript}</Text>
            )}
          </Card>
        )}
      </ScrollView>
    </View>
  );
}
