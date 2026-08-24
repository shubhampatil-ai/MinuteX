// src/app/contacts.tsx — the global contact list.
//
// One person, one row, account-wide. A contact is NOT owned by a folder: the
// same Rahul Sharma appears in Client Alpha and Product without being
// duplicated, so editing him here changes him everywhere. That is the whole
// point of the model, and it is why this screen sits at the top level rather
// than inside a folder.
//
// Search is SERVER-side and paged (see getContacts) — never "download
// everything and filter on the phone", which stops working the moment an
// account has real data in it.
//
// Adding a contact whose NAME already exists is answered by the backend with
// the candidates instead of a silent merge or a silent duplicate; the create
// sheet in lib/contact-picker.tsx renders that fork. This screen reuses that
// same sheet rather than reimplementing it, so the two paths cannot diverge.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, FlatList, Pressable, RefreshControl, StyleSheet, Text,
  View,
} from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import {
  S, R, ELEV, FONT, useTheme, ColorScale,
} from "../../lib/theme";
import {
  Button, EmptyState, ErrorText, SearchBar, SkeletonCard,
} from "../../lib/ui";
import { ApiContact, ApiError, getContacts } from "../../lib/api";
import { ContactPicker } from "../../lib/contact-picker";
import { avatarColorFor, initialsOf } from "../../lib/task-model";

const SEARCH_DEBOUNCE_MS = 300;
const PAGE_SIZE = 50;

export default function ContactsScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();

  const [contacts, setContacts] = useState<ApiContact[]>([]);
  const [cursor, setCursor] = useState("");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);

  // True until the first response lands, so the first fetch renders skeletons
  // rather than a refresh spinner over an empty screen.
  const firstLoad = useRef(true);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setDebounced(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [query]);

  const load = useCallback(
    async (search: string, isRefresh = false) => {
      // Only show the pull-to-refresh spinner for a refresh of a list that is
      // already on screen; the very first load shows skeletons instead.
      if (isRefresh && !firstLoad.current) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        const res = await getContacts({ search, limit: PAGE_SIZE });
        firstLoad.current = false;
        setContacts(res.contacts);
        setCursor(res.next_cursor);
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Could not load contacts."
        );
        setContacts([]);
        setCursor("");
      } finally {
        // Both flags clear regardless of which one was set: the single load
        // path passes isRefresh=true, so `loading` (true only from its initial
        // state) must be cleared here too or the skeletons never go away.
        setLoading(false);
        setRefreshing(false);
      }
    },
    []
  );

  // ONE load trigger, not two. useFocusEffect already fires on mount as well
  // as on every return to this screen, so pairing it with a useEffect on the
  // same data meant every cold open fetched the list twice — and the second
  // (focus) response could land first and overwrite a newer search result.
  //
  // `debounced` is a dependency on purpose: a keystroke should re-query, and
  // while the screen is focused that is exactly what this does. `isRefresh` is
  // true so a return visit updates in place rather than flashing skeletons over
  // a list the user is already looking at.
  useFocusEffect(
    useCallback(() => {
      load(debounced, true);
    }, [load, debounced])
  );

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await getContacts({
        search: debounced, limit: PAGE_SIZE, cursor,
      });
      setContacts((prev) => [...prev, ...res.contacts]);
      setCursor(res.next_cursor);
    } catch {
      // Leave the cursor untouched so scrolling again retries; a failed page
      // is not worth replacing the list the user is reading with an error.
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, debounced, loadingMore]);

  const renderContact = ({ item }: { item: ApiContact }) => {
    const sub = [item.role, item.company].filter(Boolean).join(" · ");
    return (
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
        <View style={st.row}>
          <View
            style={[st.avatar, { backgroundColor: avatarColorFor(item.name) }]}
          >
            <Text style={st.avatarTxt}>{initialsOf(item.name)}</Text>
          </View>
          <View style={{ flex: 1 }}>
            <Text style={st.name} numberOfLines={1}>{item.name}</Text>
            {!!item.email && (
              <Text style={st.sub} numberOfLines={1}>{item.email}</Text>
            )}
            {!!sub && <Text style={st.sub} numberOfLines={1}>{sub}</Text>}
          </View>
          {/* Only a contact with a linked MinuteX account can be notified in
              the app. Showing this is the difference between a user expecting
              a notification and knowing they have to message the person. */}
          {!!item.minutex_user_id && (
            <View style={st.badge}>
              <Icon name="checkmark" size={11} tintColor={C.success} />
              <Text style={st.badgeTxt}>App</Text>
            </View>
          )}
          <Icon name="chevron.right" size={15} tintColor={C.textFaint} />
        </View>
      </Pressable>
    );
  };

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Contacts" }} />
      <Text style={st.intro}>
        The people you meet with. One entry per person, shared across every
        folder and meeting.
      </Text>

      <SearchBar
        value={query}
        onChangeText={setQuery}
        placeholder="Search name, email or company"
      />

      {loading ? (
        <>
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </>
      ) : error ? (
        <View style={{ gap: S.md, marginTop: S.lg }}>
          <ErrorText>{error}</ErrorText>
          <Button
            label="Retry"
            variant="secondary"
            onPress={() => load(debounced)}
          />
        </View>
      ) : (
        <FlatList
          data={contacts}
          keyExtractor={(c) => c.id}
          renderItem={renderContact}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => load(debounced, true)}
              tintColor={C.primary}
            />
          }
          onEndReached={loadMore}
          onEndReachedThreshold={0.4}
          ListEmptyComponent={
            <EmptyState
              icon="person.fill"
              title={debounced ? "No matches" : "No contacts yet"}
              subtitle={
                debounced
                  ? "No contact matches that search."
                  : "Add the people you meet with, then map them to the speakers in your meetings so tasks land on the right person."
              }
              action={
                debounced ? undefined : (
                  <Button
                    label="Add Contact"
                    onPress={() => setCreateOpen(true)}
                  />
                )
              }
            />
          }
          ListFooterComponent={
            loadingMore ? (
              <View style={st.footer}>
                <ActivityIndicator color={C.primary} />
              </View>
            ) : contacts.length > 0 ? (
              <View style={{ marginTop: S.md, marginBottom: S.xxl }}>
                <Button
                  label="+ New Contact"
                  variant="secondary"
                  onPress={() => setCreateOpen(true)}
                />
              </View>
            ) : (
              <View style={{ height: S.xxl }} />
            )
          }
          contentContainerStyle={{ paddingTop: S.md }}
          showsVerticalScrollIndicator={false}
          keyboardShouldPersistTaps="handled"
        />
      )}

      {/* The same sheet the speaker-mapping flow uses. Opening it straight
          into create mode is not possible from outside, so it opens as a
          picker whose "+ Create New Contact" is the intended next tap; on
          pick we simply refresh, because picking here means "show me them". */}
      <ContactPicker
        visible={createOpen}
        onClose={() => setCreateOpen(false)}
        onPick={(c) =>
          router.push({
            pathname: "/contact/[id]",
            params: { id: c.id },
          } as any)
        }
        title="Add or Find Contact"
      />
    </View>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    intro: { ...T.caption, marginTop: 2, marginBottom: S.md },
    card: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.md,
      marginBottom: S.sm, shadowColor: C.shadow, ...ELEV.sm,
    },
    row: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
    },
    avatar: {
      width: 42, height: 42, borderRadius: 21, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 15, color: "#fff" },
    name: { fontFamily: FONT.bold, fontSize: 15, color: C.text },
    sub: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 1,
    },
    badge: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 3,
      paddingHorizontal: 7, paddingVertical: 3, borderRadius: R.pill,
      backgroundColor: C.successSoft,
    },
    badgeTxt: { fontFamily: FONT.bold, fontSize: 10, color: C.success },
    footer: { paddingVertical: S.lg, alignItems: "center" as const },
  });
}
