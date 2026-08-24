#!/usr/bin/env python3
# =============================================================
# mock_device.py — stateful VIRTUAL MinuteX device.
#
# device_simulator.py is the wire-protocol reference (what bytes go
# on the network). This module is the DEVICE: it holds the state the
# ESP32 will hold — battery, storage, firmware, Wi-Fi/BLE links, a
# recording state machine and an offline upload queue — and drives
# the real backend through device_simulator's primitives.
#
# Everything here is mocked EXCEPT the network calls, which hit the
# live API. That is the point: the whole pipeline (pair -> record ->
# upload -> transcribe -> AI) can be validated before hardware.
#
# Battery/storage/BLE/Wi-Fi are simulated deterministically (no
# wall-clock, no RNG unless seeded) so tests are reproducible.
#
# Typical use:
#
#   dev = MockDevice("esp32-sim-01", api_url=..., api_key=...)
#   dev.power_on(); dev.connect_wifi()
#   code = dev.show_pairing_qr()            # what the app scans
#   dev.start_recording(); dev.stop_recording(wav_bytes)
#   dev.sync()                              # flush the queue to S3
#
# CLI (one-shot end-to-end upload as a device would do it):
#
#   python tests/mock_device.py --wav tests/sample.wav
# =============================================================
import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import device_simulator as wire  # noqa: E402  (wire-protocol primitives)

# --- Simulated hardware characteristics -------------------------------------
# Chosen to make limits reachable in a test without waiting: a full 8 GB card
# and a 0.01%/s drain would take hours to exercise the edges.
DEFAULT_FIRMWARE = "1.0.0-mock"
DEFAULT_STORAGE_MB = 64
BATTERY_DRAIN_IDLE = 0.05        # % per simulated second
BATTERY_DRAIN_RECORDING = 0.4    # % per simulated second (mic + SD active)
BATTERY_DRAIN_UPLOAD = 0.8       # % per simulated second (Wi-Fi radio)
LOW_BATTERY_PCT = 15.0           # below this the device refuses to record
WAV_BYTES_PER_SEC = 32000        # 16 kHz * 16-bit mono, matches the firmware

# Recording state machine.
IDLE = "IDLE"
RECORDING = "RECORDING"
PAUSED = "PAUSED"


class DeviceError(RuntimeError):
    """Raised for operations the real hardware would reject."""


@dataclass
class PendingUpload:
    """A recording sitting on the device's SD card, not yet in S3."""
    meeting_id: str
    timestamp: str
    wav_bytes: bytes = b""
    duration_s: float = 0.0
    attempts: int = 0
    last_error: str = ""

    @property
    def size_bytes(self) -> int:
        return len(self.wav_bytes)

    def summary(self) -> dict:
        return {"meeting_id": self.meeting_id, "timestamp": self.timestamp,
                "size_bytes": self.size_bytes, "duration_s": round(self.duration_s, 2),
                "attempts": self.attempts, "last_error": self.last_error}


@dataclass
class MockDevice:
    """A virtual MinuteX recorder.

    Mirrors the firmware's externally observable behaviour, including the
    failure modes worth testing: full storage, low battery, recording while
    offline (queue and sync later), and unpaired uploads being rejected by
    the backend.
    """
    device_id: str
    api_url: str = ""
    api_key: str = ""
    serial_number: str = ""
    firmware_version: str = DEFAULT_FIRMWARE

    # Simulated hardware state.
    powered_on: bool = False
    battery_pct: float = 100.0
    charging: bool = False
    storage_total_mb: int = DEFAULT_STORAGE_MB
    wifi_connected: bool = False
    wifi_ssid: str = ""
    ble_connected: bool = False
    state: str = IDLE

    # Pairing (device side of the flow — the app calls the HTTP endpoints).
    paired: bool = False
    pairing_code: str = ""

    # Offline queue + a log of what happened, for test assertions.
    pending: list = field(default_factory=list)
    uploaded: list = field(default_factory=list)
    events: list = field(default_factory=list)

    _recording_elapsed: float = 0.0

    def __post_init__(self):
        if not self.serial_number:
            # Derived from device_id so runs are repeatable. Deliberately NOT
            # hash() — that is salted per process, so serials would differ
            # between runs and break any test that pins one.
            digest = hashlib.sha256(self.device_id.encode("utf-8")).hexdigest()
            self.serial_number = f"MX-{int(digest[:8], 16) % 10**8:08d}"

    # -- plumbing ----------------------------------------------------------
    def _log(self, event: str, **fields):
        self.events.append({"event": event, **fields})
        return self.events[-1]

    def _require_on(self):
        if not self.powered_on:
            raise DeviceError("device is powered off")

    def _drain(self, seconds: float, rate: float):
        """Advance the simulated battery. Charging offsets drain."""
        if self.charging:
            self.battery_pct = min(100.0, self.battery_pct + 0.5 * seconds)
            return
        self.battery_pct = max(0.0, self.battery_pct - rate * seconds)
        if self.battery_pct == 0.0 and self.powered_on:
            self._log("battery_empty")
            self.power_off()

    # -- power / connectivity ---------------------------------------------
    def power_on(self):
        if self.battery_pct <= 0:
            raise DeviceError("battery empty — cannot power on")
        self.powered_on = True
        return self._log("power_on", battery=self.battery_pct)

    def power_off(self):
        # Losing power mid-recording keeps the partial file, like the firmware
        # (it flushes on every buffer), but drops the live links.
        if self.state in (RECORDING, PAUSED):
            self._log("power_off_during_recording", state=self.state)
        self.state = IDLE
        self.powered_on = False
        self.wifi_connected = False
        self.ble_connected = False
        return self._log("power_off")

    def connect_wifi(self, ssid: str = "mock-wifi"):
        self._require_on()
        self.wifi_connected = True
        self.wifi_ssid = ssid
        return self._log("wifi_connected", ssid=ssid)

    def disconnect_wifi(self):
        self.wifi_connected = False
        return self._log("wifi_disconnected")

    def connect_ble(self):
        """BLE is the app<->device link used for pairing and status."""
        self._require_on()
        self.ble_connected = True
        return self._log("ble_connected")

    def disconnect_ble(self):
        self.ble_connected = False
        return self._log("ble_disconnected")

    def set_charging(self, charging: bool):
        self.charging = charging
        return self._log("charging", charging=charging)

    # -- storage -----------------------------------------------------------
    @property
    def storage_used_mb(self) -> float:
        return sum(p.size_bytes for p in self.pending) / (1024 * 1024)

    @property
    def storage_free_mb(self) -> float:
        return max(0.0, self.storage_total_mb - self.storage_used_mb)

    # -- pairing (device side) --------------------------------------------
    def show_pairing_qr(self) -> dict:
        """What the device renders / advertises for the app to scan.

        The app scans this, calls POST /devices/pair-request, and the code the
        backend returns is what the device would display. Here the QR carries
        the device_id + serial; the 6-digit code arrives via accept_pairing().
        """
        self._require_on()
        payload = {"device_id": self.device_id, "serial_number": self.serial_number,
                   "firmware_version": self.firmware_version, "v": 1}
        self._log("pairing_qr_shown", **payload)
        return payload

    @property
    def qr_payload(self) -> str:
        """The QR's encoded contents (what a scanner returns as a string)."""
        return json.dumps(self.show_pairing_qr(), separators=(",", ":"))

    def accept_pairing(self, pairing_code: str):
        """Device 'displays' the code and confirms — the mocked firmware ack.

        Matches FirmwareVerifier.confirm_pairing() in functions/userapi, which
        auto-approves; this is the device end of that same seam.
        """
        self._require_on()
        self.pairing_code = str(pairing_code)
        self.paired = True
        return self._log("pairing_accepted", pairing_code=self.pairing_code)

    def factory_reset(self):
        """Wipe local state, as POST /devices/{id}/factory-reset asks it to.

        Queued recordings are lost (they only ever lived on the SD card);
        anything already in S3 belongs to the user and is untouched.
        """
        dropped = len(self.pending)
        self.pending.clear()
        self.paired = False
        self.pairing_code = ""
        self.state = IDLE
        self.wifi_ssid = ""
        return self._log("factory_reset", dropped_pending=dropped)

    # -- recording state machine ------------------------------------------
    def start_recording(self, meeting_id: str = None, timestamp: str = None):
        self._require_on()
        if self.state != IDLE:
            raise DeviceError(f"cannot start while {self.state}")
        if self.battery_pct < LOW_BATTERY_PCT:
            raise DeviceError("battery too low to record")
        if self.storage_free_mb <= 0:
            raise DeviceError("storage full")
        self._current = PendingUpload(
            meeting_id=meeting_id or f"meeting-{self.device_id}-{len(self.uploaded) + len(self.pending) + 1}",
            timestamp=timestamp or str(int(time.time())),
        )
        self.state = RECORDING
        self._recording_elapsed = 0.0
        return self._log("recording_started", meeting_id=self._current.meeting_id)

    def pause_recording(self):
        self._require_on()
        if self.state != RECORDING:
            raise DeviceError(f"cannot pause while {self.state}")
        self.state = PAUSED
        return self._log("recording_paused", elapsed_s=self._recording_elapsed)

    def resume_recording(self):
        self._require_on()
        if self.state != PAUSED:
            raise DeviceError(f"cannot resume while {self.state}")
        self.state = RECORDING
        return self._log("recording_resumed", elapsed_s=self._recording_elapsed)

    def tick(self, seconds: float = 1.0):
        """Advance simulated time. Only RECORDING accumulates audio."""
        if not self.powered_on:
            return None
        if self.state == RECORDING:
            self._recording_elapsed += seconds
            self._drain(seconds, BATTERY_DRAIN_RECORDING)
        else:
            self._drain(seconds, BATTERY_DRAIN_IDLE)
        return self._log("tick", seconds=seconds, state=self.state,
                         battery=round(self.battery_pct, 2))

    def stop_recording(self, wav_bytes: bytes = None):
        """Close the file and queue it. Returns the queued PendingUpload."""
        self._require_on()
        if self.state not in (RECORDING, PAUSED):
            raise DeviceError(f"cannot stop while {self.state}")
        rec = self._current
        rec.duration_s = self._recording_elapsed
        # Without real audio, synthesise a plausible payload size so storage
        # accounting behaves; tests that care about content pass wav_bytes.
        rec.wav_bytes = (wav_bytes if wav_bytes is not None
                         else b"\0" * int(self._recording_elapsed * WAV_BYTES_PER_SEC))
        if rec.size_bytes / (1024 * 1024) > self.storage_free_mb:
            self.state = IDLE
            self._current = None
            raise DeviceError("storage full — recording discarded")
        self.pending.append(rec)
        self.state = IDLE
        self._current = None
        self._recording_elapsed = 0.0
        self._log("recording_stopped", meeting_id=rec.meeting_id,
                  size_bytes=rec.size_bytes, duration_s=round(rec.duration_s, 2))
        return rec

    # -- upload / sync -----------------------------------------------------
    def sync(self, max_items: int = None) -> list:
        """Flush queued recordings to S3 through the real backend.

        Mirrors the firmware: needs Wi-Fi, uploads oldest-first, and KEEPS an
        item queued if it fails so the next sync retries it. A 403 (unpaired)
        is a real backend rejection surfacing here, not a simulated one.
        """
        self._require_on()
        if not self.wifi_connected:
            raise DeviceError("no Wi-Fi — cannot sync")
        if not self.api_url or not self.api_key:
            raise DeviceError("api_url/api_key not configured")

        results, remaining = [], []
        todo = self.pending if max_items is None else self.pending[:max_items]
        held = [] if max_items is None else self.pending[max_items:]

        for rec in todo:
            rec.attempts += 1
            res = wire.record_and_upload_bytes(
                self.api_url, self.api_key, rec.meeting_id, rec.timestamp,
                rec.wav_bytes, device_id=self.device_id,
            )
            self._drain(max(1.0, rec.duration_s / 10), BATTERY_DRAIN_UPLOAD)
            ok = (res.get("handshake_status") == 200
                  and res.get("upload_status") in (200, 204))
            entry = {**rec.summary(), **res, "ok": ok}
            results.append(entry)
            if ok:
                self.uploaded.append(entry)
                self._log("upload_ok", meeting_id=rec.meeting_id, key=res.get("key"))
            else:
                rec.last_error = str(res.get("handshake_body")
                                     or res.get("upload_body") or "unknown")
                remaining.append(rec)
                self._log("upload_failed", meeting_id=rec.meeting_id,
                          handshake_status=res.get("handshake_status"),
                          error=rec.last_error[:200])
        self.pending = remaining + held
        return results

    def heartbeat(self):
        """POST /device/heartbeat — refreshes Devices.last_seen."""
        self._require_on()
        if not self.wifi_connected:
            raise DeviceError("no Wi-Fi — cannot heartbeat")
        res = wire.heartbeat(self.api_url, self.api_key,
                             firmware_version=self.firmware_version)
        self._log("heartbeat", status=res.status_code)
        return res

    def install_firmware(self, version: str):
        """Mock OTA. Real updates reboot; this just relabels and reconnects."""
        self._require_on()
        old, self.firmware_version = self.firmware_version, version
        return self._log("firmware_updated", old=old, new=version)

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        """The payload a real device would report over BLE / heartbeat."""
        return {
            "device_id": self.device_id,
            "serial_number": self.serial_number,
            "firmware_version": self.firmware_version,
            "powered_on": self.powered_on,
            "battery_pct": round(self.battery_pct, 1),
            "charging": self.charging,
            "storage_total_mb": self.storage_total_mb,
            "storage_used_mb": round(self.storage_used_mb, 3),
            "storage_free_mb": round(self.storage_free_mb, 3),
            "wifi_connected": self.wifi_connected,
            "wifi_ssid": self.wifi_ssid,
            "ble_connected": self.ble_connected,
            "state": self.state,
            "paired": self.paired,
            "pending_uploads": len(self.pending),
            "pending": [p.summary() for p in self.pending],
            "uploaded_count": len(self.uploaded),
        }

    def record_for(self, seconds: float, wav_bytes: bytes = None,
                   meeting_id: str = None, timestamp: str = None):
        """Convenience: start -> tick(seconds) -> stop, in one call."""
        self.start_recording(meeting_id=meeting_id, timestamp=timestamp)
        self.tick(seconds)
        return self.stop_recording(wav_bytes=wav_bytes)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    wire._load_dotenv(project_root / ".env")

    p = argparse.ArgumentParser(description="Drive the virtual MinuteX device.")
    p.add_argument("--wav", default=str(Path(__file__).parent / "sample.wav"))
    p.add_argument("--device-id", default=os.environ.get("DEVICE_ID", "esp32-sim-01"))
    p.add_argument("--api-url", default=os.environ.get("API_URL", ""))
    p.add_argument("--api-key", default=os.environ.get("DEVICE_API_KEY", ""))
    p.add_argument("--meeting-id", default=None)
    p.add_argument("--seconds", type=float, default=5.0,
                   help="simulated recording length")
    p.add_argument("--offline-first", action="store_true",
                   help="record with Wi-Fi down, then connect and sync")
    args = p.parse_args()

    if not args.api_url or not args.api_key:
        print("ERROR: API_URL / DEVICE_API_KEY required (see .env)")
        return 1

    dev = MockDevice(args.device_id, api_url=args.api_url, api_key=args.api_key)
    dev.power_on()
    if not args.offline_first:
        dev.connect_wifi()

    wav = Path(args.wav)
    dev.record_for(args.seconds, wav_bytes=wav.read_bytes() if wav.is_file() else None,
                   meeting_id=args.meeting_id)
    print(f"queued: {len(dev.pending)} recording(s), "
          f"{dev.storage_used_mb:.3f} MB used")

    if args.offline_first:
        print("offline — connecting Wi-Fi now")
        dev.connect_wifi()

    results = dev.sync()
    print("sync results:")
    for r in results:
        print(f"  {r['meeting_id']}: ok={r['ok']} "
              f"handshake={r.get('handshake_status')} "
              f"upload={r.get('upload_status')} key={r.get('key')}")
    print("status:")
    for k, v in dev.status().items():
        if k != "pending":
            print(f"  {k}: {v}")
    return 0 if results and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
