// lib/site-salesforce-pickers.tsx — explicit choosers for the Salesforce Site
// screens. The person picks; nothing is pre-selected from meeting content.
//
//   UserPicker    a Salesforce User from the LOCAL People Store (no
//                 Salesforce call — the same store participant tagging uses)
//   RecordPicker  a record of ONE discovered object, searched live in
//                 Salesforce as the integration user (only what it can see)
//   ObjectPicker  "Recommended Salesforce records" + the objects this
//                 workspace added, then "Find another Salesforce object"
//                 (the whole discovered catalog, searched by label). Task
//                 pickers offer only Task-capable objects; meeting pickers
//                 offer every catalog object.
//
// A person reads names and labels only: no record ids, no API names.
import { useEffect, useMemo, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { SearchBar } from "./ui";
import {
  SitePerson, SiteSfObject, listSiteSalesforceObjects, searchSiteSalesforceRecords, searchSitePeople,
} from "./api";
import { visibleObjectSections } from "./site-salesforce-flow";
import { siteSfError } from "./site-salesforce-ui";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    box: { borderWidth: 1, borderColor: C.border, borderRadius: R.md, marginTop: S.sm, overflow: "hidden" },
    item: { paddingHorizontal: S.md, paddingVertical: 11, borderBottomWidth: 1, borderBottomColor: C.border },
    picked: { backgroundColor: C.primarySoft },
    name: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    sub: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
    note: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, padding: S.md },
    group: { fontFamily: FONT.bold, fontSize: 11, letterSpacing: 0.4, color: C.textFaint,
             paddingHorizontal: S.md, paddingTop: S.sm, paddingBottom: 4,
             backgroundColor: C.surface },
    err: { fontFamily: FONT.medium, fontSize: 12.5, color: C.danger, padding: S.md },
  });
}

export function UserPicker({
  workspaceId, value, onChange,
}: {
  workspaceId: string;
  value: SitePerson | null;
  onChange: (p: SitePerson | null) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [q, setQ] = useState("");
  const [people, setPeople] = useState<SitePerson[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      searchSitePeople(workspaceId, { q, type: "user" })
        .then((p) => { if (live) { setPeople(p); setError(""); } })
        .catch((e) => { if (live) setError(siteSfError(e)); });
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [workspaceId, q]);

  return (
    <View>
      <SearchBar value={q} onChangeText={setQ} placeholder="Search Salesforce Users…" />
      <View style={st.box}>
        {error ? <Text style={st.err}>{error}</Text> : null}
        {people === null && !error ? <ActivityIndicator style={{ margin: S.md }} /> : null}
        {people?.length === 0 ? (
          <Text style={st.note}>No synced Salesforce Users match. Sync Users and Contacts first.</Text>
        ) : null}
        {(people || []).slice(0, 8).map((p) => (
          <Pressable key={p.person_key}
            onPress={() => onChange(value?.person_key === p.person_key ? null : p)}
            style={[st.item, value?.person_key === p.person_key && st.picked]}>
            <Text style={st.name} numberOfLines={1}>{p.name}</Text>
            <Text style={st.sub} numberOfLines={1}>{[p.email, p.title].filter(Boolean).join(" · ")}</Text>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

export function RecordPicker({
  workspaceId, objectApiName, objectLabel, value, onChange,
}: {
  workspaceId: string;
  objectApiName: string;
  objectLabel: string;
  value: { id: string; name: string } | null;
  onChange: (r: { id: string; name: string } | null) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [q, setQ] = useState("");
  const [records, setRecords] = useState<{ id: string; name: string }[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      setLoading(true);
      searchSiteSalesforceRecords(workspaceId, objectApiName, q)
        .then((r) => { if (live) { setRecords(r); setError(""); } })
        .catch((e) => { if (live) setError(siteSfError(e)); })
        .finally(() => { if (live) setLoading(false); });
    }, 350);
    return () => { live = false; clearTimeout(t); };
  }, [workspaceId, objectApiName, q]);

  return (
    <View>
      <SearchBar value={q} onChangeText={setQ} placeholder={`Search ${objectLabel}…`} />
      <View style={st.box}>
        {error ? <Text style={st.err}>{error}</Text> : null}
        {loading && !records ? <ActivityIndicator style={{ margin: S.md }} /> : null}
        {records?.length === 0 ? <Text style={st.note}>No {objectLabel} records match.</Text> : null}
        {(records || []).map((r) => (
          <Pressable key={r.id} onPress={() => onChange(value?.id === r.id ? null : r)}
            style={[st.item, value?.id === r.id && st.picked]}
            accessibilityRole="button" accessibilityState={{ selected: value?.id === r.id }}
            accessibilityLabel={`${objectLabel} ${r.name}`}>
            <Text style={st.name} numberOfLines={2}>{r.name || objectLabel}</Text>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

export function ObjectPicker({
  workspaceId, value, onChange, capability = "task",
}: {
  workspaceId: string;
  value: SiteSfObject | null;
  onChange: (o: SiteSfObject | null) => void;
  /** "task": only objects Salesforce will attach a Task to. "meeting": every
   *  catalog object (a meeting can be linked to a Building even when Tasks
   *  cannot). */
  capability?: "meeting" | "task";
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [short, setShort] = useState<SiteSfObject[] | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SiteSfObject[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");

  // The short list: recommended + added for this workspace.
  useEffect(() => {
    let live = true;
    setError("");
    listSiteSalesforceObjects(workspaceId, { scope: "visible", capability })
      .then((o) => { if (live) setShort(o); })
      .catch((e) => { if (live) { setShort([]); setError(siteSfError(e)); } });
    return () => { live = false; };
  }, [workspaceId, capability, reload]);

  // "Find another Salesforce object": the whole catalog, by label.
  useEffect(() => {
    const term = q.trim();
    if (!term) { setResults(null); setSearchError(""); return; }
    let live = true;
    setSearching(true);
    const t = setTimeout(() => {
      listSiteSalesforceObjects(workspaceId, { scope: "all", capability, q: term, limit: 25 })
        .then((o) => { if (live) { setResults(o); setSearchError(""); } })
        .catch((e) => { if (live) setSearchError(siteSfError(e)); })
        .finally(() => { if (live) setSearching(false); });
    }, 300);
    return () => { live = false; clearTimeout(t); };
  }, [workspaceId, capability, q]);

  const row = (o: SiteSfObject) => (
    <Pressable key={o.api_name} onPress={() => onChange(value?.api_name === o.api_name ? null : o)}
      style={[st.item, value?.api_name === o.api_name && st.picked]}
      accessibilityRole="button" accessibilityState={{ selected: value?.api_name === o.api_name }}
      accessibilityLabel={o.label}>
      <Text style={st.name} numberOfLines={1}>{o.label}</Text>
      <Text style={st.sub} numberOfLines={1}>
        {o.is_custom ? "Custom object" : "Standard object"}
        {capability === "meeting" && !o.task_relationship_supported ? " · meetings only" : ""}
      </Text>
    </Pressable>
  );

  return (
    <View>
      <View style={st.box}>
        {short === null ? <ActivityIndicator style={{ margin: S.md }} /> : null}
        {error ? (
          <Pressable onPress={() => setReload((n) => n + 1)} accessibilityRole="button">
            <Text style={st.err}>{error} Tap to retry.</Text>
          </Pressable>
        ) : null}
        {short !== null && !error && short.length === 0 ? (
          <Text style={st.note}>
            No Salesforce objects yet. A workspace admin can refresh them in Settings → Integrations →
            Salesforce → Advanced.
          </Text>
        ) : null}
        {visibleObjectSections(short || []).map((g) => (
          <View key={g.title}>
            <Text style={st.group}>{g.title.toUpperCase()}</Text>
            {g.data.map(row)}
          </View>
        ))}
      </View>

      <Text style={[st.group, { backgroundColor: "transparent", paddingHorizontal: 0, marginTop: S.md }]}>
        FIND ANOTHER SALESFORCE OBJECT
      </Text>
      <SearchBar value={q} onChangeText={setQ} placeholder="Search Salesforce objects…" />
      {q.trim() ? (
        <View style={st.box}>
          {searching && !results ? <ActivityIndicator style={{ margin: S.md }} /> : null}
          {searchError ? <Text style={st.err}>{searchError}</Text> : null}
          {results && results.length === 0 && !searchError ? (
            <Text style={st.note}>
              No Salesforce object matches{capability === "task" ? " that can take Tasks" : ""}.
            </Text>
          ) : null}
          {(results || []).map(row)}
        </View>
      ) : null}
    </View>
  );
}
