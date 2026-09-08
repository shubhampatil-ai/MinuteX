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
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from "react";
import {
  ApiWorkspace,
  WorkspaceRole,
  getWorkspaces,
  getActiveWorkspaceId,
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const stored = await getActiveWorkspaceId();
      const { workspaces: list, current_workspace_id } = await getWorkspaces();
      setWorkspaces(list);

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
      }
      setActiveId(next);
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
    await persistActiveWorkspace(target.is_personal ? null : workspaceId);
  }, [workspaces]);

  const value = useMemo<Ctx>(() => {
    const active =
      workspaces.find((w) => w.workspace_id === activeId)
      ?? workspaces.find((w) => w.is_personal)
      ?? null;
    return {
      workspaces,
      active,
      activeId: active?.workspace_id ?? "",
      isOrganisation: !!active && !active.is_personal,
      role: (active?.role ?? "") as WorkspaceRole | "",
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
