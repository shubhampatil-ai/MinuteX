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
  assignTaskToContact as apiAssignTaskToContact,
  type ApiContact,
} from "./api";
import type { GeneratedDoc } from "./meeting-documents";
import {
  taskFromApiTask, assigneeToApi, apiAssigneeToAssignee, avatarColorFor, nextId,
  type Task, type TaskStatus,
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
   * Re-read the meeting after a speaker was mapped to a Contact ELSEWHERE.
   *
   * The Participants screen writes through the participants API rather than
   * renameSpeaker, so the recording row's `speaker_names` changes server-side
   * with nothing telling this provider. Everything that renders a speaker name
   * — the transcript, the overview summary, generated documents — reads that
   * map through `rec`, so without this they keep showing the OLD name (or
   * "Speaker 0") until the provider remounts.
   */
  syncSpeakerNames: () => Promise<void>;
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
  // ---- Document staleness (speaker rename sync) ----
  documentsNeedingUpdate: string[];
  updateAllDocuments: () => Promise<{ updated: number; remaining: number }>;
  // ---- Tasks (client-side; see lib/task-model.ts) ----
  tasks: Task[];
  getTask: (id: string) => Task | undefined;
  updateTask: (id: string, patch: Partial<Task>) => void;
  setTaskStatus: (id: string, status: TaskStatus) => void;
  assignTask: (id: string, assignee: Assignee) => void;
  /** Assign to a REAL contact (resolved identity). Returns a promise so the
   *  caller can wait for the write before navigating away. */
  assignTaskToContact: (id: string, contact: ApiContact) => Promise<void>;
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

  // Re-read the tasks' ASSIGNEE ONLY, after a rename.
  //
  // A rename writes nothing to any task — the backend resolves the display
  // name per request from the meeting's speaker_names (see
  // _public_task_v2 / ApiTask.speaker_name). So the tasks already on screen
  // are correct everywhere EXCEPT in this app's own local copy, which was
  // built from the pre-rename response. Re-fetching is how that copy catches
  // up; nothing is written back.
  //
  // MERGED, never replaced: `tasks` carries client-only fields (notes,
  // subtasks, attachments, activity — see lib/task-model.ts) that exist
  // nowhere on the server, so mapping the fetch straight into state the way
  // load() does on first fetch would silently discard them. Only the assignee
  // is taken from the response, because only the assignee can have changed.
  const refreshTaskAssignees = useCallback(async () => {
    try {
      const apiTasks = await getTasks(meetingKey);
      const byId = new Map(apiTasks.map((t) => [t.id, t]));
      setTasks((prev) =>
        prev.map((t) => {
          const fresh = byId.get(t.id);
          if (!fresh) return t;
          const next = apiAssigneeToAssignee(fresh.assignee);
          // Same object identity when the name did not move, so a task whose
          // assignee is unaffected by this rename does not re-render.
          if (next?.name === t.assignee?.name) return t;
          return { ...t, assignee: next };
        })
      );
    } catch {
      // Best-effort, exactly like refreshDocumentStatus: the tasks on screen
      // keep the previous name until the next load() rather than erroring out
      // a rename that has already succeeded.
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
      // Tasks resolve their assignee server-side from the map that just
      // changed, so they need a re-read to pick the new name up — but no
      // regeneration and no "needs update" step, unlike documents.
      refreshTaskAssignees();
    } catch (e) {
      Alert.alert("Couldn't save the name", e instanceof ApiError ? e.message : "Something went wrong.");
      throw e;
    }
  }, [rec, meetingKey, refreshDocumentStatus, refreshTaskAssignees]);

  /** Pull the meeting back down after a speaker mapping changed elsewhere.
   *
   * The same three steps renameSpeaker performs, for the same reasons — the
   * only difference is that the write already happened on another screen, so
   * there is nothing to send, only to re-read. Kept as its own function rather
   * than exporting `reload` because reload() also re-fetches tasks and resets
   * loading state, which would flash a spinner over a screen the user is
   * still looking at.
   */
  const syncSpeakerNames = useCallback(async () => {
    if (!meetingKey) return;
    try {
      const data = await getRecording(meetingKey);
      setRec(data);
      // Documents bake their prose in, so they do not follow the map — ask
      // the backend which ones it now considers stale.
      refreshDocumentStatus();
      // Tasks resolve their assignee from the map that just changed.
      refreshTaskAssignees();
    } catch {
      // Best-effort: the mapping itself already succeeded, and the next
      // reload picks the name up. Failing loudly here would surface an error
      // for a write that worked.
    }
  }, [meetingKey, refreshDocumentStatus, refreshTaskAssignees]);

  // UPSERT by type, not a blind prepend. Regenerating a document, or saving
  // the structured MoM (which rewrites its mirrored minutes_of_meeting on
  // every save — see lib/mom-editor.tsx), calls this repeatedly with the same
  // type; prepending each time would stack duplicate rows in Documents(N) for
  // what is one document. A refreshed document keeps its POSITION rather than
  // jumping to the top, so the list does not reshuffle under the user while
  // they edit.
  const addDocument = useCallback((doc: GeneratedDoc) => {
    let replaced = false;
    setDocuments((prev) => {
      const at = prev.findIndex((d) => d.type === doc.type);
      if (at < 0) return [doc, ...prev];
      replaced = true;
      const next = prev.slice();
      next[at] = doc;
      return next;
    });
    if (!replaced) logActivity(`Document generated: ${doc.label}`);
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

  // Assign to a REAL contact. Distinct from assignTask above, which writes a
  // legacy NAME the backend must store as UNRESOLVED: this sends a contact_id,
  // so the task ends up with a resolved identity that can actually be filtered
  // and notified.
  //
  // The local list is patched from the SERVER's echo of the task, not from the
  // contact we sent — a reassignment must show what was actually persisted,
  // and this list is only fetched once per meeting (tasksLoadedRef), so a
  // write that skips it leaves the old assignee on screen until the provider
  // remounts. That was the reassign-does-nothing bug.
  const assignTaskToContact = useCallback(async (id: string, contact: ApiContact) => {
    const t = tasks.find((x) => x.id === id);
    const optimistic: Assignee = {
      name: contact.name,
      email: contact.email || undefined,
      phone: contact.phone || undefined,
      avatarColor: avatarColorFor(contact.name),
      source: "manual",
    };
    let saved: Assignee | null = optimistic;
    await applyTaskPatch(id, { assignee: optimistic }, async () => {
      const fresh = await apiAssignTaskToContact(meetingKey, id, contact.id);
      saved = apiAssigneeToAssignee(fresh.assignee);
    }, "Couldn't assign task");
    // Reconcile with what the server actually stored.
    setTasks((prev) => prev.map((x) => (x.id === id ? { ...x, assignee: saved } : x)));
    pushTaskActivity(id, `Assigned to ${contact.name}`);
    logActivity(`Task assigned to ${contact.name}: ${t?.task ?? ""}`);
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
    syncSpeakerNames,
    documentsNeedingUpdate, updateAllDocuments,
    tasks, getTask, addTask, updateTask, setTaskStatus, assignTask,
    assignTaskToContact, recordNotification,
    addSubtask, toggleSubtask, addAttachment, removeAttachment,
    activity, logActivity,
  }), [
    meetingKey, rec, loading, error, load, documents, addDocument, renameMeeting, renameSpeaker,
    syncSpeakerNames,
    documentsNeedingUpdate, updateAllDocuments,
    tasks, getTask, addTask, updateTask, setTaskStatus, assignTask,
    assignTaskToContact, recordNotification,
    addSubtask, toggleSubtask, addAttachment, removeAttachment, activity, logActivity,
  ]);

  return <MeetingCtx.Provider value={value}>{children}</MeetingCtx.Provider>;
}
