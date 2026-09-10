// lib/meeting-crm-records.tsx — the CRM record associations on Overview.
//
// CONFIGURATION-DRIVEN, with no object hardcoded. This renders one row per
// mapping in the user's Salesforce configuration, whatever those mappings are:
//
//   mappings: []                          -> renders NOTHING
//   [{object: "SiteVisit__c", label: "Site Visit Number"}]
//                                         -> "Site Visit"  [Site Visit Number]
//   [{object: "Lead", ...}, {object: "Opportunity", ...}]
//                                         -> both, in order, each independent
//
// A customer adding an object to their configuration makes it appear here with
// no code change, and there is deliberately no `if (object === ...)` anywhere.
//
// The row's affordances come from ONE piece of state — the entry's `status` —
// so a new Salesforce object needs no new UI branch either:
//
//   not_linked/lookup_pending  enter or fix the identifier
//   ambiguous                  choose among candidates
//   record_found               Confirm & Sync (confirmation gates the push)
//   confirmed                  Sync now
//   syncing                    in-flight, no actions
//   synced                     done, with a re-sync affordance
//   failed                     the reason, and Retry
//
// Confirmation is load-bearing: nothing syncs without an explicit user action,
// because notes written onto the wrong record are worse than no notes.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, Modal, Pressable,
  StyleSheet, Text, TextInput, View,
} from "react-native";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { Button, Card, ErrorText, KeyboardAwareSheet, SectionRule, StatusPill } from "./ui";
import { Icon, type IconName } from "./icons";
import {
  ApiError, CrmSyncJob, MeetingCrmReview, getCrmSyncJobStatus,
  getMeetingCrmReview, isCrmSyncJobTerminal, pushMeetingCrm, retryCrmSyncJob,
  type CrmCandidate, type CrmMapping, type CrmRecordValue, type CrmSyncStatus,
} from "./api";

const VALUE_MAX = 255;

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    row: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md },
    iconWrap: {
      width: 38, height: 38, borderRadius: 13,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    body: { flex: 1 },
    label: { fontFamily: FONT.medium, fontSize: 11.5, color: C.textFaint, letterSpacing: 0.3 },
    value: { fontFamily: FONT.extrabold, fontSize: 18, color: C.text, marginTop: 2 },
    absent: { fontFamily: FONT.semibold, fontSize: 15, color: C.textDim, marginTop: 2 },
    recordLabel: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 3 },
    actionTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary },
    dangerTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.danger },
    badgeRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      gap: 7, marginTop: 8, flexWrap: "wrap" as const,
    },
    evidence: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 19, color: C.textDim,
      fontStyle: "italic" as const, marginTop: 10,
      borderLeftWidth: 2, borderLeftColor: C.border, paddingLeft: 10,
    },
    note: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 19,
      color: C.textFaint, marginTop: 8,
    },
    errorNote: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 19,
      color: C.danger, marginTop: 8,
    },
    actionsRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      gap: S.lg, marginTop: S.md, flexWrap: "wrap" as const,
    },
    syncRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: 8, marginTop: S.md },
    divider: { height: 1, backgroundColor: C.border, marginVertical: S.lg },
    candidate: {
      paddingVertical: 13, borderBottomWidth: 1, borderBottomColor: C.border,
      flexDirection: "row" as const, alignItems: "center" as const, gap: 10,
    },
    candidateName: { flex: 1, fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    candidateId: { fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint, marginTop: 2 },
    backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" as const },
    sheet: {
      backgroundColor: C.surface, paddingHorizontal: 22, paddingTop: 20, paddingBottom: 32,
      borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl, maxHeight: "82%",
    },
    sheetTitle: { fontFamily: FONT.extrabold, fontSize: 20, lineHeight: 26, color: C.text, marginTop: 6 },
    sheetBlurb: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim, marginTop: 8,
    },
    input: {
      backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      fontFamily: FONT.regular, fontSize: 17, color: C.text,
      paddingVertical: 12, paddingHorizontal: 14, marginTop: 18,
    },
  });
}

// One presentation per status. A single table rather than scattered conditions,
// so adding a status is one entry and adding an OBJECT is nothing at all.
function statusChip(status: CrmSyncStatus, C: ColorScale):
    { label: string; color: string } | null {
  switch (status) {
    case "record_found": return { label: "Record found", color: C.accent };
    case "ambiguous": return { label: "Several matches", color: C.warn };
    case "confirmed": return { label: "Confirmed", color: C.primary };
    case "syncing": return { label: "Syncing", color: C.primary };
    case "synced": return { label: "Synced", color: C.success };
    case "failed": return { label: "Sync failed", color: C.danger };
    case "lookup_pending": return { label: "Not matched", color: C.warn };
    default: return null;
  }
}

function statusIcon(status: CrmSyncStatus): IconName {
  switch (status) {
    case "synced": return "checkmark";
    case "failed": return "exclamationmark.triangle";
    case "ambiguous": return "exclamationmark.triangle";
    default: return "number";
  }
}

// How the identifier got here. Manual outranks any extraction, so it is stated
// plainly rather than dressed up as AI output.
function provenance(v: CrmRecordValue): { label: string; tone: "ai" | "manual" } | null {
  if (!v.lookup_value) return null;
  if (v.source === "manual" || v.confidence === "manual") {
    return { label: "You entered this", tone: "manual" };
  }
  if (v.confidence === "explicit") return { label: "AI detected", tone: "ai" };
  if (v.confidence === "probable") return { label: "AI detected · likely", tone: "ai" };
  return null;
}

// The keyboard that suits the configured field, driven by the Salesforce field
// type — so an email lookup gets an email keyboard without this file knowing
// what a Lead is.
function keyboardFor(type?: string): "default" | "email-address" | "numeric" | "phone-pad" | "url" {
  switch ((type || "").toLowerCase()) {
    case "email": return "email-address";
    case "phone": return "phone-pad";
    case "url": return "url";
    case "double": case "int": case "currency": case "percent": return "numeric";
    default: return "default";
  }
}

function CrmRecordRow({
  mapping, current, candidates, onSave, onChoose, onConfirm, onSync, showDivider,
}: {
  mapping: CrmMapping;
  current?: CrmRecordValue | null;
  candidates?: CrmCandidate[];
  onSave: (object: string, value: string | null) => Promise<void>;
  onChoose: (object: string, candidate: CrmCandidate) => Promise<void>;
  onConfirm: (object: string) => Promise<void>;
  onSync: (object: string) => Promise<void>;
  showDivider: boolean;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [showEvidence, setShowEvidence] = useState(false);

  const value = current?.lookup_value ?? "";
  const status: CrmSyncStatus = current?.status ?? "not_linked";
  const hasValue = !!value;
  const prov = current ? provenance(current) : null;
  const chip = statusChip(status, C);
  const hasEvidence = !!current?.evidence;
  // The label comes from the configuration, never from a constant here.
  const fieldLabel = mapping.label || mapping.lookup_field_label || mapping.lookup_field;
  // What a sync will actually write — only the configured targets.
  const targetCount = Object.keys(mapping.content_targets ?? {}).length;

  // Seed the draft when the sheet OPENS rather than in an effect watching the
  // current value: an effect would re-seed on every change (including the one
  // our own save causes) and could wipe what the user is typing.
  const openSheet = () => {
    setDraft(value);
    setSheetOpen(true);
  };

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); } catch { /* the context already alerted */ }
    finally { setBusy(false); }
  };

  const commit = async (next: string | null) => {
    setBusy(true);
    try {
      await onSave(mapping.object, next);
      setSheetOpen(false);
    } catch {
      // Keep the sheet open so the typed value isn't lost and the user can retry.
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {showDivider ? <View style={st.divider} /> : null}
      <View style={st.row}>
        <View style={[st.iconWrap, {
          backgroundColor: status === "synced" ? C.successSoft
            : status === "failed" ? C.dangerSoft
            : hasValue ? C.primarySoft : C.surface2,
        }]}>
          <Icon
            name={statusIcon(status)}
            tintColor={status === "synced" ? C.success
              : status === "failed" ? C.danger
              : hasValue ? C.primary : C.textFaint}
            size={18}
          />
        </View>
        <View style={st.body}>
          <Text style={st.label}>
            {(mapping.object_label || mapping.object).toUpperCase()}
          </Text>
          {hasValue ? (
            <Text style={st.value} numberOfLines={2}>{value}</Text>
          ) : (
            <Text style={st.absent}>Not identified</Text>
          )}
          {current?.record_label && current.record_label !== value ? (
            <Text style={st.recordLabel}>{current.record_label}</Text>
          ) : null}
        </View>
      </View>

      {chip || prov ? (
        <View style={st.badgeRow}>
          {chip ? <StatusPill label={chip.label} color={chip.color} /> : null}
          {prov ? (
            <StatusPill
              label={prov.label}
              color={prov.tone === "manual" ? C.success : C.accent}
            />
          ) : null}
          {hasEvidence ? (
            <Pressable onPress={() => setShowEvidence((v) => !v)} hitSlop={6}>
              <Text style={st.actionTxt}>{showEvidence ? "Hide" : "Why this?"}</Text>
            </Pressable>
          ) : null}
        </View>
      ) : null}

      {/* The transcript sentence the value came from — the audit trail that
          makes a wrong extraction catchable BEFORE anything is pushed. */}
      {hasValue && showEvidence && hasEvidence ? (
        <Text style={st.evidence}>“{current?.evidence}”</Text>
      ) : null}

      {status === "lookup_pending" && hasValue ? (
        <Text style={st.note}>
          No matching {mapping.object_label || mapping.object} in Salesforce.
          Check the {fieldLabel.toLowerCase()} and try again.
        </Text>
      ) : null}
      {status === "failed" && current?.error ? (
        <Text style={st.errorNote}>{current.error}</Text>
      ) : null}
      {!hasValue ? (
        <Text style={st.note}>
          No {fieldLabel.toLowerCase()} was mentioned in this meeting.
        </Text>
      ) : null}
      {status === "synced" && targetCount === 0 ? (
        <Text style={st.note}>
          Linked, but no content fields are configured for this record.
        </Text>
      ) : null}

      {status === "ambiguous" && candidates?.length ? (
        <View style={{ marginTop: S.md }}>
          <Text style={st.note}>
            {candidates.length} records share that {fieldLabel.toLowerCase()}.
            Pick the right one:
          </Text>
          {candidates.map((c) => (
            <Pressable
              key={c.record_id}
              style={st.candidate}
              disabled={busy}
              onPress={() => run(() => onChoose(mapping.object, c))}
            >
              <View style={{ flex: 1 }}>
                <Text style={st.candidateName}>{c.display_name}</Text>
                <Text style={st.candidateId}>{c.record_id}</Text>
              </View>
              <Icon name="chevron.right" tintColor={C.textFaint} size={14} />
            </Pressable>
          ))}
        </View>
      ) : null}

      {status === "syncing" ? (
        <View style={st.syncRow}>
          <ActivityIndicator size="small" color={C.primary} />
          <Text style={st.note}>Writing to Salesforce…</Text>
        </View>
      ) : null}

      {/* Confirm & Sync is the gate: it appears only once a record is resolved,
          and a push never happens without it. */}
      {status === "record_found" ? (
        <Button
          label={targetCount
            ? `Confirm & sync ${targetCount} field${targetCount === 1 ? "" : "s"}`
            : "Confirm record"}
          loading={busy}
          disabled={busy}
          onPress={() => run(async () => {
            await onConfirm(mapping.object);
            if (targetCount) await onSync(mapping.object);
          })}
          style={{ marginTop: S.md }}
        />
      ) : null}
      {status === "confirmed" && targetCount ? (
        <Button
          label="Sync to Salesforce"
          loading={busy}
          disabled={busy}
          onPress={() => run(() => onSync(mapping.object))}
          style={{ marginTop: S.md }}
        />
      ) : null}
      {status === "failed" ? (
        <Button
          label="Retry sync"
          variant="ghost"
          loading={busy}
          disabled={busy}
          onPress={() => run(() => onSync(mapping.object))}
          style={{ marginTop: S.md }}
        />
      ) : null}

      <View style={st.actionsRow}>
        <Pressable onPress={openSheet} hitSlop={6} disabled={busy || status === "syncing"}>
          <Text style={[st.actionTxt, (busy || status === "syncing") && { opacity: 0.5 }]}>
            {hasValue ? "Change" : `Enter ${fieldLabel.toLowerCase()}`}
          </Text>
        </Pressable>
        {status === "synced" && targetCount ? (
          <Pressable
            onPress={() => run(() => onSync(mapping.object))}
            hitSlop={6}
            disabled={busy}
          >
            <Text style={[st.actionTxt, busy && { opacity: 0.5 }]}>Sync again</Text>
          </Pressable>
        ) : null}
      </View>

      <Modal
        visible={sheetOpen}
        transparent
        animationType="slide"
        onRequestClose={() => setSheetOpen(false)}
      >
        <KeyboardAwareSheet>
          <Pressable style={st.backdrop} onPress={() => setSheetOpen(false)}>
            <Pressable style={st.sheet} onPress={() => {}}>
              <Text style={st.sheetTitle}>{fieldLabel}</Text>
              <Text style={st.sheetBlurb}>
                This is what links the meeting to the right{" "}
                {mapping.object_label || mapping.object} record in Salesforce.
                We&apos;ll look it up and show you what we find before anything
                is written.
              </Text>
              <TextInput
                style={st.input}
                value={draft}
                onChangeText={setDraft}
                placeholder={fieldLabel}
                placeholderTextColor={C.textFaint}
                autoCapitalize={
                  keyboardFor(mapping.lookup_field_type) === "email-address"
                    ? "none" : "characters"
                }
                autoCorrect={false}
                keyboardType={keyboardFor(mapping.lookup_field_type)}
                maxLength={VALUE_MAX}
                returnKeyType="done"
                onSubmitEditing={() => commit(draft.trim() || null)}
                autoFocus
              />
              <Button
                label={busy ? "Checking Salesforce…" : "Find record"}
                loading={busy}
                disabled={busy || !draft.trim() || draft.trim() === value}
                onPress={() => commit(draft.trim() || null)}
                style={{ marginTop: 18 }}
              />
              {/* Clearing must stay reachable: it is how a wrong AI extraction
                  gets undone. Only offered when there is something to clear. */}
              {hasValue ? (
                <Button
                  label="Remove this link"
                  variant="ghost"
                  disabled={busy}
                  onPress={() => commit(null)}
                  style={{ marginTop: S.sm }}
                />
              ) : null}
            </Pressable>
          </Pressable>
        </KeyboardAwareSheet>
      </Modal>
    </>
  );
}

export function CrmRecordsBlock({
  mappings, records, ambiguity, onSave, onChoose, onConfirm, onSync,
}: {
  // The user's configured mappings. EMPTY (or absent) renders nothing at all —
  // that is the "no Salesforce fields unless configured" behaviour, expressed
  // as data rather than a condition per object type.
  mappings?: CrmMapping[] | null;
  records?: Record<string, CrmRecordValue> | null;
  ambiguity?: Record<string, CrmCandidate[]> | null;
  onSave: (object: string, value: string | null) => Promise<void>;
  onChoose: (object: string, candidate: CrmCandidate) => Promise<void>;
  onConfirm: (object: string) => Promise<void>;
  onSync: (object: string) => Promise<void>;
}) {
  const { C } = useTheme();

  if (!mappings || mappings.length === 0) return null;

  return (
    <View>
      <SectionRule right={<Icon name="cloud.fill" tintColor={C.textFaint} size={15} />}>
        Salesforce
      </SectionRule>
      <Card>
        {mappings.map((m, i) => (
          <CrmRecordRow
            key={m.object}
            mapping={m}
            current={records?.[m.object]}
            // Server-stored candidates (from a persisted ambiguous state) or
            // the ones this session's lookup just returned.
            candidates={ambiguity?.[m.object] ?? records?.[m.object]?.candidates}
            onSave={onSave}
            onChoose={onChoose}
            onConfirm={onConfirm}
            onSync={onSync}
            showDivider={i > 0}
          />
        ))}
      </Card>
    </View>
  );
}

// ---------------------------------------------------------------------------
// ORGANISATION CRM REVIEW (Phase 2D.3) — the identity-resolution summary and
// the push gate. A SEPARATE block from CrmRecordsBlock above, deliberately:
// that one is the per-object field-mapping UI shared with Personal
// Salesforce (untouched by this phase); this one is organisation-only and
// concerns WHO the meeting's speakers resolve to, not which record fields
// get written. Both can render on the same Overview screen — this one
// ABOVE crm-records, since knowing who the meeting was with comes before
// deciding what to write about it.
// ---------------------------------------------------------------------------

function speakerLine(label: string, state: {
  identity_role?: string; status: string; sf_username?: string; contact_name?: string;
} | null | undefined, C: ColorScale) {
  if (!state) {
    return { icon: "questionmark.circle" as IconName, color: C.textFaint,
             text: `${label}: not tagged` };
  }
  if (state.status === "resolved") {
    const who = state.identity_role === "internal"
      ? state.sf_username : state.contact_name;
    return { icon: "checkmark.circle.fill" as IconName, color: C.success,
             text: `${label}: ${who || "resolved"}` };
  }
  return { icon: "exclamationmark.triangle.fill" as IconName, color: C.warn,
           text: `${label}: unresolved` };
}

function buildReviewStyles(C: ColorScale) {
  return StyleSheet.create({
    row: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
      paddingVertical: 8,
    },
    rowTxt: { fontFamily: FONT.medium, fontSize: 13.5, color: C.text, flex: 1 },
    blurb: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim,
      lineHeight: 18, marginTop: 2, marginBottom: 10,
    },
    divider: { height: 1, backgroundColor: C.border, marginVertical: S.sm },
    resultRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 8,
      paddingVertical: 6,
    },
    resultTxt: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, flex: 1 },
    actionsRow: {
      flexDirection: "row" as const, gap: S.sm, marginTop: S.md, flexWrap: "wrap" as const,
    },
  });
}

// How often to poll while a job is non-terminal (Phase 2D.4). The push
// itself answers in well under a second (it only writes a DynamoDB row and
// sends one SQS message); the ACTUAL Salesforce work happens in the worker
// on its own schedule, so the app has no way to know completion except by
// asking again. 3s balances feeling responsive against hammering the API —
// slower than a chat app's typing indicator, faster than a human would
// notice as sluggish for something they just explicitly asked to run.
const CRM_JOB_POLL_INTERVAL_MS = 3000;

// The status line shown while a job is in flight — spec section 21's
// "queued... syncing... synced/failed/reconnect required" progression.
function jobStatusLabel(status: CrmSyncJob["status"] | "queuing"): string {
  switch (status) {
    case "queuing": return "CRM sync queued";
    case "PENDING": return "CRM sync queued";
    case "SYNCING": return "CRM syncing…";
    case "RETRYING": return "CRM sync retrying…";
    case "SYNCED": return "CRM synced";
    case "FAILED": return "CRM sync failed";
    case "RECONNECT_REQUIRED": return "Reconnect Salesforce required";
    default: return "";
  }
}

/** The Organisation CRM Review + Push block, rendered on the Overview screen
 *  for an organisation meeting only. Fetches its own state (get_meeting_crm_
 *  review, plus the CRM sync job for this meeting) rather than threading it
 *  through meeting-context — this is a narrow, self-contained concern that
 *  only this block needs, and the existing context already carries enough
 *  for the Personal path. */
export function OrgCrmReviewBlock({ meetingKey }: { meetingKey: string }) {
  const { C } = useTheme();
  const st = useMemo(() => buildReviewStyles(C), [C]);
  const [review, setReview] = useState<MeetingCrmReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pushing, setPushing] = useState(false);
  // "queuing" is a LOCAL transient state for the moment between tapping
  // Push and the enqueue response landing — never confused with the
  // server's own PENDING (which persists until the worker actually picks
  // the job up), but rendered identically ("CRM sync queued") since the
  // difference is not meaningful to a user watching this screen.
  const [job, setJob] = useState<CrmSyncJob | "queuing" | null>(null);
  const [retrying, setRetrying] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    setError("");
    try {
      setReview(await getMeetingCrmReview(meetingKey));
    } catch (e) {
      // A 400 here (not connected / not configured) is an ORDINARY state for
      // most organisation meetings, not a failure worth alarming over — the
      // blocking_reasons on a successful response already explain it. Only a
      // genuinely unexpected error gets its own message.
      if (!(e instanceof ApiError && e.status === 400)) {
        setError(e instanceof ApiError ? e.message : "Could not load CRM review.");
      }
      setReview(null);
    } finally {
      setLoading(false);
    }
  }, [meetingKey]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const pollOnce = useCallback(async () => {
    try {
      const latest = await getCrmSyncJobStatus(meetingKey);
      if (!latest) { stopPolling(); return; }
      setJob(latest);
      if (isCrmSyncJobTerminal(latest.status)) {
        stopPolling();
        await load(); // a push can change record states the review reads
      }
    } catch {
      // A transient poll failure must not stop the block from rendering
      // whatever it last knew — the NEXT tick tries again, and the
      // interval itself is the retry.
    }
  }, [meetingKey, stopPolling, load]);

  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(() => { void pollOnce(); }, CRM_JOB_POLL_INTERVAL_MS);
  }, [stopPolling, pollOnce]);

  // On mount: load the review AND check whether a job from an earlier visit
  // (or another device) is still in flight — the UI must not assume
  // "no local push state" means "nothing is happening", since the worker
  // runs independently of this screen being open.
  useEffect(() => {
    void load();
    (async () => {
      try {
        const existing = await getCrmSyncJobStatus(meetingKey);
        if (existing) {
          setJob(existing);
          if (!isCrmSyncJobTerminal(existing.status)) startPolling();
        }
      } catch {
        // No prior job, or a transient read failure — either way the Push
        // button itself is the recovery path, so this stays silent.
      }
    })();
    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingKey]);

  const push = async () => {
    setPushing(true);
    setError("");
    setJob("queuing");
    try {
      await pushMeetingCrm(meetingKey);
      // Read back the real row rather than trusting the enqueue response's
      // bare {job_id, status} shape — the status route is the one source of
      // job truth this component polls from here on.
      const fresh = await getCrmSyncJobStatus(meetingKey);
      setJob(fresh ?? "queuing");
      if (!fresh || !isCrmSyncJobTerminal(fresh.status)) startPolling();
    } catch (e) {
      setJob(null);
      setError(e instanceof ApiError ? e.message : "Could not queue the CRM push.");
    } finally {
      setPushing(false);
    }
  };

  const retry = async () => {
    setRetrying(true);
    setError("");
    try {
      await retryCrmSyncJob(meetingKey);
      const fresh = await getCrmSyncJobStatus(meetingKey);
      setJob(fresh);
      if (!fresh || !isCrmSyncJobTerminal(fresh.status)) startPolling();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not retry the CRM push.");
    } finally {
      setRetrying(false);
    }
  };

  if (loading) {
    return (
      <View>
        <SectionRule right={<Icon name="cloud.fill" tintColor={C.textFaint} size={15} />}>
          CRM Review
        </SectionRule>
        <Card><ActivityIndicator color={C.textFaint} /></Card>
      </View>
    );
  }

  // No review at all (not connected / not configured) — say so briefly and
  // point at the fix, rather than rendering an empty-looking block.
  if (!review) {
    return (
      <View>
        <SectionRule right={<Icon name="cloud.fill" tintColor={C.textFaint} size={15} />}>
          CRM Review
        </SectionRule>
        <Card>
          <Text style={st.blurb}>
            Organisation Salesforce isn&apos;t connected or configured yet.
            An owner or manager can set it up from Organisation → Integrations.
          </Text>
        </Card>
      </View>
    );
  }

  const { identity, content, ready, blocking_reasons } = review;

  return (
    <View>
      <SectionRule right={<Icon name="cloud.fill" tintColor={C.textFaint} size={15} />}>
        CRM Review
      </SectionRule>
      <Card>
        {error ? <ErrorText>{error}</ErrorText> : null}

        {(() => {
          const owner = speakerLine("SM", identity.owner, C);
          return (
            <View style={st.row}>
              <Icon name={owner.icon} tintColor={owner.color} size={16} />
              <Text style={st.rowTxt}>{owner.text}</Text>
            </View>
          );
        })()}
        {(() => {
          const client = speakerLine("Client", identity.primary_client, C);
          return (
            <View style={st.row}>
              <Icon name={client.icon} tintColor={client.color} size={16} />
              <Text style={st.rowTxt}>{client.text}</Text>
            </View>
          );
        })()}
        {identity.unresolved.map((s) => (
          <View key={s.speaker_id} style={st.row}>
            <Icon name="exclamationmark.triangle.fill" tintColor={C.warn} size={16} />
            <Text style={st.rowTxt}>
              Speaker {s.speaker_id}: {s.reason || "unresolved"}
            </Text>
          </View>
        ))}

        <View style={st.divider} />

        <View style={st.row}>
          <Icon
            name={content.summary_ready ? "checkmark.circle.fill" : "info.circle"}
            tintColor={content.summary_ready ? C.success : C.textFaint}
            size={16}
          />
          <Text style={st.rowTxt}>Summary {content.summary_ready ? "ready" : "not ready"}</Text>
        </View>
        <View style={st.row}>
          <Icon
            name={content.action_items_ready ? "checkmark.circle.fill" : "info.circle"}
            tintColor={content.action_items_ready ? C.success : C.textFaint}
            size={16}
          />
          <Text style={st.rowTxt}>
            Action items {content.action_items_ready ? `· ${content.action_item_count} ready` : "none"}
          </Text>
        </View>

        {!ready && blocking_reasons.length ? (
          <Text style={st.blurb}>
            {blocking_reasons.includes("unresolved_speaker_identity")
              ? "Resolve every tagged speaker's Salesforce identity before pushing."
              : blocking_reasons.includes("no_salesforce_mapping_configured")
                ? "No Salesforce mapping is configured for this organisation yet."
                : "Organisation Salesforce isn't connected."}
          </Text>
        ) : null}

        {job ? (
          <>
            <View style={st.divider} />
            <View style={st.resultRow}>
              {job === "queuing" || job.status === "PENDING" || job.status === "SYNCING"
                || job.status === "RETRYING" ? (
                <ActivityIndicator size="small" color={C.primary} />
              ) : (
                <Icon
                  name={job.status === "SYNCED" ? "checkmark"
                    : "exclamationmark.triangle"}
                  tintColor={job.status === "SYNCED" ? C.success : C.danger}
                  size={13}
                />
              )}
              <Text style={st.resultTxt}>
                {jobStatusLabel(job === "queuing" ? "queuing" : job.status)}
              </Text>
            </View>

            {/* Per-operation detail — only once the worker has actually run
                (job.result is populated from SYNCING onward). The UI must
                not assume a successful ENQUEUE means Salesforce received
                anything (spec section 21) — this section is exactly the
                proof point that distinguishes "queued" from "done". */}
            {job !== "queuing" && job.result?.pushed?.map((p) => (
              <View key={p.object} style={st.resultRow}>
                <Icon name="checkmark" tintColor={C.success} size={13} />
                <Text style={st.resultTxt}>{p.object} synced</Text>
              </View>
            ))}
            {job !== "queuing" && job.result?.push_errors?.map((e) => (
              <View key={e.object} style={st.resultRow}>
                <Icon name="exclamationmark.triangle" tintColor={C.danger} size={13} />
                <Text style={st.resultTxt}>{e.object}: {e.error}</Text>
              </View>
            ))}
            {job !== "queuing" && job.result?.tasks?.created?.length ? (
              <View style={st.resultRow}>
                <Icon name="checkmark" tintColor={C.success} size={13} />
                <Text style={st.resultTxt}>
                  {job.result.tasks.created.length} Salesforce Task
                  {job.result.tasks.created.length === 1 ? "" : "s"} created
                </Text>
              </View>
            ) : null}
            {job !== "queuing" && job.result?.tasks?.failed?.length ? (
              <View style={st.resultRow}>
                <Icon name="exclamationmark.triangle" tintColor={C.danger} size={13} />
                <Text style={st.resultTxt}>
                  {job.result.tasks.failed.length} task{job.result.tasks.failed.length === 1 ? "" : "s"} failed
                </Text>
              </View>
            ) : null}
            {job !== "queuing" && job.result?.event?.created ? (
              <View style={st.resultRow}>
                <Icon name="checkmark" tintColor={C.success} size={13} />
                <Text style={st.resultTxt}>Meeting Event created in Salesforce</Text>
              </View>
            ) : job !== "queuing" && job.result?.event?.error ? (
              <View style={st.resultRow}>
                <Icon name="exclamationmark.triangle" tintColor={C.danger} size={13} />
                <Text style={st.resultTxt}>Event: {job.result.event.error}</Text>
              </View>
            ) : null}

            {/* The job's OWN failure reason — distinct from per-operation
                detail above, and what actually decides which action button
                renders below (spec section 14: not a generic Retry for
                every failure). */}
            {job !== "queuing" && job.status === "FAILED" && job.last_error_message ? (
              <Text style={st.blurb}>Reason: {job.last_error_message}</Text>
            ) : null}
            {job !== "queuing" && job.status === "RECONNECT_REQUIRED" ? (
              <Text style={st.blurb}>
                This organisation&apos;s Salesforce connection has expired. Reconnect it from
                Organisation → Integrations, then retry.
              </Text>
            ) : null}
          </>
        ) : null}

        <View style={st.actionsRow}>
          {job !== "queuing" && job?.status === "RECONNECT_REQUIRED" ? (
            // No generic Retry for a dead credential — the actual fix is
            // reconnecting, not trying the same request again (spec
            // section 14). Once reconnected, the SAME job can be retried;
            // this screen does not itself navigate to Integrations (no
            // existing cross-tab navigation convention for this file to
            // reuse), so it names the fix rather than offering a dead-end
            // button.
            <Text style={st.blurb}>
              Reconnect Salesforce for this organisation to continue.
            </Text>
          ) : job !== "queuing" && job?.status === "FAILED"
              && job.last_error_category === "permanent" ? (
            // A permanent failure (bad config, missing identity, deleted
            // record) will fail again identically on retry — the fix is
            // reviewing configuration/identity, not repeating the request.
            <Text style={st.blurb}>
              Review the CRM configuration or speaker identity above, then push again.
            </Text>
          ) : job !== "queuing" && job?.status === "FAILED" ? (
            <Button
              label={retrying ? "Retrying…" : "Retry"}
              loading={retrying}
              disabled={retrying}
              onPress={retry}
            />
          ) : (
            <Button
              label={pushing || job === "queuing" ? "Queuing…" : "Push to Salesforce"}
              loading={pushing}
              disabled={!ready || pushing
                || (job !== null && job !== "queuing" && !isCrmSyncJobTerminal(job.status))}
              onPress={push}
            />
          )}
        </View>
      </Card>
    </View>
  );
}
