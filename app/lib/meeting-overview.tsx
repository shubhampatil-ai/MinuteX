// lib/meeting-overview.tsx — the Overview tab: the AI's own sections.
//
// THE ONE RULE IN THIS FILE: it renders `overview.sections[]` and knows
// nothing about what any section MEANS. There is no switch on section.title,
// no per-title icon map, no "Decisions goes here, Risks goes there". The
// backend prompt deliberately has no section catalogue (the model picks
// titles per meeting — "Panel Feedback" for a design review, "Objections" for
// a sales call), so a renderer that special-cased titles would either
// reintroduce that fixed template or silently fail to render any title it did
// not anticipate.
//
// Two section kinds cover everything the schema can produce: `text` (prose)
// and `list` (bullets). That is the whole vocabulary — see api.ts's
// OverviewSection. A future kind means a new branch HERE and nowhere else.
//
// LEGACY. A recording analysed before the overview shipped has no `sections`
// at all, only the old fixed `summary` string and `highlights` list. That case
// is handled by the caller falling back to lib/meeting-summary.tsx's
// MeetingSummary/Highlights, which still exist for exactly that reason — this
// file never renders the legacy shape, so neither path has to know about the
// other.
import { useMemo, useState } from "react";
import { Pressable, Share, StyleSheet, Text, View } from "react-native";
import Animated, { LinearTransition } from "react-native-reanimated";
import { FONT, S, useTheme, ColorScale } from "./theme";
import { Card, SectionRule } from "./ui";
import { Icon } from "./icons";
import type { MeetingOverview, OverviewSection } from "./api";

// Prose longer than this collapses behind "Read more". Matches the old
// summary card's clamp so a long section reads the same as a long summary did.
const TEXT_CLAMP = 360;

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    actionsRow: { flexDirection: "row" as const, gap: S.lg, marginTop: S.md },
    actionTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary },
    itemRow: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.md, paddingVertical: 9,
    },
    bullet: {
      width: 6, height: 6, borderRadius: 3, backgroundColor: C.primary,
      marginTop: 8,
    },
    itemTxt: {
      fontFamily: FONT.medium, fontSize: 14.5, lineHeight: 21,
      color: C.text, flex: 1,
    },
    empty: {
      fontFamily: FONT.medium, fontSize: 14, lineHeight: 20,
      color: C.textDim, textAlign: "center" as const, paddingVertical: S.lg,
    },
  });
}

/** A section is worth rendering when it has a title AND something under it.
 *  The backend already drops empty sections; this is the client-side half of
 *  the same rule, so a stale row or a hand-edited item can't render a heading
 *  with nothing beneath it. */
function hasContent(s: OverviewSection): boolean {
  if (!s?.title?.trim()) return false;
  return !!s.content?.trim() || !!s.items?.some((i) => i?.trim());
}

// ===========================================================================
// One section. Prose collapses; bullets don't (a list is already scannable,
// and clamping it would hide the very items that make it useful).
// ===========================================================================
function OverviewSectionCard({ section }: { section: OverviewSection }) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [expanded, setExpanded] = useState(false);

  const text = (section.content ?? "").trim();
  const items = (section.items ?? []).map((i) => (i ?? "").trim()).filter(Boolean);

  // Kind follows the CONTENT, not the label — the backend coerces this the
  // same way, so the two agree, and a mislabelled section still renders as
  // whatever it actually is rather than as an empty card.
  const asList = items.length > 0 && !text;

  const long = text.length > TEXT_CLAMP;
  const shown = expanded || !long
    ? text
    : text.slice(0, TEXT_CLAMP).replace(/\s+\S*$/, "") + "…";

  const share = async () => {
    try {
      const body = text || items.map((i) => `• ${i}`).join("\n");
      await Share.share({ message: body, title: section.title });
    } catch {
      // user cancelled the share sheet
    }
  };

  return (
    <View>
      <SectionRule right={<Icon name="sparkles" tintColor={C.accent} size={16} />}>
        {section.title}
      </SectionRule>
      <Card style={asList ? { paddingVertical: S.sm } : undefined}>
        {asList ? (
          items.map((item, i) => (
            <View
              key={i}
              style={[st.itemRow,
                      i > 0 && { borderTopWidth: 1, borderTopColor: C.border }]}
            >
              <View style={st.bullet} />
              <Text style={st.itemTxt}>{item}</Text>
            </View>
          ))
        ) : (
          <>
            <Animated.View
              layout={LinearTransition.springify().damping(16).stiffness(180)}
            >
              <Text style={T.bodyLead}>{shown}</Text>
            </Animated.View>
            {/* A section carrying BOTH prose and bullets renders the prose as
                the lead-in and the bullets under it — nothing is dropped. */}
            {items.length ? (
              <View style={{ marginTop: S.sm }}>
                {items.map((item, i) => (
                  <View key={i} style={st.itemRow}>
                    <View style={st.bullet} />
                    <Text style={st.itemTxt}>{item}</Text>
                  </View>
                ))}
              </View>
            ) : null}
          </>
        )}
        <View style={st.actionsRow}>
          {!asList && long ? (
            <Pressable onPress={() => setExpanded((v) => !v)} hitSlop={6}>
              <Text style={st.actionTxt}>
                {expanded ? "Show less" : "Read more"}
              </Text>
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
// The Overview. Renders every valid section, in the order the AI chose.
//
// Returns null (not an empty state) when there is nothing to show, so the
// CALLER decides what an absent overview means — "still generating", "analysis
// failed" and "this old recording has a legacy summary instead" are three
// different messages and only the caller knows which applies.
// ===========================================================================
export function MeetingOverviewView({
  overview,
}: {
  overview?: MeetingOverview | null;
}) {
  const sections = (overview?.sections ?? []).filter(hasContent);
  if (!sections.length) return null;

  return (
    <View>
      {sections.map((section, i) => (
        // Keyed on the backend's stable id, falling back to position for a row
        // written before ids existed. Never on the title: two sections can
        // share one, and a duplicate React key drops a card silently.
        <OverviewSectionCard key={section.id || `section_${i}`} section={section} />
      ))}
    </View>
  );
}

/** True when `overview` has at least one renderable section. Exported so the
 *  caller can choose between the overview and the legacy summary WITHOUT
 *  duplicating the emptiness rule — the two must agree, or a meeting can show
 *  both or neither. */
export function hasOverview(overview?: MeetingOverview | null): boolean {
  return (overview?.sections ?? []).some(hasContent);
}
