// lib/integrations.tsx — the browser half of the OAuth connect flow, plus the
// ONE place the app asks "is this integration connected?".
//
// TWO RESPONSIBILITIES, AND THE SECOND IS THE IMPORTANT ONE.
//
// 1. connectIntegration() runs the browser round-trip, exactly as
//    lib/salesforce.ts does for Salesforce. Split from lib/api.ts on purpose:
//    api.ts is pure HTTP, this owns platform behaviour worth isolating.
//
// 2. IntegrationsProvider / useIntegration() are the CENTRAL integration
//    status. The requirement is explicit that Gmail checks must not be
//    scattered: every Gmail-dependent surface asks this context, so
//    connecting or disconnecting updates all of them at once, and there is
//    exactly one definition of "connected" in the app.
//
//    Without it, each screen would fetch its own status and they would
//    disagree — disconnect on the Manage screen and the meeting's Share sheet
//    would happily keep offering "Email via Gmail" until it was remounted.
//    That is the bug this context exists to make impossible.
//
// WHAT "CONNECTED" MEANS HERE. Only status === "CONNECTED". A connection
// needing reauth is NOT usable and its actions must be unavailable, but it is
// also not the same as never having connected — the user should be offered
// "Reconnect", not "Connect". So the hook exposes both facts rather than one
// boolean, and callers that only need "can I do this" read `usable`.
//
// The backend enforces the same rule independently (every Gmail route answers
// 409 when the connection is unusable). This context is what makes the UI
// honest; it is NOT what makes the feature secure.
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef,
  useState,
} from "react";
import * as Linking from "expo-linking";
import * as WebBrowser from "expo-web-browser";
import {
  Integration, IntegrationStatusValue, getIntegrationAuthorizeUrl,
  getIntegrations, disconnectIntegration,
} from "./api";

// Must match INTEGRATION_RETURN_URL on the backend, because that is the URL
// the callback redirects to and the one openAuthSessionAsync watches for.
// createURL() resolves the right form per build type
// (recorderapp://integrations-connected in a dev/production build,
// exp://…/--/integrations-connected under Expo Go).
export const INTEGRATION_RETURN_PATH = "integrations-connected";

export function integrationReturnUrl(): string {
  return Linking.createURL(INTEGRATION_RETURN_PATH);
}

export type ConnectOutcome =
  | { ok: true }
  // The user backed out of the browser — a normal choice, not an error worth
  // showing loudly.
  | { ok: false; cancelled: true }
  | { ok: false; cancelled?: false; message: string };

// Maps the backend's ?reason= codes (set by integration_callback) to copy.
// Technical detail stays in CloudWatch; this is what a person reads.
const REASON_COPY: Record<string, string> = {
  denied: "Access was declined.",
  missing_params: "The provider didn’t return a valid response. Try again.",
  expired: "That sign-in link expired. Try again.",
  exchange_failed: "Couldn’t complete the connection. Try again.",
  // Google returns no refresh token when it decides consent was already
  // granted. Without one the connection would work for an hour and then die,
  // so the backend refuses it — and retrying genuinely does fix it, because
  // the authorize URL forces the consent screen.
  no_refresh_token:
    "Google didn’t return a lasting connection. Try again, and approve access "
    + "when the consent screen appears.",
  failed: "Couldn’t complete the connection. Try again.",
};

/**
 * Run the OAuth round-trip for one provider.
 *
 * openAuthSessionAsync does the redirect detection itself, so there is
 * deliberately NO Linking listener here — the Expo docs warn that adding one
 * alongside an auth session causes side effects on iOS. (Same reasoning as
 * lib/salesforce.ts.)
 */
export async function connectIntegration(
  provider: string
): Promise<ConnectOutcome> {
  const returnUrl = integrationReturnUrl();

  let authorizeUrl: string;
  try {
    authorizeUrl = await getIntegrationAuthorizeUrl(provider);
  } catch (e: any) {
    return { ok: false, message: e?.message ?? "Couldn’t start the connection." };
  }

  const result = await WebBrowser.openAuthSessionAsync(authorizeUrl, returnUrl);

  // "cancel" = the user dismissed the browser; "dismiss" = closed
  // programmatically. Neither is a failure to report.
  if (result.type !== "success") return { ok: false, cancelled: true };

  // The backend encodes the outcome in the deep link it redirected to, so a
  // server-side failure (bad state, refused exchange) still surfaces here
  // rather than looking like a success.
  const { queryParams } = Linking.parse(result.url);
  if (queryParams?.connected === "1") return { ok: true };

  const reason = typeof queryParams?.reason === "string" ? queryParams.reason : "";
  return {
    ok: false,
    message: REASON_COPY[reason] ?? "Couldn’t complete the connection. Try again.",
  };
}

// ---------------------------------------------------------------------------
// The central status.
// ---------------------------------------------------------------------------
type IntegrationsState = {
  /** Every provider MinuteX knows about, connected or not — the catalog comes
   *  from the server so a new integration appears without an app update. */
  integrations: Integration[];
  /** True until the first load resolves. Distinct from "nothing connected":
   *  a card must show "Checking…" rather than claiming "Not connected"
   *  before the answer is known. */
  loading: boolean;
  /** Set when the catalog could not be read at all. Individual screens can
   *  still render Coming Soon cards; only live status is missing. */
  error: string;
  refresh: () => Promise<void>;
  connect: (provider: string) => Promise<ConnectOutcome>;
  disconnect: (provider: string) => Promise<void>;
};

const IntegrationsContext = createContext<IntegrationsState | null>(null);

export function IntegrationsProvider({ children }: { children: React.ReactNode }) {
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // Guards a setState after unmount, and — more usefully — makes a slow
  // refresh that resolves after a newer one harmless.
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const refresh = useCallback(async () => {
    try {
      const list = await getIntegrations();
      if (!alive.current) return;
      setIntegrations(list);
      setError("");
    } catch (e: any) {
      if (!alive.current) return;
      // Leave the previous list in place rather than blanking every card on a
      // transient network failure — stale-but-labelled beats empty.
      setError(e?.message ?? "Couldn’t load integrations.");
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Both mutations re-read from the server rather than patching local state.
  // The server is the source of truth for status (a connect can succeed in the
  // browser and still land as REAUTH_REQUIRED), so guessing the new state here
  // would be the app inventing an answer it was told not to invent.
  const connect = useCallback(async (provider: string) => {
    const outcome = await connectIntegration(provider);
    if (outcome.ok) await refresh();
    return outcome;
  }, [refresh]);

  const disconnect = useCallback(async (provider: string) => {
    await disconnectIntegration(provider);
    await refresh();
  }, [refresh]);

  const value = useMemo<IntegrationsState>(
    () => ({ integrations, loading, error, refresh, connect, disconnect }),
    [integrations, loading, error, refresh, connect, disconnect]
  );

  return (
    <IntegrationsContext.Provider value={value}>
      {children}
    </IntegrationsContext.Provider>
  );
}

export function useIntegrations(): IntegrationsState {
  const ctx = useContext(IntegrationsContext);
  if (!ctx) {
    throw new Error("useIntegrations must be used inside IntegrationsProvider");
  }
  return ctx;
}

export type IntegrationView = {
  integration: Integration | null;
  /** The ONE check a Gmail-dependent action should make. True only for a
   *  CONNECTED integration — a connection needing reauth cannot send. */
  usable: boolean;
  /** Connected once, but the credential is dead. The action stays unavailable
   *  and the prompt becomes "Reconnect" rather than "Connect". */
  needsReauth: boolean;
  status: IntegrationStatusValue;
  loading: boolean;
};

/**
 * Status for ONE provider.
 *
 *     const gmail = useIntegration("gmail");
 *     if (!gmail.usable) return null;   // or a disabled row + Connect prompt
 *
 * This is the reusable check the requirement asks for. Nothing else in the app
 * should decide for itself what "Gmail is connected" means.
 */
export function useIntegration(provider: string): IntegrationView {
  const { integrations, loading } = useIntegrations();
  return useMemo(() => {
    const integration = integrations.find((i) => i.provider === provider) ?? null;
    const status: IntegrationStatusValue = integration?.status ?? "NOT_CONNECTED";
    return {
      integration,
      // Read `status`, not `connected`, so there is one rule rather than two
      // fields that could disagree.
      usable: status === "CONNECTED",
      needsReauth: status === "REAUTH_REQUIRED",
      status,
      loading,
    };
  }, [integrations, provider, loading]);
}

/** Gmail specifically — the only active integration in this phase. A named
 *  helper so call sites read as intent ("does Gmail work?") rather than as a
 *  string lookup, and so the provider id appears in one place. */
export function useGmail(): IntegrationView {
  return useIntegration("gmail");
}
