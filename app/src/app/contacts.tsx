// src/app/contacts.tsx — the global contact list.
//
// One person, one row, account-wide. A contact is account-wide: the
// same Rahul Sharma appears in Client Alpha and Product without being
// duplicated, so editing him here changes him everywhere. That is the whole
// point of the model, and it is why this screen sits at the top level rather
// than per meeting.
//
// Search is SERVER-side and paged (see getContacts) — never "download
// everything and filter on the phone", which stops working the moment an
// account has real data in it.
//
// Adding a contact whose NAME already exists is answered by the backend with
// the candidates instead of a silent merge or a silent duplicate; the create
// sheet in lib/contact-picker.tsx renders that fork. This screen reuses that
// same sheet rather than reimplementing it, so the two paths cannot diverge.
//
// SALESFORCE. When the active workspace has synced Salesforce people (the
// Site People Store), a "Salesforce" segment lists them: Contacts (people you
// meet and tag) and Users (colleagues Tasks can be assigned to), with counts
// from the store itself. It reloads the moment a People Sync finishes
// anywhere (site-salesforce-events), so a sync never leaves this list stale.
// Opening one resolves it to its MinuteX contact (found or created once).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, FlatList, Pressable, RefreshControl, StyleSheet, Text,
  View,
} from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import {
  S, R, ELEV, FONT, useTheme, ColorScale,
} from "../../lib/theme";
import {
  Avatar, Button, Chip, EmptyState, ErrorText, SearchBar, SkeletonCard,
} from "../../lib/ui";
import {
  ApiContact, ApiError, SitePeopleCounts, SitePerson, contactForSitePerson, getContacts,
  searchSitePeoplePage,
} from "../../lib/api";
import { roleLabel, useWorkspace } from "../../lib/workspace-context";
import { ContactPicker } from "../../lib/contact-picker";
import { useSiteSalesforceVersion } from "../../lib/site-salesforce-ui";
import { peopleTabLabel, salesforceBadge } from "../../lib/site-salesforce-flow";

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

  // ---- Salesforce people (the Site People Store) --------------------------
  const params = useLocalSearchParams<{ tab?: string }>();
  const { activeId } = useWorkspace();
  const [segment, setSegment] = useState<"minutex" | "salesforce">(
    params.tab === "salesforce" ? "salesforce" : "minutex");
  const [sfType, setSfType] = useState<"contact" | "user">("contact");
  const [sfPeople, setSfPeople] = useState<SitePerson[] | null>(null);
  const [sfCounts, setSfCounts] = useState<SitePeopleCounts | null>(null);
  const [sfTruncated, setSfTruncated] = useState(false);
  const [sfError, setSfError] = useState("");
  const [opening, setOpening] = useState("");
  const peopleVersion = useSiteSalesforceVersion("people", "connection");
  const hasSalesforce = !!sfCounts && sfCounts.contacts + sfCounts.users > 0;

  const loadSalesforce = useCallback(async () => {
    if (!activeId) return;
    try {
      const page = await searchSitePeoplePage(activeId, { q: debounced, type: sfType, limit: 100 });
      setSfPeople(page.people);
      setSfCounts(page.counts);
      setSfTruncated(page.truncated);
      setSfError("");
    } catch (e) {
      // A workspace without Salesforce simply has no segment.
      setSfPeople([]);
      setSfCounts(null);
      setSfError(e instanceof ApiError && e.status !== 404 && e.status !== 403 ? e.message : "");
    }
    // `peopleVersion`: a People Sync finishing anywhere reloads this list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, debounced, sfType, peopleVersion]);

  useFocusEffect(useCallback(() => { void loadSalesforce(); }, [loadSalesforce]));

  const openSalesforcePerson = useCallback(async (p: SitePerson) => {
    if (!activeId || opening) return;
    setOpening(p.person_key);
    try {
      const c = await contactForSitePerson(activeId, p.person_key);
      router.push({ pathname: "/contact/[id]", params: { id: c.id } } as any);
    } catch (e) {
      setSfError(e instanceof ApiError ? e.message : "Could not open that person.");
    } finally {
      setOpening("");
    }
  }, [activeId, opening, router]);

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
          {/* Whatever photo this person has — one the user saved, or their own
              MinuteX profile photo when they are on MinuteX. Initials when
              there is none. The API has already decided which; see
              avatar_source on ApiContact. */}
          <Avatar name={item.name} photoUri={item.avatar_view_url} size={42} />
          <View style={{ flex: 1 }}>
            <View style={st.nameRow}>
              <Text style={st.name} numberOfLines={1}>{item.name}</Text>
              {!!salesforceBadge(item) && (
                <View style={st.sfTag}>
                  <Text style={st.sfTagTxt}>{salesforceBadge(item).replace("Salesforce ", "SF ")}</Text>
                </View>
              )}
              {/* Their LIVE role in this organisation, resolved server-side
                  from membership on every read. Empty for an ordinary
                  contact, so this renders nothing in a personal workspace. */}
              {!!item.workspace_role && (
                <View style={st.roleTag}>
                  <Text style={st.roleTagTxt}>
                    {roleLabel(item.workspace_role)}
                  </Text>
                </View>
              )}
            </View>
            {!!item.email && (
              <Text style={st.sub} numberOfLines={1}>{item.email}</Text>
            )}
            {!!sub && <Text style={st.sub} numberOfLines={1}>{sub}</Text>}
          </View>
          {/* Only a contact with a linked MinuteX account can be notified in
              the app. Showing this is the difference between a user expecting
              a notification and knowing they have to message the person.
              Dropped for a colleague, who is an account by definition and
              already carries the role tag. */}
          {!!item.minutex_user_id && !item.workspace_role && (
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

  const renderSalesforcePerson = ({ item }: { item: SitePerson }) => {
    const sub = [item.title, item.company].filter(Boolean).join(" · ");
    return (
      <Pressable
        style={st.card}
        onPress={() => { void openSalesforcePerson(item); }}
        disabled={!!opening}
        accessibilityRole="button"
        accessibilityLabel={`Open ${item.name}`}
      >
        <View style={st.row}>
          <Avatar name={item.name} size={42} />
          <View style={{ flex: 1 }}>
            <View style={st.nameRow}>
              <Text style={st.name} numberOfLines={1}>{item.name}</Text>
              <View style={st.sfTag}>
                <Text style={st.sfTagTxt}>{item.type === "user" ? "SF User" : "SF Contact"}</Text>
              </View>
            </View>
            {!!item.email && <Text style={st.sub} numberOfLines={1}>{item.email}</Text>}
            {!!sub && <Text style={st.sub} numberOfLines={1}>{sub}</Text>}
          </View>
          {opening === item.person_key
            ? <ActivityIndicator color={C.primary} />
            : <Icon name="chevron.right" size={15} tintColor={C.textFaint} />}
        </View>
      </Pressable>
    );
  };

  const showSalesforce = hasSalesforce && segment === "salesforce";

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Contacts" }} />
      <Text style={st.intro}>
        {showSalesforce
          ? "People synced from Salesforce. Contacts are people you meet; Users can be assigned Salesforce Tasks."
          : "The people you meet with. One entry per person, shared across every meeting."}
      </Text>

      {hasSalesforce && (
        <View style={st.segments}>
          <Chip label="MinuteX" active={segment === "minutex"} onPress={() => setSegment("minutex")} />
          <Chip label="Salesforce" active={segment === "salesforce"} onPress={() => setSegment("salesforce")} />
        </View>
      )}

      <SearchBar
        value={query}
        onChangeText={setQuery}
        placeholder={showSalesforce ? "Search Salesforce people" : "Search name, email or company"}
      />

      {showSalesforce ? (
        <>
          <View style={st.segments}>
            <Chip label={peopleTabLabel("contact", sfCounts)} active={sfType === "contact"}
              onPress={() => { setSfType("contact"); setSfPeople(null); }} />
            <Chip label={peopleTabLabel("user", sfCounts)} active={sfType === "user"}
              onPress={() => { setSfType("user"); setSfPeople(null); }} />
          </View>
          {!!sfError && <ErrorText>{sfError}</ErrorText>}
          {sfPeople === null ? (
            <>
              <SkeletonCard />
              <SkeletonCard />
            </>
          ) : (
            <FlatList
              data={sfPeople}
              keyExtractor={(p) => p.person_key}
              renderItem={renderSalesforcePerson}
              refreshControl={
                <RefreshControl refreshing={false} onRefresh={() => { void loadSalesforce(); }}
                  tintColor={C.primary} />
              }
              ListEmptyComponent={
                <EmptyState
                  icon="person.fill"
                  title={debounced ? "No matches" : sfType === "user" ? "No Salesforce Users" : "No Salesforce Contacts"}
                  subtitle={debounced
                    ? "Nobody synced from Salesforce matches that search."
                    : "Run Sync Users & Contacts on the Salesforce (Site) screen."}
                />
              }
              ListFooterComponent={
                sfTruncated || sfPeople.length >= 100
                  ? <Text style={st.footNote}>Showing the first matches. Search to narrow the list.</Text>
                  : <View style={{ height: S.xxl }} />
              }
              contentContainerStyle={{ paddingTop: S.sm }}
              showsVerticalScrollIndicator={false}
              keyboardShouldPersistTaps="handled"
            />
          )}
        </>
      ) : loading ? (
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
    // flexShrink is what makes the numberOfLines={1} below actually bind.
    // On its own, numberOfLines does NOT constrain width in React Native —
    // the Text still measures to its full single-line width and pushes its
    // siblings (role tag, App badge, Salesforce badge, chevron — four of
    // them on this row) straight off the card.
    name: { fontFamily: FONT.bold, fontSize: 15, color: C.text, flexShrink: 1 },
    sub: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 1,
    },
    nameRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
    },
    // On the NAME line rather than the right-hand badge slot: the tag says
    // who this person is, while that slot means "notification-ready".
    // flexShrink: 0 — a role is a short known word and truncating "MANAGER"
    // to "MAN…" would be worse than letting the name give way instead.
    roleTag: {
      paddingHorizontal: 6, paddingVertical: 2, borderRadius: R.sm,
      backgroundColor: C.accentSoft, flexShrink: 0,
    },
    roleTagTxt: {
      fontFamily: FONT.bold, fontSize: 10, color: C.accent,
      letterSpacing: 0.2,
    },
    badge: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 3,
      paddingHorizontal: 7, paddingVertical: 3, borderRadius: R.pill,
      backgroundColor: C.successSoft,
      // Fixed one-word badges: they hold their size and the name yields.
      flexShrink: 0,
    },
    badgeTxt: { fontFamily: FONT.bold, fontSize: 10, color: C.success },
    footer: { paddingVertical: S.lg, alignItems: "center" as const },
    segments: {
      flexDirection: "row" as const, flexWrap: "wrap" as const, gap: S.sm,
      marginBottom: S.sm,
    },
    sfTag: {
      paddingHorizontal: 6, paddingVertical: 2, borderRadius: R.sm,
      backgroundColor: C.primarySoft, flexShrink: 0,
    },
    sfTagTxt: { fontFamily: FONT.bold, fontSize: 10, color: C.primary, letterSpacing: 0.2 },
    footNote: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textFaint,
      textAlign: "center" as const, marginVertical: S.lg,
    },
  });
}
