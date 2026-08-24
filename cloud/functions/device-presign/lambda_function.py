"""getUploadUrl — the device-facing API (presign, heartbeat, upload-complete).

Runtime: Python 3.12   (boto3 is bundled in the Lambda runtime)

ROUTES (all authenticated with x-api-key; only the presign needs pairing):
  GET  /get-upload-url        -> presigned S3 PUT URL   (403 unless PAIRED)
  POST /device/heartbeat      -> last_seen (+ firmware_version) bump
  POST /device/upload-complete-> acknowledgement + last_seen bump

The API already routed all three here, but earlier builds implemented only
the presign, so heartbeat/upload-complete fell through and 400'd.

This is the user-owned successor to the original device-centric presign.
Two things changed; the wire contract did NOT.

  1. AUTHORIZATION. The device must be paired to a user. Resolve the key ->
     device_id (as before), then look the device up in Devices and require
     paired_user_id. An unpaired device gets 403 and cannot upload at all.
  2. S3 KEY. Recordings live under the OWNER, not the device:
        recordings/{user_id}/{device_id}/{recording_id}.wav
     (was: {device_id}/{meeting_id}_{timestamp}.wav)
     recording_id is "{meeting_id}_{timestamp}", matching userApi so a
     device recording and a phone recording are addressed the same way.

FIXED DEVICE CONTRACT (do not change — the firmware is built to this):
  GET /get-upload-url
    Header:  x-api-key: <device api key>
    Query:   ?meetingId=<id>&timestamp=<unix-or-string>
  x-device-id is OPTIONAL; when sent it must match the key (else 403).
  The device then PUTs the .wav with only Content-Type: audio/wav.

SECURITY MODEL:
  - device_id is resolved ONLY from the API key in DynamoDB, never trusted
    from the client. x-device-id is an assertion we VERIFY, not a source.
  - meetingId/timestamp pass a strict allow-list regex, so a value like
    "../../evil" can never escape the user's prefix.
  - user_id comes from the Devices row. A device whose owner unpaired stops
    being able to upload immediately, which is what makes "unpair keeps the
    recordings but cuts off the hardware" true.

SEAM (signed metadata): presigns with no Metadata, matching the firmware.
If metadata is re-added, pass it in Params["Metadata"] — boto3 signs those
headers, and the device must then send matching x-amz-meta-* headers.
"""
import json
import os
import re
import time

import boto3
from botocore.config import Config as _BotoConfig
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "ap-south-1")
BUCKET_NAME = os.environ.get("BUCKET_NAME")
TABLE_NAME = os.environ.get("TABLE_NAME", "DeviceKeys")
DEVICES_TABLE = os.environ.get("DEVICES_TABLE", "Devices")
URL_EXPIRY = int(os.environ.get("URL_EXPIRY", "900"))  # seconds

# Clients are created once per container (reused across invocations).
# Regional endpoint, NOT the legacy global one: a new bucket's global
# hostname resolves to us-east-1 until DNS propagates, and S3 then answers
# with a 307 that the ESP32 firmware treats as a fatal error.
_s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=_BotoConfig(s3={"addressing_style": "virtual"}, signature_version="s3v4"),
)
_ddb = boto3.resource("dynamodb", region_name=REGION)
_table = _ddb.Table(TABLE_NAME)
_devices = _ddb.Table(DEVICES_TABLE)

# Strict allow-list: alphanumeric, dash, underscore only. Anything with "/",
# "..", spaces, etc. is rejected — this is what stops path injection like
# meetingId="../../evil" from escaping the prefix.
SAFE = re.compile(r"^[A-Za-z0-9_-]+$")

# Reserved path segments for the non-device sources (userApi writes these);
# a device must never be able to write into them.
RESERVED_DEVICE_IDS = frozenset({"mobile", "uploads"})


def _json(status_code, body):
    """Build an API Gateway (HTTP API, payload v2) response."""
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _touch_last_seen(device_id: str) -> None:
    """Best-effort last_seen bump — an upload proves the device is alive.

    Never fails the request: the presign is the useful work here, and a
    throttled telemetry write must not stop a recording from uploading.
    """
    try:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression="SET last_seen = :now",
            ExpressionAttributeValues={":now": int(time.time())},
        )
    except Exception as err:  # noqa: BLE001
        print(f"last_seen update failed for {device_id}: {err!r}")


def _authenticate(event):
    """Resolve the caller's device_id from x-api-key.

    Returns (device_id, None) or (None, error_response). Shared by every
    device-facing route so they can never drift on authentication.
    """
    # API Gateway HTTP API lowercases header names in event["headers"].
    headers = (event or {}).get("headers") or {}
    api_key = headers.get("x-api-key") or headers.get("X-Api-Key")
    if not api_key:
        return None, _json(401, {"error": "missing x-api-key header"})

    item = _table.get_item(Key={"apiKey": api_key}).get("Item")
    if not item or not item.get("deviceId"):
        return None, _json(403, {"error": "invalid api key"})
    device_id = item["deviceId"]  # TRUSTED source of truth — from DB only.

    # Newer firmware also sends x-device-id; verify rather than trust it, so a
    # swapped/misprovisioned key is caught instead of silently writing another
    # device's recordings.
    asserted = headers.get("x-device-id") or headers.get("X-Device-Id")
    if asserted and asserted != device_id:
        print(f"x-device-id '{asserted}' != key owner '{device_id}'")
        return None, _json(403, {"error": "device id does not match api key"})

    # deviceId comes from our own DB, but guard it too so a malformed
    # provisioning record can never produce a weird key.
    if not SAFE.match(device_id) or device_id in RESERVED_DEVICE_IDS:
        print(f"deviceId '{device_id}' failed sanitization")
        return None, _json(500, {"error": "server misconfigured"})
    return device_id, None


def handle_heartbeat(event, device_id):
    """POST /device/heartbeat {firmware_version?} -> {ok, device_id}.

    Liveness ping. Works whether or not the device is paired: an UNPAIRED
    device still needs to report in (that is how the pairing screen can show
    "last seen just now" before anyone owns it).
    """
    body = {}
    raw = (event or {}).get("body")
    if raw:
        try:
            body = json.loads(raw) or {}
        except (TypeError, ValueError):
            return _json(400, {"error": "body must be JSON"})

    names = {"#ls": "last_seen"}
    values = {":now": int(time.time())}
    expr = "SET #ls = :now"
    fw = body.get("firmware_version")
    if fw is not None:
        fw = str(fw)[:64]
        names["#fw"] = "firmware_version"
        values[":fw"] = fw
        expr += ", #fw = :fw"
    try:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression=expr,
            # Don't resurrect a device that was deprovisioned: the row must
            # already exist (backfill/provisioning creates it).
            ConditionExpression="attribute_exists(device_id)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _json(404, {"error": "device not provisioned"})
        raise
    return _json(200, {"ok": True, "device_id": device_id})


def handle_upload_complete(event, device_id):
    """POST /device/upload-complete {key?} -> {ok, device_id}.

    The firmware calls this after a successful PUT. Transcription is driven by
    the S3 trigger, so there is nothing to kick off here — it exists so the
    device gets a definite acknowledgement and last_seen advances. Accepts and
    ignores an unknown body rather than failing the device's retry loop.
    """
    _touch_last_seen(device_id)
    return _json(200, {"ok": True, "device_id": device_id})


def handle_presign(event, device_id):
    """GET /get-upload-url — the presign path (see the module docstring)."""
    if not BUCKET_NAME:
        print("BUCKET_NAME env var is not set")
        return _json(500, {"error": "server misconfigured"})

    # --- Authorization: the device must be PAIRED to a user ---------------
    device = _devices.get_item(Key={"device_id": device_id}).get("Item")
    if not device:
        # Provisioned key with no Devices row — treat as unpaired, not as a
        # server error; the pairing flow creates/updates the row.
        print(f"no Devices row for '{device_id}'")
        return _json(403, {"error": "Device not paired"})
    user_id = device.get("paired_user_id")
    if not user_id:
        return _json(403, {"error": "Device not paired"})
    if not SAFE.match(str(user_id)):
        print(f"paired_user_id '{user_id}' failed sanitization")
        return _json(500, {"error": "server misconfigured"})

    # --- Read + sanitize query params -------------------------------------
    qs = (event or {}).get("queryStringParameters") or {}
    meeting_id = qs.get("meetingId")
    timestamp = qs.get("timestamp")

    if not meeting_id or not timestamp:
        return _json(400, {"error": "meetingId and timestamp query params are required"})
    if not SAFE.match(meeting_id) or not SAFE.match(timestamp):
        return _json(400, {
            "error": "meetingId and timestamp may contain only letters, digits, '-' and '_'",
        })

    # --- Build the user-owned S3 object key -------------------------------
    # recording_id matches userApi's convention so every source is addressed
    # identically downstream.
    recording_id = f"{meeting_id}_{timestamp}"
    key = f"recordings/{user_id}/{device_id}/{recording_id}.wav"

    # --- Presign an S3 PUT -------------------------------------------------
    # No Metadata — the device PUTs with only Content-Type: audio/wav.
    url = _s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": key,
            "ContentType": "audio/wav",
        },
        ExpiresIn=URL_EXPIRY,
    )

    _touch_last_seen(device_id)
    return _json(200, {"url": url, "key": key, "recording_id": recording_id})


# Device-facing routes. All three share x-api-key authentication; only the
# presign requires the device to be PAIRED (see handle_presign).
_ROUTES = {
    ("GET", "/get-upload-url"): handle_presign,
    ("POST", "/device/heartbeat"): handle_heartbeat,
    ("POST", "/device/upload-complete"): handle_upload_complete,
}


def lambda_handler(event, context):
    try:
        rc = (event or {}).get("requestContext") or {}
        http = rc.get("http") or {}
        method = http.get("method", "")
        route = (event or {}).get("routeKey", "")
        # routeKey is "METHOD /path"; fall back to the raw path for a direct
        # invoke or a REST-style event with no routeKey.
        path = route.split(" ", 1)[1] if " " in route else http.get("path", "")

        handler = _ROUTES.get((method, path))
        if handler is None:
            # Historically this Lambda served ONLY the presign, so an
            # unrecognised route still lands there rather than 404ing a
            # firmware build that predates the router.
            handler = handle_presign

        device_id, err = _authenticate(event)
        if err is not None:
            return err
        return handler(event, device_id)
    except Exception as err:  # noqa: BLE001
        print("Unhandled error:", repr(err))
        return _json(500, {"error": "internal error"})
