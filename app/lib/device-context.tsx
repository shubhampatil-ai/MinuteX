// lib/device-context.tsx — app-wide device state, vendor-independent.
//
// The connected device + its live status must be shared across Home, Devices,
// and Record screens (expo-router mounts them separately), so it lives in a
// React context provider mounted above the tabs.
//
// This layer holds NO hardware knowledge. It consumes a DeviceAdapter
// (lib/device) and re-publishes its normalized state to screens:
//
//     Screens -> DeviceProvider -> DeviceAdapter -> Esp32Adapter -> BLE
//
// Device-protocol behaviour (status polling, optimistic recording updates,
// auto-reconnect, MTU, GATT-133 handling) lives in the adapter; the concerns
// that remain here are React-shaped ones: which device is selected, how
// connection state transitions, and de-duplicating user-triggered commands.
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import {
  ConnectionState,
  DeviceCapabilities,
  DeviceStatus,
  DiscoveredDevice,
  PermissionOutcome,
  ScanOptions,
  Unsubscribe,
  getDeviceAdapter,
} from "./device";

type Ctx = {
  /** The connected device, or null. Vendor-neutral: { id, name }. */
  device: DiscoveredDevice | null;
  status: DeviceStatus | null;
  connState: ConnectionState;
  /** Whether the device transport (Bluetooth radio) is on and usable. */
  btOn: boolean;
  /** What the connected device family can do. Gate UI controls on this
   *  instead of assuming a command exists. */
  capabilities: DeviceCapabilities;

  // -- discovery / link --
  scan: (onFound: (d: DiscoveredDevice) => void, options?: ScanOptions) => Unsubscribe;
  /** Connect to a scanned device and start managing it. Throws on failure. */
  attach: (d: DiscoveredDevice) => Promise<void>;
  reconnectById: (id: string) => Promise<void>;
  disconnect: () => void;

  // -- recording --
  start: () => Promise<void>;
  stop: () => Promise<void>;
  /** Rejects with UnsupportedCapabilityError when
   *  capabilities.pauseResumeRecording is false (as on the ESP32). */
  pause: () => Promise<void>;
  resume: () => Promise<void>;

  // -- provisioning / state --
  sendWifi: (ssid: string, pass: string) => Promise<void>;
  clearWifi: () => Promise<void>;
  refreshStatus: () => Promise<void>;

  // -- transport + permissions --
  // Exposed here so screens never reach into the transport layer themselves.
  enableTransport: () => Promise<boolean>;
  /** OS prerequisites for scanning beyond permissions (Android: location
   *  services). A hint for showing a fix-it row, not a hard gate. */
  areScanPrerequisitesMet: () => Promise<boolean>;
  checkPermissions: () => Promise<boolean>;
  requestPermissions: () => Promise<PermissionOutcome>;
};

const DeviceCtx = createContext<Ctx | null>(null);

export function DeviceProvider({ children }: { children: React.ReactNode }) {
  const adapter = getDeviceAdapter();

  const [device, setDevice] = useState<DiscoveredDevice | null>(null);
  const [status, setStatus] = useState<DeviceStatus | null>(null);
  const [connState, setConnState] = useState<ConnectionState>("disconnected");
  const [btOn, setBtOn] = useState(false);

  // Transport (Bluetooth radio) availability.
  useEffect(() => {
    const sub = adapter.onTransportStateChange(setBtOn);
    adapter.isTransportReady().then(setBtOn);
    return () => sub.remove();
  }, [adapter]);

  // Normalized device events. The adapter owns the polling/notify/reconnect
  // machinery and reports through here; this effect only maps events to state.
  useEffect(() => {
    const sub = adapter.addListener((event) => {
      switch (event.type) {
        case "status":
          setStatus(event.status);
          break;
        case "statusPatch":
          // A locally-derived update (e.g. an acknowledged START write) merged
          // over the last snapshot, so battery/wifi/firmware are not blanked
          // by a change that only concerns recording state.
          setStatus((s) => (s ? { ...s, ...event.patch } : s));
          break;
        case "connected":
          setDevice(event.device);
          setConnState("connected");
          break;
        case "disconnected":
          if (event.reason === "lost") {
            // Keep `device` so the UI can still name what it is reconnecting
            // to; the adapter is already retrying.
            setConnState("reconnecting");
          } else {
            setDevice(null);
            setStatus(null);
            setConnState("disconnected");
          }
          break;
        case "error":
          console.log("DEVICE error:", event.error.message);
          break;
      }
    });
    return () => sub.remove();
  }, [adapter]);

  const scan = useCallback(
    (onFound: (d: DiscoveredDevice) => void, options?: ScanOptions) =>
      adapter.scan(onFound, options),
    [adapter],
  );

  // Take a SCANNED (advertising, not-yet-connected) device and establish the
  // link before marking it connected. connect() resolves only once the link is
  // usable, and throws on failure so the pairing screen can show the error.
  const attach = useCallback(async (d: DiscoveredDevice) => {
    setConnState("connecting");
    try {
      await adapter.connect(d.id);   // emits "connected" -> sets device/state
    } catch (e) {
      setConnState("disconnected");
      throw e;
    }
  }, [adapter]);

  const reconnectById = useCallback(async (id: string) => {
    setConnState("connecting");
    try {
      await adapter.reconnect(id);
    } catch {
      setConnState("disconnected");
    }
  }, [adapter]);

  const disconnect = useCallback(() => {
    // Fire-and-forget to keep the old synchronous signature its callers use;
    // the adapter emits "disconnected" when it has torn the link down, and
    // state is cleared here immediately so the UI responds to the tap at once.
    adapter.disconnect().catch(() => {});
    setDevice(null);
    setStatus(null);
    setConnState("disconnected");
  }, [adapter]);

  // Guards against sending a command twice for what the user experiences as
  // one action. Deliberately NOT a per-screen ref: expo-router's push() opens a
  // NEW instance of a route rather than reusing one, so if the record screen is
  // reachable from more than one entry point (Home's quick-record card AND the
  // tab bar's record button), a rapid double-tap across both can mount two
  // /record screens, each with its own ref, each unaware of the other's
  // in-flight command — exactly the failure that caused two START writes to
  // reach the firmware for a single user action. Living here on the shared
  // context makes it a single source of truth no matter how many screens are
  // mounted.
  const cmdInFlight = useRef(false);
  const runCommand = useCallback(async (fn: () => Promise<void>) => {
    if (cmdInFlight.current) return;
    cmdInFlight.current = true;
    try { await fn(); } finally { cmdInFlight.current = false; }
  }, []);

  const start = useCallback(async () => {
    if (!device) return;
    await runCommand(() => adapter.startRecording());
  }, [adapter, device, runCommand]);

  const stop = useCallback(async () => {
    if (!device) return;
    await runCommand(() => adapter.stopRecording());
  }, [adapter, device, runCommand]);

  // Present for future hardware. On the ESP32 these reject with
  // UnsupportedCapabilityError; the UI should gate on
  // capabilities.pauseResumeRecording rather than calling them blind.
  const pause = useCallback(() => adapter.pauseRecording(), [adapter]);
  const resume = useCallback(() => adapter.resumeRecording(), [adapter]);

  const sendWifi = useCallback(async (ssid: string, pass: string) => {
    if (device) await adapter.setWifi(ssid, pass);
  }, [adapter, device]);

  const clearWifi = useCallback(async () => {
    if (device) await adapter.clearWifi();
  }, [adapter, device]);

  const refreshStatus = useCallback(async () => {
    if (!device) return;
    const s = await adapter.getStatus();
    if (s) setStatus(s);
  }, [adapter, device]);

  const enableTransport = useCallback(() => adapter.enableTransport(), [adapter]);
  const areScanPrerequisitesMet = useCallback(
    () => adapter.areScanPrerequisitesMet(), [adapter]);
  const checkPermissions = useCallback(() => adapter.checkPermissions(), [adapter]);
  const requestPermissions = useCallback(() => adapter.requestPermissions(), [adapter]);

  return (
    <DeviceCtx.Provider value={{
      device, status, connState, btOn, capabilities: adapter.capabilities,
      scan, attach, reconnectById, disconnect,
      start, stop, pause, resume,
      sendWifi, clearWifi, refreshStatus,
      enableTransport, areScanPrerequisitesMet, checkPermissions, requestPermissions,
    }}>
      {children}
    </DeviceCtx.Provider>
  );
}

export function useDevice(): Ctx {
  const c = useContext(DeviceCtx);
  if (!c) throw new Error("useDevice must be used inside <DeviceProvider>");
  return c;
}
