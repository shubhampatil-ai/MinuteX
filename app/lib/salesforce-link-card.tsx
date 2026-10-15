// lib/salesforce-link-card.tsx — the meeting screen's Salesforce card when
// the meeting is LINKED to records (Contract v3), instead of the older
// meeting type + one record. Shown only when the build turns linking on
// (EXPO_PUBLIC_SALESFORCE_LINK=1) AND the customer's Salesforce supports it.
//
//   Salesforce
//   Linked to  ★ Mumbai Heights · Property
//              Rahul Sharma · Lead
//   [ Change links ]   [ Send update now ]
//   ✓ Delivered · updates are sent automatically
//
// The records, outputs and task records are chosen in SalesforceLinkSheet;
// this card loads what the sheet needs (the link setup, the meeting's tasks
// and people) only when it opens, saves the choice, and pushes the meeting
// the first time.
import { useCallback, useMemo, useState } from "react";
import { Linking, Pressable, StyleSheet, Text, View } from "react-native";
import {
  StoredMeetingLinks, getMom, getParticipants, getTasks, listAiDocuments, pushWebhookMeeting,
  retryWebhookMeeting, searchLinkRecords, setMeetingLinks,
} from "./api";
import { refreshSalesforceDocumentPdfs, uploadMomPdf } from "./salesforce-pdfs";
import { FONT, S, useTheme, ColorScale } from "./theme";
import { Button, Card, ErrorText } from "./ui";
import { WebhookJob, canSendNow, deliveryLine, isDeliveryInFlight, pushButtonLabel } from "./webhook-flow";
import { webhookError } from "./webhook-ui";
import {
  LinkConfig, LinkedRecord, MeetingOutputs, TaskTarget, objectOf, sendableDocuments,
} from "./salesforce-link-flow";
import { LinkTask, SalesforceLinkSheet } from "./salesforce-link-sheet";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    heading: { fontFamily: FONT.bold, fontSize: 15, color: C.text, marginBottom: S.md },
    label: { fontFamily: FONT.bold, fontSize: 12, color: C.textDim, marginBottom: 6, letterSpacing: 0.3 },
    rec: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    sub: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim },
    none: { fontFamily: FONT.regular, fontSize: 13.5, color: C.textDim, marginBottom: S.md },
    statusTitle: { fontFamily: FONT.bold, fontSize: 14, marginTop: S.md },
    statusDetail: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim, marginTop: 2 },
    link: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary, marginTop: S.sm },
  });
}

/** Stored links -> what the sheet starts from. A task pointed at a record
 *  that isn't linked shows as "Another record" (its name isn't stored). */
export function sheetInitial(stored: StoredMeetingLinks | null) {
  if (!stored) return undefined;
  const links: LinkedRecord[] = stored.links.map((l) => ({ ...l }));
  const targets: Record<string, TaskTarget> = {};
  for (const [taskId, rel] of Object.entries(stored.task_relations || {})) {
    const linked = links.find((l) => l.id === rel.id);
    targets[taskId] = { object: rel.object, id: rel.id, name: linked?.name ?? "Another record" };
  }
  return { links, outputs: stored.included_outputs, targets,
           documents: stored.included_documents ?? null };
}

export function SalesforceLinkCard({
  meetingKey, workspaceId, config, links, job, onChanged,
}: {
  meetingKey: string;
  workspaceId: string;
  config: LinkConfig;
  links: StoredMeetingLinks | null;
  job: WebhookJob | null;
  /** Reload the meeting's Salesforce state after a change. */
  onChanged: () => Promise<void> | void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [open, setOpen] = useState(false);
  const [tasks, setTasks] = useState<LinkTask[]>([]);
  const [participantCount, setParticipantCount] = useState(0);
  const [has, setHas] = useState<MeetingOutputs | null>(null);
  const [busy, setBusy] = useState<"" | "push" | "retry" | "opening">("");
  const [error, setError] = useState("");

  const initial = useMemo(() => sheetInitial(links), [links]);
  const search = useCallback(async (q: string) => searchLinkRecords(workspaceId, q), [workspaceId]);

  async function openSheet() {
    setBusy("opening");
    setError("");
    // What the sheet shows about the meeting; a failure just shows fewer rows.
    // The MoM and the documents decide what can be ticked: if either can't
    // be loaded, nothing is held back (the server sends only what exists).
    const [t, p, mom, docs] = await Promise.all([
      getTasks(meetingKey).catch(() => []),
      getParticipants(meetingKey).catch(() => null),
      getMom(meetingKey).catch(() => null),
      listAiDocuments(meetingKey).catch(() => null),
    ]);
    setHas(mom && docs ? { mom: !!mom.exists && !!mom.mom, documents: sendableDocuments(docs.documents) }
      : null);
    setTasks(t.map((x) => ({ id: x.id, title: x.title || x.task, assignee: x.assignee?.name,
                             due: x.due_date || x.due })));
    setParticipantCount(p?.participants.length ?? 0);
    setBusy("");
    setOpen(true);
  }

  /** Render and upload the MoM PDF when the MoM is included. Never blocks
   *  the link: without it, the MoM text still goes. Returns a note or "". */
  async function attachMomPdf(): Promise<string> {
    try {
      const res = await getMom(meetingKey);
      if (!res.exists || !res.mom) return "";
      // The MoM goes to Salesforce as a PDF only, never as text.
      const why = await uploadMomPdf(meetingKey, res.mom);
      return why === "too_large" ? "The MoM PDF is larger than 2 MB, so the MoM wasn't sent."
        : why === "unsupported" ? "This version of the app can't make PDFs, so the MoM wasn't sent."
        : why ? "The MoM PDF couldn't be made, so the MoM wasn't sent." : "";
    } catch {
      return "The MoM PDF couldn't be attached, so the MoM wasn't sent. Save the MoM to try again.";
    }
  }

  async function submit(payload: Parameters<typeof setMeetingLinks>[1]) {
    // The PDF goes first, so the first delivery already carries it.
    const pdfNote = payload.included_outputs.includes("mom") ? await attachMomPdf() : "";
    try {
      await setMeetingLinks(meetingKey, payload);
      // Generated documents go as PDFs too (only the ones Salesforce lacks).
      if (payload.included_outputs.includes("documents")) await refreshSalesforceDocumentPdfs(meetingKey);
      // The first time, send it; after that saving the change is enough
      // (a delivered meeting updates itself).
      if (!job?.delivered && !isDeliveryInFlight(job)) await pushWebhookMeeting(meetingKey);
    } catch (e) {
      throw new Error(webhookError(e));
    }
    setError(pdfNote);
    await onChanged();
  }

  async function act(kind: "push" | "retry") {
    setBusy(kind);
    try {
      if (kind === "push") await pushWebhookMeeting(meetingKey);
      else await retryWebhookMeeting(meetingKey);
      setError("");
      await onChanged();
    } catch (e) {
      setError(webhookError(e));
    } finally {
      setBusy("");
    }
  }

  const line = deliveryLine(job);
  const toneColor = line.tone === "ok" ? C.success : line.tone === "warn" ? C.warn
    : line.tone === "busy" ? C.primary : C.textDim;
  const failed = job?.delivery_status === "failed";
  const linked = links?.links ?? [];

  return (
    <Card>
      <Text style={st.heading}>Salesforce</Text>
      {error ? <ErrorText>{error}</ErrorText> : null}

      {linked.length ? (
        <View style={{ marginBottom: S.md, gap: 6 }}>
          <Text style={st.label}>LINKED TO</Text>
          {linked.map((l) => (
            <View key={l.id}>
              <Text style={st.rec} numberOfLines={2}>
                {linked.length > 1 && l.primary ? "★ " : ""}{l.name}
              </Text>
              <Text style={st.sub}>{objectOf(config, l.object)?.label ?? l.object}</Text>
            </View>
          ))}
        </View>
      ) : (
        <Text style={st.none}>Link this meeting to the Salesforce records it&apos;s about.</Text>
      )}

      <Button label={linked.length ? "Change links" : "Link to Salesforce"}
        variant={linked.length ? "secondary" : "primary"}
        onPress={() => { void openSheet(); }} loading={busy === "opening"} disabled={!!busy} />
      {linked.length && failed ? (
        <Button label="Retry" onPress={() => void act("retry")} loading={busy === "retry"}
          disabled={!!busy} style={{ marginTop: S.sm }} />
      ) : linked.length && job?.delivered ? (
        <Button label={pushButtonLabel(job)} variant="ghost" onPress={() => void act("push")}
          loading={busy === "push"} disabled={!!busy || !canSendNow(job)} style={{ marginTop: S.sm }} />
      ) : null}

      {linked.length ? (
        <>
          <Text style={[st.statusTitle, { color: toneColor }]}>
            {line.tone === "ok" ? "✓ " : ""}{line.title}
          </Text>
          {line.detail ? <Text style={st.statusDetail}>{line.detail}</Text> : null}
          {job?.external?.url ? (
            <Pressable onPress={() => { void Linking.openURL(job.external!.url); }} accessibilityRole="link">
              <Text style={st.link}>Open in Salesforce</Text>
            </Pressable>
          ) : null}
        </>
      ) : null}

      <SalesforceLinkSheet visible={open} onClose={() => setOpen(false)} config={config}
        tasks={tasks} participantCount={participantCount} has={has} initial={initial}
        searchRecords={search} onSubmit={submit}
        submitLabel={linked.length ? "Save links" : "Link meeting"} />
    </Card>
  );
}
