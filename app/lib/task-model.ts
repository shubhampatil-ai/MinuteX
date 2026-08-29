// lib/task-model.ts — the task model the Task Detail / Assign / Notify /
// Tracking screens share.
//
// CORE fields (task, due, priority, status, assignee, notifiedVia) are now
// PERSISTED server-side — see lib/api.ts's ApiTask and lambda-userapi's Tasks
// section. This module's Task type still carries a few fields that stay
// client-side only, because no backend concept exists for them (confirmed
// against lambda-userapi): notes, subtasks, attachments, and the per-task
// activity log. Attachments in particular have nowhere to live — only
// recordings have an S3 upload pipeline — so a picked file's local URI is
// real but never uploaded, same as before this change. taskFromApiTask()
// is the bridge: it takes what the backend returned and fills in empty
// defaults for the client-only parts.
import type { ApiTask, ApiTaskAssignee } from "./api";

export type TaskPriority = "Low" | "Medium" | "High";
// Mirrors the backend vocabulary. "Cancelled" was added when tasks became
// first-class: the spec needs a terminal state that is NOT "done", for work
// that was dropped rather than completed. The three original values keep their
// exact spelling, so nothing that compares against them needed to change.
//
// NOTE the pickers that offer statuses (STATUS_STEPS in the task detail
// screen) are separate lists on purpose — "Cancelled" is a state a task can BE
// without being one of the steps a user walks through.
export type TaskStatus = "Open" | "In Progress" | "Completed" | "Cancelled";

export type Assignee = {
  name: string;
  // One of these identifies how to reach them — a manual-entry assignee may
  // have only a phone OR only an email, never neither.
  phone?: string;
  email?: string;
  avatarColor: string;
  source: "team" | "recent" | "phone" | "manual";
};

export type NotifyChannel = "whatsapp" | "email" | "sms" | "app";

export type ActivityEntry = {
  id: string;
  at: string; // ISO
  text: string;
};

// A real file the user picked on-device (via expo-document-picker). `uri` is
// a local file:// / content:// URI, valid only on this device this session —
// there is no backend endpoint to store task attachments (only recordings
// have an upload pipeline, see lib/uploads.tsx), so this is never persisted
// remotely and won't survive a reload or reach anyone else.
export type Attachment = {
  id: string;
  name: string;
  size: number | null;
  mimeType: string | null;
  uri: string;
};

export type Task = {
  id: string;
  task: string;
  description: string;
  meetingTitle: string;
  due: string;
  priority: TaskPriority;
  status: TaskStatus;
  assignee: Assignee | null;
  notes: string;
  subtasks: { id: string; title: string; done: boolean }[];
  attachments: Attachment[];
  notifiedVia: NotifyChannel[];
  activity: ActivityEntry[];
};

let seq = 0;
// Date.now()/Math.random() are unavailable in some execution contexts this
// module may be pulled into (workflow scripts) — a monotonic in-memory
// counter is enough since ids only need to be unique within one app session.
export function nextId(prefix: string): string {
  seq += 1;
  return `${prefix}_${seq}_${Math.floor(Math.random() * 1e6)}`;
}

const AVATAR_COLORS = ["#3E6BFF", "#1FA972", "#7C5CFF", "#F5A623", "#0EA5B7", "#E5484D"];
export function avatarColorFor(name: string): string {
  // Defensive against non-string input: real device contact records (via
  // expo-contacts) don't always honor their own TS types at runtime — a
  // bare `.length`/`.charCodeAt` on undefined here throws a message-less
  // TypeError that's very hard to trace back to its call site.
  const safe = typeof name === "string" ? name : "";
  let h = 0;
  for (let i = 0; i < safe.length; i++) h = (h * 31 + safe.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

/** Exported for the rename path: refreshTaskAssignees (lib/meeting-context)
 * takes ONLY the assignee off a re-fetched task, so it needs the same
 * conversion taskFromApiTask does rather than a second copy of it. */
export function apiAssigneeToAssignee(a: ApiTaskAssignee | null): Assignee | null {
  if (!a || !a.name) return null;
  return {
    name: a.name,
    phone: a.phone,
    email: a.email,
    avatarColor: avatarColorFor(a.name),
    source: a.source,
  };
}

export function assigneeToApi(a: Assignee | null): ApiTaskAssignee | null {
  if (!a) return null;
  return { name: a.name, phone: a.phone, email: a.email, source: a.source };
}

/** Build the full Task model from the backend's persisted ApiTask, filling
 * in empty defaults for the fields that stay client-side only (notes,
 * subtasks, attachments, activity) — see this file's header comment. */
export function taskFromApiTask(t: ApiTask, meetingTitle: string): Task {
  return {
    id: t.id,
    task: t.task,
    description: t.task,
    meetingTitle,
    due: t.due || "",
    priority: t.priority,
    status: t.status,
    assignee: apiAssigneeToAssignee(t.assignee),
    notes: "",
    subtasks: [],
    attachments: [],
    notifiedVia: (t.notified_via || []) as NotifyChannel[],
    activity: [
      {
        id: nextId("act"),
        at: t.created_at || new Date().toISOString(),
        text: t.from_action_item
          ? "Task detected from meeting transcript"
          : "Task created",
      },
    ],
  };
}

export function initialsOf(name: string): string {
  const trimmed = typeof name === "string" ? name.trim() : "";
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0][0].toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}
