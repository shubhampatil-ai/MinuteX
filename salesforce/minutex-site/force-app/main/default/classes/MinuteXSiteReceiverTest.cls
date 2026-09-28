@IsTest
private class MinuteXSiteReceiverTest {

    @TestSetup
    static void setup() {
        insert new MinuteX_Site_Settings__c(
            SetupOwnerId = UserInfo.getOrganizationId(),
            Enabled__c = true,
            Auth_Mode__c = 'none'
        );
    }

    // ---- harness ---------------------------------------------------------

    class Reply {
        Integer status;
        Map<String, Object> body;
        Map<String, Object> result() { return (Map<String, Object>) body.get('result'); }
        Map<String, Object> error() { return (Map<String, Object>) body.get('error'); }
    }

    static Reply post(String raw) {
        return post(raw, new Map<String, String>());
    }

    static Reply post(String raw, Map<String, String> headers) {
        RestRequest req = new RestRequest();
        req.requestURI = '/services/apexrest/minutex/v1/';
        req.httpMethod = 'POST';
        req.requestBody = Blob.valueOf(raw);
        for (String k : headers.keySet()) {
            req.addHeader(k, headers.get(k));
        }
        RestContext.request = req;
        RestContext.response = new RestResponse();
        MinuteXSiteReceiver.handlePost();
        Reply r = new Reply();
        r.status = RestContext.response.statusCode;
        r.body = (Map<String, Object>) JSON.deserializeUntyped(RestContext.response.responseBody.toString());
        return r;
    }

    static Map<String, Object> meetingEnvelope(String meetingId, String key, String sentAt, String transcript) {
        return new Map<String, Object>{
            'contract_version' => '1.0',
            'action' => 'upsert_meeting',
            'request_id' => 'req-' + key,
            'idempotency_key' => key,
            'sent_at' => sentAt,
            'payload' => new Map<String, Object>{
                'meeting' => new Map<String, Object>{
                    'id' => meetingId,
                    'title' => 'Quarterly review',
                    'status' => 'complete',
                    'started_at' => '2026-09-01T10:00:00Z',
                    'ended_at' => '2026-09-01T11:00:00Z',
                    'duration_seconds' => 3600,
                    'workspace' => new Map<String, Object>{ 'id' => 'org-1', 'name' => 'ABC Realty' }
                },
                'participants' => new List<Object>{
                    new Map<String, Object>{ 'speaker_id' => '0', 'name' => 'Asha', 'email' => 'asha@example.com' }
                },
                'summary' => new Map<String, Object>{
                    'text' => 'We agreed the plan.',
                    'overview' => new Map<String, Object>{ 'text' => 'Plan:\n- ship', 'sections' => new List<Object>() },
                    'highlights' => new Map<String, Object>{ 'deadlines' => new List<Object>() },
                    'decisions' => new List<Object>{ new Map<String, Object>{ 'decision' => 'Ship it' } }
                },
                'action_items' => new List<Object>{ new Map<String, Object>{ 'title' => 'Send deck' } },
                'transcript' => new Map<String, Object>{ 'available' => true, 'text' => transcript },
                'audio' => new Map<String, Object>{
                    'url' => 'https://api.example.com/delivery/audio/tok',
                    'size_bytes' => 85000000,
                    'content_type' => 'audio/wav',
                    'duration_seconds' => 3600,
                    'expires_at' => '2026-10-01T10:00:00Z'
                }
            }
        };
    }

    static List<MinuteX_Site_Meeting__c> meetings(String meetingId) {
        return [SELECT Id, Title__c, Transcript__c, Transcript_Chars__c, Delivery_Count__c, Audio_URL__c,
                       Audio_Size_Bytes__c, Decision_Count__c, Participant_Count__c, Action_Item_Count__c,
                       Workspace_Name__c, Started_At__c
                FROM MinuteX_Site_Meeting__c WHERE Meeting_Id__c = :meetingId];
    }

    // ---- health / auth / envelope ---------------------------------------

    @IsTest
    static void healthReportsReceiverAndLimits() {
        Reply r = post('{"action":"health"}');
        System.assertEquals(200, r.status);
        System.assertEquals(true, r.body.get('ok'));
        System.assertEquals('ok', r.result().get('status'));
        System.assertEquals(131072, r.result().get('max_transcript_chars'));
        System.assertEquals('none', r.result().get('auth_mode'));
    }

    @IsTest
    static void disabledReceiverRefusesEverything() {
        MinuteX_Site_Settings__c s = MinuteX_Site_Settings__c.getOrgDefaults();
        s.Enabled__c = false;
        update s;
        Reply r = post('{"action":"health"}');
        System.assertEquals(403, r.status);
        System.assertEquals('receiver_disabled', r.error().get('code'));
    }

    @IsTest
    static void unconfiguredReceiverRefusesEverything() {
        delete [SELECT Id FROM MinuteX_Site_Settings__c];
        Reply r = post('{"action":"health"}');
        System.assertEquals(403, r.status);
        System.assertEquals('receiver_disabled', r.error().get('code'));
    }

    @IsTest
    static void malformedJsonIs400() {
        Reply r = post('{"action": "health"');
        System.assertEquals(400, r.status);
        System.assertEquals('malformed_json', r.error().get('code'));
    }

    @IsTest
    static void nonObjectBodyIs400() {
        Reply r = post('[1,2,3]');
        System.assertEquals(400, r.status);
        System.assertEquals('malformed_request', r.error().get('code'));
    }

    @IsTest
    static void unknownActionIs400() {
        Reply r = post('{"action":"delete_everything","request_id":"r1"}');
        System.assertEquals(400, r.status);
        System.assertEquals('unsupported_action', r.error().get('code'));
    }

    @IsTest
    static void unsupportedContractVersionIs422() {
        Reply r = post('{"action":"health","contract_version":"2.0"}');
        System.assertEquals(422, r.status);
        System.assertEquals('unsupported_contract_version', r.error().get('code'));
    }

    @IsTest
    static void missingRequestIdIs400() {
        Reply r = post('{"action":"people_sync","payload":{}}');
        System.assertEquals(400, r.status);
        System.assertEquals('request_id', r.error().get('field'));
    }

    // ---- HMAC -----------------------------------------------------------

    static void useHmac(String secret) {
        MinuteX_Site_Settings__c s = MinuteX_Site_Settings__c.getOrgDefaults();
        s.Auth_Mode__c = 'hmac_sha256';
        s.Shared_Secret__c = secret;
        update s;
    }

    @IsTest
    static void hmacValidSignatureIsAccepted() {
        useHmac('s3cret');
        String raw = '{"action":"health"}';
        String ts = String.valueOf(Datetime.now().getTime() / 1000);
        Reply r = post(raw, new Map<String, String>{
            'X-MinuteX-Timestamp' => ts,
            'x-minutex-signature' => 'v1=' + MinuteXSiteAuth.sign('s3cret', ts, raw)
        });
        System.assertEquals(200, r.status);
        System.assertEquals('hmac_sha256', r.result().get('auth_mode'));
    }

    @IsTest
    static void hmacWrongSignatureIs401() {
        useHmac('s3cret');
        String raw = '{"action":"health"}';
        String ts = String.valueOf(Datetime.now().getTime() / 1000);
        Reply r = post(raw, new Map<String, String>{
            'X-MinuteX-Timestamp' => ts,
            'X-MinuteX-Signature' => 'v1=' + MinuteXSiteAuth.sign('other', ts, raw)
        });
        System.assertEquals(401, r.status);
        System.assertEquals('auth_failed', r.error().get('code'));
    }

    @IsTest
    static void hmacMissingOrStaleSignatureIs401() {
        useHmac('s3cret');
        String raw = '{"action":"health"}';
        System.assertEquals(401, post(raw).status);
        String old = String.valueOf(Datetime.now().getTime() / 1000 - 3600);
        Reply r = post(raw, new Map<String, String>{
            'X-MinuteX-Timestamp' => old,
            'X-MinuteX-Signature' => 'v1=' + MinuteXSiteAuth.sign('s3cret', old, raw)
        });
        System.assertEquals(401, r.status);
    }

    @IsTest
    static void unknownAuthModeFailsClosed() {
        MinuteX_Site_Settings__c s = MinuteX_Site_Settings__c.getOrgDefaults();
        s.Auth_Mode__c = 'bogus';
        update s;
        Reply r = post('{"action":"health"}');
        System.assertEquals(403, r.status);
        System.assertEquals('receiver_misconfigured', r.error().get('code'));
    }

    @IsTest
    static void constantTimeEqualsBehaves() {
        System.assert(MinuteXSiteAuth.constantTimeEquals('abc', 'abc'));
        System.assert(!MinuteXSiteAuth.constantTimeEquals('abc', 'abd'));
        System.assert(!MinuteXSiteAuth.constantTimeEquals('abc', 'ab'));
        System.assert(!MinuteXSiteAuth.constantTimeEquals(null, 'ab'));
    }

    // ---- upsert_meeting --------------------------------------------------

    @IsTest
    static void firstDeliveryCreatesLaterDeliveryUpdates() {
        Reply first = post(JSON.serialize(meetingEnvelope('mxm_1', 'k1', '2026-09-01T12:00:00Z', 'hello')));
        System.assertEquals(200, first.status, JSON.serialize(first.body));
        System.assertEquals('created', first.result().get('operation'));
        System.assertEquals(false, first.body.get('duplicate'));
        String url = (String) first.result().get('record_url');
        System.assert(url.startsWith('https://') && url.endsWith(first.result().get('record_id') + '/view'), url);

        Map<String, Object> env = meetingEnvelope('mxm_1', 'k2', '2026-09-01T13:00:00Z', 'hello again');
        ((Map<String, Object>) ((Map<String, Object>) env.get('payload')).get('meeting')).put('title', 'Renamed');
        Reply second = post(JSON.serialize(env));
        System.assertEquals(200, second.status);
        System.assertEquals('updated', second.result().get('operation'));
        System.assertEquals(first.result().get('record_id'), second.result().get('record_id'));

        List<MinuteX_Site_Meeting__c> rows = meetings('mxm_1');
        System.assertEquals(1, rows.size());
        System.assertEquals('Renamed', rows[0].Title__c);
        System.assertEquals('hello again', rows[0].Transcript__c);
        System.assertEquals(2, rows[0].Delivery_Count__c);
        System.assertEquals(1, rows[0].Decision_Count__c);
        System.assertEquals(1, rows[0].Participant_Count__c);
        System.assertEquals(1, rows[0].Action_Item_Count__c);
        System.assertEquals('ABC Realty', rows[0].Workspace_Name__c);
        System.assertEquals(Datetime.newInstanceGmt(2026, 9, 1, 10, 0, 0), rows[0].Started_At__c);
    }

    @IsTest
    static void meetingRelationshipIsStoredAndClearedWithTheMeeting() {
        Account a = new Account(Name = 'Acme Mumbai');
        insert a;
        Contact c = new Contact(LastName = 'Rao', FirstName = 'Asha', AccountId = a.Id);
        insert c;
        Map<String, Object> env = meetingEnvelope('mxm_rel', 'rk1', '2026-09-01T12:00:00Z', 'hello');
        env.put('contract_version', '1.2');
        ((Map<String, Object>) env.get('payload')).put('relation', new Map<String, Object>{
            'who' => new Map<String, Object>{ 'object_type' => 'Contact', 'record_id' => c.Id, 'name' => 'Asha Rao' },
            'what' => new Map<String, Object>{ 'object_type' => 'Account', 'record_id' => a.Id, 'name' => 'Acme Mumbai' }
        });
        Reply r = post(JSON.serialize(env));
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        MinuteX_Site_Meeting__c m = [SELECT Who_Object_Type__c, Who_Record_Id__c, Who_Record_Name__c,
                                            What_Object_Type__c, What_Record_Id__c, Related_Person__c, Related_To__c
                                     FROM MinuteX_Site_Meeting__c WHERE Meeting_Id__c = 'mxm_rel'];
        System.assertEquals('Contact', m.Who_Object_Type__c);
        System.assertEquals(String.valueOf(c.Id), m.Who_Record_Id__c);
        System.assertEquals('Asha Rao', m.Who_Record_Name__c);
        System.assertEquals('Account', m.What_Object_Type__c);
        System.assert(m.Related_Person__c.contains(String.valueOf(c.Id)), m.Related_Person__c);

        // A later delivery without a What clears it.
        Map<String, Object> env2 = meetingEnvelope('mxm_rel', 'rk2', '2026-09-01T13:00:00Z', 'hello');
        ((Map<String, Object>) env2.get('payload')).put('relation', new Map<String, Object>{
            'who' => new Map<String, Object>{ 'object_type' => 'Contact', 'record_id' => c.Id, 'name' => 'Asha Rao' },
            'what' => null
        });
        System.assertEquals(200, post(JSON.serialize(env2)).status);
        m = [SELECT What_Record_Id__c, Who_Record_Id__c FROM MinuteX_Site_Meeting__c WHERE Meeting_Id__c = 'mxm_rel'];
        System.assertEquals(null, m.What_Record_Id__c);
        System.assertEquals(String.valueOf(c.Id), m.Who_Record_Id__c);
    }

    @IsTest
    static void meetingRelationshipIdMustMatchItsObject() {
        Account a = new Account(Name = 'Acme Mumbai');
        insert a;
        Map<String, Object> env = meetingEnvelope('mxm_bad_rel', 'rk3', '2026-09-01T12:00:00Z', 'hello');
        ((Map<String, Object>) env.get('payload')).put('relation', new Map<String, Object>{
            'who' => new Map<String, Object>{ 'object_type' => 'Contact', 'record_id' => a.Id, 'name' => 'x' }
        });
        Reply r = post(JSON.serialize(env));
        System.assertEquals(422, r.status);
        System.assertEquals('invalid_record', r.error().get('code'));
        System.assertEquals(0, meetings('mxm_bad_rel').size());
    }

    @IsTest
    static void duplicateIdempotencyKeyIsNotProcessedTwice() {
        String raw = JSON.serialize(meetingEnvelope('mxm_dup', 'same-key', '2026-09-01T12:00:00Z', 'x'));
        Reply first = post(raw);
        Reply again = post(raw);
        System.assertEquals(200, again.status);
        System.assertEquals(true, again.body.get('duplicate'));
        System.assertEquals('created', again.result().get('operation'));
        System.assertEquals(first.result().get('record_id'), again.result().get('record_id'));
        System.assertEquals(1, meetings('mxm_dup')[0].Delivery_Count__c);
        System.assertEquals(1, [SELECT COUNT() FROM MinuteX_Site_Request__c WHERE Idempotency_Key__c = 'same-key']);
    }

    @IsTest
    static void olderPayloadDoesNotOverwriteNewer() {
        post(JSON.serialize(meetingEnvelope('mxm_s', 'new', '2026-09-01T13:00:00Z', 'newer')));
        Reply late = post(JSON.serialize(meetingEnvelope('mxm_s', 'old', '2026-09-01T12:00:00Z', 'older')));
        System.assertEquals(200, late.status);
        System.assertEquals('stale_ignored', late.result().get('operation'));
        System.assertEquals('newer', meetings('mxm_s')[0].Transcript__c);
    }

    @IsTest
    static void transcriptAtLimitIsStoredWhole() {
        String text = 'a'.repeat(MinuteXSiteMeetings.MAX_TRANSCRIPT_CHARS);
        Reply r = post(JSON.serialize(meetingEnvelope('mxm_big', 'kb', '2026-09-01T12:00:00Z', text)));
        System.assertEquals(200, r.status, JSON.serialize(r.error()));
        MinuteX_Site_Meeting__c row = meetings('mxm_big')[0];
        System.assertEquals(131072, row.Transcript_Chars__c);
        System.assertEquals(131072, row.Transcript__c.length());
    }

    @IsTest
    static void transcriptOverLimitIsRefusedNotTruncated() {
        String text = 'a'.repeat(MinuteXSiteMeetings.MAX_TRANSCRIPT_CHARS + 1);
        Reply r = post(JSON.serialize(meetingEnvelope('mxm_huge', 'kh', '2026-09-01T12:00:00Z', text)));
        System.assertEquals(422, r.status);
        System.assertEquals('transcript_too_large', r.error().get('code'));
        System.assertEquals('payload.transcript.text', r.error().get('field'));
        System.assertEquals(0, meetings('mxm_huge').size());
        System.assertEquals(0, [SELECT COUNT() FROM MinuteX_Site_Request__c]);
    }

    @IsTest
    static void audioUrlIsStoredAndMustBeHttps() {
        post(JSON.serialize(meetingEnvelope('mxm_a', 'ka', '2026-09-01T12:00:00Z', 'x')));
        MinuteX_Site_Meeting__c row = meetings('mxm_a')[0];
        System.assertEquals('https://api.example.com/delivery/audio/tok', row.Audio_URL__c);
        System.assertEquals(85000000, row.Audio_Size_Bytes__c);

        Map<String, Object> env = meetingEnvelope('mxm_a2', 'ka2', '2026-09-01T12:00:00Z', 'x');
        ((Map<String, Object>) ((Map<String, Object>) env.get('payload')).get('audio')).put('url', 'http://insecure');
        Reply r = post(JSON.serialize(env));
        System.assertEquals(422, r.status);
        System.assertEquals('invalid_audio_url', r.error().get('code'));
    }

    @IsTest
    static void invalidMeetingIsRejectedWithoutWrites() {
        Map<String, Object> env = meetingEnvelope('bad id with spaces', 'kx', '2026-09-01T12:00:00Z', 'x');
        Reply r = post(JSON.serialize(env));
        System.assertEquals(422, r.status);
        System.assertEquals('payload.meeting.id', r.error().get('field'));

        env = meetingEnvelope('mxm_dt', 'kdt', '2026-09-01T12:00:00Z', 'x');
        ((Map<String, Object>) ((Map<String, Object>) env.get('payload')).get('meeting')).put('started_at', 'yesterday');
        r = post(JSON.serialize(env));
        System.assertEquals(422, r.status);
        System.assertEquals('payload.meeting.started_at', r.error().get('field'));

        env = meetingEnvelope('mxm_neg', 'kneg', '2026-09-01T12:00:00Z', 'x');
        ((Map<String, Object>) ((Map<String, Object>) env.get('payload')).get('meeting')).put('duration_seconds', -5);
        r = post(JSON.serialize(env));
        System.assertEquals(422, r.status);
        System.assertEquals(0, [SELECT COUNT() FROM MinuteX_Site_Meeting__c]);
    }

    @IsTest
    static void malformedMeetingPayloadIs400() {
        Reply r = post('{"action":"upsert_meeting","request_id":"r1","idempotency_key":"k","payload":{"meeting":"nope"}}');
        System.assertEquals(400, r.status);
        System.assertEquals('malformed_request', r.error().get('code'));

        r = post('{"action":"upsert_meeting","request_id":"r1","payload":{"meeting":{"id":"m1"}}}');
        System.assertEquals(400, r.status);
        System.assertEquals('idempotency_key', r.error().get('field'));

        r = post('{"action":"upsert_meeting","request_id":"r1","idempotency_key":"k","payload":{"meeting":{"id":"m1"},"participants":{}}}');
        System.assertEquals(400, r.status);
        System.assertEquals('payload.participants', r.error().get('field'));
    }

    // ---- people_sync -----------------------------------------------------

    @IsTest
    static void peopleSyncWalksContactsThenUsers() {
        Account acct = new Account(Name = 'Acme');
        insert acct;
        List<Contact> contacts = new List<Contact>();
        for (Integer i = 0; i < 3; i++) {
            contacts.add(new Contact(LastName = 'Person' + i, FirstName = 'Test',
                Email = 'p' + i + '@example.com', AccountId = acct.Id));
        }
        insert contacts;

        Set<String> contactIds = new Set<String>();
        Set<String> seen = new Set<String>();
        Integer userRows = 0;
        String cursor = '';
        for (Integer page = 0; page < 20; page++) {
            Map<String, Object> env = new Map<String, Object>{
                'action' => 'people_sync', 'request_id' => 'ps' + page,
                'payload' => new Map<String, Object>{ 'people' => new List<Object>(), 'cursor' => cursor, 'page_size' => 2 }
            };
            Reply r = post(JSON.serialize(env));
            System.assertEquals(200, r.status, JSON.serialize(r.body));
            for (Object o : (List<Object>) r.result().get('people')) {
                Map<String, Object> p = (Map<String, Object>) o;
                System.assertEquals('salesforce', p.get('provider'));
                System.assert(seen.add((String) p.get('external_id')), 'a person was returned twice');
                if (p.get('type') == 'contact') {
                    contactIds.add((String) p.get('external_id'));
                    System.assertEquals('Acme', p.get('company'));
                } else {
                    userRows++;
                }
            }
            if (r.result().get('done') == true) {
                break;
            }
            cursor = (String) r.result().get('next_cursor');
        }
        System.assertEquals(3, contactIds.size());
        System.assert(userRows >= 1, 'the running user should be returned as a Standard user');
    }

    @IsTest
    static void peopleSyncRejectsForeignCursor() {
        Reply r = post('{"action":"people_sync","request_id":"r1","payload":{"cursor":"account:001"}}');
        System.assertEquals(400, r.status);
        System.assertEquals('invalid_cursor', r.error().get('code'));
    }
}
