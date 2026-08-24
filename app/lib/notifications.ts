// lib/notifications.ts — Android notification permission.
//
// Extracted from lib/ble.ts during the DeviceAdapter refactor: notification
// permission has nothing to do with the recorder or its transport, it was just
// living in the BLE module because the pairing screen asks for both at once.
// Keeping it here lets screens request it without importing anything device- or
// Bluetooth-specific. Behaviour is unchanged.
import { PermissionsAndroid, Platform } from "react-native";
import type { PermissionOutcome } from "./device/types";

export type { PermissionOutcome };

// Android 13+ notification permission (needs POST_NOTIFICATIONS in manifest;
// on older Android it is granted automatically).
export async function requestNotificationPermission(): Promise<boolean> {
  return (await requestNotificationPermissionDetailed()) === "granted";
}

export async function requestNotificationPermissionDetailed(): Promise<PermissionOutcome> {
  if (Platform.OS !== "android" || Platform.Version < 33) return "granted";
  try {
    const r = await PermissionsAndroid.request(
      PermissionsAndroid.PERMISSIONS.POST_NOTIFICATIONS
    );
    if (r === PermissionsAndroid.RESULTS.GRANTED) return "granted";
    // "blocked" = Android answered never_ask_again: the system dialog will NOT
    // appear again, so the only fix is the OS settings screen. UIs must branch
    // on this and offer an "Open settings" action instead of a dead re-request.
    if (r === PermissionsAndroid.RESULTS.NEVER_ASK_AGAIN) return "blocked";
    return "denied";
  } catch {
    return "denied";
  }
}

// CHECK without prompting (for initializing toggles from the real OS state).
export async function checkNotificationPermission(): Promise<boolean> {
  if (Platform.OS !== "android" || Platform.Version < 33) return true;
  try {
    return await PermissionsAndroid.check(PermissionsAndroid.PERMISSIONS.POST_NOTIFICATIONS);
  } catch {
    return false;
  }
}
