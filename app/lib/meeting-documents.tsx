// lib/meeting-documents.tsx — Documents(N) list on Overview, and the
// full-screen "Create Document" generator it and the Assistant workspace both
// open. One document-creation surface for the whole app: Quick Templates (4
// universal templates) + Custom Generation (freeform prompt, generated and
// persisted server-side via generateCustomDocument — see api.ts). No
// document grid, no per-type tiles — a simple list, exactly like the
// reference.
//
// Every generated document is stored the same way regardless of how it was
// produced (template, quick action, or custom prompt), so "Documents (N)" is
// one list, not one list per generation method. Rename and Delete are now
// REAL, persisted operations (PATCH/DELETE .../ai/documents/{key+}) — no
// longer client-only.
import { useState } from "react";
import {
  ActivityIndicator, Alert, KeyboardAvoidingView, Modal, Platform, Pressable,
  ScrollView, Share, StyleSheet, Text, TextInput, View,
} from "react-native";
import Animated, { FadeInDown, LinearTransition } from "react-native-reanimated";
import { ELEV, FONT, R, S, useTheme } from "./theme";
import { Button, Card, SectionRule } from "./ui";
import { Icon } from "./icons";
import { Markdown } from "./document-renderer";
import { canExportPdf, copyDocument, exportPdf } from "./export-doc";
import { exportDocx } from "./docx-export";
import {
  ApiError, deleteAiDocument, generateAiDocument, generateCustomDocument,
  isNotReady, isRetryable, listAiDocuments, runQuickAction, saveAiDocument,
  type AiDocument,
} from "./api";

export type GeneratedDoc = AiDocument;

// The 4 universal templates — deliberately not domain-specific (no sales,
// site-visit, requirements, etc.), so they apply to any meeting type.
const QUICK_TEMPLATES: { key: string; label: string; icon: string; kind: "document" | "quick" }[] = [
  { key: "minutes_of_meeting", label: "Minutes of Meeting", icon: "list.number", kind: "document" },
  { key: "executive_summary", label: "Executive Summary", icon: "doc.richtext", kind: "document" },
  { key: "follow_up_email", label: "Follow-up Email", icon: "envelope.badge", kind: "document" },
  { key: "whatsapp_update", label: "Short Summary", icon: "message.fill", kind: "quick" },
];

const EXAMPLE_PROMPTS = [
  "Create a proposal for the client", "Generate interview notes",
  "Create a project status report", "Convert into Jira tasks",
  "Create lecture notes", "Summarize for executives",
];

// The one document type with a structured editor behind it. Opening it from
// the list routes to lib/mom-editor.tsx instead of the Markdown viewer — the
// stored Markdown is a MIRROR of that structure (see the backend's
// _mirror_document), so editing it as text would be edited away by the next
// structured save.
const STRUCTURED_MOM_TYPE = "minutes_of_meeting";

const DOC_ICONS: Record<string, { icon: string; color: string }> = {
  minutes_of_meeting: { icon: "list.number", color: "#3E6BFF" },
  executive_summary: { icon: "doc.richtext", color: "#1FA972" },
  follow_up_email: { icon: "envelope.badge", color: "#F5A623" },
  whatsapp_update: { icon: "message.fill", color: "#1FA972" },
  action_items: { icon: "checkmark.circle", color: "#3E6BFF" },
  sales_meeting_report: { icon: "building.2.fill", color: "#7C5CFF" },
  site_visit_report: { icon: "ruler", color: "#F5A623" },
  customer_requirement_report: { icon: "doc.richtext", color: "#0EA5B7" },
};
function docVisual(type: string): { icon: string; color: string } {
  return DOC_ICONS[type] ?? { icon: "doc.richtext", color: "#3E6BFF" };
}

function buildStyles(C: ReturnType<typeof useTheme>["C"]) {
  return StyleSheet.create({
    sheetWrap: { flex: 1, backgroundColor: C.bg },
    sheetBar: {
      flexDirection: "row", alignItems: "center", gap: S.md,
      paddingHorizontal: 20, paddingTop: 16, paddingBottom: 14,
      borderBottomWidth: 1, borderBottomColor: C.border, backgroundColor: C.surface,
    },
    sheetTitle: { fontFamily: FONT.extrabold, fontSize: 19, color: C.text, flex: 1 },
    sheetBody: { paddingHorizontal: 20, paddingTop: S.lg, paddingBottom: 60 },
    templateRow: {
      backgroundColor: C.surface, borderWidth: 1, borderColor: C.border, borderRadius: R.card,
      paddingHorizontal: 14, paddingVertical: 14, marginTop: 9,
      flexDirection: "row", alignItems: "center", gap: 12,
      shadowColor: C.shadow, ...ELEV.sm,
    },
    templateIcon: {
      width: 38, height: 38, borderRadius: 12, alignItems: "center", justifyContent: "center",
    },
    templateTxt: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text, flex: 1 },
    customInput: {
      backgroundColor: C.surface, borderWidth: 1, borderColor: C.border, borderRadius: R.card,
      paddingHorizontal: 14, paddingVertical: 14, marginTop: 10, minHeight: 96,
      fontFamily: FONT.regular, fontSize: 14.5, color: C.text, textAlignVertical: "top",
    },
    exampleChip: {
      backgroundColor: C.surface2, borderRadius: R.pill,
      paddingHorizontal: 12, paddingVertical: 8, marginRight: 8, marginTop: 8,
    },
    exampleTxt: { fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim },
    docRow: {
      flexDirection: "row", alignItems: "center", gap: 12, paddingVertical: 12,
    },
    docIcon: { width: 36, height: 36, borderRadius: 11, alignItems: "center", justifyContent: "center" },
    docLabel: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    docMeta: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 2 },
    staleBanner: {
      flexDirection: "row", alignItems: "center", gap: 8,
      backgroundColor: C.warnSoft, borderRadius: R.card,
      paddingHorizontal: 14, paddingVertical: 11, marginTop: 9,
    },
    staleTxt: { fontFamily: FONT.medium, fontSize: 13, color: C.text, flex: 1 },
    staleAction: { fontFamily: FONT.bold, fontSize: 12.5, color: C.primary },
    staleToast: { fontFamily: FONT.medium, fontSize: 12.5, color: C.primary, marginTop: 8 },
    staleDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: C.warn },
    editArea: {
      fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: C.text,
      minHeight: 380, textAlignVertical: "top", paddingHorizontal: 20, paddingTop: S.lg,
    },
    exportRow: {
      flexDirection: "row", flexWrap: "wrap", gap: S.sm, paddingHorizontal: 20,
      paddingVertical: 12, borderTopWidth: 1, borderTopColor: C.border, backgroundColor: C.surface,
    },
    renameInput: {
      backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      fontFamily: FONT.regular, fontSize: 16, color: C.text, paddingVertical: 12, paddingHorizontal: 14, marginTop: 16,
    },
    sheetBackdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" },
    promptSheet: {
      backgroundColor: C.bg, paddingHorizontal: 22, paddingTop: 20, paddingBottom: 32,
      borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl,
    },
  });
}

// ===========================================================================
// Create Document — full-screen generator: Quick Templates + Custom prompt.
// Opened both from Overview's "Create Document" row and from the Assistant
// workspace, so it is exported standalone rather than bundled with a button.
// ===========================================================================
export function CreateDocumentSheet({
  visible, recordingKey, onClose, onGenerated, onOpenMomEditor,
}: {
  visible: boolean;
  recordingKey: string;
  onClose: () => void;
  onGenerated: (doc: GeneratedDoc) => void;
  /** Minutes of Meeting opens the structured editor instead of generating a
   * Markdown blob — see runTemplate. */
  onOpenMomEditor: () => void;
}) {
  const { C, T } = useTheme();
  const st = buildStyles(C);
  const [busyTemplate, setBusyTemplate] = useState<string | null>(null);
  const [customPrompt, setCustomPrompt] = useState("");
  const [customBusy, setCustomBusy] = useState(false);
  const [error, setError] = useState("");
  // What to re-run if the user taps Retry. Set on every failure so the banner
  // can offer a one-tap retry instead of making the user find the template
  // again — a 502 from Groq is transient and usually succeeds on a second try.
  const [retry, setRetry] = useState<(() => void) | null>(null);

  // Every generation path fails the same way, so they share one reporter.
  // isNotReady (409) deliberately offers NO retry: the transcript genuinely
  // isn't finished, and hammering the button won't change that.
  const report = (e: unknown, again: () => void) => {
    if (isNotReady(e)) {
      setError("The transcript isn't ready yet.");
      setRetry(null);
    } else if (isRetryable(e)) {
      setError("Unable to generate AI output.");
      setRetry(() => again);
    } else {
      setError(e instanceof ApiError ? e.message : "Something went wrong.");
      setRetry(() => again);
    }
  };

  const runTemplate = async (t: (typeof QUICK_TEMPLATES)[number]) => {
    // Minutes of Meeting is structured now: hand off to the editor, which
    // generates it (no Groq call) and lets the user edit before anything is
    // exported. The other six templates keep the prompt path unchanged.
    if (t.key === STRUCTURED_MOM_TYPE) {
      onClose();
      onOpenMomEditor();
      return;
    }
    setBusyTemplate(t.key); setError(""); setRetry(null);
    try {
      if (t.kind === "document") {
        const res = await generateAiDocument(recordingKey, t.key);
        onGenerated(res.document);
      } else {
        const res = await runQuickAction(recordingKey, t.key);
        onGenerated(res.document);
      }
      onClose();
    } catch (e) {
      report(e, () => runTemplate(t));
    } finally {
      setBusyTemplate(null);
    }
  };

  const runCustom = async () => {
    const prompt = customPrompt.trim();
    if (!prompt) return;
    setCustomBusy(true); setError(""); setRetry(null);
    try {
      const res = await generateCustomDocument(recordingKey, prompt);
      onGenerated(res.document);
      setCustomPrompt("");
      onClose();
    } catch (e) {
      report(e, () => runCustom());
    } finally {
      setCustomBusy(false);
    }
  };

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <View style={st.sheetWrap}>
        <View style={st.sheetBar}>
          <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
            <Icon name="xmark" tintColor={C.text} size={20} />
          </Pressable>
          <Text style={st.sheetTitle}>Create Document</Text>
        </View>

        <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : "height"}>
          <ScrollView contentContainerStyle={st.sheetBody} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled">
            <Text style={T.label}>Quick templates</Text>
            {QUICK_TEMPLATES.map((t) => (
              <Pressable
                key={t.key}
                onPress={() => runTemplate(t)}
                disabled={busyTemplate !== null || customBusy}
                style={({ pressed }) => [st.templateRow, pressed && { opacity: 0.75 }]}
                accessibilityLabel={t.label}
              >
                <View style={[st.templateIcon, { backgroundColor: docVisual(t.key).color + "22" }]}>
                  <Icon name={t.icon as any} tintColor={docVisual(t.key).color} size={18} />
                </View>
                <Text style={st.templateTxt}>{t.label}</Text>
                {busyTemplate === t.key ? (
                  <ActivityIndicator color={C.primary} size="small" />
                ) : (
                  <Icon name="chevron.right" tintColor={C.textFaint} size={16} />
                )}
              </Pressable>
            ))}

            <View style={{ marginTop: S.xl }}>
              <Text style={T.label}>Custom generation</Text>
              <TextInput
                style={st.customInput}
                value={customPrompt}
                onChangeText={setCustomPrompt}
                placeholder="Describe what you want to generate…"
                placeholderTextColor={C.textFaint}
                multiline
                editable={!customBusy}
              />

              <View style={{ flexDirection: "row", flexWrap: "wrap", marginTop: 2 }}>
                {EXAMPLE_PROMPTS.map((ex) => (
                  <Pressable key={ex} onPress={() => setCustomPrompt(ex)} style={st.exampleChip}>
                    <Text style={st.exampleTxt}>{ex}</Text>
                  </Pressable>
                ))}
              </View>

              {error ? (
                <View style={{ backgroundColor: C.dangerSoft, borderRadius: R.md, padding: S.md, marginTop: 16 }}>
                  <View style={{ flexDirection: "row", alignItems: "center", gap: S.sm }}>
                    <Text style={[T.body, { color: C.danger, flex: 1 }]}>{error}</Text>
                    {retry ? (
                      <Pressable
                        onPress={() => { const again = retry; setError(""); setRetry(null); again(); }}
                        disabled={busyTemplate !== null || customBusy}
                        hitSlop={6}
                        accessibilityLabel="Retry"
                        style={({ pressed }) => [
                          { flexDirection: "row", alignItems: "center", gap: 5 },
                          pressed && { opacity: 0.6 },
                        ]}
                      >
                        <Icon name="arrow.clockwise" tintColor={C.danger} size={14} />
                        <Text style={{ fontFamily: FONT.bold, fontSize: 12.5, color: C.danger }}>Retry</Text>
                      </Pressable>
                    ) : null}
                  </View>
                </View>
              ) : null}

              <Button
                label="Generate"
                loading={customBusy}
                disabled={!customPrompt.trim()}
                onPress={runCustom}
                style={{ marginTop: 18 }}
              />
            </View>
          </ScrollView>
        </KeyboardAvoidingView>
      </View>
    </Modal>
  );
}

// ===========================================================================
// Documents (N) — a simple list (not a grid), and the viewer/editor/export
// sheet per document.
// ===========================================================================
export function DocumentsList({
  recordingKey, meetingTitle, documents, onChange, onCreatePress,
  documentsNeedingUpdate, onUpdateAll, onOpenMomEditor,
}: {
  recordingKey: string;
  meetingTitle: string;
  documents: GeneratedDoc[];
  onChange: (next: GeneratedDoc[]) => void;
  onCreatePress: () => void;
  documentsNeedingUpdate: string[];
  onUpdateAll: () => Promise<{ updated: number; remaining: number }>;
  /** Tapping the Minutes of Meeting row opens the structured editor rather
   * than the Markdown viewer — see STRUCTURED_MOM_TYPE. */
  onOpenMomEditor: () => void;
}) {
  const { C } = useTheme();
  const st = buildStyles(C);
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const [updatingAll, setUpdatingAll] = useState(false);
  const [updateToast, setUpdateToast] = useState("");

  const openDoc = openIndex != null ? documents[openIndex] : null;

  const updateAt = (index: number, doc: GeneratedDoc) => {
    const next = documents.slice();
    next[index] = doc;
    onChange(next);
  };
  const removeAt = (index: number) => {
    onChange(documents.filter((_, i) => i !== index));
    setOpenIndex(null);
  };

  const runUpdateAll = async () => {
    setUpdatingAll(true);
    setUpdateToast("");
    try {
      const { updated, remaining } = await onUpdateAll();
      setUpdateToast(
        remaining > 0
          ? `${updated} updated, ${remaining} remaining — tap Update All again`
          : `${updated} document${updated === 1 ? "" : "s"} updated`
      );
    } catch (e) {
      setUpdateToast(e instanceof ApiError ? e.message : "Couldn't update documents");
    } finally {
      setUpdatingAll(false);
      setTimeout(() => setUpdateToast(""), 3200);
    }
  };

  return (
    <View>
      <SectionRule>Documents</SectionRule>

      {documentsNeedingUpdate.length > 0 ? (
        <View style={st.staleBanner}>
          <Icon name="arrow.triangle.2.circlepath" tintColor={C.warn} size={16} />
          <Text style={st.staleTxt}>
            {documentsNeedingUpdate.length} document{documentsNeedingUpdate.length === 1 ? "" : "s"} need updating
          </Text>
          <Pressable onPress={runUpdateAll} disabled={updatingAll} hitSlop={6} accessibilityLabel="Update All">
            {updatingAll ? (
              <ActivityIndicator color={C.primary} size="small" />
            ) : (
              <Text style={st.staleAction}>Update All</Text>
            )}
          </Pressable>
        </View>
      ) : null}
      {updateToast ? <Text style={st.staleToast}>{updateToast}</Text> : null}

      <Card style={{ paddingVertical: documents.length ? S.xs : S.sm }}>
        {documents.slice(0, 3).map((doc, i) => {
          const visual = docVisual(doc.type);
          const needsUpdate = doc.status === "needs_update";
          return (
            <Animated.View
              key={`${doc.type}-${doc.generated_at}`}
              entering={FadeInDown.springify().damping(16).stiffness(180)}
              layout={LinearTransition.springify().damping(16).stiffness(180)}
            >
              <Pressable
                onPress={() => (doc.type === STRUCTURED_MOM_TYPE
                  ? onOpenMomEditor()
                  : setOpenIndex(i))}
                style={({ pressed }) => [
                  st.docRow, i > 0 && { borderTopWidth: 1, borderTopColor: C.border },
                  pressed && { opacity: 0.7 },
                ]}
                accessibilityLabel={`Open ${doc.label}`}
              >
                <View style={[st.docIcon, { backgroundColor: visual.color + "22" }]}>
                  <Icon name={visual.icon as any} tintColor={visual.color} size={17} />
                </View>
                <View style={{ flex: 1 }}>
                  <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
                    <Text style={st.docLabel} numberOfLines={1}>{doc.label}</Text>
                    {needsUpdate ? <View style={st.staleDot} /> : null}
                  </View>
                  <Text style={st.docMeta}>
                    {needsUpdate ? "Needs update · " : "Generated · "}
                    {new Date(doc.generated_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
                  </Text>
                </View>
                <Icon name="chevron.right" tintColor={C.textFaint} size={15} />
              </Pressable>
            </Animated.View>
          );
        })}

        <Pressable
          onPress={onCreatePress}
          style={({ pressed }) => [
            st.docRow, documents.length > 0 && { borderTopWidth: 1, borderTopColor: C.border },
            pressed && { opacity: 0.7 },
          ]}
          accessibilityLabel="Create document"
        >
          <View style={[st.docIcon, { backgroundColor: C.primarySoft }]}>
            <Icon name="doc.badge.plus" tintColor={C.primary} size={17} />
          </View>
          <Text style={[st.docLabel, { color: C.primary, flex: 1 }]}>Create Document</Text>
        </Pressable>
      </Card>

      {openDoc ? (
        <DocumentSheet
          doc={openDoc}
          recordingKey={recordingKey}
          meetingTitle={meetingTitle}
          onClose={() => setOpenIndex(null)}
          onSave={(d) => updateAt(openIndex!, d)}
          onDelete={() => removeAt(openIndex!)}
        />
      ) : null}
    </View>
  );
}

function DocumentSheet({
  doc, recordingKey, meetingTitle, onClose, onSave, onDelete,
}: {
  doc: GeneratedDoc;
  recordingKey: string;
  meetingTitle: string;
  onClose: () => void;
  onSave: (doc: GeneratedDoc) => void;
  onDelete: () => void;
}) {
  const { C, T } = useTheme();
  const st = buildStyles(C);
  const [mode, setMode] = useState<"view" | "edit" | "rename">("view");
  const [draftContent, setDraftContent] = useState(doc.content);
  const [draftLabel, setDraftLabel] = useState(doc.label);
  const [busy, setBusy] = useState<"regenerate" | "pdf" | "docx" | "copy" | "save" | "delete" | null>(null);
  const [toast, setToast] = useState("");

  const flash = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(""), 1800);
  };

  const saveEdit = async () => {
    setBusy("save");
    try {
      const saved = await saveAiDocument(recordingKey, doc.type, { content: draftContent });
      onSave(saved);
      setMode("view");
      flash("Saved");
    } catch (e) {
      flash(e instanceof ApiError ? e.message : "Couldn't save");
    } finally {
      setBusy(null);
    }
  };

  const saveRename = async () => {
    const trimmed = draftLabel.trim();
    if (!trimmed) { setMode("view"); return; }
    setBusy("save");
    try {
      const saved = await saveAiDocument(recordingKey, doc.type, { label: trimmed });
      onSave(saved);
    } catch (e) {
      flash(e instanceof ApiError ? e.message : "Couldn't rename");
    } finally {
      setBusy(null);
      setMode("view");
    }
  };

  const doCopy = async () => {
    setBusy("copy");
    const r = await copyDocument(doc.content);
    setBusy(null);
    flash(r === "copied" ? "Copied" : r === "shared" ? "Shared" : "Couldn't copy");
  };

  const doShare = async () => {
    try {
      await Share.share({ message: doc.content, title: doc.label });
    } catch {
      // user cancelled
    }
  };

  // Export must use the LATEST PERSISTED content, not whatever happens to be
  // sitting in the `doc` prop — e.g. right after a speaker rename elsewhere
  // in the same session bumped this document to needs_update, or another
  // screen regenerated it. For the 8 fixed types, generateAiDocument with
  // regenerate=false is a plain cache-serving read (no Groq call when
  // nothing changed) and is the same freshness guarantee the rest of the
  // app relies on. Custom documents (custom_<id>) aren't a valid `type` for
  // that route, so fall back to re-listing all documents and picking this
  // one out — still a live read, just a different endpoint.
  const latestDocument = async (): Promise<AiDocument> => {
    try {
      if (doc.is_custom) {
        const res = await listAiDocuments(recordingKey);
        return res.documents[doc.type] ?? doc;
      }
      const res = await generateAiDocument(recordingKey, doc.type, false);
      return res.document;
    } catch {
      // Best-effort: exporting the possibly-stale prop beats failing the
      // export outright over a transient network hiccup.
      return doc;
    }
  };

  const doPdf = async () => {
    setBusy("pdf");
    flash("Making the PDF…");
    const fresh = await latestDocument();
    const r = await exportPdf(fresh.content, fresh.label, meetingTitle);
    setBusy(null);
    flash(r === "shared" ? "PDF shared"
      : r === "saved" ? "PDF saved to files"
      : r === "unsupported" ? "PDF needs a rebuilt app" : "Couldn't make the PDF");
  };

  const doDocx = async () => {
    setBusy("docx");
    flash("Making the Word document…");
    const fresh = await latestDocument();
    const r = await exportDocx(fresh.content, fresh.label, meetingTitle);
    setBusy(null);
    flash(r === "shared" ? "Document shared"
      : r === "saved" ? "Document saved to files"
      : r === "unsupported" ? "Export needs a rebuilt app" : "Couldn't export");
  };

  const regenerate = async () => {
    if (doc.is_custom) return; // no fixed prompt/type to regenerate against
    setBusy("regenerate");
    try {
      const res = await generateAiDocument(recordingKey, doc.type, true);
      onSave(res.document);
      flash("Regenerated");
    } catch (e) {
      flash(e instanceof ApiError ? e.message : "Couldn't regenerate");
    } finally {
      setBusy(null);
    }
  };

  const doDelete = async () => {
    setBusy("delete");
    try {
      await deleteAiDocument(recordingKey, doc.type);
      onDelete();
    } catch (e) {
      flash(e instanceof ApiError ? e.message : "Couldn't delete");
      setBusy(null);
    }
  };

  const confirmDelete = () => {
    Alert.alert("Delete document", `Delete "${doc.label}"?`, [
      { text: "Cancel", style: "cancel" },
      { text: "Delete", style: "destructive", onPress: doDelete },
    ]);
  };

  return (
    <Modal visible animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView style={st.sheetWrap} behavior={Platform.OS === "ios" ? "padding" : "height"}>
        <View style={st.sheetBar}>
          <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
            <Icon name="xmark" tintColor={C.text} size={20} />
          </Pressable>
          <Text style={st.sheetTitle} numberOfLines={1}>{doc.label}</Text>
          {mode === "edit" ? (
            <Pressable onPress={saveEdit} disabled={busy === "save"} hitSlop={8} accessibilityLabel="Save">
              {busy === "save" ? (
                <ActivityIndicator color={C.primary} size="small" />
              ) : (
                <Text style={{ fontFamily: FONT.bold, fontSize: 13, color: C.primary }}>Save</Text>
              )}
            </Pressable>
          ) : (
            <Pressable onPress={() => setMode("edit")} hitSlop={8} accessibilityLabel="Edit">
              <Icon name="square.and.pencil" tintColor={C.textDim} size={18} />
            </Pressable>
          )}
        </View>

        {mode === "edit" ? (
          <TextInput
            style={st.editArea}
            value={draftContent}
            onChangeText={setDraftContent}
            multiline
            autoFocus
            textAlignVertical="top"
          />
        ) : (
          <ScrollView contentContainerStyle={st.sheetBody} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled">
            {toast ? <Text style={[T.caption, { marginBottom: 10, color: C.primary }]}>{toast}</Text> : null}
            <Markdown text={doc.content} />
          </ScrollView>
        )}

        {mode === "view" ? (
          <ScrollView horizontal showsHorizontalScrollIndicator={false} style={st.exportRow} keyboardShouldPersistTaps="handled">
            <ActionChip icon="pencil" label="Rename" onPress={() => { setDraftLabel(doc.label); setMode("rename"); }} />
            <ActionChip icon="square.and.arrow.up" label="Share" onPress={doShare} />
            <ActionChip icon="doc.on.doc" label="Copy" onPress={doCopy} busy={busy === "copy"} />
            <ActionChip icon="arrow.down.doc" label="Export PDF" onPress={doPdf} busy={busy === "pdf"}
              disabled={!canExportPdf()} />
            <ActionChip icon="arrow.down.doc" label="Export DOCX" onPress={doDocx} busy={busy === "docx"} />
            {!doc.is_custom ? (
              <ActionChip icon="arrow.clockwise" label="Regenerate" onPress={regenerate} busy={busy === "regenerate"} />
            ) : null}
            <ActionChip icon="trash" label="Delete" onPress={confirmDelete} busy={busy === "delete"} tint={C.danger} />
          </ScrollView>
        ) : null}
      </KeyboardAvoidingView>

      {mode === "rename" ? (
        <Modal visible transparent animationType="fade" onRequestClose={() => setMode("view")}>
          <View style={st.sheetBackdrop}>
            <Pressable style={{ flex: 1 }} onPress={() => setMode("view")} />
            <View style={st.promptSheet}>
              <Text style={T.headlineSm}>Rename document</Text>
              <TextInput
                style={st.renameInput}
                value={draftLabel}
                onChangeText={setDraftLabel}
                autoFocus
                maxLength={80}
                returnKeyType="done"
                onSubmitEditing={saveRename}
              />
              <View style={{ flexDirection: "row", gap: S.sm, marginTop: 20 }}>
                <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={() => setMode("view")} disabled={busy === "save"} />
                <Button label="Save" style={{ flex: 1 }} onPress={saveRename} loading={busy === "save"} />
              </View>
            </View>
          </View>
        </Modal>
      ) : null}
    </Modal>
  );
}

function ActionChip({
  icon, label, onPress, busy, disabled, tint,
}: {
  icon: Parameters<typeof Icon>[0]["name"];
  label: string;
  onPress: () => void;
  busy?: boolean;
  disabled?: boolean;
  tint?: string;
}) {
  const { C } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || busy}
      style={({ pressed }) => [
        {
          flexDirection: "row", alignItems: "center", gap: 6,
          backgroundColor: C.surface2, borderRadius: R.pill,
          paddingHorizontal: 13, paddingVertical: 9, marginRight: 8,
        },
        (disabled) && { opacity: 0.4 },
        pressed && { opacity: 0.7 },
      ]}
      accessibilityLabel={label}
    >
      {busy ? (
        <ActivityIndicator color={tint ?? C.textDim} size="small" />
      ) : (
        <Icon name={icon} tintColor={tint ?? C.textDim} size={14} />
      )}
      <Text style={{ fontFamily: FONT.semibold, fontSize: 12.5, color: tint ?? C.text }}>{label}</Text>
    </Pressable>
  );
}
