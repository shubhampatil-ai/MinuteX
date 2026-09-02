"""pageindex_store.py — where a meeting's PageIndex tree lives.

WHY NOT ON THE RECORDING ROW
----------------------------
The reflex would be another map attribute on the recording item, next to
`documents` / `mom` / `tasks`. That is exactly the mistake transcript_store.py
was written to undo. A 2-hour meeting's tree is ~30 leaf nodes each carrying a
~180-char description plus timing and speaker lists — order 15-30 KB, and it
grows with meeting length on a row that already holds eight document types, a
chat history, a MoM structure and a task map, under DynamoDB's HARD 400 KB
ceiling. Adding a length-proportional attribute to that row reintroduces the
precise failure mode ("Item size to update has exceeded the maximum allowed
size", pipeline stranded mid-status) that offloading the transcript removed.

So the tree goes to S3, beside the transcript object it indexes, and the row
keeps a small POINTER — the same shape, and the same reasoning, as
transcript_store.

WHY THE METADATA IS ON THE ROW AND NOT A NEW TABLE
--------------------------------------------------
The pointer is five small scalars, it is only ever read together with the
recording, and it is never queried independently. A new DynamoDB table would
mean a second consistency problem (a row and an index record that can disagree
about which meeting they describe), an extra provisioning script, extra IAM,
and a second read on the chat path — to store less data than a task's title.
The metadata is a nested map attribute under one key instead, so it is stamped
in the same UpdateItem that already touches the row.

It is deliberately NOT surfaced by get_recording (Part 11): storage location and
build status are retrieval mechanics, and putting them in the public response
would make them part of an API contract the mobile app pins.

STALENESS
---------
The index is versioned by `transcript_fingerprint` — the SAME stamp the
document cache already keys on, so there is one answer in the system to "is
this derived thing still about the current transcript?". A tree whose
fingerprint no longer matches the row's is stale and is rebuilt; a tree whose
fingerprint matches is reused, however many times a meeting is opened.

What that buys, stated as the rule Part 19 asks for: a task's status, assignee,
deadline or priority changing does not touch the transcript, so it does not
change the fingerprint, so it does not rebuild anything. Neither does renaming
a speaker — the tree stores speaker IDS and the display name is resolved at
read time (see pageindex.table_of_contents). Only a re-transcription, which
rewrites the words, moves the fingerprint.

SINGLE-FLIGHT
-------------
Two people opening the same unindexed meeting at once would otherwise both
build it: two full passes, two S3 writes, and on the LLM-summary path two sets
of Groq calls racing the same quota. `claim()` is a conditional write that only
one caller can win, so the loser skips the build and falls back for that one
request. The claim EXPIRES (LOCK_TTL_SECONDS) rather than being held forever,
because a Lambda killed mid-build would otherwise lock the meeting out of ever
being indexed — a stuck lock must cost one wasted rebuild, never permanent
loss of the feature for that meeting.
"""

import gzip
import json
import os
import time
from datetime import datetime, timezone

import pageindex

# Beside the transcript, for the same debuggability reason transcript_store
# derives its key from the audio key: the object is findable from the recording
# key alone, and a rebuild overwrites in place instead of leaking one object per
# attempt.
PAGEINDEX_PREFIX = os.environ.get("PAGEINDEX_PREFIX", "pageindex")

# The row attribute holding the pointer + status. One nested map, so a stamp is
# a single SET and cannot half-apply.
PAGEINDEX_ATTR = "pageindex"

# How long a build claim stays valid. Comfortably longer than a build takes
# (deterministic builds are sub-second; the optional LLM-summary path is the
# slow one) and comfortably shorter than a user would tolerate the feature
# being unavailable after a crash.
LOCK_TTL_SECONDS = int(os.environ.get("PAGEINDEX_LOCK_TTL", "300"))

STATUS_READY = "ready"
STATUS_BUILDING = "building"
STATUS_FAILED = "failed"

_GZIP_LEVEL = 6


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def s3_key_for(audio_key):
    """The tree object's key for a given audio key."""
    return f"{PAGEINDEX_PREFIX}/{audio_key}.pageindex.json.gz"


def _dumps(tree):
    # ensure_ascii=False for the same reason transcript_store uses it: node
    # descriptions are extracted from the transcript, so a Devanagari meeting's
    # tree would otherwise inflate ~2x in \uXXXX escapes before gzip runs.
    raw = json.dumps(tree, ensure_ascii=False).encode("utf-8")
    return gzip.compress(raw, _GZIP_LEVEL)


def _loads(body):
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def meta(item):
    """The stored pointer/status map for a recording row, or {}."""
    got = (item or {}).get(PAGEINDEX_ATTR)
    return got if isinstance(got, dict) else {}


def is_fresh(item, fingerprint):
    """True when this row's stored index describes the CURRENT transcript.

    Three conditions, all required:
      * it finished (a `building` or `failed` record is not an index);
      * it was built from this exact transcript (the fingerprint rule above);
      * it was built by this code (PAGEINDEX_VERSION) — so changing how nodes
        are cut invalidates old trees WITHOUT having to disturb the
        fingerprint, which must keep meaning only "the words changed".

    A blank fingerprint returns False rather than matching a blank stored one:
    "we don't know what this was built from" is not freshness.
    """
    got = meta(item)
    if got.get("status") != STATUS_READY or not got.get("s3_key"):
        return False
    if not fingerprint or got.get("transcript_fingerprint") != fingerprint:
        return False
    return int(got.get("version") or 0) == pageindex.PAGEINDEX_VERSION


def _claim_is_live(got):
    """True while an in-flight build claim is still within its TTL."""
    if got.get("status") != STATUS_BUILDING:
        return False
    try:
        started = float(got.get("claimed_at_epoch") or 0)
    except (TypeError, ValueError):
        return False
    return (time.time() - started) < LOCK_TTL_SECONDS


def claim(table, key, fingerprint, owner_user_id="", force=False):
    """Try to become the one caller that builds this index. True if we won.

    A conditional UpdateItem: it succeeds only when there is no index record at
    all, or the one there is stale/failed/expired. Concurrent callers therefore
    serialise on DynamoDB's own conditional write — no extra lock table, no
    extra service, and nothing to clean up if the process dies (the TTL in the
    condition is what releases it).

    `force` skips only the FRESHNESS check, for the caller that has just tried
    to load a supposedly-ready index and found the object missing or corrupt.
    Without it that meeting could never recover: the pointer says ready, so the
    claim is refused, so nothing rebuilds, and every question falls back
    forever. The live-claim check is deliberately still honoured under force —
    another builder already working on it is exactly who should finish.

    Returns False on ANY failure, including the ConditionalCheckFailed that
    means "someone else is building". A caller that loses must degrade, not
    error: the index is an optimisation, and the fallback path still answers.
    """
    got = meta(table.get_item(Key={"audio_s3_key": key}).get("Item") or {})
    if _claim_is_live(got):
        return False
    if not force and is_fresh({PAGEINDEX_ATTR: got}, fingerprint):
        return False

    now = _now_iso()
    record = {
        "status": STATUS_BUILDING,
        "transcript_fingerprint": fingerprint or "",
        "version": pageindex.PAGEINDEX_VERSION,
        "owner_user_id": owner_user_id or "",
        "claimed_at": now,
        # Epoch alongside the ISO stamp: the TTL comparison is arithmetic, and
        # parsing an ISO string on every read to do it would be pure cost.
        "claimed_at_epoch": int(time.time()),
        "updated_at": now,
    }
    try:
        table.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #pi = :rec",
            ExpressionAttributeNames={"#pi": PAGEINDEX_ATTR},
            ExpressionAttributeValues={":rec": record},
            # The row must already exist. Creating one here would invent a
            # recording out of a stale key.
            ConditionExpression="attribute_exists(audio_s3_key)",
        )
        return True
    except Exception as err:  # noqa: BLE001
        print(f"[pageindex] claim failed for {key}: {err}")
        return False


def save(s3, bucket, table, key, tree, fingerprint, owner_user_id=""):
    """Persist the tree to S3, then stamp the pointer on the row.

    S3 BEFORE DynamoDB, the same ordering rule transcript_store documents: the
    two writes are not atomic, and an orphaned S3 object is harmless garbage
    whereas a row pointing at an object that was never written is an index that
    reads as ready and then produces nothing.

    Returns the stamped metadata, or None if it could not be stored — callers
    treat None as "no index", which is a state the whole feature already
    handles.
    """
    if not bucket:
        print(f"[pageindex] no bucket configured; not storing index for {key}")
        return None

    s3_key = s3_key_for(key)
    try:
        s3.put_object(
            Bucket=bucket, Key=s3_key, Body=_dumps(tree),
            ContentType="application/json", ContentEncoding="gzip",
        )
    except Exception as err:  # noqa: BLE001
        print(f"[pageindex] S3 put FAILED for {key}: {err}")
        mark_failed(table, key, fingerprint, str(err))
        return None

    now = _now_iso()
    record = {
        "status": STATUS_READY,
        "s3_key": s3_key,
        "transcript_fingerprint": fingerprint or "",
        "version": pageindex.PAGEINDEX_VERSION,
        "owner_user_id": owner_user_id or "",
        # Counts, not content — enough for a backfill script or an operator to
        # judge an index without paying for the S3 GET.
        "node_count": len(tree.get("nodes") or []),
        "segment_count": int(tree.get("segment_count") or 0),
        "created_at": now,
        "updated_at": now,
    }
    try:
        table.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #pi = :rec",
            ExpressionAttributeNames={"#pi": PAGEINDEX_ATTR},
            ExpressionAttributeValues={":rec": record},
            ConditionExpression="attribute_exists(audio_s3_key)",
        )
    except Exception as err:  # noqa: BLE001
        # The object IS written; only the pointer is missing. Harmless — the
        # next request finds no fresh index and rebuilds over the same key.
        print(f"[pageindex] pointer stamp FAILED for {key}: {err}")
        return None
    return record


def mark_failed(table, key, fingerprint, error=""):
    """Record that a build failed, so the state is visible and retryable.

    Stored rather than silently dropped because "failed" and "never attempted"
    look identical otherwise, and an operator needs to tell them apart. It does
    NOT block a retry: is_fresh() rejects a failed record, so the very next
    request is free to claim and rebuild.
    """
    now = _now_iso()
    try:
        table.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #pi = :rec",
            ExpressionAttributeNames={"#pi": PAGEINDEX_ATTR},
            ExpressionAttributeValues={":rec": {
                "status": STATUS_FAILED,
                "transcript_fingerprint": fingerprint or "",
                "version": pageindex.PAGEINDEX_VERSION,
                # Bounded: an exception's repr can be long, and this rides on a
                # row with a 400 KB ceiling.
                "error": str(error)[:500],
                "updated_at": now,
            }},
            ConditionExpression="attribute_exists(audio_s3_key)",
        )
    except Exception as err:  # noqa: BLE001
        print(f"[pageindex] mark_failed could not be written for {key}: {err}")


def load(s3, bucket, item):
    """The stored tree for a recording row, or None.

    Never raises. A missing or corrupt index object must degrade to "no index"
    — which sends the caller down the fallback context path and still answers
    the user's question — rather than 500 a request the system can serve.
    """
    got = meta(item)
    s3_key = got.get("s3_key")
    if not s3_key or not bucket:
        return None
    try:
        body = s3.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
        tree = _loads(body)
    except Exception as err:  # noqa: BLE001
        print(f"[pageindex] load FAILED for {s3_key}: {err}")
        return None
    return tree if isinstance(tree, dict) and tree.get("nodes") is not None else None


def build_for(item, timestamps, transcript, groq=None, deadline=None):
    """The tree for one meeting, with LLM summaries when enabled.

    Kept here rather than in pageindex.py so that module stays free of any
    notion of environment flags or a Groq client, and can be unit-tested as
    pure functions.
    """
    tree = pageindex.build_tree(timestamps, transcript)
    if groq is not None and pageindex.llm_summaries_enabled() and tree.get("nodes"):
        segs_by_node = {
            n["node_id"]: pageindex.segments_for_nodes(tree, timestamps, [n["node_id"]])
            for n in tree["nodes"]
        }
        pageindex.add_llm_summaries(tree, segs_by_node, groq, deadline=deadline)
    return tree


def delete(s3, bucket, key):
    """Remove a meeting's index object. Used by the permanent-delete path.

    Idempotent and non-fatal: S3 DELETE on an absent key succeeds, and a
    failure here must not block deleting the recording itself — a leftover
    index object is garbage, not a correctness problem.
    """
    if not bucket:
        return
    try:
        s3.delete_object(Bucket=bucket, Key=s3_key_for(key))
    except Exception as err:  # noqa: BLE001
        print(f"[pageindex] delete failed for {key}: {err}")
