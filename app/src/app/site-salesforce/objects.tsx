// src/app/site-salesforce/objects.tsx — the Salesforce objects this workspace
// works with.
//
// DISCOVERED IS NOT SHOWN. Discovery catalogues every queryable business and
// custom object in the org (hundreds, sometimes 1,000+). This screen shows the
// short list — "Recommended Salesforce records" (Account, Contact, Lead,
// Opportunity, Case) plus the objects this workspace added — and "Find
// another Salesforce object" searches the whole catalog by label. Adding an
// object puts it in every picker for everyone in the workspace; using one
// (linking a meeting to its record) adds it automatically. Labels only: no
// API names, no ids.
import { useCallback, useEffect, useMemo, useState } from "react";
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { FONT, R, S, useTheme, ColorScale } from "../../../lib/theme";
import { EmptyState, ErrorText, KeyboardAware, Loading, SearchBar } from "../../../lib/ui";
import {
  SiteSfObject, listSiteSalesforceObjectsPage, setSiteSalesforceObjectPrefs,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { Flag, FlagRow, siteSfError, useSiteSalesforceVersion } from "../../../lib/site-salesforce-ui";
import { objectUseLabel, visibleObjectSections } from "../../../lib/site-salesforce-flow";
import { invalidateSiteSalesforce } from "../../../lib/site-salesforce-events";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.x4 },
    intro: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim },
    section: { fontFamily: FONT.bold, fontSize: 11.5, letterSpacing: 0.4, color: C.textFaint,
               marginTop: S.xl, marginBottom: 6 },
    row: { flexDirection: "row", alignItems: "center", gap: S.md, paddingVertical: 12,
           borderBottomWidth: 1, borderBottomColor: C.border },
    label: { fontFamily: FONT.semibold, fontSize: 15, color: C.text },
    kind: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 2 },
    action: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary, paddingHorizontal: 4 },
    remove: { fontFamily: FONT.semibold, fontSize: 13, color: C.danger, paddingHorizontal: 4 },
    note: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: S.sm },
    pill: { borderRadius: R.pill, paddingHorizontal: 8, paddingVertical: 2, backgroundColor: C.primarySoft },
  });
}

export default function SiteSalesforceObjects() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId, can } = useWorkspace();
  const canRemove = can("manage_integrations");
  const [visible, setVisible] = useState<SiteSfObject[] | null>(null);
  const [catalogSize, setCatalogSize] = useState(0);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SiteSfObject[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState("");
  // A discovery finishing on the Salesforce screen refreshes this list.
  const version = useSiteSalesforceVersion("objects", "connection");

  const load = useCallback(async () => {
    if (!activeId) return;
    try {
      const page = await listSiteSalesforceObjectsPage(activeId, { scope: "visible" });
      setVisible(page.objects);
      setCatalogSize(page.catalog_size);
      setError("");
    } catch (e) {
      setError(siteSfError(e));
      setVisible((v) => v ?? []);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, version]);
  useFocusEffect(useCallback(() => { void load(); }, [load]));

  // Find another Salesforce object: the whole catalog, by label.
  useEffect(() => {
    const term = q.trim();
    if (!term || !activeId) { setResults(null); return; }
    let live = true;
    setSearching(true);
    const t = setTimeout(() => {
      listSiteSalesforceObjectsPage(activeId, { scope: "all", q: term, limit: 50 })
        .then((page) => { if (live) { setResults(page.objects); setTruncated(page.truncated); } })
        .catch((e) => { if (live) setError(siteSfError(e)); })
        .finally(() => { if (live) setSearching(false); });
    }, 300);
    return () => { live = false; clearTimeout(t); };
  }, [activeId, q, version]);

  const change = async (o: SiteSfObject, add: boolean) => {
    setBusy(o.api_name); setError("");
    try {
      await setSiteSalesforceObjectPrefs(activeId, add ? { enable: [o.api_name] } : { disable: [o.api_name] });
      setResults((r) => r?.map((x) => (x.api_name === o.api_name ? { ...x, enabled: add } : x)) ?? r);
      invalidateSiteSalesforce("objects");
      await load();
    } catch (e) {
      setError(siteSfError(e));
    } finally {
      setBusy("");
    }
  };

  const row = (o: SiteSfObject, inSearch: boolean) => {
    const shown = o.recommended || o.enabled;
    return (
      <View key={o.api_name} style={st.row}>
        <Pressable style={{ flex: 1 }} accessibilityRole="button" accessibilityLabel={o.label}
          onPress={() => router.push({ pathname: "/site-salesforce/fields", params: { object: o.api_name } })}>
          <Text style={st.label} numberOfLines={1}>{o.label}</Text>
          <Text style={st.kind} numberOfLines={2}>
            {o.is_custom ? "Custom object" : "Standard object"} · {objectUseLabel(o)}
          </Text>
          <FlagRow>
            <Flag label="Meetings" on={o.meeting_relationship_supported !== false} tone="info" />
            <Flag label="Tasks" on={!!o.task_relationship_supported} tone="info" />
          </FlagRow>
        </Pressable>
        {busy === o.api_name ? <ActivityIndicator color={C.primary} /> : o.recommended ? (
          <View style={st.pill}><Text style={[st.kind, { marginTop: 0, color: C.primary }]}>Recommended</Text></View>
        ) : inSearch && !shown ? (
          <Pressable onPress={() => { void change(o, true); }} disabled={!!busy}
            accessibilityRole="button" accessibilityLabel={`Add ${o.label}`} hitSlop={8}>
            <Text style={st.action}>Add</Text>
          </Pressable>
        ) : o.enabled && canRemove ? (
          <Pressable onPress={() => { void change(o, false); }} disabled={!!busy}
            accessibilityRole="button" accessibilityLabel={`Remove ${o.label}`} hitSlop={8}>
            <Text style={st.remove}>Remove</Text>
          </Pressable>
        ) : o.enabled ? <Text style={st.kind}>Added</Text> : null}
      </View>
    );
  };

  if (visible === null && !error) return <Loading />;

  return (
    <KeyboardAware style={st.screen}>
      <ScrollView style={st.screen} contentContainerStyle={st.body} keyboardShouldPersistTaps="handled">
        <Stack.Screen options={{ title: "Salesforce objects" }} />
        <Text style={st.intro}>
          The Salesforce records meetings and Tasks can be linked to. Common ones are recommended;
          find any other object your integration user can read and add it for this workspace.
        </Text>
        {error ? <ErrorText>{error}</ErrorText> : null}

        {visible && visible.length === 0 && !error ? (
          <EmptyState icon="magnifyingglass" title="No Salesforce objects yet"
            subtitle="Tap Discover Salesforce Objects on the Salesforce (Site) screen." />
        ) : null}
        {visibleObjectSections(visible || []).map((g) => (
          <View key={g.title}>
            <Text style={st.section}>{g.title.toUpperCase()}</Text>
            {g.data.map((o) => row(o, false))}
          </View>
        ))}

        <Text style={st.section}>FIND ANOTHER SALESFORCE OBJECT</Text>
        <SearchBar value={q} onChangeText={setQ}
          placeholder={catalogSize ? `Search ${catalogSize} Salesforce objects…` : "Search Salesforce objects…"} />
        {q.trim() ? (
          searching && !results ? <ActivityIndicator style={{ marginTop: S.md }} color={C.primary} /> : (
            <View>
              {results && results.length === 0 ? (
                <Text style={st.note}>No Salesforce object matches. Refresh Salesforce metadata if it was just created.</Text>
              ) : null}
              {(results || []).map((o) => row(o, true))}
              {truncated ? <Text style={st.note}>Showing the first matches. Type more to narrow.</Text> : null}
            </View>
          )
        ) : null}
      </ScrollView>
    </KeyboardAware>
  );
}
