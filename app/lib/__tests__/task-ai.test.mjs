// lib/__tests__/task-ai.test.mjs — the Task AI dashboard: the real assistant
// entry point, workspace evidence deep-linking, and confirmation-gated
// proposals.
//
// Run:  node --test lib/__tests__/task-ai.test.mjs
//
// WHAT THIS PINS.
//
//   1. THE AI CARD IS NO LONGER A FAKE. Its chips used to apply a local
//      filter and print a sentence about it, because there was no task-agent
//      endpoint to call. There is one now, so the honest implementation is
//      the real call — and the stale comments that said otherwise must be
//      gone, or the next reader will "fix" the code back to the fake.
//
//   2. ONE NAVIGATION MECHANISM FOR EVIDENCE. A workspace answer's source, a
//      meeting answer's source and a task's "View in transcript" all resolve
//      through /recording/[key]/transcript?evidence=seg_N. The workspace one
//      differs in exactly one respect — it must take its key from the
//      source's own meeting_id, because an answer can cite several meetings
//      and the screen has no meeting in context.
//
//   3. NOTHING MUTATES WITHOUT A CONFIRMATION. A proposal arrives with
//      applied:false and a patch body; the app must render it and wait. The
//      write goes through the EXISTING patchTaskById, not a new AI endpoint.
//
//   4. A RECOMMENDATION IS NEVER TASK STATE. An intelligence row carries a
//      task id and a judgement. Every value a card shows about the task is
//      resolved from the loaded task, and a row whose task is not loaded is
//      dropped rather than rendered from the AI's own words.
//
// The modules under test are TSX, so the pure logic is mirrored here rather
// than imported — the convention in task-insights.test.mjs and
// ai-chat-sources.test.mjs (node --test runs .mjs with no transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

const read = (p) => readFileSync(join(ROOT, p), "utf8");

// --- mirrored from src/app/assistant.tsx ----------------------------------

/** The route a workspace source row pushes. The key comes from the SOURCE,
 *  not from screen context — that is the whole difference from the
 *  meeting-scoped version. */
function workspaceSourceRoute(src) {
  return {
    pathname: "/recording/[key]/transcript",
    params: { key: src.meeting_id, evidence: src.segment_id },
  };
}

/** Which sources are renderable. A workspace source needs BOTH ids: without
 *  meeting_id there is no transcript to open, so the row would scroll
 *  nowhere — worse than no row. */
function usableSources(sources) {
  return (sources ?? []).filter((s) => s.segment_id && s.meeting_id);
}

/** What the screen sends as the first message of a task-scoped conversation.
 *  The id is prefixed ONCE: after that it is in the history and the agent has
 *  it from its own tool results. */
function outboundMessage(message, scopedTaskId, priorTurns) {
  return scopedTaskId && !priorTurns.length
    ? `About task ${scopedTaskId}: ${message}`
    : message;
}

// --- mirrored from lib/task-action-center.tsx ------------------------------

const INTEL_KINDS = [
  "priority", "needs_attention", "overdue_risk", "stale", "duplicate",
];

/** Intelligence rows grouped into sections, over rows whose task is loaded. */
function groupIntel(rows, tasksById) {
  return INTEL_KINDS
    .map((kind) => ({
      kind,
      items: rows
        .filter((r) => r.kind === kind && tasksById.has(r.task_id))
        .map((r) => ({ row: r, task: tasksById.get(r.task_id) })),
    }))
    .filter((sec) => sec.items.length > 0);
}

/** What a card DISPLAYS for a task — always from the loaded task, never from
 *  the AI row. */
function cardTitle(task) {
  return task.task || task.title;
}

// --- mirrored from lib/api.ts ---------------------------------------------

/** applyTaskProposal: the confirmed write, through the existing route. */
function proposalPatch(proposal) {
  return { taskId: proposal.task.id, body: proposal.confirm.body };
}

function defaultedReply(res) {
  return {
    reply: res.reply ?? "",
    tools_used: res.tools_used ?? [],
    sources: res.sources ?? [],
    proposals: res.proposals ?? [],
  };
}

// ===========================================================================
describe("workspace evidence deep-linking", () => {
  const src = {
    segment_id: "seg_12", meeting_id: "recordings/u-1/dev/m_1.wav",
    start_time: 61.5, end_time: 64.0,
  };

  it("pushes the same transcript route the rest of the app uses", () => {
    const r = workspaceSourceRoute(src);
    assert.equal(r.pathname, "/recording/[key]/transcript");
    assert.equal(r.params.evidence, "seg_12");
  });

  it("takes the recording key from the source, not from screen context", () => {
    assert.equal(workspaceSourceRoute(src).params.key,
      "recordings/u-1/dev/m_1.wav");
  });

  it("two sources from different meetings each open their own transcript", () => {
    const a = { segment_id: "seg_1", meeting_id: "key-a" };
    const b = { segment_id: "seg_9", meeting_id: "key-b" };
    assert.equal(workspaceSourceRoute(a).params.key, "key-a");
    assert.equal(workspaceSourceRoute(b).params.key, "key-b");
  });

  it("drops a source with no meeting_id rather than linking nowhere", () => {
    const out = usableSources([
      { segment_id: "seg_1" },
      { segment_id: "seg_2", meeting_id: "key-a" },
    ]);
    assert.deepEqual(out.map((s) => s.segment_id), ["seg_2"]);
  });

  it("drops a source with no segment_id", () => {
    const out = usableSources([
      { meeting_id: "key-a" },
      { segment_id: "seg_2", meeting_id: "key-a" },
    ]);
    assert.equal(out.length, 1);
  });

  it("treats an absent sources array as no sources, not as an error", () => {
    assert.deepEqual(usableSources(undefined), []);
    assert.deepEqual(usableSources(null), []);
    assert.deepEqual(usableSources([]), []);
  });
});

describe("task-scoped conversation addressing", () => {
  it("prefixes the task id on the first turn", () => {
    assert.equal(
      outboundMessage("Why does this exist?", "t-1", []),
      "About task t-1: Why does this exist?");
  });

  it("does not repeat the prefix on later turns", () => {
    assert.equal(
      outboundMessage("And who owns it?", "t-1", [{ role: "user" }]),
      "And who owns it?");
  });

  it("sends the message unchanged in workspace scope", () => {
    assert.equal(outboundMessage("What's overdue?", "", []), "What's overdue?");
  });
});

describe("proposals are applied through the existing task route", () => {
  const proposal = {
    applied: false,
    requires_user_confirmation: true,
    task: { id: "t-7", title: "Send the revised quotation" },
    current: { status: "Open" },
    proposed: { status: "Completed" },
    reason: "You said it went out yesterday.",
    confirm: { method: "PATCH", path: "/tasks/t-7", body: { status: "Completed" } },
  };

  it("arrives not applied and asking for confirmation", () => {
    assert.equal(proposal.applied, false);
    assert.equal(proposal.requires_user_confirmation, true);
  });

  it("patches the task named by the proposal, with the server's body", () => {
    assert.deepEqual(proposalPatch(proposal),
      { taskId: "t-7", body: { status: "Completed" } });
  });

  it("carries the current value so a before/after can be rendered", () => {
    assert.equal(proposal.current.status, "Open");
    assert.equal(proposal.proposed.status, "Completed");
  });

  it("uses `due` — the key the real PATCH route accepts — for a deadline", () => {
    // A proposal that used `due_date` here would 400 on confirm.
    const p = { confirm: { body: { due: "2026-09-09" } }, task: { id: "t-8" } };
    assert.deepEqual(Object.keys(p.confirm.body), ["due"]);
  });
});

describe("an AI recommendation is never task state", () => {
  const tasksById = new Map([
    ["t-1", { id: "t-1", task: "Send the quotation", status: "Open" }],
    ["t-2", { id: "t-2", task: "Book the venue", status: "In Progress" }],
  ]);

  it("renders the title from the loaded task, not from the AI row", () => {
    const row = { task_id: "t-1", kind: "priority", reason: "due tomorrow" };
    const [sec] = groupIntel([row], tasksById);
    assert.equal(cardTitle(sec.items[0].task), "Send the quotation");
  });

  it("drops a row whose task is not loaded", () => {
    const rows = [
      { task_id: "t-1", kind: "priority", reason: "a" },
      { task_id: "t-99", kind: "priority", reason: "invented" },
    ];
    const [sec] = groupIntel(rows, tasksById);
    assert.deepEqual(sec.items.map((i) => i.row.task_id), ["t-1"]);
  });

  it("renders no section when nothing in it resolves", () => {
    const rows = [{ task_id: "t-99", kind: "stale", reason: "x" }];
    assert.deepEqual(groupIntel(rows, tasksById), []);
  });

  it("groups rows under the section their kind names", () => {
    const rows = [
      { task_id: "t-1", kind: "overdue_risk", reason: "a" },
      { task_id: "t-2", kind: "stale", reason: "b" },
    ];
    const kinds = groupIntel(rows, tasksById).map((s) => s.kind);
    assert.deepEqual(kinds, ["overdue_risk", "stale"]);
  });

  it("orders sections by urgency, not by the order the model returned", () => {
    const rows = [
      { task_id: "t-1", kind: "stale", reason: "a" },
      { task_id: "t-2", kind: "priority", reason: "b" },
    ];
    assert.deepEqual(groupIntel(rows, tasksById).map((s) => s.kind),
      ["priority", "stale"]);
  });

  it("lets one task appear under two sections", () => {
    const rows = [
      { task_id: "t-1", kind: "priority", reason: "a" },
      { task_id: "t-1", kind: "overdue_risk", reason: "b" },
    ];
    assert.equal(groupIntel(rows, tasksById).length, 2);
  });

  it("shows a duplicate's counterpart by its real title", () => {
    const rows = [{
      task_id: "t-1", kind: "duplicate", reason: "same work",
      related_task_id: "t-2",
    }];
    const [sec] = groupIntel(rows, tasksById);
    const related = tasksById.get(sec.items[0].row.related_task_id);
    assert.equal(cardTitle(related), "Book the venue");
  });
});

describe("the reply shape is additive", () => {
  it("an older backend's response still parses", () => {
    const out = defaultedReply({ reply: "You have 2 tasks." });
    assert.deepEqual(out.sources, []);
    assert.deepEqual(out.proposals, []);
    assert.deepEqual(out.tools_used, []);
  });

  it("a grounded answer keeps its sources", () => {
    const out = defaultedReply({
      reply: "It came from the Acme call.",
      sources: [{ segment_id: "seg_3", meeting_id: "k", start_time: 1, end_time: 2 }],
    });
    assert.equal(out.sources.length, 1);
  });
});

// ---------------------------------------------------------------------------
// Source-of-truth checks against the real files, so this mirror cannot
// quietly drift from the screens it claims to describe.
describe("the mirrored logic matches the shipped code", () => {
  const assistant = read("src/app/assistant.tsx");
  const api = read("lib/api.ts");
  const center = read("lib/task-action-center.tsx");
  const tasks = read("src/app/tasks.tsx");
  const detail = read("src/app/task/[id].tsx");
  const layout = read("src/app/_layout.tsx");

  it("the workspace assistant screen exists and is registered", () => {
    assert.match(layout, /name="assistant"/);
  });

  it("the assistant pushes the shared transcript evidence route", () => {
    assert.match(assistant, /pathname: "\/recording\/\[key\]\/transcript"/);
    assert.match(assistant, /key: src\.meeting_id, evidence: src\.segment_id/);
  });

  it("the assistant filters sources that cannot be opened", () => {
    assert.match(assistant, /s\.segment_id && s\.meeting_id/);
  });

  it("a proposal is applied only from a press, never during render", () => {
    // The mutation must sit inside the confirm handler.
    assert.match(assistant, /const confirm = useCallback\(async \(\) => \{[\s\S]*?applyTaskProposal\(proposal\)/);
  });

  it("the proposal card offers both Confirm and a way out", () => {
    assert.match(assistant, /Confirm</);
    assert.match(assistant, /Not now</);
  });

  it("applyTaskProposal routes through the existing patchTaskById", () => {
    assert.match(api, /export async function applyTaskProposal/);
    assert.match(api, /return patchTaskById\(p\.task\.id, p\.confirm\.body/);
  });

  it("api.ts defaults the additive keys rather than passing undefined", () => {
    assert.match(api, /sources: res\.sources \?\? \[\]/);
    assert.match(api, /proposals: res\.proposals \?\? \[\]/);
  });

  it("AssistantSource extends the meeting ChatSource with a meeting id", () => {
    assert.match(api, /AssistantSource = ChatSource & \{ meeting_id: string \}/);
  });

  it("TaskProposal has no assignee field", () => {
    const block = api.slice(api.indexOf("export type TaskProposal"),
      api.indexOf("export type AssistantReply"));
    assert.ok(!/assignee/.test(block),
      "reassignment must stay a UI flow — a proposal cannot carry a name");
  });

  it("TaskIntelRow carries a judgement and NOT task state", () => {
    const block = api.slice(api.indexOf("export type TaskIntelRow"),
      api.indexOf("export type TaskIntelligence"));
    for (const field of ["status", "assignee", "due_date", "priority"]) {
      assert.ok(!new RegExp(`^\\s*${field}\\??:`, "m").test(block),
        `TaskIntelRow must not carry ${field} — state comes from the Tasks API`);
    }
    assert.match(block, /task_id: string;/);
    assert.match(block, /reason: string;/);
  });

  it("the AI chips carry the question they ask", () => {
    // The label a user taps and the message the model receives are the same
    // string, so the card cannot promise something different from what it asks.
    assert.match(center, /label: "What needs my attention\?"/);
    assert.match(center, /label: "What's overdue\?"/);
    assert.match(center, /onAsk\(p\.label\)/);
  });

  it("the AI card no longer takes a local `note` to print", () => {
    assert.ok(!/note\?: string/.test(center),
      "the fake local-note affordance must be gone, not merely unused");
  });

  it("the dashboard opens the real assistant from a chip", () => {
    assert.match(tasks, /pathname: "\/assistant", params: \{ q: question \}/);
  });

  it("the dashboard no longer claims there is no task-agent endpoint", () => {
    for (const stale of [
      /There is no task-agent endpoint/i,
      /a route that does not exist/i,
      /NO task-agent backend/i,
    ]) {
      assert.ok(!stale.test(tasks) && !stale.test(center),
        `a stale "no backend" claim survives: ${stale}`);
    }
  });

  it("the dashboard resolves AI rows against real loaded tasks", () => {
    assert.match(tasks, /const tasksById = useMemo/);
    assert.match(tasks, /tasksById=\{tasksById\}/);
  });

  it("task intelligence is fetched on demand, not on mount or focus", () => {
    // loadIntel must not appear inside a focus effect or the initial load.
    assert.match(tasks, /const loadIntel = useCallback/);
    assert.ok(!/useFocusEffect\([\s\S]{0,400}?loadIntel/.test(tasks),
      "a Groq call must not fire on every visit to the tab");
    assert.match(tasks, /onAnalyze=\{loadIntel\}/);
  });

  it("the intelligence section labels itself as suggestions", () => {
    assert.match(center, /AI suggestions/);
  });

  it("a partial analysis says so", () => {
    assert.match(center, /not all of them/);
  });

  it("Task Details offers a scoped AI entry point", () => {
    assert.match(detail, /pathname: "\/assistant", params: \{ taskId: task\.id \}/);
  });

  it("Task Details still shows its own state and provenance", () => {
    // The AI section is additive: none of the existing surfaces may be lost.
    assert.match(detail, /Where this came from/);
    assert.match(detail, /What was said/);
    assert.match(detail, /evidence: evidenceIds\.join\(","\)/);
  });
});

// ---------------------------------------------------------------------------
// THE "Could not analyze your tasks" REGRESSION.
//
// The dashboard reported that message for a request that never reached the
// backend: POST /ai/task-intelligence was registered in the Lambda's route
// table but never wired into API Gateway, so it 404'd before invocation and
// left no CloudWatch trace. A bare `catch {}` in the screen then rendered a
// missing endpoint identically to a Groq outage, which is what made it hard
// to find. The three causes need different actions, so they are told apart.
describe("task-intelligence failures are distinguishable", () => {
  // Mirrored from src/app/tasks.tsx loadIntel.
  function intelErrorFor(e, isRetryableFn) {
    if (e?.__isApiError && e.status === 404) {
      return "Task analysis isn't available on this server yet.";
    }
    if (isRetryableFn(e)) {
      return "The analysis service is busy. Try again in a moment.";
    }
    return e?.__isApiError ? e.message : "Could not analyze your tasks.";
  }

  const apiError = (status, message) =>
    ({ __isApiError: true, status, message });
  const retryable = (e) => e?.__isApiError && (e.status === 502 || e.status === 429);

  it("a missing route reads as a server-side gap, not a task problem", () => {
    assert.match(
      intelErrorFor(apiError(404, "not found"), retryable),
      /isn't available on this server/);
  });

  it("a busy model reads as temporary", () => {
    assert.match(
      intelErrorFor(apiError(502, "Unable to generate task intelligence."),
        retryable),
      /busy/);
  });

  it("any other API error surfaces its own message", () => {
    assert.equal(
      intelErrorFor(apiError(500, "Unable to generate task intelligence."),
        retryable),
      "Unable to generate task intelligence.");
  });

  it("a non-API failure still says something", () => {
    assert.equal(intelErrorFor(new Error("network down"), retryable),
      "Could not analyze your tasks.");
  });
});

describe("the intelligence failure path is wired as mirrored", () => {
  const tasks = read("src/app/tasks.tsx");
  const center = read("lib/task-action-center.tsx");

  it("the screen no longer swallows the reason", () => {
    assert.ok(!/\}\s*catch\s*\{\s*\n\s*setIntelState\("error"\);/.test(tasks),
      "a bare catch here is what made the 404 undiagnosable");
    assert.match(tasks, /catch \(e\) \{/);
  });

  it("a 404 is told apart from a model failure", () => {
    assert.match(tasks, /e\.status === 404/);
    assert.match(tasks, /isRetryable\(e\)/);
  });

  it("the card renders the carried reason", () => {
    assert.match(center, /error \|\| "Your task list is unaffected\."/);
  });

  it("the analysis failure never touches the task list", () => {
    // setIntelError/setIntelState only; no setTasks in the catch block.
    const block = tasks.slice(tasks.indexOf("const loadIntel = useCallback"),
      tasks.indexOf("const onHealthSelect"));
    assert.ok(!/setTasks\(/.test(block),
      "an AI failure must not disturb the authoritative task list");
  });
});

// ---------------------------------------------------------------------------
// PERSISTENT CONVERSATIONS.
//
// The workspace assistant used to keep its thread in component state and
// replay it as `history`, so navigating away lost the conversation. It is now
// persisted server-side and addressed by `session_id`.
//
// WHAT THESE PIN, and why each would be a real regression:
//
//   1. THE SERVER IS THE SOURCE OF TRUTH. History is sent only when there is
//      no session yet. Sending both would let a stale client thread overwrite
//      what the model believes was said.
//   2. THE ID IS NEVER INVENTED CLIENT-SIDE. An id the server does not know
//      404s on the next turn, so the screen adopts what the reply returns and
//      nothing else.
//   3. A FAILED SAVE IS VISIBLE. The answer is real, but it will not be there
//      when the user comes back — silently losing it is the worse outcome.
//   4. ONLY AN ID IS CACHED ON THE DEVICE, never the messages (spec §20).
describe("session request shaping", () => {
  // Mirrored from lib/api.ts sendAIMessage.
  function chatBody(message, sessionId, history) {
    const body = { message };
    if (sessionId) body.session_id = sessionId;
    else if (history?.length) body.history = history;
    return body;
  }

  const history = [
    { role: "user", content: "First question" },
    { role: "assistant", content: "First answer" },
  ];

  it("a first turn sends no session_id", () => {
    assert.deepEqual(chatBody("Hello", "", []), { message: "Hello" });
  });

  it("a continued turn sends the session and NOT the history", () => {
    assert.deepEqual(chatBody("Next?", "s-1", history),
      { message: "Next?", session_id: "s-1" });
  });

  it("a sessionless client still sends history, as it always did", () => {
    assert.deepEqual(chatBody("Next?", "", history),
      { message: "Next?", history });
  });

  it("never sends both — the server's copy would win anyway", () => {
    const body = chatBody("Next?", "s-1", history);
    assert.ok(!("history" in body));
  });
});

describe("session adoption and failure reporting", () => {
  // Mirrored from src/app/assistant.tsx ask().
  function adopt(prev, res) {
    const next = res.session_id && res.session_id !== prev ? res.session_id : prev;
    return { sessionId: next, unsaved: !res.persisted || !res.session_id };
  }

  it("adopts the id the first reply returns", () => {
    assert.equal(adopt("", { session_id: "s-9", persisted: true }).sessionId, "s-9");
  });

  it("keeps the same id on later turns", () => {
    assert.equal(adopt("s-9", { session_id: "s-9", persisted: true }).sessionId, "s-9");
  });

  it("flags a turn the backend could not save", () => {
    assert.equal(adopt("s-9", { session_id: "s-9", persisted: false }).unsaved, true);
  });

  it("treats a missing id as unsaved rather than inventing one", () => {
    const out = adopt("", { session_id: "", persisted: true });
    assert.equal(out.sessionId, "");
    assert.equal(out.unsaved, true);
  });

  it("a normal turn is not flagged", () => {
    assert.equal(adopt("s-9", { session_id: "s-9", persisted: true }).unsaved, false);
  });
});

describe("resuming the last conversation", () => {
  // Mirrored from the resume effect in src/app/assistant.tsx.
  function shouldResume(lastId, list, scopedTaskId, seedQuestion) {
    if (scopedTaskId || seedQuestion) return false;
    return !!lastId && list.some((s) => s.session_id === lastId);
  }

  const list = [{ session_id: "s-1" }, { session_id: "s-2" }];

  it("reopens the last conversation when it still exists", () => {
    assert.equal(shouldResume("s-2", list, "", ""), true);
  });

  it("does not reopen an id the list no longer contains", () => {
    // Deleted on another device — resuming would flash an error.
    assert.equal(shouldResume("s-gone", list, "", ""), false);
  });

  it("starts fresh for a task-scoped visit", () => {
    // "Ask about this task" is a new question, not a continuation.
    assert.equal(shouldResume("s-2", list, "t-1", ""), false);
  });

  it("starts fresh when a question was handed in by the route", () => {
    assert.equal(shouldResume("s-2", list, "", "What's overdue?"), false);
  });

  it("starts fresh with nothing remembered", () => {
    assert.equal(shouldResume("", list, "", ""), false);
  });
});

describe("the session wiring matches the shipped code", () => {
  const api = read("lib/api.ts");
  const assistant = read("src/app/assistant.tsx");

  it("api.ts exposes the three session routes", () => {
    assert.match(api, /export async function listChatSessions/);
    assert.match(api, /export async function getChatSession/);
    assert.match(api, /export async function deleteChatSession/);
    assert.match(api, /`\/ai\/chat\/sessions\$\{qs\}`/);
  });

  it("sendAIMessage takes a session id and defaults the new keys", () => {
    assert.match(api, /sessionId\?: string/);
    assert.match(api, /session_id: res\.session_id \?\? ""/);
    assert.match(api, /persisted: res\.persisted \?\? true/);
  });

  it("history is sent only when there is no session", () => {
    assert.match(api, /if \(sessionId\) body\.session_id = sessionId;\s*\n\s*else if \(history\?\.length\) body\.history = history;/);
  });

  it("the screen sends the session id", () => {
    assert.match(assistant, /sessionId \|\| undefined/);
    assert.match(assistant, /sessionId \? undefined : prior\.map/);
  });

  it("only the session ID is cached on the device, never the messages", () => {
    // Spec §20: backend persistence is the source of truth.
    assert.match(assistant, /store\.setItemAsync\(LAST_SESSION_KEY, /);
    assert.ok(!/setItemAsync\([^)]*turns/.test(assistant),
      "messages must never be written to device storage");
    assert.ok(!/setItemAsync\([^)]*JSON\.stringify/.test(assistant),
      "nothing but the id belongs in device storage");
  });

  it("the screen offers History, New and Delete", () => {
    assert.match(assistant, /Recent conversations/);
    assert.match(assistant, /accessibilityLabel="New conversation"/);
    assert.match(assistant, /Delete conversation\?/);
  });

  it("deleting a conversation asks first", () => {
    // Destructive and irreversible server-side.
    assert.match(assistant, /Alert\.alert\(\s*"Delete conversation\?"/);
    assert.match(assistant, /style: "destructive"/);
  });

  it("an unsaved turn is surfaced to the user", () => {
    assert.match(assistant, /could not be saved/);
  });

  it("a 404 on open falls back to a new conversation", () => {
    // The conversation is genuinely gone; stranding the screen would be worse.
    assert.match(assistant, /e\.status === 404[\s\S]{0,220}?deleteItemAsync\(LAST_SESSION_KEY\)/);
  });

  it("the stale STATELESS claim is gone from the header", () => {
    assert.ok(!/\*\s+STATELESS\. \/ai\/chat persists nothing/.test(assistant),
      "the header comment must not still say the endpoint is stateless");
  });
});
