// lib/webhook-meeting.tsx — the meeting screen's Salesforce block (Webhook
// Contract v2 underneath; on screen it is simply "Salesforce"):
//
//   Meeting type        [ Site Visit ▾ ]          <- the customer's types
//   Property            PV-2026-00452 — Mumbai Heights   (only if the type
//                                                         uses a record)
//   [ Push to Salesforce ]   ✓ Delivered · updates are sent automatically
//
// The people in the meeting are NOT chosen here: they are the tagged
// participants (Speakers tab), sent as-is. The meeting type and the record
// are separate from them and from each other (docs §3.1).
//
// Three states (meetingBlockMode): connected -> the interactive block above;
// not connected (or `readOnly`) but already sent -> a read-only card saying
// where it went; never sent and not connected -> nothing at all.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Linking, Pressable, StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import {
  ApiLinkConfig, StoredMeetingLinks, getSalesforceOrgConnection,
  pushWebhookMeeting, refreshWebhookConfiguration, retryWebhookMeeting, setWebhookContext,
} from "./api";
import { fetchLinkConfig, fetchWebhookMeeting } from "./shared-data";
import { SALESFORCE_LINK_ENABLED } from "./salesforce-link-sheet";
import { SALESFORCE_OAUTH_ENABLED } from "./salesforce-login-card";
import { useWorkspace } from "./workspace-context";
import { SalesforceLinkCard } from "./salesforce-link-card";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { Button, Card, ErrorText } from "./ui";
import {
  WebhookConfiguration, WebhookContext, WebhookEarlierRecord, WebhookJob, WebhookMeetingType,
  WebhookMode, WebhookRecord, canPush, deliveryLine, historyLine, isDeliveryInFlight,
  meetingBlockMode, pushButtonLabel, recordDisplay, recordFieldState, recordLabel, selectedType,
} from "./webhook-flow";
import { MeetingTypeSheet, RecordSearchSheet } from "./webhook-pickers";
import { webhookError } from "./webhook-ui";
import { usePolling } from "./site-salesforce-ui";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    heading: { fontFamily: FONT.bold, fontSize: 15, color: C.text, marginBottom: S.md },
    label: { fontFamily: FONT.bold, fontSize: 12, color: C.textDim, marginBottom: 6,
             letterSpacing: 0.3 },
    field: { borderWidth: 1, borderColor: C.border, borderRadius: R.md, minHeight: 48,
             paddingHorizontal: S.md, paddingVertical: 10, justifyContent: "center",
             marginBottom: S.md, backgroundColor: C.surface },
    fieldText: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    placeholder: { fontFamily: FONT.regular, fontSize: 14.5, color: C.textFaint },
    sub: { fontFamily: FONT.regular, fontSize: 12, color: C.textDim, marginTop: 2 },
    labelRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
    required: { fontFamily: FONT.bold, fontSize: 10.5, color: C.warn, marginBottom: 6 },
    clear: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary, marginBottom: 6 },
    statusTitle: { fontFamily: FONT.bold, fontSize: 14, marginTop: S.md },
    statusDetail: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim,
                    marginTop: 2 },
    reason: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textFaint, marginTop: S.sm },
    link: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary, marginTop: S.sm },
  });
}

type State = {
  mode: WebhookMode;
  endpoint: { enabled: boolean };
  earlier_record: WebhookEarlierRecord | null;
  links: StoredMeetingLinks | null;
  job: WebhookJob | null;
  context: WebhookContext;
  configuration: WebhookConfiguration | null;
};

export function WebhookMeetingBlock({
  meetingKey, workspaceId, enabled = true, readOnly = false,
}: {
  meetingKey: string;
  workspaceId: string;
  /** The build flag (EXPO_PUBLIC_WEBHOOK_V2). Off: renders nothing and
   *  never calls the webhook routes. */
  enabled?: boolean;
  /** Show only what was already sent; nothing can be pressed (the internal
   *  "coming soon" builds). */
  readOnly?: boolean;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [state, setState] = useState<State | null>(null);
  const [off, setOff] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"" | "push" | "retry" | "context" | "refresh">("");
  const [sheet, setSheet] = useState<"" | "type" | "record">("");

  // `force` (the default: the delivery poll, and after every change) always
  // asks the server. Only the mount passes false: this block is unmounted on
  // every tab switch, and a 15-second-old answer is good enough to paint
  // (lib/shared-data.ts fetchWebhookMeeting).
  const load = useCallback(async (force = true) => {
    if (!enabled) return;
    try {
      const read = await fetchWebhookMeeting(meetingKey, { force });
      // Switched off on the server, or this person can't manage the meeting:
      // no Salesforce section rather than an error.
      if (read.off) { setOff(true); return; }
      const r = read.meeting;
      setState({ mode: r.mode, endpoint: { enabled: !!r.endpoint?.enabled },
                 earlier_record: r.earlier_record ?? null, links: r.links ?? null,
                 job: r.job, context: r.context, configuration: r.configuration });
      setOff(false);
      setError("");
    } catch (e) {
      setError(webhookError(e));
    }
  }, [meetingKey, enabled]);

  useEffect(() => { void load(false); }, [load]);

  // Linking (behind EXPO_PUBLIC_SALESFORCE_LINK): the setup. With the flag
  // on, the older meeting type + record card is NEVER shown; a failure to
  // load the setup says so and offers Try again.
  const [linkConfig, setLinkConfig] = useState<ApiLinkConfig | null>(null);
  const [linkConfigError, setLinkConfigError] = useState("");
  const [linkConfigTry, setLinkConfigTry] = useState(0);
  useEffect(() => {
    if (!enabled || !SALESFORCE_LINK_ENABLED || !workspaceId) return undefined;
    let live = true;
    setLinkConfigError("");
    // Reused for 5 minutes; "Try again" (linkConfigTry > 0) always asks.
    fetchLinkConfig(workspaceId, { force: linkConfigTry > 0 })
      .then((c) => { if (live) setLinkConfig(c); })
      .catch((e) => { if (live) setLinkConfigError(webhookError(e)); });
    return () => { live = false; };
  }, [enabled, workspaceId, linkConfigTry]);
  const blockMode = state ? meetingBlockMode(state, readOnly) : "none";
  usePolling(blockMode === "live" && isDeliveryInFlight(state?.job), () => { void load(); }, 3000);

  // HALF SET UP: the Salesforce login is connected but the receiver (address
  // + signing secret) isn't, so nothing can be linked or pushed. Asked only
  // then — a workspace that doesn't use Salesforce never sees this.
  const router = useRouter();
  const { can } = useWorkspace();
  const [loginOnly, setLoginOnly] = useState(false);
  // Keyed on WHETHER the state is loaded, not on the state object: that is
  // rebuilt on every reload, and re-asked this each time.
  const hasState = !!state;
  useEffect(() => {
    if (!enabled || readOnly || !SALESFORCE_OAUTH_ENABLED || !workspaceId
        || !hasState || blockMode !== "none") return undefined;
    let live = true;
    getSalesforceOrgConnection(workspaceId)
      .then((c) => { if (live) setLoginOnly(!!c.connected); })
      .catch(() => { if (live) setLoginOnly(false); });
    return () => { live = false; };
  }, [enabled, readOnly, workspaceId, hasState, blockMode]);

  // Feature off (build or server): nothing is offered that cannot work.
  if (!enabled || off) return null;
  if (!state) return error ? <ErrorText>{error}</ErrorText> : null;
  // Not connected and never sent: no Salesforce section on the meeting —
  // unless the login is connected without the receiver, which is a setup
  // to finish, not "no Salesforce".
  if (blockMode === "none") {
    if (!loginOnly) return null;
    return (
      <Card>
        <Text style={st.heading}>Salesforce</Text>
        <Text style={st.statusDetail}>
          Salesforce isn&apos;t fully set up for this workspace, so this meeting can&apos;t be
          linked or sent yet.
        </Text>
        {can("manage_integrations") ? (
          <Button label="Finish Salesforce setup" variant="secondary" style={{ marginTop: S.md }}
            onPress={() => router.push("/salesforce" as never)} />
        ) : (
          <Text style={st.reason}>An owner or manager of this workspace finishes the setup.</Text>
        )}
      </Card>
    );
  }
  if (blockMode === "history") {
    // Already sent, not connected now (or pushing is off in this build):
    // where it went, read-only. The link comes from the delivery, or from
    // the retired package's own record of it.
    const delivered = !!state.job?.delivered;
    const sent = delivered ? recordDisplay(state.context.record) : null;
    const url = (delivered && state.job?.external?.url) || state.earlier_record?.url || "";
    return (
      <Card>
        <Text style={st.heading}>Salesforce</Text>
        <Text style={[st.statusTitle, { color: C.success, marginTop: 0 }]}>
          {"\u2713 "}{historyLine(state.job, state.earlier_record)}
        </Text>
        {delivered && state.context.meeting_type_name ? (
          <Text style={st.statusDetail}>{state.context.meeting_type_name}</Text>
        ) : null}
        {sent ? (
          <Text style={st.statusDetail} numberOfLines={2}>
            {sent.title}{sent.sub ? ` \u00b7 ${sent.sub}` : ""}
          </Text>
        ) : !delivered && state.earlier_record?.name ? (
          <Text style={st.statusDetail} numberOfLines={2}>{state.earlier_record.name}</Text>
        ) : null}
        <Text style={st.reason}>
          {readOnly ? "Changes to this meeting aren't sent to Salesforce in this version."
            : "Salesforce isn't connected now, so changes to this meeting aren't sent."}
        </Text>
        {url ? (
          <Pressable onPress={() => { void Linking.openURL(url); }} accessibilityRole="link">
            <Text style={st.link}>Open in Salesforce</Text>
          </Pressable>
        ) : null}
      </Card>
    );
  }

  // Linked to records (Contract v3). With the flag on this is the ONLY
  // card: meeting type + record never comes back.
  if (SALESFORCE_LINK_ENABLED) {
    if (linkConfig?.supported) {
      return (
        <SalesforceLinkCard meetingKey={meetingKey} workspaceId={workspaceId}
          config={linkConfig} links={state.links} job={state.job} onChanged={load} />
      );
    }
    return (
      <Card>
        <Text style={st.heading}>Salesforce</Text>
        {linkConfigError ? (
          <>
            <ErrorText>{linkConfigError}</ErrorText>
            <Button label="Try again" variant="secondary" style={{ marginTop: S.md }}
              onPress={() => setLinkConfigTry((n) => n + 1)} />
          </>
        ) : linkConfig ? (
          <Text style={st.statusDetail}>
            This Salesforce setup doesn&apos;t support linking meetings yet. Your Salesforce
            admin updates the MinuteX receiver.
          </Text>
        ) : (
          <Text style={st.statusDetail}>Loading Salesforce…</Text>
        )}
      </Card>
    );
  }

  const { job, context, configuration } = state;
  const type = selectedType(configuration, context);
  const recordState = recordFieldState(type);
  const record = recordDisplay(context.record);
  const blocked = canPush(configuration, context, job);
  const line = deliveryLine(job);
  const toneColor = line.tone === "ok" ? C.success : line.tone === "warn" ? C.warn
    : line.tone === "busy" ? C.primary : C.textDim;

  async function saveContext(meetingTypeId: string | null,
                             rec: { id: string; display_name: string; secondary_text?: string } | null) {
    setBusy("context");
    try {
      const ctx = await setWebhookContext(meetingKey, { meeting_type_id: meetingTypeId, record: rec });
      setState((s) => (s ? { ...s, context: ctx } : s));
      setError("");
      await load();
    } catch (e) {
      setError(webhookError(e));
    } finally {
      setBusy("");
    }
  }

  function pickType(t: WebhookMeetingType | null) {
    setSheet("");
    if ((t?.id ?? null) === context.meeting_type_id) return;
    // A different type means a different kind of record: never carry one over.
    void saveContext(t ? t.id : null, null);
  }

  function pickRecord(r: WebhookRecord) {
    setSheet("");
    void saveContext(context.meeting_type_id,
                     { id: r.id, display_name: r.display_name, secondary_text: r.secondary_text });
  }

  async function act(kind: "push" | "retry") {
    setBusy(kind);
    try {
      const j = kind === "push" ? await pushWebhookMeeting(meetingKey)
        : await retryWebhookMeeting(meetingKey);
      setState((s) => (s ? { ...s, job: j } : s));
      setError("");
    } catch (e) {
      setError(webhookError(e));
    } finally {
      setBusy("");
    }
  }

  async function refresh() {
    setBusy("refresh");
    try {
      const r = await refreshWebhookConfiguration(workspaceId);
      setState((s) => (s ? { ...s, configuration: r.configuration } : s));
    } catch (e) {
      setError(webhookError(e));
    } finally {
      setBusy("");
    }
  }

  const failed = job?.delivery_status === "failed";

  return (
    <Card>
      <Text style={st.heading}>Salesforce</Text>
      {error ? <ErrorText>{error}</ErrorText> : null}

      <Text style={st.label}>MEETING TYPE</Text>
      <Pressable style={st.field} onPress={() => setSheet("type")} disabled={!!busy}
        accessibilityRole="button" accessibilityLabel="Choose meeting type">
        {context.meeting_type_id ? (
          <>
            <Text style={st.fieldText}>{type?.name || context.meeting_type_name}</Text>
            {!type ? <Text style={st.sub}>No longer offered by Salesforce</Text> : null}
          </>
        ) : <Text style={st.placeholder}>Choose meeting type</Text>}
      </Pressable>

      {type && recordState !== "hidden" ? (
        <>
          <View style={st.labelRow}>
            <Text style={st.label}>{recordLabel(type).toUpperCase()}</Text>
            {record ? (
              <Pressable onPress={() => void saveContext(context.meeting_type_id, null)}
                disabled={!!busy} accessibilityRole="button" hitSlop={8}>
                <Text style={st.clear}>Clear</Text>
              </Pressable>
            ) : recordState === "required" ? <Text style={st.required}>REQUIRED</Text> : null}
          </View>
          <Pressable style={st.field} onPress={() => setSheet("record")} disabled={!!busy}
            accessibilityRole="button" accessibilityLabel={`Choose ${recordLabel(type)}`}>
            {record ? (
              <>
                <Text style={st.fieldText} numberOfLines={2}>{record.title}</Text>
                {record.sub ? <Text style={st.sub}>{record.sub}</Text> : null}
              </>
            ) : (
              <Text style={st.placeholder}>
                {type.record_selection?.placeholder || `Search ${recordLabel(type)}…`}
              </Text>
            )}
          </Pressable>
        </>
      ) : null}

      {failed ? (
        <Button label="Retry" onPress={() => void act("retry")} loading={busy === "retry"}
          disabled={!!busy} />
      ) : (
        <Button label={pushButtonLabel(job)} onPress={() => void act("push")}
          loading={busy === "push"} disabled={!!busy || !!blocked} />
      )}
      {blocked && !failed && !isDeliveryInFlight(job) ? <Text style={st.reason}>{blocked}</Text> : null}

      <Text style={[st.statusTitle, { color: toneColor }]}>
        {line.tone === "ok" ? "✓ " : ""}{line.title}
      </Text>
      {line.detail ? <Text style={st.statusDetail}>{line.detail}</Text> : null}
      {job?.external?.url ? (
        <Pressable onPress={() => { void Linking.openURL(job.external!.url); }}
          accessibilityRole="link">
          <Text style={st.link}>Open in Salesforce</Text>
        </Pressable>
      ) : null}

      <MeetingTypeSheet visible={sheet === "type"} configuration={configuration}
        selectedId={context.meeting_type_id} onPick={pickType} onClose={() => setSheet("")}
        onRefresh={() => { void refresh(); }} refreshing={busy === "refresh"} />
      {type && recordState !== "hidden" ? (
        <RecordSearchSheet visible={sheet === "record"} workspaceId={workspaceId}
          meetingType={type} selectedId={context.record?.id ?? null}
          onPick={pickRecord} onClose={() => setSheet("")} />
      ) : null}
    </Card>
  );
}
