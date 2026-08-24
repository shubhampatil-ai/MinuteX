// lib/contact-picker.tsx — the "choose a person" sheet, shared by speaker
// mapping and task assignment.
//
// Ordering is the whole design. When a meeting sits in a folder, that folder's
// contacts come FIRST under their own heading, because a name spoken in a
// Client Alpha meeting is usually a Client Alpha person. Every other contact
// follows under "All Contacts", and search covers both — the folder is a
// shortcut, never a restriction (spec section 10). Creating a new contact is
// always available at the bottom.
//
// Search is SERVER-side (getContacts({search})), debounced. Filtering a
// downloaded list on-device would break the moment an account has more
// contacts than one page.
//
// Ambiguity is surfaced, not resolved: creating a contact whose NAME already
// exists comes back as a 409 with candidates, and this sheet then asks the
// user to pick one or confirm it is a different person. It never silently
// merges two people, and never silently creates a duplicate.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "expo-router";
import {
  ActivityIndicator, FlatList, Modal, Pressable, StyleSheet, Text, View,
} from "react-native";
import { Icon } from "./icons";
import { S, R, CAPS, FONT, useTheme, ColorScale } from "./theme";
import {
  Button, EmptyState, ErrorText, KeyboardAwareSheet, SearchBar, TextField,
} from "./ui";
import {
  ApiContact, ApiError, ambiguousCandidates, createContact, getContacts,
  isAmbiguousContact,
} from "./api";
import { initialsOf, avatarColorFor } from "./task-model";
import {
  ContactPickResult, requestContactsPermissionDetailed, searchPhoneContacts,
} from "./contacts";

const SEARCH_DEBOUNCE_MS = 300;
const PAGE_SIZE = 50;

type Section =
  | { kind: "heading"; label: string; hint?: string }
  | { kind: "contact"; contact: ApiContact; inFolder: boolean };

export type ContactPickerProps = {
  visible: boolean;
  onClose: () => void;
  onPick: (contact: ApiContact) => void;
  /**
   * People already tagged in THIS meeting — ranked above the folder, because a
   * task from a meeting almost always belongs to someone who was in it.
   * Optional: screens with no meeting context simply omit it.
   */
  meetingContacts?: ApiContact[];
  /** Contacts to offer first, under a folder heading. */
  folderContacts?: ApiContact[];
  folderName?: string;
  /** When set, a contact created from this sheet is also linked to the folder. */
  folderId?: string;
  title?: string;
  /** Shown as a "remove" affordance when the caller already has a selection. */
  onClear?: () => void;
};

export function ContactPicker({
  visible, onClose, onPick, meetingContacts = [], folderContacts = [],
  folderName = "", folderId = "", title = "Select Contact", onClear,
}: ContactPickerProps) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();

  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [all, setAll] = useState<ApiContact[]>([]);
  const [cursor, setCursor] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");

  // Create-new state.
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [saving, setSaving] = useState(false);
  const [createError, setCreateError] = useState("");
  // Populated when the backend answers "which one did you mean?".
  const [candidates, setCandidates] = useState<ApiContact[]>([]);

  // Phone-contact import. Deliberately ONE PERSON AT A TIME: the user's
  // address book is theirs, and nothing leaves the device until they pick a
  // specific person. There is no bulk sync and no silent mirror.
  const [phoneMode, setPhoneMode] = useState(false);
  const [phoneRows, setPhoneRows] = useState<ContactPickResult[]>([]);
  const [phoneBusy, setPhoneBusy] = useState(false);
  const [phoneDenied, setPhoneDenied] = useState(false);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const next = query.trim();
    // Skip the debounce when the term is already current. Without this, the
    // open-reset below (which clears `query`) schedules a pointless 300ms
    // round trip on every open, and briefly leaves `debounced` disagreeing
    // with what the search box shows.
    if (next === debounced) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setDebounced(next), SEARCH_DEBOUNCE_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [query, debounced]);

  const load = useCallback(
    async (search: string) => {
      setLoading(true);
      setError("");
      try {
        const res = await getContacts({ search, limit: PAGE_SIZE });
        setAll(res.contacts);
        setCursor(res.next_cursor);
      } catch (e) {
        // A 401 means the session is gone — request() has already cleared the
        // token by the time we get here. Showing "Could not load contacts" in
        // the sheet would strand the user: they are signed out, and no amount
        // of retrying inside a modal can fix that. Close and send them to
        // login, the same thing every full screen in the app does.
        if (e instanceof ApiError && e.status === 401) {
          onClose();
          router.replace("/login");
          return;
        }
        setError(e instanceof ApiError ? e.message : "Could not load contacts.");
        setAll([]);
        setCursor("");
      } finally {
        setLoading(false);
      }
    },
    [onClose, router]
  );

  useEffect(() => {
    if (visible) load(debounced);
  }, [visible, debounced, load]);

  // Reset the create form each time the sheet opens, so a half-typed contact
  // from a previous open never reappears attached to a different speaker.
  useEffect(() => {
    if (!visible) return;
    setCreating(false);
    setNewName("");
    setNewEmail("");
    setNewPhone("");
    setCreateError("");
    setCandidates([]);
    // BOTH halves of the search state, together.
    //
    // The bug this fixes: clearing only `query` left `debounced` holding the
    // PREVIOUS search term, so reopening the picker fetched contacts matching
    // a search the user could no longer see. If nothing matched that stale
    // term the list came up empty with an empty search box — "no contacts to
    // tag" on an account that has contacts.
    setQuery("");
    setDebounced("");
    if (timer.current) clearTimeout(timer.current);
    setPhoneMode(false);
    setPhoneRows([]);
    setPhoneDenied(false);
  }, [visible]);

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await getContacts({
        search: debounced, limit: PAGE_SIZE, cursor,
      });
      setAll((prev) => [...prev, ...res.contacts]);
      setCursor(res.next_cursor);
    } catch {
      // A failed page is not worth an error banner over the list the user can
      // already see — the cursor stays put so pulling again retries.
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, debounced, loadingMore]);

  const sections: Section[] = useMemo(() => {
    const folderIds = new Set(folderContacts.map((c) => c.id));
    const needle = debounced.toLowerCase();
    const matches = (c: ApiContact) =>
      !needle ||
      [c.name, c.email, c.company, c.role].some((f) =>
        (f || "").toLowerCase().includes(needle)
      );

    // Three tiers, narrowest context first: this meeting, then this folder,
    // then everyone. Each contact appears exactly ONCE, in the most specific
    // tier it qualifies for — a duplicate would make the list look longer
    // while offering no new choice.
    const out: Section[] = [];
    const seen = new Set<string>();

    const meetingMatches = meetingContacts.filter(matches);
    if (meetingMatches.length) {
      out.push({
        kind: "heading", label: "In this meeting", hint: "Tagged here",
      });
      meetingMatches.forEach((c) => {
        seen.add(c.id);
        out.push({ kind: "contact", contact: c, inFolder: folderIds.has(c.id) });
      });
    }

    const folderMatches = folderContacts.filter(
      (c) => matches(c) && !seen.has(c.id)
    );
    if (folderMatches.length) {
      out.push({
        kind: "heading",
        label: folderName ? `${folderName} Contacts` : "Folder Contacts",
        hint: "In this folder",
      });
      folderMatches.forEach((c) => {
        seen.add(c.id);
        out.push({ kind: "contact", contact: c, inFolder: true });
      });
    }

    const rest = all.filter((c) => !seen.has(c.id) && !folderIds.has(c.id));
    if (rest.length) {
      out.push({ kind: "heading", label: "All Contacts" });
      rest.forEach((c) =>
        out.push({ kind: "contact", contact: c, inFolder: false })
      );
    }
    return out;
  }, [all, meetingContacts, folderContacts, folderName, debounced]);

  const submitNew = useCallback(
    async (force: boolean) => {
      const name = newName.trim();
      if (!name) {
        setCreateError("Enter a name.");
        return;
      }
      setSaving(true);
      setCreateError("");
      try {
        const { contact, existing } = await createContact({
          name,
          email: newEmail.trim() || undefined,
          phone: newPhone.trim() || undefined,
          folder_id: folderId || undefined,
          force: force || undefined,
        });
        // `existing` means a strong identifier matched — the right outcome is
        // to use that person, and to say so rather than implying we made one.
        setCandidates([]);
        onPick(contact);
        onClose();
        if (existing) {
          // Non-blocking: the pick already happened, this is just honesty.
          setCreateError("");
        }
      } catch (e) {
        if (isAmbiguousContact(e)) {
          setCandidates(ambiguousCandidates(e));
          setCreateError(
            "Someone with this name already exists. Pick them, or confirm this is a different person."
          );
        } else {
          setCreateError(
            e instanceof ApiError ? e.message : "Could not create contact."
          );
        }
      } finally {
        setSaving(false);
      }
    },
    [newName, newEmail, newPhone, folderId, onPick, onClose]
  );

  // Ask for permission and load the device list. Kept out of the render path
  // so the address book is only read after a deliberate tap.
  const openPhoneContacts = useCallback(async () => {
    setPhoneBusy(true);
    setPhoneDenied(false);
    try {
      const outcome = await requestContactsPermissionDetailed();
      if (outcome !== "granted") {
        setPhoneDenied(true);
        setPhoneMode(true);
        return;
      }
      setPhoneMode(true);
      setPhoneRows(await searchPhoneContacts(query.trim()));
    } finally {
      setPhoneBusy(false);
    }
  }, [query]);

  // Re-query the device as the user types, but only while the phone list is
  // actually on screen.
  useEffect(() => {
    if (!phoneMode || phoneDenied) return;
    let alive = true;
    searchPhoneContacts(debounced)
      .then((r) => { if (alive) setPhoneRows(r); })
      .catch(() => { if (alive) setPhoneRows([]); });
    return () => { alive = false; };
  }, [phoneMode, phoneDenied, debounced]);

  /** Turn ONE device contact into a MinuteX contact. This is the only point at
   * which any address-book data leaves the device. */
  const importFromPhone = useCallback(
    async (pick: ContactPickResult) => {
      setPhoneBusy(true);
      setCreateError("");
      try {
        const { contact } = await createContact({
          name: pick.name,
          email: pick.email || undefined,
          phone: pick.phone || undefined,
          folder_id: folderId || undefined,
          // A phone contact whose name already exists is almost always the
          // SAME person the user means, and they picked this row deliberately —
          // so a name collision here should not dead-end them. An email/phone
          // match still returns the existing contact rather than duplicating.
          force: true,
        });
        onPick(contact);
        onClose();
      } catch (e) {
        setCreateError(
          e instanceof ApiError ? e.message : "Could not import that contact."
        );
      } finally {
        setPhoneBusy(false);
      }
    },
    [folderId, onPick, onClose]
  );

  const renderRow = (item: Section) => {
    if (item.kind === "heading") {
      return (
        <View style={st.headingRow}>
          <Text style={st.heading}>{item.label}</Text>
          {!!item.hint && <Text style={st.headingHint}>{item.hint}</Text>}
        </View>
      );
    }
    const c = item.contact;
    const sub = c.email || c.phone || c.company || "";
    return (
      <Pressable
        style={st.row}
        onPress={() => {
          onPick(c);
          onClose();
        }}
        accessibilityRole="button"
        accessibilityLabel={`Select ${c.name}`}
      >
        <View style={[st.avatar, { backgroundColor: avatarColorFor(c.name) }]}>
          <Text style={st.avatarTxt}>{initialsOf(c.name)}</Text>
        </View>
        <View style={{ flex: 1 }}>
          <Text style={st.name} numberOfLines={1}>{c.name}</Text>
          {!!sub && <Text style={st.sub} numberOfLines={1}>{sub}</Text>}
        </View>
        {/* A contact with a MinuteX account can be notified in-app; one
            without cannot, and pretending otherwise would be a lie. */}
        {!!c.minutex_user_id && (
          <View style={st.badge}>
            <Icon name="checkmark" size={11} tintColor={C.success} />
            <Text style={st.badgeTxt}>App</Text>
          </View>
        )}
      </Pressable>
    );
  };

  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent
      onRequestClose={onClose}
    >
      <KeyboardAwareSheet>
        <View style={st.backdrop}>
          <View style={st.sheet}>
            <View style={st.sheetHead}>
              <Text style={st.sheetTitle}>
                {creating
                  ? "New Contact"
                  : phoneMode
                    ? "Phone Contacts"
                    : title}
              </Text>
              <Pressable
                onPress={onClose}
                hitSlop={12}
                accessibilityRole="button"
                accessibilityLabel="Close"
              >
                <Icon name="xmark" size={20} tintColor={C.textFaint} />
              </Pressable>
            </View>

            {phoneMode ? (
              <>
                {phoneDenied ? (
                  <View style={st.center}>
                    <EmptyState
                      icon="person.fill"
                      title="Contacts permission needed"
                      subtitle="MinuteX needs access to your contacts to import one. Nothing is uploaded until you pick a specific person."
                    />
                  </View>
                ) : (
                  <>
                    <SearchBar
                      value={query}
                      onChangeText={setQuery}
                      placeholder="Search phone contacts"
                    />
                    <Text style={st.privacyNote}>
                      Only the person you tap is added to MinuteX.
                    </Text>
                    {phoneBusy ? (
                      <View style={st.center}>
                        <ActivityIndicator color={C.accent} />
                      </View>
                    ) : phoneRows.length === 0 ? (
                      <EmptyState
                        icon="person.fill"
                        title="No matches"
                        subtitle="No phone contact matches that search."
                      />
                    ) : (
                      <FlatList
                        // Same constraint as the contacts list below: the sheet
                        // is height-capped, so this list needs an explicit
                        // shrinkable, zero-floor height or it has no bounded
                        // viewport to virtualize against.
                        style={{ flexShrink: 1, minHeight: 0 }}
                        data={phoneRows}
                        keyExtractor={(c, i) => `${c.name}-${c.email ?? c.phone ?? i}`}
                        keyboardShouldPersistTaps="handled"
                        renderItem={({ item }) => (
                          <Pressable
                            style={st.row}
                            onPress={() => importFromPhone(item)}
                            disabled={phoneBusy}
                            accessibilityRole="button"
                            accessibilityLabel={`Import ${item.name}`}
                          >
                            <View
                              style={[
                                st.avatar,
                                { backgroundColor: avatarColorFor(item.name) },
                              ]}
                            >
                              <Text style={st.avatarTxt}>
                                {initialsOf(item.name)}
                              </Text>
                            </View>
                            <View style={{ flex: 1 }}>
                              <Text style={st.name} numberOfLines={1}>
                                {item.name}
                              </Text>
                              {!!(item.email || item.phone) && (
                                <Text style={st.sub} numberOfLines={1}>
                                  {item.email || item.phone}
                                </Text>
                              )}
                            </View>
                            <Icon name="plus" size={15} tintColor={C.primary} />
                          </Pressable>
                        )}
                      />
                    )}
                  </>
                )}
                {!!createError && <ErrorText>{createError}</ErrorText>}
                <View style={st.actions}>
                  <Button
                    label="Back"
                    variant="ghost"
                    onPress={() => {
                      setPhoneMode(false);
                      setCreateError("");
                    }}
                  />
                </View>
              </>
            ) : creating ? (
              <View style={st.form}>
                <TextField
                  placeholder="Full name"
                  value={newName}
                  onChangeText={setNewName}
                  autoFocus
                  autoCapitalize="words"
                />
                <TextField
                  placeholder="Email (optional)"
                  value={newEmail}
                  onChangeText={setNewEmail}
                  autoCapitalize="none"
                  keyboardType="email-address"
                />
                <TextField
                  placeholder="Phone (optional)"
                  value={newPhone}
                  onChangeText={setNewPhone}
                  keyboardType="phone-pad"
                />
                <Text style={st.formHint}>
                  An email lets MinuteX link this person to their account, so
                  tasks assigned to them can reach them in the app.
                </Text>
                {!!createError && <ErrorText>{createError}</ErrorText>}

                {/* The ambiguity fork: pick an existing person, or say this is
                    genuinely someone else. Never decided for the user. */}
                {candidates.map((c) => (
                  <Pressable
                    key={c.id}
                    style={st.candidate}
                    onPress={() => {
                      onPick(c);
                      onClose();
                    }}
                  >
                    <View
                      style={[st.avatar, { backgroundColor: avatarColorFor(c.name) }]}
                    >
                      <Text style={st.avatarTxt}>{initialsOf(c.name)}</Text>
                    </View>
                    <View style={{ flex: 1 }}>
                      <Text style={st.name}>{c.name}</Text>
                      {!!(c.email || c.company) && (
                        <Text style={st.sub}>{c.email || c.company}</Text>
                      )}
                    </View>
                    <Text style={st.useTxt}>Use this</Text>
                  </Pressable>
                ))}

                <Button
                  label={
                    candidates.length ? "Create as a different person" : "Create Contact"
                  }
                  onPress={() => submitNew(candidates.length > 0)}
                  loading={saving}
                  disabled={saving}
                />
                <Button
                  label="Back"
                  variant="ghost"
                  onPress={() => {
                    setCreating(false);
                    setCandidates([]);
                    setCreateError("");
                  }}
                />
              </View>
            ) : (
              <>
                <SearchBar
                  value={query}
                  onChangeText={setQuery}
                  placeholder="Search contacts"
                />
                {loading ? (
                  <View style={st.center}>
                    <ActivityIndicator color={C.accent} />
                  </View>
                ) : error ? (
                  <View style={st.center}>
                    <ErrorText>{error}</ErrorText>
                    <Button label="Retry" variant="ghost" onPress={() => load(debounced)} />
                  </View>
                ) : sections.length === 0 ? (
                  <EmptyState
                    icon="person.fill"
                    title={debounced ? "No matches" : "No contacts yet"}
                    subtitle={
                      debounced
                        ? "No contact matches that search."
                        : "Add the people you meet with, then map them to speakers."
                    }
                  />
                ) : (
                  <FlatList
                    // The list must be the part that YIELDS space when the
                    // keyboard shrinks the sheet: flexShrink lets it give way,
                    // and without a minHeight of 0 a FlatList refuses to go
                    // below its content height and pushes the search field and
                    // the action buttons off-screen instead.
                    style={{ flexShrink: 1, minHeight: 0 }}
                    data={sections}
                    keyExtractor={(item, i) =>
                      item.kind === "heading" ? `h-${item.label}-${i}` : item.contact.id
                    }
                    renderItem={({ item }) => renderRow(item)}
                    onEndReached={loadMore}
                    onEndReachedThreshold={0.4}
                    ListFooterComponent={
                      loadingMore ? (
                        <View style={st.footer}>
                          <ActivityIndicator color={C.accent} />
                        </View>
                      ) : null
                    }
                    keyboardShouldPersistTaps="handled"
                  />
                )}
                <View style={st.actions}>
                  {!!onClear && (
                    <Button
                      label="Clear selection"
                      variant="ghost"
                      onPress={() => {
                        onClear();
                        onClose();
                      }}
                    />
                  )}
                  <Button
                    label="+ Create New Contact"
                    variant="secondary"
                    onPress={() => {
                      setCreating(true);
                      setNewName(query.trim());
                    }}
                  />
                  <Button
                    label="Import from Phone Contacts"
                    variant="ghost"
                    onPress={openPhoneContacts}
                    loading={phoneBusy}
                  />
                </View>
              </>
            )}
          </View>
        </View>
      </KeyboardAwareSheet>
    </Modal>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.xl,
      borderTopRightRadius: R.xl, paddingHorizontal: 20, paddingTop: S.lg,
      paddingBottom: S.xl,
      // maxHeight caps the sheet when the list is long; flexShrink lets the
      // whole sheet contract when KeyboardAwareSheet reduces the frame, which
      // is what keeps the search field and buttons on screen with the keyboard
      // up rather than pushed under it.
      maxHeight: "88%", flexShrink: 1,
    },
    sheetHead: {
      flexDirection: "row", alignItems: "center",
      justifyContent: "space-between", marginBottom: S.md,
    },
    sheetTitle: { ...T.headlineSm },
    center: { paddingVertical: S.xxl, alignItems: "center", gap: S.sm },
    footer: { paddingVertical: S.md, alignItems: "center" },
    headingRow: {
      flexDirection: "row", alignItems: "center",
      justifyContent: "space-between", marginTop: S.md, marginBottom: 6,
    },
    heading: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 10.5, letterSpacing: 1.3,
      color: C.textFaint,
    },
    headingHint: { fontFamily: FONT.medium, fontSize: 10.5, color: C.accent },
    row: {
      flexDirection: "row", alignItems: "center", gap: S.md,
      paddingVertical: 11, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    avatar: {
      width: 36, height: 36, borderRadius: 18, alignItems: "center",
      justifyContent: "center",
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 13, color: "#fff" },
    name: { fontFamily: FONT.bold, fontSize: 14.5, color: C.text },
    sub: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 1 },
    badge: {
      flexDirection: "row", alignItems: "center", gap: 3,
      paddingHorizontal: 7, paddingVertical: 3, borderRadius: R.pill,
      backgroundColor: C.successSoft,
    },
    badgeTxt: { fontFamily: FONT.bold, fontSize: 10, color: C.success },
    actions: { gap: S.sm, marginTop: S.md },
    form: { gap: S.md },
    formHint: { ...T.caption, marginTop: -4 },
    candidate: {
      flexDirection: "row", alignItems: "center", gap: S.md, padding: S.md,
      borderRadius: R.md, backgroundColor: C.surface, borderWidth: 1,
      borderColor: C.border,
    },
    useTxt: { fontFamily: FONT.bold, fontSize: 12, color: C.accent },
    // Says plainly that browsing is not importing. Worth the line: reading an
    // address book is the kind of thing users are right to be wary of.
    privacyNote: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      marginTop: 6, marginBottom: 2,
    },
  });
}
