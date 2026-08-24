# Migration: personal AWS account → company AWS account

**Source:** `624734879550` (`iam-user/shubham`), region `ap-south-1`
**Target:** company account (new ID), region `ap-south-1` — keep the region, only the account changes.

The good news up front: **this is a much easier migration than the
eu-north-1 → ap-south-1 move**, for two reasons.

1. **The region does not change.** Nothing region-scoped needs rethinking — no
   RTT re-benchmark, no endpoint-style questions, and the Lambda code already
   reads `AWS_REGION` from the runtime.
2. **The repo is now the source of truth.** Last time, `lambda/` held Node.js
   Salesforce code that was *not* what was deployed, so artifacts had to be
   pulled from the live functions. That is no longer true: `cloud/functions/device-presign/`,
   `cloud/functions/transcribe/`, `cloud/functions/userapi/` + `cloud/shared/` are the live
   Python 3.12 sources, and `cloud/scripts/*` deploy exactly them. So the new account is
   built by **re-running the scripts**, not by copying artifacts.

The one thing that is *harder*: the S3 bucket name and every IAM/Secrets/KMS ARN
embeds the account ID, and a bucket cannot be moved between accounts — its
contents must be copied while both accounts still exist.

---

## What exists today

### Lambda — 3 functions, all `python3.12`, x86_64, 256 MB
| Function | Timeout | Source dir | Role |
|---|---|---|---|
| `getUploadUrl` | 10 s | `cloud/functions/device-presign/` | `getUploadUrl-aps1` |
| `transcribeRecording` | 300 s | `cloud/functions/transcribe/` | `transcribeRecording-aps1` |
| `userApi` | 29 s | `cloud/functions/userapi/` | `userApi-aps1` |

`cloud/shared/` (`ai_schema`, `groq_client`, `prompts`, `stt_result`,
`transcript_store`) is vendored into the transcribe + userApi zips at package time.

### API Gateway — HTTP API `iot-audio-api` (`q87zfn5vyj`), `$default` stage, autodeploy
**53 routes**, all `AuthorizationType: NONE` (auth is JWT-in-Lambda, not an API
Gateway authorizer). Two integrations only:
* `35ed8e8` → `getUploadUrl` — `GET /get-upload-url`, `POST /device/upload-complete`, `POST /device/heartbeat`
* `iaj7p5d` → `userApi` — the other 50 routes

### DynamoDB — 6 tables, all PAY_PER_REQUEST, **94 items / ~1.25 MB**
| Table | Key | Items | GSI |
|---|---|---|---|
| `Users` | `user_id` (H) | 23 | `email-index` |
| `Recordings` | `audio_s3_key` (H) | 63 | `user-index`, `device-index` (both + `created_at` range) |
| `UserDevices` | `user_id` (H), `device_id` (R) | 5 | — |
| `Devices` | `device_id` (H) | 1 | `paired-user-index` |
| `DeviceKeys` | `apiKey` (H) | 1 | — |
| `CrmConnections` | `user_id` (H), `provider` (R) | 1 | — |

No streams, no TTL anywhere.

### S3 — 1 bucket, `meeting-recorder-shubham-aps1`
**78 objects, 1,561,256,668 B (1.45 GiB)** — `recordings/` audio plus
`trancloud/scripts/**.transcript.json.gz`. SSE-S3 + BucketKey, no versioning, no CORS,
no lifecycle. Notification `transcribe-on-audio`: `s3:ObjectCreated:*`
(**no suffix filter**) → `transcribeRecording`.

### Secrets Manager — 3 secrets
`userApi/jwtSecret`, `userApi/salesforceClientSecret`, `userApi/elevenlabsWebhookSecret`

### KMS — 1 CMK
`alias/crm-salesforce` (`b5a78676-…`) — encrypts each user's Salesforce refresh token.

### IAM — 3 roles, 11 inline policies
`getUploadUrl-aps1` (3 inline + `AWSLambdaBasicExecutionRole`),
`transcribeRecording-aps1` (2 inline + basic execution),
`userApi-aps1` (6 inline, **no managed policy** — its logs permission is inline).

---

## Seven things that will bite

**1. The S3 bucket name cannot be reused, and buckets do not move accounts.**
Pick a company-namespaced name, e.g. `minutex-recordings-prod-aps1`. That name
then changes in 3 Lambda env vars, ~4 IAM policies, and `.env`. Copying 1.45 GiB
cross-account needs a bucket policy on the *source* granting the *target*
principal read — see step 8.

**2. The S3 notification has NO suffix filter.** The previous migration doc says
`.wav`; the live config fires on **every** `s3:ObjectCreated:*`. So copying
objects into the new bucket with the trigger already wired would re-invoke
`transcribeRecording` 78 times — including on the `trancloud/scripts/*.json.gz` objects
the pipeline writes itself. **Wire the notification LAST**, after the copy.
(`transcribeRecording` does filter by key prefix internally, but do not rely on
that as the safety net.)

**3. The KMS key must be re-created, and Salesforce refresh tokens re-encrypted
or discarded.** A CMK cannot leave its account. Every `CrmConnections` row holds
a refresh token encrypted under the old key — copying the rows verbatim leaves
them **undecryptable**. Only 1 row exists, so the cheap correct answer is: copy
the table *without* the token field and have that user reconnect Salesforce. Do
**not** just re-point `SALESFORCE_KMS_KEY_ID`.

**4. Salesforce refresh-token rotation is ON.** Related to #3: a refresh token is
single-use here, so any stale copy is worthless anyway — another reason to prefer
reconnection over migration for CRM state.

**5. The JWT secret must be copied byte-for-byte** or all 23 users are logged
out. Note the app treats **401 as session death** — a changed secret means every
user gets bounced to login, not a silent refresh.

**6. The ElevenLabs webhook belongs to a WORKSPACE, not a key — and its URL is
about to change.** The new API Gateway means a new webhook URL, so a **new
webhook must be registered** and its signing secret captured (ElevenLabs shows it
once). `cloud/scripts/28_deploy_async_stt.sh` does exactly this and is safe to re-run.
The STT key on **both** Lambdas must belong to the same workspace that owns the
new webhook, or async STT silently degrades to the synchronous path and long
recordings start timing out.

**7. The API endpoint URL changes → the ESP32 must be reflashed.**
* [device/src/Config.h:23](../device/src/Config.h#L23) — `PRESIGN_ENDPOINT` is hardcoded
* [app/lib/api.ts:23](../app/lib/api.ts#L23) — hardcoded fallback
* The Salesforce Connected App **callback URL** must be updated to the new
  `${API_URL}/crm/salesforce/callback`, and Salesforce takes ~10 min to propagate.

The device is the slowest thing to update — plan the cutover around it.

---

## Gaps in the scripts that this migration exposes

The `cloud/scripts/` chain is `.env`-driven and idempotent, but it was written to
*evolve* an existing stack, not bootstrap a bare account:

* **`userApi` is never created by any script.** Only `02_deploy_lambda.sh`
  (`getUploadUrl`) and `12_deploy_transcribe_lambda.sh` (`transcribeRecording`)
  have a `create-function` path. Scripts 19–30 all call `update-function-code` and
  read the existing role via `lambda get-function`. On a fresh account they will
  **fail at the first `get-function`**.
  → Fix: add a create-or-update bootstrap for `userApi` (mirror the pattern in
  `02_deploy_lambda.sh`), or hand-create it once before running 17+.
* **`cloud/scripts/iam/userApi-permanent-delete.json` has the account ID and bucket name
  baked in** as a checked-in literal (`624734879550`,
  `meeting-recorder-shubham-aps1`). Every other IAM policy in the chain is
  generated at runtime by a helper that takes the bucket and ARNs as arguments
  (e.g. `_stt_webhook_policy.py`), so it follows `.env` automatically. This one
  does not.
  **This fails silently, which makes it the most dangerous item in this document.**
  `30_deploy_trash.sh` will apply it to the new account's `userApi` role without
  error — IAM accepts a policy referencing a foreign account's resources. Nothing
  breaks at deploy time or in the step-11 checks. It breaks later, in production,
  when a user permanently deletes a recording: AccessDenied on both the S3 object
  and the `Recordings` row, so trash appears to empty while the audio and the row
  survive.
  → Fix: parameterise it like the other policies (a `_permanent_delete_policy.py`
  taking bucket + table ARN), or at minimum `sed` the bucket and account ID at
  deploy time. Then add "permanently delete a recording, confirm the object and
  row are gone" to step 11 — the current verification would not catch this.
* Secrets and the KMS key *are* created by scripts 23 and 28, so those are covered.
* [cloud/scripts/resync.py:43](../cloud/scripts/resync.py#L43) defaults `BUCKET_NAME` to
  `meeting-recorder-shubham` — the **pre-migration eu-north-1 bucket**, which no
  longer exists. Harmless while `.env` is loaded, but a stale default that
  survived the last migration; point it at the new bucket or drop the default.
* `cloud/scripts/migrate_*.py` are eu-north-1-era one-offs — ignore them.
* [cloud/tests/test_async_stt.py:124](../cloud/tests/test_async_stt.py#L124) hardcodes the bucket.
  Fine for a fixture, but update it so the suite is meaningful post-migration.

Doing this properly is the chance to close that gap: the sequence below is
effectively the `00_bootstrap` the repo never had.

---

## Runbook

Both accounts run side by side until cutover. Nothing below touches the old
account except adding one temporary read-only bucket policy.

### 0. Access + profiles
Get from company IT an IAM identity with admin, or at minimum Lambda, API
Gateway, DynamoDB, S3, IAM, Secrets Manager, KMS, and CloudWatch Logs.

```bash
aws configure --profile company        # new account
aws configure --profile personal       # existing 624734879550
export SRC_PROFILE=personal DST_PROFILE=company
export DST_ACCT=<new-account-id>
export SRC_BUCKET=meeting-recorder-shubham-aps1
export DST_BUCKET=minutex-recordings-prod-aps1
```

Ask IT two questions **before** starting, because they can invalidate the plan:
* Are there **SCPs / Control Tower guardrails** (region allow-lists, "no public
  buckets", mandatory tags, permission boundaries)? An SCP denying `ap-south-1`
  or forcing KMS-encrypted buckets changes the steps.
* Is there a **required tagging standard**, or a mandated IaC path (i.e. must
  this be Terraform/CDK rather than these scripts)?

### 1. `.env` for the new account
Copy `.env` → `.env.company`, change `BUCKET_NAME`, and blank `API_URL` and
`SALESFORCE_KMS_KEY_ID` so scripts 23/28 create fresh ones. Keep
`AWS_REGION=ap-south-1`.

### 2. DynamoDB — create 6 tables, then copy 94 items
Scripts `01`, `10`, `11`, `14`, `15`, `22` create the tables with the right GSIs.
Then copy — same shape as the last migration, but profile-aware rather than
region-aware:

```bash
python - <<'PY'
import boto3
src = boto3.Session(profile_name="personal", region_name="ap-south-1").resource("dynamodb")
dst = boto3.Session(profile_name="company",  region_name="ap-south-1").resource("dynamodb")
for t in ["Users","Recordings","UserDevices","Devices","DeviceKeys","CrmConnections"]:
    s, d, n, kw = src.Table(t), dst.Table(t), 0, {}
    while True:
        page = s.scan(**kw)
        with d.batch_writer() as b:
            for it in page["Items"]:
                if t == "CrmConnections":
                    # refresh token is encrypted under a CMK that cannot move accounts
                    it.pop("refresh_token_ciphertext", None)
                    it["status"] = "reconnect_required"
                b.put_item(Item=it); n += 1
        if "LastEvaluatedKey" not in page: break
        kw = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    print(f"{t}: {n}")
PY
```

Confirm the real attribute name for the encrypted token in `cloud/functions/userapi/`
before relying on that `pop` — if it differs, the row copies over broken.

Watch the GSI-key rule while copying: never write `NULL` or `""` for `device_id`
on a `Recordings` row, or the `device-index` write fails.

### 3. Secrets Manager — copy the JWT secret verbatim
```bash
V=$(aws --profile $SRC_PROFILE secretsmanager get-secret-value \
      --secret-id userApi/jwtSecret --query SecretString --output text)
aws --profile $DST_PROFILE secretsmanager create-secret \
      --name userApi/jwtSecret --secret-string "$V"
unset V
```

Do **not** copy `elevenlabsWebhookSecret` (script 28 makes a new one for the new
webhook) or `salesforceClientSecret` (script 23 writes it from `.env`).

### 4. S3 — create the bucket
```bash
aws --profile $DST_PROFILE s3api create-bucket --bucket $DST_BUCKET \
  --region ap-south-1 --create-bucket-configuration LocationConstraint=ap-south-1
aws --profile $DST_PROFILE s3api put-bucket-encryption --bucket $DST_BUCKET \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
aws --profile $DST_PROFILE s3api put-public-access-block --bucket $DST_BUCKET \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

### 5–7. Run the deploy chain against the new account
```bash
cp .env.company .env
export AWS_PROFILE=company
bash cloud/scripts/01_create_dynamodb.sh
bash cloud/scripts/02_deploy_lambda.sh              # creates getUploadUrl + role
bash cloud/scripts/03_create_apigateway.sh          # NEW API ID — record it
bash cloud/scripts/10_create_recordings_table.sh
bash cloud/scripts/11_create_device_map_table.sh
bash cloud/scripts/12_deploy_transcribe_lambda.sh   # creates transcribeRecording
# >>> userApi must exist before 17 — see "Gaps". Add a create path first. <<<
bash cloud/scripts/14_create_devices_table.sh
bash cloud/scripts/15_add_recordings_user_index.sh
bash cloud/scripts/17_wire_pairing_routes.sh
bash cloud/scripts/18_wire_upload_routes.sh
bash cloud/scripts/19_deploy_multisource_lambdas.sh
bash cloud/scripts/20_deploy_device_mgmt.sh
bash cloud/scripts/21_deploy_ai_workspace.sh
bash cloud/scripts/22_create_crm_connections_table.sh
bash cloud/scripts/23_deploy_salesforce_oauth.sh    # creates KMS key + SF secret
bash cloud/scripts/24_deploy_site_visit_extraction.sh
bash cloud/scripts/25_deploy_crm_config.sh
bash cloud/scripts/27_deploy_reprocess.sh
bash cloud/scripts/28_deploy_async_stt.sh           # registers the NEW webhook
bash cloud/scripts/30_deploy_trash.sh
```

Run them one at a time and read the output — this is the first time they are being
used as a bootstrap rather than an upgrade. Once script 03 prints the new API ID,
put it in `.env` as `API_URL` **before** running 23 and 28, so the Salesforce
redirect URI and the webhook URL are correct on the first pass.

Rotate `GROQ_API_KEY` and `ELEVENLABS_API_KEY` during this step. They are
currently **plaintext Lambda env vars** in the personal account and were readable
during this inventory; the company account should not inherit keys a
soon-to-be-decommissioned personal account has seen. Better still, store them in
Secrets Manager like the JWT and Salesforce secrets already are. If the
ElevenLabs key moves to a company workspace, register the webhook with a key from
**that** workspace.

### 8. S3 — copy 1.45 GiB, still no trigger
Temporary policy on the **source** bucket allowing the new account to read:
```bash
aws --profile $SRC_PROFILE s3api put-bucket-policy --bucket $SRC_BUCKET --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Principal\": {\"AWS\": \"arn:aws:iam::${DST_ACCT}:root\"},
    \"Action\": [\"s3:GetObject\", \"s3:ListBucket\"],
    \"Resource\": [\"arn:aws:s3:::${SRC_BUCKET}\", \"arn:aws:s3:::${SRC_BUCKET}/*\"]
  }]
}"

aws --profile $DST_PROFILE s3 sync s3://$SRC_BUCKET s3://$DST_BUCKET

aws --profile $SRC_PROFILE s3api delete-bucket-policy --bucket $SRC_BUCKET
```

Run the sync from the **destination** profile so the new account owns the
resulting objects. Revoke the policy immediately after. ~1.45 GiB egress ≈ **$0.13**.

Unlike last time, **do not skip this copy** — 63 `Recordings` rows point at these
keys and the app serves historical audio and transcripts from them. Keys are
bucket-relative, so they survive the bucket rename untouched.

### 9. S3 → Lambda trigger — **LAST**
`cloud/scripts/13_wire_s3_trigger.sh`. Afterwards, confirm no unexpected
`transcribeRecording` invocations fired.

### 10. Cut over the clients
* [device/src/Config.h:23](../device/src/Config.h#L23) → new endpoint. **Rebuild + reflash.**
* [app/lib/api.ts:23](../app/lib/api.ts#L23) → new endpoint,
  then a new **EAS build** (no Android SDK on this laptop — use `eas-cli build`).
* Salesforce Connected App → new callback URL; wait ~10 min. If the company has
  its own Salesforce org, create a company Connected App and update
  `SALESFORCE_CLIENT_ID` + secret too.
* `.env` → `BUCKET_NAME`, `API_URL`.

### 11. Verify
```bash
curl -H "x-api-key: <DEVICE_API_KEY>" "https://<NEW_API>/get-upload-url?meetingId=probe&timestamp=1"
# existing user's password — proves the JWT secret copied correctly
curl -X POST https://<NEW_API>/login -d '{"email":"...","password":"..."}'
```
Then end to end: record on the device → upload → transcript appears; and a mobile
upload → async STT webhook delivers. `cloud/scripts/_preflight_check.py` and `cloud/tests/`
(incl. `cloud/tests/mock_device.py`) are the faster loop before touching real hardware.

**Also permanently delete a test recording and confirm both the S3 object and the
`Recordings` row are actually gone.** This is the only check that catches the
hardcoded-ARN policy described in "Gaps" — that failure is invisible at deploy
time and every other check above passes with it broken.

### 12. Decommission — and the part that is not technical
Soak for 1–2 weeks with both stacks alive, then delete in order: notification,
Lambdas, API, tables, secrets, KMS (scheduled deletion, 7–30 day window), bucket
last.

**Billing and ownership, which is the actual point of the move:**
* Do **not** close the personal account until the company stack has soaked.
* Decide whether the company account should join an **AWS Organization** —
  usually yes, for consolidated billing and SCPs.
* **Raise this alternative with IT first: AWS account transfer.** Instead of
  rebuilding, the *existing* account can be moved under the company's
  Organization and its billing and root contact reassigned. That migrates
  everything atomically — no bucket rename, no reflash, no re-encryption, no
  user logout, none of this runbook. It is strictly less work; the trade-off is
  that the company inherits an account carrying your personal history, IAM
  users, and CloudTrail. Ask which they prefer **before** doing any of the above.

---

## Rollback
Nothing in the old account changes until step 10 — and step 8's bucket policy,
which is reverted immediately. To roll back after cutover: revert
`PRESIGN_ENDPOINT` and `api.ts`, reflash, revert the Salesforce callback. The old
stack is still running and still holds the data.

---

## Effort

| Step | Time |
|---|---|
| 0 access, profiles, SCP questions | 30 min (+ IT wait) |
| 1–4 env, tables, data copy, secret, bucket | 30 min |
| 5–7 script chain (incl. fixing the `userApi` create gap) | 1.5–2.5 h |
| 8 S3 copy 1.45 GiB | 10–20 min |
| 9 trigger | 5 min |
| 10 clients: reflash + EAS build + Salesforce | 1–2 h (EAS queue) |
| 11 verify e2e | 45 min |
| **Total** | **~5–7 h**, plus a 1–2 week soak |

One-off cost ≈ $0.15. Ongoing cost is unchanged — everything is pay-per-request;
the bill simply lands on the company account.
