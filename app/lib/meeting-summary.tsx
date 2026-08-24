// lib/meeting-summary.tsx — Overview tab content: Summary and Highlights.
//
// One AI summary, read like a professional meeting note — never split into
// Decisions/Actions/Risks/Timeline/Budget sections. Below it, four or five
// lightweight highlight cards. Chat ("Ask MinuteX") no longer lives here — it
// moved to the dedicated Assistant workspace so Overview has exactly one job:
// let the user understand the meeting at a glance.
import { useMemo, useState } from "react";
import { Pressable, Share, StyleSheet, Text, View } from "react-native";
import Animated, { LinearTransition } from "react-native-reanimated";
import { FONT, S, useTheme, ColorScale } from "./theme";
import { Card, SectionRule } from "./ui";
import { Icon, type IconName } from "./icons";
import type { MeetingHighlights, Participant } from "./api";
import { normalizeSpeakerLabel } from "./sources";

const SUMMARY_CLAMP = 360;

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    actionsRow: { flexDirection: "row" as const, gap: S.lg, marginTop: S.md },
    actionTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary },
    highlightRow: {
      flexDirection: "row" as const, alignItems: "flex-start" as const, gap: S.md,
      paddingVertical: 11,
    },
    highlightDot: {
      width: 34, height: 34, borderRadius: 12, backgroundColor: C.primarySoft,
      alignItems: "center" as const, justifyContent: "center" as const, marginTop: 1,
    },
    highlightTxt: { fontFamily: FONT.medium, fontSize: 14.5, lineHeight: 21, color: C.text, flex: 1 },
  });
}

// ===========================================================================
// Summary — one AI-generated note. Read More / Share are the only actions.
// ===========================================================================
export function MeetingSummary({ summary }: { summary: string }) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [expanded, setExpanded] = useState(false);
  const long = summary.length > SUMMARY_CLAMP;
  const shown = expanded || !long
    ? summary
    : summary.slice(0, SUMMARY_CLAMP).replace(/\s+\S*$/, "") + "…";

  const share = async () => {
    try {
      await Share.share({ message: summary, title: "Summary" });
    } catch {
      // user cancelled the share sheet
    }
  };

  return (
    <View>
      <SectionRule
        right={<Icon name="sparkles" tintColor={C.accent} size={16} />}
      >
        Summary
      </SectionRule>
      <Card>
        <Animated.View layout={LinearTransition.springify().damping(16).stiffness(180)}>
          <Text style={T.bodyLead}>{shown}</Text>
        </Animated.View>
        <View style={st.actionsRow}>
          {long ? (
            <Pressable onPress={() => setExpanded((v) => !v)} hitSlop={6}>
              <Text style={st.actionTxt}>{expanded ? "Show less" : "Read more"}</Text>
            </Pressable>
          ) : null}
          <Pressable onPress={share} hitSlop={6}>
            <Text style={st.actionTxt}>Share</Text>
          </Pressable>
        </View>
      </Card>
    </View>
  );
}

// ===========================================================================
// Highlights — at most 5 plain-string rows.
//
// `highlights` (the new, primary field — a flat string[] straight from the
// single-pass analysis) is preferred. `legacyHighlights` (meeting_highlights,
// the older structured decisions/action_items/deadlines/numbers/risks
// extraction) is the fallback ONLY for a recording processed before
// `highlights` existed — new recordings never need it here.
// ===========================================================================
type HighlightRow = { icon: IconName; text: string };

function buildLegacyHighlightRows(h: MeetingHighlights): HighlightRow[] {
  const rows: HighlightRow[] = [];
  for (const d of h.decisions ?? []) rows.push({ icon: "gavel", text: d.decision });
  for (const n of h.important_numbers ?? []) rows.push({ icon: "number", text: `${n.label}: ${n.value}` });
  for (const dl of h.deadlines ?? []) rows.push({ icon: "calendar.badge.clock", text: `${dl.what} — ${dl.when}` });
  for (const a of h.action_items ?? []) rows.push({ icon: "checkmark.circle", text: a.task });
  for (const r of h.risks ?? []) rows.push({ icon: "exclamationmark.triangle", text: r });
  return rows.slice(0, 5);
}

export function Highlights({
  highlights, legacyHighlights,
}: {
  highlights?: string[] | null;
  legacyHighlights?: MeetingHighlights | null;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const rows: HighlightRow[] = highlights?.length
    ? highlights.slice(0, 5).map((text) => ({ icon: "sparkles", text }))
    : legacyHighlights
      ? buildLegacyHighlightRows(legacyHighlights)
      : [];
  if (!rows.length) return null;

  return (
    <View>
      <SectionRule right={<Icon name="lightbulb.fill" tintColor={C.warn} size={16} />}>
        Highlights
      </SectionRule>
      <Card style={{ paddingVertical: S.sm }}>
        {rows.map((row, i) => (
          <View key={i} style={[st.highlightRow, i > 0 && { borderTopWidth: 1, borderTopColor: C.border }]}>
            <View style={st.highlightDot}>
              <Icon name={row.icon} tintColor={C.primary} size={16} />
            </View>
            <Text style={st.highlightTxt}>{row.text}</Text>
          </View>
        ))}
      </Card>
    </View>
  );
}

// ===========================================================================
// Participants — who spoke, resolved to their real name LIVE via
// speaker_names (same as the Transcript tab's speaker labels, lib/sources.ts'
// speakerName/normalizeSpeakerLabel). This is a rendered VIEW over
// rec.participants, never mutated data — a rename updates it on the next
// render with no backend round trip and no document-style staleness flag,
// because there's no baked-in text here to go stale.
//
// `participants[].speaker` is Groq-instructed to be exactly "Speaker N"
// (lambda-shared/prompts.py), unlike action_items[].owner which may be a
// real spoken name — that reliability is what makes live resolution safe
// here but not for owners.
//
// Naming works from here too, not just the Transcript tab: tapping a row
// opens the same rename sheet via the same openRenameSpeaker/renameSpeaker
// path, so one mapping still drives every surface. This is where an unnamed
// speaker is most visible, so it's where the user reaches for the rename.
// ===========================================================================
export function Participants({
  participants, resolveName, onRenameSpeaker,
}: {
  participants?: Participant[] | null;
  resolveName: (label: string) => string;
  /** Omitted on read-only surfaces; rows fall back to plain, unpressable Views. */
  onRenameSpeaker?: (label: string) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  if (!participants?.length) return null;

  return (
    <View>
      <SectionRule right={<Icon name="person.2.fill" tintColor={C.accent} size={16} />}>
        Participants
      </SectionRule>
      <Card style={{ paddingVertical: S.sm }}>
        {participants.map((p, i) => {
          // Normalize once: the rename sheet keys off the raw label ("0"), while
          // Groq hands us the display form ("Speaker 0") in p.speaker.
          const raw = normalizeSpeakerLabel(p.speaker);
          const name = resolveName(raw);
          // Same unnamed test the transcript uses — drives the pencil affordance.
          const isNamed = name !== `Speaker ${raw}`;
          const rowStyle = [st.highlightRow, i > 0 && { borderTopWidth: 1, borderTopColor: C.border }];

          const body = (
            <>
              <View style={st.highlightDot}>
                <Icon name="person.fill" tintColor={C.primary} size={16} />
              </View>
              <View style={{ flex: 1 }}>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
                  <Text style={[st.highlightTxt, { fontFamily: FONT.semibold, flex: 0 }]}>
                    {name}
                  </Text>
                  {onRenameSpeaker && !isNamed ? (
                    <Icon name="pencil" tintColor={C.textFaint} size={11} />
                  ) : null}
                </View>
                {p.summary ? (
                  <Text style={[st.highlightTxt, { marginTop: 2, color: C.textDim }]}>{p.summary}</Text>
                ) : null}
              </View>
            </>
          );

          return onRenameSpeaker ? (
            <Pressable
              key={i}
              onPress={() => onRenameSpeaker(raw)}
              style={({ pressed }) => [...rowStyle, pressed && { opacity: 0.6 }]}
              accessibilityRole="button"
              accessibilityLabel={isNamed ? `Rename ${name}` : `Name ${name}`}
            >
              {body}
            </Pressable>
          ) : (
            <View key={i} style={rowStyle}>{body}</View>
          );
        })}
      </Card>
    </View>
  );
}
