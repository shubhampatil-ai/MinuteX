#!/usr/bin/env bash
# =============================================================
# 41_create_notifications_table.sh — the in-app notification store (Phase 1).
#
# Table: Notifications  (override with NOTIFICATIONS_TABLE in .env)
#   Partition key: notification_id (String, uuid hex)
#   Billing:       PAY_PER_REQUEST
#
# WHY notification_id AS THE TABLE KEY, AND NOT (user_id, created_at).
#
# A composite (user_id HASH, created_at RANGE) table would serve the list
# query directly and look like the obvious design. It is rejected because
# MARK-AS-READ needs to address ONE notification by the id the client holds,
# and with that key schema an id alone is not addressable — the client would
# have to send back the user_id and the timestamp, and the server would have
# to trust the pair. A single-attribute key makes "read this one" a GetItem
# whose ownership is then checked against the JWT (see _owned_notification),
# which is the same shape Tasks already uses (PK task_id + owner-index) and
# for the same reason. Copying the proven pattern beats inventing a second.
#
# INDEXES — both are per-user because a notification is only ever read by its
# owner. There is no "all notifications of type X" query and no cross-user
# query anywhere in the product; adding an index for one would be inventing a
# capability with no consumer.
#
#   user-index      (user_id HASH, created_at RANGE)
#                   The notification centre's list: newest first, paginated.
#                   ScanIndexForward=false + ExclusiveStartKey, exactly like
#                   the Tasks owner-index.
#
#   user-unread-index (user_id HASH, unread_marker RANGE)  -- SPARSE
#                   The badge count, and the mark-all-read sweep.
#
#                   `unread_marker` is written ONLY while a notification is
#                   unread, and REMOVED when it is read. DynamoDB leaves an
#                   item out of a GSI when an index key attribute is absent,
#                   so this index contains exactly the unread rows and nothing
#                   else. That is what makes the badge a bounded query over
#                   unread rows instead of a scan over the user's whole
#                   history filtering on is_read — the difference between a
#                   count that stays cheap forever and one that gets slower
#                   every week the user keeps using the app.
#
#                   The marker's VALUE is created_at, so the index is also
#                   ordered — the mark-all sweep pages through it in a stable
#                   order rather than an arbitrary one.
#
#   Item shape (written by functions/userapi's notification engine; also
#   raised by functions/transcribe through the same shared contract):
#     notification_id   PK (uuid hex)
#     user_id           the RECIPIENT. The tenant boundary — every read is
#                       scoped to this and it is never taken from a request.
#     type              MEETING_PROCESSING_COMPLETED | TASK_ASSIGNED | ...
#                       (shared/notification_schema.py owns the vocabulary)
#     title             short label, assembled by notification_schema.build()
#     message           one line of detail — never a document, never a
#                       transcript (the schema clips both fields)
#     priority          LOW | NORMAL | HIGH
#     entity_type       task | meeting | document
#     entity_id         the id/key the app opens. A REFERENCE, not a copy.
#     is_read           bool
#     unread_marker     created_at while unread; ABSENT once read (see above)
#     read_at           ISO timestamp, "" until read
#     created_at        ISO timestamp
#     metadata          small flat map of display extras (bounded by the
#                       schema — 10 keys, 200 chars each)
#     channels          delivered-on list. Always ["IN_APP"] in Phase 1; the
#                       seam for EMAIL/WHATSAPP/PUSH later.
#     dedupe_key        deterministic identity for "this fact, once" —
#                       user + type + entity (+ day for the deadline types).
#                       The uniqueness CONDITION is written against this; see
#                       the unique-index note below.
#
# DEDUPE — WHY A SECOND TABLE AND NOT A CONDITION ON THIS ONE.
#
# DynamoDB can only enforce a conditional write against the item's OWN primary
# key, and the primary key here is a random uuid — so `attribute_not_exists`
# on this table cannot express "no other row has this dedupe_key". A GSI
# cannot enforce uniqueness either (GSIs are not unique in DynamoDB, and
# reading one before writing is a race, not a guarantee).
#
# So uniqueness gets its own tiny table whose PRIMARY KEY IS the dedupe key —
# a claim row. The engine writes the claim with attribute_not_exists(dedupe_key)
# FIRST; whoever wins the claim writes the notification, and the loser drops
# the event. That is the same conditional-claim pattern this codebase already
# uses for folder-name uniqueness (_folder_name_claim, script 31) and for the
# STT webhook's idempotency, so it is a familiar shape rather than a new one.
#
# Table: NotificationDedupe (override with NOTIFICATION_DEDUPE_TABLE)
#   Partition key: dedupe_key (String)
#   TTL attribute: expires_at  -- claims are garbage after their fact is old
#                                news, and letting DynamoDB expire them keeps
#                                this table small without a cleanup job.
#
# Idempotent: no-op for any table that already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

NOTIFICATIONS_TABLE="${NOTIFICATIONS_TABLE:-Notifications}"
NOTIFICATION_DEDUPE_TABLE="${NOTIFICATION_DEDUPE_TABLE:-NotificationDedupe}"

echo ">> Region: $AWS_REGION"
echo ">> Tables: $NOTIFICATIONS_TABLE, $NOTIFICATION_DEDUPE_TABLE"

table_exists() {
  aws dynamodb describe-table --table-name "$1" >/dev/null 2>&1
}

# ------------------------------------------------------------ Notifications
if table_exists "$NOTIFICATIONS_TABLE"; then
  status="$(aws dynamodb describe-table --table-name "$NOTIFICATIONS_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$NOTIFICATIONS_TABLE' already exists (status: $status). Skipping."
else
  echo ">> Creating table '$NOTIFICATIONS_TABLE' ..."
  aws dynamodb create-table \
    --table-name "$NOTIFICATIONS_TABLE" \
    --attribute-definitions \
        AttributeName=notification_id,AttributeType=S \
        AttributeName=user_id,AttributeType=S \
        AttributeName=created_at,AttributeType=S \
        AttributeName=unread_marker,AttributeType=S \
    --key-schema \
        AttributeName=notification_id,KeyType=HASH \
    --global-secondary-indexes '[
      {"IndexName":"user-index",
       "KeySchema":[{"AttributeName":"user_id","KeyType":"HASH"},
                    {"AttributeName":"created_at","KeyType":"RANGE"}],
       "Projection":{"ProjectionType":"ALL"}},
      {"IndexName":"user-unread-index",
       "KeySchema":[{"AttributeName":"user_id","KeyType":"HASH"},
                    {"AttributeName":"unread_marker","KeyType":"RANGE"}],
       "Projection":{"ProjectionType":"ALL"}}
    ]' \
    --billing-mode PAY_PER_REQUEST >/dev/null

  echo "   waiting for '$NOTIFICATIONS_TABLE' to become ACTIVE ..."
  aws dynamodb wait table-exists --table-name "$NOTIFICATIONS_TABLE"
  echo "   '$NOTIFICATIONS_TABLE' is ACTIVE."
fi

# -------------------------------------------------------- NotificationDedupe
if table_exists "$NOTIFICATION_DEDUPE_TABLE"; then
  status="$(aws dynamodb describe-table --table-name "$NOTIFICATION_DEDUPE_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$NOTIFICATION_DEDUPE_TABLE' already exists (status: $status). Skipping."
else
  echo ">> Creating table '$NOTIFICATION_DEDUPE_TABLE' ..."
  aws dynamodb create-table \
    --table-name "$NOTIFICATION_DEDUPE_TABLE" \
    --attribute-definitions \
        AttributeName=dedupe_key,AttributeType=S \
    --key-schema \
        AttributeName=dedupe_key,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null

  echo "   waiting for '$NOTIFICATION_DEDUPE_TABLE' to become ACTIVE ..."
  aws dynamodb wait table-exists --table-name "$NOTIFICATION_DEDUPE_TABLE"
  echo "   '$NOTIFICATION_DEDUPE_TABLE' is ACTIVE."
fi

# TTL on the claim table. Enabling it is idempotent in effect (AWS answers
# with the current state when it is already enabled), but the call errors if
# the status is already ENABLED, so the status is checked first.
ttl_status="$(aws dynamodb describe-time-to-live \
                --table-name "$NOTIFICATION_DEDUPE_TABLE" \
                --query 'TimeToLiveDescription.TimeToLiveStatus' \
                --output text 2>/dev/null || echo "UNKNOWN")"
if [ "$ttl_status" = "ENABLED" ] || [ "$ttl_status" = "ENABLING" ]; then
  echo ">> TTL on '$NOTIFICATION_DEDUPE_TABLE' already $ttl_status."
else
  echo ">> Enabling TTL (expires_at) on '$NOTIFICATION_DEDUPE_TABLE' ..."
  aws dynamodb update-time-to-live \
    --table-name "$NOTIFICATION_DEDUPE_TABLE" \
    --time-to-live-specification "Enabled=true,AttributeName=expires_at" \
    >/dev/null
  echo "   TTL enabled."
fi

echo ">> Done."
