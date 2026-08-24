#!/usr/bin/env python3
# =============================================================
# device_simulator.py — stand-in for the ESP32 firmware.
#
# This mimics the FIXED DEVICE CONTRACT exactly. It is the reference
# for the wire format the backend must accept. The real firmware is
# owned separately; keep it aligned to what this file does.
#
# Contract:
#   1. Handshake:
#        GET  {API_URL}/get-upload-url?meetingId=<id>&timestamp=<ts>
#        Header:  x-api-key: <device api key>
#      (deviceId is NOT sent — the backend resolves it from the key.)
#   2. Upload:
#        PUT  <presigned url>
#        Header:  Content-Type: audio/wav     (and nothing else)
#        Body:    the raw .wav bytes
#
# This module is imported by run_tests.py, and can also be run
# directly for a one-off real upload:
#
#   python tests/device_simulator.py --wav tests/sample.wav \
#          --meeting-id meeting-abc --timestamp 1721460000
# =============================================================
import argparse
import os
import time
from pathlib import Path

import requests


def get_upload_url(api_url: str, api_key: str, meeting_id: str, timestamp: str,
                   timeout: int = 15, device_id: str = None):
    """Handshake step. Returns the raw requests.Response.

    Mirrors the device: x-api-key header + query params, no body.
    device_id is OPTIONAL, like the firmware: legacy builds send only the
    key; newer builds also send x-device-id, which the backend verifies
    against the key (403 on mismatch).
    """
    endpoint = api_url.rstrip("/") + "/get-upload-url"
    headers = {"x-api-key": api_key} if api_key is not None else {}
    if device_id is not None:
        headers["x-device-id"] = device_id
    return requests.get(
        endpoint,
        headers=headers,
        params={"meetingId": meeting_id, "timestamp": timestamp},
        timeout=timeout,
    )


def heartbeat(api_url: str, api_key: str, firmware_version: str = None,
              timeout: int = 15):
    """POST /device/heartbeat — updates Devices.last_seen (+ firmware)."""
    endpoint = api_url.rstrip("/") + "/device/heartbeat"
    body = {}
    if firmware_version is not None:
        body["firmware_version"] = firmware_version
    return requests.post(
        endpoint,
        headers={"x-api-key": api_key} if api_key is not None else {},
        json=body,
        timeout=timeout,
    )


def upload_to_presigned(url: str, wav_bytes: bytes, timeout: int = 30):
    """Upload step. PUT with ONLY Content-Type: audio/wav.

    Deliberately sends no x-amz-meta-* headers, matching the device.
    """
    return requests.put(
        url,
        data=wav_bytes,
        headers={"Content-Type": "audio/wav"},
        timeout=timeout,
    )


def record_and_upload_bytes(api_url: str, api_key: str, meeting_id: str,
                            timestamp: str, wav_bytes: bytes,
                            device_id: str = None):
    """Full device flow (handshake then upload) for in-memory audio.

    Returns a dict summary; a non-200 handshake short-circuits, so callers can
    distinguish an authorization failure (e.g. 403 unpaired) from an S3 error.
    """
    hs = get_upload_url(api_url, api_key, meeting_id, timestamp,
                        device_id=device_id)
    result = {"handshake_status": hs.status_code}
    if hs.status_code != 200:
        result["handshake_body"] = hs.text
        return result

    data = hs.json()
    result["url"] = data.get("url")
    result["key"] = data.get("key")

    up = upload_to_presigned(data["url"], wav_bytes)
    result["upload_status"] = up.status_code
    if up.status_code not in (200, 204):
        result["upload_body"] = up.text
    return result


def record_and_upload(api_url: str, api_key: str, meeting_id: str,
                      timestamp: str, wav_path: Path):
    """Full device flow for a .wav on disk."""
    return record_and_upload_bytes(api_url, api_key, meeting_id, timestamp,
                                   wav_path.read_bytes())


def _load_dotenv(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    _load_dotenv(project_root / ".env")

    p = argparse.ArgumentParser(description="Simulate the ESP32 device upload.")
    p.add_argument("--wav", default=str(Path(__file__).parent / "sample.wav"))
    p.add_argument("--meeting-id", default="meeting-abc")
    p.add_argument("--timestamp", default=str(int(time.time())))
    p.add_argument("--api-url", default=os.environ.get("API_URL", ""))
    p.add_argument("--api-key", default=os.environ.get("DEVICE_API_KEY", ""))
    args = p.parse_args()

    if not args.api_url:
        print("ERROR: API_URL not set (pass --api-url or set it in .env)")
        return 1
    if not args.api_key:
        print("ERROR: DEVICE_API_KEY not set (pass --api-key or set it in .env)")
        return 1

    res = record_and_upload(
        args.api_url, args.api_key, args.meeting_id, args.timestamp,
        Path(args.wav),
    )
    print("Result:")
    for k, v in res.items():
        print(f"  {k}: {v}")
    ok = res.get("handshake_status") == 200 and res.get("upload_status") in (200, 204)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
