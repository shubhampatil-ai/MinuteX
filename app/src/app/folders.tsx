// src/app/folders.tsx — Folders: organizational views over the ONE master
// meeting collection.
//
// The mental model this screen has to convey, because getting it wrong is how
// users lose trust in a filing system: a folder does NOT contain copies of
// meetings. Every meeting lives in All Meetings exactly once and carries at
// most one folder. So:
//
//   * moving a meeting between folders moves the original, it never copies;
//   * "General" is not a folder, it is the absence of one — which is why it
//     appears here as a row with a count but no rename/delete;
//   * deleting a folder deletes the FOLDER. Its meetings return to General,
//     its contacts stay in your contact list, its tasks keep existing. The
//     confirm dialog states each of those counts, because "delete" next to a
//     list of meetings is otherwise a terrifying and ambiguous button.
//
// Layout follows Trash and The Desk (masthead, cards, skeletons,
// pull-to-refresh, stated empty state) so this reads as the same product.
import { useCallback, useMemo, useState } from "react";
import {
  Alert, FlatList, Pressable, RefreshControl, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, ELEV, FONT, TABULAR, useTheme, ColorScale } from "../../lib/theme";
import {
  Button, EmptyState, ErrorText, SkeletonCard,
} from "../../lib/ui";
import { ApiError, ApiFolder, deleteFolder, getFolders } from "../../lib/api";
import { FolderSheet } from "../../lib/folder-sheet";
import { folderIcon, folderSwatch } from "../../lib/folder-appearance";

export default function FoldersScreen() {
  const { C, T, mode } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();

  const [folders, setFolders] = useState<ApiFolder[]>([]);
  const [generalCount, setGeneralCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  // Create and edit share ONE sheet (lib/folder-sheet.tsx) — same fields, same
  // validation; `editing` is all that distinguishes them.
  const [sheetOpen, setSheetOpen] = useState(false);
  const [editing, setEditing] = useState<ApiFolder | null>(null);

  const load = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      const res = await getFolders();
      setFolders(res.folders);
      setGeneralCount(res.general_count);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load folders.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  const openCreate = () => {
    setEditing(null);
    setSheetOpen(true);
  };

  const openEdit = (f: ApiFolder) => {
    setEditing(f);
    setSheetOpen(true);
  };

  const confirmDelete = useCallback(
    (f: ApiFolder) => {
      const count = f.meeting_count ?? 0;
      Alert.alert(
        `Delete "${f.name}"?`,
        // Spell out what SURVIVES. This is the difference between a user
        // deleting a folder confidently and never touching the feature again.
        count > 0
          ? `The folder will be removed. Its ${count} meeting${
              count === 1 ? "" : "s"
            } will move to General — nothing is deleted, and your contacts and tasks are kept.`
          : "The folder will be removed. Your contacts and tasks are kept.",
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Delete Folder",
            style: "destructive",
            onPress: async () => {
              try {
                const res = await deleteFolder(f.id);
                await load();
                if (res.meetings_moved > 0) {
                  Alert.alert(
                    "Folder deleted",
                    `${res.meetings_moved} meeting${
                      res.meetings_moved === 1 ? "" : "s"
                    } moved to General.`
                  );
                }
              } catch (e) {
                Alert.alert(
                  "Could not delete",
                  e instanceof ApiError ? e.message : "Please try again."
                );
              }
            },
          },
        ]
      );
    },
    [load]
  );

  const renderFolder = ({ item }: { item: ApiFolder }) => {
    const count = item.meeting_count ?? 0;
    const sw = folderSwatch(item.color, mode === "dark");
    return (
      <Pressable
        style={st.card}
        onPress={() =>
          router.push({
            pathname: "/folder/[id]",
            params: { id: item.id },
          } as any)
        }
        accessibilityRole="button"
        accessibilityLabel={`Open folder ${item.name}`}
      >
        <View style={st.top}>
          <View style={[st.folderChip, { backgroundColor: sw.soft }]}>
            <Icon name={folderIcon(item.icon)} size={19} tintColor={sw.solid} />
          </View>
          <View style={{ flex: 1 }}>
            <Text style={st.headline} numberOfLines={1}>{item.name}</Text>
            <Text style={st.meta}>
              {count} meeting{count === 1 ? "" : "s"}
            </Text>
            {!!item.description && (
              <Text style={st.desc} numberOfLines={2}>{item.description}</Text>
            )}
          </View>
          <Icon name="chevron.right" size={16} tintColor={C.textFaint} />
        </View>
        <View style={st.actions}>
          <Pressable
            style={st.action}
            onPress={() => openEdit(item)}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel={`Edit ${item.name}`}
          >
            <Icon name="pencil" size={13} tintColor={C.primary} />
            <Text style={[st.actionTxt, { color: C.primary }]}>Edit</Text>
          </Pressable>
          <Pressable
            style={st.action}
            onPress={() => confirmDelete(item)}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel={`Delete ${item.name}`}
          >
            <Icon name="trash" size={13} tintColor={C.danger} />
            <Text style={[st.actionTxt, { color: C.danger }]}>Delete</Text>
          </Pressable>
        </View>
      </Pressable>
    );
  };

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Folders" }} />
      <Text style={st.intro}>
        Folders organize your meetings. A meeting lives in one folder at a
        time — moving it never makes a copy.
      </Text>

      {loading ? (
        <>
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </>
      ) : error ? (
        <View style={{ gap: S.md }}>
          <ErrorText>{error}</ErrorText>
          <Button label="Retry" variant="secondary" onPress={() => load()} />
        </View>
      ) : (
        <FlatList
          data={folders}
          keyExtractor={(f) => f.id}
          renderItem={renderFolder}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => load(true)}
              tintColor={C.primary}
            />
          }
          ListHeaderComponent={
            // "General" is the absence of a folder, so it gets a row (users
            // need to reach those meetings) but no rename or delete.
            <Pressable
              style={[st.card, st.generalCard]}
              onPress={() =>
                router.push({
                  pathname: "/folder/[id]",
                  params: { id: "general" },
                } as any)
              }
              accessibilityRole="button"
              accessibilityLabel="Open General"
            >
              <View style={st.top}>
                <View style={[st.folderChip, { backgroundColor: C.surface2 }]}>
                  <Icon name="tray.fill" size={19} tintColor={C.textDim} />
                </View>
                <View style={{ flex: 1 }}>
                  <Text style={st.headline}>General</Text>
                  <Text style={st.meta}>
                    {generalCount} meeting{generalCount === 1 ? "" : "s"} · not
                    in a folder
                  </Text>
                </View>
                <Icon name="chevron.right" size={16} tintColor={C.textFaint} />
              </View>
            </Pressable>
          }
          ListEmptyComponent={
            <EmptyState
              icon="folder"
              title="No folders yet"
              subtitle="Group meetings by client, project or team. Your meetings stay in All Meetings either way."
              action={<Button label="Create Folder" onPress={openCreate} />}
            />
          }
          ListFooterComponent={
            folders.length > 0 ? (
              <View style={{ marginTop: S.md, marginBottom: S.xxl }}>
                <Button
                  label="+ New Folder"
                  variant="secondary"
                  onPress={openCreate}
                />
              </View>
            ) : (
              <View style={{ height: S.xxl }} />
            )
          }
          contentContainerStyle={{ paddingTop: S.xs }}
          showsVerticalScrollIndicator={false}
        />
      )}

      <FolderSheet
        visible={sheetOpen}
        onClose={() => setSheetOpen(false)}
        folder={editing}
        onSaved={() => load()}
      />
    </View>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    intro: { ...T.caption, marginTop: 2, marginBottom: S.lg },
    card: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      marginBottom: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    generalCard: { borderWidth: 1, borderColor: C.border },
    // Square-ish chip rather than IconCircle: it carries the folder's own
    // colour, so it needs to read as a swatch with an icon in it.
    folderChip: {
      width: 42, height: 42, borderRadius: R.md,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    top: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.md,
    },
    headline: { ...T.headlineSm },
    meta: { ...TABULAR, fontSize: 11.5, color: C.textFaint, marginTop: 3 },
    desc: { ...T.bodyDim, fontSize: 12.5, marginTop: 5 },
    actions: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
      marginTop: 14, paddingTop: 12, borderTopWidth: 1,
      borderTopColor: C.border,
    },
    action: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      paddingVertical: 6, paddingHorizontal: 10, borderRadius: R.pill,
    },
    actionTxt: { fontFamily: FONT.bold, fontSize: 12.5 },
    backdrop: {
      flex: 1, backgroundColor: "rgba(0,0,0,0.45)",
      justifyContent: "flex-end" as const,
    },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.xl,
      borderTopRightRadius: R.xl, paddingHorizontal: 20, paddingTop: S.lg,
      paddingBottom: S.xxl,
    },
    sheetHead: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const, marginBottom: S.md,
    },
    sheetTitle: { ...T.headlineSm },
  });
}
