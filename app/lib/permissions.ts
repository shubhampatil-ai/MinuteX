// lib/permissions.ts — glue between in-app permission UI and the OS.
//
// Two production realities this file handles:
//  1. Once Android answers "never_ask_again" (or iOS denies), the system
//     dialog will never re-appear — the only path is the phone's Settings
//     app. openAppSettings() deep-links straight to MinuteX's settings page.
//  2. When the user flips a permission there and swipes back, the app gets
//     NO event — it just returns to foreground. useOnForeground() lets
//     screens re-check permission state at that exact moment, so pills and
//     toggles reflect reality without a manual refresh.
import { useEffect, useRef } from "react";
import { AppState, Linking, Platform } from "react-native";

/** Open this app's page in the OS Settings (Android app info / iOS Settings). */
export function openAppSettings(): void {
  Linking.openSettings().catch(() => {
    // Extremely old/locked-down builds — nothing more we can do.
  });
}

/** Run `cb` every time the app returns to the foreground. */
export function useOnForeground(cb: () => void): void {
  const cbRef = useRef(cb);
  cbRef.current = cb;
  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => {
      if (state === "active") cbRef.current();
    });
    return () => sub.remove();
  }, []);
}

// ---------------------------------------------------------------------------
// Location SERVICES (the phone's master GPS toggle) — distinct from the
// ACCESS_FINE_LOCATION *permission*.
//
// The "are they on?" probe is transport-specific (it detects the state from a
// BLE scan error code), so it lives behind the device adapter and is reached
// via useDevice().areLocationServicesEnabled(). What remains here is pure OS
// glue: the deep-link that lets the user fix it.

/** Android's location-source settings screen (the GPS master toggle). */
const ACTION_LOCATION_SOURCE_SETTINGS = "android.settings.LOCATION_SOURCE_SETTINGS";

/**
 * Open the phone's location-services screen so the user can flip GPS on
 * without leaving the flow. Falls back to this app's settings page if the
 * intent isn't available (non-Android, or an OEM build that blocks it).
 *
 * Pair with useOnForeground() to re-check state when the user swipes back.
 */
export function openLocationSettings(): void {
  if (Platform.OS !== "android") { openAppSettings(); return; }
  Linking.sendIntent(ACTION_LOCATION_SOURCE_SETTINGS).catch(() => {
    openAppSettings();
  });
}
