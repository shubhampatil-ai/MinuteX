// lib/device/index.ts — the device layer's public surface.
//
// Import device types and the active adapter from here, never from
// esp32-adapter.ts directly. That keeps the choice of hardware to exactly one
// line in one file (getDeviceAdapter below), which is the whole point of the
// abstraction.

import type { DeviceAdapter } from "./types";
import { Esp32Adapter } from "./esp32-adapter";

export * from "./types";
export { Esp32Adapter, ESP32_CAPABILITIES, normalizeEsp32Status } from "./esp32-adapter";

// One adapter instance per app run. It owns the live device link, so it must be
// a singleton — a second instance would hold a second connection and the two
// would fight over the shared BLE manager's global scan state.
let adapter: DeviceAdapter | null = null;

/**
 * The adapter for the device family this build talks to.
 *
 * Adding a vendor later means implementing DeviceAdapter and selecting it
 * here (by build flavour, feature flag, or which device the user paired) —
 * nothing above this function changes.
 */
export function getDeviceAdapter(): DeviceAdapter {
  if (!adapter) adapter = new Esp32Adapter();
  return adapter;
}

/** Test/dev seam: swap the adapter (e.g. for a mock) before the provider
 *  mounts. Not used in production code paths. */
export function setDeviceAdapter(next: DeviceAdapter | null) {
  adapter = next;
}
