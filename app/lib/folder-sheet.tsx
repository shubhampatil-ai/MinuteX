// lib/folder-sheet.tsx — the New / Edit folder bottom sheet.
//
// Name, colour, icon, one big Create button — the shape of a file-manager's
// folder sheet, because that is the interaction users already know.
//
// Three details that matter:
//
//   * Colour is a row of swatches, always visible. Folder colour is the thing
//     that makes a folder list scannable, so hiding it behind a sub-screen
//     would mean most folders stay default-coloured.
//   * Icon opens a grid, because twelve icons do not fit on one row without
//     becoming unhittable. The row shows the CURRENT icon so the closed state
//     still tells you what you picked.
//   * Create stays disabled until there is a name. A folder with no name is
//     the one thing the backend rejects, so the button should not offer it.
//
// Used for BOTH create and edit — the fields and validation are identical and
// `folder` is what distinguishes them. Two sheets would drift.
import { useEffect, useMemo, useState } from "react";
import {
  Modal, Pressable, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Icon } from "./icons";
import { S, R, CAPS, FONT, useTheme, ColorScale } from "./theme";
import {
  Button, ErrorText, KeyboardAwareSheet, TextField, scrollFormProps,
} from "./ui";
import {
  ApiError, ApiFolder, FolderColor, FolderIconToken, createFolder,
  updateFolder,
} from "./api";
import {
  FOLDER_COLOR_TOKENS, FOLDER_ICON_TOKENS, folderIcon, folderSwatch,
} from "./folder-appearance";

export type FolderSheetProps = {
  visible: boolean;
  onClose: () => void;
  /** Absent = create. Present = edit that folder. */
  folder?: ApiFolder | null;
  onSaved: (folder: ApiFolder) => void;
};

export function FolderSheet({
  visible, onClose, folder, onSaved,
}: FolderSheetProps) {
  const { C, T, mode } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const dark = mode === "dark";
  const editing = !!folder;

  const [name, setName] = useState("");
  const [color, setColor] = useState<FolderColor>("slate");
  const [icon, setIcon] = useState<FolderIconToken>("folder");
  const [iconOpen, setIconOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // Re-seed every time the sheet opens, so a half-typed name from a previous
  // open never reappears — and so Edit always starts from the real folder.
  useEffect(() => {
    if (!visible) return;
    setName(folder?.name ?? "");
    setColor((folder?.color as FolderColor) ?? "slate");
    setIcon((folder?.icon as FolderIconToken) ?? "folder");
    setIconOpen(false);
    setError("");
  }, [visible, folder]);

  const trimmed = name.trim();

  const save = async () => {
    if (!trimmed) {
      setError("Enter a folder name.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const saved = editing
        ? await updateFolder(folder!.id, { name: trimmed, color, icon })
        : await createFolder({ name: trimmed, color, icon });
      onSaved(saved);
      onClose();
    } catch (e) {
      // 409 is the duplicate-name case and deserves its own wording: the fix
      // is to pick another name, which the generic message doesn't convey.
      setError(
        e instanceof ApiError && e.status === 409
          ? "You already have a folder with that name."
          : e instanceof ApiError
            ? e.message
            : "Could not save the folder."
      );
    } finally {
      setSaving(false);
    }
  };

  const swatch = folderSwatch(color, dark);

  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent
      onRequestClose={onClose}
    >
      <KeyboardAwareSheet>
        <View style={st.backdrop}>
          <Pressable style={st.backdropTap} onPress={onClose} />
          <View style={st.sheet}>
            {/* Header — title and a close affordance, over a hairline. */}
            <View style={st.head}>
              <Text style={st.title}>
                {editing ? "Edit folder" : "New folder"}
              </Text>
              <Pressable
                onPress={onClose}
                hitSlop={14}
                accessibilityRole="button"
                accessibilityLabel="Close"
              >
                <Icon name="xmark" size={19} tintColor={C.textDim} />
              </Pressable>
            </View>
            <View style={st.rule} />

            <ScrollView {...scrollFormProps}>
              {/* Name */}
              <View style={st.field}>
                <Text style={st.fieldLabel}>Name</Text>
                <TextField
                  value={name}
                  onChangeText={setName}
                  placeholder=""
                  autoFocus={!editing}
                  autoCapitalize="words"
                  maxLength={80}
                  style={st.nameInput}
                />
                {!!name && (
                  <Pressable
                    onPress={() => setName("")}
                    hitSlop={12}
                    accessibilityRole="button"
                    accessibilityLabel="Clear name"
                  >
                    <Icon name="xmark" size={16} tintColor={C.textFaint} />
                  </Pressable>
                )}
              </View>

              {/* Colour — always visible, because colour is what makes a folder
                  list scannable. */}
              <View style={st.field}>
                <Text style={st.fieldLabel}>Color</Text>
                <View style={st.swatchRow}>
                  {FOLDER_COLOR_TOKENS.map((token) => {
                    const sw = folderSwatch(token, dark);
                    const on = token === color;
                    return (
                      <Pressable
                        key={token}
                        onPress={() => setColor(token)}
                        style={[
                          st.swatch,
                          { backgroundColor: sw.solid },
                          on && { borderColor: C.text, borderWidth: 2.5 },
                        ]}
                        accessibilityRole="button"
                        accessibilityState={{ selected: on }}
                        accessibilityLabel={`Colour ${token}`}
                      >
                        {on ? (
                          <Icon name="checkmark" size={13} tintColor="#fff" />
                        ) : null}
                      </Pressable>
                    );
                  })}
                </View>
              </View>

              {/* Icon — a row that shows the current pick and opens a grid. */}
              <Pressable
                style={st.field}
                onPress={() => setIconOpen((v) => !v)}
                accessibilityRole="button"
                accessibilityLabel="Choose icon"
                accessibilityState={{ expanded: iconOpen }}
              >
                <Text style={st.fieldLabel}>Icon</Text>
                <View style={{ flex: 1 }} />
                <View style={[st.iconChip, { backgroundColor: swatch.soft }]}>
                  <Icon name={folderIcon(icon)} size={17} tintColor={swatch.solid} />
                </View>
                <Icon
                  name={iconOpen ? "chevron.down" : "chevron.right"}
                  size={16}
                  tintColor={C.textFaint}
                />
              </Pressable>

              {iconOpen && (
                <View style={st.iconGrid}>
                  {FOLDER_ICON_TOKENS.map((token) => {
                    const on = token === icon;
                    return (
                      <Pressable
                        key={token}
                        onPress={() => {
                          setIcon(token);
                          setIconOpen(false);
                        }}
                        style={[
                          st.iconCell,
                          on && {
                            backgroundColor: swatch.soft,
                            borderColor: swatch.solid,
                          },
                        ]}
                        accessibilityRole="button"
                        accessibilityState={{ selected: on }}
                        accessibilityLabel={`Icon ${token}`}
                      >
                        <Icon
                          name={folderIcon(token)}
                          size={19}
                          tintColor={on ? swatch.solid : C.textDim}
                        />
                      </Pressable>
                    );
                  })}
                </View>
              )}

              {!!error && (
                <View style={{ marginTop: S.md }}>
                  <ErrorText>{error}</ErrorText>
                </View>
              )}

              <View style={st.cta}>
                <Button
                  label={editing ? "Save" : "Create"}
                  onPress={save}
                  loading={saving}
                  // A nameless folder is the one thing the backend refuses, so
                  // don't offer the button in a state that must fail.
                  disabled={saving || !trimmed}
                />
              </View>
              </ScrollView>
          </View>
        </View>
      </KeyboardAwareSheet>
    </Modal>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)" },
    // Tapping the dimmed area closes — expected of a bottom sheet, and the
    // only way out on a device whose back gesture is disabled mid-modal.
    backdropTap: { flex: 1 },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.xl,
      borderTopRightRadius: R.xl, paddingHorizontal: 22, paddingTop: S.lg,
      paddingBottom: S.xxl,
      // flexShrink so the sheet contracts when KeyboardAwareSheet reduces the
      // frame; its inner ScrollView then scrolls, keeping the name field and
      // the Create button reachable with the keyboard up.
      maxHeight: "86%", flexShrink: 1,
    },
    head: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const, paddingBottom: S.md,
    },
    title: { ...T.headline, fontSize: 25 },
    rule: { height: 1, backgroundColor: C.border, marginBottom: S.lg },
    // Each control is its own outlined row — label on the left, control on the
    // right — which is what makes the three read as one stack of settings.
    field: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      borderWidth: 1, borderColor: C.borderStrong, borderRadius: R.md,
      paddingHorizontal: 16, minHeight: 58, marginBottom: S.md,
    },
    fieldLabel: {
      fontFamily: FONT.medium, fontSize: 14.5, color: C.textDim,
    },
    // The name input has to sit flush inside the row rather than bring its own
    // border, or the row would render a box inside a box.
    nameInput: {
      flex: 1, borderWidth: 0, backgroundColor: "transparent",
      paddingHorizontal: 0, paddingVertical: 0, minHeight: 0,
    },
    swatchRow: {
      flex: 1, flexDirection: "row" as const, justifyContent: "flex-end" as const,
      alignItems: "center" as const, gap: 9, paddingVertical: 10,
    },
    swatch: {
      width: 30, height: 30, borderRadius: 9, alignItems: "center" as const,
      justifyContent: "center" as const, borderColor: "transparent",
      borderWidth: 2.5,
    },
    iconChip: {
      width: 34, height: 34, borderRadius: R.sm,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    iconGrid: {
      flexDirection: "row" as const, flexWrap: "wrap" as const, gap: S.sm,
      marginTop: -S.xs, marginBottom: S.md, paddingHorizontal: 2,
    },
    iconCell: {
      width: 52, height: 48, borderRadius: R.md, borderWidth: 1,
      borderColor: C.border, backgroundColor: C.surface,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    cta: { marginTop: S.lg },
  });
}
