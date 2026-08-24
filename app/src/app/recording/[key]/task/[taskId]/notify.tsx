// src/app/recording/[key]/task/[taskId]/notify.tsx — Notify Assignee.
//
// Multi-select channels (WhatsApp / Email / SMS / MinuteX App), an optional
// custom message, then "Send Notification" opens a per-channel preview for
// confirmation before anything actually happens. WhatsApp/Email/SMS are real
// deep-link handoffs (see lib/contacts.ts) — each opens the native app
// pre-filled and the user taps Send there; "MinuteX App" has no transport at
// all (no push backend exists) and is recorded as a local acknowledgement
// only, never claimed as delivered.
import { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View } from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { ELEV, FONT, R, S, useTheme, ColorScale } from "../../../../../../lib/theme";
import { Button, KeyboardAware } from "../../../../../../lib/ui";
import { Icon } from "../../../../../../lib/icons";
import { useMeeting } from "../../../../../../lib/meeting-context";
import { initialsOf, type NotifyChannel, type Task } from "../../../../../../lib/task-model";
import { buildNotificationText, sendEmail, sendSms, sendWhatsApp } from "../../../../../../lib/contacts";

const CHANNELS: { key: NotifyChannel; label: string; icon: string; color: string }[] = [
  { key: "whatsapp", label: "WhatsApp", icon: "message.fill", color: "#1FA972" },
  { key: "email", label: "Email", icon: "envelope", color: "#3E6BFF" },
  { key: "sms", label: "SMS", icon: "text.bubble", color: "#7C5CFF" },
  { key: "app", label: "MinuteX App", icon: "sparkles", color: "#F5A623" },
];

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg },
    body: { paddingHorizontal: 20, paddingTop: S.lg, paddingBottom: 60 },
    assigneeCard: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      backgroundColor: C.surface, borderWidth: 1, borderColor: C.border, borderRadius: R.card,
      padding: S.lg, shadowColor: C.shadow, ...ELEV.sm,
    },
    avatar: { width: 46, height: 46, borderRadius: 23, alignItems: "center" as const, justifyContent: "center" as const },
    name: { fontFamily: FONT.bold, fontSize: 15.5, color: C.text },
    contact: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint, marginTop: 2 },
    label: {
      fontFamily: FONT.semibold, fontSize: 11, letterSpacing: 0.6, textTransform: "uppercase" as const,
      color: C.textFaint, marginTop: S.xl, marginBottom: 8,
    },
    channelRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      paddingVertical: 13, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    channelIcon: { width: 34, height: 34, borderRadius: 11, alignItems: "center" as const, justifyContent: "center" as const },
    channelLabel: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text, flex: 1 },
    messageInput: {
      backgroundColor: C.surface2, borderRadius: R.card, borderWidth: 1, borderColor: C.border,
      paddingHorizontal: 14, paddingVertical: 12, fontFamily: FONT.regular, fontSize: 14, color: C.text,
      minHeight: 80, textAlignVertical: "top" as const, marginTop: 8,
    },
    charCount: { fontFamily: FONT.regular, fontSize: 11, color: C.textFaint, textAlign: "right" as const, marginTop: 4 },
  });
}

const MSG_LIMIT = 200;

export default function NotifyAssigneeScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ key: string | string[]; taskId: string }>();
  const key = Array.isArray(params.key) ? params.key.join("/") : (params.key ?? "");
  const taskId = params.taskId;
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { rec, getTask, recordNotification } = useMeeting();
  const task = getTask(taskId);

  const [selected, setSelected] = useState<Set<NotifyChannel>>(new Set(["whatsapp", "email"]));
  const [message, setMessage] = useState("");
  const [previewChannel, setPreviewChannel] = useState<NotifyChannel | null>(null);

  if (!task || !task.assignee) return null;
  const assignee = task.assignee;

  const toggle = (ch: NotifyChannel) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(ch)) next.delete(ch); else next.add(ch);
      return next;
    });
  };

  const openPreview = () => {
    const channels = Array.from(selected);
    if (!channels.length) return;
    // Preview the first selected channel; the sheet lets you step through
    // the rest before the flow is considered done.
    setPreviewChannel(channels[0]);
  };

  const finishChannel = (ch: NotifyChannel) => {
    recordNotification(task.id, [ch]);
    const remaining = Array.from(selected).filter((c) => c !== ch);
    if (remaining.length) {
      setSelected(new Set(remaining));
      setPreviewChannel(remaining[0]);
    } else {
      setPreviewChannel(null);
      router.replace({ pathname: "/recording/[key]/task/[taskId]", params: { key, taskId } });
    }
  };

  return (
    <KeyboardAware style={st.container}>
      <Stack.Screen options={{ title: "Notify Assignee" }} />
      <ScrollView contentContainerStyle={st.body} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled">
        <View style={st.assigneeCard}>
          <View style={[st.avatar, { backgroundColor: assignee.avatarColor }]}>
            <Text style={{ fontFamily: FONT.bold, fontSize: 16, color: "#FFFFFF" }}>{initialsOf(assignee.name)}</Text>
          </View>
          <View style={{ flex: 1 }}>
            <Text style={st.name}>{assignee.name}</Text>
            {assignee.email ? <Text style={st.contact}>{assignee.email}</Text> : null}
            {assignee.phone ? <Text style={st.contact}>{assignee.phone}</Text> : null}
          </View>
        </View>

        <Text style={st.label}>Send notification via</Text>
        {CHANNELS.map((ch) => (
          <View key={ch.key} style={st.channelRow}>
            <View style={[st.channelIcon, { backgroundColor: ch.color + "22" }]}>
              <Icon name={ch.icon as any} tintColor={ch.color} size={17} />
            </View>
            <Text style={st.channelLabel}>{ch.label}</Text>
            <Switch
              value={selected.has(ch.key)}
              onValueChange={() => toggle(ch.key)}
              trackColor={{ true: C.primary, false: C.border }}
              thumbColor="#FFFFFF"
            />
          </View>
        ))}

        <Text style={st.label}>Personal message (optional)</Text>
        <TextInput
          style={st.messageInput}
          value={message}
          onChangeText={(t) => setMessage(t.slice(0, MSG_LIMIT))}
          placeholder="Please check and share the proposal by tomorrow."
          placeholderTextColor={C.textFaint}
          multiline
        />
        <Text style={st.charCount}>{message.length}/{MSG_LIMIT}</Text>

        <Button
          label="Send Notification"
          onPress={openPreview}
          disabled={!selected.size}
          style={{ marginTop: S.xl }}
        />
      </ScrollView>

      {previewChannel ? (
        <NotificationPreviewSheet
          channel={previewChannel}
          task={task}
          meetingTitle={rec?.title || task.meetingTitle}
          message={message}
          onClose={() => setPreviewChannel(null)}
          onSent={() => finishChannel(previewChannel)}
        />
      ) : null}
    </KeyboardAware>
  );
}

function NotificationPreviewSheet({
  channel, task, meetingTitle, message, onClose, onSent,
}: {
  channel: NotifyChannel;
  task: Task;
  meetingTitle: string;
  message: string;
  onClose: () => void;
  onSent: () => void;
}) {
  const { C, T } = useTheme();
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState("");
  const assignee = task.assignee!;

  const text = buildNotificationText({
    assigneeName: assignee.name,
    meetingTitle,
    taskTitle: task.task,
    due: task.due || "no due date",
    priority: task.priority,
    fromName: "MinuteX Team",
    customMessage: message,
  }, channel);

  const send = async () => {
    setSending(true);
    setResult("");
    try {
      if (channel === "whatsapp") {
        const r = await sendWhatsApp(assignee.phone, text);
        setResult(
          r === "opened" ? "opened" :
          r === "no-contact-method" ? "This person has no phone number on file." :
          r === "unavailable" ? "WhatsApp isn't installed on this device." : "Couldn't open WhatsApp."
        );
        if (r === "opened") { onSent(); return; }
      } else if (channel === "email") {
        const r = await sendEmail(assignee.email, `Task Assigned — ${meetingTitle}`, text);
        setResult(
          r === "opened" ? "opened" :
          r === "no-contact-method" ? "This person has no email on file." :
          r === "unavailable" ? "No mail app is set up on this device." : "Couldn't open Mail."
        );
        if (r === "opened") { onSent(); return; }
      } else if (channel === "sms") {
        const r = await sendSms(assignee.phone, text);
        setResult(
          r === "opened" ? "opened" :
          r === "no-contact-method" ? "This person has no phone number on file." :
          r === "unavailable" ? "SMS isn't available on this device." : "Couldn't open Messages."
        );
        if (r === "opened") { onSent(); return; }
      } else {
        // "MinuteX App" — no push/notification backend exists. Recorded as
        // an in-app-only acknowledgement, not a real delivered notification.
        setResult("opened");
        onSent();
        return;
      }
    } finally {
      setSending(false);
    }
  };

  return (
    <View style={{ position: "absolute", left: 0, right: 0, top: 0, bottom: 0, backgroundColor: C.bg }}>
      <View style={{
        flexDirection: "row", alignItems: "center", gap: S.md,
        paddingHorizontal: 20, paddingTop: 60, paddingBottom: 14,
        borderBottomWidth: 1, borderBottomColor: C.border,
      }}>
        <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
          <Icon name="xmark" tintColor={C.text} size={20} />
        </Pressable>
        <Text style={{ fontFamily: FONT.extrabold, fontSize: 18, color: C.text, flex: 1 }}>
          {CHANNELS.find((c) => c.key === channel)?.label} Notification
        </Text>
      </View>

      <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 40 }}>
        <View style={{
          backgroundColor: channel === "whatsapp" ? "#0B141A" : C.surface2,
          borderRadius: R.card, padding: S.lg,
        }}>
          <Text style={{
            fontFamily: FONT.regular, fontSize: 14, lineHeight: 21,
            color: channel === "whatsapp" ? "#E9EDEF" : C.text,
          }}>
            {text}
          </Text>
        </View>

        {channel === "app" ? (
          <Text style={[T.caption, { marginTop: 12 }]}>
            There&apos;s no push notification service yet — this records the task as notified in MinuteX,
            but {assignee.name} won&apos;t receive an alert on their device.
          </Text>
        ) : (
          <Text style={[T.caption, { marginTop: 12 }]}>
            This opens {CHANNELS.find((c) => c.key === channel)?.label} with the message above pre-filled —
            you&apos;ll tap Send there yourself.
          </Text>
        )}

        {result && result !== "opened" ? (
          <View style={{ backgroundColor: C.dangerSoft, borderRadius: R.md, padding: S.md, marginTop: 12 }}>
            <Text style={[T.body, { color: C.danger }]}>{result}</Text>
          </View>
        ) : null}
      </ScrollView>

      <View style={{ padding: 20, paddingBottom: 32 }}>
        <Button label={`Send ${channel === "app" ? "" : "via " + CHANNELS.find((c) => c.key === channel)?.label}`.trim()} onPress={send} loading={sending} />
      </View>
    </View>
  );
}
