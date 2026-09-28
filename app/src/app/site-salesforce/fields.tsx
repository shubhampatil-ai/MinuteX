// src/app/site-salesforce/fields.tsx — one object's fields, as discovered:
// type, whether the integration user may set it on create/update, whether
// Salesforce requires it, and what a lookup points to.
import { useCallback, useMemo, useState } from "react";
import { FlatList, StyleSheet, Text, View } from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { FONT, S, useTheme, ColorScale } from "../../../lib/theme";
import { Button, ErrorText, Loading } from "../../../lib/ui";
import { SiteSfField, SiteSfObject, getSiteSalesforceFields } from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { Flag, FlagRow, siteSfError } from "../../../lib/site-salesforce-ui";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    head: { padding: S.lg, borderBottomWidth: 1, borderBottomColor: C.border },
    title: { fontFamily: FONT.bold, fontSize: 18, color: C.text },
    api: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint, marginTop: 2 },
    row: { paddingHorizontal: S.lg, paddingVertical: 11, borderBottomWidth: 1, borderBottomColor: C.border },
    label: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    meta: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
  });
}

export default function SiteSalesforceFields() {
  const { object } = useLocalSearchParams<{ object: string }>();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId } = useWorkspace();
  const [obj, setObj] = useState<SiteSfObject | null>(null);
  const [fields, setFields] = useState<SiteSfField[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (refresh = false) => {
    if (!activeId || !object) return;
    setBusy(refresh);
    try {
      const r = await getSiteSalesforceFields(activeId, String(object), refresh);
      setObj(r.object); setFields(r.fields); setError("");
    } catch (e) {
      setError(siteSfError(e));
    } finally {
      setBusy(false);
    }
  }, [activeId, object]);
  useFocusEffect(useCallback(() => { void load(); }, [load]));

  if (fields === null && !error) return <Loading />;

  return (
    <View style={st.screen}>
      <Stack.Screen options={{ title: obj?.label || "Salesforce object" }} />
      <View style={st.head}>
        <Text style={st.title}>{obj?.label}</Text>
        <Text style={st.api}>{obj?.is_custom ? "Custom object" : "Standard object"}</Text>
        {obj ? (
          <FlagRow>
            <Flag label="Queryable" on={obj.queryable} />
            <Flag label="Createable" on={obj.createable} />
            <Flag label="Updateable" on={obj.updateable} />
            <Flag label="Deletable" on={obj.deletable} />
          </FlagRow>
        ) : null}
        {error ? <ErrorText>{error}</ErrorText> : null}
        <Button label="Refresh from Salesforce" variant="ghost" loading={busy}
          onPress={() => { void load(true); }} />
      </View>
      <FlatList
        data={fields || []}
        keyExtractor={(f) => f.api_name}
        renderItem={({ item: f }) => (
          <View style={st.row}>
            <Text style={st.label}>{f.label}</Text>
            <Text style={st.meta}>{f.reference_to.length ? "Lookup" : f.type}</Text>
            <FlagRow>
              {f.required ? <Flag label="Required" on tone="warn" /> : null}
              <Flag label="Create" on={f.createable} />
              <Flag label="Update" on={f.updateable} />
              {f.is_custom ? <Flag label="Custom" on tone="info" /> : null}
            </FlagRow>
          </View>
        )}
      />
    </View>
  );
}
