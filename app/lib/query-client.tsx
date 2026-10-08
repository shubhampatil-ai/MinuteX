// lib/query-client.tsx — the on-device cache (local-first, phase 1).
//
// Screens that opt in read through TanStack Query: the last good answer is
// shown at once (also offline), the server is asked again in the
// background, identical requests in flight are merged into one, and a
// failed request backs off instead of being repeated immediately.
//
// Only queries with `meta: { persist: true }` are written to the phone
// (AsyncStorage). The cache is wiped on sign-out (onSignOut in api.ts), and
// every key carries the account and workspace (offline-cache-rules.ts).
//
// AsyncStorage and NetInfo are NATIVE modules: an older dev client built
// without them must not crash. They are loaded defensively, like
// lib/storage.ts does; without them the cache still works, in memory only.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { AppState } from "react-native";
import { QueryClient, QueryClientProvider, focusManager, onlineManager } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { createAsyncStoragePersister } from "@tanstack/query-async-storage-persister";
import { getActiveWorkspaceId, getToken, onSignOut } from "./api";
import { useWorkspace } from "./workspace-context";
import {
  CACHE_BUSTER, CACHE_MAX_AGE_MS, CACHE_STORAGE_KEY, FRESH_FOR_MS,
  retryDelay, shouldPersist, shouldRetry, userIdFromToken, type CacheScope,
} from "./offline-cache-rules";

type KV = {
  getItem(k: string): Promise<string | null>;
  setItem(k: string, v: string): Promise<void>;
  removeItem(k: string): Promise<void>;
};

function loadAsyncStorage(): KV | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require("@react-native-async-storage/async-storage");
    const s = (mod?.default ?? mod) as KV | undefined;
    return s && typeof s.getItem === "function" ? s : null;
  } catch {
    return null;
  }
}

/** Every call guarded: a storage failure costs the offline copy, never the app. */
function safeStorage(s: KV): KV {
  return {
    async getItem(k) { try { return await s.getItem(k); } catch { return null; } },
    async setItem(k, v) { try { await s.setItem(k, v); } catch { /* best effort */ } },
    async removeItem(k) { try { await s.removeItem(k); } catch { /* best effort */ } },
  };
}

const storage = (() => {
  const s = loadAsyncStorage();
  return s ? safeStorage(s) : null;
})();

const persister = storage
  ? createAsyncStoragePersister({ storage, key: CACHE_STORAGE_KEY, throttleTime: 1000 })
  : null;

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: FRESH_FOR_MS,
      gcTime: CACHE_MAX_AGE_MS,
      retry: shouldRetry,
      retryDelay,
      // Show the cached answer and try the network; offline, wait for it.
      networkMode: "offlineFirst",
      // Screens refresh on focus themselves (useFocusEffect); no double fetch.
      refetchOnWindowFocus: false,
    },
  },
});

// Online/offline from NetInfo, when the native module is there.
try {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const NetInfo = require("@react-native-community/netinfo").default;
  if (NetInfo?.addEventListener) {
    onlineManager.setEventListener((setOnline) =>
      NetInfo.addEventListener((s: { isConnected: boolean | null }) => setOnline(s.isConnected !== false)));
  }
} catch { /* old dev client: TanStack's default (always online) */ }

// Foreground/background from AppState.
focusManager.setEventListener((handleFocus) => {
  const sub = AppState.addEventListener("change", (s) => handleFocus(s === "active"));
  return () => sub.remove();
});

/** Drop every cached answer, in memory and on the phone. */
export async function wipeQueryCache(): Promise<void> {
  queryClient.clear();
  await persister?.removeClient();
}

// Signing out (any of the app's clearToken() paths) wipes the cache.
onSignOut(wipeQueryCache);

export function QueryProvider({ children }: { children: ReactNode }) {
  if (!persister) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return (
    <PersistQueryClientProvider client={queryClient}
      persistOptions={{
        persister, maxAge: CACHE_MAX_AGE_MS, buster: CACHE_BUSTER,
        dehydrateOptions: { shouldDehydrateQuery: shouldPersist },
      }}>
      {children}
    </PersistQueryClientProvider>
  );
}

/** Whose data the current screen shows: the account (from the token) and
 *  the workspace ("" = personal). Before the workspace list has loaded —
 *  or when it can't, offline — the workspace stored on the phone is used,
 *  so an offline launch still finds its cache. null until the account is
 *  known (nothing is read from the cache before then). */
export function useCacheScope(): CacheScope | null {
  const { active, loading } = useWorkspace();
  const [userId, setUserId] = useState<string | null>(null);
  const [stored, setStored] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    let live = true;
    getToken().then((t) => { if (live) setUserId(userIdFromToken(t)); }).catch(() => { if (live) setUserId(""); });
    getActiveWorkspaceId().then((w) => { if (live) setStored(w); }).catch(() => { if (live) setStored(null); });
    return () => { live = false; };
  }, []);
  const workspaceId = !loading && active
    ? (active.is_personal ? "" : active.workspace_id)
    : (stored || "");
  // A new object only when the account or workspace really changes, so a
  // workspace-list refresh doesn't re-run every screen's load.
  return useMemo(() => {
    if (userId === null || stored === undefined) return null;
    // An unreadable token still gets a scope (never "no data"); sign-out
    // wipes the cache either way.
    return { userId: userId || "unknown", workspaceId };
  }, [userId, stored, workspaceId]);
}
