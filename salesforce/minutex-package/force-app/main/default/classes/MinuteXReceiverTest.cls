/**
 * Tests for MinuteXReceiver: the signature, configuration (meeting types),
 * record search, people and routing. MinuteXMeetingSyncTest covers what an
 * event writes. Salesforce needs these to move the receiver to production.
 */
@IsTest
private class MinuteXReceiverTest {
    static final String SECRET = 'mxwhsec_' + 'a'.repeat(43);
    static final Long NOW = 1790000000L;

    static void setUp() {
        MinuteXReceiver.settingsOverride = new MinuteX_Webhook_Settings__c(
            Enabled__c = true, Shared_Secret__c = SECRET);
        MinuteXReceiver.nowOverride = NOW;
        // A search type on Account, which exists in every org.
        MinuteXReceiver.typesOverride = new List<MinuteXReceiver.MeetingType>{
            MinuteXReceiver.plain('general_meeting', 'General Meeting'),
            MinuteXReceiver.withRecord(MinuteXReceiver.plain('account_call', 'Account Call'), true,
                'Account', 'Search accounts…', 'Account', 'Name', '{Name}', '{Phone}')
        };
    }

    static RestResponse post(String route, Map<String, Object> body, String secret) {
        String raw = JSON.serialize(body);
        RestRequest req = new RestRequest();
        req.requestURI = '/minutex/v2/' + route;
        req.httpMethod = 'POST';
        req.requestBody = Blob.valueOf(raw);
        req.addHeader('X-MinuteX-Timestamp', String.valueOf(NOW));
        req.addHeader('X-MinuteX-Signature', 'v1=' + EncodingUtil.convertToHex(Crypto.generateMac(
            'hmacSHA256', Blob.valueOf(String.valueOf(NOW) + '.' + raw), Blob.valueOf(secret))));
        RestContext.request = req;
        RestContext.response = new RestResponse();
        MinuteXReceiver.handlePost();
        return RestContext.response;
    }

    static Map<String, Object> bodyOf(RestResponse res) {
        return (Map<String, Object>) JSON.deserializeUntyped(res.responseBody.toString());
    }

    static Map<String, Object> envelope(Integer seq) {
        return new Map<String, Object>{
            'type' => 'event', 'contract_version' => '2.0', 'event' => 'meeting.created',
            'event_id' => 'evt_' + String.valueOf(seq).leftPad(32, '0'),
            'meeting_id' => 'mxm_' + '9'.repeat(32), 'sequence' => seq,
            'data' => new Map<String, Object>{'meeting' => new Map<String, Object>{'title' => 'Site walk'}}
        };
    }

    static Map<String, Object> peopleSearchBody(String query, List<Object> types) {
        return new Map<String, Object>{'type' => 'people.search', 'contract_version' => '2.0',
            'query' => query, 'types' => types, 'limit' => 20, 'user_email' => 'amit@example.com'};
    }

    @IsTest
    static void peopleSearchFindsContactsAndLeads() {
        setUp();
        Contact c = new Contact(LastName = 'Sharma', FirstName = 'Rahul', Email = 'rahul@client.com');
        Lead l = new Lead(LastName = 'Mehta', FirstName = 'Rahul', Company = 'Web enquiry');
        insert new List<SObject>{c, l};
        RestResponse res = post('people/search', peopleSearchBody('rah', null), SECRET);
        System.assertEquals(200, res.statusCode);
        Set<String> kinds = new Set<String>();
        for (Object p : (List<Object>) bodyOf(res).get('people')) {
            kinds.add((String) ((Map<String, Object>) p).get('type'));
        }
        System.assert(kinds.contains('contact'), kinds);
        System.assert(kinds.contains('lead'), kinds);
    }

    @IsTest
    static void peopleSearchHonoursTypesAndMinimumLength() {
        setUp();
        Contact c = new Contact(LastName = 'Sharma', FirstName = 'Rahul');
        insert c;
        RestResponse onlyLeads = post('people/search', peopleSearchBody('rah', new List<Object>{'lead'}), SECRET);
        System.assertEquals(0, ((List<Object>) bodyOf(onlyLeads).get('people')).size());
        RestResponse tooShort = post('people/search', peopleSearchBody('r', null), SECRET);
        System.assertEquals(400, tooShort.statusCode);
        System.assertEquals('query_too_short',
            ((Map<String, Object>) bodyOf(tooShort).get('error')).get('code'));
    }

    static Map<String, Object> relatedBody(List<Object> recordIds) {
        return new Map<String, Object>{'type' => 'people.related', 'contract_version' => '2.0',
            'record_ids' => recordIds, 'limit' => 50, 'user_email' => 'amit@example.com'};
    }

    static Map<String, String> relationsOf(RestResponse res) {
        Map<String, String> out = new Map<String, String>();
        for (Object p : (List<Object>) bodyOf(res).get('people')) {
            Map<String, Object> m = (Map<String, Object>) p;
            out.put((String) m.get('name'), (String) m.get('relation'));
        }
        return out;
    }

    @IsTest
    static void relatedPeopleComeFromAccountsOpportunitiesAndLeads() {
        setUp();
        Account a = new Account(Name = 'Sharma Family');
        insert a;
        Contact priya = new Contact(LastName = 'Nair', FirstName = 'Priya', AccountId = a.Id);
        Contact ravi = new Contact(LastName = 'Rao', FirstName = 'Ravi');
        insert new List<Contact>{priya, ravi};
        Opportunity o = new Opportunity(Name = 'Mumbai Heights', StageName = 'Prospecting',
            CloseDate = Date.today().addDays(30), AccountId = a.Id);
        insert o;
        insert new OpportunityContactRole(OpportunityId = o.Id, ContactId = ravi.Id);
        Lead l = new Lead(LastName = 'Mehta', FirstName = 'Rahul', Company = 'Web enquiry');
        insert l;

        RestResponse res = post('people/related', relatedBody(new List<Object>{o.Id, l.Id, 'not-an-id'}), SECRET);
        System.assertEquals(200, res.statusCode);
        Map<String, String> got = relationsOf(res);
        System.assertEquals('Contact at Sharma Family', got.get('Priya Nair'));
        System.assertEquals('Opportunity contact role', got.get('Ravi Rao'));
        System.assertEquals('Linked Lead', got.get('Rahul Mehta'));
    }

    @IsTest
    static void relatedPeopleFollowLookupFieldsOnOtherObjects() {
        setUp();
        Contact c = new Contact(LastName = 'Sharma', FirstName = 'Rahul');
        insert c;
        Task t = new Task(Subject = 'Visit', WhoId = c.Id);
        insert t;
        Map<String, String> got = relationsOf(
            post('people/related', relatedBody(new List<Object>{t.Id}), SECRET));
        System.assertEquals('Linked Contact', got.get('Rahul Sharma'));
    }

    @IsTest
    static void configurationListsTheLinkableObjectsThatExist() {
        setUp();
        MinuteXReceiver.linkableOverride = new List<MinuteXReceiver.LinkableObject>{
            MinuteXReceiver.linkable('Account', true), MinuteXReceiver.linkable('No_Such_Object__c', true)};
        Map<String, Object> body = bodyOf(post('configuration',
            new Map<String, Object>{'type' => 'configuration.request'}, SECRET));
        List<Object> objs = (List<Object>) body.get('linkable_objects');
        System.assertEquals(1, objs.size());
        System.assertEquals('Account', ((Map<String, Object>) objs[0]).get('object'));
    }

    @IsTest
    static void linkSearchCoversOnlyLinkableObjects() {
        setUp();
        MinuteXReceiver.linkableOverride = new List<MinuteXReceiver.LinkableObject>{
            MinuteXReceiver.linkable('Account', true)};
        Account a = new Account(Name = 'Sharma Family');
        Contact c = new Contact(LastName = 'Sharma');
        insert a;
        insert c;
        RestResponse res = post('records/search', new Map<String, Object>{
            'type' => 'records.search', 'query' => 'sharma',
            'objects' => new List<Object>{'Account', 'Contact'}, 'limit' => 10}, SECRET);
        System.assertEquals(200, res.statusCode);
        List<Object> recs = (List<Object>) bodyOf(res).get('records');
        System.assertEquals(1, recs.size(), 'Contact is not linkable here');
        System.assertEquals('Account', ((Map<String, Object>) recs[0]).get('object_type'));
        System.assertEquals('Sharma Family', ((Map<String, Object>) recs[0]).get('display_name'));
    }

    @IsTest
    static void aLinkedEventKeepsTheMainRecord() {
        setUp();
        Map<String, Object> env = envelope(1);
        env.put('contract_version', '3.0');
        env.put('links', new List<Object>{
            new Map<String, Object>{'object' => 'Lead', 'id' => '00Q000000000001AAA', 'name' => 'Rahul', 'primary' => false},
            new Map<String, Object>{'object' => 'Account', 'id' => '001000000000001AAA', 'name' => 'Sharma Family', 'primary' => true}});
        System.assertEquals(200, post('events', env, SECRET).statusCode);
        MinuteX_Meeting__c m = [SELECT Record_Id__c, Record_Name__c FROM MinuteX_Meeting__c];
        System.assertEquals('001000000000001AAA', m.Record_Id__c);
        System.assertEquals('Sharma Family', m.Record_Name__c);
    }

    @IsTest
    static void likeTermsAreEscaped() {
        System.assertEquals('50\\%\\_off', MinuteXReceiver.likeEscape('50%_off'));
    }

    @IsTest
    static void searchFindsByEmailToo() {
        setUp();
        insert new Lead(LastName = 'Malhotra', FirstName = 'Karan', Company = 'Traders',
                        Email = 'karan.m@example.com');
        RestResponse res = post('people/search', peopleSearchBody('karan.m@', null), SECRET);
        System.assertEquals(1, ((List<Object>) bodyOf(res).get('people')).size());
    }

    @IsTest
    static void searchTermsAreEscaped() {
        System.assertEquals('o\\\'brien', MinuteXReceiver.escapeSosl('o\'brien'));
        System.assertEquals('a\\-b\\*', MinuteXReceiver.escapeSosl('a-b*'));
    }

    static Map<String, Object> secretBody(String secret) {
        return new Map<String, Object>{'type' => 'setup.secret', 'contract_version' => '3.0', 'secret' => secret};
    }

    /** A user holding the MinuteX Integration custom permission. */
    static User integrationUser() {
        Profile p = [SELECT Id FROM Profile WHERE Name = 'Standard User' LIMIT 1];
        User u = new User(Alias = 'mxint', Email = 'int@minutex.test', EmailEncodingKey = 'UTF-8',
            LastName = 'Integration', LanguageLocaleKey = 'en_US', LocaleSidKey = 'en_US', ProfileId = p.Id,
            TimeZoneSidKey = 'Asia/Kolkata', Username = 'int' + Datetime.now().getTime() + '@minutex.test');
        System.runAs(new User(Id = UserInfo.getUserId())) {
            insert u;
            PermissionSet ps = new PermissionSet(Name = 'MX_Test_Int', Label = 'MX Test Int');
            insert ps;
            insert new SetupEntityAccess(ParentId = ps.Id, SetupEntityId =
                [SELECT Id FROM CustomPermission WHERE DeveloperName = 'MinuteX_Integration'].Id);
            insert new PermissionSetAssignment(AssigneeId = u.Id, PermissionSetId = ps.Id);
        }
        return u;
    }

    @IsTest
    static void onlyTheIntegrationUserCanSetTheSecret() {
        // The running test user has no MinuteX Integration permission.
        RestResponse res = post('setup/secret', secretBody(SECRET), 'anything');
        System.assertEquals(403, res.statusCode);
        System.assertEquals(null, MinuteX_Webhook_Settings__c.getOrgDefaults().Shared_Secret__c);
    }

    @IsTest
    static void theIntegrationUserSetsAndRotatesTheSecret() {
        String second = 'mxwhsec_' + 'b'.repeat(43);
        System.runAs(integrationUser()) {
            System.assertEquals(200, post('setup/secret', secretBody(SECRET), 'unsigned').statusCode);
            System.assertEquals(400, post('setup/secret', secretBody('short'), 'unsigned').statusCode);
            System.assertEquals(200, post('setup/secret', secretBody(second), 'unsigned').statusCode);
        }
        MinuteX_Webhook_Settings__c s = MinuteX_Webhook_Settings__c.getOrgDefaults();
        System.assertEquals(second, s.Shared_Secret__c);
        System.assertEquals(SECRET, s.Previous_Secret__c, 'the old secret keeps working');
        System.assertEquals(true, s.Enabled__c);
    }

    @IsTest
    static void aSignedEventIsSaved() {
        setUp();
        RestResponse res = post('events', envelope(1), SECRET);
        System.assertEquals(200, res.statusCode);
        System.assertEquals('Site walk', [SELECT Name FROM MinuteX_Meeting__c].Name);
        System.assertNotEquals(null, ((Map<String, Object>) bodyOf(res).get('result')).get('record_id'));
    }

    @IsTest
    static void aWrongSecretOrDisabledReceiverIsRefused() {
        setUp();
        System.assertEquals(401, post('events', envelope(1), 'mxwhsec_' + 'b'.repeat(43)).statusCode);
        System.assertEquals(0, [SELECT COUNT() FROM MinuteX_Meeting__c]);
        MinuteXReceiver.settingsOverride.Enabled__c = false;
        RestResponse off = post('events', envelope(1), SECRET);
        System.assertEquals(403, off.statusCode);
        System.assertEquals('receiver_disabled',
            ((Map<String, Object>) bodyOf(off).get('error')).get('code'));
    }

    @IsTest
    static void thePreviousSecretWorksDuringARotation() {
        setUp();
        MinuteXReceiver.settingsOverride.Previous_Secret__c = 'mxwhsec_' + 'c'.repeat(43);
        System.assertEquals(200, post('events', envelope(1), 'mxwhsec_' + 'c'.repeat(43)).statusCode);
    }

    @IsTest
    static void anOldRequestIsRefused() {
        setUp();
        MinuteXReceiver.nowOverride = NOW + 301;
        System.assertEquals(401, post('events', envelope(1), SECRET).statusCode);
    }

    @IsTest
    static void configurationListsTheMeetingTypesAndTheirRecordRules() {
        setUp();
        Map<String, Object> body = bodyOf(post('configuration', new Map<String, Object>{
            'type' => 'configuration.request', 'contract_version' => '2.0'}, SECRET));
        List<Object> types = (List<Object>) body.get('meeting_types');
        System.assertEquals(2, types.size());
        Map<String, Object> sel = (Map<String, Object>) ((Map<String, Object>) types[1]).get('record_selection');
        System.assertEquals(true, sel.get('enabled'));
        System.assertEquals(true, sel.get('required'));
        System.assertEquals('Account', sel.get('label'));
        System.assert(String.isNotBlank((String) body.get('configuration_version')));
    }

    @IsTest
    static void theShippedMeetingTypesAreValid() {
        MinuteXReceiver.typesOverride = null;
        Set<String> ids = new Set<String>();
        for (MinuteXReceiver.MeetingType t : MinuteXReceiver.meetingTypes()) {
            System.assert(Pattern.matches('[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}', t.typeId), t.typeId);
            System.assert(ids.add(t.typeId), 'duplicate id ' + t.typeId);
        }
    }

    @IsTest
    static void recordSearchReturnsDisplayTextOnly() {
        setUp();
        insert new List<Account>{new Account(Name = 'Mumbai Heights Pvt', Phone = '022 555'),
                                 new Account(Name = 'Pune Greens')};
        Map<String, Object> req = new Map<String, Object>{
            'type' => 'records.search', 'contract_version' => '2.0',
            'meeting_type_id' => 'account_call', 'query' => 'Mumbai', 'limit' => 10};
        List<Object> records = (List<Object>) bodyOf(post('records/search', req, SECRET)).get('records');
        System.assertEquals(1, records.size());
        Map<String, Object> r = (Map<String, Object>) records[0];
        System.assertEquals('Mumbai Heights Pvt', r.get('display_name'));
        System.assertEquals('022 555', r.get('secondary_text'));

        req.put('query', '\' OR Name != \'');  // an injection attempt finds nothing
        System.assertEquals(0, ((List<Object>) bodyOf(post('records/search', req, SECRET)).get('records')).size());
        req.put('meeting_type_id', 'general_meeting');
        System.assertEquals(422, post('records/search', req, SECRET).statusCode);
    }

    @IsTest
    static void aMisconfiguredSearchIsReported() {
        setUp();
        MinuteXReceiver.typesOverride.add(MinuteXReceiver.withRecord(
            MinuteXReceiver.plain('broken', 'Broken'), false, 'X', '', 'No_Such_Object__c', 'Name', '{Name}', ''));
        RestResponse res = post('records/search', new Map<String, Object>{
            'type' => 'records.search', 'contract_version' => '2.0',
            'meeting_type_id' => 'broken', 'query' => 'x'}, SECRET);
        System.assertEquals(500, res.statusCode);
    }

    @IsTest
    static void peopleSendsContactsThenUsers() {
        setUp();
        insert new Contact(LastName = 'Smith', Email = 'john@example.com');
        Map<String, Object> page = bodyOf(post('people', new Map<String, Object>{
            'type' => 'people.sync', 'contract_version' => '2.0', 'cursor' => '', 'page_size' => 500}, SECRET));
        System.assertEquals('contact', ((Map<String, Object>) ((List<Object>) page.get('people'))[0]).get('type'));
        System.assertEquals('user:', page.get('next_cursor'));
        page = bodyOf(post('people', new Map<String, Object>{
            'type' => 'people.sync', 'contract_version' => '2.0', 'cursor' => 'user:', 'page_size' => 500}, SECRET));
        System.assertNotEquals(null, page.get('done'));
    }

    @IsTest
    static void wrongEndpointOrBadBodyIsRefused() {
        setUp();
        System.assertEquals(404, post('events', new Map<String, Object>{'type' => 'people.sync'}, SECRET).statusCode);
        System.assertEquals(400, post('events', new Map<String, Object>{'type' => 'event', 'event' => 'x'}, SECRET).statusCode);
        System.assertEquals('records/search', MinuteXReceiver.routeOf('/services/apexrest/DevX/minutex/v2/records/search/'));
        System.assert(MinuteXReceiver.sameText('abc', 'abc'));
        System.assert(!MinuteXReceiver.sameText('abc', 'abd'));
        System.assertNotEquals(null, MinuteXReceiver.namespacePrefix());
        System.assertEquals('Mumbai', MinuteXReceiver.render('{Name} —', new Account(Name = 'Mumbai'),
            new Map<String, String>{'Name' => 'Name'}));
    }
}
