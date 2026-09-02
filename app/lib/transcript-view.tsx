// lib/transcript-view.tsx — the transcript reading experience, in one place.
//
// Extracted from recording/[key]/index.tsx when the Transcript stopped being a
// tab and became its own screen (recording/[key]/transcript). The rendering,
// the speaker-block grouping and the colour assignment all moved here TOGETHER
// and unchanged, because the three are one behaviour: tap-to-seek only works
// if a block's `start` is the real segment start, and the speaker colour must
// match the one the rest of the app shows for that label.
//
// WHAT MUST NOT CHANGE, and why it is spelled out:
//   * SEGMENT ORDER is the transcript's order. Never sorted, never filtered
//     on the way in — search filters the RENDERED list only.
//   * Consecutive segments from ONE speaker are grouped into a block. That is
//     presentation; the underlying segment list is untouched.
//   * `start` / `end` are read straight off the segment via Number(). The
//     audio player seeks on that value, so a rounded or re-derived timestamp
//     would seek to the wrong moment.
//   * Segment ids are NOT used for grouping or ordering. They are new and
//     additive (see api.ts's TimestampSeg), and positional alignment is what
//     tap-to-seek has always relied on — so ids are carried, not depended on.
import { useMemo } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import { S, R, FONT, TABULAR, useTheme, ColorScale } from "./theme";
import { Icon } from "./icons";
import { useAudioSeek } from "./audio-player";
import type { TimestampSeg } from "./api";

export type SpeakerBlock = {
  speaker: string;
  start: number;
  end: number;
  texts: string[];
  /** The ids of the segments folded into this block, in order. Carried so a
   *  caller can resolve an AI evidence reference ("seg_12") to the block that
   *  contains it; nothing in the rendering depends on it. */
  ids: string[];
};

const SPEAKER_LABEL_HIT = 6;

/** Seconds -> "MM:SS". Zero-padded on BOTH halves, as the transcript has
 *  always rendered it: the timestamps sit in a fixed-width tabular column, and
 *  an unpadded minute would make the column ragged. */
export function fmtTs(sec: number) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** Consecutive same-speaker segments folded into one block, in transcript
 *  order. The ONE implementation — the detail screen and the transcript screen
 *  must agree, or a timestamp shown on one seeks somewhere else on the other. */
export function buildSpeakerBlocks(segs?: TimestampSeg[] | null): SpeakerBlock[] {
  const blocks: SpeakerBlock[] = [];
  for (const s of segs ?? []) {
    const last = blocks[blocks.length - 1];
    if (last && last.speaker === s.speaker) {
      last.texts.push(s.text);
      last.end = Number(s.end) || last.end;
      if (s.id) last.ids.push(s.id);
    } else {
      blocks.push({
        speaker: s.speaker,
        start: Number(s.start) || 0,
        end: Number(s.end) || 0,
        texts: [s.text],
        ids: s.id ? [s.id] : [],
      });
    }
  }
  return blocks;
}

/** Speaker label -> colour, assigned in FIRST-APPEARANCE order so a given
 *  speaker keeps one colour across every surface that renders them. */
export function buildSpeakerColors(
  segs: TimestampSeg[] | null | undefined,
  speakers: string[]
): Map<string, string> {
  const map = new Map<string, string>();
  for (const seg of segs ?? []) {
    if (!map.has(seg.speaker)) {
      map.set(seg.speaker, speakers[map.size % speakers.length]);
    }
  }
  return map;
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    // Carried over verbatim from the old Transcript tab's styles, so moving
    // the transcript to its own screen changed where it lives and nothing
    // about how it looks.
    searchBar: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 9,
      backgroundColor: C.surface2, borderRadius: R.pill,
      paddingHorizontal: S.md, paddingVertical: 10, marginTop: S.lg,
    },
    searchInput: {
      flex: 1, fontFamily: FONT.regular, fontSize: 14.5, color: C.text,
      paddingVertical: 4,
    },
    empty: {
      fontFamily: FONT.regular, fontSize: 13.5, color: C.textFaint,
      fontStyle: "italic" as const, marginTop: 16,
    },
    plain: {
      fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: C.text,
      marginTop: S.lg,
    },
  });
}

// ===========================================================================
// The transcript body: search, speaker labels/colours, timestamps,
// tap-to-seek. No AI features here at all, by design.
// ===========================================================================
export function TranscriptView({
  transcript, speakerBlocks, speakerColors, resolveName, onRenameSpeaker,
  search, onSearchChange, highlightIds, onHighlightLayout,
}: {
  transcript: string;
  speakerBlocks: SpeakerBlock[];
  speakerColors: Map<string, string>;
  resolveName: (label: string) => string;
  onRenameSpeaker: (label: string) => void;
  search: string;
  onSearchChange: (v: string) => void;
  /** Segment ids to call out — an AI task's `ai_evidence_segment_ids`. The
   *  block model has carried `ids` since it was written precisely so a
   *  reference like "seg_12" could be resolved without a second data path.
   *  Optional: every existing caller renders exactly as before. */
  highlightIds?: string[];
  /** Fired with the y-offset of the FIRST highlighted block, so the screen can
   *  scroll to it. Reported rather than scrolled here because this component
   *  does not own the ScrollView — the screen does. */
  onHighlightLayout?: (y: number) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { seekTo, currentTime, playing } = useAudioSeek();

  // A Set so a block with many segments is still one cheap lookup per block.
  const wanted = useMemo(
    () => new Set((highlightIds ?? []).filter(Boolean)), [highlightIds]);

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
          // The evidence this screen was opened for. Kept SEPARATE from
          // `active` (the playback cursor): they mean different things and can
          // legitimately be true at once, so one must not silently mask the
          // other.
          const isEvidence = wanted.size > 0 && b.ids.some((id) => wanted.has(id));
          const firstEvidence =
            isEvidence &&
            filteredBlocks.findIndex((x) => x.ids.some((id) => wanted.has(id))) === i;
          return (
            <View
              key={i}
              style={{ flexDirection: "row", gap: 12, marginTop: 18 }}
              onLayout={
                firstEvidence && onHighlightLayout
                  ? (e) => onHighlightLayout(e.nativeEvent.layout.y)
                  : undefined
              }
            >
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
                flex: 1,
                backgroundColor: isEvidence
                  ? C.accentSoft
                  : active ? C.primarySoft : "transparent",
                borderRadius: R.md,
                padding: isEvidence || active ? 10 : 0,
                // A left rule rather than a border box: the evidence block
                // should read as marked, not as a separate card interrupting
                // the transcript.
                borderLeftWidth: isEvidence ? 3 : 0,
                borderLeftColor: isEvidence ? C.accent : "transparent",
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
        // No timestamps (a non-diarized STT result): the flat transcript is
        // still the source of truth and must still be readable.
        <Text style={st.plain}>{transcript}</Text>
      )}
    </View>
  );
}
