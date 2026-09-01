// lib/__tests__/task-reassignment.test.mjs — reassigning a task inside a
// meeting, and the two bugs that made it feel broken.
//
// Run:  node --test lib/__tests__/task-reassignment.test.mjs
//
// WHAT THIS PINS.
//
//   1. ASSIGNMENT GOES THROUGH THE MEETING CONTEXT, NEVER THE API DIRECTLY.
//      The meeting's task list is fetched ONCE per meeting (tasksLoadedRef in
//      lib/meeting-context.tsx) and every meeting surface — Overview, the
//      Assistant, the Tasks list, Task Detail, Assign To — renders from that
//      one array. A write that calls the API behind its back therefore
//      persists correctly and still shows the OLD assignee everywhere until
//      the provider remounts. That was the "reassigning does nothing" bug:
//      the request succeeded, the screen just never heard about it.
//
//   2. ASSIGNING DOES NOT FORCE THE NOTIFY SCREEN. Assign To used to
//      router.replace() into Notify Assignee on success. Who owns a task and
//      whether to message them are independent decisions, and most
//      reassignments are bookkeeping. Notifying stays available as an
//      explicit action on Task Detail — so this pins BOTH halves: the forced
//      redirect is gone AND the deliberate entry point exists, because
//      removing the redirect without adding the button would silently strip
//      the feature (that redirect was its only route in).
//
//   3. THE ASSIGNEE IS RECONCILED FROM THE SERVER'S RESPONSE. The optimistic
//      patch makes the UI feel instant; the echo is what it settles on. A
//      reassignment must display what was actually persisted, not what we
//      hoped to persist.
//
// Source assertions rather than a render harness: these are TSX modules and
// node --test runs .mjs with no transpile step — the same convention
// task-provenance.test.mjs and notification-center.test.mjs follow.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

const read = (p) => readFileSync(join(ROOT, p), "utf8");

const CONTEXT = read("lib/meeting-context.tsx");
const ASSIGN = read("src/app/recording/[key]/task/[taskId]/assign.tsx");
const TASK_DETAIL = read("src/app/recording/[key]/task/[taskId]/index.tsx");
const LAYOUT = read("src/app/recording/[key]/_layout.tsx");

describe("assignment flows through the meeting context", () => {
  it("the context exposes a contact-assignment action", () => {
    assert.match(
      CONTEXT,
      /assignTaskToContact:\s*\(id: string, contact: ApiContact\) => Promise<void>/,
      "meeting-context must expose assignTaskToContact so every meeting " +
      "surface updates from one write"
    );
    assert.match(
      CONTEXT, /const assignTaskToContact = useCallback\(/,
      "assignTaskToContact must be implemented, not just declared"
    );
  });

  it("that action is published on the context value", () => {
    // Twice: once in the memo'd value, once in its dependency list.
    const published = CONTEXT.match(/^\s*assignTaskToContact, recordNotification,$/gm);
    assert.equal(
      published?.length, 2,
      "assignTaskToContact must appear in the context value AND its deps"
    );
  });

  it("Assign To calls the context, not the API", () => {
    assert.match(
      ASSIGN, /const \{ getTask, assignTaskToContact \} = useMeeting\(\)/,
      "Assign To must take assignTaskToContact from the meeting context"
    );
    assert.match(
      ASSIGN, /await assignTaskToContact\(String\(taskId\), contact\)/,
      "Assign To must assign through the context"
    );
    assert.doesNotMatch(
      ASSIGN, /assignTaskToContact,?\s*\n?\s*getParticipants,?\s*\n?\} from "[^"]*lib\/api"/,
      "Assign To must NOT import the raw API assign call — going straight to " +
      "the API is what left the meeting's task list showing the old assignee"
    );
  });

  it("the local list settles on the SERVER's assignee, not the optimistic one", () => {
    assert.match(
      CONTEXT, /saved = apiAssigneeToAssignee\(fresh\.assignee\)/,
      "the reconciled assignee must come from the API response"
    );
    assert.match(
      CONTEXT,
      /setTasks\(\(prev\) => prev\.map\(\(x\) => \(x\.id === id \? \{ \.\.\.x, assignee: saved \} : x\)\)\)/,
      "the task list must be patched with the server's echo after the write"
    );
  });

  it("one provider spans every meeting screen, so one write reaches them all", () => {
    assert.match(LAYOUT, /<MeetingProvider meetingKey=\{key\}>/);
    for (const screen of ["task/[taskId]/index", "task/[taskId]/assign", "index"]) {
      assert.ok(
        LAYOUT.includes(`name="${screen}"`),
        `${screen} must live under the shared MeetingProvider`
      );
    }
  });
});

describe("notifying is optional, not a forced step", () => {
  it("Assign To no longer redirects into Notify Assignee", () => {
    assert.doesNotMatch(
      ASSIGN, /pathname: "\/recording\/\[key\]\/task\/\[taskId\]\/notify"/,
      "picking an assignee must not push or replace into the notify screen"
    );
    assert.doesNotMatch(
      ASSIGN, /router\.replace\(/,
      "Assign To should pop back to the task, not replace the route"
    );
    assert.match(
      ASSIGN, /router\.back\(\)/,
      "a successful assignment returns to the task it came from"
    );
  });

  it("Task Detail still offers Notify Assignee, deliberately", () => {
    // The redirect was the ONLY route into notify.tsx. Deleting it without
    // this button would remove the capability rather than make it optional.
    assert.match(
      TASK_DETAIL, /label="Notify Assignee"/,
      "Task Detail must offer notifying as an explicit action"
    );
    assert.match(
      TASK_DETAIL,
      /pathname: "\/recording\/\[key\]\/task\/\[taskId\]\/notify"/,
      "that action must route to the notify screen"
    );
  });

  it("notifying is offered only for an assigned task", () => {
    // The channels are the assignee's contact details — there is nothing to
    // send when nobody owns the task.
    const idx = TASK_DETAIL.indexOf('label="Notify Assignee"');
    assert.ok(idx > 0);
    const before = TASK_DETAIL.slice(Math.max(0, idx - 400), idx);
    assert.match(
      before, /\{task\.assignee \? \(/,
      "the Notify Assignee button must be gated on task.assignee"
    );
  });
});
