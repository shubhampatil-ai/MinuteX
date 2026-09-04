// lib/document-renderer.tsx — renders the small Markdown subset the AI
// document/summary/chat prompts are instructed to emit: headings, bold/
// italic, bullets, numbered lists, tables, paragraphs, horizontal rules.
//
// Pulling in a full Markdown library to render a grammar we control ourselves
// would be dependency weight for no gain — see lib/export-doc.ts, which
// applies the same reasoning to the HTML-for-PDF path and handles the exact
// same subset.
import { useMemo } from "react";
import { StyleSheet, Text, View } from "react-native";
import { unescapeMarkdown } from "./export-doc";
import { CAPS, FONT, TABULAR, useTheme, type ColorScale } from "./theme";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    bulletMark: { width: 5, height: 5, backgroundColor: C.primary, marginTop: 8, flexShrink: 0 },
    mdH2: { fontFamily: FONT.extrabold, fontSize: 19, lineHeight: 25, color: C.text, marginTop: 16, marginBottom: 4 },
    mdH3: { fontFamily: FONT.bold, fontSize: 14, color: C.text, marginTop: 12, marginBottom: 3 },
    mdP: { fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: C.text, marginTop: 8 },
    mdLi: { flexDirection: "row" as const, gap: 9, marginTop: 6 },
    mdTable: { borderWidth: 1, borderColor: C.border, borderRadius: 3, marginTop: 12 },
    mdTr: { flexDirection: "row" as const, borderBottomWidth: 1, borderBottomColor: C.border },
    mdTh: {
      flex: 1, padding: 8, ...CAPS, fontSize: 8.5, letterSpacing: 0.8,
      color: C.textDim, backgroundColor: C.surface2,
    },
    mdTd: { flex: 1, padding: 8, fontFamily: FONT.regular, fontSize: 12, color: C.text },
  });
}

function inline(text: string, key: string, baseStyle: any) {
  const parts = unescapeMarkdown(text)
    .split(/(\*\*[^*]+\*\*|\*[^*\n]+\*)/g).filter(Boolean);
  return (
    <Text key={key} style={baseStyle}>
      {parts.map((p, i) => {
        if (/^\*\*[^*]+\*\*$/.test(p)) {
          return <Text key={i} style={{ fontFamily: FONT.bold }}>{p.slice(2, -2)}</Text>;
        }
        if (/^\*[^*\n]+\*$/.test(p)) {
          return <Text key={i} style={{ fontStyle: "italic" }}>{p.slice(1, -1)}</Text>;
        }
        return p;
      })}
    </Text>
  );
}

export function Markdown({ text }: { text: string }) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);

  const blocks = useMemo(() => {
    const lines = (text || "").split("\n");
    const out: React.ReactNode[] = [];
    let i = 0;

    const isDivider = (l: string) => /^\|?[\s:|-]+\|[\s:|-]*$/.test(l) && l.includes("-");
    // Cells render directly (not through inline()), so they unescape here.
    const cells = (l: string) => l.replace(/^\||\|$/g, "").split("|")
      .map((c) => unescapeMarkdown(c.trim()));

    while (i < lines.length) {
      const line = lines[i].trim();
      if (!line) { i++; continue; }

      if (line.startsWith("|") && isDivider((lines[i + 1] ?? "").trim())) {
        const header = cells(line);
        const rows: string[][] = [];
        i += 2;
        while (i < lines.length && lines[i].trim().startsWith("|")) {
          rows.push(cells(lines[i].trim()));
          i++;
        }
        out.push(
          <View key={`t${i}`} style={st.mdTable}>
            <View style={st.mdTr}>
              {header.map((h, x) => (
                <Text key={x} style={st.mdTh} numberOfLines={2}>{h}</Text>
              ))}
            </View>
            {rows.map((r, y) => (
              <View key={y} style={[st.mdTr, y === rows.length - 1 && { borderBottomWidth: 0 }]}>
                {r.map((c, x) => <Text key={x} style={st.mdTd}>{c}</Text>)}
              </View>
            ))}
          </View>
        );
        continue;
      }

      const h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) {
        out.push(
          // Unescaped explicitly: headings are the one block that renders its
          // text directly rather than through inline().
          <Text key={`h${i}`} style={h[1].length <= 2 ? st.mdH2 : st.mdH3}>
            {unescapeMarkdown(h[2])}
          </Text>
        );
        i++; continue;
      }

      const bullet = /^[-*+]\s+(.*)$/.exec(line);
      if (bullet) {
        out.push(
          <View key={`b${i}`} style={st.mdLi}>
            <View style={st.bulletMark} />
            {inline(bullet[1], `bt${i}`, [st.mdP, { marginTop: 0, flex: 1 }])}
          </View>
        );
        i++; continue;
      }

      const num = /^(\d+)[.)]\s+(.*)$/.exec(line);
      if (num) {
        out.push(
          <View key={`n${i}`} style={st.mdLi}>
            <Text style={{ ...TABULAR, fontSize: 11.5, color: C.primary, marginTop: 4 }}>
              {num[1]}.
            </Text>
            {inline(num[2], `nt${i}`, [st.mdP, { marginTop: 0, flex: 1 }])}
          </View>
        );
        i++; continue;
      }

      if (/^(---|___|\*\*\*)$/.test(line)) {
        out.push(<View key={`d${i}`} style={{ height: 1, backgroundColor: C.border, marginVertical: 14 }} />);
        i++; continue;
      }

      out.push(inline(line, `p${i}`, st.mdP));
      i++;
    }
    return out;
  }, [text, st, C]);

  return <View>{blocks}</View>;
}
