// lib/mom-preview.tsx — what the exported document will look like.
//
// WHY NATIVE VIEWS AND NOT A WEBVIEW. The obvious way to guarantee the preview
// matches the PDF is to render lib/mom-html.ts's HTML in a WebView. But
// react-native-webview is not in this project, and adding a native module for
// a preview screen means every developer rebuilds their dev client and the
// preview breaks on any build that predates it — the exact degradation problem
// lib/export-doc.ts spends its header comment defending against.
//
// So the preview is drawn with the same React Native primitives as the rest of
// the app, reading its colours, type scale and column weights from
// lib/mom-template.ts — the SAME constants the PDF and DOCX renderers use.
// The document's proportions are reproduced rather than its pixels: a phone is
// not 8.5 inches wide, so a true-to-scale page would be unreadable anyway.
// What the preview promises is content, order, structure and styling, and it
// keeps that promise because all four come from one place.
//
// Points are converted to device-independent pixels at the ratio that makes a
// letter-width page fill a phone screen, so relative type sizes are honest
// even though absolute ones cannot be.
import { useMemo } from "react";
import {
  ScrollView, StyleSheet, Text, View, useWindowDimensions,
} from "react-native";
import type { Mom, MomSection } from "./api";
import { visibleSections } from "./mom-model";
import { CONTENT_WIDTH_IN, MOM_TEMPLATE as T, columnWeights, templateDate } from "./mom-template";
import { FONT, R, useTheme, type ColorScale } from "./theme";

/** Points -> px for the on-screen rendering.
 *
 * A point is 1/72", so a letter page's 6.7" text column would need ~482pt of
 * width. Scaling that to the phone's content width keeps every relative size
 * (a heading vs body vs table cell) exactly as it appears in the PDF.
 */
function useScale() {
  const { width } = useWindowDimensions();
  const pageWidth = Math.min(width - 32, 560);
  return {
    pageWidth,
    pt: (v: number) => (v / (CONTENT_WIDTH_IN * 72)) * pageWidth,
  };
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    page: {
      backgroundColor: "#FFFFFF",
      borderRadius: R.card,
      borderWidth: 1,
      borderColor: C.border,
      overflow: "hidden",
      alignSelf: "center",
      // A real page's shadow, so the preview reads as a document rather than
      // as another app screen.
      shadowColor: "#000", shadowOpacity: 0.1, shadowRadius: 14,
      shadowOffset: { width: 0, height: 5 }, elevation: 3,
    },
    empty: {
      fontFamily: FONT.regular, fontStyle: "italic",
      color: "#" + T.inkFaint, textAlign: "center", paddingVertical: 28,
    },
  });
}

function Masthead({ mom, meetingTitle, pt }: {
  mom: Mom; meetingTitle: string; pt: (v: number) => number;
}) {
  const subtitle = mom.subtitle?.trim() || meetingTitle;
  return (
    <View style={{ borderBottomWidth: pt(2.5), borderBottomColor: "#" + T.accent,
                   paddingBottom: pt(9), marginBottom: pt(4) }}>
      <Text style={{
        fontFamily: FONT.bold, fontSize: pt(T.kickerPt), letterSpacing: 1.2,
        color: "#" + T.accent, marginBottom: pt(3),
      }}>
        {T.company.toUpperCase()}
      </Text>
      <Text style={{
        fontFamily: FONT.extrabold, fontSize: pt(T.titlePt),
        lineHeight: pt(T.titlePt * 1.15), color: "#" + T.ink,
      }}>
        {mom.title?.trim() || "Minutes of Meeting"}
      </Text>
      {subtitle ? (
        <Text style={{
          fontFamily: FONT.semibold, fontSize: pt(T.subtitlePt),
          color: "#" + T.inkDim, marginTop: pt(2),
        }}>
          {subtitle}
        </Text>
      ) : null}
    </View>
  );
}

function FieldsBlock({ section, pt }: {
  section: MomSection; pt: (v: number) => number;
}) {
  const fields = (section.fields ?? []).filter((f) => f.visible && (f.label || f.value));
  return (
    <View>
      {fields.map((f) => (
        <View key={f.id} style={{ flexDirection: "row", marginBottom: pt(4) }}>
          <Text style={{
            fontFamily: FONT.semibold, fontSize: pt(T.bodyPt),
            color: "#" + T.inkDim, width: "33%", paddingRight: pt(8),
          }}>
            {f.label}
          </Text>
          <Text style={{
            fontFamily: FONT.regular, fontSize: pt(T.bodyPt),
            color: "#" + T.ink, flex: 1,
          }}>
            {f.value}
          </Text>
        </View>
      ))}
    </View>
  );
}

function TableBlock({ section, pt, width }: {
  section: MomSection; pt: (v: number) => number; width: number;
}) {
  // Memoised so the weights below have a dependency that is stable between
  // renders — `section.columns ?? []` allocates a new array every time.
  const columns = useMemo(() => section.columns ?? [], [section.columns]);
  const rows = (section.rows ?? []).filter((r) => r.visible);
  const weights = useMemo(
    () => columnWeights(columns.map((c) => c.label)), [columns]);

  // A wide table gets a minimum per-column width and scrolls horizontally
  // rather than crushing five columns into a phone's width — the same
  // trade-off the editor's table view makes.
  const minColumn = pt(52);
  const natural = weights.map((w) => Math.max(minColumn, w * width));
  const total = natural.reduce((a, b) => a + b, 0);
  const scrolls = total > width + 1;
  const widths = scrolls ? natural : weights.map((w) => w * width);

  const table = (
    <View style={{ width: scrolls ? total : width }}>
      <View style={{ flexDirection: "row", backgroundColor: "#" + T.accent }}>
        {columns.map((c, i) => (
          <Text
            key={c.id}
            style={{
              width: widths[i], paddingVertical: pt(5), paddingHorizontal: pt(6),
              fontFamily: FONT.bold, fontSize: pt(T.kickerPt),
              letterSpacing: 0.5, color: "#FFFFFF",
            }}
          >
            {c.label.toUpperCase()}
          </Text>
        ))}
      </View>
      {rows.map((r, ri) => (
        <View
          key={r.id}
          style={{
            flexDirection: "row",
            backgroundColor: ri % 2 === 1 ? "#" + T.zebra : "#FFFFFF",
            borderBottomWidth: StyleSheet.hairlineWidth,
            borderBottomColor: "#" + T.rule,
          }}
        >
          {columns.map((c, ci) => (
            <Text
              key={c.id}
              style={{
                width: widths[ci], paddingVertical: pt(5),
                paddingHorizontal: pt(6), fontFamily: FONT.regular,
                fontSize: pt(T.tablePt), color: "#" + T.ink,
              }}
            >
              {r.cells[c.id] ?? ""}
            </Text>
          ))}
        </View>
      ))}
    </View>
  );

  if (!scrolls) return table;
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator style={{ width }}>
      {table}
    </ScrollView>
  );
}

function ListBlock({ section, pt }: {
  section: MomSection; pt: (v: number) => number;
}) {
  const items = (section.items ?? []).filter((i) => i.visible && i.text.trim());
  return (
    <View>
      {items.map((i) => (
        <View key={i.id} style={{ flexDirection: "row", marginBottom: pt(4) }}>
          <Text style={{
            fontSize: pt(T.bodyPt), color: "#" + T.accent,
            marginRight: pt(6), lineHeight: pt(T.bodyPt * 1.5),
          }}>
            •
          </Text>
          <Text style={{
            fontFamily: FONT.regular, fontSize: pt(T.bodyPt),
            lineHeight: pt(T.bodyPt * 1.5), color: "#" + T.ink, flex: 1,
          }}>
            {i.text}
          </Text>
        </View>
      ))}
    </View>
  );
}

function SectionBlock({ section, pt, width }: {
  section: MomSection; pt: (v: number) => number; width: number;
}) {
  return (
    <View style={{ marginBottom: pt(16) }}>
      <Text style={{
        fontFamily: FONT.bold, fontSize: pt(T.headingPt), letterSpacing: 0.7,
        color: "#" + T.accent, marginBottom: pt(7),
      }}>
        {section.title.toUpperCase()}
      </Text>
      {section.kind === "fields" ? <FieldsBlock section={section} pt={pt} /> : null}
      {section.kind === "table" ? (
        <TableBlock section={section} pt={pt} width={width} />
      ) : null}
      {section.kind === "text" ? (
        <Text style={{
          fontFamily: FONT.regular, fontSize: pt(T.bodyPt),
          lineHeight: pt(T.bodyPt * 1.5), color: "#" + T.ink,
        }}>
          {section.text}
        </Text>
      ) : null}
      {section.kind === "list" ? <ListBlock section={section} pt={pt} /> : null}
    </View>
  );
}

/**
 * The MoM as it will be exported.
 *
 * One continuous page rather than a paginated stack: where the page breaks
 * fall is decided by the PDF engine against the rendered text, and drawing
 * invented break lines here would promise a layout the export may not match.
 * The page COUNT is reported separately by the editor, from expo-print's own
 * numberOfPages once a PDF has actually been rendered.
 */
export function MomPreview({ mom, meetingTitle }: {
  mom: Mom; meetingTitle: string;
}) {
  const { C } = useTheme();
  const st = buildStyles(C);
  const { pageWidth, pt } = useScale();
  const padding = pt(T.marginIn * 72 * 0.55);
  const contentWidth = pageWidth - padding * 2;
  const sections = visibleSections(mom);

  return (
    <ScrollView
      contentContainerStyle={{ padding: 16, paddingBottom: 48 }}
      showsVerticalScrollIndicator={false}
    >
      <View style={[st.page, { width: pageWidth, padding }]}>
        <Masthead mom={mom} meetingTitle={meetingTitle} pt={pt} />
        <Text style={{
          fontFamily: FONT.regular, fontSize: pt(T.kickerPt),
          color: "#" + T.inkFaint, marginBottom: pt(18),
        }}>
          {templateDate()}
        </Text>

        {sections.length ? (
          sections.map((s) => (
            <SectionBlock key={s.id} section={s} pt={pt} width={contentWidth} />
          ))
        ) : (
          <Text style={st.empty}>
            Nothing to preview yet — add a section in the editor.
          </Text>
        )}

        <View style={{
          borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: "#" + T.rule,
          marginTop: pt(6), paddingTop: pt(8),
        }}>
          <Text style={{
            fontFamily: FONT.regular, fontSize: pt(T.footerPt),
            color: "#" + T.inkFaint,
          }}>
            {T.footerNote}
          </Text>
        </View>
      </View>
    </ScrollView>
  );
}
