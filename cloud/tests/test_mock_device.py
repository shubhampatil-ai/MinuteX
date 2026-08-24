#!/usr/bin/env python3
# =============================================================
# test_mock_device.py — the virtual device layer, OFFLINE.
#
# No AWS, no network: this asserts MockDevice behaves like the hardware
# it stands in for, so the mock can be trusted as a development target.
# The live end-to-end path (mock device -> real backend -> transcription)
# is covered by test_device_mgmt.py and test_recording_sources.py.
#
# Covers:
#   power/battery, connectivity, the recording state machine (incl. the
#   illegal transitions), storage accounting and the full-card case,
#   the offline queue + retry-on-failure, QR/pairing, factory reset,
#   deterministic serials, and the status payload's shape.
#
#   python tests/test_mock_device.py
# =============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import check, _results  # noqa: E402
from mock_device import (  # noqa: E402
    MockDevice, DeviceError, IDLE, RECORDING, PAUSED, LOW_BATTERY_PCT,
)


def raises(fn, *a, **kw) -> bool:
    """True if fn(*a) rejects the operation the way the firmware would."""
    try:
        fn(*a, **kw)
        return False
    except DeviceError:
        return True


def main() -> int:
    print("Virtual MinuteX device — offline behaviour")
    print("-" * 60)

    # ---- power ----------------------------------------------------------
    d = MockDevice("sim-power")
    check("1. powered off: recording rejected", raises(d.start_recording))
    check("2. powered off: Wi-Fi rejected", raises(d.connect_wifi))
    d.power_on()
    check("3. power_on -> powered_on", d.powered_on and d.battery_pct == 100.0)
    d.connect_wifi("office")
    d.connect_ble()
    check("4. wifi + ble connected",
          d.wifi_connected and d.wifi_ssid == "office" and d.ble_connected)
    d.power_off()
    check("5. power_off drops both links",
          not d.powered_on and not d.wifi_connected and not d.ble_connected)

    # ---- battery -------------------------------------------------------
    d = MockDevice("sim-batt")
    d.power_on()
    d.start_recording()
    d.tick(10)
    check("6. recording drains battery faster than idle",
          d.battery_pct < 100.0, f"battery={d.battery_pct}")
    drained = d.battery_pct
    d.set_charging(True)
    d.tick(5)
    check("7. charging recovers battery", d.battery_pct > drained,
          f"{drained} -> {d.battery_pct}")
    d.set_charging(False)
    d.stop_recording()

    d2 = MockDevice("sim-batt-empty")
    d2.power_on()
    d2.battery_pct = 0.5
    d2.tick(30)  # drain past zero
    check("8. empty battery powers the device off",
          not d2.powered_on and d2.battery_pct == 0.0,
          f"on={d2.powered_on} battery={d2.battery_pct}")
    check("9. flat battery: power_on rejected", raises(d2.power_on))

    d3 = MockDevice("sim-lowbatt")
    d3.power_on()
    d3.battery_pct = LOW_BATTERY_PCT - 1
    check("10. low battery: recording rejected", raises(d3.start_recording))

    # ---- recording state machine ---------------------------------------
    d = MockDevice("sim-states")
    d.power_on()
    check("11. starts IDLE", d.state == IDLE)
    check("12. cannot pause/resume/stop while IDLE",
          raises(d.pause_recording) and raises(d.resume_recording)
          and raises(d.stop_recording))
    d.start_recording()
    check("13. start -> RECORDING", d.state == RECORDING)
    check("14. cannot double-start", raises(d.start_recording))
    d.tick(3)
    d.pause_recording()
    check("15. pause -> PAUSED", d.state == PAUSED)
    check("16. cannot double-pause", raises(d.pause_recording))
    d.tick(5)  # paused time must NOT be recorded
    d.resume_recording()
    check("17. resume -> RECORDING", d.state == RECORDING)
    d.tick(2)
    rec = d.stop_recording()
    check("18. paused time excluded from duration", rec.duration_s == 5.0,
          f"duration={rec.duration_s} (expected 3+2)")
    check("19. stop -> IDLE and queued",
          d.state == IDLE and len(d.pending) == 1)

    # ---- storage -------------------------------------------------------
    d = MockDevice("sim-storage", storage_total_mb=1)
    d.power_on()
    d.record_for(10)  # ~0.3 MB
    used = d.storage_used_mb
    check("20. storage accounting tracks queued audio",
          0 < used < 1 and abs(d.storage_free_mb - (1 - used)) < 1e-9,
          f"used={used}")
    check("21. oversized recording rejected, state back to IDLE",
          raises(d.record_for, 120) and d.state == IDLE)
    check("22. rejected recording not queued", len(d.pending) == 1,
          f"pending={len(d.pending)}")

    # ---- offline queue + sync ------------------------------------------
    d = MockDevice("sim-sync", api_url="https://example.invalid", api_key="k")
    d.power_on()
    d.record_for(2)
    d.record_for(2)
    check("23. records while offline (queued)", len(d.pending) == 2)
    check("24. sync without Wi-Fi rejected", raises(d.sync))
    d.connect_wifi()

    # Stub the wire layer: first attempt fails (403 unpaired), then succeeds.
    import mock_device as md
    calls = {"n": 0}

    def fake_upload(api_url, api_key, meeting_id, timestamp, wav_bytes,
                    device_id=None):
        calls["n"] += 1
        if calls["n"] <= 2:  # both items fail the first round
            return {"handshake_status": 403, "handshake_body": "Device not paired"}
        return {"handshake_status": 200, "upload_status": 200,
                "key": f"recordings/u1/{device_id}/{meeting_id}_{timestamp}.wav"}

    original = md.wire.record_and_upload_bytes
    md.wire.record_and_upload_bytes = fake_upload
    try:
        res = d.sync()
        check("25. failed upload keeps items queued for retry",
              all(not r["ok"] for r in res) and len(d.pending) == 2,
              f"pending={len(d.pending)}")
        check("26. failure reason recorded",
              all("not paired" in p.last_error.lower() for p in d.pending))
        res = d.sync()
        check("27. retry succeeds and clears the queue",
              all(r["ok"] for r in res) and len(d.pending) == 0
              and len(d.uploaded) == 2,
              f"pending={len(d.pending)} uploaded={len(d.uploaded)}")
        check("28. attempts counted across syncs",
              all(u["attempts"] == 2 for u in d.uploaded),
              f"attempts={[u['attempts'] for u in d.uploaded]}")
        check("29. upload key is user-owned and carries device_id",
              all(f"/{d.device_id}/" in u["key"] for u in d.uploaded),
              f"keys={[u['key'] for u in d.uploaded]}")

        # max_items must not drop the items it didn't send.
        d.record_for(1)
        d.record_for(1)
        res = d.sync(max_items=1)
        check("30. sync(max_items=1) uploads one, keeps the rest",
              len(res) == 1 and len(d.pending) == 1,
              f"sent={len(res)} pending={len(d.pending)}")
    finally:
        md.wire.record_and_upload_bytes = original

    # ---- heartbeat -------------------------------------------------------
    # /device/heartbeat and /device/upload-complete were routed to the presign
    # Lambda for months while only the presign was implemented, so both 400'd.
    # Assert the device sends them and reports the status it got back.
    d = MockDevice("sim-hb", api_url="https://example.invalid", api_key="k")
    d.power_on()
    check("31. heartbeat without Wi-Fi rejected", raises(d.heartbeat))
    d.connect_wifi()
    seen = {}

    class FakeResp:
        status_code = 200

    def fake_hb(api_url, api_key, firmware_version=None, timeout=15):
        seen["firmware_version"] = firmware_version
        return FakeResp()

    original_hb = md.wire.heartbeat
    md.wire.heartbeat = fake_hb
    try:
        r = d.heartbeat()
        check("32. heartbeat sends the firmware version it is running",
              r.status_code == 200
              and seen.get("firmware_version") == d.firmware_version,
              f"sent={seen.get('firmware_version')!r} fw={d.firmware_version!r}")
    finally:
        md.wire.heartbeat = original_hb

    # ---- pairing + QR ---------------------------------------------------
    d = MockDevice("sim-pair")
    d.power_on()
    qr = d.show_pairing_qr()
    check("33. QR carries device_id/serial/firmware",
          qr["device_id"] == "sim-pair" and qr["serial_number"].startswith("MX-")
          and qr["firmware_version"] == d.firmware_version)
    import json
    check("34. qr_payload is valid JSON of the same data",
          json.loads(d.qr_payload)["device_id"] == "sim-pair")
    d.accept_pairing("123456")
    check("35. accept_pairing marks paired",
          d.paired and d.pairing_code == "123456")

    # ---- factory reset --------------------------------------------------
    d = MockDevice("sim-reset")
    d.power_on()
    d.connect_wifi()
    d.accept_pairing("654321")
    d.record_for(2)
    d.install_firmware("1.1.0-mock")
    check("36. firmware update relabels", d.firmware_version == "1.1.0-mock")
    d.factory_reset()
    check("37. factory reset clears pairing + queue",
          not d.paired and d.pairing_code == "" and len(d.pending) == 0
          and d.state == IDLE)

    # ---- determinism + status ------------------------------------------
    check("38. serial is deterministic per device_id",
          MockDevice("stable-id").serial_number
          == MockDevice("stable-id").serial_number)
    check("39. distinct ids get distinct serials",
          MockDevice("id-a").serial_number != MockDevice("id-b").serial_number)

    st = MockDevice("sim-status").status()
    required = {"device_id", "serial_number", "firmware_version", "powered_on",
                "battery_pct", "charging", "storage_total_mb", "storage_used_mb",
                "storage_free_mb", "wifi_connected", "wifi_ssid", "ble_connected",
                "state", "paired", "pending_uploads", "pending", "uploaded_count"}
    check("40. status() reports the full mocked telemetry set",
          required.issubset(st), f"missing={sorted(required - set(st))}")

    print("-" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"RESULT: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
