// src/app/contact/[id].tsx — one person: their details, their folders, their
// open work.
//
// Editing here edits them EVERYWHERE. That follows from the model — one global
// contact per person — and the screen says so, because a user who thinks they
// are editing a folder-local copy will be surprised later.
//
// Deleting a contact is the one destructive action, and it deliberately does
// NOT delete their work: tasks assigned to them survive and revert to
// unresolved (with the name kept), folder associations go, and the confirm
// dialog states exactly that. Losing a person's task list because someone
// tidied up a contact would be unforgivable.
import { useCallback, useMemo, useState } from "react";
import {
  Alert, Pressable, RefreshControl, ScrollView, StyleSheet, Text, View,
} from "react-native";
import { Stack, useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { Icon } from "../../../lib/icons";
import {
  S, R, ELEV, CAPS, FONT, useTheme, ColorScale,
} from "../../../lib/theme";
import {
  Button, Card, ErrorText, KeyboardAware, Loading, SectionTitle, TextField,
  scrollFormProps,
} from "../../../lib/ui";
import {
  ApiContact, ApiError, ApiFolder, ApiTask, deleteContact, getAllTasks,
  getContact, updateContact,
} from "../../../lib/api";
import { avatarColorFor, initialsOf } from "../../../lib/task-model";

export default function ContactDetailScreen() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id: string }>();
  const contactId = String(id || "");

  const [contact, setContact] = useState<ApiContact | null>(null);
  const [folders, setFolders] = useState<ApiFolder[]>([]);
  const [tasks, setTasks] = useState<ApiTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  const load = useCallback(
    async (isRefresh = false) => {
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        const res = await getContact(contactId);
        setContact(res.contact);
        setFolders(res.folders);
        // Their open work — server-filtered by assignee, never by pulling
        // every task down and filtering here.
        const t = await getAllTasks({
          assignee_contact_id: contactId,
          limit: 50,
        });
        setTasks(t.tasks);
      } catch (e) {
        setError(
          e instanceof ApiError && e.status === 404
            ? "This contact no longer exists."
            : e instanceof ApiError
              ? e.message
              : "Could not load this contact."
        );
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [contactId]
  );

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  const beginEdit = () => {
    if (!contact) return;
    setName(contact.name);
    setEmail(contact.email);
    setPhone(contact.phone);
    setCompany(contact.company);
    setRole(contact.role);
    setSaveError("");
    setEditing(true);
  };

  const save = useCallback(async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setSaveError("Name cannot be empty.");
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      const updated = await updateContact(contactId, {
        name: trimmed,
        email: email.trim(),
        phone: phone.trim(),
        company: company.trim(),
        role: role.trim(),
      });
      setContact(updated);
      setEditing(false);
    } catch (e) {
      // 409 here means another contact already holds this email or phone —
      // worth its own wording, since the fix is to use that contact instead.
      setSaveError(
        e instanceof ApiError && e.status === 409
          ? e.message
          : e instanceof ApiError
            ? e.message
            : "Could not save changes."
      );
    } finally {
      setSaving(false);
    }
  }, [contactId, name, email, phone, company, role]);

  const confirmDelete = useCallback(() => {
    if (!contact) return;
    const openTasks = tasks.filter((t) => t.status !== "Completed").length;
    Alert.alert(
      `Delete ${contact.name}?`,
      // Every clause here is a promise the backend actually keeps — see
      // delete_contact. Vague reassurance would be worse than none.
      openTasks > 0
        ? `They will be removed from your contacts and from every folder. Their ${openTasks} open task${
            openTasks === 1 ? "" : "s"
          } will stay, marked as needing a new assignee.`
        : "They will be removed from your contacts and from every folder. Any tasks assigned to them will stay, marked as needing a new assignee.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete Contact",
          style: "destructive",
          onPress: async () => {
            try {
              await deleteContact(contactId);
              router.back();
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
  }, [contact, contactId, tasks, router]);

  if (loading) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title: "Contact" }} />
        <Loading label="Loading contact" />
      </View>
    );
  }

  if (error || !contact) {
    return (
      <View style={st.container}>
        <Stack.Screen options={{ title: "Contact" }} />
        <View style={{ gap: S.md, marginTop: S.lg }}>
          <ErrorText>{error || "Contact not found."}</ErrorText>
          <Button label="Retry" variant="secondary" onPress={() => load()} />
          <Button label="Back" variant="ghost" onPress={() => router.back()} />
        </View>
      </View>
    );
  }

  const openTasks = tasks.filter((t) => t.status !== "Completed");

  return (
    <KeyboardAware>
    <ScrollView
      style={st.container}
      contentContainerStyle={{ paddingBottom: S.xxl * 2 }}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={() => load(true)}
          tintColor={C.primary}
        />
      }
      {...scrollFormProps}
    >
      <Stack.Screen options={{ title: contact.name || "Contact" }} />

      <View style={st.hero}>
        <View
          style={[st.avatar, { backgroundColor: avatarColorFor(contact.name) }]}
        >
          <Text style={st.avatarTxt}>{initialsOf(contact.name)}</Text>
        </View>
        <Text style={st.heroName}>{contact.name}</Text>
        {!!(contact.role || contact.company) && (
          <Text style={st.heroSub}>
            {[contact.role, contact.company].filter(Boolean).join(" · ")}
          </Text>
        )}
        {/* State the notification reality plainly rather than implying it. */}
        <View
          style={[
            st.linkPill,
            {
              backgroundColor: contact.minutex_user_id
                ? C.successSoft
                : C.surface2,
            },
          ]}
        >
          <Icon
            name={contact.minutex_user_id ? "checkmark" : "person.fill"}
            size={12}
            tintColor={contact.minutex_user_id ? C.success : C.textFaint}
          />
          <Text
            style={[
              st.linkTxt,
              { color: contact.minutex_user_id ? C.success : C.textFaint },
            ]}
          >
            {contact.minutex_user_id
              ? "Has a MinuteX account — can be notified in the app"
              : "No MinuteX account — reach them by email or phone"}
          </Text>
        </View>
      </View>

      {editing ? (
        <Card style={{ gap: S.md }}>
          <SectionTitle>Edit Contact</SectionTitle>
          <Text style={st.editNote}>
            This person is shared across every folder and meeting, so these
            changes apply everywhere.
          </Text>
          <TextField
            placeholder="Full name"
            value={name}
            onChangeText={setName}
            autoCapitalize="words"
            maxLength={120}
          />
          <TextField
            placeholder="Email"
            value={email}
            onChangeText={setEmail}
            autoCapitalize="none"
            keyboardType="email-address"
          />
          <TextField
            placeholder="Phone"
            value={phone}
            onChangeText={setPhone}
            keyboardType="phone-pad"
          />
          <TextField
            placeholder="Company"
            value={company}
            onChangeText={setCompany}
          />
          <TextField placeholder="Role" value={role} onChangeText={setRole} />
          {!!saveError && <ErrorText>{saveError}</ErrorText>}
          <Button
            label="Save Changes"
            onPress={save}
            loading={saving}
            disabled={saving}
          />
          <Button
            label="Cancel"
            variant="ghost"
            onPress={() => setEditing(false)}
          />
        </Card>
      ) : (
        <Card>
          <SectionTitle>Details</SectionTitle>
          <DetailRow label="Email" value={contact.email} C={C} st={st} />
          <DetailRow label="Phone" value={contact.phone} C={C} st={st} />
          <DetailRow label="Company" value={contact.company} C={C} st={st} />
          <DetailRow label="Role" value={contact.role} C={C} st={st} />
          {!!contact.notes && (
            <DetailRow label="Notes" value={contact.notes} C={C} st={st} />
          )}
          <View style={{ marginTop: S.md }}>
            <Button label="Edit" variant="secondary" onPress={beginEdit} />
          </View>
        </Card>
      )}

      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Folders</SectionTitle>
        {folders.length === 0 ? (
          <Text style={st.emptyLine}>
            Not in any folder. Add them from a folder to have them offered
            first when mapping that folder&apos;s speakers.
          </Text>
        ) : (
          <View style={st.chips}>
            {folders.map((f) => (
              <Pressable
                key={f.id}
                style={st.chip}
                onPress={() =>
                  router.push({
                    pathname: "/folder/[id]",
                    params: { id: f.id },
                  } as any)
                }
                accessibilityRole="button"
                accessibilityLabel={`Open folder ${f.name}`}
              >
                <Icon name="folder" size={12} tintColor={C.primary} />
                <Text style={st.chipTxt}>{f.name}</Text>
              </Pressable>
            ))}
          </View>
        )}
      </View>

      <View style={{ marginTop: S.lg }}>
        <SectionTitle>Open Tasks ({openTasks.length})</SectionTitle>
        {openTasks.length === 0 ? (
          <Text style={st.emptyLine}>No open tasks assigned to them.</Text>
        ) : (
          openTasks.map((t) => (
            <Pressable
              key={t.id}
              style={st.taskRow}
              onPress={() =>
                router.push({
                  pathname: "/task/[id]",
                  params: { id: t.id },
                } as any)
              }
              accessibilityRole="button"
              accessibilityLabel={`Open task ${t.task}`}
            >
              <View style={{ flex: 1 }}>
                <Text style={st.taskTitle} numberOfLines={2}>{t.task}</Text>
                <Text style={st.taskMeta}>
                  {t.status}
                  {t.due ? ` · due ${t.due}` : ""}
                  {t.is_overdue ? " · overdue" : ""}
                </Text>
              </View>
              <Icon name="chevron.right" size={14} tintColor={C.textFaint} />
            </Pressable>
          ))
        )}
      </View>

      <View style={{ marginTop: S.xl }}>
        <Button
          label="Delete Contact"
          variant="danger"
          onPress={confirmDelete}
        />
      </View>
    </ScrollView>
    </KeyboardAware>
  );
}

function DetailRow({
  label, value, C, st,
}: {
  label: string;
  value: string;
  C: ColorScale;
  st: ReturnType<typeof buildStyles>;
}) {
  return (
    <View style={st.detailRow}>
      <Text style={st.detailLabel}>{label}</Text>
      <Text style={[st.detailValue, !value && { color: C.textFaint }]}>
        {value || "—"}
      </Text>
    </View>
  );
}

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    hero: { alignItems: "center" as const, paddingVertical: S.lg, gap: 6 },
    avatar: {
      width: 72, height: 72, borderRadius: 36, alignItems: "center" as const,
      justifyContent: "center" as const, marginBottom: 4,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 26, color: "#fff" },
    heroName: { ...T.headline, textAlign: "center" as const },
    heroSub: { ...T.bodyDim, textAlign: "center" as const },
    linkPill: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 6,
      paddingHorizontal: 11, paddingVertical: 6, borderRadius: R.pill,
      marginTop: S.sm, maxWidth: "100%",
    },
    linkTxt: { fontFamily: FONT.medium, fontSize: 11.5, flexShrink: 1 },
    editNote: { ...T.caption },
    detailRow: {
      flexDirection: "row" as const, justifyContent: "space-between" as const,
      alignItems: "flex-start" as const, gap: S.md, paddingVertical: 9,
      borderBottomWidth: 1, borderBottomColor: C.border,
    },
    detailLabel: {
      ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 1.2,
      color: C.textFaint, paddingTop: 2,
    },
    detailValue: {
      fontFamily: FONT.medium, fontSize: 13.5, color: C.text, flexShrink: 1,
      textAlign: "right" as const,
    },
    emptyLine: { ...T.bodyDim, fontSize: 13, marginTop: 4 },
    chips: {
      flexDirection: "row" as const, flexWrap: "wrap" as const, gap: S.sm,
      marginTop: S.sm,
    },
    chip: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 5,
      paddingHorizontal: 11, paddingVertical: 7, borderRadius: R.pill,
      backgroundColor: C.primarySoft,
    },
    chipTxt: { fontFamily: FONT.bold, fontSize: 12, color: C.primary },
    taskRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      backgroundColor: C.surface, borderRadius: R.md, padding: S.md,
      marginTop: S.sm, shadowColor: C.shadow, ...ELEV.sm,
    },
    taskTitle: { fontFamily: FONT.bold, fontSize: 13.5, color: C.text },
    taskMeta: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      marginTop: 2,
    },
  });
}
