// lib/workspace-context.tsx — app-wide ACTIVE WORKSPACE state.
//
// Which workspace the user is looking at has to be shared across Desk,
// Record, Tasks, Contacts and the Organisation screens (expo-router mounts
// them separately), so it lives in a provider mounted above the tabs — the
// same shape device-context.tsx uses.
//
// WHAT THIS IS, AND WHAT IT IS NOT
//
//   It is UI/APPLICATION STATE. It decides which workspace the app ASKS
//   about, and it is persisted so a restart lands you back where you were.
//
//   It is NOT authorization, and nothing here is trusted by the server. The
//   id travels as a header (lib/api.ts attaches it at one choke point) and
//   the backend re-resolves membership and role from the database on EVERY
//   request. A tampered value produces a 404, not access. The `role` this
//   context exposes is only for hiding buttons the user cannot use — every
//   one of those actions is enforced again server-side.
//
// WHY THE LIST IS RE-FETCHED RATHER THAN CACHED HARD
//
//   Membership can be revoked at any time, and the backend applies that on
//   the very next request (it re-reads membership precisely because the
//   24h JWT cannot be revoked). So a stored workspace id is a HINT that
//   must be re-validated against a fresh /workspaces list, and when it no
//   longer appears the app falls back to Personal instead of showing a
//   screen full of 404s.
import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from "react";
import {
  ApiWorkspace,
  WorkspaceRole,
  getMe,
  getToken,
  getWorkspaces,
  getActiveWorkspaceId,
  peekWorkspaceFor,
  rememberWorkspaceFor,
  setActiveWorkspace as persistActiveWorkspace,
} from "./api";

type Ctx = {
  /** Every workspace the user may act in, personal first. */
  workspaces: ApiWorkspace[];
  /** The workspace the app is currently scoped to. Never null once loaded. */
  active: ApiWorkspace | null;
  /** Convenience: the active workspace's id, or "" while loading. */
  activeId: string;
  /** True while the active workspace is an organisation. */
  isOrganisation: boolean;
  /** The caller's role in the active workspace. Display gating ONLY. */
  role: WorkspaceRole | "";
  /** Display gating ONLY, exactly like `role` above — reads the
   *  `capabilities` map the backend already computes from the role
   *  (workspace_schema.capabilities_for) and sends on every workspace, so
   *  a capability name change on the server needs no matching change here.
   *  The backend re-checks the same capability on every mutating request;
   *  this only decides whether a button renders as usable. */
  can: (capability: string) => boolean;
  loading: boolean;
  /** Non-fatal: the last load error, for screens that want to show it. */
  error: string;
  /** Switch workspace. Persists, and is a no-op for an unknown id. */
  switchTo: (workspaceId: string) => Promise<void>;
  /** Re-read the list from the server (after creating or joining one). */
  refresh: () => Promise<void>;
};

const WorkspaceContext = createContext<Ctx | null>(null);

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [workspaces, setWorkspaces] = useState<ApiWorkspace[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  // WHOSE workspaces these are. Needed only so a switch can be remembered
  // under the right account (see rememberWorkspaceFor) — never for
  // authorization, which the server does on every request regardless.
  const userIdRef = useRef<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    // NO TOKEN, NO REQUEST. This provider is mounted ABOVE the auth gate, so
    // it also mounts on /login — where a fetch is guaranteed to 401 with
    // "missing bearer token". That 401 used to land in the catch below and
    // leave `workspaces` empty for the whole session, which is exactly how an
    // owner's organisation "disappeared" after signing back in: nothing
    // re-fetched once the token existed.
    //
    // Signed out is a STATE, not an error: clear the list so the next
    // account never sees the previous one's workspaces, and leave `error`
    // empty so /login does not render a failure the user cannot act on.
    if (!(await getToken())) {
      setWorkspaces([]);
      setActiveId("");
      userIdRef.current = "";
      setLoading(false);
      return;
    }
    try {
      const { workspaces: list, current_workspace_id } = await getWorkspaces();
      setWorkspaces(list);

      // WHOSE list this is, resolved BEFORE the workspace is chosen, because
      // the per-account memory below is keyed on it. Awaited deliberately:
      // the earlier fire-and-forget version made this whole feature a race —
      // it worked when /me happened to win and silently fell back to Personal
      // when it did not, which is exactly the "works once, then doesn't"
      // behaviour.
      if (!userIdRef.current) {
        try {
          userIdRef.current = (await getMe()).user_id || "";
        } catch {
          /* fall through: the live pointer below still scopes this session */
        }
      }

      // WHERE TO LAND. The live pointer is the first choice, but it is
      // module-level state that a sign-out sets to null, so after a
      // sign-out/sign-in cycle within one app run it can legitimately be
      // empty even though this account HAS a remembered workspace. Falling
      // back to that per-account memory is what makes the restore reliable
      // rather than dependent on which async path finished first.
      //
      // "" is a real answer meaning Personal (see rememberWorkspaceFor), so
      // it is distinguished from null, which means "nothing remembered".
      let stored = await getActiveWorkspaceId();
      if (!stored) {
        const remembered = await peekWorkspaceFor(userIdRef.current);
        if (remembered) stored = remembered;
      }
      // RE-VALIDATE the stored id against what the server just returned.
      // If the user was removed from an organisation while the app was
      // closed, the id is gone from the list and we must not keep sending
      // it — every request would 404. Fall back to the server's own answer
      // (which is the personal workspace when the hint is unusable).
      const stillValid = stored && list.some((w) => w.workspace_id === stored);
      const next = stillValid
        ? (stored as string)
        : (current_workspace_id
           || list.find((w) => w.is_personal)?.workspace_id
           || "");
      if (!stillValid && stored) {
        // Clear the dead hint so the next cold start does not retry it.
        await persistActiveWorkspace(null);
      } else if (stillValid) {
        // SYNC THE LIVE POINTER. `stored` may have come from the per-account
        // memory rather than the pointer, and the pointer is what request()
        // reads to attach the workspace header. Without this the UI would
        // show the organisation while every call was still scoped personal.
        // setActiveWorkspace maps a personal id to null on its own.
        await persistActiveWorkspace(next);
      }
      setActiveId(next);

      // RECORD WHERE THIS SESSION ACTUALLY LANDED, not just explicit
      // switches. A user who is already in their organisation may never tap
      // the switcher at all, and remembering only on switchTo would leave
      // them with nothing stored and drop them into Personal on their next
      // sign-in — the very bug this is meant to fix. Written in the
      // background so it never delays first paint.
      if (next && userIdRef.current) {
        const isPersonal = !!list.find(
          (w) => w.workspace_id === next && w.is_personal);
        await rememberWorkspaceFor(
          userIdRef.current, isPersonal ? null : next);
      }
    } catch (e: any) {
      // Leave whatever we had. A failed workspace list must not lock the
      // user out of the app — the personal path works with no header at all,
      // which is exactly what an empty activeId produces.
      setError(e?.message || "Could not load workspaces.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const switchTo = useCallback(async (workspaceId: string) => {
    // Only ever switch to something the server said we may act in. A caller
    // passing an arbitrary id would be refused by the backend anyway; this
    // keeps the UI from entering a state it cannot render.
    const target = workspaces.find((w) => w.workspace_id === workspaceId);
    if (!target) return;
    setActiveId(workspaceId);
    const value = target.is_personal ? null : workspaceId;
    await persistActiveWorkspace(value);
    // Remember it against the ACCOUNT too, so signing out and back in lands
    // here again instead of defaulting to Personal.
    //
    // RESOLVED ON DEMAND when the background /me above has not landed yet.
    // Without this the id is still "" for the first seconds after launch,
    // rememberWorkspaceFor no-ops on the empty id, and the switch is silently
    // forgotten — which is precisely the case that matters, since switching
    // workspace is one of the first things a user does after opening the app.
    let uid = userIdRef.current;
    if (!uid) {
      try {
        uid = (await getMe()).user_id || "";
        userIdRef.current = uid;
      } catch {
        // Genuinely offline: the live pointer is already persisted above, so
        // this session is correct; only the cross-sign-in memory is lost.
      }
    }
    await rememberWorkspaceFor(uid, value);
  }, [workspaces]);

  const value = useMemo<Ctx>(() => {
    const active =
      workspaces.find((w) => w.workspace_id === activeId)
      ?? workspaces.find((w) => w.is_personal)
      ?? null;
    const capabilities = active?.capabilities;
    return {
      workspaces,
      active,
      activeId: active?.workspace_id ?? "",
      isOrganisation: !!active && !active.is_personal,
      role: (active?.role ?? "") as WorkspaceRole | "",
      can: (capability: string) => !!capabilities?.[capability],
      loading,
      error,
      switchTo,
      refresh: load,
    };
  }, [workspaces, activeId, loading, error, switchTo, load]);

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace(): Ctx {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) {
    // A screen rendered outside the provider would otherwise silently act
    // personally, which is the ambiguous recording context the product
    // rules forbid. Fail loudly in development instead.
    throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  }
  return ctx;
}

/** The label the UI shows for a workspace: "Personal" or the org's name. */
export function workspaceLabel(w: ApiWorkspace | null): string {
  if (!w) return "Personal";
  return w.is_personal ? "Personal" : (w.name || "Organisation");
}

/** The icon token for a workspace, from the existing icon set. */
export function workspaceIcon(w: ApiWorkspace | null): string {
  return !w || w.is_personal ? "person.fill" : "building.2.fill";
}

/** Title-case a role for display ("MANAGER" -> "Manager"). */
export function roleLabel(role: string): string {
  const r = String(role || "").trim();
  if (!r) return "";
  return r.charAt(0) + r.slice(1).toLowerCase();
}
