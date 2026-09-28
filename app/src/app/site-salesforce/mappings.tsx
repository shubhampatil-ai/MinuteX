// src/app/site-salesforce/mappings.tsx — the workspace's manual Task mappings.
//
// A mapping says: "a Task of this kind belongs to records of THIS Salesforce
// object". It is always chosen by a person; MinuteX never decides which object
// a meeting or task is about.
import { useCallback, useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text } from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { FONT, S, useTheme, ColorScale } from "../../../lib/theme";
import { Button, EmptyState, ErrorText, Loading } from "../../../lib/ui";
import { SiteSfMapping, listSiteSalesforceMappings } from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { Flag, FlagRow, siteSfError } from "../../../lib/site-salesforce-ui";

const PROBLEMS: Record<string, string> = {
  object_not_discovered: "Its object is no longer visible to the integration user.",
  object_not_usable: "Tasks can no longer be related to its object.",
};

function dueText(m: SiteSfMapping): string {
  if (m.due_rule?.type === "days_after_meeting") return `Due ${m.due_rule.days} days after the meeting`;
  if (m.due_rule?.type === "days_after_creation") return `Due ${m.due_rule.days} days after creation`;
  return "No default due date";
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.x4 },
    row: { paddingVertical: 13, borderBottomWidth: 1, borderBottomColor: C.border },
    name: { fontFamily: FONT.semibold, fontSize: 15, color: C.text },
    meta: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 3 },
    problem: { fontFamily: FONT.medium, fontSize: 12.5, color: C.warn, marginTop: 4 },
  });
}

export default function SiteSalesforceMappings() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId, can } = useWorkspace();
  const canManage = can("manage_integrations");
  const [mappings, setMappings] = useState<SiteSfMapping[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!activeId) return;
    try {
      setMappings(await listSiteSalesforceMappings(activeId));
      setError("");
    } catch (e) {
      setError(siteSfError(e));
    }
  }, [activeId]);
  useFocusEffect(useCallback(() => { void load(); }, [load]));

  if (mappings === null && !error) return <Loading />;

  return (
    <ScrollView style={st.screen} contentContainerStyle={st.body}>
      <Stack.Screen options={{ title: "Task mappings" }} />
      {error ? <ErrorText>{error}</ErrorText> : null}
      {canManage ? (
        <Button label="New mapping" onPress={() => router.push("/site-salesforce/mapping-edit")} />
      ) : null}
      {mappings?.length === 0 ? (
        <EmptyState icon="slider.horizontal.3" title="No Task mappings yet"
          subtitle="A mapping names the Salesforce object a kind of Task belongs to, like Site Visit or Booking." />
      ) : null}
      {(mappings || []).map((m) => (
        <Pressable key={m.mapping_id} disabled={!canManage}
          onPress={() => router.push({ pathname: "/site-salesforce/mapping-edit", params: { id: m.mapping_id } })}
          style={({ pressed }) => [st.row, pressed && { opacity: 0.7 }]}>
          <Text style={st.name}>{m.name}</Text>
          <Text style={st.meta}>{m.object_label || "Salesforce object"} · “{m.subject}”</Text>
          <Text style={st.meta}>{dueText(m)}</Text>
          <FlagRow>
            <Flag label="Record required" on={m.record_required} tone="info" />
            <Flag label="Default assignee" on={!!m.default_assignee_person_key} tone="info" />
            {!m.enabled ? <Flag label="Disabled" on tone="warn" /> : null}
          </FlagRow>
          {(m.problems || []).map((p) => (
            <Text key={p} style={st.problem}>{PROBLEMS[p] || p}</Text>
          ))}
        </Pressable>
      ))}
    </ScrollView>
  );
}
