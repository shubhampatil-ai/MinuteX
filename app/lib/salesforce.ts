// lib/salesforce.ts — the browser half of the Salesforce OAuth connect flow.
//
// Split from lib/api.ts on purpose: api.ts is pure HTTP, this owns the
// browser round-trip, which is the part with platform behaviour worth
// isolating.
//
// The flow (three parties, and the app is only two of the hops):
//   app  -> GET /crm/salesforce/connect        (bearer JWT) -> authorize_url
//   app  -> opens authorize_url in an auth session browser
//   user -> logs into Salesforce, approves
//   SF   -> 302 to our backend /crm/salesforce/callback?code&state
//   backend -> exchanges the code, stores the encrypted refresh token, then
//              302s to RETURN_URL (this app's deep link)
//   browser -> hits the deep link; openAuthSessionAsync resolves
//              {type:"success", url} and closes itself
//
// openAuthSessionAsync does the redirect detection itself, so there is
// deliberately NO Linking listener here — the Expo docs warn that adding one
// alongside an auth session causes side effects on iOS.
import * as Linking from "expo-linking";
import * as WebBrowser from "expo-web-browser";
import { getSalesforceAuthorizeUrl } from "./api";

// Must match SALESFORCE_RETURN_URL on the backend, because that is the URL
// the backend redirects to and the one openAuthSessionAsync watches for.
// createURL() resolves the right form per build type (recorderapp://crm-connected
// in a dev/production build, exp://…/--/crm-connected under Expo Go).
export const SALESFORCE_RETURN_PATH = "crm-connected";

export function salesforceReturnUrl(): string {
  return Linking.createURL(SALESFORCE_RETURN_PATH);
}

export type ConnectOutcome =
  | { ok: true }
  // The user backed out of the browser — not an error worth showing loudly.
  | { ok: false; cancelled: true }
  | { ok: false; cancelled?: false; message: string };

// Maps the backend's ?reason= codes (set by salesforce_callback) to copy.
const REASON_COPY: Record<string, string> = {
  denied: "Salesforce access was declined.",
  missing_params: "Salesforce didn’t return a valid response. Try again.",
  exchange_failed: "Couldn’t complete the Salesforce connection. Try again.",
  // The sign-in link went stale (its PKCE transaction is gone). Starting over
  // mints a fresh one, so "try again" is genuinely the fix.
  pkce_missing: "That Salesforce sign-in link expired. Try again.",
};

export async function connectSalesforce(): Promise<ConnectOutcome> {
  const returnUrl = salesforceReturnUrl();

  let authorizeUrl: string;
  try {
    authorizeUrl = await getSalesforceAuthorizeUrl();
  } catch (e: any) {
    return { ok: false, message: e?.message ?? "Couldn’t start the Salesforce connection." };
  }

  const result = await WebBrowser.openAuthSessionAsync(authorizeUrl, returnUrl);

  // "cancel" = user dismissed the browser; "dismiss" = closed programmatically.
  if (result.type !== "success") return { ok: false, cancelled: true };

  // The backend encodes the outcome in the deep link it redirected to, so a
  // failure that happened server-side (bad state, refused exchange) still
  // surfaces here rather than looking like a success.
  const { queryParams } = Linking.parse(result.url);
  const connected = queryParams?.connected;
  if (connected === "1") return { ok: true };

  const reason = typeof queryParams?.reason === "string" ? queryParams.reason : "";
  return {
    ok: false,
    message: REASON_COPY[reason] ?? "Couldn’t complete the Salesforce connection. Try again.",
  };
}
