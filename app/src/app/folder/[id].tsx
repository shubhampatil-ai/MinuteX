// src/app/folder/[id].tsx — one folder: its meetings and its people.
//
// This is a VIEW over All Meetings, not a container. The meetings listed here
// are the same rows MinuteX shows, filtered by folder_id — which is why
// "Remove from folder" is worded that way rather than "Delete": it moves the
// meeting back to General and touches nothing else.
//
// The special id "general" renders the meetings that have NO folder. It has no
// contacts section and no rename/delete, because General is the absence of a
// folder rather than a folder itself.
//
// Contacts are shown here because a folder's people are the ones offered first
// when mapping speakers in its meetings (see lib/contact-picker.tsx). Removing
// one removes the ASSOCIATION — the contact stays in the global list.
import { useCallback, useMemo, useState } from "react";
import {
  Alert, FlatList, Pressable, RefreshControl, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { Icon } from "../../../lib/icons";
import {
  S, R, ELEV, FONT, TABULAR, useTheme, ColorScale,
} from "../../../lib/theme";
import {
  Button, EmptyState, ErrorText, IconCircle, SegmentedTabs, SkeletonCard,
} from "../../../lib/ui";
import {
  ApiContact, ApiError, ApiFolder, RecordingSummary, addContactToFolder,
  getFolder, getFolderContacts, getRecordings, moveRecordingToFolder,
  removeContactFromFolder,
} from "../../../lib/api";
import { ContactPicker } from "../../../lib/contact-picker";
import { fmtDuration, statusMeta } from "../../../lib/sources";
import { avatarColorFor, initialsOf } from "../../../lib/task-model";

const GENERAL = "general";

export default function FolderDetailScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id: string }>();
  const folderId = String(id || "");
  const isGeneral = folderId === GENERAL;

  const [folder, setFolder] = useState<ApiFolder | null>(null);
  const [contacts, setContacts] = useState<ApiContact[]>([]);
  const [meetings, setMeetings] = useState<RecordingSummary[]>([]);
  const [tab, setTab] = useState("meetings");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);

  const load = useCallback(
    async (isRefresh = false) => {
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        // The meeting list comes from the SAME endpoint MinuteX uses and is
        // filtered by folder here — there is one master collection, and this
        // screen must not imply otherwise by reading a different source.
        const all = await getRecordings();
        setMeetings(
          all.filter((r) =>
            isGeneral ? !r.folder_id : r.folder_id === folderId
          )
        );
        if (!isGeneral) {
          const res = await getFolder(folderId);
          setFolder(res.folder);
          setContacts(res.contacts);
        }
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Could not load this folder."
        );
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [folderId, isGeneral]
  );

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  const removeMeeting = useCallback(
    (rec: RecordingSummary) => {
      Alert.alert(
        "Remove from folder?",
        "The meeting moves back to General. Nothing is deleted.",
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Remove",
            onPress: async () => {
              try {
                await moveRecordingToFolder(rec.audio_s3_key, null);
                await load();
              } catch (e) {
                Alert.alert(
                  "Could not move",
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

  const unlinkContact = useCallback(
    (c: ApiContact) => {
      Alert.alert(
        `Remove ${c.name} from this folder?`,
        // Say plainly what is NOT happening — otherwise this reads as deleting
        // the person.
        "They stay in your contacts and in any other folder they belong to.",
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Remove",
            onPress: async () => {
              try {
                await removeContactFromFolder(folderId, c.id);
                setContacts((prev) => prev.filter((x) => x.id !== c.id));
              } catch (e) {
                Alert.alert(
                  "Could not remove",
                  e instanceof ApiError ? e.message : "Please try again."
                );
              }
            },
          },
        ]
      );
    },
    [folderId]
  );

  const linkContact = useCallback(
    async (c: ApiContact) => {
      try {
        await addContactToFolder(folderId, c.id);
        // Re-read rather than pushing locally: the create-inside-folder path
        // already associated the contact, so a local push could double it.
        setContacts(await getFolderContacts(folderId));
      } catch (e) {
        Alert.alert(
          "Could not add",
          e instanceof ApiError ? e.message : "Please try again."
        );
      }
    },
    [folderId]
  );

  const title = isGeneral ? "General" : folder?.name || "Folder";

  const renderMeeting = ({ item }: { item: RecordingSummary }) => {
    const status = statusMeta(item.status);
    return (
      <Pressable
        style={st.card}
        onPress={() =>
          router.push({
            pathname: "/recording/[key]",
            params: { key: item.audio_s3_key },
          } as any)
        }
        accessibilityRole="button"
        accessibilityLabel={`Open ${item.title || "meeting"}`}
      >
        <View style={st.top}>
          <IconCircle name="waveform" />
          <View style={{ flex: 1 }}>
            <Text style={st.headline} numberOfLines={2}>
              {item.title || "Untitled meeting"}
            </Text>
            <Text style={st.meta}>
              {fmtDuration(item.duration)} · {status.label}
            </Text>
          </View>
          <Icon name="chevron.right" size={16} tintColor={C.textFaint} />
        </View>
        {!isGeneral && (
          <View style={st.actions}>
            <Pressable
              style={st.action}
              onPress={() => removeMeeting(item)}
              hitSlop={6}
              accessibilityRole="button"
              accessibilityLabel="Remove from folder"
            >
              <Icon name="tray.fill" size={13} tintColor={C.textDim} />
              <Text style={[st.actionTxt, { color: C.textDim }]}>
                Remove from folder
              </Text>
            </Pressable>
          </View>
        )}
      </Pressable>
    );
  };

  const renderContact = ({ item }: { item: ApiContact }) => (
    <Pressable
      style={st.card}
      onPress={() =>
        router.push({
          pathname: "/contact/[id]",
          params: { id: item.id },
        } as any)
      }
      accessibilityRole="button"
      accessibilityLabel={`Open ${item.name}`}
    >
      <View style={st.top}>
        <View style={[st.avatar, { backgroundColor: avatarColorFor(item.name) }]}>
          <Text style={st.avatarTxt}>{initialsOf(item.name)}</Text>
        </View>
        <View style={{ flex: 1 }}>
          <Text style={st.headline} numberOfLines={1}>{item.name}</Text>
          {!!(item.email || item.company) && (
            <Text style={st.meta} numberOfLines={1}>
              {item.email || item.company}
            </Text>
          )}
        </View>
        <Pressable
          onPress={() => unlinkContact(item)}
          hitSlop={10}
          accessibilityRole="button"
          accessibilityLabel={`Remove ${item.name} from folder`}
        >
          <Icon name="xmark" size={15} tintColor={C.textFaint} />
        </Pressable>
      </View>
    </Pressable>
  );

  if (loading) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title }} />
        <SkeletonCard />
        <SkeletonCard />
      </View>
    );
  }

  if (error) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title }} />
        <View style={{ gap: S.md, marginTop: S.lg }}>
          <ErrorText>{error}</ErrorText>
          <Button label="Retry" variant="secondary" onPress={() => load()} />
        </View>
      </View>
    );
  }

  const showingContacts = !isGeneral && tab === "contacts";

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title }} />

      {!!folder?.description && (
        <Text style={st.intro}>{folder.description}</Text>
      )}

      {!isGeneral && (
        <SegmentedTabs
          tabs={[
            { key: "meetings", label: `Meetings (${meetings.length})` },
            { key: "contacts", label: `Contacts (${contacts.length})` },
          ]}
          value={tab}
          onChange={setTab}
        />
      )}

      {showingContacts ? (
        <FlatList
          data={contacts}
          keyExtractor={(c) => c.id}
          renderItem={renderContact}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => load(true)}
              tintColor={C.primary}
            />
          }
          ListEmptyComponent={
            <EmptyState
              icon="person.fill"
              title="No contacts in this folder"
              subtitle="Add the people involved, and they will be offered first when you map speakers in this folder's meetings."
              action={
                <Button
                  label="Add Contact"
                  onPress={() => setPickerOpen(true)}
                />
              }
            />
          }
          ListFooterComponent={
            contacts.length > 0 ? (
              <View style={{ marginTop: S.md, marginBottom: S.xxl }}>
                <Button
                  label="+ Add Contact"
                  variant="secondary"
                  onPress={() => setPickerOpen(true)}
                />
              </View>
            ) : (
              <View style={{ height: S.xxl }} />
            )
          }
          contentContainerStyle={{ paddingTop: S.md }}
          showsVerticalScrollIndicator={false}
        />
      ) : (
        <FlatList
          data={meetings}
          keyExtractor={(r) => r.audio_s3_key}
          renderItem={renderMeeting}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => load(true)}
              tintColor={C.primary}
            />
          }
          ListEmptyComponent={
            <EmptyState
              icon="waveform"
              title={
                isGeneral ? "Every meeting is filed" : "No meetings in this folder"
              }
              subtitle={
                isGeneral
                  ? "Meetings with no folder appear here."
                  : "Open a meeting and use Move to Folder to file it here."
              }
            />
          }
          ListFooterComponent={<View style={{ height: S.xxl }} />}
          contentContainerStyle={{ paddingTop: S.md }}
          showsVerticalScrollIndicator={false}
        />
      )}

      {/* Record straight into this folder. The whole point of the button being
          HERE rather than only on MinuteX: the folder is already the context,
          so the recording is filed at presign time and never has to be moved.
          General gets one too — it is where an unfiled recording would land
          anyway, so the button is honest there as well. */}
      <Pressable
        style={({ pressed }) => [st.fab, pressed && { opacity: 0.85 }]}
        onPress={() =>
          router.push({
            pathname: "/new-recording",
            params: isGeneral ? {} : { folderId },
          } as any)
        }
        accessibilityRole="button"
        accessibilityLabel={
          isGeneral ? "New recording" : `New recording in ${title}`
        }
      >
        <Icon name="mic.fill" size={21} tintColor={C.textOnPrimary} />
        <Text style={st.fabTxt}>Record</Text>
      </Pressable>

      <ContactPicker
        visible={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onPick={linkContact}
        folderId={folderId}
        folderName={folder?.name}
        title="Add Contact to Folder"
      />
    </View>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    intro: { ...T.caption, marginTop: 2, marginBottom: S.md },
    card: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      marginBottom: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    top: {
      flexDirection: "row" as const, alignItems: "flex-start" as const,
      gap: S.md,
    },
    headline: { ...T.headlineSm },
    meta: { ...TABULAR, fontSize: 11.5, color: C.textFaint, marginTop: 3 },
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
    avatar: {
      width: 40, height: 40, borderRadius: 20, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 14, color: "#fff" },
    // Pill FAB rather than a bare circle: "Record" needs a word next to it
    // here, because on this screen a lone mic could read as "record a note
    // about the folder" rather than "start a meeting in it".
    fab: {
      position: "absolute" as const, right: 20, bottom: 26,
      flexDirection: "row" as const, alignItems: "center" as const, gap: 8,
      paddingHorizontal: 18, paddingVertical: 13, borderRadius: R.pill,
      backgroundColor: C.primary, shadowColor: C.shadow, ...ELEV.md,
    },
    fabTxt: {
      fontFamily: FONT.bold, fontSize: 14, color: C.textOnPrimary,
      letterSpacing: 0.2,
    },
  });
}
