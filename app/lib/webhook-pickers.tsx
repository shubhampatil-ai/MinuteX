// lib/webhook-pickers.tsx — the two sheets the Salesforce webhook meeting
// block opens: the customer's MEETING TYPES, and a SEARCH over the records
// the chosen type allows. Both come from the customer's own endpoint
// (docs/webhook-contract-v2.md §5-§6); MinuteX knows nothing about the
// underlying Salesforce object.
//
// Records are shown by display_name / secondary_text only. The id is carried
// back to the server but never rendered.
import { useEffect, useMemo, useState } from "react";
import { ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { searchWebhookRecords } from "./api";
import { Icon } from "./icons";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { KeyboardAwareSheet, SearchBar } from "./ui";
import {
  WebhookConfiguration, WebhookMeetingType, WebhookRecord, configStatusLine, recordLabel,
} from "./webhook-flow";
import { webhookError } from "./webhook-ui";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl,
      paddingHorizontal: 20, paddingTop: S.lg, paddingBottom: S.xl,
      maxHeight: "88%", flexShrink: 1,
    },
    head: { flexDirection: "row", alignItems: "center", justifyContent: "space-between",
            marginBottom: S.md },
    title: { fontFamily: FONT.bold, fontSize: 18, color: C.text },
    note: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim,
            marginBottom: S.md },
    warn: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.warn, marginBottom: S.md },
    item: { paddingVertical: 12, paddingHorizontal: S.md, borderRadius: R.md, minHeight: 48,
            borderWidth: 1, borderColor: C.border, marginBottom: S.sm, justifyContent: "center" },
    picked: { borderColor: C.primary, backgroundColor: C.primarySoft },
    name: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    sub: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
    err: { fontFamily: FONT.medium, fontSize: 13, color: C.danger, marginVertical: S.sm },
    link: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.primary, paddingVertical: S.sm },
  });
}

function SheetHead({ title, onClose }: { title: string; onClose: () => void }) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  return (
    <View style={st.head}>
      <Text style={st.title}>{title}</Text>
      <Pressable onPress={onClose} hitSlop={12} accessibilityRole="button"
        accessibilityLabel="Close">
        <Icon name="xmark" size={20} tintColor={C.textFaint} />
      </Pressable>
    </View>
  );
}

/** The customer's meeting types. A stale configuration is still offered
 *  (the last good list) and says so; Refresh asks Salesforce again. */
export function MeetingTypeSheet({
  visible, configuration, selectedId, onPick, onClose, onRefresh, refreshing,
}: {
  visible: boolean;
  configuration: WebhookConfiguration | null;
  selectedId: string | null;
  onPick: (t: WebhookMeetingType | null) => void;
  onClose: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const types = configuration?.meeting_types ?? [];
  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      <View style={st.backdrop}>
        <View style={st.sheet}>
          <SheetHead title="Meeting type" onClose={onClose} />
          {configuration?.status === "stale" ? (
            <Text style={st.warn}>{configStatusLine(configuration)}</Text>
          ) : (
            <Text style={st.note}>From your Salesforce org · {configStatusLine(configuration)}</Text>
          )}
          <ScrollView style={{ flexShrink: 1 }}>
            {types.length === 0 ? (
              <Text style={st.note}>Salesforce hasn&apos;t sent any meeting types yet.</Text>
            ) : null}
            {types.map((t) => (
              <Pressable key={t.id} onPress={() => onPick(t)}
                style={[st.item, selectedId === t.id && st.picked]}
                accessibilityRole="button" accessibilityState={{ selected: selectedId === t.id }}>
                <Text style={st.name}>{t.name}</Text>
                {t.description ? <Text style={st.sub}>{t.description}</Text> : null}
                {t.record_selection?.enabled ? (
                  <Text style={st.sub}>
                    {t.record_selection.required ? "Needs" : "Can link"} a {recordLabel(t)}
                  </Text>
                ) : null}
              </Pressable>
            ))}
            {selectedId ? (
              <Pressable onPress={() => onPick(null)} accessibilityRole="button">
                <Text style={st.link}>Clear meeting type</Text>
              </Pressable>
            ) : null}
            <Pressable onPress={onRefresh} disabled={refreshing} accessibilityRole="button">
              <Text style={[st.link, refreshing && { opacity: 0.5 }]}>
                {refreshing ? "Refreshing…" : "Refresh from Salesforce"}
              </Text>
            </Pressable>
          </ScrollView>
        </View>
      </View>
    </Modal>
  );
}

/** Search the records a meeting type allows ("PV-2026-00452"). The search
 *  runs on the customer's endpoint; results show display names only. */
export function RecordSearchSheet({
  visible, workspaceId, meetingType, selectedId, onPick, onClose,
}: {
  visible: boolean;
  workspaceId: string;
  meetingType: WebhookMeetingType;
  selectedId: string | null;
  onPick: (r: WebhookRecord) => void;
  onClose: () => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [q, setQ] = useState("");
  const [records, setRecords] = useState<WebhookRecord[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const label = recordLabel(meetingType);

  useEffect(() => {
    if (!visible) return undefined;
    let live = true;
    const t = setTimeout(() => {
      setLoading(true);
      searchWebhookRecords(workspaceId, meetingType.id, q)
        .then((r) => { if (live) { setRecords(r.records); setError(""); } })
        .catch((e) => { if (live) setError(webhookError(e)); })
        .finally(() => { if (live) setLoading(false); });
    }, 350);
    return () => { live = false; clearTimeout(t); };
  }, [visible, workspaceId, meetingType.id, q]);

  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      <KeyboardAwareSheet>
        <View style={st.backdrop}>
          <View style={st.sheet}>
            <SheetHead title={label} onClose={onClose} />
            <SearchBar value={q} onChangeText={setQ} autoFocus
              placeholder={meetingType.record_selection?.placeholder || `Search ${label}…`} />
            {error ? <Text style={st.err}>{error}</Text> : null}
            {loading && !records ? <ActivityIndicator style={{ margin: S.md }} /> : null}
            <ScrollView style={{ flexShrink: 1, minHeight: 0, marginTop: S.md }}
              keyboardShouldPersistTaps="handled">
              {records?.length === 0 ? (
                <Text style={st.note}>No {label} matches “{q}”.</Text>
              ) : null}
              {(records || []).map((r) => (
                <Pressable key={r.id} onPress={() => onPick(r)}
                  style={[st.item, selectedId === r.id && st.picked]}
                  accessibilityRole="button" accessibilityState={{ selected: selectedId === r.id }}
                  accessibilityLabel={`${label} ${r.display_name}`}>
                  <Text style={st.name} numberOfLines={2}>{r.display_name}</Text>
                  {r.secondary_text ? <Text style={st.sub}>{r.secondary_text}</Text> : null}
                </Pressable>
              ))}
            </ScrollView>
          </View>
        </View>
      </KeyboardAwareSheet>
    </Modal>
  );
}
