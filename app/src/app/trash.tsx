// src/app/trash.tsx — Trash: briefs the user deleted, and the two ways out.
//
// Deleting on MinuteX is a SOFT delete — the recording, its transcript and
// every AI artifact stay exactly where they were, with a `trashed` flag on the
// row. This screen is where that becomes visible and reversible:
//
//   Restore            -> back to MinuteX, transcript and AI output intact
//   Delete permanently -> the ONLY action in the app that destroys data
//
// The two are deliberately not peers. Restore is the quiet, safe default and
// reads as a normal action; permanent delete is danger-coloured, sits behind a
// blunt confirm, and is the only place the phrase "cannot be undone" appears.
//
// Layout mirrors MinuteX (masthead, cards, skeletons, pull-to-refresh, stated
// empty state) so Trash reads as the same product rather than a settings
// sub-page — but it is a plain FlatList: rows are grouped by when they were
// DELETED, not recorded, and "what did I just bin" is one flat recency
// question rather than a per-day archive.
import { useCallback, useMemo, useState } from "react";
import {
  ActivityIndicator, Alert, FlatList, Pressable, RefreshControl, StyleSheet,
  Text, View,
} from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, ELEV, CAPS, FONT, TABULAR, useTheme, ColorScale } from "../../lib/theme";
import { Button, EmptyState, ErrorText, IconCircle, SkeletonCard } from "../../lib/ui";
import {
  getTrash, permanentlyDeleteRecording, restoreRecording, clearToken,
  isStillUploading, TrashedRecording, ApiError,
} from "../../lib/api";
import { fmtDuration, sourceMeta, statusMeta } from "../../lib/sources";

// "Deleted 2 hours ago" — in Trash the useful stamp is how long ago the user
// binned it (and, once retention ships, how long it has left), not the wall
// clock time it happened at.
function fmtAgo(iso: string): string {
  if (!iso) return "Deleted recently";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "Deleted recently";
  const mins = Math.floor((Date.now() - t) / 60000);
  if (mins < 1) return "Deleted just now";
  if (mins < 60) return `Deleted ${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `Deleted ${hrs} hour${hrs === 1 ? "" : "s"} ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `Deleted ${days} day${days === 1 ? "" : "s"} ago`;
  return `Deleted ${new Date(t).toLocaleDateString(undefined, {
    day: "numeric", month: "long",
  })}`;
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    intro: { ...T.caption, marginTop: 2, marginBottom: S.lg },
    card: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      marginBottom: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    top: { flexDirection: "row" as const, alignItems: "flex-start" as const, gap: S.md },
    kicker: { ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.4, color: C.textFaint },
    meta: { ...TABULAR, fontSize: 11, color: C.textFaint },
    headline: { ...T.headlineSm, marginTop: 5 },
    deletedAt: { fontFamily: FONT.medium, fontSize: 12, color: C.danger, marginTop: 8 },
    // Actions sit on a ruled row beneath the content so the destructive one is
    // never adjacent to the card-level tap target.
    actions: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
      marginTop: 14, paddingTop: 12, borderTopWidth: 1, borderTopColor: C.border,
    },
    action: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      paddingVertical: 6, paddingHorizontal: 10, borderRadius: R.pill,
    },
    actionTxt: { fontFamily: FONT.bold, fontSize: 12.5 },
  });
}

export default function TrashScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);

  const [items, setItems] = useState<TrashedRecording[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  // The key currently being acted on, so one row can show a spinner without
  // freezing the rest of the list.
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError("");
    try {
      setItems(await getTrash());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        await clearToken();
        router.replace("/login");
        return;
      }
      setError(e instanceof ApiError ? e.message : "Could not load Trash.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [router]);

  // Refetch on focus, as MinuteX does: a brief trashed on MinuteX while
  // this screen is mounted must be here when the user comes back.
  useFocusEffect(useCallback(() => { load(); }, [load]));

  // Restore is optimistic — it only ever ADDS a recording back to MinuteX, so
  // the worst case of a failure is a row reappearing in Trash, which load()
  // does for us. Nothing can be lost by guessing wrong here.
  const restore = useCallback(async (item: TrashedRecording) => {
    const key = item.audio_s3_key;
    setBusyKey(key);
    setItems((prev) => prev.filter((r) => r.audio_s3_key !== key));
    try {
      await restoreRecording(key);
    } catch (e) {
      Alert.alert(
        "Couldn't restore",
        e instanceof ApiError ? e.message : "Something went wrong. Please try again."
      );
      load();
    } finally {
      setBusyKey(null);
    }
  }, [load]);

  // Permanent delete is NOT optimistic, unlike every other list mutation in
  // the app. Removing the row before the backend confirms would tell the user
  // their recording is gone forever at a moment when it might still exist —
  // and that is the one lie this screen must never tell. The row stays, with a
  // spinner, until the delete actually succeeds.
  const confirmPermanent = useCallback((item: TrashedRecording) => {
    Alert.alert(
      "Permanently delete this meeting?",
      "This will remove the recording, transcript, and AI data and cannot be undone.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete permanently",
          style: "destructive",
          onPress: async () => {
            const key = item.audio_s3_key;
            setBusyKey(key);
            try {
              await permanentlyDeleteRecording(key);
              setItems((prev) => prev.filter((r) => r.audio_s3_key !== key));
            } catch (e) {
              Alert.alert(
                "Couldn't delete",
                isStillUploading(e)
                  ? "This recording is still being processed. Try again in a minute."
                  : e instanceof ApiError
                    ? e.message
                    : "Something went wrong. Please try again."
              );
            } finally {
              setBusyKey(null);
            }
          },
        },
      ]
    );
  }, []);

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Trash" }} />

      <Text style={st.intro}>
        Deleted briefs stay here with their transcripts and AI notes. Restore
        one to put it back on MinuteX, or delete it permanently to remove it
        for good.
      </Text>

      {error ? (
        <View>
          <ErrorText>{error}</ErrorText>
          <Button
            label="Try again"
            variant="secondary"
            onPress={() => { setLoading(true); load(); }}
            style={{ marginTop: S.md, alignSelf: "flex-start", paddingHorizontal: S.xl }}
          />
        </View>
      ) : null}

      {loading ? (
        <View style={{ marginTop: S.sm }}>
          <SkeletonCard />
          <SkeletonCard />
        </View>
      ) : (
        <FlatList
          data={items}
          keyExtractor={(i) => i.audio_s3_key}
          showsVerticalScrollIndicator={false}
          contentContainerStyle={{ paddingBottom: 60 }}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => { setRefreshing(true); load(); }}
              tintColor={C.primary}
            />
          }
          ListEmptyComponent={
            // Suppressed while an error is showing: a load that FAILED tells
            // us nothing about whether Trash is empty, and rendering both
            // "Could not load Trash" and "Trash is empty" together stated a
            // fact we do not have and contradicted the retry sitting above it.
            error ? null : (
              <EmptyState
                title="Trash is empty"
                subtitle="Briefs you delete land here, and can be restored until you remove them for good."
              />
            )
          }
          renderItem={({ item }) => {
            const failed = statusMeta(item.status).kind === "failed";
            const source = sourceMeta(item);
            const duration = fmtDuration(item.duration);
            const busy = busyKey === item.audio_s3_key;
            return (
              <View style={[st.card, busy && { opacity: 0.55 }]}>
                <View style={st.top}>
                  <IconCircle
                    name={failed ? "exclamationmark.triangle.fill" : source.icon}
                    tint={failed ? C.danger : C.textFaint}
                    bg={failed ? C.dangerSoft : C.surface2}
                    size={40}
                  />
                  <View style={{ flex: 1 }}>
                    <View style={{ flexDirection: "row", alignItems: "baseline", gap: 8 }}>
                      <Text style={st.kicker}>
                        {failed ? "Couldn't finish" : source.label}
                      </Text>
                      {duration ? <Text style={st.meta}>{duration}</Text> : null}
                    </View>
                    <Text style={st.headline} numberOfLines={2}>
                      {item.title || "Untitled conversation"}
                    </Text>
                    <Text style={st.deletedAt}>{fmtAgo(item.deleted_at)}</Text>
                  </View>
                  {busy ? <ActivityIndicator color={C.primary} /> : null}
                </View>

                <View style={st.actions}>
                  <Pressable
                    onPress={() => restore(item)}
                    disabled={busy}
                    accessibilityLabel="Restore this brief"
                    style={({ pressed }) => [
                      st.action,
                      { backgroundColor: C.primarySoft },
                      pressed && { opacity: 0.6 },
                    ]}
                  >
                    <Icon name="clock.arrow.circlepath" tintColor={C.primary} size={14} />
                    <Text style={[st.actionTxt, { color: C.primary }]}>Restore</Text>
                  </Pressable>

                  {/* Danger-tinted, and the only control in the app that
                      destroys anything. Kept visually distinct from Restore
                      so the two can never be confused at a glance. */}
                  <Pressable
                    onPress={() => confirmPermanent(item)}
                    disabled={busy}
                    accessibilityLabel="Delete this brief permanently"
                    style={({ pressed }) => [
                      st.action,
                      { backgroundColor: C.dangerSoft },
                      pressed && { opacity: 0.6 },
                    ]}
                  >
                    <Icon name="trash" tintColor={C.danger} size={14} />
                    <Text style={[st.actionTxt, { color: C.danger }]}>
                      Delete permanently
                    </Text>
                  </Pressable>
                </View>
              </View>
            );
          }}
        />
      )}
    </View>
  );
}
