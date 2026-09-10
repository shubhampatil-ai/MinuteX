// src/app/org-salesforce-config.tsx — map the ORGANISATION onto its own
// Salesforce schema. Workspace-scoped twin of salesforce-config.tsx
// (Personal) — deliberately its own file rather than a shared component
// parameterized by scope, so Personal's config screen stays exactly as it
// was: same file, same behaviour, zero risk of a workspace conditional
// leaking into a path Personal already relies on. Every mapping/validation
// RULE here is identical to Personal's (see the backend's _validate_mapping,
// which both org_salesforce_put_config and salesforce_put_config call
// through) — only the API calls and the reconnect destination differ.
//
// OWNER/MANAGER ONLY. This screen is reached only from org-salesforce.tsx's
// "Salesforce mapping" row, which itself only renders as tappable when
// can("manage_integrations") is true — but the guard below is repeated here
// too, because a Member could still navigate here directly (a stale deep
// link, a back-button race) and the backend's 403 on save would otherwise be
// the first they hear of it. Never the ONLY enforcement — org_salesforce_
// put_config re-checks CAP_MANAGE_INTEGRATIONS server-side regardless.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator, Alert, Modal, Pressable, ScrollView, StyleSheet, Text,
  TextInput, View,
} from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { S, R, FONT, useTheme, ColorScale } from "../../lib/theme";
import {
  Button, Card, ErrorText, Loading, Masthead, SectionRule,
  KeyboardAware,
  KeyboardAwareSheet,
} from "../../lib/ui";
import { Icon } from "../../lib/icons";
import {
  ApiError, SalesforceConfig, SalesforceField, SalesforceFieldsResponse,
  SalesforceMappingInput, SalesforceObject, clearToken,
  getOrgSalesforceConfig, getOrgSalesforceFields, getOrgSalesforceObjects,
  isSalesforceReconnect, saveOrgSalesforceConfig,
} from "../../lib/api";
import { useWorkspace, workspaceLabel } from "../../lib/workspace-context";

// Mirrors CRM_DATA_TARGETS in lambda-userapi — same keys, same order, same
// as Personal's salesforce-config.tsx.
const DATA_TARGETS = [
  { key: "transcript_field", label: "Transcript" },
  { key: "summary_field", label: "Summary" },
  { key: "highlights_field", label: "Highlights" },
  { key: "action_items_field", label: "Action Items" },
] as const;

type TargetKey = (typeof DATA_TARGETS)[number]["key"];

type Draft = {
  object: string;
  objectLabel: string;
  lookupField: string;
  label: string;
  targets: Partial<Record<TargetKey, string>>;
  schema: SalesforceFieldsResponse | null;
};

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    blurb: {
      fontFamily: FONT.regular, fontSize: 13, lineHeight: 19, color: C.textFaint,
      marginTop: 2, marginBottom: 4,
    },
    mapHead: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
      paddingBottom: 10, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    mapTitle: { flex: 1, fontFamily: FONT.extrabold, fontSize: 15.5, color: C.text },
    mapApi: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint, marginTop: 2 },
    pickRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      paddingVertical: 12, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    pickBody: { flex: 1 },
    pickLabel: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.text },
    pickValue: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 3 },
    pickEmpty: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint, marginTop: 3,
      fontStyle: "italic" as const,
    },
    required: { fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 1, color: C.textFaint },
    empty: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textFaint,
      paddingVertical: 6,
    },
    addRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 9,
      paddingVertical: 14,
    },
    addTxt: { fontFamily: FONT.semibold, fontSize: 14, color: C.primary },
    removeTxt: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.danger },
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" as const },
    sheet: {
      backgroundColor: C.surface, paddingTop: 18,
      borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl, maxHeight: "82%",
    },
    sheetHead: { paddingHorizontal: 22 },
    sheetTitle: { fontFamily: FONT.extrabold, fontSize: 19, color: C.text },
    sheetSub: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18,
      color: C.textDim, marginTop: 5,
    },
    search: {
      backgroundColor: C.surface2, borderRadius: R.pill, marginTop: 14,
      paddingHorizontal: S.md, paddingVertical: 9,
      fontFamily: FONT.regular, fontSize: 14.5, color: C.text,
    },
    optRow: {
      paddingVertical: 13, paddingHorizontal: 22, borderBottomWidth: 1,
      borderBottomColor: C.border, flexDirection: "row" as const,
      alignItems: "center" as const, gap: 10,
    },
    optLabel: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    optMeta: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint, marginTop: 2 },
    optNone: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.textDim },
    emptyList: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textFaint,
      paddingHorizontal: 22, paddingVertical: 22,
    },
  });
}

function fieldMeta(f: SalesforceField): string {
  const kind = f.type === "richtextarea" ? "Rich text"
    : f.type === "textarea" ? "Long text"
    : f.type === "string" ? "Text"
    : f.type.charAt(0).toUpperCase() + f.type.slice(1);
  const size = f.length ? ` · ${f.length.toLocaleString()} chars` : "";
  return `${f.name} · ${kind}${size}`;
}

type Picker =
  | { kind: "object"; forIndex: number | "new" }
  | { kind: "lookup"; forIndex: number }
  | { kind: "target"; forIndex: number; target: TargetKey; label: string }
  | null;

export default function OrgSalesforceConfigScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);
  const { active, activeId, isOrganisation, can, loading: wsLoading } = useWorkspace();

  const [objects, setObjects] = useState<SalesforceObject[]>([]);
  const [suggestedObject, setSuggestedObject] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [picker, setPicker] = useState<Picker>(null);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [describing, setDescribing] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const canManage = can("manage_integrations");

  const on401 = useCallback(async () => {
    await clearToken();
    router.replace("/login");
  }, [router]);

  const onSalesforceReconnect = useCallback(() => {
    router.replace("/org-salesforce");
  }, [router]);

  const handleAuthError = useCallback((e: unknown): boolean => {
    if (isSalesforceReconnect(e)) { onSalesforceReconnect(); return true; }
    if (e instanceof ApiError && e.status === 401) { void on401(); return true; }
    return false;
  }, [on401, onSalesforceReconnect]);

  const adoptConfig = useCallback((cfg: SalesforceConfig | null) => {
    if (!cfg) return;
    setDrafts((cfg.mappings ?? []).map((m) => ({
      object: m.object,
      objectLabel: m.object_label || m.object,
      lookupField: m.lookup_field,
      label: m.label || "",
      targets: {
        transcript_field: m.transcript_field ?? undefined,
        summary_field: m.summary_field ?? undefined,
        highlights_field: m.highlights_field ?? undefined,
        action_items_field: m.action_items_field ?? undefined,
      },
      schema: null,
    })));
  }, []);

  useEffect(() => {
    if (wsLoading) return;
    if (!activeId || !isOrganisation) { setLoading(false); return; }
    let cancelled = false;
    (async () => {
      try {
        const [objRes, cfg] = await Promise.all([
          getOrgSalesforceObjects(activeId),
          getOrgSalesforceConfig(activeId).catch(() => null),
        ]);
        if (cancelled) return;
        setObjects(objRes.objects);
        setSuggestedObject(objRes.suggested);
        adoptConfig(cfg);
      } catch (e) {
        if (cancelled) return;
        if (handleAuthError(e)) return;
        setError(e instanceof ApiError ? e.message
          : "Could not read your Salesforce schema.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [handleAuthError, adoptConfig, activeId, isOrganisation, wsLoading]);

  const ensureSchema = useCallback(async (index: number): Promise<SalesforceFieldsResponse | null> => {
    const draft = drafts[index];
    if (!draft || !activeId) return null;
    if (draft.schema) return draft.schema;
    setDescribing(index);
    try {
      const res = await getOrgSalesforceFields(activeId, draft.object);
      setDrafts((prev) => prev.map((d, i) => {
        if (i !== index) return d;
        const targets = { ...d.targets };
        for (const t of DATA_TARGETS) {
          if (!targets[t.key]) targets[t.key] = res.suggested?.[t.key] ?? undefined;
        }
        return {
          ...d,
          schema: res,
          lookupField: d.lookupField || res.suggested?.lookup_field || "",
          targets,
        };
      }));
      return res;
    } catch (e) {
      if (handleAuthError(e)) return null;
      setError(e instanceof ApiError ? e.message
        : "Could not read that object's fields.");
      return null;
    } finally {
      setDescribing(null);
    }
  }, [drafts, handleAuthError, activeId]);

  const ensureSchemaByObject = useCallback(async (objectName: string) => {
    setDrafts((prev) => {
      const index = prev.findIndex((d) => d.object === objectName);
      if (index >= 0 && !prev[index].schema) void ensureSchema(index);
      return prev;
    });
  }, [ensureSchema]);

  const addMapping = (objectName: string) => {
    const obj = objects.find((o) => o.name === objectName);
    setDrafts((prev) => [...prev, {
      object: objectName,
      objectLabel: obj?.label || objectName,
      lookupField: "",
      label: "",
      targets: {},
      schema: null,
    }]);
    setTimeout(() => { void ensureSchemaByObject(objectName); }, 0);
  };

  const removeMapping = (index: number) => {
    const d = drafts[index];
    Alert.alert(
      `Remove ${d.objectLabel}?`,
      "Meetings will stop showing this record field. Nothing already in "
      + "Salesforce is changed.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Remove", style: "destructive",
          onPress: () => setDrafts((prev) => prev.filter((_, i) => i !== index)),
        },
      ]
    );
  };

  const incomplete = drafts.filter((d) => !d.lookupField);
  const canSave = incomplete.length === 0 && !saving && describing === null && canManage;

  const save = async () => {
    if (!canSave || !activeId) return;
    setSaving(true); setError("");
    try {
      const mappings: SalesforceMappingInput[] = drafts.map((d) => ({
        object: d.object,
        lookup_field: d.lookupField,
        label: d.label || undefined,
        transcript_field: d.targets.transcript_field || null,
        summary_field: d.targets.summary_field || null,
        highlights_field: d.targets.highlights_field || null,
        action_items_field: d.targets.action_items_field || null,
      }));
      await saveOrgSalesforceConfig(activeId, { mappings });
      router.back();
    } catch (e) {
      if (handleAuthError(e)) return;
      setError(e instanceof ApiError ? e.message
        : "Could not save the configuration.");
    } finally {
      setSaving(false);
    }
  };

  const openPicker = async (p: Picker) => {
    setSearch("");
    if (p && p.kind !== "object") await ensureSchema(p.forIndex);
    setPicker(p);
  };

  const options: { value: string; label: string; meta: string }[] = useMemo(() => {
    if (!picker) return [];
    if (picker.kind === "object") {
      const already = new Set(drafts.map((d) => d.object));
      return objects
        .filter((o) => !already.has(o.name))
        .map((o) => ({
          value: o.name,
          label: o.label,
          meta: o.custom ? `${o.name} · Custom object` : `${o.name} · Standard object`,
        }));
    }
    const draft = drafts[picker.forIndex];
    if (!draft?.schema) return [];
    if (picker.kind === "lookup") {
      return draft.schema.number_fields.map((f) => ({
        value: f.name, label: f.label, meta: fieldMeta(f),
      }));
    }
    const taken = new Set(
      DATA_TARGETS.filter((t) => t.key !== picker.target)
        .map((t) => draft.targets[t.key])
        .filter((v): v is string => !!v)
    );
    return draft.schema.long_text_fields
      .filter((f) => !taken.has(f.name))
      .map((f) => ({ value: f.name, label: f.label, meta: fieldMeta(f) }));
  }, [picker, objects, drafts]);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) => o.label.toLowerCase().includes(q) || o.meta.toLowerCase().includes(q)
    );
  }, [options, search]);

  const choose = (value: string | null) => {
    if (!picker) return;
    if (picker.kind === "object") {
      if (value) addMapping(value);
    } else if (picker.kind === "lookup") {
      if (value) {
        setDrafts((prev) => prev.map((d, i) =>
          i === picker.forIndex ? { ...d, lookupField: value } : d));
      }
    } else {
      setDrafts((prev) => prev.map((d, i) =>
        i === picker.forIndex
          ? { ...d, targets: { ...d.targets, [picker.target]: value ?? undefined } }
          : d));
    }
    setPicker(null);
  };

  const fieldLabel = (index: number, name?: string, pool?: "lookup" | "long") => {
    if (!name) return null;
    const schema = drafts[index]?.schema;
    const list = pool === "lookup" ? schema?.number_fields : schema?.long_text_fields;
    const f = list?.find((x) => x.name === name);
    return f ? `${f.label} (${f.name})` : name;
  };

  if (wsLoading || loading) {
    return (
      <View style={[st.container, { paddingTop: insets.top + S.lg }]}>
        <Loading label="Reading your Salesforce schema…" />
      </View>
    );
  }
  if (!isOrganisation) {
    return (
      <View style={[st.container, { paddingTop: insets.top + S.lg }]}>
        <Masthead kicker="Organisation" title="Salesforce mapping" />
        <ErrorText>
          Switch to an organisation workspace to manage its Salesforce mapping.
        </ErrorText>
      </View>
    );
  }
  if (!canManage) {
    return (
      <View style={[st.container, { paddingTop: insets.top + S.lg }]}>
        <Masthead kicker={workspaceLabel(active)} title="Salesforce mapping" />
        <ErrorText>
          Only an owner or manager can configure this organisation's
          Salesforce mapping.
        </ErrorText>
      </View>
    );
  }

  return (
    <KeyboardAware>
      <ScrollView
        style={[st.container, { paddingTop: insets.top + S.lg }]}
        contentContainerStyle={{ paddingBottom: 120 }}
        showsVerticalScrollIndicator={false}
        keyboardShouldPersistTaps="handled"
      >
        <Masthead
          kicker={drafts.length
            ? `${drafts.length} record${drafts.length === 1 ? "" : "s"} · ${workspaceLabel(active)}`
            : `No records configured · ${workspaceLabel(active)}`}
          title="Salesforce mapping"
        />
        {error ? <ErrorText>{error}</ErrorText> : null}

        <Text style={st.blurb}>
          Choose which Salesforce records this organisation's meetings relate
          to. Each one adds a field on the meeting screen for its identifier
          — configure none and meetings show no Salesforce fields at all.
        </Text>

        {drafts.length === 0 ? (
          <Card>
            <Text style={st.empty}>
              Nothing configured yet. Add the record type your meetings are
              about — a site visit, a lead, an opportunity, or any other object
              in your org.
            </Text>
          </Card>
        ) : null}

        {drafts.map((d, index) => (
          <View key={d.object} style={{ marginTop: S.lg }}>
            <SectionRule>{d.objectLabel}</SectionRule>
            <Card>
              <View style={st.mapHead}>
                <View style={{ flex: 1 }}>
                  <Text style={st.mapTitle}>{d.objectLabel}</Text>
                  <Text style={st.mapApi}>{d.object}</Text>
                </View>
                <Pressable onPress={() => removeMapping(index)} hitSlop={8}>
                  <Text style={st.removeTxt}>Remove</Text>
                </Pressable>
              </View>

              <Pressable
                style={st.pickRow}
                onPress={() => openPicker({ kind: "lookup", forIndex: index })}
              >
                <View style={st.pickBody}>
                  <Text style={st.pickLabel}>Identified by</Text>
                  {describing === index && !d.schema ? (
                    <Text style={st.pickEmpty}>Loading fields…</Text>
                  ) : d.lookupField ? (
                    <Text style={st.pickValue}>
                      {fieldLabel(index, d.lookupField, "lookup")}
                    </Text>
                  ) : (
                    <Text style={st.pickEmpty}>Not selected</Text>
                  )}
                </View>
                <Text style={st.required}>REQUIRED</Text>
                {describing === index && !d.schema
                  ? <ActivityIndicator size="small" color={C.textFaint} />
                  : <Icon name="chevron.right" tintColor={C.textFaint} size={15} />}
              </Pressable>

              {DATA_TARGETS.map((t, i) => (
                <Pressable
                  key={t.key}
                  style={[st.pickRow, i === DATA_TARGETS.length - 1 && { borderBottomWidth: 0 }]}
                  onPress={() => openPicker({
                    kind: "target", forIndex: index, target: t.key, label: t.label,
                  })}
                >
                  <View style={st.pickBody}>
                    <Text style={st.pickLabel}>{t.label}</Text>
                    {d.targets[t.key] ? (
                      <Text style={st.pickValue}>
                        {fieldLabel(index, d.targets[t.key], "long")}
                      </Text>
                    ) : (
                      <Text style={st.pickEmpty}>Not synced</Text>
                    )}
                  </View>
                  <Icon name="chevron.right" tintColor={C.textFaint} size={15} />
                </Pressable>
              ))}
            </Card>
          </View>
        ))}

        <Pressable
          style={st.addRow}
          onPress={() => openPicker({ kind: "object", forIndex: "new" })}
        >
          <Icon name="plus" tintColor={C.primary} size={16} />
          <Text style={st.addTxt}>
            {drafts.length ? "Add another record type" : "Add a record type"}
          </Text>
        </Pressable>

        {suggestedObject && !drafts.some((d) => d.object === suggestedObject) ? (
          <Text style={st.blurb}>
            Tip: your org has{" "}
            {objects.find((o) => o.name === suggestedObject)?.label || suggestedObject},
            which looks like a good fit.
          </Text>
        ) : null}

        <Button
          label={saving ? "Saving…" : "Save mapping"}
          loading={saving}
          disabled={!canSave}
          onPress={save}
          style={{ marginTop: S.xl }}
        />
        {incomplete.length ? (
          <Text style={st.blurb}>
            Pick an “Identified by” field for {incomplete[0].objectLabel} to save.
          </Text>
        ) : (
          <Text style={st.blurb}>
            {drafts.length === 0
              ? "Saving with nothing configured hides Salesforce fields from meetings."
              : " "}
          </Text>
        )}
      </ScrollView>

      <Modal
        visible={!!picker}
        transparent
        animationType="slide"
        onRequestClose={() => setPicker(null)}
      >
        <KeyboardAwareSheet>
          <Pressable style={st.backdrop} onPress={() => setPicker(null)}>
            <Pressable style={st.sheet} onPress={() => {}}>
              <View style={st.sheetHead}>
                <Text style={st.sheetTitle}>
                  {picker?.kind === "object" ? "Choose a record type"
                    : picker?.kind === "lookup" ? "Identified by"
                    : picker?.label}
                </Text>
                <Text style={st.sheetSub}>
                  {picker?.kind === "object"
                    ? "Read from your Salesforce org. Custom objects are listed first."
                    : picker?.kind === "lookup"
                      ? "Only searchable fields are listed — MinuteX filters on this to find the record."
                      : "Only writable long text fields are listed, so your content can't be truncated."}
                </Text>
                <TextInput
                  style={st.search}
                  value={search}
                  onChangeText={setSearch}
                  placeholder="Search…"
                  placeholderTextColor={C.textFaint}
                  autoCorrect={false}
                />
              </View>
              <ScrollView keyboardShouldPersistTaps="handled">
                {picker?.kind === "target" ? (
                  <Pressable style={st.optRow} onPress={() => choose(null)}>
                    <View style={{ flex: 1 }}>
                      <Text style={st.optNone}>Don&apos;t sync this</Text>
                    </View>
                    {!drafts[picker.forIndex]?.targets[picker.target] ? (
                      <Icon name="checkmark" tintColor={C.primary} size={16} />
                    ) : null}
                  </Pressable>
                ) : null}
                {shown.map((o) => {
                  const draft = picker && picker.kind !== "object"
                    ? drafts[picker.forIndex] : null;
                  const selected = picker?.kind === "lookup"
                    ? draft?.lookupField === o.value
                    : picker?.kind === "target"
                      ? draft?.targets[picker.target] === o.value
                      : false;
                  return (
                    <Pressable key={o.value} style={st.optRow} onPress={() => choose(o.value)}>
                      <View style={{ flex: 1 }}>
                        <Text style={st.optLabel}>{o.label}</Text>
                        <Text style={st.optMeta}>{o.meta}</Text>
                      </View>
                      {selected ? <Icon name="checkmark" tintColor={C.primary} size={16} /> : null}
                    </Pressable>
                  );
                })}
                {shown.length === 0 ? (
                  <Text style={st.emptyList}>
                    {search.trim() ? "Nothing matches that search."
                      : picker?.kind === "object"
                        ? "Every available object is already configured."
                        : picker?.kind === "target"
                          ? "This object has no writable long text fields. Add a Long Text Area field in Salesforce Setup."
                          : "No searchable fields on this object."}
                  </Text>
                ) : null}
                <View style={{ height: 28 }} />
              </ScrollView>
            </Pressable>
          </Pressable>
        </KeyboardAwareSheet>
      </Modal>
    </KeyboardAware>
  );
}
