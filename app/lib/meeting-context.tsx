// lib/meeting-context.tsx — loads one RecordingDetail once per meeting and
// shares it (plus title/speaker rename actions, the locally-held Documents
// list, Tasks, and the meeting's activity timeline) across every route nested
// under recording/[key]/ — Overview+Transcript, Assistant, Task Detail,
// Assign To and Notify — so they all see the same in-flight state without N
// independent fetches or N copies of the same mutation logic.
//
// MeetingProvider is mounted ONCE, in recording/[key]/_layout.tsx, wrapping a
// nested Stack — not per-screen — precisely so that e.g. assigning a task on
// the Assign To screen and then navigating back to Task Detail sees the
// update immediately, rather than each screen holding its own stale copy.
//
// Tasks' CORE fields (task/due/priority/status/assignee/notifiedVia) are now
// persisted server-side (see lib/api.ts's getTasks/createTask/updateTask/
// deleteTask and lambda-userapi's Tasks section) — every mutator below calls
// the backend FIRST, then applies the same patch to local state so the UI
// updates immediately without waiting for a refetch. Subtasks/attachments/
// notes/activity remain client-side-only local-state mutations, same as
// before, since no backend concept exists for them.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Alert } from "react-native";
import { useRouter } from "expo-router";
import {
  getRecording, updateRecording, clearToken, ApiError, RecordingDetail,
  getTasks, createTask as apiCreateTask, updateTask as apiUpdateTask,
  deleteTask as apiDeleteTask, listAiDocuments, updateStaleDocuments,
  getSalesforceConfig, lookupCrmRecord, syncCrmRecord,
  type CrmMapping, type CrmCandidate, type CrmLookupResult,
} from "./api";
import type { GeneratedDoc } from "./meeting-documents";
import {
  taskFromApiTask, assigneeToApi, nextId, type Task, type TaskStatus,
  type Assignee, type NotifyChannel, type Attachment,
} from "./task-model";

type MeetingCtxValue = {
  key: string;
  rec: RecordingDetail | null;
  loading: boolean;
  error: string;
  reload: () => void;
  documents: GeneratedDoc[];
  addDocument: (doc: GeneratedDoc) => void;
  setDocuments: (docs: GeneratedDoc[]) => void;
  renameMeeting: (title: string) => Promise<void>;
  renameSpeaker: (rawLabel: string, name: string) => Promise<void>;
  /**
   * Add a task the AI did not extract.
   *
   * The AI is deliberately conservative — prompts.SUMMARY_SYSTEM forbids
   * inventing a task or inferring an owner — so it misses real commitments,
   * and on a short or silent recording it finds nothing at all. Without this
   * the meeting owner had no way to record work themselves: createTask existed
   * in lib/api.ts and was imported here, but nothing ever called it.
   *
   * Resolves with the created task so the caller can navigate to it.
   */
  addTask: (input: {
    task: string;
    due?: string;
    priority?: Task["priority"];
    assigneeContactId?: string;
  }) => Promise<Task>;
  // Set or clear the identifier linking this meeting to a record of the given
  // Salesforce OBJECT. Pass null (or "") to clear — that is how a wrong AI
  // extraction gets undone, so it must stay reachable, not just "set".
  //
  // Setting an identifier also RESOLVES it (one lookup) and persists whatever
  // came back, so the UI lands directly on record_found / ambiguous / a
  // not-found message instead of making the user press a second button.
  setCrmRecord: (objectName: string, value: string | null) => Promise<void>;
  // Pick one candidate after an ambiguous lookup, then confirm it.
  chooseCrmCandidate: (objectName: string, candidate: CrmCandidate) => Promise<void>;
  // Approve the resolved record — the gate before anything is written.
  confirmCrmRecord: (objectName: string) => Promise<void>;
  // Push the configured content onto the confirmed record.
  syncCrmRecordNow: (objectName: string) => Promise<void>;
  // The user's Salesforce mappings, or [] when Salesforce isn't connected or
  // has no mappings. The meeting UI renders one input per entry and nothing at
  // all for an empty list, so no object name is hardcoded in the app.
  crmMappings: CrmMapping[];
  // Candidates from an ambiguous lookup, per object. Transient (this session
  // only) — it exists until the user picks one.
  crmAmbiguity: Record<string, CrmCandidate[]>;
  // ---- Document staleness (speaker rename sync) ----
  documentsNeedingUpdate: string[];
  updateAllDocuments: () => Promise<{ updated: number; remaining: number }>;
  // ---- Tasks (client-side; see lib/task-model.ts) ----
  tasks: Task[];
  getTask: (id: string) => Task | undefined;
  updateTask: (id: string, patch: Partial<Task>) => void;
  setTaskStatus: (id: string, status: TaskStatus) => void;
  assignTask: (id: string, assignee: Assignee) => void;
  recordNotification: (id: string, channels: NotifyChannel[]) => void;
  addSubtask: (id: string, title: string) => void;
  toggleSubtask: (id: string, subtaskId: string) => void;
  addAttachment: (id: string, attachment: Attachment) => void;
  removeAttachment: (id: string, attachmentId: string) => void;
  // ---- Activity timeline (meeting-wide; see Assistant / Task Detail) ----
  activity: { id: string; at: string; text: string }[];
  logActivity: (text: string) => void;
};

const MeetingCtx = createContext<MeetingCtxValue | null>(null);

export function useMeeting(): MeetingCtxValue {
  const ctx = useContext(MeetingCtx);
  if (!ctx) throw new Error("useMeeting must be used inside <MeetingProvider>");
  return ctx;
}

export function MeetingProvider({ meetingKey, children }: { meetingKey: string; children: React.ReactNode }) {
  const router = useRouter();
  const [rec, setRec] = useState<RecordingDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [documents, setDocuments] = useState<GeneratedDoc[]>([]);
  const [documentsNeedingUpdate, setDocumentsNeedingUpdate] = useState<string[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  // True once tasks have been fetched for this meeting, so load() re-running
  // (it is a useCallback dep of an effect, and reload() is user-triggerable)
  // refetches the meeting without stomping client-only task fields. A ref,
  // not state: reading it must not re-run load() or re-render.
  const tasksLoadedRef = useRef(false);
  // The meetingKey the guard above belongs to, so a key change refetches.
  const tasksKeyRef = useRef("");
  const [activity, setActivity] = useState<{ id: string; at: string; text: string }[]>([]);
  // [] until proven otherwise: a user with no Salesforce connection, no
  // mappings, or an unreachable config all render the same — no record inputs.
  const [crmMappings, setCrmMappings] = useState<CrmMapping[]>([]);
  // Ambiguous-lookup candidates, per object. Transient UI state (not stored
  // server-side): it exists only until the user picks one.
  const [crmAmbiguity, setCrmAmbiguity] =
    useState<Record<string, CrmCandidate[]>>({});

  const logActivity = useCallback((text: string) => {
    setActivity((prev) => [{ id: nextId("meeting-act"), at: new Date().toISOString(), text }, ...prev]);
  }, []);

  // getRecording's embedded `documents` map is the raw stored shape — it
  // has no computed `status`/`speaker_mapping_version` (only list_documents/
  // generate_document run each document through _public_document to compute
  // that). So documents' staleness is fetched separately here, the same
  // "seed from getRecording, then follow up with a dedicated call" pattern
  // already used below for tasks.
  const refreshDocumentStatus = useCallback(async () => {
    try {
      const res = await listAiDocuments(meetingKey);
      setDocuments(Object.values(res.documents));
      setDocumentsNeedingUpdate(res.documentsNeedingUpdate);
    } catch {
      // Best-effort: the Documents list still works from getRecording's
      // copy, just without the "needs update" indicator until next load.
    }
  }, [meetingKey]);

  const load = useCallback(async () => {
    if (!meetingKey) { setError("Missing recording id."); setLoading(false); return; }
    // A different meeting is a different task list — clear the once-only
    // guard (and the previous meeting's tasks) so it fetches again. Normally
    // the provider remounts per meeting and this is a no-op, but meetingKey
    // can also change under a mounted provider.
    if (tasksKeyRef.current !== meetingKey) {
      tasksKeyRef.current = meetingKey;
      tasksLoadedRef.current = false;
      setTasks([]);
    }
    setError(""); setLoading(true);
    try {
      const data = await getRecording(meetingKey);
      setRec(data);
      setDocuments(Object.values(data.documents ?? {}));
      refreshDocumentStatus();
      // Which CRM record inputs to show, if any. Best-effort and deliberately
      // not awaited into the critical path: a 400 (Salesforce not connected) is
      // the ordinary case for most users and must leave the meeting screen
      // exactly as it was before this feature existed.
      getSalesforceConfig()
        .then((cfg) => setCrmMappings(cfg.mappings ?? []))
        .catch(() => setCrmMappings([]));
      // Tasks are fetched from the backend (getTasks seeds from ai_tasks
      // server-side on first call, once, so every device sees the same
      // ids — see lambda-userapi's _seed_tasks_from_action_items).
      //
      // The fetch is fired HERE, from the effect body, and NOT from inside a
      // setTasks updater. A state updater must be a pure prev -> next
      // function: React may call it twice (StrictMode) or skip it entirely,
      // and with `reactCompiler` enabled (app.json experiments) an updater
      // that just returns `prev` is free to be optimized away — which meant
      // getTasks() never ran and the list stayed empty forever.
      //
      // "Only fetch once" is still honoured, via the tasksLoadedRef guard
      // rather than by reading `prev` — a 15s polling refresh must not stomp
      // client-only fields (notes/subtasks/attachments) the user has since
      // added on top of a task object that already exists locally.
      const title = data.title || "Meeting";
      if (!tasksLoadedRef.current) {
        tasksLoadedRef.current = true;
        try {
          const apiTasks = await getTasks(meetingKey);
          setTasks(apiTasks.map((t) => taskFromApiTask(t, title)));
        } catch {
          // Let a failed fetch be retryable on the next load() rather than
          // latching the meeting into a permanently empty task list.
          tasksLoadedRef.current = false;
        }
      }
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
        await clearToken(); router.replace("/login"); return;
      }
      if (e instanceof ApiError && e.status === 404) setError("This meeting was not found.");
      else setError(e instanceof ApiError ? e.message : "Could not load this meeting.");
    } finally {
      setLoading(false);
    }
  }, [meetingKey, router]);

  useEffect(() => { load(); }, [load]);

  const processing = !!rec && (
    rec.status === "uploading" || rec.status === "uploaded" ||
    rec.status === "transcribing" || rec.status === "generating_ai"
  );
  useEffect(() => {
    if (!processing) return;
    const t = setInterval(async () => {
      try {
        const data = await getRecording(meetingKey);
        setRec(data);
      } catch { /* keep the last good state */ }
    }, 15_000);
    return () => clearInterval(t);
  }, [processing, meetingKey]);

  const renameMeeting = useCallback(async (title: string) => {
    const trimmed = title.trim();
    if (!rec || !trimmed || trimmed === rec.title) return;
    try {
      const updated = await updateRecording(meetingKey, { title: trimmed });
      setRec(updated);
      logActivity(`Meeting renamed to "${trimmed}"`);
    } catch (e) {
      Alert.alert("Couldn't rename", e instanceof ApiError ? e.message : "Something went wrong.");
      throw e;
    }
  }, [rec, meetingKey, logActivity]);

  // The object's own label, from the CONFIGURATION — so every message below
  // reads naturally for any object without naming one here.
  const crmLabel = useCallback((objectName: string) =>
    crmMappings.find((m) => m.object === objectName)?.object_label || objectName,
    [crmMappings]);

  const setCrmRecord = useCallback(async (objectName: string, value: string | null) => {
    if (!rec || !objectName) return;
    const trimmed = (value ?? "").trim();
    const label = crmLabel(objectName);
    try {
      if (!trimmed) {
        // "" and null both clear it server-side; send null explicitly so the
        // intent reads the same on the wire as it does here.
        setRec(await updateRecording(meetingKey, {
          crm_records: { [objectName]: null },
        }));
        logActivity(`${label} cleared`);
        return;
      }

      // Resolve first, then persist identifier AND outcome together, so the
      // stored status always matches what Salesforce actually said.
      let result: CrmLookupResult | null = null;
      try {
        result = await lookupCrmRecord(objectName, trimmed);
      } catch (e) {
        // A lookup failure must not lose the identifier the user typed — store
        // it as lookup_pending so they can retry without retyping.
        setRec(await updateRecording(meetingKey, {
          crm_records: { [objectName]: trimmed },
        }));
        Alert.alert(`Couldn't search Salesforce`,
                    e instanceof ApiError ? e.message : "Something went wrong.");
        return;
      }

      const patch = result.status === "found"
        ? { lookup_value: trimmed, record_id: result.record_id || "",
            record_label: result.record_label || "", status: "record_found" as const }
        // ambiguous / not_found both persist just the identifier: there is no
        // record to associate yet, and the UI reads the outcome from `result`.
        : { lookup_value: trimmed };
      setRec(await updateRecording(meetingKey, {
        crm_records: { [objectName]: patch },
      }));

      if (result.status === "found") {
        logActivity(`${label} ${trimmed} matched a Salesforce record`);
      } else if (result.status === "ambiguous") {
        setCrmAmbiguity((prev) => ({ ...prev, [objectName]: result.records ?? [] }));
        logActivity(`${label} ${trimmed} matched several records`);
      } else {
        setCrmAmbiguity((prev) => {
          const next = { ...prev }; delete next[objectName]; return next;
        });
        logActivity(`${label} ${trimmed} matched no Salesforce record`);
      }
    } catch (e) {
      Alert.alert(`Couldn't save the ${label} reference`,
                  e instanceof ApiError ? e.message : "Something went wrong.");
      throw e;
    }
  }, [rec, meetingKey, logActivity, crmLabel]);

  const chooseCrmCandidate = useCallback(async (objectName: string,
                                                candidate: CrmCandidate) => {
    if (!rec) return;
    const current = rec.crm_records?.[objectName];
    const value = current?.lookup_value ?? "";
    if (!value || !candidate.record_id) return;
    try {
      setRec(await updateRecording(meetingKey, {
        crm_records: {
          [objectName]: {
            lookup_value: value,
            record_id: candidate.record_id,
            record_label: candidate.display_name,
            // Choosing among candidates IS the user identifying the record, so
            // it lands confirmed — a second "confirm" tap would be theatre.
            status: "confirmed",
          },
        },
      }));
      setCrmAmbiguity((prev) => {
        const next = { ...prev }; delete next[objectName]; return next;
      });
      logActivity(`${crmLabel(objectName)} linked to ${candidate.display_name}`);
    } catch (e) {
      Alert.alert("Couldn't link that record",
                  e instanceof ApiError ? e.message : "Something went wrong.");
      throw e;
    }
  }, [rec, meetingKey, logActivity, crmLabel]);

  const confirmCrmRecord = useCallback(async (objectName: string) => {
    if (!rec) return;
    const current = rec.crm_records?.[objectName];
    if (!current?.lookup_value || !current.record_id) return;
    try {
      setRec(await updateRecording(meetingKey, {
        crm_records: {
          [objectName]: {
            lookup_value: current.lookup_value,
            record_id: current.record_id,
            record_label: current.record_label || "",
            status: "confirmed",
          },
        },
      }));
      logActivity(`${crmLabel(objectName)} record confirmed`);
    } catch (e) {
      Alert.alert("Couldn't confirm that record",
                  e instanceof ApiError ? e.message : "Something went wrong.");
      throw e;
    }
  }, [rec, meetingKey, logActivity, crmLabel]);

  const syncCrmRecordNow = useCallback(async (objectName: string) => {
    if (!rec) return;
    const label = crmLabel(objectName);
    try {
      const res = await syncCrmRecord(meetingKey, objectName);
      // Adopt the returned record rather than refetching the whole meeting: the
      // sync route returns the authoritative entry for this object.
      setRec((prev) => prev ? {
        ...prev,
        crm_records: { ...(prev.crm_records ?? {}), [objectName]: res.crm_record },
      } : prev);
      logActivity(`${label} synced to Salesforce`
        + (res.synced_fields?.length ? ` (${res.synced_fields.length} fields)` : ""));
    } catch (e) {
      // The backend has already marked the entry failed and stored the reason;
      // reload so the UI shows that state, then surface the message.
      load();
      Alert.alert(`Couldn't sync ${label}`,
                  e instanceof ApiError ? e.message : "Something went wrong.");
      throw e;
    }
  }, [rec, meetingKey, logActivity, crmLabel, load]);

  const renameSpeaker = useCallback(async (rawLabel: string, name: string) => {
    if (!rec) return;
    const next = { ...(rec.speaker_names ?? {}) };
    const trimmed = name.trim();
    if (trimmed) next[rawLabel] = trimmed; else delete next[rawLabel];
    try {
      const updated = await updateRecording(meetingKey, { speaker_names: next });
      setRec(updated);
      // Transcript/Participants read speaker_names live and update on their
      // own re-render — no fetch needed. Generated documents don't (their
      // content is baked-in prose), so re-check which ones the backend now
      // considers stale under the bumped speaker_mapping_version.
      refreshDocumentStatus();
    } catch (e) {
      Alert.alert("Couldn't save the name", e instanceof ApiError ? e.message : "Something went wrong.");
      throw e;
    }
  }, [rec, meetingKey, refreshDocumentStatus]);

  const addDocument = useCallback((doc: GeneratedDoc) => {
    setDocuments((prev) => [doc, ...prev]);
    logActivity(`Document generated: ${doc.label}`);
  }, [logActivity]);

  // "Update All": regenerate every currently-flagged-stale document against
  // the CURRENT speaker names. The backend runs this against a shared
  // deadline and may not finish everything in one call — `remaining` says
  // how many are left, so the UI can offer to tap again.
  const updateAllDocuments = useCallback(async () => {
    const res = await updateStaleDocuments(meetingKey);
    setDocuments((prev) => {
      const byType = new Map(res.updated.map((u) => [u.type, u.document]));
      const next = prev.map((d) => byType.get(d.type) ?? d);
      // Defensive: every updated type should already be in `prev` (Update
      // All only touches already-generated documents), but append anything
      // that somehow isn't rather than silently dropping it.
      for (const u of res.updated) {
        if (!next.some((d) => d.type === u.type)) next.push(u.document);
      }
      return next;
    });
    setDocumentsNeedingUpdate(res.remaining);
    return { updated: res.updated.length, remaining: res.remaining.length };
  }, [meetingKey]);

  // ---- Task mutators ----
  // updateTask/setTaskStatus/assignTask/recordNotification touch CORE fields
  // that are now persisted server-side: each applies the change locally
  // first (so the UI responds immediately) then calls the backend, rolling
  // the local state back and surfacing an alert if the write fails — same
  // pattern renameMeeting/renameSpeaker already use above. addSubtask/
  // toggleSubtask/addAttachment/removeAttachment stay pure local-state
  // mutations (no backend concept exists for them — see task-model.ts).
  const getTask = useCallback((id: string) => tasks.find((t) => t.id === id), [tasks]);

  const pushTaskActivity = useCallback((id: string, text: string) => {
    setTasks((prev) => prev.map((t) => (
      t.id === id ? { ...t, activity: [{ id: nextId("act"), at: new Date().toISOString(), text }, ...t.activity] } : t
    )));
  }, []);

  // Snapshot-and-restore helper: apply `patch` locally now, call the backend,
  // and on failure put the pre-patch task back exactly as it was — a failed
  // PATCH must never leave the UI showing a change that didn't actually save.
  const applyTaskPatch = useCallback(async (
    id: string,
    patch: Partial<Task>,
    toApi: () => Promise<unknown>,
    failureTitle: string
  ) => {
    let previous: Task | undefined;
    setTasks((prev) => prev.map((t) => {
      if (t.id !== id) return t;
      previous = t;
      return { ...t, ...patch };
    }));
    try {
      await toApi();
    } catch (e) {
      if (previous) setTasks((prev) => prev.map((t) => (t.id === id ? previous! : t)));
      Alert.alert(failureTitle, e instanceof ApiError ? e.message : "Something went wrong.");
      throw e;
    }
  }, [meetingKey]);

  const addTask = useCallback(async (input: {
    task: string;
    due?: string;
    priority?: Task["priority"];
    assigneeContactId?: string;
  }): Promise<Task> => {
    const text = input.task.trim();
    if (!text) throw new Error("A task needs a description.");
    try {
      const created = await apiCreateTask(meetingKey, {
        task: text,
        due: input.due?.trim() || undefined,
        priority: input.priority,
        // A real contact, when one was picked — that is what makes the task
        // RESOLVED and notification-ready rather than a bare name.
        assignee_contact_id: input.assigneeContactId || undefined,
      });
      const task = taskFromApiTask(created, rec?.title || "Meeting");
      // Append rather than replace: the server is the source of truth for the
      // task itself, but the local list carries client-only fields (notes,
      // subtasks, attachments) on the OTHER tasks that a refetch would drop.
      setTasks((prev) => [...prev, task]);
      logActivity(`Added task "${task.task}"`);
      return task;
    } catch (e) {
      Alert.alert(
        "Couldn't add task",
        e instanceof ApiError ? e.message : "Something went wrong."
      );
      throw e;
    }
  }, [meetingKey, rec, logActivity]);

  const updateTask = useCallback((id: string, patch: Partial<Task>) => {
    applyTaskPatch(id, patch, () => apiUpdateTask(meetingKey, id, {
      task: patch.task, due: patch.due, priority: patch.priority,
    }), "Couldn't update task").catch(() => { /* already alerted */ });
  }, [applyTaskPatch, meetingKey]);

  const setTaskStatus = useCallback((id: string, status: TaskStatus) => {
    const t = tasks.find((x) => x.id === id);
    applyTaskPatch(id, { status }, () => apiUpdateTask(meetingKey, id, { status }),
      "Couldn't update status")
      .then(() => {
        pushTaskActivity(id, `Status changed to ${status}`);
        logActivity(`${t?.assignee?.name ?? "Task"} marked "${t?.task ?? "task"}" as ${status}`);
      })
      .catch(() => { /* already alerted */ });
  }, [applyTaskPatch, meetingKey, tasks, pushTaskActivity, logActivity]);

  const assignTask = useCallback((id: string, assignee: Assignee) => {
    const t = tasks.find((x) => x.id === id);
    applyTaskPatch(id, { assignee }, () => apiUpdateTask(meetingKey, id, {
      assignee: assigneeToApi(assignee),
    }), "Couldn't assign task")
      .then(() => {
        pushTaskActivity(id, `Assigned to ${assignee.name}`);
        logActivity(`Task assigned to ${assignee.name}: ${t?.task ?? ""}`);
      })
      .catch(() => { /* already alerted */ });
  }, [applyTaskPatch, meetingKey, tasks, pushTaskActivity, logActivity]);

  const recordNotification = useCallback((id: string, channels: NotifyChannel[]) => {
    const t = tasks.find((x) => x.id === id);
    const nextChannels = Array.from(new Set([...(t?.notifiedVia ?? []), ...channels]));
    applyTaskPatch(id, { notifiedVia: nextChannels }, () => apiUpdateTask(meetingKey, id, {
      notify_channels: channels,
    }), "Couldn't record notification")
      .then(() => pushTaskActivity(id, `Notified via ${channels.join(", ")}`))
      .catch(() => { /* already alerted */ });
  }, [applyTaskPatch, meetingKey, tasks, pushTaskActivity]);

  const addSubtask = useCallback((id: string, title: string) => {
    setTasks((prev) => prev.map((t) => (
      t.id === id ? { ...t, subtasks: [...t.subtasks, { id: nextId("sub"), title, done: false }] } : t
    )));
  }, []);

  const toggleSubtask = useCallback((id: string, subtaskId: string) => {
    setTasks((prev) => prev.map((t) => (
      t.id === id
        ? { ...t, subtasks: t.subtasks.map((s) => (s.id === subtaskId ? { ...s, done: !s.done } : s)) }
        : t
    )));
  }, []);

  const addAttachment = useCallback((id: string, attachment: Attachment) => {
    setTasks((prev) => prev.map((t) => (
      t.id === id ? { ...t, attachments: [...t.attachments, attachment] } : t
    )));
    pushTaskActivity(id, `Attachment added: ${attachment.name}`);
  }, [pushTaskActivity]);

  const removeAttachment = useCallback((id: string, attachmentId: string) => {
    setTasks((prev) => prev.map((t) => (
      t.id === id ? { ...t, attachments: t.attachments.filter((a) => a.id !== attachmentId) } : t
    )));
  }, []);

  const value = useMemo<MeetingCtxValue>(() => ({
    key: meetingKey, rec, loading, error, reload: load,
    documents, addDocument, setDocuments, renameMeeting, renameSpeaker,
    setCrmRecord, chooseCrmCandidate, confirmCrmRecord, syncCrmRecordNow,
    crmMappings, crmAmbiguity,
    documentsNeedingUpdate, updateAllDocuments,
    tasks, getTask, addTask, updateTask, setTaskStatus, assignTask, recordNotification,
    addSubtask, toggleSubtask, addAttachment, removeAttachment,
    activity, logActivity,
  }), [
    meetingKey, rec, loading, error, load, documents, addDocument, renameMeeting, renameSpeaker,
    setCrmRecord, chooseCrmCandidate, confirmCrmRecord, syncCrmRecordNow,
    crmMappings, crmAmbiguity,
    documentsNeedingUpdate, updateAllDocuments,
    tasks, getTask, addTask, updateTask, setTaskStatus, assignTask, recordNotification,
    addSubtask, toggleSubtask, addAttachment, removeAttachment, activity, logActivity,
  ]);

  return <MeetingCtx.Provider value={value}>{children}</MeetingCtx.Provider>;
}
