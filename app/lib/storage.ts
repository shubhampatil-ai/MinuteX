// lib/storage.ts — shared key/value persistence, graceful degradation.
//
// Preferred: expo-secure-store (encrypted, persistent). But that's a NATIVE
// module: it only exists if the dev-client / release build was compiled with
// it. In an older dev client it isn't present and importing it throws
// "Cannot find native module 'ExpoSecureStore'" at load time — which would
// crash the whole app before any screen renders.
//
// So we load it defensively. If it's available we use it (encrypted). If not,
// we fall back to an in-memory store: values survive for the current app
// session but not across a full restart. Once the app is rebuilt WITH
// expo-secure-store, this automatically upgrades to persistence — no code
// change needed.
//
// Extracted from lib/api.ts (originally just for the JWT) so lib/theme.tsx
// can persist the light/dark preference through the same proven fallback
// instead of duplicating it or pulling in a second storage library.
export type Store = {
  getItemAsync(k: string): Promise<string | null>;
  setItemAsync(k: string, v: string): Promise<void>;
  deleteItemAsync(k: string): Promise<void>;
};

function makeMemoryStore(): Store {
  const mem = new Map<string, string>();
  return {
    async getItemAsync(k) { return mem.has(k) ? (mem.get(k) as string) : null; },
    async setItemAsync(k, v) { mem.set(k, v); },
    async deleteItemAsync(k) { mem.delete(k); },
  };
}

function loadStore(): Store {
  try {
    // Require (not static import) so a missing native module doesn't crash
    // module load — we can catch it and fall back instead.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const SecureStore = require("expo-secure-store");
    // Touch a function to confirm the native module actually resolved.
    if (SecureStore && typeof SecureStore.setItemAsync === "function") {
      return SecureStore as Store;
    }
  } catch {
    // fall through to memory store
  }
  if (__DEV__) {
    console.warn(
      "[storage] expo-secure-store native module unavailable — using " +
      "in-memory storage (values won't persist across restarts). Rebuild " +
      "the dev client to enable persistence."
    );
  }
  return makeMemoryStore();
}

export const store: Store = loadStore();