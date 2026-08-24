// lib/device/types.ts — vendor-independent device abstraction for MinuteX.
//
// Everything above this file (DeviceProvider, screens) talks in these types
// only. Nothing here mentions BLE, GATT, UUIDs, ble-plx, or any ESP32 command
// string — that knowledge lives entirely inside a concrete adapter
// (lib/device/esp32-adapter.ts today; a vendor adapter later).

// ---------------------------------------------------------------------------
// Capabilities
// ---------------------------------------------------------------------------
// Not every recorder can do everything. The UI must branch on these flags
// rather than assuming a command exists — e.g. the current ESP32 has no
// pause/resume, while future vendor hardware may. A capability that is false
// means "calling the corresponding method throws UnsupportedCapabilityError".
export type DeviceCapabilities = {
  /** START / STOP recording commands. */
  startStopRecording: boolean;
  /** PAUSE / RESUME mid-recording. ESP32: false. */
  pauseResumeRecording: boolean;
  /** Reports a battery percentage in its status payload. */
  batteryStatus: boolean;
  /** Reports recording state (idle/recording/saving/uploading). */
  recordingStatus: boolean;
  /** Accepts Wi-Fi credentials over the control channel. */
  wifiProvisioning: boolean;
  /** Can enumerate recordings stored on the device. ESP32: false. */
  fileListing: boolean;
  /** Can transfer recording files to the phone over the device link.
   *  ESP32: false — it uploads to S3 over its own Wi-Fi instead. */
  fileTransfer: boolean;
  /** Firmware update over the device link. ESP32: false. */
  ota: boolean;
  /** Reports how much recording time has elapsed. ESP32: false — the app
   *  derives elapsed time itself from when START was accepted. */
  elapsedRecordingTime: boolean;
  /** Reports on-device storage figures. ESP32: false — it reports only a
   *  count of pending uploads, surfaced as DeviceStatus.pendingUploads. */
  storageInfo: boolean;
};

/** Every capability off — adapters spread this and enable what they support,
 *  so adding a new capability to the type can never silently read as
 *  "supported" in an existing adapter. */
export const NO_CAPABILITIES: DeviceCapabilities = {
  startStopRecording: false,
  pauseResumeRecording: false,
  batteryStatus: false,
  recordingStatus: false,
  wifiProvisioning: false,
  fileListing: false,
  fileTransfer: false,
  ota: false,
  elapsedRecordingTime: false,
  storageInfo: false,
};

/** Thrown by an adapter method whose backing capability flag is false. Callers
 *  that gate on capabilities never see this; it exists so an ungated call
 *  fails loudly instead of silently pretending to succeed. */
export class UnsupportedCapabilityError extends Error {
  readonly capability: keyof DeviceCapabilities;
  constructor(capability: keyof DeviceCapabilities, deviceKind = "This device") {
    super(`${deviceKind} does not support ${capability}.`);
    this.name = "UnsupportedCapabilityError";
    this.capability = capability;
  }
}

// ---------------------------------------------------------------------------
// Device handles
// ---------------------------------------------------------------------------
// A scan result. Screens render and re-select these, so they must NOT be a
// vendor object (ble-plx Device, a vendor SDK peripheral, ...). `id` is opaque
// and only meaningful to the adapter that produced it.
export type DiscoveredDevice = {
  id: string;
  name: string | null;
};

/** A device the adapter currently holds a live link to. Same shape as a scan
 *  result — screens only ever need id + name — kept as its own alias so intent
 *  reads clearly at call sites. */
export type ConnectedDevice = DiscoveredDevice;

// ---------------------------------------------------------------------------
// Normalized state
// ---------------------------------------------------------------------------
export type ConnectionState =
  | "disconnected"
  | "connecting"
  | "connected"
  | "reconnecting";

/** Where the device is in a recording cycle. "saving" and "uploading" are
 *  distinct because the ESP32 finalizes the file before it starts pushing it,
 *  and the UI reflects that difference. "paused" is unreachable on hardware
 *  without pauseResumeRecording. */
export type RecordingState =
  | "idle"
  | "recording"
  | "paused"
  | "saving"
  | "uploading";

/** Progress of the device's own network provisioning. "unknown" covers
 *  firmware that reports connected/not-connected without the intermediate
 *  states. */
export type WifiState =
  | "unknown"
  | "unprovisioned"
  | "connecting"
  | "connected"
  | "failed";

/** Normalized device status — the only status shape the app knows about.
 *  Every field beyond recordingState/isRecording/wifiState is optional: a
 *  device that cannot report something leaves it undefined rather than
 *  reporting a fake zero, so the UI can render "—" honestly. */
export type DeviceStatus = {
  recordingState: RecordingState;
  /** Convenience mirror of recordingState === "recording". */
  isRecording: boolean;
  /** 0-100, when batteryStatus is supported. */
  batteryLevel?: number;
  wifiState: WifiState;
  /** SSID the device is on / attempting, when the firmware reports it. */
  wifiSsid?: string;
  /** Recordings finished on the device but not yet uploaded by it. */
  pendingUploads?: number;
  /** Seconds since the current recording started, when the device reports it. */
  elapsedSeconds?: number;
  /** Firmware version string, for display only. */
  firmwareVersion?: string;
  /** The device's own network address, for display only. */
  ipAddress?: string;
  /** Free/total bytes, when storageInfo is supported. */
  storage?: { freeBytes?: number; totalBytes?: number };
};

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------
// Deliberately minimal: the provider needs exactly these signals to maintain
// its state, and nothing else in the app subscribes. This is one callback list,
// not an event bus.
export type DeviceEvent =
  /** A full status snapshot read from the device — replaces what came before. */
  | { type: "status"; status: DeviceStatus }
  /** A locally-derived partial update, merged over the last known status.
   *  Used when an adapter knows something changed (a command write was
   *  acknowledged) but cannot read a fresh snapshot to prove it. */
  | { type: "statusPatch"; patch: Partial<DeviceStatus> }
  | { type: "connected"; device: ConnectedDevice }
  | { type: "disconnected"; reason: "lost" | "requested" }
  | { type: "error"; error: Error };

export type DeviceEventListener = (event: DeviceEvent) => void;

/** Anything with a remove() — a scan handle, a subscription, a listener. */
export type Unsubscribe = { remove: () => void };

// ---------------------------------------------------------------------------
// The adapter contract
// ---------------------------------------------------------------------------
export type ScanOptions = {
  timeoutMs?: number;
  onTimeout?: () => void;
};

/**
 * One physical device family behind one interface.
 *
 * Contract notes an implementer must honour:
 * - Methods whose capability flag is false MUST throw UnsupportedCapabilityError.
 * - connect()/reconnect() resolve only once the link is usable (transport set
 *   up, services discovered, whatever the vendor requires) and reject on
 *   failure. Both are expected to retry internally.
 * - After a successful connect the adapter owns keeping status fresh and
 *   emitting "status" events, including across its own automatic reconnects.
 * - disconnect() is idempotent and must stop all internal timers/retries.
 */
export interface DeviceAdapter {
  /** Stable identifier for the device family, e.g. "esp32". Diagnostics only. */
  readonly kind: string;

  /** What this device family can do. Read by the UI before offering controls. */
  readonly capabilities: DeviceCapabilities;

  /** Subscribe to normalized device events. */
  addListener(listener: DeviceEventListener): Unsubscribe;

  // -- discovery --
  /** Start scanning. Returns a handle whose remove() stops the scan. */
  scan(onFound: (d: DiscoveredDevice) => void, options?: ScanOptions): Unsubscribe;
  /** Whether a scan started by this adapter is currently running. */
  isScanning(): boolean;

  // -- link lifecycle --
  connect(deviceId: string): Promise<ConnectedDevice>;
  /** Re-establish a link to a previously known device. */
  reconnect(deviceId: string): Promise<ConnectedDevice>;
  disconnect(): Promise<void>;
  /** The device currently linked, or null. */
  getConnectedDevice(): ConnectedDevice | null;

  // -- recording --
  startRecording(): Promise<void>;
  stopRecording(): Promise<void>;
  pauseRecording(): Promise<void>;
  resumeRecording(): Promise<void>;

  // -- state --
  /** Read current status from the device. Returns null if unreadable. */
  getStatus(): Promise<DeviceStatus | null>;
  /** Battery percentage, or null if unavailable. */
  getBattery(): Promise<number | null>;

  // -- provisioning --
  setWifi(ssid: string, password: string): Promise<void>;
  clearWifi(): Promise<void>;

  // -- radio / transport availability --
  // Not a "device capability" (it describes the phone, not the recorder), but
  // it is transport-specific, so it belongs behind the adapter rather than in
  // a screen that would otherwise have to know the transport is Bluetooth.
  /** Whether the underlying transport radio is currently usable. */
  isTransportReady(): Promise<boolean>;
  /** Fires immediately with the current value, then on every change. */
  onTransportStateChange(cb: (ready: boolean) => void): Unsubscribe;
  /** Ask the OS to enable the transport, if the platform allows it.
   *  Resolves with the resulting readiness. */
  enableTransport(): Promise<boolean>;
  /** Whether any OS-level prerequisite beyond permissions is satisfied — on
   *  Android BLE this is the master location/GPS toggle, without which scans
   *  silently return nothing. Adapters whose transport has no such
   *  prerequisite return true. Treat the answer as a HINT for showing a fix-it
   *  affordance, never as a hard gate: an inconclusive result must not be able
   *  to wedge the flow. */
  areScanPrerequisitesMet(): Promise<boolean>;

  // -- permissions --
  // Permission handling is platform + transport specific (Android
  // BLUETOOTH_SCAN/CONNECT, the "blocked" -> OS-settings path), so it stays
  // behind the adapter too. The outcome type is deliberately generic.
  /** Check without prompting — safe to call on mount. */
  checkPermissions(): Promise<boolean>;
  /** Request, showing the OS dialog. Call only from an explicit user action. */
  requestPermissions(): Promise<PermissionOutcome>;
}

/**
 * "blocked" = the OS will not show the request dialog again (Android's
 * never_ask_again), so the only remaining fix is the OS settings screen. UIs
 * must branch on this and offer an "Open settings" action rather than a dead
 * re-request.
 */
export type PermissionOutcome = "granted" | "denied" | "blocked";
