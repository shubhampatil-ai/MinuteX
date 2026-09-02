// lib/meeting-crm-records.tsx — the CRM record associations on Overview.
//
// CONFIGURATION-DRIVEN, with no object hardcoded. This renders one row per
// mapping in the user's Salesforce configuration, whatever those mappings are:
//
//   mappings: []                          -> renders NOTHING
//   [{object: "SiteVisit__c", label: "Site Visit Number"}]
//                                         -> "Site Visit"  [Site Visit Number]
//   [{object: "Lead", ...}, {object: "Opportunity", ...}]
//                                         -> both, in order, each independent
//
// A customer adding an object to their configuration makes it appear here with
// no code change, and there is deliberately no `if (object === ...)` anywhere.
//
// The row's affordances come from ONE piece of state — the entry's `status` —
// so a new Salesforce object needs no new UI branch either:
//
//   not_linked/lookup_pending  enter or fix the identifier
//   ambiguous                  choose among candidates
//   record_found               Confirm & Sync (confirmation gates the push)
//   confirmed                  Sync now
//   syncing                    in-flight, no actions
//   synced                     done, with a re-sync affordance
//   failed                     the reason, and Retry
//
// Confirmation is load-bearing: nothing syncs without an explicit user action,
// because notes written onto the wrong record are worse than no notes.
import { useMemo, useState } from "react";
import {
  ActivityIndicator, Modal, Pressable,
  StyleSheet, Text, TextInput, View,
} from "react-native";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { Button, Card, KeyboardAwareSheet, SectionRule, StatusPill } from "./ui";
import { Icon, type IconName } from "./icons";
import type { CrmCandidate, CrmMapping, CrmRecordValue, CrmSyncStatus } from "./api";

const VALUE_MAX = 255;

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    row: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md },
    iconWrap: {
      width: 38, height: 38, borderRadius: 13,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    body: { flex: 1 },
    label: { fontFamily: FONT.medium, fontSize: 11.5, color: C.textFaint, letterSpacing: 0.3 },
    value: { fontFamily: FONT.extrabold, fontSize: 18, color: C.text, marginTop: 2 },
    absent: { fontFamily: FONT.semibold, fontSize: 15, color: C.textDim, marginTop: 2 },
    recordLabel: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 3 },
    actionTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary },
    dangerTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.danger },
    badgeRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      gap: 7, marginTop: 8, flexWrap: "wrap" as const,
    },
    evidence: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 19, color: C.textDim,
      fontStyle: "italic" as const, marginTop: 10,
      borderLeftWidth: 2, borderLeftColor: C.border, paddingLeft: 10,
    },
    note: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 19,
      color: C.textFaint, marginTop: 8,
    },
    errorNote: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 19,
      color: C.danger, marginTop: 8,
    },
    actionsRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      gap: S.lg, marginTop: S.md, flexWrap: "wrap" as const,
    },
    syncRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 8, marginTop: S.md },
    divider: { height: 1, backgroundColor: C.border, marginVertical: S.lg },
    candidate: {
      paddingVertical: 13, borderBottomWidth: 1, borderBottomColor: C.border,
      flexDirection: "row" as const, alignItems: "center" as const, gap: 10,
    },
    candidateName: { flex: 1, fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    candidateId: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint, marginTop: 2 },
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" as const },
    sheet: {
      backgroundColor: C.surface, paddingHorizontal: 22, paddingTop: 20, paddingBottom: 32,
      borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl, maxHeight: "82%",
    },
    sheetTitle: { fontFamily: FONT.extrabold, fontSize: 20, lineHeight: 26, color: C.text, marginTop: 6 },
    sheetBlurb: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim, marginTop: 8,
    },
    input: {
      backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      fontFamily: FONT.regular, fontSize: 17, color: C.text,
      paddingVertical: 12, paddingHorizontal: 14, marginTop: 18,
    },
  });
}

// One presentation per status. A single table rather than scattered conditions,
// so adding a status is one entry and adding an OBJECT is nothing at all.
function statusChip(status: CrmSyncStatus, C: ColorScale):
    { label: string; color: string } | null {
  switch (status) {
    case "record_found": return { label: "Record found", color: C.accent };
    case "ambiguous": return { label: "Several matches", color: C.warn };
    case "confirmed": return { label: "Confirmed", color: C.primary };
    case "syncing": return { label: "Syncing", color: C.primary };
    case "synced": return { label: "Synced", color: C.success };
    case "failed": return { label: "Sync failed", color: C.danger };
    case "lookup_pending": return { label: "Not matched", color: C.warn };
    default: return null;
  }
}

function statusIcon(status: CrmSyncStatus): IconName {
  switch (status) {
    case "synced": return "checkmark";
    case "failed": return "exclamationmark.triangle";
    case "ambiguous": return "exclamationmark.triangle";
    default: return "number";
  }
}

// How the identifier got here. Manual outranks any extraction, so it is stated
// plainly rather than dressed up as AI output.
function provenance(v: CrmRecordValue): { label: string; tone: "ai" | "manual" } | null {
  if (!v.lookup_value) return null;
  if (v.source === "manual" || v.confidence === "manual") {
    return { label: "You entered this", tone: "manual" };
  }
  if (v.confidence === "explicit") return { label: "AI detected", tone: "ai" };
  if (v.confidence === "probable") return { label: "AI detected · likely", tone: "ai" };
  return null;
}

// The keyboard that suits the configured field, driven by the Salesforce field
// type — so an email lookup gets an email keyboard without this file knowing
// what a Lead is.
function keyboardFor(type?: string): "default" | "email-address" | "numeric" | "phone-pad" | "url" {
  switch ((type || "").toLowerCase()) {
    case "email": return "email-address";
    case "phone": return "phone-pad";
    case "url": return "url";
    case "double": case "int": case "currency": case "percent": return "numeric";
    default: return "default";
  }
}

function CrmRecordRow({
  mapping, current, candidates, onSave, onChoose, onConfirm, onSync, showDivider,
}: {
  mapping: CrmMapping;
  current?: CrmRecordValue | null;
  candidates?: CrmCandidate[];
  onSave: (object: string, value: string | null) => Promise<void>;
  onChoose: (object: string, candidate: CrmCandidate) => Promise<void>;
  onConfirm: (object: string) => Promise<void>;
  onSync: (object: string) => Promise<void>;
  showDivider: boolean;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [showEvidence, setShowEvidence] = useState(false);

  const value = current?.lookup_value ?? "";
  const status: CrmSyncStatus = current?.status ?? "not_linked";
  const hasValue = !!value;
  const prov = current ? provenance(current) : null;
  const chip = statusChip(status, C);
  const hasEvidence = !!current?.evidence;
  // The label comes from the configuration, never from a constant here.
  const fieldLabel = mapping.label || mapping.lookup_field_label || mapping.lookup_field;
  // What a sync will actually write — only the configured targets.
  const targetCount = Object.keys(mapping.content_targets ?? {}).length;

  // Seed the draft when the sheet OPENS rather than in an effect watching the
  // current value: an effect would re-seed on every change (including the one
  // our own save causes) and could wipe what the user is typing.
  const openSheet = () => {
    setDraft(value);
    setSheetOpen(true);
  };

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); } catch { /* the context already alerted */ }
    finally { setBusy(false); }
  };

  const commit = async (next: string | null) => {
    setBusy(true);
    try {
      await onSave(mapping.object, next);
      setSheetOpen(false);
    } catch {
      // Keep the sheet open so the typed value isn't lost and the user can retry.
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {showDivider ? <View style={st.divider} /> : null}
      <View style={st.row}>
        <View style={[st.iconWrap, {
          backgroundColor: status === "synced" ? C.successSoft
            : status === "failed" ? C.dangerSoft
            : hasValue ? C.primarySoft : C.surface2,
        }]}>
          <Icon
            name={statusIcon(status)}
            tintColor={status === "synced" ? C.success
              : status === "failed" ? C.danger
              : hasValue ? C.primary : C.textFaint}
            size={18}
          />
        </View>
        <View style={st.body}>
          <Text style={st.label}>
            {(mapping.object_label || mapping.object).toUpperCase()}
          </Text>
          {hasValue ? (
            <Text style={st.value} numberOfLines={2}>{value}</Text>
          ) : (
            <Text style={st.absent}>Not identified</Text>
          )}
          {current?.record_label && current.record_label !== value ? (
            <Text style={st.recordLabel}>{current.record_label}</Text>
          ) : null}
        </View>
      </View>

      {chip || prov ? (
        <View style={st.badgeRow}>
          {chip ? <StatusPill label={chip.label} color={chip.color} /> : null}
          {prov ? (
            <StatusPill
              label={prov.label}
              color={prov.tone === "manual" ? C.success : C.accent}
            />
          ) : null}
          {hasEvidence ? (
            <Pressable onPress={() => setShowEvidence((v) => !v)} hitSlop={6}>
              <Text style={st.actionTxt}>{showEvidence ? "Hide" : "Why this?"}</Text>
            </Pressable>
          ) : null}
        </View>
      ) : null}

      {/* The transcript sentence the value came from — the audit trail that
          makes a wrong extraction catchable BEFORE anything is pushed. */}
      {hasValue && showEvidence && hasEvidence ? (
        <Text style={st.evidence}>“{current?.evidence}”</Text>
      ) : null}

      {status === "lookup_pending" && hasValue ? (
        <Text style={st.note}>
          No matching {mapping.object_label || mapping.object} in Salesforce.
          Check the {fieldLabel.toLowerCase()} and try again.
        </Text>
      ) : null}
      {status === "failed" && current?.error ? (
        <Text style={st.errorNote}>{current.error}</Text>
      ) : null}
      {!hasValue ? (
        <Text style={st.note}>
          No {fieldLabel.toLowerCase()} was mentioned in this meeting.
        </Text>
      ) : null}
      {status === "synced" && targetCount === 0 ? (
        <Text style={st.note}>
          Linked, but no content fields are configured for this record.
        </Text>
      ) : null}

      {status === "ambiguous" && candidates?.length ? (
        <View style={{ marginTop: S.md }}>
          <Text style={st.note}>
            {candidates.length} records share that {fieldLabel.toLowerCase()}.
            Pick the right one:
          </Text>
          {candidates.map((c) => (
            <Pressable
              key={c.record_id}
              style={st.candidate}
              disabled={busy}
              onPress={() => run(() => onChoose(mapping.object, c))}
            >
              <View style={{ flex: 1 }}>
                <Text style={st.candidateName}>{c.display_name}</Text>
                <Text style={st.candidateId}>{c.record_id}</Text>
              </View>
              <Icon name="chevron.right" tintColor={C.textFaint} size={14} />
            </Pressable>
          ))}
        </View>
      ) : null}

      {status === "syncing" ? (
        <View style={st.syncRow}>
          <ActivityIndicator size="small" color={C.primary} />
          <Text style={st.note}>Writing to Salesforce…</Text>
        </View>
      ) : null}

      {/* Confirm & Sync is the gate: it appears only once a record is resolved,
          and a push never happens without it. */}
      {status === "record_found" ? (
        <Button
          label={targetCount
            ? `Confirm & sync ${targetCount} field${targetCount === 1 ? "" : "s"}`
            : "Confirm record"}
          loading={busy}
          disabled={busy}
          onPress={() => run(async () => {
            await onConfirm(mapping.object);
            if (targetCount) await onSync(mapping.object);
          })}
          style={{ marginTop: S.md }}
        />
      ) : null}
      {status === "confirmed" && targetCount ? (
        <Button
          label="Sync to Salesforce"
          loading={busy}
          disabled={busy}
          onPress={() => run(() => onSync(mapping.object))}
          style={{ marginTop: S.md }}
        />
      ) : null}
      {status === "failed" ? (
        <Button
          label="Retry sync"
          variant="ghost"
          loading={busy}
          disabled={busy}
          onPress={() => run(() => onSync(mapping.object))}
          style={{ marginTop: S.md }}
        />
      ) : null}

      <View style={st.actionsRow}>
        <Pressable onPress={openSheet} hitSlop={6} disabled={busy || status === "syncing"}>
          <Text style={[st.actionTxt, (busy || status === "syncing") && { opacity: 0.5 }]}>
            {hasValue ? "Change" : `Enter ${fieldLabel.toLowerCase()}`}
          </Text>
        </Pressable>
        {status === "synced" && targetCount ? (
          <Pressable
            onPress={() => run(() => onSync(mapping.object))}
            hitSlop={6}
            disabled={busy}
          >
            <Text style={[st.actionTxt, busy && { opacity: 0.5 }]}>Sync again</Text>
          </Pressable>
        ) : null}
      </View>

      <Modal
        visible={sheetOpen}
        transparent
        animationType="slide"
        onRequestClose={() => setSheetOpen(false)}
      >
        <KeyboardAwareSheet>
          <Pressable style={st.backdrop} onPress={() => setSheetOpen(false)}>
            <Pressable style={st.sheet} onPress={() => {}}>
              <Text style={st.sheetTitle}>{fieldLabel}</Text>
              <Text style={st.sheetBlurb}>
                This is what links the meeting to the right{" "}
                {mapping.object_label || mapping.object} record in Salesforce.
                We&apos;ll look it up and show you what we find before anything
                is written.
              </Text>
              <TextInput
                style={st.input}
                value={draft}
                onChangeText={setDraft}
                placeholder={fieldLabel}
                placeholderTextColor={C.textFaint}
                autoCapitalize={
                  keyboardFor(mapping.lookup_field_type) === "email-address"
                    ? "none" : "characters"
                }
                autoCorrect={false}
                keyboardType={keyboardFor(mapping.lookup_field_type)}
                maxLength={VALUE_MAX}
                returnKeyType="done"
                onSubmitEditing={() => commit(draft.trim() || null)}
                autoFocus
              />
              <Button
                label={busy ? "Checking Salesforce…" : "Find record"}
                loading={busy}
                disabled={busy || !draft.trim() || draft.trim() === value}
                onPress={() => commit(draft.trim() || null)}
                style={{ marginTop: 18 }}
              />
              {/* Clearing must stay reachable: it is how a wrong AI extraction
                  gets undone. Only offered when there is something to clear. */}
              {hasValue ? (
                <Button
                  label="Remove this link"
                  variant="ghost"
                  disabled={busy}
                  onPress={() => commit(null)}
                  style={{ marginTop: S.sm }}
                />
              ) : null}
            </Pressable>
          </Pressable>
        </KeyboardAwareSheet>
      </Modal>
    </>
  );
}

export function CrmRecordsBlock({
  mappings, records, ambiguity, onSave, onChoose, onConfirm, onSync,
}: {
  // The user's configured mappings. EMPTY (or absent) renders nothing at all —
  // that is the "no Salesforce fields unless configured" behaviour, expressed
  // as data rather than a condition per object type.
  mappings?: CrmMapping[] | null;
  records?: Record<string, CrmRecordValue> | null;
  ambiguity?: Record<string, CrmCandidate[]> | null;
  onSave: (object: string, value: string | null) => Promise<void>;
  onChoose: (object: string, candidate: CrmCandidate) => Promise<void>;
  onConfirm: (object: string) => Promise<void>;
  onSync: (object: string) => Promise<void>;
}) {
  const { C } = useTheme();

  if (!mappings || mappings.length === 0) return null;

  return (
    <View>
      <SectionRule right={<Icon name="cloud.fill" tintColor={C.textFaint} size={15} />}>
        Salesforce
      </SectionRule>
      <Card>
        {mappings.map((m, i) => (
          <CrmRecordRow
            key={m.object}
            mapping={m}
            current={records?.[m.object]}
            // Server-stored candidates (from a persisted ambiguous state) or
            // the ones this session's lookup just returned.
            candidates={ambiguity?.[m.object] ?? records?.[m.object]?.candidates}
            onSave={onSave}
            onChoose={onChoose}
            onConfirm={onConfirm}
            onSync={onSync}
            showDivider={i > 0}
          />
        ))}
      </Card>
    </View>
  );
}
