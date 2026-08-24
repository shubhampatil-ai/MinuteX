// lib/device/esp32-adapter.ts — the ESP32 recorder behind the DeviceAdapter
// interface.
//
// This is the ONLY file above lib/ble.ts that knows the device is an ESP32.
// It owns:
//   - the raw ESP32 status payload shape and its translation to DeviceStatus
//   - which capabilities this hardware actually has
//   - the status-poll strategy this firmware requires
//   - holding the live ble-plx Device object so nothing above ever sees one
//
// The BLE mechanics themselves (service/characteristic UUIDs, command strings,
// MTU negotiation, the GATT-133 settle delay, connect retry/backoff,
// auto-reconnect, Android permissions) are unchanged and still live in
// lib/ble.ts — this adapter wraps them rather than reimplementing them, so
// ESP32 behaviour is preserved exactly.
import type { Device } from "react-native-ble-plx";
import {
  Esp32Status,
  areLocationServicesEnabled,
  autoReconnect,
  checkBlePermissions,
  clearWifi as bleClearWifi,
  connectWithRetry,
  enableBluetooth,
  isBluetoothOn,
  isScanning as bleIsScanning,
  monitorStatus,
  onBluetoothState,
  readStatus,
  requestBlePermissionsDetailed,
  scanForRecorders,
  setWifi as bleSetWifi,
  startMeeting,
  stopMeeting,
} from "../ble";
import {
  ConnectedDevice,
  DeviceAdapter,
  DeviceCapabilities,
  DeviceEvent,
  DeviceEventListener,
  DeviceStatus,
  DiscoveredDevice,
  NO_CAPABILITIES,
  PermissionOutcome,
  RecordingState,
  ScanOptions,
  UnsupportedCapabilityError,
  Unsubscribe,
  WifiState,
} from "./types";

// How often to re-read status over GATT. BLE notifications from this
// firmware/stack combo were found never to reach the app (zero "STATUS notify"
// logs even during an active recording where the firmware calls
// statusChar->notify() repeatedly); only the explicit read works. We subscribe
// anyway — it costs nothing and starts working if the firmware is ever fixed —
// but the poll is what actually keeps the UI in sync.
const STATUS_POLL_MS = 1500;

// The ESP32 does not report elapsed recording time, so nothing here derives it;
// see capabilities.elapsedRecordingTime.
export const ESP32_CAPABILITIES: DeviceCapabilities = {
  ...NO_CAPABILITIES,
  startStopRecording: true,
  batteryStatus: true,
  recordingStatus: true,
  wifiProvisioning: true,
  // Explicitly NOT supported by firmware v1.8:
  //   pauseResumeRecording — no PAUSE/RESUME command exists
  //   fileListing / fileTransfer — the device uploads to S3 over its own Wi-Fi
  //   ota — firmware is flashed over USB
  //   elapsedRecordingTime — the status payload carries no timer
  //   storageInfo — only a pending-upload count, exposed as pendingUploads
};

// --- ESP32 status payload -> normalized DeviceStatus -------------------------
// Raw shape (firmware v1.7/v1.8):
//   { rec: 0|1, state: "idle"|"recording"|"saving"|"uploading"|"uploaded",
//     wifi: 0|1, wifi_state?: "idle"|"connecting"|"connected"|"failed",
//     wifi_ssid?: string, ip: string, batt: number, pend: number, fw: string }

function toRecordingState(raw: Esp32Status): RecordingState {
  // `rec` is the authoritative flag — the app sets it optimistically on a
  // confirmed START write because this firmware can stall status reads for the
  // whole recording (see DeviceProvider). Trust it over `state` when they
  // disagree.
  if (raw.rec === 1) return "recording";
  switch (raw.state) {
    case "recording": return "recording";
    case "saving": return "saving";
    // "uploaded" is a terminal blip the firmware reports after a push
    // completes; for the UI that is simply back to idle.
    case "uploading": return "uploading";
    default: return "idle";
  }
}

function toWifiState(raw: Esp32Status): WifiState {
  // wifi_state is firmware v1.8+; older builds omit it and only set `wifi`.
  switch (raw.wifi_state) {
    case "connecting": return "connecting";
    case "connected": return "connected";
    case "failed": return "failed";
    case "idle": return raw.wifi === 1 ? "connected" : "unprovisioned";
    default: return raw.wifi === 1 ? "connected" : "unknown";
  }
}

/** Exported for unit-testing the mapping without a radio. */
export function normalizeEsp32Status(raw: Esp32Status): DeviceStatus {
  const recordingState = toRecordingState(raw);
  return {
    recordingState,
    isRecording: recordingState === "recording",
    batteryLevel: typeof raw.batt === "number" ? raw.batt : undefined,
    wifiState: toWifiState(raw),
    wifiSsid: raw.wifi_ssid || undefined,
    pendingUploads: typeof raw.pend === "number" ? raw.pend : undefined,
    firmwareVersion: raw.fw || undefined,
    ipAddress: raw.ip || undefined,
    // elapsedSeconds / storage: not reported by this firmware.
  };
}

function toDiscovered(d: Device): DiscoveredDevice {
  return { id: d.id, name: d.name ?? d.localName ?? null };
}

export class Esp32Adapter implements DeviceAdapter {
  readonly kind = "esp32";
  readonly capabilities = ESP32_CAPABILITIES;

  /** The live ble-plx handle. Private on purpose: this reference is exactly
   *  what the rest of the app must stop passing around. */
  private device: Device | null = null;
  private listeners = new Set<DeviceEventListener>();
  private statusSub: Unsubscribe | null = null;
  private reconnectHandle: { stop: () => void } | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  // Bumped on every command write. A poll read that was ALREADY IN FLIGHT when
  // a command fired carries a pre-command snapshot; applying it afterwards
  // clobbers the optimistic update (observed: the timer flips to recording for
  // a moment, then reverts, because a straggling read overwrote rec:1 with the
  // rec:0 it captured before START was sent). Each read is tagged with the
  // generation it started under and dropped if that generation is stale.
  private commandGeneration = 0;

  // --- events ---------------------------------------------------------------
  addListener(listener: DeviceEventListener): Unsubscribe {
    this.listeners.add(listener);
    return { remove: () => this.listeners.delete(listener) };
  }

  private emit(event: DeviceEvent) {
    this.listeners.forEach((l) => l(event));
  }

  // --- discovery ------------------------------------------------------------
  scan(onFound: (d: DiscoveredDevice) => void, options?: ScanOptions): Unsubscribe {
    const stop = scanForRecorders(
      (d) => onFound(toDiscovered(d)),
      options?.timeoutMs,
      options?.onTimeout,
    );
    return { remove: stop };
  }

  isScanning(): boolean {
    return bleIsScanning();
  }

  // --- link lifecycle -------------------------------------------------------
  async connect(deviceId: string): Promise<ConnectedDevice> {
    // connectWithRetry() carries the ESP32/Android-specific behaviour: the
    // 400ms settle delay that avoids GATT 133 right after a scan stop, MTU
    // negotiation so the status JSON fits one packet, service discovery, and
    // bounded retries with backoff. Unchanged.
    const d = await connectWithRetry(deviceId, 3);
    this.device = d;
    this.wire(d);
    this.emit({ type: "connected", device: toDiscovered(d) });
    return toDiscovered(d);
  }

  /** Same mechanics as connect() for this hardware — kept as its own method so
   *  a vendor whose reconnect path differs (bonded fast-reconnect, cached
   *  handles) can diverge without touching callers. */
  reconnect(deviceId: string): Promise<ConnectedDevice> {
    return this.connect(deviceId);
  }

  async disconnect(): Promise<void> {
    this.teardown();
    const d = this.device;
    this.device = null;
    if (d) {
      // Best-effort: the link may already be gone, which is not an error here.
      try { await d.cancelConnection(); } catch {}
    }
    this.emit({ type: "disconnected", reason: "requested" });
  }

  getConnectedDevice(): ConnectedDevice | null {
    return this.device ? toDiscovered(this.device) : null;
  }

  /** Attach status notify + poll + auto-reconnect to a freshly connected
   *  device. Called again after each successful automatic reconnect. */
  private wire(d: Device) {
    this.statusSub?.remove();
    this.statusSub = monitorStatus(d, (raw) => {
      this.emit({ type: "status", status: normalizeEsp32Status(raw) });
    });

    this.readAndEmitStatus(d);

    this.stopPolling();
    this.pollTimer = setInterval(() => {
      const generationAtRequest = this.commandGeneration;
      readStatus(d).then((raw) => {
        // A command was sent while this read was in flight, so `raw` predates
        // it — dropping it protects the optimistic update.
        if (generationAtRequest !== this.commandGeneration) return;
        if (raw) this.emit({ type: "status", status: normalizeEsp32Status(raw) });
      });
    }, STATUS_POLL_MS);

    this.reconnectHandle?.stop();
    this.reconnectHandle = autoReconnect(
      d.id,
      (fresh) => {
        this.device = fresh;
        this.wire(fresh);
        this.emit({ type: "connected", device: toDiscovered(fresh) });
      },
      () => {
        // The link just dropped. Stop polling the dead Device — readStatus()
        // on a disconnected device fails silently (by design in ble.ts), which
        // would leave the last status frozen and readable by the UI as
        // still-truth instead of "we lost the connection".
        this.stopPolling();
        this.emit({ type: "disconnected", reason: "lost" });
      },
    );
  }

  private readAndEmitStatus(d: Device) {
    readStatus(d).then((raw) => {
      if (raw) this.emit({ type: "status", status: normalizeEsp32Status(raw) });
    });
  }

  private stopPolling() {
    if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
  }

  private teardown() {
    this.reconnectHandle?.stop();
    this.reconnectHandle = null;
    this.statusSub?.remove();
    this.statusSub = null;
    this.stopPolling();
  }

  private requireDevice(): Device {
    if (!this.device) throw new Error("No device connected.");
    return this.device;
  }

  // --- recording ------------------------------------------------------------
  async startRecording(): Promise<void> {
    const d = this.requireDevice();
    this.commandGeneration++;   // invalidate any poll read already in flight
    await startMeeting(d);      // resolves only on a successful GATT write response
    // Optimistic status push. The firmware's I2S recording loop runs on the
    // same core as its BLE stack and blocks on i2s_channel_read() per audio
    // chunk, which starves the status-poll GATT reads for the ENTIRE duration
    // of a recording — readStatus() then fails silently and the UI would never
    // learn that recording started. The write response already confirms the
    // command reached the firmware, so report it now rather than waiting on a
    // poll that may not land until recording stops.
    this.emitOptimistic("recording");
  }

  async stopRecording(): Promise<void> {
    const d = this.requireDevice();
    this.commandGeneration++;
    await stopMeeting(d);
    this.emitOptimistic("saving");
  }

  /** Firmware v1.8 has no PAUSE command. */
  async pauseRecording(): Promise<void> {
    throw new UnsupportedCapabilityError("pauseResumeRecording", "The ESP32 recorder");
  }

  /** Firmware v1.8 has no RESUME command. */
  async resumeRecording(): Promise<void> {
    throw new UnsupportedCapabilityError("pauseResumeRecording", "The ESP32 recorder");
  }

  /** Emit a locally-derived recording state without a device read. Only the
   *  recording fields are asserted; the provider merges this patch over the
   *  last known status so battery/wifi/etc. are not blanked. */
  private emitOptimistic(state: RecordingState) {
    this.emit({
      type: "statusPatch",
      patch: { recordingState: state, isRecording: state === "recording" },
    });
  }

  // --- state ----------------------------------------------------------------
  async getStatus(): Promise<DeviceStatus | null> {
    if (!this.device) return null;
    const raw = await readStatus(this.device);
    return raw ? normalizeEsp32Status(raw) : null;
  }

  async getBattery(): Promise<number | null> {
    const s = await this.getStatus();
    return s?.batteryLevel ?? null;
  }

  // --- provisioning ---------------------------------------------------------
  async setWifi(ssid: string, password: string): Promise<void> {
    await bleSetWifi(this.requireDevice(), ssid, password);
  }

  async clearWifi(): Promise<void> {
    await bleClearWifi(this.requireDevice());
  }

  // --- transport ------------------------------------------------------------
  isTransportReady(): Promise<boolean> {
    return isBluetoothOn();
  }

  onTransportStateChange(cb: (ready: boolean) => void): Unsubscribe {
    return onBluetoothState(cb);
  }

  enableTransport(): Promise<boolean> {
    return enableBluetooth();
  }

  /** On Android, BLE scanning finds nothing while the master location toggle
   *  is off — even with every permission granted. */
  areScanPrerequisitesMet(): Promise<boolean> {
    return areLocationServicesEnabled();
  }

  // --- permissions ----------------------------------------------------------
  checkPermissions(): Promise<boolean> {
    return checkBlePermissions();
  }

  requestPermissions(): Promise<PermissionOutcome> {
    return requestBlePermissionsDetailed();
  }
}
