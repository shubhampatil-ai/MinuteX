// lib/contact-picker.tsx — the "choose a person" sheet, shared by speaker
// mapping and task assignment.
//
// Ordering is the whole design. People already tagged in THIS meeting come
// FIRST under their own heading, because a name spoken in a meeting usually
// belongs to someone in it. Every other contact follows under "All Contacts",
// and search covers both — the meeting tier is a shortcut, never a
// restriction. Creating a new contact is always available at the bottom.
//
// Search is SERVER-side (getContacts({search})), debounced. Filtering a
// downloaded list on-device would break the moment an account has more
// contacts than one page.
//
// Ambiguity is surfaced, not resolved: creating a contact whose NAME already
// exists comes back as a 409 with candidates, and this sheet then asks the
// user to pick one or confirm it is a different person. It never silently
// merges two people, and never silently creates a duplicate.
//
// SALESFORCE. When the workspace has synced Salesforce people (the Site
// People Store), a "From Salesforce" tier offers them too — Contacts (people
// you meet) and Users (colleagues). Picking one resolves it server-side to
// its MinuteX contact (found or created once, linked to the Salesforce
// record) and then goes through onPick like any other contact, so every
// caller — speaker tagging, attendance, task assignment — works unchanged.
// Tagging a Salesforce Contact this way is what relates the meeting to it.
// People already shown as a linked contact are not listed twice.
//
// REQUIRE EMAIL (opt-in). A caller that will EMAIL the person it gets back —
// the Gmail share sheet — passes `requireEmail`, and then every route out of
// this sheet ends at a contact with an address: no-email rows are shown but
// disabled, the create form refuses a blank email, a phone contact without
// one is routed to the create form to add it, and an existing match that has
// no address gets the typed one filled in (only ever into an EMPTY field).
// Every other caller leaves it off, so email stays optional for them.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "expo-router";
import {
  ActivityIndicator, FlatList, Modal, Pressable, StyleSheet, Text, View,
} from "react-native";
import { Icon } from "./icons";
import { S, R, CAPS, FONT, useTheme, ColorScale } from "./theme";
import {
  Avatar, Button, EmptyState, ErrorText, KeyboardAwareSheet, SearchBar,
  TextField,
} from "./ui";
import {
  ApiContact, ApiError, SitePerson, ambiguousCandidates, contactForSitePerson, createContact,
  getContacts, getMe, getRelatedPeople, isAmbiguousContact, searchLivePeople, searchSitePeoplePage,
  updateContact,
} from "./api";
import { useWorkspace } from "./workspace-context";
import { SITE_SALESFORCE_ENABLED, useSiteSalesforceVersion } from "./site-salesforce-ui";
import { WEBHOOK_V2_ENABLED } from "./webhook-ui";
import {
  mergePeople, salesforceBadge, salesforceTypeTag, shouldSearchLive,
} from "./site-salesforce-flow";
import { invalidateSiteSalesforce } from "./site-salesforce-events";
import {
  ContactPickResult, requestContactsPermissionDetailed, searchPhoneContacts,
} from "./contacts";
import { roleLabel } from "./workspace-context";
import { uploadPhoneContactPhoto } from "./avatars";

const SEARCH_DEBOUNCE_MS = 300;
const PAGE_SIZE = 50;

// Shape check only — the backend validates properly (contact_create answers
// 400 "email is not a valid address"). This just catches the obvious typo
// before a round trip.
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const hasEmail = (email?: string) => !!(email || "").trim();

type Section =
  | { kind: "heading"; label: string; hint?: string }
  | { kind: "contact"; contact: ApiContact }
  | { kind: "person"; person: SitePerson };

// One row per PERSON, keeping first appearance. Callers pass speaker→contact
// mappings, where the same person legitimately appears more than once (one
// contact tagged as several speakers), and every list here keys on contact.id.
function dedupById(contacts: ApiContact[]): ApiContact[] {
  const seen = new Set<string>();
  return contacts.filter((c) => {
    if (seen.has(c.id)) return false;
    seen.add(c.id);
    return true;
  });
}

export type ContactPickerProps = {
  visible: boolean;
  onClose: () => void;
  onPick: (contact: ApiContact) => void;
  /**
   * People already tagged in THIS meeting — ranked first, because a
   * task from a meeting almost always belongs to someone who was in it.
   * Optional: screens with no meeting context simply omit it.
   */
  meetingContacts?: ApiContact[];
  title?: string;
  /** Shown as a "remove" affordance when the caller already has a selection. */
  onClear?: () => void;
  /**
   * Offer a "That's me" shortcut that picks the SIGNED-IN user.
   *
   * Opt-in, because it only makes sense where the answer could genuinely be
   * the user themselves — tagging a speaker in your own meeting, mostly.
   * Without it a user had to hand-type a contact for themselves and get their
   * own email exactly right, or the account link (and every notification that
   * depends on it) silently never happened.
   */
  allowSelf?: boolean;
  /**
   * The workspace whose synced Salesforce people to offer (a meeting's own
   * workspace). Defaults to the active workspace; a workspace without a
   * Salesforce People Store simply shows no Salesforce tier.
   */
  salesforceWorkspaceId?: string;
  /**
   * Only hand back contacts that HAVE an email address — for callers that
   * will mail the person (see the file header). Off by default.
   */
  requireEmail?: boolean;
  /**
   * The meeting being tagged. When it's linked to a Salesforce record, the
   * people on that record (an Account's contacts, the Lead itself...) are
   * suggested before anyone types.
   */
  meetingKey?: string;
};

export function ContactPicker({
  visible, onClose, onPick, meetingContacts = [],
  title = "Select Contact", onClear,
  allowSelf = false, salesforceWorkspaceId, requireEmail = false, meetingKey,
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

  // Synced Salesforce people matching the same search.
  const { activeId } = useWorkspace();
  const sfWorkspace = salesforceWorkspaceId || activeId;
  const [sfPeople, setSfPeople] = useState<SitePerson[]>([]);
  // Keys of people that came from LIVE search (not yet saved in MinuteX).
  const [livePeopleKeys, setLivePeopleKeys] = useState<Set<string>>(new Set());
  // People on the meeting's linked Salesforce record(s), fetched once per open.
  const [relatedPeople, setRelatedPeople] = useState<SitePerson[]>([]);
  const [resolving, setResolving] = useState("");
  const peopleVersion = useSiteSalesforceVersion("people", "connection");

  // Create-new state.
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [saving, setSaving] = useState(false);
  const [createError, setCreateError] = useState("");
  // Populated when the backend answers "which one did you mean?".
  const [candidates, setCandidates] = useState<ApiContact[]>([]);

  // THE SIGNED-IN USER, for the "That's me" shortcut. Loaded only when the
  // caller asked for it and only while the sheet is open — most pickers never
  // offer it, and it must not cost a request on every mount.
  const [me, setMe] = useState<{ name: string; email: string } | null>(null);
  const [selfBusy, setSelfBusy] = useState(false);
  useEffect(() => {
    if (!allowSelf || !visible || me) return;
    let alive = true;
    getMe()
      .then((u) => {
        // An account with no email cannot be linked to a contact (see
        // _resolve_minutex_user server-side), so the shortcut would produce an
        // UNRESOLVED contact that can never be notified. Better to not offer
        // it than to offer a broken version of it.
        if (alive && u.email) setMe({ name: u.name || "Me", email: u.email });
      })
      .catch(() => { /* the shortcut simply does not appear */ });
    return () => { alive = false; };
  }, [allowSelf, visible, me]);

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

  // `onClose` is an inline arrow at every call site, so it is a NEW function
  // on every parent render. Depending on it directly made `load` unstable,
  // which re-fired the load effect below, which setState'd, which re-rendered
  // the parent — an unbounded render loop that fired at mount, because this
  // sheet is always mounted and only `visible` toggles. Reading it through a
  // ref keeps the latest callback without making it a render-loop input.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

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
          onCloseRef.current();
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
    [router]
  );

  useEffect(() => {
    if (visible) load(debounced);
  }, [visible, debounced, load]);

  // Salesforce tier. Browsing (no search term): the people MinuteX already
  // knows (synced, or tagged before), from the local People Store. Searching:
  // a LIVE search of the customer's org as well, so anyone in their
  // Salesforce can be found however many contacts they have. A receiver
  // without live search yet, or any failure, leaves just the stored matches.
  // A workspace without Salesforce simply has no tier.
  useEffect(() => {
    if (!visible || !(SITE_SALESFORCE_ENABLED || WEBHOOK_V2_ENABLED) || !sfWorkspace) {
      setSfPeople([]); setLivePeopleKeys(new Set()); return;
    }
    let alive = true;
    const stored = searchSitePeoplePage(sfWorkspace, { q: debounced, limit: debounced ? 20 : 15 })
      .then((r) => r.people).catch(() => [] as SitePerson[]);
    const live = WEBHOOK_V2_ENABLED && shouldSearchLive(debounced)
      ? searchLivePeople(sfWorkspace, debounced, { limit: 20 }).catch(() => [] as SitePerson[])
      : Promise.resolve([] as SitePerson[]);
    void Promise.all([live, stored]).then(([l, s]) => {
      if (!alive) return;
      const storedKeys = new Set(s.map((p) => p.person_key));
      setLivePeopleKeys(new Set(l.filter((p) => !storedKeys.has(p.person_key)).map((p) => p.person_key)));
      setSfPeople(mergePeople(l, s));
    });
    return () => { alive = false; };
  }, [visible, debounced, sfWorkspace, peopleVersion]);

  // Suggestions from the meeting's linked record. Any failure (not linked, an
  // older receiver, Salesforce down) just means no suggestions.
  useEffect(() => {
    if (!visible || !WEBHOOK_V2_ENABLED || !meetingKey) { setRelatedPeople([]); return; }
    let alive = true;
    getRelatedPeople(meetingKey)
      .then((p) => { if (alive) setRelatedPeople(p); })
      .catch(() => { if (alive) setRelatedPeople([]); });
    return () => { alive = false; };
  }, [visible, meetingKey]);

  /** A Salesforce person -> their MinuteX contact -> the ordinary onPick. */
  const pickSalesforcePerson = useCallback(async (p: SitePerson) => {
    if (!sfWorkspace || resolving) return;
    setResolving(p.person_key);
    setCreateError("");
    try {
      // Live and related people may not be saved in MinuteX yet: send the
      // person so the pick can save them.
      const fromSalesforceNow = livePeopleKeys.has(p.person_key)
        || relatedPeople.some((r) => r.person_key === p.person_key);
      const contact = await contactForSitePerson(
        sfWorkspace, p.person_key, fromSalesforceNow ? p : undefined);
      onPick(contact);
      onClose();
      // A tagged Salesforce Contact is what relates a meeting to Salesforce.
      invalidateSiteSalesforce("relation");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not use that Salesforce person.");
    } finally {
      setResolving("");
    }
  }, [sfWorkspace, resolving, onPick, onClose, livePeopleKeys, relatedPeople]);

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
    const needle = debounced.toLowerCase();
    const matches = (c: ApiContact) =>
      !needle ||
      [c.name, c.email, c.company, c.role].some((f) =>
        (f || "").toLowerCase().includes(needle)
      );

    // Two tiers, narrowest context first: this meeting, then everyone. Each
    // contact appears exactly ONCE, in the most specific tier it qualifies
    // for — a duplicate would make the list look longer while offering no
    // new choice.
    const out: Section[] = [];
    const seen = new Set<string>();

    // Dedup WITHIN this tier too, not just against later ones. The meeting's
    // participants are speaker→contact MAPPINGS, so one person tagged as two
    // speakers (Speaker 0 and Speaker 2 are both Priya — routine in a diarized
    // meeting) arrives here twice. Both rows would key on contact.id and React
    // would warn about duplicate keys and may drop one of them.
    const meetingMatches = dedupById(meetingContacts.filter(matches));
    if (meetingMatches.length) {
      out.push({
        kind: "heading", label: "In this meeting", hint: "Tagged here",
      });
      meetingMatches.forEach((c) => {
        seen.add(c.id);
        out.push({ kind: "contact", contact: c });
      });
    }

    // `all` is paginated, so a contact can arrive twice if the underlying set
    // shifts between page fetches.
    const rest = dedupById(all.filter((c) => !seen.has(c.id)));

    // A THIRD TIER, between "in this meeting" and everyone: the colleagues
    // from the active organisation. They are the people a member assigns
    // work to most often, and separating them makes a shared book of clients
    // and teammates scannable instead of one long alphabet.
    //
    // Identified by workspace_role, which the server sets only for a live
    // organisation member — so in a personal workspace this tier is simply
    // empty and the list looks exactly as it did before.
    const colleagues = rest.filter((c) => !!c.workspace_role);
    const others = rest.filter((c) => !c.workspace_role);

    // Salesforce people not already on screen as their linked contact.
    const shownIds = new Set([...seen, ...rest.map((c) => c.id)]);
    // Browsing: the people on the meeting's linked record come right after
    // this meeting's own people. Searching: live search covers them.
    const relatedKeys = new Set(relatedPeople.map((p) => p.person_key));
    const relatedTier: Section[] = needle ? [] : relatedPeople
      .map((p) => ({ kind: "person" as const, person: p }));
    if (relatedTier.length) {
      out.push({ kind: "heading", label: "On this meeting's Salesforce record",
                 hint: "Suggested from what the meeting is linked to" }, ...relatedTier);
    }
    const sfTier: Section[] = sfPeople
      .filter((p) => !p.linked_contact_id || !shownIds.has(p.linked_contact_id))
      .filter((p) => needle || !relatedKeys.has(p.person_key))
      .map((p) => ({ kind: "person" as const, person: p }));
    const sfHeading: Section = {
      kind: "heading", label: "From Salesforce",
      hint: needle ? "Contacts, Leads & Users in your Salesforce" : "People you've used before",
    };
    // Searching: Salesforce matches right after this meeting's people, so a
    // typed name finds its Salesforce Contact without scrolling. Browsing:
    // they come last, after the address book.
    if (needle && sfTier.length) out.push(sfHeading, ...sfTier);
    if (colleagues.length) {
      out.push({
        kind: "heading", label: "Your organisation",
        hint: "Synced from members",
      });
      colleagues.forEach((c) => out.push({ kind: "contact", contact: c }));
    }
    if (others.length) {
      out.push({
        kind: "heading",
        // Only worth distinguishing from the tier above when there IS one.
        label: colleagues.length ? "Other Contacts" : "All Contacts",
      });
      others.forEach((c) => out.push({ kind: "contact", contact: c }));
    }
    if (!needle && sfTier.length) out.push(sfHeading, ...sfTier);
    return out;
  }, [all, meetingContacts, debounced, sfPeople, relatedPeople]);

  /** In requireEmail mode, make sure the contact handed back HAS an address.
   *
   * createContact can resolve to an EXISTING person matched by phone, and the
   * backend then returns that row as stored — the email the user just typed
   * is dropped (contact_create only fills a gap for the avatar). For a caller
   * about to email them, that would be a contact the send refuses. So the
   * typed address fills the gap here, and ONLY a gap: an existing address is
   * the person's real one and is never overwritten. A failed update (a member
   * contact, an org row this user cannot edit) throws into the caller's catch,
   * so the pick does not happen and the reason is shown. */
  const ensureEmail = useCallback(
    async (contact: ApiContact, typedEmail: string): Promise<ApiContact> => {
      if (!requireEmail || hasEmail(contact.email) || !typedEmail) return contact;
      return updateContact(contact.id, { email: typedEmail });
    },
    [requireEmail]
  );

  const submitNew = useCallback(
    async (force: boolean) => {
      const name = newName.trim();
      if (!name) {
        setCreateError("Enter a name.");
        return;
      }
      const email = newEmail.trim();
      if (requireEmail && !email) {
        setCreateError("Enter an email address — it’s where the documents will be sent.");
        return;
      }
      if (email && !EMAIL_SHAPE.test(email)) {
        setCreateError("Enter a valid email address.");
        return;
      }
      setSaving(true);
      setCreateError("");
      try {
        const { contact: created, existing } = await createContact({
          name,
          email: email || undefined,
          phone: newPhone.trim() || undefined,
          force: force || undefined,
        });
        const contact = await ensureEmail(created, email);
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
    [newName, newEmail, newPhone, onPick, onClose, requireEmail, ensureEmail]
  );

  /** Pick the signed-in user, creating their self-contact once if needed.
   *
   * A contact is still the unit of assignment everywhere in MinuteX, so "me"
   * has to BE one — there is no separate self-participant concept, and
   * inventing one would mean every screen that reads a contact learning about
   * a second shape. Instead this reuses the ordinary create path.
   *
   * Reuse rather than duplicate: createContact answers 200 {existing:true}
   * when a strong identifier (the email) already matches, so tapping this
   * twice, or after having typed yourself in by hand once, converges on the
   * SAME contact rather than growing a pile of self-rows.
   *
   * The email is what makes it work — the backend links a contact to an
   * account by looking it up in the Users table (_resolve_minutex_user), so
   * taking it from the authenticated profile means the link cannot be broken
   * by a typo the way hand-entry could.
   */
  const pickSelf = useCallback(async () => {
    if (!me) return;
    setSelfBusy(true);
    setCreateError("");
    try {
      const { contact } = await createContact({
        name: me.name,
        email: me.email,
        // Same-name collisions must not stop the user identifying THEMSELVES:
        // the email is an exact identifier, so an "is this a different
        // person?" prompt would be noise here.
        force: true,
      });
      onPick(contact);
      onClose();
    } catch (e) {
      setCreateError(
        e instanceof ApiError ? e.message : "Could not add you as a contact."
      );
    } finally {
      setSelfBusy(false);
    }
  }, [me, onPick, onClose]);

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
   * which any address-book data leaves the device.
   *
   * NOT force:true. Forcing here was creating the duplicates the rest of this
   * sheet works to prevent: importing "Rahul Patil" when a MinuteX contact of
   * that name already existed made a SECOND record, and that twin then showed
   * up twice everywhere a contact is chosen — speaker mapping, task assignment,
   * the contacts list. An email or phone match still resolves silently to the
   * existing person (the backend answers 200 existing:true), because those are
   * strong identifiers. A NAME-only collision comes back 409, and the right
   * answer is the same one the create form gives: show the candidates and let
   * the user say "that's them" or "different person". */
  const importFromPhone = useCallback(
    async (pick: ContactPickResult) => {
      // No address in the phone book, and the caller needs one: hand over to
      // the create form, pre-filled, so the user only has to add the email.
      // Nothing has left the device yet at this point.
      if (requireEmail && !hasEmail(pick.email)) {
        setNewName(pick.name);
        setNewEmail("");
        setNewPhone(pick.phone || "");
        setCandidates([]);
        setCreateError("Add an email address to send to this person.");
        setPhoneMode(false);
        setCreating(true);
        return;
      }
      setPhoneBusy(true);
      setCreateError("");
      try {
        // The address-book photo, if this person has one. Uploaded BEFORE the
        // create so the contact is written already carrying it, in one call —
        // and best-effort inside uploadPhoneContactPhoto, because the user
        // asked to import a PERSON: a thumbnail that will not upload must not
        // fail the import, it just leaves them on initials.
        //
        // The device URI itself is never stored. It is local to this phone and
        // the OS can revoke it, so it would be a dangling path everywhere else.
        const avatarKey = await uploadPhoneContactPhoto(pick.photoUri);
        const { contact: created } = await createContact({
          name: pick.name,
          email: pick.email || undefined,
          phone: pick.phone || undefined,
          avatar_url: avatarKey || undefined,
        });
        const contact = await ensureEmail(created, (pick.email || "").trim());
        onPick(contact);
        onClose();
      } catch (e) {
        if (isAmbiguousContact(e)) {
          // Hand the decision to the create form, pre-filled from the device
          // row so "Create as a different person" carries the phone contact's
          // details rather than an empty form the user must retype.
          setNewName(pick.name);
          setNewEmail(pick.email || "");
          setNewPhone(pick.phone || "");
          setCandidates(ambiguousCandidates(e));
          setCreateError(
            "You already have a contact with this name. Pick them, or confirm this is a different person."
          );
          setPhoneMode(false);
          setCreating(true);
          return;
        }
        setCreateError(
          e instanceof ApiError ? e.message : "Could not import that contact."
        );
      } finally {
        setPhoneBusy(false);
      }
    },
    [onPick, onClose, requireEmail, ensureEmail]
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
    if (item.kind === "person") {
      const p = item.person;
      const busy = resolving === p.person_key;
      const noEmail = requireEmail && !hasEmail(p.email);
      return (
        <Pressable
          style={[st.row, noEmail && st.rowDisabled]}
          onPress={() => { void pickSalesforcePerson(p); }}
          disabled={!!resolving || noEmail}
          accessibilityRole="button"
          accessibilityState={{ disabled: !!resolving || noEmail }}
          accessibilityLabel={`Select ${p.name}, Salesforce ${p.type === "user" ? "User" : p.type === "lead" ? "Lead" : "Contact"}${noEmail ? ", no email address" : ""}`}
        >
          <Avatar name={p.name} size={36} fontSize={13} />
          <View style={{ flex: 1 }}>
            <View style={st.nameRow}>
              <Text style={st.name} numberOfLines={1}>{p.name}</Text>
              <View style={st.roleTag}>
                <Text style={st.roleTagTxt}>{salesforceTypeTag(p.type)}</Text>
              </View>
            </View>
            {noEmail ? (
              <Text style={st.sub} numberOfLines={1}>No email address</Text>
            ) : !!(p.relation || p.email || p.company) && (
              <Text style={st.sub} numberOfLines={1}>
                {[p.relation, p.email, p.relation ? "" : p.company].filter(Boolean).join(" · ")}
              </Text>
            )}
          </View>
          {busy ? <ActivityIndicator color={C.accent} /> : null}
        </Pressable>
      );
    }
    const c = item.contact;
    // Shown, not hidden: the user should see the person exists and WHY they
    // cannot be chosen here, rather than wonder where they went.
    const noEmail = requireEmail && !hasEmail(c.email);
    const sub = noEmail ? "No email address" : c.email || c.phone || c.company || "";
    return (
      <Pressable
        style={[st.row, noEmail && st.rowDisabled]}
        onPress={() => {
          onPick(c);
          onClose();
        }}
        disabled={noEmail}
        accessibilityRole="button"
        accessibilityState={{ disabled: noEmail }}
        accessibilityLabel={`Select ${c.name}${noEmail ? ", no email address" : ""}`}
      >
        <Avatar name={c.name} photoUri={c.avatar_view_url} size={36}
          fontSize={13} />
        <View style={{ flex: 1 }}>
          <View style={st.nameRow}>
            <Text style={st.name} numberOfLines={1}>{c.name}</Text>
            {/* THE ROLE TAG. Present only on an organisation colleague, and
                resolved server-side from LIVE membership on every read — so
                it cannot show a role somebody no longer holds. */}
            {!!c.workspace_role && (
              <View style={st.roleTag}>
                <Text style={st.roleTagTxt}>
                  {roleLabel(c.workspace_role)}
                </Text>
              </View>
            )}
            {!c.workspace_role && !!salesforceBadge(c) && (
              <View style={st.roleTag}>
                <Text style={st.roleTagTxt}>
                  {salesforceTypeTag(c.people_type)}
                </Text>
              </View>
            )}
          </View>
          {!!sub && <Text style={st.sub} numberOfLines={1}>{sub}</Text>}
        </View>
        {/* A contact with a MinuteX account can be notified in-app; one
            without cannot, and pretending otherwise would be a lie.
            Redundant for a colleague — they are an account by definition and
            already carry the role tag — so it is dropped there rather than
            putting two badges on one row. */}
        {!!c.minutex_user_id && !c.workspace_role && (
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
                        data={phoneRows}
                        keyExtractor={(c) => c.id}
                        keyboardShouldPersistTaps="handled"
                        renderItem={({ item }) => (
                          <Pressable
                            style={st.row}
                            onPress={() => importFromPhone(item)}
                            disabled={phoneBusy}
                            accessibilityRole="button"
                            accessibilityLabel={`Import ${item.name}`}
                          >
                            {/* The DEVICE photo, straight from the address
                                book — a local URI, shown before any upload so
                                the user recognises who they are importing. */}
                            <Avatar
                              name={item.name}
                              photoUri={item.photoUri}
                              size={36}
                              fontSize={13}
                            />
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
                {/* When the email is the field that matters and the name is
                    already filled (a phone import, or a typed search), focus
                    goes straight to the email. */}
                <TextField
                  placeholder="Full name"
                  value={newName}
                  onChangeText={setNewName}
                  autoFocus={!(requireEmail && newName)}
                  autoCapitalize="words"
                />
                <TextField
                  placeholder={requireEmail ? "Email" : "Email (optional)"}
                  value={newEmail}
                  onChangeText={setNewEmail}
                  autoFocus={requireEmail && !!newName}
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
                  {requireEmail
                    ? "An email is required — it’s the address the documents will be sent to."
                    : "An email lets MinuteX link this person to their account, so "
                      + "tasks assigned to them can reach them in the app."}
                </Text>
                {!!createError && <ErrorText>{createError}</ErrorText>}

                {/* The ambiguity fork: pick an existing person, or say this is
                    genuinely someone else. Never decided for the user. */}
                {candidates.map((c) => {
                  const noEmail = requireEmail && !hasEmail(c.email);
                  return (
                  <Pressable
                    key={c.id}
                    style={[st.candidate, noEmail && st.rowDisabled]}
                    onPress={() => {
                      onPick(c);
                      onClose();
                    }}
                    disabled={noEmail}
                    accessibilityState={{ disabled: noEmail }}
                  >
                    <Avatar name={c.name} photoUri={c.avatar_view_url}
                      size={36} fontSize={13} />
                    {/* Matches the two sibling rows in this file, which
                        already truncate — this one was the odd one out. */}
                    <View style={{ flex: 1, minWidth: 0 }}>
                      <Text style={st.name} numberOfLines={1}>{c.name}</Text>
                      {noEmail ? (
                        <Text style={st.sub} numberOfLines={1}>No email address</Text>
                      ) : !!(c.email || c.company) && (
                        <Text style={st.sub} numberOfLines={1}>
                          {c.email || c.company}
                        </Text>
                      )}
                    </View>
                    {!noEmail && (
                      <Text style={[st.useTxt, { flexShrink: 0 }]}>Use this</Text>
                    )}
                  </Pressable>
                  );
                })}

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
                      item.kind === "heading" ? `h-${item.label}-${i}`
                        : item.kind === "person" ? `sf-${item.person.person_key}`
                          : item.contact.id
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
                  {/* THAT'S ME. First, because when the answer is the user
                      themselves it is the answer — and because without it the
                      only route was typing your own name and email into
                      "Create New Contact" and getting the address exactly
                      right, since that email is what links the contact to
                      your account. Shown only when the caller opted in AND
                      the account has an email to link with. */}
                  {allowSelf && !!me && (
                    <Button
                      label={`That’s me (${me.name})`}
                      variant="secondary"
                      onPress={pickSelf}
                      loading={selfBusy}
                    />
                  )}
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
    rowDisabled: { opacity: 0.45 },
    // flexShrink so a long name yields to the role tag / badges beside it
    // rather than pushing them off the sheet (numberOfLines alone does not
    // constrain width in RN). Mirrors src/app/contacts.tsx, which renders
    // the same row.
    name: { fontFamily: FONT.bold, fontSize: 14.5, color: C.text, flexShrink: 1 },
    sub: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 1 },
    nameRow: { flexDirection: "row", alignItems: "center", gap: 6 },
    // The role tag sits ON the name line, not in the badge slot on the right:
    // it qualifies WHO this person is, and the right-hand slot already means
    // "notification-ready". Colour-coded by seniority so a long shared book
    // can be scanned without reading every tag.
    roleTag: {
      paddingHorizontal: 6, paddingVertical: 2, borderRadius: R.sm,
      backgroundColor: C.accentSoft, flexShrink: 0,
    },
    roleTagTxt: {
      fontFamily: FONT.bold, fontSize: 10, color: C.accent,
      letterSpacing: 0.2,
    },
    badge: {
      flexDirection: "row", alignItems: "center", gap: 3,
      paddingHorizontal: 7, paddingVertical: 3, borderRadius: R.pill,
      backgroundColor: C.successSoft, flexShrink: 0,
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
