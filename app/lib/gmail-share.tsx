// lib/gmail-share.tsx — email a meeting's minutes through the user's Gmail.
//
// THE SHEET EXISTS BECAUSE SENDING MUST BE DELIBERATE. Gmail being connected
// is permission to send when asked, never a reason to send automatically. So
// nothing leaves until the user has seen exactly who will receive it and
// exactly what will be attached, and pressed Send. That is the whole design
// brief for this component, and it is why the recipient list is unchecked-by-
// default for nobody: participants are pre-selected (they were in the meeting)
// but every one is visibly listed and can be removed before sending.
//
// PARTICIPANTS WITHOUT AN EMAIL ADDRESS ARE SHOWN, NOT HIDDEN. The backend
// returns them separately (`unresolved`) and refuses a send that includes one.
// Dropping them silently would make "sent to 2 of 3 people" look identical to
// "sent to everyone", and the person left out would never know. So they appear
// in their own group, greyed, with the reason — and a route to fix it, since
// adding the address to the contact is a thing the user can actually do.
//
// IT SHARES MEETING OUTPUTS, NOT "the MoM". A meeting produces Minutes, an
// Executive Summary, an Action Item Report, a Follow-up Email draft, domain
// reports and any number of freeform AI documents. Every one is something a
// person would email, so the sheet lists whatever this meeting actually has
// (lib/meeting-outputs.ts) and lets the user pick outputs AND per-output
// formats. A new AI document type needs no change here — it just appears.
//
// ATTACHMENTS ARE RENDERED ON-DEVICE by the SAME renderers the preview and
// the export button use, so what lands in the inbox is what the sender saw.
// PDF is offered only when expo-print is in the build rather than offered and
// then failing.
//
// This component assumes Gmail is usable — its caller gates on useGmail().
// The backend enforces the same rule independently (409 with an integration
// code), so a stale mount cannot send.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Text,
  TextInput, View,
} from "react-native";
import { useRouter } from "expo-router";
import { Icon } from "./icons";
import { R, FONT, useTheme, ColorScale } from "./theme";
import {
  Button, Divider, ErrorText, KeyboardAwareSheet, scrollFormProps,
} from "./ui";
import {
  AiDocument, ApiError, EmailAttachment, Mom, ResolvedRecipient,
  getMeetingEmailRecipients, getMom, isIntegrationUnavailable, listAiDocuments,
  sendMeetingEmail,
} from "./api";
import {
  FORMAT_LABEL, MAX_ATTACHMENTS, MeetingOutput, OutputFormat,
  buildOutputAttachment, defaultFormat, describeBuildFailure, meetingOutputs,
} from "./meeting-outputs";

export type GmailShareSheetProps = {
  visible: boolean;
  onClose: () => void;
  recordingKey: string;
  meetingTitle: string;
  /** The structured MoM, when the caller already has it. Omitted, the sheet
   *  fetches it itself — most callers (an overflow menu) do not. */
  mom?: Mom | null;
  /** The meeting's generated AI documents. The caller usually HAS these
   *  already (MeetingProvider loads them for the Documents tab), so passing
   *  them avoids a redundant fetch; omitted, the sheet loads them itself.
   *
   *  A meeting with no outputs at all is NOT an error: the sheet still sends a
   *  message, it just has nothing to attach — a legitimate follow-up note. */
  documents?: AiDocument[];
  /** Called after a successful send, so the caller can show its own toast. */
  onSent?: (recipientCount: number) => void;
};

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" },
    sheet: {
      backgroundColor: C.bg, borderTopLeftRadius: R.lg, borderTopRightRadius: R.lg,
      paddingHorizontal: 20, paddingTop: 18, maxHeight: "90%",
    },
    head: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "space-between" as const, marginBottom: 4,
    },
    title: { fontFamily: FONT.extrabold, fontSize: 19, color: C.text },
    from: {
      fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 3,
    },
    sectionTitle: {
      fontFamily: FONT.bold, fontSize: 12, color: C.textDim,
      textTransform: "uppercase" as const, letterSpacing: 0.8, marginTop: 20,
      marginBottom: 8,
    },
    row: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 12,
      paddingVertical: 11,
    },
    box: {
      width: 21, height: 21, borderRadius: 6, borderWidth: 1.5,
      borderColor: C.border, alignItems: "center" as const,
      justifyContent: "center" as const,
    },
    boxOn: { backgroundColor: C.primary, borderColor: C.primary },
    rowName: { fontFamily: FONT.semibold, fontSize: 14, color: C.text },
    rowSub: { fontFamily: FONT.regular, fontSize: 12, color: C.textFaint, marginTop: 2 },
    rowMuted: { opacity: 0.5 },
    warn: {
      fontFamily: FONT.regular, fontSize: 12, color: C.warn, lineHeight: 17,
      marginTop: 2,
    },
    // Format chips sit indented under their output, so the hierarchy
    // "this document, in these formats" is visible rather than implied.
    formatRow: {
      flexDirection: "row" as const, gap: 8, paddingLeft: 33, paddingBottom: 10,
      flexWrap: "wrap" as const,
    },
    chip: {
      borderWidth: 1, borderColor: C.border, borderRadius: R.pill,
      paddingHorizontal: 12, paddingVertical: 5,
    },
    chipOn: { backgroundColor: C.primary, borderColor: C.primary },
    chipTxt: { fontFamily: FONT.semibold, fontSize: 11.5, color: C.textDim },
    chipTxtOn: { color: C.textOnPrimary },
    input: {
      borderWidth: 1, borderColor: C.border, borderRadius: R.sm,
      backgroundColor: C.surface, paddingHorizontal: 13, paddingVertical: 11,
      fontFamily: FONT.regular, fontSize: 14, color: C.text,
    },
    bodyInput: { minHeight: 96, textAlignVertical: "top" as const },
    empty: {
      fontFamily: FONT.regular, fontSize: 13, color: C.textFaint,
      lineHeight: 19, paddingVertical: 8,
    },
    link: { fontFamily: FONT.bold, fontSize: 13, color: C.primary },
    footer: { paddingTop: 18, paddingBottom: 26, gap: 10 },
    progress: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim,
      textAlign: "center" as const,
    },
  });
}

export function GmailShareSheet({
  visible, onClose, recordingKey, meetingTitle, mom, documents, onSent,
}: GmailShareSheetProps) {
  const { C } = useTheme();
  const router = useRouter();
  const st = useMemo(() => buildStyles(C), [C]);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [recipients, setRecipients] = useState<ResolvedRecipient[]>([]);
  const [unresolved, setUnresolved] = useState<ResolvedRecipient[]>([]);
  // Selected by contact_id. Participants start selected — they were in the
  // meeting — but every one is listed and removable before Send.
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  // What this meeting can actually share, resolved when the sheet opens.
  const [outputs, setOutputs] = useState<MeetingOutput[]>([]);
  // Which formats are ticked, per output id. An output with no entry (or an
  // empty array) is simply not being sent — one map answers both "is this
  // output selected" and "in which formats", which are the same question.
  const [picked, setPicked] = useState<Record<string, OutputFormat[]>>({});
  // The structured MoM behind a mom-kind output, needed at render time.
  const [activeMom, setActiveMom] = useState<Mom | null>(null);
  // "Preparing Summary (PDF)…" — rendering several documents takes a moment,
  // and a silent spinner makes a slow send look like a hung one.
  const [progress, setProgress] = useState("");

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      // Three reads in parallel. The sheet cannot render until it knows the
      // recipients; waiting for the MoM and the documents in series would
      // triple the wait for values only the attachment rows need.
      //
      // The MoM and document reads are each allowed to fail on their own. A
      // meeting without them is an ordinary case (nobody generated any yet),
      // and it must not stop the user sending a message — so a failure
      // narrows the sheet to "nothing to attach" rather than erroring.
      const [res, momRes, docRes] = await Promise.all([
        getMeetingEmailRecipients(recordingKey),
        mom ? Promise.resolve(null) : getMom(recordingKey).catch(() => null),
        documents
          ? Promise.resolve(null)
          : listAiDocuments(recordingKey).catch(() => null),
      ]);

      const loadedMom = mom ?? (momRes?.exists ? momRes.mom : null);
      const loadedDocs = documents ?? (docRes ? Object.values(docRes.documents) : []);

      setActiveMom(loadedMom);
      setOutputs(meetingOutputs(loadedDocs, loadedMom));
      // NOTHING is ticked by default. Attachments are the part of this sheet
      // with a cost to the recipient, and pre-selecting items from a list the
      // user has not read yet is how people send documents they did not mean
      // to. Recipients are different: they are this meeting's own
      // participants, and choosing the meeting already chose them.
      setPicked({});

      setRecipients(res.recipients);
      setUnresolved(res.unresolved);
      setSelected(Object.fromEntries(
        res.recipients.map((r) => [r.contact_id, true])));

      const title = res.meeting_title || meetingTitle;
      // GENERIC wording. This sheet no longer sends only Minutes, so a
      // hardcoded "Minutes of Meeting" subject would be wrong the moment
      // somebody shares an Executive Summary. Both stay fully editable.
      setSubject(`Meeting notes — ${title}`);
      setBody(`Hi,\n\nPlease find the meeting outputs from ${title}.`
        + `\n\nRegards,\nMinuteX`);
    } catch (e) {
      // A Gmail connection that died between opening the meeting and opening
      // this sheet lands here. Say so plainly; the caller's gate catches up on
      // its next status read.
      setError(
        isIntegrationUnavailable(e)
          ? (e as ApiError).message
          : e instanceof ApiError ? e.message
            : "Couldn’t load this meeting’s outputs.");
    } finally {
      setLoading(false);
    }
  }, [recordingKey, meetingTitle, mom, documents]);

  // Re-read every time the sheet OPENS, not once on mount: participants,
  // their email addresses and the document list all change while the meeting
  // screen stays mounted behind it.
  useEffect(() => { if (visible) load(); }, [visible, load]);

  const chosenRecipients = recipients.filter((r) => selected[r.contact_id]);
  const chosenFormatCount = Object.values(picked)
    .reduce((n, f) => n + f.length, 0);

  const toggleRecipient = (contactId: string) =>
    setSelected((prev) => ({ ...prev, [contactId]: !prev[contactId] }));

  /** Total files a selection would produce. The cap counts FILES, not
   *  documents — one output in three formats is three attachments. */
  const fileCount = (p: Record<string, OutputFormat[]>) =>
    Object.values(p).reduce((n, f) => n + f.length, 0);

  /** Refuse a change that would exceed the backend's ceiling, and say so.
   *  Enforced here as well as server-side because the backend can only reject
   *  AFTER the device has rendered every file — see MAX_ATTACHMENTS. */
  const commit = (next: Record<string, OutputFormat[]>) => {
    if (fileCount(next) > MAX_ATTACHMENTS) {
      setError(`You can attach at most ${MAX_ATTACHMENTS} files. `
        + `Unselect something first.`);
      return;
    }
    setError("");
    setPicked(next);
  };

  /** Tick/untick a whole output. Turning one ON selects its default format so
   *  a checked row always means at least one real file — a ticked document
   *  with no format would silently send nothing. */
  const toggleOutput = (output: MeetingOutput) => {
    const next = { ...picked };
    if (next[output.id]?.length) { delete next[output.id]; setError(""); setPicked(next); return; }
    next[output.id] = [defaultFormat(output)];
    commit(next);
  };

  /** Tick/untick ONE format. Removing the last one unticks the output, so the
   *  checkbox never contradicts the chips underneath it. */
  const toggleFormat = (output: MeetingOutput, format: OutputFormat) => {
    const current = picked[output.id] ?? [];
    const next = { ...picked };
    const updated = current.includes(format)
      ? current.filter((f) => f !== format)
      : [...current, format];
    if (updated.length) next[output.id] = updated;
    else delete next[output.id];
    // Removing never needs the cap check — it can only reduce the count.
    if (updated.length < current.length) { setError(""); setPicked(next); return; }
    commit(next);
  };

  const onSend = async () => {
    if (!chosenRecipients.length) return;
    setSending(true); setError(""); setProgress("");
    try {
      // Render every selected output/format pair. Sequential on purpose:
      // printToFileAsync drives a native print engine, and several concurrent
      // renders on a phone is how it runs out of memory on a long document.
      const attachments: EmailAttachment[] = [];
      for (const output of outputs) {
        for (const format of picked[output.id] ?? []) {
          setProgress(`Preparing ${output.label} (${FORMAT_LABEL[format]})…`);
          const built = await buildOutputAttachment(
            output, format, meetingTitle, activeMom);
          if (!built.ok) {
            // Stop rather than send a partial set. The user ticked these
            // documents; quietly dropping one and reporting success is the
            // failure mode this whole sheet is built to avoid.
            setSending(false); setProgress("");
            setError(describeBuildFailure(built));
            return;
          }
          attachments.push(built.attachment);
        }
      }

      setProgress("Sending…");
      const res = await sendMeetingEmail(recordingKey, {
        // contact_id only: the backend resolves the address from the OWNER's
        // contact, so a tampered payload cannot redirect the mail elsewhere.
        recipients: chosenRecipients.map((r) => ({ contact_id: r.contact_id })),
        subject, body, attachments,
      });
      setSending(false); setProgress("");
      onSent?.(res.recipient_count);
      onClose();
    } catch (e) {
      setSending(false); setProgress("");
      setError(e instanceof ApiError ? e.message : "Couldn’t send the email.");
    }
  };

  const openContact = (contactId: string) => {
    onClose();
    router.push({ pathname: "/contact/[id]", params: { id: contactId } });
  };

  return (
    <Modal visible={visible} transparent animationType="slide"
      onRequestClose={onClose}>
      {/* The subject and message inputs sit near the bottom of the sheet, so
          without this the keyboard covers exactly what the user is typing.
          "height" is the right behavior inside a Modal — a modal is its own
          window and is never resized by the OS. See
          lib/__tests__/keyboard-avoidance.test.mjs for the full rule. */}
      <KeyboardAwareSheet>
      <View style={st.backdrop}>
        <Pressable style={{ flex: 1 }} onPress={onClose} accessibilityLabel="Close" />
        <View style={st.sheet}>
          <View style={st.head}>
            <View style={{ flex: 1 }}>
              <Text style={st.title}>Share meeting outputs</Text>
              <Text style={st.from}>Sent from your connected Gmail account</Text>
            </View>
            <Pressable onPress={onClose} hitSlop={12} accessibilityLabel="Close">
              <Icon name="xmark" tintColor={C.textFaint} size={19} />
            </Pressable>
          </View>

          {loading ? (
            <View style={{ paddingVertical: 44, alignItems: "center" }}>
              <ActivityIndicator color={C.textFaint} />
            </View>
          ) : (
            <ScrollView {...scrollFormProps} style={{ maxHeight: 430 }}>
              {/* ---- Documents ---- */}
              <Text style={st.sectionTitle}>Documents</Text>
              {!outputs.length ? (
                <Text style={st.empty}>
                  This meeting has no generated documents yet, so there is
                  nothing to attach. The message will still be sent.
                </Text>
              ) : outputs.map((output) => {
                const formats = picked[output.id] ?? [];
                const on = formats.length > 0;
                return (
                  <View key={output.id}>
                    <Pressable style={st.row} onPress={() => toggleOutput(output)}
                      accessibilityRole="checkbox"
                      accessibilityState={{ checked: on }}
                      accessibilityLabel={output.label}>
                      <View style={[st.box, on && st.boxOn]}>
                        {on ? <Icon name="checkmark" tintColor={C.textOnPrimary} size={13} /> : null}
                      </View>
                      <View style={{ flex: 1 }}>
                        <Text style={st.rowName}>{output.label}</Text>
                        {on ? (
                          <Text style={st.rowSub}>
                            {formats.map((f) => FORMAT_LABEL[f]).join(" · ")}
                          </Text>
                        ) : null}
                      </View>
                    </Pressable>
                    {/* Formats appear only once the document is selected —
                        an unticked row does not need three more controls. */}
                    {on ? (
                      <View style={st.formatRow}>
                        {output.formats.map((f) => {
                          const chosen = formats.includes(f);
                          return (
                            <Pressable
                              key={f}
                              onPress={() => toggleFormat(output, f)}
                              style={[st.chip, chosen && st.chipOn]}
                              accessibilityRole="checkbox"
                              accessibilityState={{ checked: chosen }}
                              accessibilityLabel={`${output.label} ${FORMAT_LABEL[f]}`}
                            >
                              <Text style={[st.chipTxt, chosen && st.chipTxtOn]}>
                                {FORMAT_LABEL[f]}
                              </Text>
                            </Pressable>
                          );
                        })}
                      </View>
                    ) : null}
                  </View>
                );
              })}

              <Divider style={{ marginTop: 8 }} />

              {/* ---- Recipients ---- */}
              <Text style={st.sectionTitle}>Recipients</Text>

              {!recipients.length && !unresolved.length ? (
                <Text style={st.empty}>
                  No participants have been matched to a contact yet. Map
                  speakers to contacts on this meeting, then share.
                </Text>
              ) : null}

              {recipients.map((r) => {
                const on = !!selected[r.contact_id];
                return (
                  <Pressable key={r.contact_id} style={st.row}
                    onPress={() => toggleRecipient(r.contact_id)}
                    accessibilityRole="checkbox"
                    accessibilityState={{ checked: on }}>
                    <View style={[st.box, on && st.boxOn]}>
                      {on ? <Icon name="checkmark" tintColor={C.textOnPrimary} size={13} /> : null}
                    </View>
                    <View style={{ flex: 1 }}>
                      <Text style={st.rowName}>{r.name || r.email}</Text>
                      <Text style={st.rowSub}>{r.email}</Text>
                    </View>
                  </Pressable>
                );
              })}

              {/* Participants with no address. Shown, never silently dropped —
                  see the file header. */}
              {unresolved.length ? (
                <>
                  <Text style={st.sectionTitle}>No email address</Text>
                  {unresolved.map((r) => (
                    <View key={r.contact_id} style={[st.row, st.rowMuted]}>
                      <View style={st.box}>
                        <Icon name="xmark" tintColor={C.textFaint} size={11} />
                      </View>
                      <View style={{ flex: 1 }}>
                        <Text style={st.rowName}>{r.name || "This participant"}</Text>
                        <Text style={st.warn}>
                          Email address unavailable for{" "}
                          {r.name || "this participant"}.
                        </Text>
                      </View>
                      <Pressable onPress={() => openContact(r.contact_id)} hitSlop={8}>
                        <Text style={st.link}>Add</Text>
                      </Pressable>
                    </View>
                  ))}
                </>
              ) : null}

              {/* ---- Message ---- */}
              <Text style={st.sectionTitle}>Subject</Text>
              <TextInput
                style={st.input}
                value={subject}
                onChangeText={setSubject}
                placeholder="Subject"
                placeholderTextColor={C.textFaint}
              />

              <Text style={st.sectionTitle}>Message</Text>
              <TextInput
                style={[st.input, st.bodyInput]}
                value={body}
                onChangeText={setBody}
                multiline
                placeholder="Write a short message…"
                placeholderTextColor={C.textFaint}
              />
            </ScrollView>
          )}

          <View style={st.footer}>
            {error ? <ErrorText>{error}</ErrorText> : null}
            {sending && progress ? <Text style={st.progress}>{progress}</Text> : null}
            <Button
              label={
                chosenRecipients.length
                  ? `Send to ${chosenRecipients.length} `
                    + `${chosenRecipients.length === 1 ? "person" : "people"}`
                    + (chosenFormatCount
                      ? ` · ${chosenFormatCount} file${chosenFormatCount === 1 ? "" : "s"}`
                      : "")
                  : "Send"
              }
              onPress={onSend}
              loading={sending}
              disabled={loading || !chosenRecipients.length || !subject.trim()}
            />
          </View>
        </View>
      </View>
      </KeyboardAwareSheet>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// The Gmail-dependent entry point.
// ---------------------------------------------------------------------------

/**
 * The row a screen renders to offer "Share meeting outputs".
 *
 * THIS IS THE VISIBILITY RULE, in one place. When Gmail is not usable the row
 * does not become an action — it becomes a prompt that explains why and routes
 * to the place that fixes it. Screens must not re-derive this: they render
 * <GmailShareRow> and it decides.
 *
 * `hidden` mode exists for surfaces where an inert row would be clutter (an
 * overflow menu that is already long). Both are legitimate readings of the
 * requirement — hide, or disable with an explanation — and the choice belongs
 * to the surface, not to this component.
 */
export function GmailShareRow({
  usable, needsReauth, onPress, mode = "prompt", style,
}: {
  usable: boolean;
  needsReauth: boolean;
  onPress: () => void;
  mode?: "prompt" | "hidden";
  style?: any;
}) {
  const { C } = useTheme();
  const router = useRouter();
  const st = useMemo(() => buildStyles(C), [C]);

  if (!usable && mode === "hidden") return null;

  if (!usable) {
    return (
      <Pressable
        onPress={() => router.push("/integrations/gmail" as any)}
        style={({ pressed }) => [st.row, { opacity: pressed ? 0.6 : 0.75 }, style]}
        accessibilityRole="button"
        accessibilityLabel={needsReauth
          ? "Reconnect Gmail to use this feature"
          : "Connect Gmail to use this feature"}
      >
        <Icon name="envelope.badge" tintColor={C.textFaint} size={19} />
        <View style={{ flex: 1 }}>
          <Text style={[st.rowName, { color: C.textDim }]}>Share outputs by Gmail</Text>
          <Text style={st.rowSub}>
            {needsReauth
              ? "Reconnect Gmail to use this feature."
              : "Connect Gmail to use this feature."}
          </Text>
        </View>
        <Icon name="chevron.right" tintColor={C.textFaint} size={15} />
      </Pressable>
    );
  }

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [st.row, pressed && { opacity: 0.6 }, style]}
      accessibilityRole="button"
      accessibilityLabel="Share meeting outputs by Gmail"
    >
      <Icon name="envelope.badge" tintColor={C.text} size={19} />
      <Text style={[st.rowName, { flex: 1 }]}>Share outputs by Gmail</Text>
      <Icon name="chevron.right" tintColor={C.textFaint} size={15} />
    </Pressable>
  );
}
