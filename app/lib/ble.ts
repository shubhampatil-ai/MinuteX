// lib/ble.ts — BLE manager for the AI Recorder (matches firmware v1.7)
// Place at: recorder-app/lib/ble.ts
import { BleManager, Device, Subscription } from "react-native-ble-plx";
import { PermissionsAndroid, Platform } from "react-native";
import { Buffer } from "buffer";
import type { PermissionOutcome } from "./device/types";

// Must match the firmware exactly:
export const SERVICE_UUID = "5f47a3c0-1e0b-4f8a-9a1d-8f0c2e7b4d10";
export const CONTROL_UUID = "5f47a3c1-1e0b-4f8a-9a1d-8f0c2e7b4d10";
export const STATUS_UUID  = "5f47a3c2-1e0b-4f8a-9a1d-8f0c2e7b4d10";

// The RAW ESP32 status payload, exactly as the firmware serializes it. Named
// Esp32Status (not DeviceStatus) to keep it clearly distinct from the
// normalized, vendor-independent DeviceStatus in lib/device/types.ts — this
// shape must not leak above lib/device/esp32-adapter.ts.
export type Esp32Status = {
  rec: 0 | 1;
  state: "idle" | "recording" | "saving" | "uploading" | "uploaded";
  wifi: 0 | 1;
  // New in firmware v1.8: real Wi-Fi provisioning feedback. Optional so older
  // firmware (which omits them) still parses.
  wifi_state?: "idle" | "connecting" | "connected" | "failed";
  wifi_ssid?: string;
  ip: string;
  batt: number;
  pend: number;
  fw: string;
};

const manager = new BleManager();

// startDeviceScan/stopDeviceScan are GLOBAL on the manager — a second caller
// starting or stopping a scan silently kills the first one's. This flag lets
// other modules (lib/permissions.ts's location probe) tell whether a real
// scan is already in flight and back off instead of stomping on it.
let scanning = false;
export function isScanning(): boolean {
  return scanning;
}

// ---- Bluetooth radio state ----
export async function isBluetoothOn(): Promise<boolean> {
  return (await manager.state()) === "PoweredOn";
}

// Fires cb immediately with current state and on every change ("PoweredOn"...)
export function onBluetoothState(cb: (on: boolean) => void) {
  return manager.onStateChange((s) => cb(s === "PoweredOn"), true);
}

// Shows the native "App wants to turn on Bluetooth" system dialog (Android).
// Requires BLUETOOTH_CONNECT to actually be GRANTED first (not just
// requested) — manager.enable() throws/no-ops otherwise on many devices.
export async function enableBluetooth(): Promise<boolean> {
  if (Platform.OS !== "android") return isBluetoothOn();
  try {
    if (Platform.Version >= 31) {
      const res = await PermissionsAndroid.request(PermissionsAndroid.PERMISSIONS.BLUETOOTH_CONNECT);
      if (res !== PermissionsAndroid.RESULTS.GRANTED) return isBluetoothOn();
    }
    await manager.enable();          // triggers the system popup
    // Some OEM builds resolve enable() before the radio has actually flipped
    // on — give it a beat and re-check the real state rather than trusting
    // the resolved promise alone.
    await new Promise((r) => setTimeout(r, 300));
    return isBluetoothOn();
  } catch {
    return isBluetoothOn();          // user declined (or already on)
  }
}

// ---- Permission outcomes ----
// Re-exported from the device layer so both transports describe permission
// results with one vocabulary. See lib/device/types.ts for the semantics of
// "blocked" (Android never_ask_again -> only the OS settings screen can fix it).
export type { PermissionOutcome } from "./device/types";

function outcomeOf(results: string[]): PermissionOutcome {
  if (results.every((v) => v === PermissionsAndroid.RESULTS.GRANTED)) return "granted";
  if (results.some((v) => v === PermissionsAndroid.RESULTS.NEVER_ASK_AGAIN)) return "blocked";
  return "denied";
}

// (Notification permission moved to lib/notifications.ts — see that file.)

// The runtime permissions BLE scanning needs on Android. On Android 12+ (API
// 31) BLUETOOTH_SCAN/CONNECT replace the old location requirement, but many
// devices still want FINE_LOCATION for scan results, so we keep it.
function blePermsList() {
  return [
    PermissionsAndroid.PERMISSIONS.BLUETOOTH_SCAN,
    PermissionsAndroid.PERMISSIONS.BLUETOOTH_CONNECT,
    PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION,
  ];
}

// CHECK permissions WITHOUT prompting. Use this on screen mount so we don't
// re-trigger the system dialog on every launch — only request when something
// is actually missing AND the user taps an explicit "Allow" action.
export async function checkBlePermissions(): Promise<boolean> {
  if (Platform.OS !== "android") return true;
  for (const p of blePermsList()) {
    // PermissionsAndroid.check never shows a dialog.
    // eslint-disable-next-line no-await-in-loop
    const ok = await PermissionsAndroid.check(p);
    if (!ok) return false;
  }
  return true;
}

// REQUEST permissions (shows the system dialog). Call this only from an
// explicit user action, not on mount. If already granted, requestMultiple
// returns "granted" silently without a popup.
export async function requestBlePermissions(): Promise<boolean> {
  return (await requestBlePermissionsDetailed()) === "granted";
}

// Detailed variant — callers that can deep-link to OS settings should use
// this so a permanently-denied permission gets the "Open settings" path
// instead of a request dialog that will never show.
export async function requestBlePermissionsDetailed(): Promise<PermissionOutcome> {
  if (Platform.OS !== "android") return "granted";
  try {
    const res = await PermissionsAndroid.requestMultiple(blePermsList());
    return outcomeOf(Object.values(res));
  } catch {
    return "denied";
  }
}

// Scan for our recorders: match by service UUID OR name prefix, because the
// ESP32 advertisement packet can't always fit both the 128-bit UUID and name.
// DEBUG VERSION: shows EVERY named BLE device + logs everything to Metro.
export function scanForRecorders(
  onFound: (d: Device) => void,
  timeoutMs = 15000,
  onTimeout?: () => void
): () => void {
  const seen = new Set<string>();
  let foundAny = false;
  console.log("SCAN: starting...");
  scanning = true;
  manager.startDeviceScan(null, { allowDuplicates: false }, (error, device) => {
    if (error) {
      console.log("SCAN ERROR:", error.message, (error as any).reason ?? "");
      scanning = false;
      manager.stopDeviceScan();
      return;
    }
    if (!device || seen.has(device.id)) return;
    seen.add(device.id);
    const name = device.name ?? device.localName ?? "(no name)";
    console.log("SCAN FOUND:", name, device.id, device.serviceUUIDs ?? []);
    if (name !== "(no name)") { foundAny = true; onFound(device); }
  });
  const t = setTimeout(() => {
    console.log("SCAN: timeout stop, foundAny=" + foundAny);
    scanning = false;
    manager.stopDeviceScan();
    if (!foundAny) onTimeout?.();
  }, timeoutMs);
  return () => { clearTimeout(t); scanning = false; manager.stopDeviceScan(); };
}

// Are location services (the phone's master GPS toggle) switched on?
//
// Why this lives here: on Android 11 and below, BLE scanning silently returns
// ZERO results when location services are off, even with every permission
// granted. The scan does not error — it just finds nothing, so the user sits on
// "Looking for your recorder..." forever with no idea why. Android 12+ avoids
// this only when the app declares neverForLocation, which we deliberately do
// NOT (see app.json's react-native-ble-plx config), so it applies on every
// version.
//
// Implemented via a throwaway BLE scan rather than expo-location: when services
// are off, Android's scan callback reports BleErrorCode 601
// (LocationServicesDisabled), which keeps the check dependency-free. Moved here
// from lib/permissions.ts during the DeviceAdapter refactor so the raw manager
// is not reached into from outside this module. Behaviour is unchanged.
//
// Returns true on iOS and whenever the state cannot be determined — callers
// must treat this as a HINT for showing a fix-it affordance, never as a hard
// gate, so an inconclusive answer cannot wedge the flow.
export async function areLocationServicesEnabled(): Promise<boolean> {
  if (Platform.OS !== "android") return true;
  try {
    // NEVER probe while a real scan is running: startDeviceScan/stopDeviceScan
    // are global on the manager, so probing here would tear down the pairing
    // screen's own scan mid-flight. A running scan is itself proof that
    // location services are on, so answer from that instead.
    if (scanning) return true;
    // The probe costs one immediate start/stop of the scanner; results are
    // never consumed.
    return await new Promise<boolean>((resolve) => {
      let settled = false;
      const done = (v: boolean) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { manager.stopDeviceScan(); } catch {}
        resolve(v);
      };
      // No callback at all within a beat means the scanner started fine.
      const timer = setTimeout(() => done(true), 700);
      try {
        manager.startDeviceScan(null, null, (error) => {
          done(!error || error.errorCode !== 601);
        });
      } catch {
        done(true);
      }
    });
  } catch {
    return true;
  }
}

// Negotiate a larger ATT MTU so the device's status JSON (~150 bytes, grew
// with the wifi_state/wifi_ssid fields) fits in ONE notification. At the
// default MTU (23) only ~20 bytes fit and the JSON arrives truncated →
// "Unexpected end of input" when parsing. 185 is a safe, widely-supported size.
async function negotiateMtu(d: Device): Promise<Device> {
  try {
    return await d.requestMTU(185);
  } catch {
    return d; // some platforms/devices don't allow it — fall back to default
  }
}

export async function connectToDevice(device: Device): Promise<Device> {
  const d = await manager.connectToDevice(device.id, { timeout: 10000 });
  await negotiateMtu(d);
  await d.discoverAllServicesAndCharacteristics();
  return d;
}

// Subscribe to STATUS notifications. The firmware pushes JSON on every state
// change (including physical button presses) — this is the two-way binding.
export function monitorStatus(
  device: Device,
  onStatus: (s: Esp32Status) => void
): Subscription {
  return device.monitorCharacteristicForService(
    SERVICE_UUID,
    STATUS_UUID,
    (error, characteristic) => {
      if (error) {
        console.log("STATUS notify ERROR:", error.message);
        return;
      }
      if (!characteristic?.value) {
        console.log("STATUS notify: empty value");
        return;
      }
      const json = Buffer.from(characteristic.value, "base64").toString("utf8");
      try {
        const parsed = JSON.parse(json);
        console.log("STATUS notify OK:", json);
        onStatus(parsed);
      } catch (e) {
        // DIAGNOSTIC (temporary): log the raw payload so we can see whether
        // it's truncated (MTU too small) or something else entirely, instead
        // of silently dropping it.
        console.log("STATUS notify BAD JSON (len=" + json.length + "):", JSON.stringify(json));
      }
    }
  );
}

// One-time read of current status (e.g. right after connecting).
export async function readStatus(device: Device): Promise<Esp32Status | null> {
  try {
    const c = await device.readCharacteristicForService(SERVICE_UUID, STATUS_UUID);
    if (!c.value) { console.log("readStatus: empty value"); return null; }
    const json = Buffer.from(c.value, "base64").toString("utf8");
    console.log("readStatus OK:", json);
    return JSON.parse(json);
  } catch (e: any) {
    console.log("readStatus FAILED:", e?.message ?? e);
    return null;
  }
}

async function writeCommand(device: Device, cmd: string) {
  const b64 = Buffer.from(cmd, "utf8").toString("base64");
  await device.writeCharacteristicWithResponseForService(
    SERVICE_UUID,
    CONTROL_UUID,
    b64
  );
}

export const startMeeting = (d: Device) => writeCommand(d, "START");
export const stopMeeting  = (d: Device) => writeCommand(d, "STOP");
export const getStatus    = (d: Device) => writeCommand(d, "GET_STATUS");
export const setWifi = (d: Device, ssid: string, pass: string) =>
  writeCommand(d, `SET_WIFI:${ssid}|${pass}`);
export const clearWifi = (d: Device) => writeCommand(d, "CLEAR_WIFI");

export function onDisconnected(device: Device, cb: () => void): Subscription {
  return manager.onDeviceDisconnected(device.id, () => cb());
}

// Connect with bounded retries + backoff. Rediscovers services each attempt.
export async function connectWithRetry(
  deviceId: string,
  attempts = 3
): Promise<Device> {
  // Android's BLE stack frequently throws GATT 133 ("operation failed") when
  // connectToDevice() is called immediately after stopDeviceScan() — the
  // radio hasn't finished tearing down the scan yet. Callers (e.g. pair.tsx)
  // stop the scan right before calling this, so give the stack a beat to
  // settle before the first connect attempt too, not just between retries.
  await new Promise((r) => setTimeout(r, 400));
  let lastErr: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      const d = await manager.connectToDevice(deviceId, { timeout: 10000 });
      await negotiateMtu(d);
      await d.discoverAllServicesAndCharacteristics();
      return d;
    } catch (e) {
      lastErr = e;
      // Backoff: 500ms, 1s, 1.5s…
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 500 * (i + 1)));
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error("connect failed");
}

// Auto-reconnect: when the device drops unexpectedly, keep trying to restore
// the link (until stop() is called). Calls onReconnect with the fresh Device
// on success and onLost each time the link goes down. Returns a stop handle.
export function autoReconnect(
  deviceId: string,
  onReconnect: (d: Device) => void,
  onLost?: () => void
): { stop: () => void } {
  let stopped = false;
  const sub = manager.onDeviceDisconnected(deviceId, async () => {
    if (stopped) return;
    onLost?.();
    try {
      const d = await connectWithRetry(deviceId, 5);
      if (!stopped) onReconnect(d);
    } catch {
      // Give up silently after retries; UI already showed "lost" via onLost.
    }
  });
  return {
    stop: () => { stopped = true; sub.remove(); },
  };
}

export default manager;