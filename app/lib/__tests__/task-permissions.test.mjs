// lib/__tests__/task-permissions.test.mjs — the Task Details UI under the
// creator/assignee permission model.
//
// Run:  node --test lib/__tests__/task-permissions.test.mjs
//
// WHAT THIS PINS.
//
//   1. THE SERVER DECIDES, THE APP RENDERS. `permissions` is computed
//      backend-side and sent with the task. The app must READ it, never
//      re-derive it from owner/assignee ids — a second implementation of the
//      rule is a second thing that can disagree with the API, and the
//      disagreement would show as controls that appear then fail with 403.
//
//   2. HIDING IS NOT ENFORCEMENT. Every control gated here is also refused by
//      the backend (cloud/tests/test_task_permissions.py). These assertions
//      are about not OFFERING an action that would be refused; they are not
//      the security boundary, and this file would be worthless on its own.
//
//   3. THE ASSIGNEE KEEPS STATUS. Status is the one field they own, so the
//      status controls must NOT be gated — a permission model that locked the
//      assignee out of the single thing they are meant to do would be worse
//      than no model at all.
//
//   4. AN OLDER BACKEND STILL WORKS. A response with no `permissions` falls
//      back to CREATOR_PERMISSIONS, which is how every task behaved before
//      assignees could see anything. A missing field must not silently
//      read-only the screen for its own creator.
//
// Source assertions rather than a render harness: these are TSX modules and
// node --test runs .mjs with no transpile step — the convention
// task-provenance.test.mjs and notification-center.test.mjs follow.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

const read = (p) => readFileSync(join(ROOT, p), "utf8");

const API = read("lib/api.ts");
const DETAIL = read("src/app/task/[id].tsx");

describe("the permission contract crosses the wire", () => {
  it("TaskPermissions covers every action in the matrix", () => {
    for (const field of [
      "is_creator", "is_assignee", "can_view", "can_change_status",
      "can_edit_details", "can_change_deadline", "can_change_assignee",
      "can_resolve_assignment", "can_delete",
    ]) {
      assert.match(
        API, new RegExp(`${field}:\\s*boolean`),
        `TaskPermissions must carry ${field}`
      );
    }
  });

  it("TaskDetail carries the permissions the backend computed", () => {
    assert.match(
      API, /permissions\?:\s*TaskPermissions/,
      "TaskDetail must expose the server's permissions block"
    );
  });

  it("the fallback is creator-shaped, for a backend that predates this", () => {
    const block = API.slice(
      API.indexOf("CREATOR_PERMISSIONS"),
      API.indexOf("export type TaskDetail")
    );
    assert.match(block, /can_change_status:\s*true/);
    assert.match(block, /can_change_deadline:\s*true/);
    assert.match(block, /can_change_assignee:\s*true/);
    assert.match(block, /can_resolve_assignment:\s*true/);
  });
});

describe("Task Details renders by relationship", () => {
  it("reads permissions from the response rather than re-deriving them", () => {
    assert.match(
      DETAIL, /const perms = detail\.permissions \?\? CREATOR_PERMISSIONS/,
      "the screen must take the server's decision, with a safe fallback"
    );
  });

  it("assignee editing is gated on can_change_assignee", () => {
    assert.match(
      DETAIL, /\{confirmed && perms\.can_change_assignee && \(/,
      "Reassign must be creator-only"
    );
    assert.match(
      DETAIL, /\{perms\.can_change_assignee && \(\s*<Button\s*\n\s*label="Choose a Contact"/,
      "Choose a Contact must be creator-only"
    );
  });

  it("the deadline row is read-only without can_change_deadline", () => {
    assert.match(
      DETAIL,
      /onPress=\{perms\.can_change_deadline \? \(\) => setDueOpen\(true\) : undefined\}/,
      "the due-date row must not open the picker for an assignee"
    );
    assert.match(
      DETAIL, /disabled=\{busy \|\| !perms\.can_change_deadline\}/,
      "the due-date row must be disabled for an assignee"
    );
    // A row that cannot be tapped must not claim to be a button.
    assert.match(
      DETAIL, /accessibilityRole=\{perms\.can_change_deadline \? "button" : "text"\}/,
      "the read-only row must not announce itself as a button"
    );
  });

  it("AI assignment resolution is offered only to the creator", () => {
    assert.match(
      DETAIL,
      /needsAssigneeResolution\(task\) && perms\.can_resolve_assignment/,
      "an assignee must not be shown a resolution prompt they cannot act on"
    );
  });

  it("STATUS CONTROLS ARE NOT GATED — the assignee's one field", () => {
    // The status buttons must be reachable by both roles. If a future change
    // wraps them in a permission check, this fails loudly: locking the
    // assignee out of status would defeat the entire point of the model.
    const start = DETAIL.indexOf("{/* ---------------- STATUS ----------------");
    const end = DETAIL.indexOf("{/* ---------------- CONTEXT");
    assert.ok(start > 0 && end > start, "status section must exist");
    const section = DETAIL.slice(start, end);
    assert.match(section, /onPress=\{\(\) => setStatus\(s\)\}/);
    assert.doesNotMatch(
      section, /perms\.can_change_status \?|!perms\.can_change_status/,
      "status must stay available to the assignee"
    );
  });

  it("the assignee card is not a link when it carries no contact id", () => {
    // An assignee is shown THEMSELVES, built from the task row rather than
    // the creator's address book. There is no contact id to open, so the card
    // must not offer navigation that would 404.
    assert.match(
      DETAIL, /disabled=\{!contact\.id\}/,
      "the assignee card must be inert without a contact id"
    );
    assert.match(
      DETAIL, /accessibilityRole=\{contact\.id \? "button" : "text"\}/,
      "a non-navigating card must not announce itself as a button"
    );
  });

  it("the meeting row shows provenance without offering navigation", () => {
    // The backend sends an assignee the title and date but no audio key.
    assert.match(
      DETAIL, /disabled=\{!recording\.audio_s3_key\}/,
      "the meeting row must not be tappable without a key"
    );
    assert.match(
      DETAIL, /recording\.recorded_at/,
      "the meeting date is the other half of where this came from"
    );
  });

  it("shows an assignee who gave them the task", () => {
    assert.match(
      API, /assigned_by\?:\s*\{ name: string; avatar_view_url\?: string \}/,
      "TaskDetail must carry the assigner"
    );
    assert.match(
      DETAIL, /\{!!assignedBy\?\.name && \(/,
      "the Assigned by row must render when the backend sends one"
    );
  });

  it("explains the read-only controls to an assignee", () => {
    // Otherwise the screen just quietly stops responding, which reads as a
    // bug rather than a rule.
    assert.match(
      DETAIL, /\{!perms\.is_creator && perms\.is_assignee \? \(/,
      "the role note must show only for an assignee who is not the creator"
    );
  });
});
