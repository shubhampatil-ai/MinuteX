/**
 * upsert_meeting: validate a MinuteX meeting payload and create or update
 * exactly one MinuteX_Site_Meeting__c for it.
 *
 * CREATE vs UPDATE is keyed on payload.meeting.id (Meeting_Id__c, a unique
 * external id): the first delivery for a meeting creates the record, every
 * later delivery for the same meeting updates it.
 *
 * DUPLICATES. Every request carries an idempotency_key. A key that has
 * already been processed successfully is answered from its
 * MinuteX_Site_Request__c row with duplicate=true and NOTHING is written.
 * That is what makes a MinuteX retry after a timeout safe: the first attempt
 * may have committed here even though MinuteX never saw the response.
 * Failed requests are never logged, so fixing the data and retrying with the
 * same key is processed normally.
 *
 * CONCURRENCY. The meeting row is locked (FOR UPDATE) BEFORE the idempotency
 * check, so two concurrent deliveries for one meeting serialize: the second
 * waits, then sees the first one's committed idempotency row. Two concurrent
 * FIRST deliveries have no row to lock; the loser hits DUPLICATE_VALUE on
 * the unique key, rolls back and retries once as an update.
 *
 * STALE WRITES. `sent_at` is when MinuteX generated the content. A delivery
 * older than the content already on the record (a slow retry overtaken by a
 * newer push) is acknowledged as stale_ignored instead of overwriting newer
 * data with older.
 *
 * NO SILENT TRUNCATION. A value too long for its field is a 422 naming the
 * field. The transcript limit is the Long Text Area maximum, 131,072.
 *
 * RELATIONSHIP (contract 1.2, optional). payload.relation.who is the Contact
 * or Lead the meeting was with; payload.relation.what the Account,
 * Opportunity or other record it is about. Stored as object/id/name text plus
 * link formulas (a custom object cannot hold a polymorphic lookup). Each id
 * must be a real id of the object named for it. A delivery without a side
 * clears it, so the record always shows the meeting's current relationship.
 */
public without sharing class MinuteXSiteMeetings {
    public static final Integer MAX_TRANSCRIPT_CHARS = 131072;
    public static final Integer MAX_LONG_TEXT_CHARS = 131072;
    public static final Integer MAX_SUMMARY_CHARS = 32768;
    public static final Integer MAX_AUDIO_URL_CHARS = 4000;
    static final Pattern MEETING_ID = Pattern.compile('[A-Za-z0-9_:.\\-]{1,255}');
    static final Pattern API_NAME = Pattern.compile('[A-Za-z][A-Za-z0-9_]{0,79}');
    static final Pattern SF_ID = Pattern.compile('[A-Za-z0-9]{15}([A-Za-z0-9]{3})?');
    static final Pattern ISO_DATETIME = Pattern.compile('[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+\\-][0-9]{2}:?[0-9]{2})?');

    public class Outcome {
        public Map<String, Object> result;
        public Boolean duplicate = false;
    }

    public static Outcome upsertMeeting(Map<String, Object> envelope) {
        String requestId = MinuteXSiteReceiver.str(envelope.get('request_id'));
        String idempotencyKey = MinuteXSiteReceiver.str(envelope.get('idempotency_key'));
        if (String.isBlank(idempotencyKey) || idempotencyKey.length() > 255) {
            throw MinuteXSiteException.forField(400, 'invalid_request', 'idempotency_key',
                'idempotency_key is required (at most 255 characters).');
        }
        Map<String, Object> payload = MinuteXSiteReceiver.obj(envelope.get('payload'), 'payload');
        Map<String, Object> meeting = MinuteXSiteReceiver.obj(payload.get('meeting'), 'payload.meeting');
        String meetingId = MinuteXSiteReceiver.str(meeting.get('id'));
        if (!MEETING_ID.matcher(meetingId).matches()) {
            throw MinuteXSiteException.forField(422, 'validation_failed', 'payload.meeting.id',
                'payload.meeting.id is required and may contain only letters, digits, _ : . -');
        }

        // Validate and build the record BEFORE any lock or DML, so a bad
        // payload costs nothing and leaves nothing behind.
        MinuteX_Site_Meeting__c incoming = buildRecord(envelope, payload, meeting, meetingId,
            requestId, idempotencyKey);
        Datetime sentAt = incoming.Last_Payload_At__c;

        for (Integer attempt = 0; attempt < 2; attempt++) {
            Savepoint sp = Database.setSavepoint();
            try {
                List<MinuteX_Site_Meeting__c> existing = [
                    SELECT Id, Last_Payload_At__c, Delivery_Count__c
                    FROM MinuteX_Site_Meeting__c
                    WHERE Meeting_Id__c = :meetingId
                    LIMIT 1 FOR UPDATE
                ];
                Outcome replay = replay(idempotencyKey);
                if (replay != null) {
                    return replay;
                }

                String operation;
                Id recordId;
                if (existing.isEmpty()) {
                    incoming.Delivery_Count__c = 1;
                    insert incoming;
                    operation = 'created';
                    recordId = incoming.Id;
                } else {
                    MinuteX_Site_Meeting__c current = existing[0];
                    recordId = current.Id;
                    if (sentAt != null && current.Last_Payload_At__c != null
                            && sentAt < current.Last_Payload_At__c) {
                        operation = 'stale_ignored';
                    } else {
                        incoming.Id = current.Id;
                        incoming.Delivery_Count__c = (current.Delivery_Count__c == null
                            ? 0 : current.Delivery_Count__c) + 1;
                        update incoming;
                        operation = 'updated';
                    }
                }

                Map<String, Object> result = new Map<String, Object>{
                    'operation' => operation,
                    'meeting_id' => meetingId,
                    'record_id' => String.valueOf(recordId),
                    // Where a person opens this meeting in Salesforce (the
                    // org's My Domain, Lightning record page).
                    'record_url' => recordUrl(recordId)
                };
                insert new MinuteX_Site_Request__c(
                    Idempotency_Key__c = idempotencyKey,
                    Request_Id__c = requestId.left(64),
                    Action__c = 'upsert_meeting',
                    Meeting_Id__c = meetingId,
                    Outcome__c = operation,
                    Response_JSON__c = JSON.serialize(result),
                    Processed_At__c = Datetime.now()
                );
                Outcome out = new Outcome();
                out.result = result;
                return out;
            } catch (DmlException e) {
                Database.rollback(sp);
                if (e.getDmlType(0) != StatusCode.DUPLICATE_VALUE || attempt > 0) {
                    throw e;
                }
                // Lost a race: either the same key committed first (the
                // replay check on the next pass answers it) or another push
                // created the meeting first (the next pass updates it).
            }
        }
        throw MinuteXSiteException.of(503, 'conflict_retry', 'Concurrent delivery conflict; retry.');
    }

    static String recordUrl(Id recordId) {
        try {
            return URL.getOrgDomainUrl().toExternalForm()
                + '/lightning/r/MinuteX_Site_Meeting__c/' + String.valueOf(recordId) + '/view';
        } catch (Exception e) {
            return '';
        }
    }

    static Outcome replay(String idempotencyKey) {
        List<MinuteX_Site_Request__c> prior = [
            SELECT Response_JSON__c FROM MinuteX_Site_Request__c
            WHERE Idempotency_Key__c = :idempotencyKey LIMIT 1
        ];
        if (prior.isEmpty()) {
            return null;
        }
        Outcome out = new Outcome();
        out.duplicate = true;
        out.result = String.isBlank(prior[0].Response_JSON__c)
            ? new Map<String, Object>()
            : (Map<String, Object>) JSON.deserializeUntyped(prior[0].Response_JSON__c);
        return out;
    }

    static MinuteX_Site_Meeting__c buildRecord(Map<String, Object> envelope, Map<String, Object> payload,
            Map<String, Object> meeting, String meetingId, String requestId, String idempotencyKey) {
        MinuteX_Site_Meeting__c r = new MinuteX_Site_Meeting__c();
        String title = MinuteXSiteReceiver.str(meeting.get('title'));
        r.Meeting_Id__c = meetingId;
        r.Title__c = limited(title, 255, 'payload.meeting.title');
        r.Name = String.isBlank(title) ? 'MinuteX meeting' : title.left(80);
        r.Status__c = limited(MinuteXSiteReceiver.str(meeting.get('status')), 40, 'payload.meeting.status');
        r.Source__c = limited(MinuteXSiteReceiver.str(meeting.get('source')), 40, 'payload.meeting.source');
        r.Language__c = limited(MinuteXSiteReceiver.str(meeting.get('language')), 20, 'payload.meeting.language');
        r.Started_At__c = isoDatetime(meeting.get('started_at'), 'payload.meeting.started_at');
        r.Ended_At__c = isoDatetime(meeting.get('ended_at'), 'payload.meeting.ended_at');
        r.Duration_Seconds__c = nonNegative(meeting.get('duration_seconds'), 'payload.meeting.duration_seconds');

        Map<String, Object> workspace = MinuteXSiteReceiver.optObj(meeting.get('workspace'), 'payload.meeting.workspace');
        r.Workspace_Id__c = limited(MinuteXSiteReceiver.str(workspace.get('id')), 255, 'payload.meeting.workspace.id');
        r.Workspace_Name__c = limited(MinuteXSiteReceiver.str(workspace.get('name')), 255, 'payload.meeting.workspace.name');

        String link = MinuteXSiteReceiver.str(meeting.get('url'));
        if (String.isNotBlank(link) && !link.startsWith('https://')) {
            throw MinuteXSiteException.forField(422, 'validation_failed', 'payload.meeting.url',
                'payload.meeting.url must be an https URL.');
        }
        r.MinuteX_URL__c = limited(link, 255, 'payload.meeting.url');

        Map<String, Object> summary = MinuteXSiteReceiver.optObj(payload.get('summary'), 'payload.summary');
        r.Summary__c = limited(MinuteXSiteReceiver.str(summary.get('text')), MAX_SUMMARY_CHARS, 'payload.summary.text');
        Map<String, Object> overview = MinuteXSiteReceiver.optObj(summary.get('overview'), 'payload.summary.overview');
        r.Overview__c = limited(MinuteXSiteReceiver.str(overview.get('text')), MAX_LONG_TEXT_CHARS, 'payload.summary.overview.text');
        r.Overview_JSON__c = jsonField(overview.get('sections'), 'payload.summary.overview.sections');
        r.Highlights_JSON__c = jsonField(summary.get('highlights'), 'payload.summary.highlights');
        List<Object> decisions = MinuteXSiteReceiver.optList(summary.get('decisions'), 'payload.summary.decisions');
        r.Decisions_JSON__c = jsonField(decisions, 'payload.summary.decisions');
        r.Decision_Count__c = decisions.size();

        List<Object> participants = MinuteXSiteReceiver.optList(payload.get('participants'), 'payload.participants');
        r.Participants_JSON__c = jsonField(participants, 'payload.participants');
        r.Participant_Count__c = participants.size();
        List<Object> actionItems = MinuteXSiteReceiver.optList(payload.get('action_items'), 'payload.action_items');
        r.Action_Items_JSON__c = jsonField(actionItems, 'payload.action_items');
        r.Action_Item_Count__c = actionItems.size();

        Map<String, Object> transcript = MinuteXSiteReceiver.optObj(payload.get('transcript'), 'payload.transcript');
        String text = MinuteXSiteReceiver.str(transcript.get('text'));
        if (text.length() > MAX_TRANSCRIPT_CHARS) {
            throw MinuteXSiteException.forField(422, 'transcript_too_large', 'payload.transcript.text',
                'The transcript is ' + text.length() + ' characters; the maximum is '
                + MAX_TRANSCRIPT_CHARS + '. Nothing was written.');
        }
        r.Transcript__c = String.isBlank(text) ? null : text;
        r.Transcript_Chars__c = text.length();
        r.Transcript_Available__c = transcript.get('available') == true || String.isNotBlank(text);

        Map<String, Object> audio = MinuteXSiteReceiver.optObj(payload.get('audio'), 'payload.audio');
        String audioUrl = MinuteXSiteReceiver.str(audio.get('url'));
        if (String.isNotBlank(audioUrl) && !audioUrl.startsWith('https://')) {
            throw MinuteXSiteException.forField(422, 'invalid_audio_url', 'payload.audio.url',
                'payload.audio.url must be an https URL.');
        }
        r.Audio_URL__c = String.isBlank(audioUrl) ? null
            : limited(audioUrl, MAX_AUDIO_URL_CHARS, 'payload.audio.url');
        r.Audio_URL_Expires_At__c = isoDatetime(audio.get('expires_at'), 'payload.audio.expires_at');
        r.Audio_Size_Bytes__c = nonNegative(audio.get('size_bytes'), 'payload.audio.size_bytes');
        r.Audio_Content_Type__c = limited(MinuteXSiteReceiver.str(audio.get('content_type')), 100, 'payload.audio.content_type');
        r.Audio_Duration_Seconds__c = nonNegative(audio.get('duration_seconds'), 'payload.audio.duration_seconds');

        Map<String, Object> relation = MinuteXSiteReceiver.optObj(payload.get('relation'), 'payload.relation');
        String[] who = relationSide(relation, 'who');
        r.Who_Object_Type__c = who[0];
        r.Who_Record_Id__c = who[1];
        r.Who_Record_Name__c = who[2];
        String[] what = relationSide(relation, 'what');
        r.What_Object_Type__c = what[0];
        r.What_Record_Id__c = what[1];
        r.What_Record_Name__c = what[2];

        r.Contract_Version__c = limited(MinuteXSiteReceiver.str(envelope.get('contract_version')), 20, 'contract_version');
        r.Last_Request_Id__c = requestId.left(64);
        r.Last_Idempotency_Key__c = idempotencyKey;
        r.Last_Payload_At__c = isoDatetime(envelope.get('sent_at'), 'sent_at');
        return r;
    }

    /** [object type, record id, name] for one side, all null when absent. */
    static String[] relationSide(Map<String, Object> relation, String side) {
        String field = 'payload.relation.' + side;
        Map<String, Object> ref = MinuteXSiteReceiver.optObj(relation.get(side), field);
        String objectType = MinuteXSiteReceiver.str(ref.get('object_type'));
        String recordId = MinuteXSiteReceiver.str(ref.get('record_id'));
        if (String.isBlank(recordId)) {
            return new String[]{ null, null, null };
        }
        if (!API_NAME.matcher(objectType).matches() || !SF_ID.matcher(recordId).matches()) {
            throw MinuteXSiteException.forField(422, 'validation_failed', field,
                field + ' needs an object_type and a Salesforce record_id.');
        }
        SObjectType st = Schema.getGlobalDescribe().get(objectType.toLowerCase());
        Id parsed;
        try {
            parsed = Id.valueOf(recordId);
        } catch (Exception e) {
            parsed = null;
        }
        if (st == null || parsed == null || parsed.getSObjectType() != st) {
            throw MinuteXSiteException.forField(422, 'invalid_record', field,
                field + '.record_id is not a ' + objectType.left(80) + ' record id in this org.');
        }
        return new String[]{
            st.getDescribe(SObjectDescribeOptions.DEFERRED).getName(),
            String.valueOf(parsed),
            limited(MinuteXSiteReceiver.str(ref.get('name')), 255, field + '.name')
        };
    }

    static String limited(String value, Integer max, String field) {
        if (value != null && value.length() > max) {
            throw MinuteXSiteException.forField(422, 'field_too_large', field,
                field + ' is ' + value.length() + ' characters; the maximum is ' + max + '.');
        }
        return String.isBlank(value) ? null : value;
    }

    static String jsonField(Object value, String field) {
        if (value == null) {
            return null;
        }
        return limited(JSON.serialize(value), MAX_LONG_TEXT_CHARS, field);
    }

    static Datetime isoDatetime(Object value, String field) {
        String s = MinuteXSiteReceiver.str(value);
        if (String.isBlank(s)) {
            return null;
        }
        if (!ISO_DATETIME.matcher(s).matches()) {
            throw MinuteXSiteException.forField(422, 'validation_failed', field,
                field + ' must be an ISO 8601 date-time.');
        }
        try {
            return (Datetime) JSON.deserialize('"' + s + '"', Datetime.class);
        } catch (Exception e) {
            throw MinuteXSiteException.forField(422, 'validation_failed', field,
                field + ' must be an ISO 8601 date-time.');
        }
    }

    static Decimal nonNegative(Object value, String field) {
        if (value == null) {
            return null;
        }
        Decimal d;
        if (value instanceof Decimal) {
            d = (Decimal) value;
        } else if (value instanceof Integer) {
            d = (Integer) value;
        } else if (value instanceof Long) {
            d = (Long) value;
        } else if (value instanceof Double) {
            d = Decimal.valueOf((Double) value);
        } else {
            throw MinuteXSiteException.forField(422, 'validation_failed', field, field + ' must be a number.');
        }
        if (d < 0) {
            throw MinuteXSiteException.forField(422, 'validation_failed', field, field + ' must not be negative.');
        }
        return d.round(System.RoundingMode.HALF_UP);
    }
}
