# MinuteX / AI_voice — Device + Cloud + App

End-to-end meeting-recording platform: an ESP32-S3 hardware recorder, a
serverless AWS backend that transcribes and analyses the audio, and a React
Native (Expo) mobile app. All three live in this repo.

Pipeline: **audio** (device · mobile · file upload) → S3 → ElevenLabs Scribe
(async STT) → Groq (summary, highlights, tasks, CRM extraction) → DynamoDB →
app + Salesforce.

## Region
All resources live in **`ap-south-1`** (migrated from `eu-north-1` on
2026-08-01 — see [docs/MIGRATION_ap-south-1.md](docs/MIGRATION_ap-south-1.md)).
Scripts pin the region explicitly via `cloud/scripts/aws.sh` so a different
local `aws configure` default cannot send resources elsewhere.

## Repo structure
```
AI_voice/
├── cloud/                        # Everything that runs in AWS
│   ├── functions/
│   │   ├── userapi/              # userApi — the app-facing API (Python 3.12)
│   │   ├── transcribe/           # transcribeRecording — S3-triggered STT + AI
│   │   └── device-presign/       # getUploadUrl — pairing-gated device presign
│   ├── shared/                   # Vendored flat into BOTH Lambda zips:
│   │                             #   groq_client · prompts · ai_schema
│   │                             #   stt_result · transcript_store
│   ├── scripts/                  # Numbered provisioning/deploy chain + aws.sh
│   │   └── iam/                  # Role trust + inline policy documents
│   └── tests/                    # pytest suite, mock device, fake DynamoDB
├── device/                       # ESP32-S3 firmware
│   ├── src/                      # Sources (flat: Core_* Driver_* Service_* Task_*)
│   ├── platformio.ini            # THE build (raises lwIP TCP send buffer)
│   ├── app3M_fat9M_16MB.csv      # Vendored partition table
│   └── sdkconfig.*               # IDF config snapshots
├── app/                          # React Native (Expo) mobile app — own git repo
├── docs/                         # Specs, migration runbooks, onboarding
├── _archive/                     # Retired code + build caches (see below)
├── .env.example                  # Config template (copy to .env at this root)
└── .gitignore
```

### `_archive/` — not part of the build
| Path | What it is |
|---|---|
| `dead-code/lambda-node-*` | The original Node.js `getUploadUrl` / `transcribeAndSync`, replaced by the Python functions above |
| `dead-code/benchmark_server/` | One-off upload-throughput experiment; `FINDINGS.md` is the measurement trail cited by `platformio.ini` |
| `dead-code/*.bak`, `.e2e_testkey` | Pre-refactor firmware monolith and throwaway credentials |
| `one-shot-scripts/` | Completed region-migration + superseded provisioning scripts |
| `build-cache/` | `.pio/` and `managed_components/` (~517 MB) — regenerable, but a firmware rebuild costs 20–45 min |
| `snapshots/` | Old firmware / design-handoff zips |

## Running the tests
```bash
python -m pytest cloud/tests/ -q
```
709 tests, no AWS credentials or network required (DynamoDB and every external
API are faked). Three failures in `TestWriteCrmRecordUpdateExpression` /
`test_duplicate_delivery_does_not_reprocess` are pre-existing and unrelated to
layout.

## Building the firmware
```bash
cd device && pio run              # build
pio run -t upload                 # flash (auto-detects COM port)
pio device monitor -b 115200      # serial
```

## Pipelines
1. **Upload backend** (Stage 1) — device gets a presigned PUT URL and uploads a
   `.wav`. See the run order below.
2. **Transcribe & Sync** — when a `.wav` lands in S3, it's transcribed
   (ElevenLabs Scribe) and summarized (Groq), stored in DynamoDB, and pushed to
   Salesforce (seam).
   **See [README_TRANSCRIBE.md](README_TRANSCRIBE.md)** for the full flow, the
   DynamoDB-vs-Aurora rationale, the Salesforce customization seam, and
   re-run/ops instructions.

## Setup
1. `cp .env.example .env` and fill in `BUCKET_NAME` (already set for this
   deployment: `meeting-recorder-shubham`).
2. Ensure `aws configure` is done with an IAM user that can manage
   DynamoDB / Lambda / IAM / API Gateway and `s3:PutObject` on the bucket.
3. Follow the stage run order (documented as stages are completed).

_Full run order, device contract, security model, and later seams are
documented at the end of the build._

## User-owned architecture (pairing & ownership)

The backend is the source of truth for device ownership, pairing, upload
authorization, and recording ownership. **Users own recordings; a device is
only an authenticated hardware endpoint.**

### Data model (DynamoDB)
| Table | Key | Purpose |
|---|---|---|
| `DeviceKeys` | `apiKey` | Device **authentication** (apiKey → deviceId). Unchanged firmware contract. |
| `Devices` | `device_id` | Device **ownership/lifecycle**: `status` (`UNPAIRED`\|`PAIRING`\|`PAIRED`), `paired_user_id`, `paired_at`, `last_seen`, `firmware_version`, `serial_number`. GSI `paired-user-index`. |
| `Users` / `UserDevices` | `user_id` / `user_id`+`device_id` | Accounts; `UserDevices` is the **legacy** claim link kept for pre-user_id recordings. |
| `Recordings` | `audio_s3_key` | Per-recording metadata incl. `recording_id`, `user_id`, `device_id`, `duration`, `s3_key`. GSIs `device-index` (legacy) and `user-index` (user-owned). |
| `Folders` | `folder_id` | Meeting grouping, per owner. `name`, `name_lc`, `description`. GSI `owner-index` (`owner_user_id`+`name_lc`) doubles as the duplicate-name check. Also holds `name#{owner}#{name_lc}` **uniqueness-claim** rows, filtered out of every read. |
| `Contacts` | `contact_id` | A **person**, globally unique per owner — never per folder. `name`, `email`/`email_lc`, `phone`/`phone_e164`, `company`, `role`, `minutex_user_id`. GSIs `owner-index`, `owner-email-index`, `owner-phone-index` (the last two **sparse**, so a contact with no email is not a dedupe candidate). |
| `FolderContacts` | `folder_id`+`contact_id` | Many-to-many. The composite key **is** the uniqueness constraint — a double add is an overwrite, not a duplicate. GSI `contact-index` for the reverse lookup. |
| `MeetingParticipants` | `audio_s3_key`+`speaker_id` | Speaker label → contact, per meeting. The transcript keeps its `"0"`/`"1"` labels; this sits beside it. GSI `contact-index`. |
| `Tasks` | `task_id` | **First-class** tasks, queryable across meetings. `title`, `status`, `priority`, `due_date`, `assignee_contact_id`, `assignee_user_id`, `resolution_status`, `folder_id`, `source_recording_id`, `fingerprint`. GSIs `owner-index`, `meeting-index`, `folder-index`, `assignee-index`, `dedupe-index`. |

### Upload authorization
`GET /get-upload-url` (device: `x-api-key`, optional verified `x-device-id`)
resolves apiKey → deviceId → Devices row, and **rejects with
`403 Device not paired`** unless `status == PAIRED` with a `paired_user_id`.
Uploads land at `recordings/{user_id}/{device_id}/{recording_id}.wav` and an
ownership stub row is upserted at presign time. Every authenticated device
call also bumps `Devices.last_seen` (`/get-upload-url`, `POST
/device/heartbeat`, `POST /device/upload-complete`). `user_id` is **never**
taken from a request — always resolved server-side.

### Pairing API (userApi, JWT)
| Route | Behaviour |
|---|---|
| `POST /devices/pair-request {device_id}` | 6-digit code, 5-min expiry, `status → PAIRING`. `409` if already paired. |
| `POST /devices/pair {device_id, pairing_code}` | Validates code/expiry/account, firmware confirmation **mocked** (`FirmwareVerifier` seam), sets `PAIRED` + `paired_at`. `409`/`403`/`410` on conflict/bad code/expiry. |
| `GET /devices` | Ids (legacy shape) + `details` rows for the caller's devices. |
| `GET /devices/{device_id}` | Owner-only detail (`firmware_version`, `last_seen`, `paired_at`, `serial_number`, `status`; `battery`/`storage` are null for now). |
| `PATCH /devices/{device_id} {name}` | Rename (≤64 chars). `""` clears it back to the `device_id`. Owner-only (`404` otherwise). |
| `DELETE /devices/{device_id}` | Unpair: releases ownership only. Recordings/AI data/device row survive; the departing owner's `user_id` is stamped onto the device's legacy rows first. |
| `POST /devices/{device_id}/factory-reset` | Asks the firmware to wipe itself (**mocked**, `FirmwareVerifier.factory_reset`) **first**, then unpairs and clears the name, stamping `factory_reset_at`. A device that doesn't acknowledge stays paired (`502`), so the call is safe to retry. Recordings are kept, exactly like unpair. |
| `POST /devices/claim {apiKey}` | **Legacy** — bridged into the same single-owner rules; new apps should never handle the device API key. |

One device belongs to at most **one** user, enforced with DynamoDB
conditional writes. The mobile app only ever sees `device_id` + pairing
code — never the device API key.

### Migration (run in order, idempotent)
```
bash cloud/scripts/14_create_devices_table.sh        # Devices table + GSI
bash cloud/scripts/15_add_recordings_user_index.sh   # Recordings user-index GSI
python cloud/scripts/16_backfill_devices.py          # existing devices -> UNPAIRED
#   add --adopt-claims to keep a live fleet uploading (promotes
#   single-user legacy claims to PAIRED; multi-user conflicts reported)
bash cloud/scripts/17_wire_pairing_routes.sh         # API Gateway routes
bash cloud/scripts/18_wire_upload_routes.sh          # MOBILE/UPLOAD routes + IAM + S3 suffixes
bash cloud/scripts/19_deploy_multisource_lambdas.sh  # userApi + transcribeRecording
bash cloud/scripts/20_deploy_device_mgmt.sh          # rename/factory-reset routes +
#   the PAIRING-GATED device presign (cloud/functions/device-presign/)
```

> **Script 20 changes live device behaviour.** Until it runs, `getUploadUrl`
> is the original device-centric build: any valid API key gets a URL and keys
> are written as `{device_id}/{meeting_id}_{ts}.wav`. After it runs, an
> **unpaired device gets `403 Device not paired`**. `esp32-001` is currently
> `UNPAIRED` with five competing legacy `UserDevices` claims, so
> `16_backfill_devices.py --adopt-claims` cannot pick an owner for it — pair
> it explicitly (app, or `/devices/pair-request` + `/devices/pair`) to restore
> its uploads.
New Lambda env vars: `DEVICES_TABLE` (both), `PAIRED_USER_INDEX`,
`USER_INDEX`, `PAIRING_CODE_TTL` (userApi). Tests:
`python cloud/tests/run_tests.py` (upload contract incl. unpaired-403),
`python tests/test_pairing_api.py` (pairing lifecycle),
`python tests/test_device_mgmt.py` (rename/factory-reset + the
pairing-gated presign, against real AWS),
`python tests/test_mock_device.py` (the virtual device, offline — no AWS) and
`python tests/test_recording_sources.py` (MOBILE/UPLOAD sources; add
`--live` for the real-AWS end-to-end pass).

### Mock device layer
`tests/device_simulator.py` is the **wire contract** (exact bytes the firmware
sends). `tests/mock_device.py` is the **virtual device**: a stateful `MockDevice`
with battery, storage, firmware, Wi-Fi/BLE links, a recording state machine
(`IDLE`/`RECORDING`/`PAUSED`) and an offline upload queue that retries, driving
the real backend. It makes the failure modes testable without hardware — full
card, low battery, recording while offline, uploading while unpaired.

```
python tests/mock_device.py --wav tests/sample.wav          # one-shot upload
python tests/mock_device.py --offline-first                 # queue, then sync
```

## Recording sources (MOBILE / UPLOAD / DEVICE — one pipeline)

MinuteX is a complete AI meeting assistant **without** the hardware: the
MinuteX recorder is one of three recording sources, all feeding **exactly
the same** backend pipeline (upload → S3 → transcription → AI notes → chat
→ timeline). Only the entry point differs.

| Source | Entry point | Auth | S3 key |
|---|---|---|---|
| `DEVICE` | `GET /get-upload-url` (getUploadUrl Lambda) | `x-api-key` + PAIRED gate | `recordings/{user_id}/{device_id}/{recording_id}.wav` |
| `MOBILE` | `POST /recordings/upload-request` (userApi) | JWT | `recordings/{user_id}/mobile/{recording_id}.{ext}` |
| `UPLOAD` | `POST /recordings/upload-request` (userApi) | JWT | `recordings/{user_id}/uploads/{recording_id}.{ext}` |

`mobile` / `uploads` are **reserved** path segments (a device can never be
provisioned with those ids), so one key parser serves all three layouts.
`recording_id` keeps the device convention `{meeting_id}_{timestamp}`
(e.g. `mobile-3fa8c2d91b_1754200000`). Every Recordings row carries
`source`; the userApi derives it from the key for legacy rows.

### Upload API (userApi, JWT)
| Route | Behaviour |
|---|---|
| `POST /recordings/upload-request` `{source, format?, title?, duration?, size?}` | Validates (`source ∈ {MOBILE, UPLOAD}`, format ∈ any common audio container — wav mp3 m4a aac ogg opus flac webm mp4 mp2 mpga amr 3gp aiff aif wma caf mka, ≤2 GB, ≤4 h), mints the recording identity, presigns the S3 PUT and upserts the timeline stub (`status="uploading"`, `device_id=null`). Returns `{upload_url, key, recording_id, expires_in, content_type}` — the PUT must send `Content-Type: <content_type>` (it is signed). |
| `POST /recordings/upload-complete` `{key, duration?}` | Owner-only; flips `uploading → uploaded` (never moves a status backwards — if transcription already advanced it, only `duration` is filled in). |

Env (userApi, all optional): `MAX_UPLOAD_BYTES` (2 GB),
`MAX_DURATION_SECONDS` (4 h), `UPLOAD_URL_EXPIRY` (900 s). The userApi role
needs `s3:PutObject` on `recordings/*` for its presigned PUTs to be
honored — script 18 attaches it.

### Processing status (all sources)
```
uploading -> uploaded -> transcribing -> generating_ai -> complete
                                      \-> failed (retried by S3/Lambda)
```
`transcribed` is the legacy "transcript ok, AI step failed" terminal state.
The transcribe Lambda stamps the transitions; the app polls while anything
is in flight, so the timeline and detail views update on their own. The S3
trigger is unfiltered (script 18 replaces the `.wav`-only notification) —
the transcribe Lambda skips non-audio keys by extension, and since it never
writes to S3 an unfiltered trigger cannot loop.

### Mobile app
The app's one upload path is `lib/uploads.tsx` (**UploadManager**):
request → PUT → complete, used by both the phone recorder
(`src/app/record-phone.tsx`, expo-audio: record/pause/resume/stop/cancel +
background recording) and the file importer (`src/app/upload.tsx`,
expo-document-picker + client-side validation). `+ New Recording` (Files
screen and the center tab button) opens the source chooser
(`src/app/new-recording.tsx`); the device option shows *Coming Soon → Pair
Device* until a device is paired. The timeline shows a source icon, title,
duration, and AI/transcript status for every recording; the detail screen
adds device name/id for DEVICE recordings only.

> **Deploy note:** the LIVE `transcribeRecording` in ap-south-1 is the Python
> build in `cloud/functions/transcribe/` (ElevenLabs Scribe + Groq analysis).
> `lambda-transcribe/index.mjs` is the older Node build, kept for reference
> only — it is **not** what runs. Deploy the Python one with
> `scripts/21_deploy_ai_workspace.sh`.

---

## AI Meeting Workspace

After a recording is processed, the app lands on a **workspace of AI output**,
not on the transcript. The transcript is still the source of truth — it just
isn't the first thing you read. One scroll, in this order:

```
Audio Player → Executive Summary → Meeting Highlights
    → AI Documents → Quick AI → Ask MinuteX → Transcript
```

### One Groq integration, two entry points

There is exactly **one** Groq client in the backend. It lives in
`cloud/shared/` and is vendored (flat) into both Lambda zips:

| Module | Owns |
|---|---|
| `cloud/shared/groq_client.py` | The client: request shape, `response_format=json_object`, the 429 policy (prefers Groq's own "try again in 6.5s"), TPM chunk budgeting, `map_reduce()` |
| `cloud/shared/prompts.py` | **Every** prompt template. A document type is one dict entry — no prompt text exists anywhere else |
| `cloud/shared/ai_schema.py` | Strict coercion (a coercer never raises), merge/de-dupe, `AI_VERSION`, and the cache `fingerprint()` |

```
                    cloud/shared/  (groq_client · prompts · ai_schema)
                            |
        +-------------------+--------------------+
        |                                        |
  transcribeRecording  (S3 trigger)        userApi  (JWT)
  staged, nobody waiting:                  on demand, user waiting:
    ElevenLabs -> transcript                 documents
    Groq -> executive summary                Quick AI
    Groq -> meeting_highlights               chat
                                             highlights (regenerate)
```

Why shared rather than duplicated: the TPM limit is a **per-account** quota, so
both callers spend from one bucket and must pace identically to be correct.

### Staged generation (spec: generate in stages, show progress)

```
Transcript ready -> Executive Summary -> Meeting Highlights -> Chat ready
```

Summary and highlights are two **separate** Groq calls. They are not one call
with more fields: extraction (numbers, deadlines) has different failure modes
than prose summarization, and asking for both at once measurably weakened the
numbers. Each degrades independently — if highlights are rate-limited, the
brief still ships and the workspace regenerates them on first open. That is
also how the **existing back catalogue** gets highlights: no backfill job.

`GROQ_DEADLINE_SECONDS` (180) is split 60/40 between the two stages, so a slow
summary cannot starve highlights.

### AI API (userApi, JWT)

> **Path shape:** the action comes **before** the recording key —
> `/recordings/ai/{action}/{key+}`. API Gateway rejects a greedy path variable
> in any but the final position (`BadRequestException: Greedy variables may
> only be in last position of route key`, verified against live AWS), and a
> recording key contains slashes so it must be greedy and must be last.
> Percent-encode the key whole (`encodeURIComponent`). Do not "tidy" these to
> `/recordings/{key+}/chat` — that route cannot be created.

| Route | Behaviour |
|---|---|
| `GET /recordings/ai/documents/{key+}` | `{documents, available}` — what's been generated, plus all 8 types with a `fresh` flag, so the UI renders every button in one call |
| `POST /recordings/ai/documents/{key+}` `{type, regenerate?}` | Generates (or serves the cache) → `{document, cached}` |
| `PATCH /recordings/ai/documents/{key+}` `{type, content}` | Stores a user edit and marks it `edited` — never overwritten by a later generation |
| `POST /recordings/ai/quick/{key+}` `{action, regenerate?}` | Quick AI. Aliased actions share the document's prompt **and** cache entry |
| `POST /recordings/ai/highlights/{key+}` `{regenerate?}` | `meeting_highlights`, generating them if absent |
| `GET /recordings/ai/chat/{key+}` | `{chat_history, suggestions}` — suggestions come from the backend so a new prompt ships without an app release |
| `POST /recordings/ai/chat/{key+}` `{message, history?}` | `{reply, chat_history}` |
| `DELETE /recordings/ai/chat/{key+}` | Clears the thread |

Statuses: **409** = transcript not ready yet (the app keeps showing progress,
not an error). **502** = Groq failed but may succeed on retry → "Unable to
generate AI output. Retry". **500** = configuration error, retrying won't help.
**404** for a recording you don't own — never 403, so the routes can't be used
to probe for other users' recordings. The transcript is never touched by a
failed generation.

**Document types** (8): `minutes_of_meeting`, `executive_summary`,
`follow_up_email`, `whatsapp_summary`, `action_items`,
`sales_meeting_report`, `site_visit_report`, `customer_requirement_report`.

**Quick actions** (12): the six above that alias a document
(`minutes_of_meeting`, `follow_up_email`, `whatsapp_update`, `action_items`,
`sales_summary`, `site_visit_report`) plus six standalone extractions
(`decisions`, `deadlines`, `risks`, `budget`, `customer_requirements`,
`timeline`).

### Caching

The rule is *transcript unchanged **and** document exists → serve the stored
copy; regenerate only when asked*. Each document stores the
`transcript_fingerprint` (a content hash) and `ai_version` it was generated
from; a mismatch is stale and regenerates transparently.

The cache keys on transcript **content**, not a timestamp — reprocessing
rewrites `updated_at` even when the transcript comes back byte-identical, and
that would throw away perfectly good documents. Conversely a re-transcription
that genuinely changes the text invalidates every document built on the old
one. A **user-edited** document is always fresh, so an edit is never silently
replaced.

### Data model additions (Recordings row)

| Attribute | Written by | Notes |
|---|---|---|
| `meeting_highlights` | transcribe / userApi | 6 sections. Only stored when **non-empty** — an all-empty result is indistinguishable from "never generated" and would make the cache serve emptiness forever |
| `documents` | userApi | Map of `type -> {content, format, generated_at, transcript_fingerprint, ai_version, edited}`. One map, not a table: always read with the recording, and one atomic `UpdateItem` per generation |
| `chat_history` | userApi | Capped at 40 turns; only the last 6 are sent to Groq |
| `transcript_fingerprint`, `ai_version` | both | Cache identity |

## Transcript storage: S3, not the DynamoDB item

A DynamoDB item is hard-capped at **400 KB**. The transcript used to be stored
inline in *two* shapes at once — `transcript` (flat diarized text) and
`timestamps` (the same words again as per-speaker-turn segments) — which on a
real 45-minute meeting measured **151 KB + 173 KB = 96% of the limit**, leaving
the ~10 KB of actual analysis to fit in what was left. The next longer recording
crossed 400 KB and the final `UpdateItem` threw:

```
ValidationException: Item size to update has exceeded the maximum allowed size
```

Two things made that inevitable rather than unlucky:

* **The text was stored twice.** The segments' text and the flat transcript are
  the same words; only the `"Speaker N: "` prefixes and newlines differ.
* **DynamoDB counts UTF-8 bytes, not characters.** That 151 KB was only ~46k
  chars — Devanagari at ~3 bytes/char. A Hindi/Marathi meeting therefore hit the
  ceiling at roughly a **third** the duration an English one would.

The failure also stranded the row: the small status writes
(`transcribing` → `generating_ai`) succeeded, only the big final write failed, so
the app polled a status that never advanced and showed "writing the summary"
forever — after ElevenLabs and Groq had already been paid.

**Now:** `cloud/shared/transcript_store.py` writes both to one gzipped S3 object
and the row keeps only a pointer.

| Attribute | Notes |
|---|---|
| `transcript_s3_key` | `transcripts/{audio_key}.transcript.json.gz` — absent on legacy rows |
| `transcript_chars`, `timestamps_count` | Counts, so a list view can say "has a transcript" without an S3 GET |

Measured on the row that failed: **334.8 KB → 10.9 KB** (83% of the limit → 2%,
36× headroom), S3 object 42.5 KB (gzip 6.1×).

* **`hydrate()` is the one read path.** It refills `transcript`/`timestamps`
  under their original names, so `get_recording` returns a **byte-identical**
  JSON shape and the app needs **no change and no new build**.
* **Legacy rows still work.** Rows written before this keep their inline values
  and no pointer; `hydrate()` serves those as-is, with zero S3 GETs. No backfill
  is required — both layouts coexist indefinitely.
* **S3 is written before DynamoDB.** The two writes are not atomic; an orphaned
  S3 object is harmless, but a row pointing at a missing object is a recording
  whose transcript is gone.
* **IAM:** `transcribeRecording` needs `s3:PutObject` on `transcripts/*` (scoped
  to that prefix so it can never overwrite a user's audio). Added by script 19.
* Routes that only need the cache fingerprint use `_row_fingerprint()` and
  `_owned_recording(event, hydrate=False)` to skip the S3 read entirely.

```bash
python tests/test_transcript_store.py       # 16 offline unit tests, no AWS
python cloud/scripts/26_reprocess_stuck.py        # dry run: list stranded rows
python cloud/scripts/26_reprocess_stuck.py --yes  # re-run them (costs ElevenLabs+Groq)
```

### Env (userApi — new)

`GROQ_API_KEY` (**required**, it never called Groq before), `GROQ_MODEL`,
`GROQ_TPM_LIMIT` (12000), `ONDEMAND_DEADLINE_SECONDS` (22). The Lambda timeout
is raised to **29s** — API Gateway's integration timeout caps there, so a
longer Lambda timeout cannot help; the generation code returns a partial result
rather than being killed mid-flight.

### Deploy & test

```bash
bash cloud/scripts/21_deploy_ai_workspace.sh   # vendors cloud/shared/ into both zips
python tests/test_ai_workspace.py        # 104 offline unit tests, no AWS/Groq
python tests/test_ai_api.py --live       # live end-to-end, spends Groq quota
```

The deploy script self-checks the shared modules import flat and runs the unit
suite before it ships anything.

### App

`lib/workspace.tsx` holds the sections (each owns its generation state and
fails independently); `src/app/recording/[key].tsx` composes them.
`lib/export-doc.ts` does Copy / Share / Markdown / PDF —
**PDF needs `expo-print`**, so it is hidden until the next EAS dev-client build
(`eas build --profile development`); everything else works today. The audio
player publishes `seekTo` through context, which is what makes tapping a
transcript timestamp jump the audio, and playback speed cycles 1 / 1.25 / 1.5 /
2 / 0.75×.

## Contacts, Folders & first-class Tasks

Meetings had no organization and tasks had no identity. A task lived inside its
recording row, so "what do I owe this week" could not be answered without
opening meetings one at a time, and an assignee was a bare name string that
nothing could notify.

**One master collection.** Every meeting lives in All Meetings exactly once and
carries at most one `folder_id`. Folders are **views**, not containers: moving a
meeting rewrites one attribute and never copies the row, `folder_id` absent
means General, and deleting a folder moves its meetings back to General without
deleting anything.

**One contact per person.** A contact is global per account, never owned by a
folder — "Rahul in Client Alpha" and "Rahul in Product" are the same row reached
from two folders, so editing him once edits him everywhere.

**Identity is never guessed.** This is the rule that shapes the whole design.
The AI hears *"Rahul, send the proposal by Friday"* and records the **name** and
**which speaker** said it. It does not decide which Rahul that is, and neither
does the backend — not even when exactly one contact has that name, because two
people sharing a name is ordinary and silently assigning work to the wrong
person is the failure worth engineering against. So:

| `resolution_status` | Meaning |
|---|---|
| `RESOLVED` | A real contact is attached; `assignee_contact_id` is set. |
| `UNRESOLVED` | Only a name is known. Kept verbatim in `assignee_name_legacy`; the app offers to resolve it. |
| `AMBIGUOUS` | Several contacts could match — the user chooses. |
| `NONE` | Nobody assigned. A normal state, not an error. |

Dedupe is by **strong identifier only**: a matching email or phone returns the
existing contact; a matching *name* answers `409` with the candidates so the UI
can ask "which Rahul?".

**Speaker → contact → user.** Mapping a speaker is what closes the loop:
`speaker_0` → contact → `minutex_user_id`, at which point the task is
notification-ready. The transcript is never rewritten — diarization labels stay
`"0"`/`"1"` forever and the existing `speaker_names` map (plus its
`speaker_mapping_version` counter, which invalidates generated documents) is
kept in step.

### API (userApi, JWT)

| Method | Path | Purpose |
|---|---|---|
| `POST`/`GET` | `/folders` | Create / list (with `meeting_count` and `general_count`) |
| `GET`/`PATCH`/`DELETE` | `/folders/{folder_id}` | Get (with contacts) / rename / delete |
| `GET` | `/folders/{folder_id}/contacts` | The folder's people |
| `POST`/`DELETE` | `/folders/{folder_id}/contacts/{contact_id}` | Associate / dissociate |
| `POST`/`GET` | `/contacts` | Create (`409` on ambiguous name) / list with `?search`, `?limit`, `?cursor` |
| `GET`/`PATCH`/`DELETE` | `/contacts/{contact_id}` | Get (with folders) / edit / delete |
| `PATCH` | `/recordings/folder/{key+}` | File a meeting; `folder_id: null` → General |
| `GET`/`PUT` | `/recordings/participants/{key+}` | Speaker mapping (`PUT` reports `tasks_resolved`) |
| `GET` | `/tasks` | Cross-meeting query: `?status`, `?folder_id`, `?assignee_contact_id`, `?recording_key`, `?overdue`, `?assigned_to_me`, `?due_before`, `?limit`, `?cursor` |
| `GET`/`PATCH` | `/tasks/{task_id}` | Detail (task + contact + folder + meeting) / update |
| `POST` | `/tasks/{task_id}/resolve` | Answer "which Rahul?" |
| `GET` | `/tasks/{task_id}/assignee-candidates` | Who a name might mean, folder members first |

The four existing `/recordings/ai/tasks/{key+}` routes keep their route keys and
change behaviour only — served from the `Tasks` table now, with a
backward-compatible response shape, so no coordinated app release was needed.

**Every filter is server-side and every list is paged.** `overdue` is *computed*
from `due_date` + `status` rather than stored, so it is right now rather than
right as of the last write.

### Task migration (nothing is destroyed)

The embedded `recording.tasks` map is **still written and never cleared**:

1. Reads come from the `Tasks` table; writes go to both (`_mirror_task_to_recording`).
2. Each meeting migrates **lazily** on first task read — idempotent via `legacy_task_id`.
3. `scripts/32_backfill_tasks.py` does the whole account eagerly, and `--verify` compares counts.
4. A legacy assignee becomes `UNRESOLVED` with the name kept — never a guessed contact.
5. Deleting a seeded task records a **tombstone** (`deleted_task_fingerprints`), so it does not come back on the next read.

The map stays as the rollback path until a later change retires it.

### Deploy & test

```bash
bash cloud/scripts/31_create_workspace_tables.sh   # 5 tables + GSIs (idempotent)
bash cloud/scripts/33_deploy_workspace_org.sh      # IAM, env, code, 21 routes
python cloud/scripts/32_backfill_tasks.py --dry-run
python cloud/scripts/32_backfill_tasks.py
python cloud/scripts/32_backfill_tasks.py --verify

python tests/test_workspace_org.py           # 130 offline unit tests, no AWS
python tests/test_workspace_api.py --live    # live end-to-end
```

The deploy script refuses to run if the tables are missing, and simulates the
role's `dynamodb:Query` on a GSI before finishing — the table-vs-index ARN
mistake otherwise makes every list route return empty instead of failing loudly.

### App

`src/app/folders.tsx`, `folder/[id].tsx`, `contacts.tsx`, `contact/[id].tsx`,
`tasks.tsx` and `task/[id].tsx`, reached from **You › Organize**;
`recording/[key]/participants.tsx` and a Move-to-folder sheet hang off the
meeting overflow menu. `lib/contact-picker.tsx` is the shared "choose a person"
sheet — it offers the meeting's folder contacts first, then all contacts, and
renders the ambiguity fork rather than silently merging or duplicating.
