# Migration: `eu-north-1` → `ap-south-1`

**Account:** 624734879550 · **Motive:** RTT, see [benchmark_server/FINDINGS.md](benchmark_server/FINDINGS.md)

Upload throughput is `lwIP send buffer ÷ RTT` = `5744 bytes ÷ RTT`. Measured
TCP-connect RTT from the office:

| Region | min | median | `5744 ÷ RTT` |
|---|---|---|---|
| `eu-north-1` (Stockholm, current) | 154 ms | 167–224 ms | 26–34 KB/s |
| **`ap-south-1` (Mumbai)** | 15.7 ms | **18.8 ms** | **~295 KB/s** |

Expect **150–250 KB/s** in practice. A 14.8 MB upload goes from ~9.5 min to ~1 min.

> Endpoint style is *not* a factor. The presigned URL uses the legacy global
> host `bucket.s3.amazonaws.com`, but DNS resolves it to
> `s3-w.eu-north-1.amazonaws.com` — it already lands in Stockholm. No change needed.

---

## What exists in `eu-north-1`

### S3 — 1 bucket
| | |
|---|---|
| Name | `meeting-recorder-shubham` |
| Contents | **54 objects, 1,376,835,364 B (1.28 GB)** |
| Encryption | SSE-S3 (AES256), BucketKey enabled |
| Versioning | off · CORS: none · Lifecycle: none |
| Event notification | `transcribe-wav-created`: `s3:ObjectCreated:*`, suffix `.wav` → `transcribeRecording` |

### Lambda — 3 functions, all `python3.12`
| Function | Mem | Timeout | Env vars |
|---|---|---|---|
| `getUploadUrl` | 256 | 10 s | `BUCKET_NAME`, `TABLE_NAME=DeviceKeys`, `URL_EXPIRY=900` |
| `transcribeRecording` | 256 | 300 s | `BUCKET_NAME`, `RECORDINGS_TABLE=Recordings`, `URL_EXPIRY`, `GROQ_API_KEY`, `GROQ_MODEL`, `ELEVENLABS_API_KEY`, `ELEVENLABS_MODEL` |
| `userApi` | 256 | 15 s | `BUCKET_NAME`, `JWT_SECRET_ARN`, `JWT_TTL=86400` |

All three read `REGION = os.environ.get("AWS_REGION")`, which Lambda sets
automatically — **the code is already region-portable**. `getUploadUrl` has a
`"eu-north-1"` fallback default that is never used in Lambda but should be
updated for tidiness.

### API Gateway — HTTP API `iot-audio-api` (`241vb4dyo5`), stage `$default`, autodeploy
```
GET  /get-upload-url        -> getUploadUrl
POST /signup, /login        -> userApi
GET  /me      PATCH /me     -> userApi
POST /me/password           -> userApi
GET  /devices  POST /devices/claim -> userApi
GET  /recordings  GET /recordings/{key+} -> userApi
```

### DynamoDB — 4 tables, all PAY_PER_REQUEST, **39 items / ~65 KB total**
| Table | Key | Items | GSI |
|---|---|---|---|
| `DeviceKeys` | `apiKey` (H) | 1 | — |
| `Recordings` | `audio_s3_key` (H) | 25 | `device-index` |
| `UserDevices` | `user_id` (H), `device_id` (R) | 5 | — |
| `Users` | `user_id` (H) | 8 | `email-index` |

### Secrets Manager
`userApi/jwtSecret` → `arn:...:secret:userApi/jwtSecret-rNa77x`

### IAM — 3 roles (global, but policies are region- and bucket-scoped)
`getUploadUrl-role`, `transcribeRecording-role`, `userApi-role`. Every inline
policy hardcodes `arn:aws:dynamodb:eu-north-1:...`, `arn:aws:logs:eu-north-1:...`,
`arn:aws:s3:::meeting-recorder-shubham/*`, and the secret ARN.

---

## Five things that will bite

**1. The S3 bucket name cannot be reused.** Bucket names are globally unique,
so `meeting-recorder-shubham` cannot exist in ap-south-1 while it exists in
eu-north-1. **A new name is required** — proposed `meeting-recorder-shubham-aps1`.
That name then has to change in 3 Lambda env vars and 3 IAM policies.

**2. Copying the 1.28 GB *after* wiring the trigger re-transcribes everything.**
54 `.wav` objects would each fire `transcribeRecording` → 54 ElevenLabs + Groq
calls and 54 duplicate `Recordings` rows. **Wire the S3 notification LAST**, after
the copy. (Or skip the copy entirely — see step 8.)

**3. The JWT secret must be copied byte-for-byte.** Generating a new one
invalidates every existing session for all 8 users.

**4. `DeviceKeys` must be copied or the device gets 403.** The ESP32's
`DEVICE_API_KEY` is looked up there; without the row, presign fails.

**5. The API endpoint URL changes.** `PRESIGN_ENDPOINT` in
[device/src/Config.h](../device/src/Config.h) must be updated **and the device reflashed**,
and the mobile app's `API_URL` updated. Plan the cutover around this — the
device is the slowest thing to update.

Good news: `Recordings.audio_s3_key` stores a *bucket-relative* key, so it
survives the bucket rename untouched.

---

## Security note — act on this during the migration

`GROQ_API_KEY` and `ELEVENLABS_API_KEY` are stored as **plaintext Lambda
environment variables** on `transcribeRecording`. They are readable by anyone
with `lambda:GetFunctionConfiguration`, appear in the console, and were surfaced
in plain text during this inventory.

Recommend: **rotate both keys**, and store the new ones in Secrets Manager
(the pattern `userApi` already uses for its JWT secret) rather than recreating
them as env vars in the new region.

---

## Runbook

Roles are **global**. Do not edit the existing ones — that would break
eu-north-1 instantly and remove any rollback. Create parallel `-aps1` roles so
both stacks run side by side until cutover.

```bash
SRC=eu-north-1;  DST=ap-south-1;  ACCT=624734879550
SRC_BUCKET=meeting-recorder-shubham
DST_BUCKET=meeting-recorder-shubham-aps1
```

### 1. DynamoDB — create 4 tables
```bash
aws dynamodb create-table --region $DST --table-name DeviceKeys \
  --attribute-definitions AttributeName=apiKey,AttributeType=S \
  --key-schema AttributeName=apiKey,KeyType=HASH --billing-mode PAY_PER_REQUEST

aws dynamodb create-table --region $DST --table-name UserDevices \
  --attribute-definitions AttributeName=user_id,AttributeType=S AttributeName=device_id,AttributeType=S \
  --key-schema AttributeName=user_id,KeyType=HASH AttributeName=device_id,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST

aws dynamodb create-table --region $DST --table-name Users \
  --attribute-definitions AttributeName=user_id,AttributeType=S AttributeName=email,AttributeType=S \
  --key-schema AttributeName=user_id,KeyType=HASH --billing-mode PAY_PER_REQUEST \
  --global-secondary-indexes '[{"IndexName":"email-index","KeySchema":[{"AttributeName":"email","KeyType":"HASH"}],"Projection":{"ProjectionType":"ALL"}}]'

# Recordings: confirm device-index key schema against the source before running
aws dynamodb describe-table --region $SRC --table-name Recordings \
  --query 'Table.GlobalSecondaryIndexes[].{Name:IndexName,Keys:KeySchema,Proj:Projection}'
```

### 2. DynamoDB — copy 39 items
```bash
python - <<'PY'
import boto3
SRC, DST = "eu-north-1", "ap-south-1"
for t in ["DeviceKeys", "Recordings", "UserDevices", "Users"]:
    src = boto3.resource("dynamodb", region_name=SRC).Table(t)
    dst = boto3.resource("dynamodb", region_name=DST).Table(t)
    n, kw = 0, {}
    while True:
        page = src.scan(**kw)
        with dst.batch_writer() as b:
            for it in page["Items"]:
                b.put_item(Item=it); n += 1
        if "LastEvaluatedKey" not in page: break
        kw = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    print(f"{t}: {n} items copied")
PY
```

### 3. Secrets Manager — copy the JWT secret verbatim
```bash
V=$(aws secretsmanager get-secret-value --region $SRC \
      --secret-id userApi/jwtSecret --query SecretString --output text)
aws secretsmanager create-secret --region $DST \
  --name userApi/jwtSecret --secret-string "$V"
NEW_SECRET_ARN=$(aws secretsmanager describe-secret --region $DST \
  --secret-id userApi/jwtSecret --query ARN --output text)
unset V
```

### 4. S3 — create the bucket
```bash
aws s3api create-bucket --bucket $DST_BUCKET --region $DST \
  --create-bucket-configuration LocationConstraint=$DST
aws s3api put-bucket-encryption --bucket $DST_BUCKET --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
aws s3api put-public-access-block --bucket $DST_BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

### 5. IAM — parallel roles (`*-aps1`)
For each of the three, create a role with the Lambda trust policy and copy the
inline policy with these substitutions:

| From | To |
|---|---|
| `arn:aws:dynamodb:eu-north-1:...` | `arn:aws:dynamodb:ap-south-1:...` |
| `arn:aws:logs:eu-north-1:...` | `arn:aws:logs:ap-south-1:...` |
| `arn:aws:s3:::meeting-recorder-shubham/*` | `arn:aws:s3:::meeting-recorder-shubham-aps1/*` |
| the eu-north-1 secret ARN | `$NEW_SECRET_ARN` |

Also attach `arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole`
to `getUploadUrl-aps1` and `transcribeRecording-aps1` (matching the source).
Note `userApi-role` has **no** managed policy — its logs permission is inline.

### 6. Lambda — deploy the **live** code, not the repo
The repo's `lambda/`, `lambda-transcribe/` contain **Node.js/Salesforce code
that is not what is deployed**. The running functions are Python 3.12. Pull the
live artifacts:
```bash
for f in getUploadUrl transcribeRecording userApi; do
  url=$(aws lambda get-function --function-name $f --region $SRC --query Code.Location --output text)
  curl -s -o $f.zip "$url"
done
```
Then create each in `$DST` with the same handler/memory/timeout, the `-aps1`
role, and env vars with `BUCKET_NAME=$DST_BUCKET` (and `JWT_SECRET_ARN=$NEW_SECRET_ARN`
for `userApi`). Use rotated keys for the Groq/ElevenLabs values — see the
security note.

### 7. API Gateway — new HTTP API
Create `iot-audio-api` in `$DST`, add the 10 routes above with integrations to
the new Lambda ARNs, `$default` stage with autodeploy, and add
`lambda:InvokeFunction` permissions for `apigateway.amazonaws.com` scoped to the
new API ID. **Record the new endpoint URL.**

### 8. S3 — copy existing objects (still no trigger)
```bash
aws s3 sync s3://$SRC_BUCKET s3://$DST_BUCKET --source-region $SRC --region $DST
```
1.28 GB egress ≈ **$0.12**. Consider skipping: those 54 recordings are already
transcribed and their rows are in `Recordings`. Copying is only needed if the
app must serve historical audio.

### 9. S3 → Lambda trigger — **LAST**
```bash
aws lambda add-permission --region $DST --function-name transcribeRecording \
  --statement-id s3-invoke-transcribe --action lambda:InvokeFunction \
  --principal s3.amazonaws.com --source-arn arn:aws:s3:::$DST_BUCKET

aws s3api put-bucket-notification-configuration --bucket $DST_BUCKET \
  --notification-configuration "{\"LambdaFunctionConfigurations\":[{\"Id\":\"transcribe-wav-created\",\"LambdaFunctionArn\":\"arn:aws:lambda:$DST:$ACCT:function:transcribeRecording\",\"Events\":[\"s3:ObjectCreated:*\"],\"Filter\":{\"Key\":{\"FilterRules\":[{\"Name\":\"Suffix\",\"Value\":\".wav\"}]}}}]}"
```

### 10. Cut over the clients
* [device/src/Config.h](../device/src/Config.h) → `PRESIGN_ENDPOINT` = new API URL + `/get-upload-url`. **Rebuild and reflash.**
* Mobile app / `.env` → `API_URL` = new endpoint.
* `.env` → `AWS_REGION`, `BUCKET_NAME`.

### 11. Verify
```bash
curl -H "x-api-key: <DEVICE_API_KEY>" \
  "https://<NEW_API>/get-upload-url?meetingId=probe&timestamp=1"
```
The returned host must contain `ap-south-1`. Then record on the device and
confirm the `UPLOAD ANALYSIS REPORT` shows **Average Speed ≫ 24 KB/s**.

### 12. Decommission
Leave eu-north-1 running for a soak period. Only then delete — bucket last, and
remember the old bucket name is unusable until it is deleted.

---

## Rollback

Until step 10, nothing in production has changed — eu-north-1 is untouched and
serving. To roll back after cutover, revert `PRESIGN_ENDPOINT` and reflash.
This is why the roles are parallel rather than edited in place.

---

## Effort

| Step | Time |
|---|---|
| 1–4 tables, secret, bucket | 10 min |
| 5 IAM roles | 15 min |
| 6–7 Lambda + API Gateway | 25 min |
| 8 S3 copy (1.28 GB) | 5–10 min |
| 9 trigger | 2 min |
| 10–11 client cutover + verify | 15 min |
| **Total** | **~1h 15m** |

Ongoing cost is unchanged (all pay-per-request); one-off egress ≈ $0.12.
