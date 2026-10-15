// src/app/recording/[key]/task/[taskId]/assign.tsx — Assign To.
//
// Assigns a task to a REAL contact, so the task genuinely belongs to a person
// the system can identify and (when they have a MinuteX account) notify.
//
// WHAT CHANGED AND WHY. This screen used to offer four sources: two lists of
// hardcoded SAMPLE contacts ("Team Members", "Recent Contacts" — invented names
// from lib/contacts.ts, kept because no contact backend existed yet), the device
// address book, and a typed email/phone. Assignment stored a NAME. That is now
// wrong in three ways:
//
//   * a contact backend exists, so the sample entries showed the user people
//     who are not their contacts while hiding the ones who are;
//   * a name-only assignee is stored UNRESOLVED — it cannot be notified, cannot
//     be filtered by in the task tracker, and has to be resolved again by hand;
//   * the same "who is this person" problem is already solved once, in
//     lib/contact-picker.tsx, with the ordering the product wants.
//
// So this screen now delegates to that picker and assigns a contact_id. The
// picker ranks: people tagged in THIS meeting,
// then every contact — plus create-new and import-from-phone, which together
// replace the manual-entry and phone-contacts paths this file used to own.
//
// ASSIGNING ENDS HERE. This screen used to redirect into Notify Assignee on
// success, which conflated two independent decisions: who owns the task, and
// whether to message them about it. Notifying is genuinely optional — most
// reassignments are bookkeeping — so picking someone now returns to the task,
// where "Notify Assignee" remains one tap away for the times it is wanted.
import { useCallback, useEffect, useMemo, useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { FONT, R, S, useTheme, ColorScale } from "../../../../../../lib/theme";
import { Button, ErrorText, Loading } from "../../../../../../lib/ui";
import { ContactPicker } from "../../../../../../lib/contact-picker";
import { useMeeting } from "../../../../../../lib/meeting-context";
import { avatarColorFor, initialsOf } from "../../../../../../lib/task-model";
import {
  ApiContact, ApiError, getParticipants,
} from "../../../../../../lib/api";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    intro: {
      fontFamily: FONT.regular, fontSize: 13, color: C.textFaint,
      marginTop: S.md, marginBottom: S.lg, lineHeight: 19,
    },
    taskCard: {
      backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      borderWidth: 1, borderColor: C.border, marginBottom: S.lg,
    },
    taskLabel: {
      fontFamily: FONT.semibold, fontSize: 10.5, letterSpacing: 1.2,
      textTransform: "uppercase" as const, color: C.textFaint,
    },
    taskText: {
      fontFamily: FONT.bold, fontSize: 15, color: C.text, marginTop: 5,
      lineHeight: 21,
    },
    current: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      marginTop: S.md, paddingTop: S.md, borderTopWidth: 1,
      borderTopColor: C.border,
    },
    avatar: {
      width: 38, height: 38, borderRadius: 19,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.bold, fontSize: 14, color: "#fff" },
    name: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    sub: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint,
      marginTop: 2,
    },
  });
}

export default function AssignScreen() {
  const { key, taskId } = useLocalSearchParams<{ key: string; taskId: string }>();
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  // Assign through the CONTEXT, not the API directly. The meeting's task list
  // is fetched once per meeting, so a write that bypasses it leaves the old
  // assignee on screen when this flow pops back — which is exactly what made
  // reassignment look like it did nothing.
  const { getTask, assignTaskToContact } = useMeeting();
  const task = getTask(taskId);

  // Opens immediately: this screen exists to pick someone, so making the user
  // tap again to see the list would be a step for nothing.
  const [pickerOpen, setPickerOpen] = useState(true);
  const [meetingContacts, setMeetingContacts] = useState<ApiContact[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // The ranking context. One call gives us both the meeting's tagged people and
  // Failure only degrades ORDERING — the picker still
  // lists every contact — so it is not surfaced as an error.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const p = await getParticipants(String(key));
        if (!alive) return;
        setMeetingContacts(
          p.participants
            .map((x) => x.contact)
            .filter((c): c is ApiContact => !!c)
        );
      } catch {
        if (alive) setMeetingContacts([]);
      }
    })();
    return () => { alive = false; };
  }, [key]);

  const assign = useCallback(
    async (contact: ApiContact) => {
      setBusy(true);
      setError("");
      try {
        // A real contact_id, not a name: this is what makes the task RESOLVED,
        // filterable by assignee, and notification-ready when the contact has a
        // MinuteX account.
        await assignTaskToContact(String(taskId), contact);
        // Assigning is DONE at this point. Notifying is a separate, optional
        // decision, so this goes back to the task — where "Notify Assignee"
        // still sits for the user who wants it. Forcing the notify screen here
        // made every reassignment feel like an unfinished, half-committed
        // action and gave no way out but the back button.
        router.back();
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "Could not assign that task."
        );
        setPickerOpen(false);
      } finally {
        setBusy(false);
      }
    },
    [taskId, router, assignTaskToContact, setPickerOpen]
  );

  if (!task) return null;

  const assignee = task.assignee;

  return (
    <View style={st.container}>
      <Stack.Screen options={{ title: "Assign to" }} />

      <View style={st.taskCard}>
        <Text style={st.taskLabel}>Task</Text>
        <Text style={st.taskText}>{task.task}</Text>
        {!!assignee && (
          <View style={st.current}>
            <View
              style={[
                st.avatar,
                { backgroundColor: avatarColorFor(assignee.name) },
              ]}
            >
              <Text style={st.avatarTxt}>{initialsOf(assignee.name)}</Text>
            </View>
            <View style={{ flex: 1 }}>
              <Text style={st.name}>{assignee.name}</Text>
              <Text style={st.sub}>Currently assigned</Text>
            </View>
          </View>
        )}
      </View>

      {busy ? (
        <Loading label="Assigning" />
      ) : (
        <>
          <Text style={st.intro}>
            People tagged in this meeting come first, then everyone. You can
            also add someone new.
          </Text>
          {!!error && <ErrorText>{error}</ErrorText>}
          <Button
            label={assignee ? "Choose someone else" : "Choose a Contact"}
            onPress={() => setPickerOpen(true)}
          />
        </>
      )}

      <ContactPicker
        visible={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onPick={assign}
        meetingContacts={meetingContacts}
        title="Assign Task To"
      />
    </View>
  );
}
