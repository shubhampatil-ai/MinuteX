#!/usr/bin/env bash
# =============================================================
# 31_create_workspace_tables.sh — Contacts, Folders, FolderContacts,
# MeetingParticipants and Tasks: the organization layer over the master
# recording collection.
#
# Five tables, one per entity — the same shape every other entity in this
# stack uses (Users, Devices, Recordings, CrmConnections are all separate
# tables), so each one's GSIs can be reasoned about and tuned on its own.
#
#   Contacts  (override CONTACTS_TABLE)
#     PK contact_id (S)
#     GSI owner-index        owner_user_id HASH, created_at RANGE
#                            -> "every contact this user owns", newest first.
#     GSI owner-email-index  owner_user_id HASH, email_lc RANGE
#                            -> exact-email dedupe + the strong-match lookup
#                               that resolves an AI assignee. SPARSE: a
#                               contact with no email carries no email_lc and
#                               never enters the index, which is what makes
#                               "no email" mean "not a dedupe candidate"
#                               rather than "collides with every other
#                               email-less contact".
#     GSI owner-phone-index  owner_user_id HASH, phone_e164 RANGE
#                            -> same, for phone. Also sparse.
#
#   Folders  (override FOLDERS_TABLE)
#     PK folder_id (S)
#     GSI owner-index        owner_user_id HASH, name_lc RANGE
#                            -> a user's folders in name order, AND the
#                               duplicate-name check without a scan.
#
#   FolderContacts  (override FOLDER_CONTACTS_TABLE)
#     PK folder_id (S), SK contact_id (S)
#       The composite key IS the uniqueness constraint from spec section 6:
#       (folder_id, contact_id) can physically only exist once, so a double
#       "add contact to folder" is an overwrite, never a duplicate row. No
#       application-level check is involved.
#     GSI contact-index      contact_id HASH, folder_id RANGE
#                            -> the reverse direction: "which folders is this
#                               contact in", needed to clean up associations
#                               when a contact is deleted.
#
#   MeetingParticipants  (override MEETING_PARTICIPANTS_TABLE)
#     PK audio_s3_key (S), SK speaker_id (S)
#       Keyed by the recording's own PK so a participant row is a child of the
#       recording it belongs to. speaker_id as the sort key means one speaker
#       label maps to exactly one contact per meeting, enforced by the key
#       rather than by a read-then-write.
#     GSI contact-index      contact_id HASH, audio_s3_key RANGE
#                            -> "every meeting this contact appeared in".
#
#   Tasks  (override TASKS_TABLE)
#     PK task_id (S)
#     GSI owner-index        owner_user_id HASH, created_at RANGE
#     GSI meeting-index      source_recording_id HASH, created_at RANGE
#                            -> one meeting's tasks (replaces reading the
#                               embedded map).
#     GSI folder-index       folder_id HASH, created_at RANGE   (sparse)
#     GSI assignee-index     assignee_contact_id HASH, created_at RANGE
#     GSI assignee-user-index assignee_user_id HASH, created_at RANGE
#                             "tasks I must DO" — keyed on the MinuteX account,
#                             not the address-book contact. Sparse: only tasks
#                             assigned to a linked account are in it.
#                            (sparse — unresolved tasks carry no contact id
#                             and correctly do not appear)
#     GSI dedupe-index       owner_user_id HASH, fingerprint RANGE
#                            -> the idempotency lookup that stops a second AI
#                               run re-creating tasks it already created.
#
# WHY sparse indexes matter here: DynamoDB refuses to index an item whose key
# attribute is NULL or "" (it is not "indexed as empty" — the write itself
# fails). Every optional key attribute above is therefore OMITTED when absent,
# never written as an empty string. This is the same GSI-key rule the Devices
# and Recordings tables already live by.
#
# Billing: PAY_PER_REQUEST throughout, matching every other table here.
#
# IAM: the userApi role needs Get/Put/Update/Delete/Query on all five tables
# and their indexes — see scripts/iam/.
#
# Idempotent: each table is skipped if it already exists, so this is safe to
# re-run after adding one table or on a partially-created stack.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

CONTACTS_TABLE="${CONTACTS_TABLE:-Contacts}"
FOLDERS_TABLE="${FOLDERS_TABLE:-Folders}"
FOLDER_CONTACTS_TABLE="${FOLDER_CONTACTS_TABLE:-FolderContacts}"
MEETING_PARTICIPANTS_TABLE="${MEETING_PARTICIPANTS_TABLE:-MeetingParticipants}"
TASKS_TABLE="${TASKS_TABLE:-Tasks}"

echo ">> Region: $AWS_REGION"

# create_table <name> <attribute-definitions> <key-schema> <gsi-json>
create_table() {
  local name="$1" attrs="$2" keys="$3" gsi="$4"

  if aws dynamodb describe-table --table-name "$name" >/dev/null 2>&1; then
    local status
    status="$(aws dynamodb describe-table --table-name "$name" \
                --query 'Table.TableStatus' --output text)"
    echo ">> Table '$name' already exists (status: $status). Skipping."
    return 0
  fi

  echo ">> Creating table '$name' ..."
  # Word-splitting on $attrs/$keys is intended — they are multi-token CLI args.
  # shellcheck disable=SC2086
  aws dynamodb create-table \
    --table-name "$name" \
    --attribute-definitions $attrs \
    --key-schema $keys \
    --global-secondary-indexes "$gsi" \
    --billing-mode PAY_PER_REQUEST >/dev/null

  echo "   waiting for '$name' to become ACTIVE ..."
  aws dynamodb wait table-exists --table-name "$name"
  echo "   '$name' is ACTIVE."
}

# ---------------------------------------------------------------- Contacts
create_table "$CONTACTS_TABLE" \
  "AttributeName=contact_id,AttributeType=S
   AttributeName=owner_user_id,AttributeType=S
   AttributeName=created_at,AttributeType=S
   AttributeName=email_lc,AttributeType=S
   AttributeName=phone_e164,AttributeType=S" \
  "AttributeName=contact_id,KeyType=HASH" \
  '[
    {"IndexName":"owner-index",
     "KeySchema":[{"AttributeName":"owner_user_id","KeyType":"HASH"},
                  {"AttributeName":"created_at","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}},
    {"IndexName":"owner-email-index",
     "KeySchema":[{"AttributeName":"owner_user_id","KeyType":"HASH"},
                  {"AttributeName":"email_lc","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}},
    {"IndexName":"owner-phone-index",
     "KeySchema":[{"AttributeName":"owner_user_id","KeyType":"HASH"},
                  {"AttributeName":"phone_e164","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}}
  ]'

# ----------------------------------------------------------------- Folders
create_table "$FOLDERS_TABLE" \
  "AttributeName=folder_id,AttributeType=S
   AttributeName=owner_user_id,AttributeType=S
   AttributeName=name_lc,AttributeType=S" \
  "AttributeName=folder_id,KeyType=HASH" \
  '[
    {"IndexName":"owner-index",
     "KeySchema":[{"AttributeName":"owner_user_id","KeyType":"HASH"},
                  {"AttributeName":"name_lc","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}}
  ]'

# ---------------------------------------------------------- FolderContacts
create_table "$FOLDER_CONTACTS_TABLE" \
  "AttributeName=folder_id,AttributeType=S
   AttributeName=contact_id,AttributeType=S" \
  "AttributeName=folder_id,KeyType=HASH
   AttributeName=contact_id,KeyType=RANGE" \
  '[
    {"IndexName":"contact-index",
     "KeySchema":[{"AttributeName":"contact_id","KeyType":"HASH"},
                  {"AttributeName":"folder_id","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}}
  ]'

# ----------------------------------------------------- MeetingParticipants
create_table "$MEETING_PARTICIPANTS_TABLE" \
  "AttributeName=audio_s3_key,AttributeType=S
   AttributeName=speaker_id,AttributeType=S
   AttributeName=contact_id,AttributeType=S" \
  "AttributeName=audio_s3_key,KeyType=HASH
   AttributeName=speaker_id,KeyType=RANGE" \
  '[
    {"IndexName":"contact-index",
     "KeySchema":[{"AttributeName":"contact_id","KeyType":"HASH"},
                  {"AttributeName":"audio_s3_key","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}}
  ]'

# ------------------------------------------------------------------- Tasks
create_table "$TASKS_TABLE" \
  "AttributeName=task_id,AttributeType=S
   AttributeName=owner_user_id,AttributeType=S
   AttributeName=created_at,AttributeType=S
   AttributeName=source_recording_id,AttributeType=S
   AttributeName=folder_id,AttributeType=S
   AttributeName=assignee_contact_id,AttributeType=S
   AttributeName=assignee_user_id,AttributeType=S
   AttributeName=fingerprint,AttributeType=S" \
  "AttributeName=task_id,KeyType=HASH" \
  '[
    {"IndexName":"owner-index",
     "KeySchema":[{"AttributeName":"owner_user_id","KeyType":"HASH"},
                  {"AttributeName":"created_at","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}},
    {"IndexName":"meeting-index",
     "KeySchema":[{"AttributeName":"source_recording_id","KeyType":"HASH"},
                  {"AttributeName":"created_at","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}},
    {"IndexName":"folder-index",
     "KeySchema":[{"AttributeName":"folder_id","KeyType":"HASH"},
                  {"AttributeName":"created_at","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}},
    {"IndexName":"assignee-index",
     "KeySchema":[{"AttributeName":"assignee_contact_id","KeyType":"HASH"},
                  {"AttributeName":"created_at","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}},
    {"IndexName":"assignee-user-index",
     "KeySchema":[{"AttributeName":"assignee_user_id","KeyType":"HASH"},
                  {"AttributeName":"created_at","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}},
    {"IndexName":"dedupe-index",
     "KeySchema":[{"AttributeName":"owner_user_id","KeyType":"HASH"},
                  {"AttributeName":"fingerprint","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}}
  ]'

echo
echo ">> All five workspace tables are ready."
echo ">> Next: python scripts/32_backfill_tasks.py --dry-run"
