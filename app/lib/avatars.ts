// lib/avatars.ts — profile and contact photos: pick, upload, store.
//
// THE MODEL. A stored avatar is an S3 KEY, never a URL. Objects in the bucket
// stay private and the backend re-signs a short-lived GET on every read, which
// it returns as `avatar_view_url` on the user/contact. So:
//
//   render   avatar_view_url  (expires — never cache it in storage)
//   store    avatar_url       (the key, returned by requestAvatarUpload)
//
// THE FLOW is the same three steps the recording upload uses, for the same
// reason (see requestAvatarUpload in lib/api.ts): presign, PUT the bytes, then
// save the key. An upload that fails leaves an unreferenced S3 object rather
// than a profile pointing at bytes that never arrived.
//
// WHERE A PHOTO COMES FROM, and who owns it:
//   * The user picks one — their own profile, or a contact's.
//   * A phone-contact import carries the device address-book photo (see
//     ContactPickResult.photoUri), uploaded here at import time so it survives
//     across devices; the local content:// URI would not.
//   * A contact who is also a MinuteX user shows THEIR profile photo, resolved
//     server-side. Nothing in this file uploads or stores that — it is theirs,
//     it updates itself, and `avatar_source: "minutex"` is how the UI knows not
//     to offer a delete for it.
//
// expo-image-picker is a native module, loaded with the same defensive
// require() as expo-contacts in lib/contacts.ts: a build that did not bundle it
// degrades to "cannot choose a photo" instead of crashing on import. It was
// added after the last native build, so a JS-only reload will hit exactly that
// path until the dev client is rebuilt.
import { File, UploadType } from "expo-file-system";
import { ApiError, requestAvatarUpload } from "./api";

type PickerModule = typeof import("expo-image-picker");

function loadPicker(): PickerModule | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    return require("expo-image-picker") as PickerModule;
  } catch {
    return null;
  }
}
const Picker = loadPicker();

/** Whether a photo can be chosen at all in THIS build. Screens use it to hide
 * the affordance rather than show one that cannot work. */
export function canPickImage(): boolean {
  return !!Picker;
}

// The formats the backend accepts (AVATAR_FORMATS in the userApi). Anything
// else is converted by the picker before it gets here, but a file picked from a
// cloud provider can still arrive with an unexpected extension.
const ALLOWED_EXT = new Set(["jpg", "jpeg", "png", "webp", "heic"]);
const DEFAULT_EXT = "jpg";

// Matches MAX_AVATAR_BYTES in the userApi. Checked here too so an oversized
// pick fails immediately with a clear message instead of after the presign.
export const MAX_AVATAR_BYTES = 8 * 1024 * 1024;

/** The extension to upload under, derived from a local URI.
 *
 * Query strings and fragments are stripped first: an Android content:// URI can
 * carry them, and "jpg?width=320" is not a format the backend knows. Anything
 * unrecognised becomes jpg — the picker has already transcoded to a real image
 * by then, and the extension only has to be an honest label of the bytes. */
export function extFromUri(uri: string): string {
  const path = String(uri || "").split(/[?#]/)[0];
  const dot = path.lastIndexOf(".");
  if (dot < 0) return DEFAULT_EXT;
  const ext = path.slice(dot + 1).toLowerCase();
  return ALLOWED_EXT.has(ext) ? ext : DEFAULT_EXT;
}

export type PickOutcome =
  | { status: "picked"; uri: string; size?: number }
  | { status: "cancelled" }
  /** The OS dialog was declined but can be shown again. */
  | { status: "denied" }
  /** Declined permanently, or the module is missing — needs Settings, or a
   * build that includes expo-image-picker. */
  | { status: "blocked" }
  | { status: "error"; message: string };

// Square, modest resolution, re-encoded to JPEG. An avatar is rendered at ~72pt
// at most, so uploading a 12 MP original would cost the user's data for pixels
// nothing will ever display. allowsEditing gives the square crop the circular
// frame implies — without it the OS hands back the full frame and the circle
// crops it arbitrarily, usually off-centre.
const PICK_OPTIONS = {
  allowsEditing: true,
  aspect: [1, 1] as [number, number],
  quality: 0.8,
};

async function outcomeFromResult(
  result: { canceled: boolean; assets?: { uri: string; fileSize?: number }[] | null }
): Promise<PickOutcome> {
  if (result.canceled) return { status: "cancelled" };
  const asset = result.assets?.[0];
  if (!asset?.uri) return { status: "error", message: "No image was returned." };
  // fileSize is not reported on every platform; fall back to a stat so the size
  // guard is not silently skipped where the picker omits it.
  let size = asset.fileSize;
  if (size == null) {
    try {
      // File.size is 0 — not null — when the file cannot be read (SDK 57), so
      // a falsy value here means "unknown", not "an empty image". Treating 0 as
      // a real size would silently skip the ceiling check below.
      size = new File(asset.uri).size || undefined;
    } catch {
      size = undefined;
    }
  }
  if (size != null && size > MAX_AVATAR_BYTES) {
    return {
      status: "error",
      message: `That image is too large — the limit is ${
        Math.round(MAX_AVATAR_BYTES / (1024 * 1024))
      } MB.`,
    };
  }
  return { status: "picked", uri: asset.uri, size };
}

/** Choose an image from the photo library. */
export async function pickImageFromLibrary(): Promise<PickOutcome> {
  if (!Picker) return { status: "blocked" };
  try {
    const perm = await Picker.requestMediaLibraryPermissionsAsync();
    if (!perm.granted) {
      return { status: perm.canAskAgain ? "denied" : "blocked" };
    }
    const result = await Picker.launchImageLibraryAsync({
      ...PICK_OPTIONS,
      mediaTypes: ["images"],
    });
    return await outcomeFromResult(result);
  } catch (e) {
    return {
      status: "error",
      message: e instanceof Error ? e.message : "Could not open your photos.",
    };
  }
}

/** Take a new photo with the camera. */
export async function pickImageFromCamera(): Promise<PickOutcome> {
  if (!Picker) return { status: "blocked" };
  try {
    const perm = await Picker.requestCameraPermissionsAsync();
    if (!perm.granted) {
      return { status: perm.canAskAgain ? "denied" : "blocked" };
    }
    const result = await Picker.launchCameraAsync(PICK_OPTIONS);
    return await outcomeFromResult(result);
  } catch (e) {
    return {
      status: "error",
      message: e instanceof Error ? e.message : "Could not open the camera.",
    };
  }
}

/** Upload a local image and return the S3 KEY to store on the row.
 *
 * Does NOT save the key anywhere — the caller decides whether it belongs on a
 * user (updateMe) or a contact (updateContact/createContact), which is what
 * keeps an abandoned flow from half-writing a profile.
 *
 * The PUT goes through expo-file-system's native upload rather than fetch(),
 * for the reason documented at length in lib/uploads.tsx: React Native's fetch
 * cannot reliably turn a file:// URI into a body — on Android it throws or
 * sends zero bytes, so the object would never reach S3. No Content-Type header
 * is sent, because the backend deliberately leaves it out of the signature.
 */
export async function uploadAvatar(params: {
  uri: string;
  size?: number;
  scope?: "user" | "contact";
  contactId?: string;
}): Promise<string> {
  const { uri, size, scope = "user", contactId } = params;
  if (!uri) throw new ApiError(400, "No image to upload.");

  const ticket = await requestAvatarUpload({
    format: extFromUri(uri),
    scope,
    contact_id: scope === "contact" ? contactId : undefined,
    size,
  });

  const res = await new File(uri).upload(ticket.upload_url, {
    httpMethod: "PUT",
    uploadType: UploadType.BINARY_CONTENT,
  });
  if (res.status < 200 || res.status >= 300) {
    // S3 explains itself in an XML <Code> — surface it rather than a bare
    // status, so a signing or permission problem is diagnosable.
    const code = /<Code>([^<]+)<\/Code>/.exec(res.body ?? "")?.[1];
    throw new ApiError(
      res.status,
      `Could not upload the image (${res.status}${code ? `: ${code}` : ""}).`
    );
  }
  return ticket.key;
}

/** Upload a phone contact's address-book photo, returning the key or "".
 *
 * Best-effort ON PURPOSE, and the one place in this file that swallows an
 * error: this runs inside the import of a contact the user asked for. Failing
 * the whole import because a thumbnail would not upload would be the wrong
 * trade — they wanted the person, the picture is a bonus. The contact is
 * created without it and still shows initials.
 */
export async function uploadPhoneContactPhoto(
  photoUri: string | undefined
): Promise<string> {
  if (!photoUri) return "";
  try {
    // "contact" scope with no contactId: the contact does not exist yet, so
    // there is no id to pass. The backend allows that (the key is still
    // owner-scoped, which is what the ownership check reads) and POST /contacts
    // stores the returned key on the person it creates.
    return await uploadAvatar({ uri: photoUri, scope: "contact" });
  } catch (e) {
    if (__DEV__) console.warn("[avatars] phone photo upload failed:", e);
    return "";
  }
}
