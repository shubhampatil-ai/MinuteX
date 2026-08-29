// lib/mom-editor.tsx — the MoM editor's screens.
//
// THREE surfaces, all mobile-first:
//
//   MomEditorScreen   the section list — reorder, hide, delete, add
//   SectionEditor     one section's contents, by kind
//   TableEditor       a grid, one column at a time (see below)
//
// WHY A TABLE IS EDITED ONE COLUMN AT A TIME. The obvious mobile table editor
// is a horizontally-scrolling grid of TextInputs. It does not work: a
// five-column table gives each cell about 60pt on a phone, the keyboard covers
// the row being edited, and horizontal scroll fights the vertical scroll of
// the list it sits in. So the grid is shown READ-ONLY (scrolling horizontally,
// which is fine for reading), and editing happens a ROW at a time in a sheet
// where every cell gets a full-width labelled input. Same data, no cramped
// inputs, no gesture conflict.
//
// Every mutation goes through lib/mom-model.ts rather than being written
// inline, so provenance (`ai` -> `user_edited`) and deletion tombstones are
// recorded in ONE place. An edit made directly here would be silently
// discarded by the next regeneration.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator, Alert, KeyboardAvoidingView, Modal, Platform, Pressable,
  ScrollView, StyleSheet, Text, TextInput, View,
} from "react-native";
import Animated, { FadeInDown, LinearTransition } from "react-native-reanimated";
import {
  ApiError, deleteMom, generateMom, getMom, isNotReady, isRetryable, saveMom,
  type AiDocument, type Mom, type MomSection, type MomSectionKind,
} from "./api";
import { canExportPdf } from "./export-doc";
import { Icon } from "./icons";
import { exportMomDocx } from "./mom-docx";
import {
  addColumn, addField, addListItem, addRow, addSection, deleteColumn,
  deleteField, deleteListItem, deleteRow, deleteSection, hasUserEdits,
  isSectionEmpty, moveSection, renameColumn, renameSection, sectionSummary,
  toggleFieldVisible, toggleSectionVisible, updateCell, updateField,
  updateListItem, updateText,
} from "./mom-model";
import { exportMomPdf } from "./mom-pdf";
import { MomPreview } from "./mom-preview";
import { ELEV, FONT, R, S, useTheme, type ColorScale } from "./theme";
import { Button, Card } from "./ui";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------
function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    wrap: { flex: 1, backgroundColor: C.bg },
    bar: {
      flexDirection: "row", alignItems: "center", gap: S.md,
      paddingHorizontal: 18, paddingTop: 14, paddingBottom: 12,
      borderBottomWidth: 1, borderBottomColor: C.border,
      backgroundColor: C.surface,
    },
    barTitle: { fontFamily: FONT.extrabold, fontSize: 18, color: C.text, flex: 1 },
    barAction: { fontFamily: FONT.bold, fontSize: 13, color: C.primary },
    body: { paddingHorizontal: 18, paddingTop: S.lg, paddingBottom: 40 },

    meetingCard: { padding: 16, marginBottom: S.lg },
    kicker: {
      fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.1,
      color: C.primary, marginBottom: 4,
    },
    meetingTitle: { fontFamily: FONT.extrabold, fontSize: 19, color: C.text },
    meetingMeta: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint, marginTop: 4,
    },

    sectionRow: {
      flexDirection: "row", alignItems: "center", gap: 10,
      paddingVertical: 12, paddingHorizontal: 12,
      backgroundColor: C.surface, borderRadius: R.card, marginBottom: 8,
      borderWidth: 1, borderColor: C.border, shadowColor: C.shadow, ...ELEV.sm,
    },
    sectionTitle: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    sectionMeta: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint, marginTop: 2,
    },
    kindPill: {
      paddingHorizontal: 7, paddingVertical: 2, borderRadius: R.pill,
      backgroundColor: C.surface2,
    },
    kindTxt: {
      fontFamily: FONT.bold, fontSize: 9, letterSpacing: 0.6, color: C.textDim,
    },
    editedDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: C.primary },

    fieldCard: {
      backgroundColor: C.surface, borderRadius: R.md, borderWidth: 1,
      borderColor: C.border, padding: 12, marginBottom: 8,
    },
    label: {
      fontFamily: FONT.bold, fontSize: 10, letterSpacing: 0.9,
      color: C.textFaint, marginBottom: 5,
    },
    input: {
      backgroundColor: C.surface2, borderRadius: R.sm, borderWidth: 1,
      borderColor: C.border, paddingHorizontal: 11, paddingVertical: 9,
      fontFamily: FONT.regular, fontSize: 14, color: C.text,
    },
    inputMulti: { minHeight: 96, textAlignVertical: "top" },

    grid: { borderWidth: 1, borderColor: C.border, borderRadius: R.sm },
    gridHead: { flexDirection: "row", backgroundColor: C.surface2 },
    gridHeadTxt: {
      fontFamily: FONT.bold, fontSize: 9.5, letterSpacing: 0.5,
      color: C.textDim, padding: 8,
    },
    gridRow: {
      flexDirection: "row", borderTopWidth: 1, borderTopColor: C.border,
      alignItems: "stretch",
    },
    gridCell: {
      fontFamily: FONT.regular, fontSize: 12, color: C.text, padding: 8,
    },

    footer: {
      flexDirection: "row", gap: S.sm, paddingHorizontal: 18,
      paddingTop: 12, paddingBottom: 22,
      borderTopWidth: 1, borderTopColor: C.border, backgroundColor: C.surface,
    },
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" },
    sheet: {
      backgroundColor: C.bg, paddingHorizontal: 20, paddingTop: 18,
      paddingBottom: 30, borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl,
      maxHeight: "88%",
    },
    sheetTitle: { fontFamily: FONT.extrabold, fontSize: 17, color: C.text },
    menuRow: {
      flexDirection: "row", alignItems: "center", gap: 12, paddingVertical: 13,
    },
    menuTxt: { fontFamily: FONT.medium, fontSize: 15, color: C.text },
    toast: {
      fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary,
      paddingHorizontal: 18, paddingBottom: 6,
    },
    error: {
      backgroundColor: C.dangerSoft, borderRadius: R.md, padding: S.md,
      marginBottom: S.md,
    },
  });
}

const KIND_LABEL: Record<MomSectionKind, string> = {
  fields: "FIELDS", table: "TABLE", text: "TEXT", list: "LIST",
};

/** Destructive actions always confirm — a deleted section takes its content
 * with it, and the tombstone means a regeneration will not bring it back. */
function confirmDestroy(what: string, onConfirm: () => void) {
  Alert.alert(`Delete ${what}?`, "This can't be undone.", [
    { text: "Cancel", style: "cancel" },
    { text: "Delete", style: "destructive", onPress: onConfirm },
  ]);
}

// ===========================================================================
// The editor screen
// ===========================================================================
export function MomEditorScreen({
  recordingKey, meetingTitle, onClose, onDocumentChange,
}: {
  recordingKey: string;
  meetingTitle: string;
  onClose: () => void;
  /** The refreshed Markdown mirror, so the Documents list on the screen
   * underneath updates without a second fetch. */
  onDocumentChange?: (doc: AiDocument) => void;
}) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);

  const [mom, setMom] = useState<Mom | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<
    "generate" | "save" | "pdf" | "docx" | "reset" | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState<(() => void) | null>(null);
  const [toast, setToast] = useState("");
  const [dirty, setDirty] = useState(false);
  const [openSection, setOpenSection] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  // The print engine's OWN pagination, learned only once a PDF has actually
  // been rendered. Deliberately not an estimate: nothing on the JS side knows
  // how the text will lay out, and a made-up page count shown next to a real
  // document is exactly the kind of confident-but-wrong detail to avoid.
  const [pageCount, setPageCount] = useState(0);

  const flash = useCallback((msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(""), 2000);
  }, []);

  const report = useCallback((e: unknown, again: () => void) => {
    if (isNotReady(e)) {
      setError("The transcript isn't ready yet.");
      setRetry(null);
    } else if (isRetryable(e)) {
      setError("Couldn't reach the server.");
      setRetry(() => again);
    } else {
      setError(e instanceof ApiError ? e.message : "Something went wrong.");
      setRetry(() => again);
    }
  }, []);

  // ---- load, generating on first open ------------------------------------
  //
  // Retries go through a ref rather than the callback referring to its own
  // binding: a self-reference captures the FIRST closure, so a retry queued
  // after a prop changed would re-run the stale version. The ref always holds
  // the current one.
  // Fetch the MoM, building one if this is the first open. Plain async, not a
  // useCallback: a retry needs to re-run the WORK, and a closure over
  // recordingKey is all that takes — chasing a stable callback identity is
  // what pushed an earlier version of this into refs it did not need.
  const fetchMom = async (): Promise<Mom> => {
    const res = await getMom(recordingKey);
    if (res.exists && res.mom.sections.length) return res.mom;
    // No MoM yet: build one. Costs no Groq call, so doing it on open is
    // cheaper than making the user tap through an empty state first.
    const built = await generateMom(recordingKey);
    if (built.document) onDocumentChange?.(built.document);
    return built.mom;
  };

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      setMom(await fetchMom());
      setDirty(false);
    } catch (e) {
      report(e, () => { void load(); });
    } finally {
      setLoading(false);
    }
  };

  // Mount fetch, in the shape the rest of this codebase uses (see
  // recording/[key]/assistant.tsx): the effect starts async work and every
  // setState lands after an await, so there is no synchronous cascade.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const next = await fetchMom();
        if (!cancelled) { setMom(next); setDirty(false); }
      } catch (e) {
        if (!cancelled) report(e, () => { void load(); });
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // fetchMom/load are re-created every render by design. The fetch is keyed
    // on the RECORDING, which is the only thing that should re-trigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordingKey]);

  // ---- mutate ------------------------------------------------------------
  // Every editor operation funnels through this, so nothing can change the
  // document without also marking it unsaved.
  const mutate = useCallback((fn: (m: Mom) => Mom) => {
    setMom((current) => (current ? fn(current) : current));
    setDirty(true);
  }, []);

  // Persist the CURRENT document. Takes `mom` as an argument rather than
  // closing over it so the retry closure below can be built without the
  // function needing to name itself — the same reasoning as fetchMom/load.
  const persist = async (doc: Mom): Promise<boolean> => {
    setBusy("save");
    setError("");
    try {
      const res = await saveMom(recordingKey, doc);
      setMom(res.mom);
      setDirty(false);
      if (res.document) onDocumentChange?.(res.document);
      flash("Saved");
      return true;
    } catch (e) {
      report(e, () => { void persist(doc); });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const save = useCallback(async (): Promise<boolean> => {
    if (!mom) return false;
    return persist(mom);
    // `persist` is re-created every render and reads only its argument, so
    // there is nothing stale for the dependency list to guard against.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mom]);

  const regenerate = useCallback(async () => {
    setMenuOpen(false);
    const run = async () => {
      setBusy("generate");
      setError("");
      try {
        const res = await generateMom(recordingKey);
        setMom(res.mom);
        setDirty(false);
        if (res.document) onDocumentChange?.(res.document);
        flash("Regenerated");
      } catch (e) {
        report(e, run);
      } finally {
        setBusy(null);
      }
    };
    // Honest about what regeneration does: user edits ARE preserved by the
    // merge, so the confirmation says so rather than implying a data loss
    // that will not happen.
    if (mom && hasUserEdits(mom)) {
      Alert.alert(
        "Regenerate from the meeting?",
        "Fresh AI content is merged in. Anything you edited, added, reordered "
        + "or deleted stays as you left it.",
        [{ text: "Cancel", style: "cancel" },
         { text: "Regenerate", onPress: run }]);
    } else {
      run();
    }
  }, [mom, recordingKey, onDocumentChange, flash, report]);

  const reset = useCallback(() => {
    setMenuOpen(false);
    Alert.alert(
      "Start over?",
      "Your edits to these minutes are discarded and a fresh MoM is built "
      + "from the meeting. The exported document in your Documents list is "
      + "kept.",
      [{ text: "Cancel", style: "cancel" },
       {
         text: "Start over", style: "destructive",
         onPress: async () => {
           setBusy("reset");
           try {
             await deleteMom(recordingKey);
             const built = await generateMom(recordingKey);
             setMom(built.mom);
             setDirty(false);
             if (built.document) onDocumentChange?.(built.document);
             flash("Reset");
           } catch (e) {
             report(e, () => {});
           } finally {
             setBusy(null);
           }
         },
       }]);
  }, [recordingKey, onDocumentChange, flash, report]);

  // ---- export ------------------------------------------------------------
  // Exports always SAVE first when there are unsaved edits: exporting one
  // thing while the app stores another is the failure users cannot see until
  // they open the file.
  const exportAs = useCallback(async (kind: "pdf" | "docx") => {
    if (!mom) return;
    setMenuOpen(false);
    if (dirty && !(await save())) {
      flash("Save failed — nothing exported");
      return;
    }
    setBusy(kind);
    flash(kind === "pdf" ? "Making the PDF…" : "Making the Word document…");
    let res;
    if (kind === "pdf") {
      const out = await exportMomPdf(mom, meetingTitle);
      res = out.result;
      // Only trust a real count; 0 means the render never happened.
      if (out.pages > 0) setPageCount(out.pages);
    } else {
      res = await exportMomDocx(mom, meetingTitle);
    }
    setBusy(null);
    flash(
      res === "shared" ? (kind === "pdf" ? "PDF shared" : "Document shared")
      : res === "saved" ? "Saved to files"
      : res === "unsupported" ? "Export needs a rebuilt app"
      : "Couldn't export");
  }, [mom, dirty, save, meetingTitle, flash]);

  const closeWithGuard = useCallback(() => {
    if (!dirty) { onClose(); return; }
    Alert.alert("Unsaved changes", "Save your minutes before closing?", [
      { text: "Discard", style: "destructive", onPress: onClose },
      { text: "Cancel", style: "cancel" },
      { text: "Save", onPress: async () => { if (await save()) onClose(); } },
    ]);
  }, [dirty, onClose, save]);

  const section = mom?.sections.find((s) => s.id === openSection) ?? null;

  // ---- render ------------------------------------------------------------
  if (loading) {
    return (
      <View style={[st.wrap, { alignItems: "center", justifyContent: "center" }]}>
        <ActivityIndicator color={C.primary} />
        <Text style={[T.caption, { marginTop: 12 }]}>Building your minutes…</Text>
      </View>
    );
  }

  return (
    <View style={st.wrap}>
      <View style={st.bar}>
        <Pressable onPress={closeWithGuard} hitSlop={8} accessibilityLabel="Back">
          <Icon name="chevron.left" tintColor={C.text} size={20} />
        </Pressable>
        <Text style={st.barTitle}>MoM Editor</Text>
        {dirty ? (
          <Pressable onPress={save} hitSlop={8} accessibilityLabel="Save"
                     disabled={busy === "save"}>
            {busy === "save"
              ? <ActivityIndicator color={C.primary} size="small" />
              : <Text style={st.barAction}>Save</Text>}
          </Pressable>
        ) : null}
        <Pressable onPress={() => setPreviewOpen(true)} hitSlop={8}
                   accessibilityLabel="Preview" disabled={!mom}>
          <Icon name="eye" tintColor={C.textDim} size={19} />
        </Pressable>
        <Pressable onPress={() => setMenuOpen(true)} hitSlop={8}
                   accessibilityLabel="More">
          <Icon name="ellipsis" tintColor={C.textDim} size={19} />
        </Pressable>
      </View>

      {toast ? <Text style={st.toast}>{toast}</Text> : null}

      <ScrollView contentContainerStyle={st.body} showsVerticalScrollIndicator={false}>
        {error ? (
          <View style={st.error}>
            <View style={{ flexDirection: "row", alignItems: "center", gap: S.sm }}>
              <Text style={[T.body, { color: C.danger, flex: 1 }]}>{error}</Text>
              {retry ? (
                <Pressable
                  onPress={() => { const again = retry; setError(""); setRetry(null); again(); }}
                  hitSlop={6} accessibilityLabel="Retry"
                >
                  <Text style={{ fontFamily: FONT.bold, fontSize: 12.5, color: C.danger }}>
                    Retry
                  </Text>
                </Pressable>
              ) : null}
            </View>
          </View>
        ) : null}

        {mom ? (
          <>
            <Card style={st.meetingCard}>
              <Text style={st.kicker}>MINUTES OF MEETING</Text>
              <Text style={st.meetingTitle} numberOfLines={2}>
                {mom.subtitle || meetingTitle || "Meeting"}
              </Text>
              <Text style={st.meetingMeta}>
                {mom.sections.length} section{mom.sections.length === 1 ? "" : "s"}
                {pageCount > 0
                  ? ` · ${pageCount} page${pageCount === 1 ? "" : "s"}`
                  : ""}
                {dirty ? " · unsaved" : ""}
              </Text>
            </Card>

            {mom.sections.length === 0 ? (
              <Card style={{ padding: 20, alignItems: "center" }}>
                <Text style={[T.body, { color: C.textDim, textAlign: "center" }]}>
                  No sections yet. Add one to start building your minutes.
                </Text>
              </Card>
            ) : null}

            {mom.sections.map((s, i) => (
              <Animated.View
                key={s.id}
                entering={FadeInDown.springify().damping(16).stiffness(180)}
                layout={LinearTransition.springify().damping(16).stiffness(180)}
              >
                <SectionRow
                  st={st} C={C} section={s} index={i}
                  total={mom.sections.length}
                  onOpen={() => setOpenSection(s.id)}
                  onToggle={() => mutate((m) => toggleSectionVisible(m, s.id))}
                  onMove={(d) => mutate((m) => moveSection(m, s.id, d))}
                  onDelete={() => confirmDestroy(
                    `"${s.title}"`, () => mutate((m) => deleteSection(m, s.id)))}
                />
              </Animated.View>
            ))}

            <Pressable
              onPress={() => setAddOpen(true)}
              style={({ pressed }) => [
                st.sectionRow,
                { justifyContent: "center", borderStyle: "dashed" },
                pressed && { opacity: 0.7 },
              ]}
              accessibilityLabel="Add section"
            >
              <Icon name="plus" tintColor={C.primary} size={16} />
              <Text style={{ fontFamily: FONT.bold, fontSize: 14, color: C.primary }}>
                Add Section
              </Text>
            </Pressable>
          </>
        ) : null}
      </ScrollView>

      <View style={st.footer}>
        <Button label="Preview" variant="secondary" style={{ flex: 1 }}
                onPress={() => setPreviewOpen(true)} disabled={!mom} />
        <Button label="Export" style={{ flex: 1 }} onPress={() => setMenuOpen(true)}
                loading={busy === "pdf" || busy === "docx"} disabled={!mom} />
      </View>

      {/* ---- Section editor ---- */}
      {section && mom ? (
        <SectionEditor
          section={section}
          onClose={() => setOpenSection(null)}
          onChange={mutate}
        />
      ) : null}

      {/* ---- Preview ---- */}
      {previewOpen && mom ? (
        <Modal visible animationType="slide" onRequestClose={() => setPreviewOpen(false)}>
          <View style={st.wrap}>
            <View style={st.bar}>
              <Pressable onPress={() => setPreviewOpen(false)} hitSlop={8}
                         accessibilityLabel="Close preview">
                <Icon name="xmark" tintColor={C.text} size={20} />
              </Pressable>
              <Text style={st.barTitle}>Preview</Text>
            </View>
            <MomPreview mom={mom} meetingTitle={meetingTitle} />
          </View>
        </Modal>
      ) : null}

      {/* ---- Add section ---- */}
      <AddSectionSheet
        visible={addOpen}
        onClose={() => setAddOpen(false)}
        onAdd={(kind, title) => {
          setAddOpen(false);
          let created = "";
          mutate((m) => {
            const { mom: next, section: s } = addSection(m, kind, title);
            created = s.id;
            return next;
          });
          // Straight into the new section — an empty row in the list is not a
          // useful place to leave the user.
          setTimeout(() => setOpenSection(created), 220);
        }}
      />

      {/* ---- Overflow menu ---- */}
      <Modal visible={menuOpen} transparent animationType="fade"
             onRequestClose={() => setMenuOpen(false)}>
        <Pressable style={st.backdrop} onPress={() => setMenuOpen(false)}>
          <Pressable style={st.sheet} onPress={() => {}}>
            <Text style={st.sheetTitle}>Minutes of Meeting</Text>
            <View style={{ height: 8 }} />
            <MenuRow st={st} C={C} icon="arrow.down.doc" label="Export as PDF"
                     disabled={!canExportPdf()}
                     note={canExportPdf() ? "" : "Needs a rebuilt app"}
                     onPress={() => exportAs("pdf")} />
            <MenuRow st={st} C={C} icon="arrow.down.doc" label="Export as Word (DOCX)"
                     onPress={() => exportAs("docx")} />
            <MenuRow st={st} C={C} icon="arrow.clockwise" label="Regenerate from meeting"
                     note="Your edits are kept" onPress={regenerate} />
            <MenuRow st={st} C={C} icon="trash" label="Start over"
                     tint={C.danger} onPress={reset} />
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}

function MenuRow({ st, C, icon, label, note, onPress, tint, disabled }: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  icon: Parameters<typeof Icon>[0]["name"];
  label: string;
  note?: string;
  onPress: () => void;
  tint?: string;
  disabled?: boolean;
}) {
  return (
    <Pressable onPress={onPress} disabled={disabled}
               style={({ pressed }) => [st.menuRow, (pressed || disabled) && { opacity: 0.55 }]}
               accessibilityLabel={label}>
      <Icon name={icon} tintColor={tint ?? C.text} size={18} />
      <View style={{ flex: 1 }}>
        <Text style={[st.menuTxt, tint ? { color: tint } : null]}>{label}</Text>
        {note ? (
          <Text style={{ fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint }}>
            {note}
          </Text>
        ) : null}
      </View>
    </Pressable>
  );
}

function SectionRow({
  st, C, section, index, total, onOpen, onToggle, onMove, onDelete,
}: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  section: MomSection;
  index: number;
  total: number;
  onOpen: () => void;
  onToggle: () => void;
  onMove: (delta: number) => void;
  onDelete: () => void;
}) {
  const hidden = !section.visible;
  const empty = isSectionEmpty(section);
  return (
    <Pressable
      onPress={onOpen}
      style={({ pressed }) => [st.sectionRow, pressed && { opacity: 0.75 },
                               hidden && { opacity: 0.55 }]}
      accessibilityLabel={`Edit ${section.title}`}
    >
      <Pressable onPress={onToggle} hitSlop={8}
                 accessibilityLabel={hidden ? "Show section" : "Hide section"}>
        <Icon name={hidden ? "square" : "checkmark.square.fill"}
              tintColor={hidden ? C.textFaint : C.primary} size={19} />
      </Pressable>

      <View style={{ flex: 1 }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          <Text style={st.sectionTitle} numberOfLines={1}>{section.title}</Text>
          {section.source !== "ai" ? <View style={st.editedDot} /> : null}
        </View>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 6, marginTop: 3 }}>
          <View style={st.kindPill}>
            <Text style={st.kindTxt}>{KIND_LABEL[section.kind]}</Text>
          </View>
          <Text style={st.sectionMeta}>
            {sectionSummary(section)}
            {empty ? " · empty" : ""}
            {hidden ? " · hidden" : ""}
          </Text>
        </View>
      </View>

      <Pressable onPress={() => onMove(-1)} hitSlop={6} disabled={index === 0}
                 accessibilityLabel="Move up"
                 style={index === 0 && { opacity: 0.25 }}>
        <Icon name="chevron.up" tintColor={C.textDim} size={16} />
      </Pressable>
      <Pressable onPress={() => onMove(1)} hitSlop={6} disabled={index === total - 1}
                 accessibilityLabel="Move down"
                 style={index === total - 1 && { opacity: 0.25 }}>
        <Icon name="chevron.down" tintColor={C.textDim} size={16} />
      </Pressable>
      <Pressable onPress={onDelete} hitSlop={6} accessibilityLabel="Delete section">
        <Icon name="trash" tintColor={C.danger} size={16} />
      </Pressable>
    </Pressable>
  );
}

// ===========================================================================
// Add section
// ===========================================================================
const NEW_SECTION_KINDS: {
  kind: MomSectionKind; label: string; hint: string; icon: string;
}[] = [
  { kind: "fields", label: "Fields", hint: "Label and value pairs, like Meeting Details",
    icon: "list.bullet" },
  { kind: "table", label: "Table", hint: "A grid with columns and rows",
    icon: "square.grid.2x2" },
  { kind: "text", label: "Text", hint: "A paragraph of prose", icon: "text.alignleft" },
  { kind: "list", label: "List", hint: "Bullet points", icon: "checklist" },
];

function AddSectionSheet({ visible, onClose, onAdd }: {
  visible: boolean;
  onClose: () => void;
  onAdd: (kind: MomSectionKind, title: string) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<MomSectionKind>("list");

  // Reset by REMOUNTING rather than by an effect that writes state: the caller
  // renders this only while it is open, so each open starts from a clean form
  // with no cascading render.
  if (!visible) return null;

  return (
    <Modal visible transparent animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView style={{ flex: 1 }}
                            behavior={Platform.OS === "ios" ? "padding" : undefined}>
        <Pressable style={st.backdrop} onPress={onClose}>
          <Pressable style={st.sheet} onPress={() => {}}>
            <Text style={st.sheetTitle}>Add section</Text>

            <Text style={[st.label, { marginTop: 16 }]}>SECTION NAME</Text>
            <TextInput
              style={st.input} value={title} onChangeText={setTitle}
              placeholder="e.g. Client Notes" placeholderTextColor={C.textFaint}
              autoFocus maxLength={120} returnKeyType="done"
            />

            <Text style={[st.label, { marginTop: 16 }]}>TYPE</Text>
            {NEW_SECTION_KINDS.map((k) => (
              <Pressable
                key={k.kind}
                onPress={() => setKind(k.kind)}
                style={({ pressed }) => [
                  st.fieldCard,
                  { flexDirection: "row", alignItems: "center", gap: 11, marginBottom: 7 },
                  kind === k.kind && { borderColor: C.primary, backgroundColor: C.primarySoft },
                  pressed && { opacity: 0.8 },
                ]}
                accessibilityLabel={k.label}
              >
                <Icon name={k.icon as any}
                      tintColor={kind === k.kind ? C.primary : C.textDim} size={17} />
                <View style={{ flex: 1 }}>
                  <Text style={{ fontFamily: FONT.semibold, fontSize: 14, color: C.text }}>
                    {k.label}
                  </Text>
                  <Text style={{ fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint }}>
                    {k.hint}
                  </Text>
                </View>
                {kind === k.kind ? (
                  <Icon name="checkmark.circle.fill" tintColor={C.primary} size={17} />
                ) : null}
              </Pressable>
            ))}

            <View style={{ flexDirection: "row", gap: S.sm, marginTop: 12 }}>
              <Button label="Cancel" variant="secondary" style={{ flex: 1 }}
                      onPress={onClose} />
              <Button label="Add" style={{ flex: 1 }}
                      disabled={!title.trim()}
                      onPress={() => onAdd(kind, title)} />
            </View>
          </Pressable>
        </Pressable>
      </KeyboardAvoidingView>
    </Modal>
  );
}

// ===========================================================================
// Section editor
// ===========================================================================
function SectionEditor({ section, onClose, onChange }: {
  section: MomSection;
  onClose: () => void;
  onChange: (fn: (m: Mom) => Mom) => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [renaming, setRenaming] = useState(false);
  const [draftTitle, setDraftTitle] = useState(section.title);
  const [editingRow, setEditingRow] = useState<string | null>(null);
  const [columnsOpen, setColumnsOpen] = useState(false);

  const id = section.id;

  return (
    <Modal visible animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView style={st.wrap}
                            behavior={Platform.OS === "ios" ? "padding" : undefined}>
        <View style={st.bar}>
          <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Back">
            <Icon name="chevron.left" tintColor={C.text} size={20} />
          </Pressable>
          <Text style={st.barTitle} numberOfLines={1}>{section.title}</Text>
          <Pressable onPress={() => { setDraftTitle(section.title); setRenaming(true); }}
                     hitSlop={8} accessibilityLabel="Rename section">
            <Icon name="pencil" tintColor={C.textDim} size={18} />
          </Pressable>
        </View>

        <ScrollView contentContainerStyle={st.body} keyboardShouldPersistTaps="handled"
                    showsVerticalScrollIndicator={false}>
          {section.kind === "fields" ? (
            <FieldsEditor st={st} C={C} section={section} onChange={onChange} />
          ) : null}

          {section.kind === "text" ? (
            <>
              <Text style={st.label}>CONTENT</Text>
              <TextInput
                style={[st.input, st.inputMulti]}
                value={section.text ?? ""}
                onChangeText={(v) => onChange((m) => updateText(m, id, v))}
                multiline placeholder="Write this section…"
                placeholderTextColor={C.textFaint}
              />
            </>
          ) : null}

          {section.kind === "list" ? (
            <ListEditor st={st} C={C} section={section} onChange={onChange} />
          ) : null}

          {section.kind === "table" ? (
            <TableEditor
              st={st} C={C} section={section} onChange={onChange}
              onEditRow={setEditingRow}
              onOpenColumns={() => setColumnsOpen(true)}
            />
          ) : null}
        </ScrollView>
      </KeyboardAvoidingView>

      {/* Rename */}
      {renaming ? (
        <Modal visible transparent animationType="fade"
               onRequestClose={() => setRenaming(false)}>
          <View style={st.backdrop}>
            <Pressable style={{ flex: 1 }} onPress={() => setRenaming(false)} />
            <View style={st.sheet}>
              <Text style={st.sheetTitle}>Rename section</Text>
              <TextInput
                style={[st.input, { marginTop: 14 }]}
                value={draftTitle} onChangeText={setDraftTitle}
                autoFocus maxLength={120} returnKeyType="done"
                onSubmitEditing={() => {
                  onChange((m) => renameSection(m, id, draftTitle));
                  setRenaming(false);
                }}
              />
              <View style={{ flexDirection: "row", gap: S.sm, marginTop: 18 }}>
                <Button label="Cancel" variant="secondary" style={{ flex: 1 }}
                        onPress={() => setRenaming(false)} />
                <Button label="Save" style={{ flex: 1 }}
                        onPress={() => {
                          onChange((m) => renameSection(m, id, draftTitle));
                          setRenaming(false);
                        }} />
              </View>
            </View>
          </View>
        </Modal>
      ) : null}

      {/* One row, full-width inputs — see the header note on why editing is
          row-at-a-time rather than in the grid. */}
      {editingRow ? (
        <RowEditor
          st={st} C={C} section={section} rowId={editingRow}
          onClose={() => setEditingRow(null)} onChange={onChange}
        />
      ) : null}

      {columnsOpen ? (
        <ColumnsEditor
          st={st} C={C} section={section}
          onClose={() => setColumnsOpen(false)} onChange={onChange}
        />
      ) : null}
    </Modal>
  );
}

// ---- fields ---------------------------------------------------------------
function FieldsEditor({ st, C, section, onChange }: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  section: MomSection;
  onChange: (fn: (m: Mom) => Mom) => void;
}) {
  const id = section.id;
  const fields = section.fields ?? [];
  return (
    <View>
      {fields.map((f) => (
        <View key={f.id} style={[st.fieldCard, !f.visible && { opacity: 0.55 }]}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
            <TextInput
              style={[st.input, { flex: 1, fontFamily: FONT.semibold }]}
              value={f.label}
              onChangeText={(v) => onChange((m) => updateField(m, id, f.id, { label: v }))}
              placeholder="Field name" placeholderTextColor={C.textFaint}
              maxLength={120}
            />
            <Pressable onPress={() => onChange((m) => toggleFieldVisible(m, id, f.id))}
                       hitSlop={8} accessibilityLabel={f.visible ? "Hide field" : "Show field"}>
              <Icon name={f.visible ? "eye" : "eye.slash"} tintColor={C.textDim} size={17} />
            </Pressable>
            <Pressable
              onPress={() => confirmDestroy(
                f.label ? `"${f.label}"` : "this field",
                () => onChange((m) => deleteField(m, id, f.id)))}
              hitSlop={8} accessibilityLabel="Delete field"
            >
              <Icon name="trash" tintColor={C.danger} size={16} />
            </Pressable>
          </View>
          <TextInput
            style={[st.input, { marginTop: 8 }]}
            value={f.value}
            onChangeText={(v) => onChange((m) => updateField(m, id, f.id, { value: v }))}
            placeholder="Value" placeholderTextColor={C.textFaint}
            multiline
          />
        </View>
      ))}
      <AddRowButton st={st} C={C} label="Add Field"
                    onPress={() => onChange((m) => addField(m, id))} />
    </View>
  );
}

// ---- list -----------------------------------------------------------------
function ListEditor({ st, C, section, onChange }: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  section: MomSection;
  onChange: (fn: (m: Mom) => Mom) => void;
}) {
  const id = section.id;
  const items = section.items ?? [];
  return (
    <View>
      {items.map((i) => (
        <View key={i.id} style={[st.fieldCard, { flexDirection: "row", alignItems: "flex-start", gap: 10 }]}>
          <Text style={{ fontSize: 15, color: C.primary, marginTop: 9 }}>•</Text>
          <TextInput
            style={[st.input, { flex: 1 }]}
            value={i.text}
            onChangeText={(v) => onChange((m) => updateListItem(m, id, i.id, v))}
            placeholder="Write a point…" placeholderTextColor={C.textFaint}
            multiline
          />
          <Pressable
            onPress={() => confirmDestroy("this point",
              () => onChange((m) => deleteListItem(m, id, i.id)))}
            hitSlop={8} accessibilityLabel="Delete point"
            style={{ marginTop: 9 }}
          >
            <Icon name="trash" tintColor={C.danger} size={16} />
          </Pressable>
        </View>
      ))}
      <AddRowButton st={st} C={C} label="Add Point"
                    onPress={() => onChange((m) => addListItem(m, id))} />
    </View>
  );
}

// ---- table ----------------------------------------------------------------
function TableEditor({ st, C, section, onChange, onEditRow, onOpenColumns }: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  section: MomSection;
  onChange: (fn: (m: Mom) => Mom) => void;
  onEditRow: (rowId: string) => void;
  onOpenColumns: () => void;
}) {
  const id = section.id;
  const columns = section.columns ?? [];
  const rows = section.rows ?? [];
  // A fixed per-column width plus horizontal scroll: proportional widths on a
  // phone give a five-column table ~60pt each, which is unreadable.
  const colWidth = 128;

  return (
    <View>
      <View style={{ flexDirection: "row", alignItems: "center", marginBottom: 10 }}>
        <Text style={[st.label, { flex: 1, marginBottom: 0 }]}>
          {rows.length} ROW{rows.length === 1 ? "" : "S"} · {columns.length} COLUMN
          {columns.length === 1 ? "" : "S"}
        </Text>
        <Pressable onPress={onOpenColumns} hitSlop={8} accessibilityLabel="Edit columns">
          <Text style={{ fontFamily: FONT.bold, fontSize: 12.5, color: C.primary }}>
            Columns
          </Text>
        </Pressable>
      </View>

      {columns.length && rows.length ? (
        <ScrollView horizontal showsHorizontalScrollIndicator style={st.grid}>
          <View>
            <View style={st.gridHead}>
              {columns.map((c) => (
                <Text key={c.id} style={[st.gridHeadTxt, { width: colWidth }]}
                      numberOfLines={2}>
                  {c.label.toUpperCase()}
                </Text>
              ))}
              <View style={{ width: 44 }} />
            </View>
            {rows.map((r) => (
              <Pressable
                key={r.id}
                onPress={() => onEditRow(r.id)}
                style={({ pressed }) => [st.gridRow, pressed && { opacity: 0.7 },
                                         !r.visible && { opacity: 0.5 }]}
                accessibilityLabel="Edit row"
              >
                {columns.map((c) => (
                  <Text key={c.id} style={[st.gridCell, { width: colWidth }]}
                        numberOfLines={3}>
                    {r.cells[c.id] ?? ""}
                  </Text>
                ))}
                <View style={{ width: 44, alignItems: "center", justifyContent: "center" }}>
                  <Pressable
                    onPress={() => confirmDestroy("this row",
                      () => onChange((m) => deleteRow(m, id, r.id)))}
                    hitSlop={8} accessibilityLabel="Delete row"
                  >
                    <Icon name="trash" tintColor={C.danger} size={15} />
                  </Pressable>
                </View>
              </Pressable>
            ))}
          </View>
        </ScrollView>
      ) : (
        <View style={[st.fieldCard, { alignItems: "center", paddingVertical: 18 }]}>
          <Text style={{ fontFamily: FONT.regular, fontSize: 13, color: C.textFaint }}>
            {columns.length ? "No rows yet." : "Add a column to start this table."}
          </Text>
        </View>
      )}

      <Text style={{ fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
                     marginTop: 8 }}>
        Tap a row to edit its cells.
      </Text>

      <AddRowButton st={st} C={C} label="Add Row"
                    disabled={!columns.length}
                    onPress={() => onChange((m) => addRow(m, id))} />
    </View>
  );
}

function RowEditor({ st, C, section, rowId, onClose, onChange }: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  section: MomSection;
  rowId: string;
  onClose: () => void;
  onChange: (fn: (m: Mom) => Mom) => void;
}) {
  const row = (section.rows ?? []).find((r) => r.id === rowId);
  const columns = section.columns ?? [];
  if (!row) return null;
  return (
    <Modal visible transparent animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView style={{ flex: 1 }}
                            behavior={Platform.OS === "ios" ? "padding" : undefined}>
        <Pressable style={st.backdrop} onPress={onClose}>
          <Pressable style={st.sheet} onPress={() => {}}>
            <View style={{ flexDirection: "row", alignItems: "center" }}>
              <Text style={[st.sheetTitle, { flex: 1 }]}>Edit row</Text>
              <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Done">
                <Text style={st.barAction}>Done</Text>
              </Pressable>
            </View>
            <ScrollView style={{ marginTop: 12 }} keyboardShouldPersistTaps="handled">
              {columns.map((c) => (
                <View key={c.id} style={{ marginBottom: 12 }}>
                  <Text style={st.label}>{c.label.toUpperCase()}</Text>
                  <TextInput
                    style={st.input}
                    value={row.cells[c.id] ?? ""}
                    onChangeText={(v) =>
                      onChange((m) => updateCell(m, section.id, row.id, c.id, v))}
                    placeholder={c.label} placeholderTextColor={C.textFaint}
                    multiline
                  />
                </View>
              ))}
            </ScrollView>
          </Pressable>
        </Pressable>
      </KeyboardAvoidingView>
    </Modal>
  );
}

function ColumnsEditor({ st, C, section, onClose, onChange }: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  section: MomSection;
  onClose: () => void;
  onChange: (fn: (m: Mom) => Mom) => void;
}) {
  const columns = section.columns ?? [];
  const id = section.id;
  const last = columns.length <= 1;
  return (
    <Modal visible transparent animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView style={{ flex: 1 }}
                            behavior={Platform.OS === "ios" ? "padding" : undefined}>
        <Pressable style={st.backdrop} onPress={onClose}>
          <Pressable style={st.sheet} onPress={() => {}}>
            <View style={{ flexDirection: "row", alignItems: "center" }}>
              <Text style={[st.sheetTitle, { flex: 1 }]}>Columns</Text>
              <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Done">
                <Text style={st.barAction}>Done</Text>
              </Pressable>
            </View>
            <ScrollView style={{ marginTop: 12 }} keyboardShouldPersistTaps="handled">
              {columns.map((c) => (
                <View key={c.id} style={{ flexDirection: "row", alignItems: "center",
                                          gap: 10, marginBottom: 9 }}>
                  <TextInput
                    style={[st.input, { flex: 1 }]}
                    value={c.label}
                    onChangeText={(v) => onChange((m) => renameColumn(m, id, c.id, v))}
                    placeholder="Column name" placeholderTextColor={C.textFaint}
                    maxLength={120}
                  />
                  <Pressable
                    onPress={() => confirmDestroy(
                      `the "${c.label}" column and its cells`,
                      () => onChange((m) => deleteColumn(m, id, c.id)))}
                    hitSlop={8} disabled={last}
                    accessibilityLabel="Delete column"
                    style={last && { opacity: 0.3 }}
                  >
                    <Icon name="trash" tintColor={C.danger} size={16} />
                  </Pressable>
                </View>
              ))}
              {last ? (
                <Text style={{ fontFamily: FONT.regular, fontSize: 11.5,
                               color: C.textFaint, marginBottom: 8 }}>
                  A table needs at least one column.
                </Text>
              ) : null}
              <AddRowButton st={st} C={C} label="Add Column"
                            onPress={() => onChange((m) => addColumn(m, id))} />
            </ScrollView>
          </Pressable>
        </Pressable>
      </KeyboardAvoidingView>
    </Modal>
  );
}

function AddRowButton({ st, C, label, onPress, disabled }: {
  st: ReturnType<typeof buildStyles>;
  C: ColorScale;
  label: string;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress} disabled={disabled}
      style={({ pressed }) => [
        st.fieldCard,
        { flexDirection: "row", alignItems: "center", justifyContent: "center",
          gap: 7, borderStyle: "dashed", marginTop: 4 },
        (pressed || disabled) && { opacity: 0.55 },
      ]}
      accessibilityLabel={label}
    >
      <Icon name="plus" tintColor={C.primary} size={15} />
      <Text style={{ fontFamily: FONT.bold, fontSize: 13.5, color: C.primary }}>
        {label}
      </Text>
    </Pressable>
  );
}
