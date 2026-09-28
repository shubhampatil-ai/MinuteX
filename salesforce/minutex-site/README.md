# MinuteX Site Receiver (Salesforce Site + Apex REST)

The receiver for MinuteX's **Salesforce Site delivery** (`delivery_type = salesforce_site`).
MinuteX sends an HTTPS POST to one Apex REST endpoint on a public Salesforce Site.
It needs no OAuth, no connected app, no External Client App and no package. The Site
endpoint is the **only** API boundary: meeting delivery and the org connection (object
discovery, record search, Tasks) go through the same URL and the same auth mode.

```
MinuteX ──HTTPS POST──▶ https://<site-domain>/services/apexrest/minutex/v1/
                         └─ MinuteXSiteReceiver (Apex REST, without sharing)
                              ├─ health
                              ├─ upsert_meeting ─▶ MinuteX_Site_Meeting__c
                              ├─ people_sync    ◀─ Contact + User (read only)
                              └─ org actions, within the INTEGRATION USER's permissions:
                                   org_info · object_schema · record_search
                                   create_task · update_task ─▶ Task
```

## What gets deployed

| Component | Purpose |
|---|---|
| `MinuteXSiteReceiver` | The REST endpoint (`/minutex/v1/*`), envelope validation and routing |
| `MinuteXSiteAuth` | Pluggable, fails closed. A `Verifier` per mode in `VERIFIERS`: `none` (development only), `hmac_sha256` |
| `MinuteXSiteMeetings` | Validates and upserts one meeting. Handles idempotency and rejects stale writes |
| `MinuteXSitePeople` | Returns Contacts, then Standard Users, paged by Id with a cursor |
| `MinuteXSitePermissions` | The integration user's object, field, Task and record access, computed from Salesforce's permission data |
| `MinuteXSiteSchema` | `org_info` and `object_schema` (objects, then fields, filtered by the integration user's access) |
| `MinuteXSiteRecords` | `record_search`: the records the integration user can read |
| `MinuteXSiteTasks` | `create_task` / `update_task` (WhatId/WhoId, owner, due date, picklists), idempotent |
| `MinuteX_Site_Meeting__c` | One record per MinuteX meeting. The external id `Meeting_Id__c` is the upsert key. **Related Person** / **Related To** link to the Contact (or Lead) and the record the meeting is about |
| `MinuteX Site Meeting Layout` / tab / `All` list view | Every meeting field on the record page, a **MinuteX Site Meetings** tab in App Launcher (visible to Viewer holders), and an All list |
| `MinuteX_Site_Request__c` | Idempotency log: one row per successfully processed `idempotency_key` |
| `MinuteX_Site_Settings__c` | Hierarchy custom setting: `Enabled__c`, `Auth_Mode__c`, `Shared_Secret__c`, `Max_Clock_Skew_Seconds__c`, `Integration_Username__c` |
| `MinuteX_Site_Guest` (perm set) | Apex class access **only**, for the Site guest user |
| `MinuteX_Site_Viewer` (perm set) | Read access to the two objects, for the people who view meetings |
| `MinuteXSiteReceiverTest` | Envelope, auth, meetings (including the relationship), people |
| `MinuteXSiteSalesforceTest` | Org actions, integration-user permissions, Who + What Tasks, pluggable auth |

## Setup

1. **Deploy the metadata**

   ```bash
   cd salesforce/minutex-site
   sf project deploy start --source-dir force-app --target-org <alias>
   sf apex run test --class-names MinuteXSiteReceiverTest --class-names MinuteXSiteSalesforceTest \
     --target-org <alias> --wait 10
   ```

2. **Create a Site.** Go to Setup → Sites, register a Sites domain if the org has none, then click New.
   Any label and path prefix works (for example `minutex`). Activate it. Set
   **Guest record default owner** (Site details) to the user who should own delivered meetings.

3. **Give the Site guest user the receiver class.** Go to Setup → Sites → *your site* → Public Access
   Settings → View Users → the guest user. Assign the **MinuteX Site Guest Access** permission set.
   The guest user needs no object permissions: the receiver runs without sharing and does its own authentication.

4. **Configure the receiver** (Setup → Custom Settings → MinuteX Site Settings → Manage →
   New, at the org-default level):

   | Field | Development | Production |
   |---|---|---|
   | Enabled | ✔ | ✔ |
   | Auth Mode | `none` | `hmac_sha256` |
   | Shared Secret | *(blank)* | 32+ random characters. Enter the same value in MinuteX |
   | Integration Username | the user whose permissions MinuteX follows | same |

   With no settings record, or with **Enabled** unchecked, every request gets a 403 `receiver_disabled`.
   Meeting delivery and people sync work without an Integration Username; the org actions
   answer 409 `integration_user_not_configured` until it names an active user.
   Use a dedicated user with exactly the object, field and Task access MinuteX should have.

5. **Let people see the records.** Assign **MinuteX Site Viewer** to anyone who should read
   delivered meetings, admins included. Source-deployed fields get no field-level security,
   so without this permission set even a System Administrator gets "No such column" in SOQL.

6. **Your receiver URL** is the Site's secure URL plus the Apex REST path:

   ```
   https://<your-domain>.my.salesforce-sites.com/<path-prefix>/services/apexrest/minutex/v1/
   ```

   Setup → Sites shows the Site URL. Don't use the org's My Domain URL (`*.my.salesforce.com`).
   That URL requires a login, and MinuteX reports it as `site_requires_login`.

## Contract (v1)

Every request is a JSON envelope (any `1.x` version is accepted; MinuteX sends `1.2`,
receiver version `1.4.0`):

```json
{ "contract_version": "1.2", "action": "upsert_meeting",
  "request_id": "…", "idempotency_key": "…", "sent_at": "ISO-8601",
  "payload": { "meeting": {}, "participants": [], "summary": {},
               "action_items": [], "transcript": {}, "audio": {},
               "relation": { "who": {"object_type", "record_id", "name"} | null,
                             "what": {"object_type", "record_id", "name"} | null } } }
```

1.2 is additive. A 1.1 receiver ignores `relation` and `related_records`, so the meeting
is stored without its relationship and a Task keeps its single primary record.

Every response is JSON with `ok`, `org_id` and either `result` or `error: {code, message, field}`.
MinuteX pins the org id when a workspace connects and refuses any answer carrying a different
one (`org_mismatch`), so repointing the Site URL at another org can't mix tenants.
The HTTP status tells MinuteX whether to retry:

| Status | Meaning | MinuteX |
|---|---|---|
| 200 | Done. `duplicate: true` means this `idempotency_key` was already processed and nothing was written | success |
| 400 | Malformed request (`malformed_json`, `malformed_request`, `unsupported_action`, `invalid_cursor`) | no retry |
| 401 / 403 | `auth_failed` / `receiver_disabled` / `receiver_misconfigured` / `permission_denied` | no retry |
| 409 | `integration_user_not_configured` / `integration_user_invalid` | no retry |
| 413 / 422 | Validation (`transcript_too_large`, `field_too_large`, `invalid_audio_url`, `validation_failed`, `salesforce_rejected`) | no retry |
| 503 / 500 | `record_locked`, `conflict_retry`, `internal_error` | retried with backoff |

**Meetings.** The first delivery of a `payload.meeting.id` creates the record. Later
deliveries update it. A delivery whose `sent_at` is older than the content already on the
record returns `stale_ignored`, so a slow retry can't overwrite newer data. Every failure
rolls back the whole request.

**Relationship (1.2).** `relation.who` is the Contact or Lead the meeting was with. MinuteX
fills it automatically from the synced Salesforce Contact tagged on a speaker. `relation.what`
is the Account, Opportunity or other record a person chose in MinuteX ("Change Salesforce
record"). Each is stored as object, id and name fields, with the **Related Person** and
**Related To** formula links. A custom object can't hold a polymorphic lookup. Each id must
be a real id of the object named for it, or the request gets a 422 `invalid_record`. A
delivery without a side clears that side.

**Object discovery (1.4.0).** `object_schema` (mode `objects`, paged by cursor) returns
every object the integration user can read, and MinuteX walks every page. Each object carries
`mru_enabled`, which says whether its records appear in "Recently Viewed". MinuteX builds its
object catalog from these results:
- It keeps every queryable custom object.
- It keeps standard objects whose records appear in Recently Viewed.
- It drops technical and system objects (by the platform's naming conventions) and its own
  objects.

A person first sees the recommended objects (Account, Contact, Lead, Opportunity, Case) plus
the ones their workspace added, and finds any other object by search.

**Where the meeting landed.** The `upsert_meeting` result carries `record_id` and
`record_url` (the org's My Domain Lightning page for the record). MinuteX shows "In
Salesforce: MinuteX Meeting · <title>" with **Open in Salesforce**. MinuteX attaches every
Task it pushes for a meeting to that record (Related To) and the meeting's Contact (Name),
so a meeting's Tasks can only be pushed after the meeting itself. The integration user
therefore needs read access to MinuteX Site Meetings: assign it **MinuteX Site Viewer**.

**Tasks: Who and What (1.2).** `create_task` / `update_task` accept `related_records`:
up to one person (Contact/Lead → `WhoId`) and one record (Account, Opportunity, custom
objects → `WhatId`). The org's own Task schema decides which field an object uses. When
`related_records` is present it is the whole relationship, so an update clears the field it
doesn't name. Two records for one field, or a Lead together with a What, get a 422
`invalid_relationship`. The result carries `last_modified_at`, the Task's `SystemModstamp`
after the write, which MinuteX keeps for a future two-way sync.

**Transcript.** Sent as text, up to **131,072 characters** (the Long Text Area maximum,
counted in UTF-16 units like Apex `String.length()`). A longer transcript gets a 422
`transcript_too_large` and is never truncated.

**Audio.** A MinuteX URL, never file bytes. On each request it checks access and redirects
to a short-lived download. It stops working when Site delivery is turned off, the audio key
is rotated, the meeting is deleted or `Audio_URL_Expires_At__c` passes. The next delivery
issues a fresh link.

**People.** `people_sync` returns `{people, next_cursor, done}`. MinuteX calls it until
`done`, stores the results in its own People Store and tags participants from that store.
It never queries Salesforce while tagging.

**Org actions.** Through a Site, Apex runs as the guest user, so `describe` and
`USER_MODE` would answer for the guest, not for anyone meaningful. `MinuteXSitePermissions`
therefore computes the configured integration user's access from Salesforce's own data
(PermissionSetAssignment, ObjectPermissions, FieldPermissions, `ModifyAllData` /
`ViewAllData` / `EditTask`, and `UserRecordAccess` for record sharing). Discovery offers
only what that user can read, record search returns only records it can read, and a Task
is written only when it may create/edit Tasks, edit each field sent, and read the related record.

## Authentication is pluggable

The receiver picks a `Verifier` from `MinuteXSiteAuth.VERIFIERS` by the setting's
**Auth Mode**; an unknown mode fails closed (`receiver_misconfigured`). MinuteX mirrors this
with `site_delivery_client.register_auth_mode()`. Adding a production mode is one Verifier
here plus one registered factory in MinuteX, with no change to the actions, the envelope
or the org connection. The workspace's mode is chosen when its Site endpoint is configured
(`PUT /workspaces/{id}/site-delivery`), and the org connection simply uses it.

## Security notes

- `Auth Mode = none` lets **anyone who knows the Site URL** read every Contact and
  User through `people_sync`, write meeting records, and read or create whatever the
  integration user can (objects, records, Tasks). Use it only in a scratch or dev org.
- In `hmac_sha256` mode, MinuteX signs `"{unix seconds}.{raw body}"` and sends
  `X-MinuteX-Timestamp` and `X-MinuteX-Signature: v1=<hex>`. The receiver rejects requests
  more than 300 seconds old (`Max_Clock_Skew_Seconds__c`), which limits how long a captured
  request can be replayed.
- Admins with Setup access can read the shared secret in the custom setting.

## Limitations

- **Writes run in system mode after the permission checks.** The checks follow the
  integration user, but the DML itself runs as the Site guest user: a Task's `CreatedById`
  and `LastModifiedById` are the guest user, and validation rules or flows that test
  `$User` see the guest, not the integration user. `OwnerId` is set explicitly (the
  assignee, or the integration user when unassigned).
- **Field writability is reconstructed, not asked.** It combines FieldPermissions with
  the field's describe (formula, auto-number, system and compound-name fields are never
  writable). Restriction rules, record types and page-layout requirements are not modelled.
- **Permission set groups and muting permission sets** are counted through their
  PermissionSet rows as Salesforce reports them; confirm edge cases in the target org.
