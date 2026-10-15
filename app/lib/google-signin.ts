// lib/google-signin.ts — "Continue with Google", native side.
//
// Uses @react-native-google-signin/google-signin (the Original/free API): the
// OS account picker returns a Google ID TOKEN minted for our WEB OAuth client
// (webClientId), and the backend (POST /auth/google) verifies it and answers
// with our own JWT. Nothing Google-issued is stored on the device.
//
// LAZY NATIVE MODULE. The package is native code, so a binary built before it
// was added does not contain it, and a top-level import would crash the whole
// bundle on such a build. It is required on first use instead, and its absence
// makes isGoogleSignInAvailable() false — the button then says so honestly.
//
// CONFIG (EAS env / .env, public by nature — client ids are not secrets):
//   EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID   the web client; the token's `aud`
//   EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID   iOS only (app.config.js adds the URL scheme)
// Android needs no id here: Google matches the Android OAuth client by package
// name + signing-certificate SHA-1, registered in the Google Cloud console.
import { Platform } from "react-native";

const WEB_CLIENT_ID = process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID || "";
const IOS_CLIENT_ID = process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID || "";
type GoogleModule = typeof import("@react-native-google-signin/google-signin");

let mod: GoogleModule | null | undefined;
let configured = false;

function load(): GoogleModule | null {
  if (mod !== undefined) return mod;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    mod = require("@react-native-google-signin/google-signin") as GoogleModule;
  } catch {
    mod = null; // binary predates the native module
  }
  return mod;
}

function ready(): GoogleModule | null {
  const m = load();
  if (!m) return null;
  if (!configured) {
    m.GoogleSignin.configure({
      webClientId: WEB_CLIENT_ID,
      ...(Platform.OS === "ios" && IOS_CLIENT_ID ? { iosClientId: IOS_CLIENT_ID } : {}),
    });
    configured = true;
  }
  return m;
}

/** True when this build can run Google sign-in at all. */
export function isGoogleSignInAvailable(): boolean {
  if (Platform.OS === "web" || !WEB_CLIENT_ID) return false;
  if (Platform.OS === "ios" && !IOS_CLIENT_ID) return false;
  return load() !== null;
}

export class GoogleSignInError extends Error {}

/** Opens Google's account picker. Resolves the ID token, or null when the
 *  user backed out (not an error — nothing to show). */
export async function getGoogleIdToken(): Promise<string | null> {
  const m = ready();
  if (!m) throw new GoogleSignInError("Google sign-in isn't available in this version of the app.");
  const { GoogleSignin, statusCodes, isErrorWithCode, isSuccessResponse } = m;
  try {
    await GoogleSignin.hasPlayServices({ showPlayServicesUpdateDialog: true });
    const res = await GoogleSignin.signIn();
    if (!isSuccessResponse(res)) return null; // cancelled
    if (!res.data.idToken) throw new GoogleSignInError("Google didn't return a sign-in token. Please try again.");
    return res.data.idToken;
  } catch (e) {
    if (e instanceof GoogleSignInError) throw e;
    if (isErrorWithCode(e)) {
      if (e.code === statusCodes.SIGN_IN_CANCELLED) return null;
      if (e.code === statusCodes.IN_PROGRESS) return null;
      if (e.code === statusCodes.PLAY_SERVICES_NOT_AVAILABLE) {
        throw new GoogleSignInError("Google Play services is unavailable or out of date on this device.");
      }
    }
    // DEVELOPER_ERROR (code 10) on Android = this build's SHA-1/package isn't
    // registered on an Android OAuth client. A config fault, not the user's.
    console.warn("[google-signin] native error", e);
    throw new GoogleSignInError("Google sign-in failed. Please try again.");
  }
}

/** Forget the Google account picked last time, so the next "Continue with
 *  Google" shows the picker again. Best-effort; never throws. */
export async function googleSignOut(): Promise<void> {
  // Loaded on demand: after an app restart the module is not loaded yet, but
  // Google still remembers the account from the previous session.
  const m = isGoogleSignInAvailable() ? ready() : null;
  if (!m) return;
  try {
    await m.GoogleSignin.signOut();
  } catch {
    // nothing to do: a stale Google session only pre-selects an account
  }
}
