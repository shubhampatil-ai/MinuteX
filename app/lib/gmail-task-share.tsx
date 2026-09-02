// lib/gmail-task-share.tsx — email one task's details through the user's Gmail.
//
// THIS IS COMMUNICATION, NOT A NOTIFICATION. The distinction matters and is
// the reason this does not live in the existing Notify Assignee screen: that
// screen is a multi-channel deep-link handoff (WhatsApp/SMS/mailto open the
// native app pre-filled and the USER presses send there), and it is the seed
// of a notification feature that is explicitly a later task. This sends one
// email, from the user's own Gmail, because the user pressed a button now.
// Nothing here schedules, batches, retries or reacts to a task changing.
//
// THE ASSIGNEE IS THE DEFAULT RECIPIENT and the backend resolves them: the
// send route falls back to the task's assignee_contact_id when no recipients
// are supplied, so this component does not have to know how assignment is
// modelled. If that contact has no email address the backend refuses and says
// whose address is missing, rather than sending to nobody and reporting
// success.
import { useMemo, useState } from "react";
import {
  Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View,
} from "react-native";
import { useRouter } from "expo-router";
import { Icon } from "./icons";
import { R, FONT, useTheme, ColorScale } from "./theme";
import {
  Button, ErrorText, KeyboardAwareSheet, scrollFormProps,
} from "./ui";
import { ApiError, sendTaskEmail } from "./api";

export type GmailTaskSheetProps = {
  visible: boolean;
  onClose: () => void;
  taskId: string;
  taskTitle: string;
  /** Shown so the user knows who this is going to before they send. Purely
   *  informational — the backend resolves the real recipient from the task,
   *  so a stale name here cannot redirect the mail. */
  assigneeName?: string;
  onSent?: (recipientCount: number) => void;
};

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.lg, borderTopRightRadius: R.lg,
      paddingHorizontal: 20, paddingTop: 18, paddingBottom: 26, maxHeight: "85%",
    },
    head: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const,
    },
    title: { fontFamily: FONT.extrabold, fontSize: 19, color: C.text },
    to: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint, marginTop: 4,
    },
    label: {
      fontFamily: FONT.bold, fontSize: 12, color: C.textDim,
      textTransform: "uppercase" as const, letterSpacing: 0.8,
      marginTop: 20, marginBottom: 8,
    },
    input: {
      borderWidth: 1, borderColor: C.border, borderRadius: R.sm,
      backgroundColor: C.surface, paddingHorizontal: 13, paddingVertical: 11,
      fontFamily: FONT.regular, fontSize: 14, color: C.text,
    },
    bodyInput: { minHeight: 110, textAlignVertical: "top" as const },
    note: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      lineHeight: 17, marginTop: 12,
    },
  });
}

export function GmailTaskSheet({
  visible, onClose, taskId, taskTitle, assigneeName, onSent,
}: GmailTaskSheetProps) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);

  const [subject, setSubject] = useState(`Action item — ${taskTitle}`);
  // Empty by default, NOT pre-filled with the task's own fields. The backend
  // composes those (title, description, due, priority) from the stored task,
  // and only from fields that are actually set — a client-side template would
  // risk printing "Due: " with nothing after it, inviting the reader to infer
  // a deadline nobody set.
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  const onSend = async () => {
    setSending(true); setError("");
    try {
      const res = await sendTaskEmail(taskId, {
        subject: subject.trim() || undefined,
        body: body.trim() || undefined,
      });
      setSending(false);
      onSent?.(res.recipient_count);
      onClose();
    } catch (e) {
      setSending(false);
      // Covers the two cases worth distinguishing to a user: Gmail became
      // unusable, or the assignee has no email address. Both arrive as prose
      // the backend already phrased for a person.
      setError(e instanceof ApiError ? e.message : "Couldn’t send the email.");
    }
  };

  return (
    <Modal visible={visible} transparent animationType="slide"
      onRequestClose={onClose}>
      {/* Both inputs are near the bottom of the sheet — see the same note in
          lib/gmail-share.tsx and the rule in the keyboard-avoidance test. */}
      <KeyboardAwareSheet>
      <View style={st.backdrop}>
        <Pressable style={{ flex: 1 }} onPress={onClose} accessibilityLabel="Close" />
        <View style={st.sheet}>
          <View style={st.head}>
            <View style={{ flex: 1 }}>
              <Text style={st.title}>Email this task</Text>
              <Text style={st.to}>
                {assigneeName
                  ? `To ${assigneeName}, from your Gmail`
                  : "To the assignee, from your Gmail"}
              </Text>
            </View>
            <Pressable onPress={onClose} hitSlop={12} accessibilityLabel="Close">
              <Icon name="xmark" tintColor={C.textFaint} size={19} />
            </Pressable>
          </View>

          <ScrollView {...scrollFormProps} style={{ maxHeight: 340 }}>
            <Text style={st.label}>Subject</Text>
            <TextInput
              style={st.input}
              value={subject}
              onChangeText={setSubject}
              placeholder="Subject"
              placeholderTextColor={C.textFaint}
            />

            <Text style={st.label}>Message</Text>
            <TextInput
              style={[st.input, st.bodyInput]}
              value={body}
              onChangeText={setBody}
              multiline
              placeholder="Leave blank to send the task details."
              placeholderTextColor={C.textFaint}
            />
            <Text style={st.note}>
              Left blank, MinuteX sends the task title and whichever of the
              due date, priority and description are set.
            </Text>
          </ScrollView>

          {error ? <ErrorText>{error}</ErrorText> : null}
          <Button
            label="Send"
            onPress={onSend}
            loading={sending}
            disabled={!subject.trim()}
            style={{ marginTop: 14 }}
          />
        </View>
      </View>
      </KeyboardAwareSheet>
    </Modal>
  );
}

/**
 * The Gmail-dependent button on a task screen.
 *
 * Same visibility rule as GmailShareRow, in button form: when Gmail is not
 * usable this is not an action, it is a prompt that routes to the place that
 * fixes it. `hideWhenUnavailable` lets a cramped screen omit it entirely
 * instead — both readings satisfy the rule, and the surface chooses.
 */
export function GmailTaskButton({
  usable, needsReauth, onPress, hideWhenUnavailable, style,
}: {
  usable: boolean;
  needsReauth: boolean;
  onPress: () => void;
  hideWhenUnavailable?: boolean;
  style?: any;
}) {
  const router = useRouter();

  if (!usable && hideWhenUnavailable) return null;

  if (!usable) {
    return (
      <Button
        label={needsReauth ? "Reconnect Gmail to email this task"
          : "Connect Gmail to email this task"}
        variant="secondary"
        onPress={() => router.push("/integrations/gmail")}
        style={style}
      />
    );
  }

  return (
    <Button label="Email this task" variant="secondary" onPress={onPress}
      style={style} />
  );
}
