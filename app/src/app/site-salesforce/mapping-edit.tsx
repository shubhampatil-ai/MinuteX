// src/app/site-salesforce/mapping-edit.tsx — create or edit one Task mapping.
//
// Every choice on this screen comes from Salesforce: the object list is the
// discovered objects Tasks can relate to (Task.WhatId / Task.WhoId), and the
// status/priority options are the org's own Task picklist values. The server
// re-validates all of it against discovered metadata before saving.
import { useCallback, useMemo, useRef, useState } from "react";
import { Alert, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { FONT, S, useTheme, ColorScale } from "../../../lib/theme";
import { Button, Chip, ErrorText, Loading, SearchBar, SwitchRow, TextField , KeyboardAware } from "../../../lib/ui";
import {
  SiteSfDueRule, SiteSfField, SiteSfMapping, SiteSfObject, SitePerson,
  deleteSiteSalesforceMapping, getSiteSalesforceFields, listSiteSalesforceMappings,
  listSiteSalesforceObjects, saveSiteSalesforceMapping, searchSitePeople,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { siteSfError } from "../../../lib/site-salesforce-ui";
import { RecordPicker, UserPicker } from "../../../lib/site-salesforce-pickers";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.x4 },
    label: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.textDim, marginTop: S.lg, marginBottom: 6 },
    chips: { flexDirection: "row", flexWrap: "wrap", gap: S.sm },
    help: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 6 },
  });
}

function picklist(fields: SiteSfField[], name: string): string[] {
  return (fields.find((f) => f.api_name === name)?.picklist_values || []).map((v) => v.value);
}

export default function SiteSalesforceMappingEdit() {
  const { id } = useLocalSearchParams<{ id?: string }>();
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId } = useWorkspace();

  const [objects, setObjects] = useState<SiteSfObject[] | null>(null);
  const [taskFields, setTaskFields] = useState<SiteSfField[]>([]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [objectQ, setObjectQ] = useState("");

  const [name, setName] = useState("");
  const [objectName, setObjectName] = useState("");
  const [subject, setSubject] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState("");
  const [priority, setPriority] = useState("");
  const [assignee, setAssignee] = useState<SitePerson | null>(null);
  const [due, setDue] = useState<SiteSfDueRule>({ type: "none", days: 0 });
  const [recordRequired, setRecordRequired] = useState(true);
  const [defaultRecord, setDefaultRecord] = useState<{ id: string; name: string } | null>(null);

  const fill = useCallback((m: SiteSfMapping) => {
    setName(m.name); setObjectName(m.object_api_name); setSubject(m.subject);
    setDescription(m.description || ""); setStatus(m.status || ""); setPriority(m.priority || "");
    setDue(m.due_rule || { type: "none", days: 0 }); setRecordRequired(m.record_required);
    setDefaultRecord(m.default_record_id ? { id: m.default_record_id, name: m.default_record_name } : null);
    if (m.default_assignee_person_key && activeId) {
      searchSitePeople(activeId, { type: "user" })
        .then((people) => setAssignee(people.find((p) => p.person_key === m.default_assignee_person_key) || null))
        .catch(() => undefined);
    }
  }, [activeId]);

  // Loaded once per visit. The form's own edits are not overwritten by a
  // reload because nothing on this screen navigates away and back mid-edit.
  const loaded = useRef(false);
  const load = useCallback(async () => {
    if (!activeId || loaded.current) return;
    loaded.current = true;
    try {
      const [objs, task] = await Promise.all([
        listSiteSalesforceObjects(activeId, { capability: "task", scope: "all", limit: 1000 }),
        getSiteSalesforceFields(activeId, "Task"),
      ]);
      setObjects(objs);
      setTaskFields(task.fields);
      if (id) {
        const m = (await listSiteSalesforceMappings(activeId)).find((x) => x.mapping_id === id);
        if (m) fill(m);
      }
    } catch (e) {
      setError(siteSfError(e));
      setObjects((o) => o ?? []);
    }
  }, [activeId, id, fill]);
  useFocusEffect(useCallback(() => { void load(); }, [load]));

  const chosen = objects?.find((o) => o.api_name === objectName) || null;
  const visibleObjects = (objects || []).filter((o) => {
    const n = objectQ.trim().toLowerCase();
    return !n || o.label.toLowerCase().includes(n) || o.api_name.toLowerCase().includes(n);
  });

  const save = async () => {
    setSaving(true); setError("");
    try {
      await saveSiteSalesforceMapping(activeId, {
        name: name.trim(), object_api_name: objectName, subject: subject.trim(),
        description, status, priority,
        default_assignee_person_key: assignee?.person_key || "",
        due_rule: due, record_required: recordRequired,
        default_record_id: defaultRecord?.id || "", default_record_name: defaultRecord?.name || "",
      }, id ? String(id) : undefined);
      router.back();
    } catch (e) {
      setError(siteSfError(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = () => {
    if (!id) return;
    Alert.alert("Delete this mapping?", "Tasks already created in Salesforce are not affected.", [
      { text: "Cancel", style: "cancel" },
      { text: "Delete", style: "destructive", onPress: async () => {
        try { await deleteSiteSalesforceMapping(activeId, String(id)); router.back(); }
        catch (e) { setError(siteSfError(e)); }
      } },
    ]);
  };

  if (objects === null) return <Loading />;

  const statuses = picklist(taskFields, "Status");
  const priorities = picklist(taskFields, "Priority");

  return (
    <KeyboardAware>
      <ScrollView style={st.screen} contentContainerStyle={st.body} keyboardShouldPersistTaps="handled">
      <Stack.Screen options={{ title: id ? "Edit mapping" : "New mapping" }} />
      {error ? <ErrorText>{error}</ErrorText> : null}

      <Text style={st.label}>Mapping name</Text>
      <TextField value={name} onChangeText={setName} placeholder="Follow-up Call" />

      <Text style={st.label}>Salesforce object</Text>
      {chosen ? (
        <View style={st.chips}>
          <Chip label={chosen.label} active onPress={() => setObjectName("")} />
        </View>
      ) : (
        <>
          <SearchBar value={objectQ} onChangeText={setObjectQ} placeholder="Search objects Tasks can relate to…" />
          <View style={[st.chips, { marginTop: S.sm }]}>
            {visibleObjects.slice(0, 30).map((o) => (
              <Chip key={o.api_name} label={o.label} onPress={() => { setObjectName(o.api_name); setDefaultRecord(null); }} />
            ))}
          </View>
          <Text style={st.help}>
            Only objects your integration user can read and that Salesforce lets Tasks relate to are listed.
          </Text>
        </>
      )}

      <Text style={st.label}>Task subject</Text>
      <TextField value={subject} onChangeText={setSubject} placeholder="Follow-up Call" />
      <Text style={st.label}>Description (optional)</Text>
      <TextField value={description} onChangeText={setDescription} multiline style={{ minHeight: 70 }} />

      {statuses.length ? (
        <>
          <Text style={st.label}>Status</Text>
          <View style={st.chips}>
            {statuses.map((v) => <Chip key={v} label={v} active={status === v} onPress={() => setStatus(status === v ? "" : v)} />)}
          </View>
        </>
      ) : null}
      {priorities.length ? (
        <>
          <Text style={st.label}>Priority</Text>
          <View style={st.chips}>
            {priorities.map((v) => <Chip key={v} label={v} active={priority === v} onPress={() => setPriority(priority === v ? "" : v)} />)}
          </View>
        </>
      ) : null}

      <Text style={st.label}>Default assignee (optional)</Text>
      {assignee ? <Text style={st.help}>Selected: {assignee.name}</Text> : null}
      <UserPicker workspaceId={activeId} value={assignee} onChange={setAssignee} />

      <Text style={st.label}>Due date</Text>
      <View style={st.chips}>
        <Chip label="None" active={due.type === "none"} onPress={() => setDue({ type: "none", days: 0 })} />
        <Chip label="After the meeting" active={due.type === "days_after_meeting"}
          onPress={() => setDue({ type: "days_after_meeting", days: due.days || 7 })} />
        <Chip label="After creation" active={due.type === "days_after_creation"}
          onPress={() => setDue({ type: "days_after_creation", days: due.days || 7 })} />
      </View>
      {due.type !== "none" ? (
        <TextField value={String(due.days)} keyboardType="number-pad" style={{ marginTop: S.sm }}
          onChangeText={(t) => setDue({ ...due, days: Math.min(365, parseInt(t.replace(/\D/g, "") || "0", 10)) })} />
      ) : null}

      <View style={{ marginTop: S.lg }}>
        <SwitchRow label="Require a Salesforce record" value={recordRequired} onValueChange={setRecordRequired}
          sub="When on, a Task is never created without the record it belongs to." />
      </View>

      {chosen ? (
        <>
          <Text style={st.label}>Default {chosen.label} record (optional)</Text>
          {defaultRecord ? <Text style={st.help}>Selected: {defaultRecord.name}</Text> : null}
          <RecordPicker workspaceId={activeId} objectApiName={chosen.api_name} objectLabel={chosen.label}
            value={defaultRecord} onChange={setDefaultRecord} />
        </>
      ) : null}

      <Button label="Save mapping" onPress={() => { void save(); }} loading={saving}
        disabled={!name.trim() || !objectName || !subject.trim()} style={{ marginTop: S.xl }} />
      {id ? <Button label="Delete mapping" variant="destructive" onPress={remove} style={{ marginTop: S.sm }} /> : null}
      </ScrollView>
    </KeyboardAware>
  );
}
