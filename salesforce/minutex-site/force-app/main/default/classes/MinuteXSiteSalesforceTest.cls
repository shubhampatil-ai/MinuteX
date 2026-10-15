/**
 * Tests for the org actions (org_info, object_schema, record_search,
 * create_task, update_task) through the single Site boundary, working within
 * the CONFIGURED INTEGRATION USER's permissions (MinuteXSitePermissions).
 *
 * The permission data is real: the admin integration user is this org's
 * System Administrator (Modify All Data), and the limited one is a real user
 * on the "Minimum Access - Salesforce" profile, evaluated from Salesforce's
 * own PermissionSetAssignment / ObjectPermissions / FieldPermissions.
 */
@IsTest
private class MinuteXSiteSalesforceTest {

    @TestSetup
    static void setup() {
        insert new MinuteX_Site_Settings__c(
            SetupOwnerId = UserInfo.getOrganizationId(), Enabled__c = true, Auth_Mode__c = 'none',
            Integration_Username__c = UserInfo.getUserName());
        Account mumbai = new Account(Name = 'Acme Mumbai');
        insert new List<Account>{ mumbai, new Account(Name = 'Beta Pune'), new Account(Name = '100% Real') };
        insert new Contact(LastName = 'Rao', FirstName = 'Asha', Email = 'asha@acme.test', AccountId = mumbai.Id);
        insert new MinuteX_Site_Meeting__c(Name = 'Site visit SV-1', Meeting_Id__c = 'mxm_test_1');
        System.runAs(new User(Id = UserInfo.getUserId())) {
            Profile minimum = [SELECT Id FROM Profile WHERE Name = 'Minimum Access - Salesforce' LIMIT 1];
            Profile standard = [SELECT Id FROM Profile WHERE Name = 'Standard User' LIMIT 1];
            String suffix = String.valueOf(Crypto.getRandomInteger()).replace('-', '') + '@minutex-test.example';
            insert new List<User>{
                newUser('mxlimited' + suffix, minimum.Id, true),
                newUser('mxinactive' + suffix, standard.Id, false)
            };
        }
    }

    static User newUser(String username, Id profileId, Boolean active) {
        return new User(Username = username, LastName = username.left(20), Email = 'u@minutex-test.example',
            Alias = username.left(8), ProfileId = profileId, IsActive = active,
            TimeZoneSidKey = 'Asia/Kolkata', LocaleSidKey = 'en_US', EmailEncodingKey = 'UTF-8',
            LanguageLocaleKey = 'en_US');
    }

    static User limitedUser() {
        return [SELECT Id, Username FROM User WHERE Username LIKE 'mxlimited%@minutex-test.example' LIMIT 1];
    }

    static User inactiveUser() {
        return [SELECT Id, Username FROM User WHERE Username LIKE 'mxinactive%@minutex-test.example' LIMIT 1];
    }

    static void integrationUser(String username) {
        MinuteX_Site_Settings__c s = MinuteX_Site_Settings__c.getOrgDefaults();
        s.Integration_Username__c = username;
        update s;
        MinuteXSitePermissions.reset();
    }

    static Account account(String name) {
        return [SELECT Id FROM Account WHERE Name = :name LIMIT 1];
    }

    // ---- harness ---------------------------------------------------------

    class Reply {
        Integer status;
        Map<String, Object> body;
        Map<String, Object> result() { return (Map<String, Object>) body.get('result'); }
        Map<String, Object> error() { return (Map<String, Object>) body.get('error'); }
    }

    static Reply call(String action, Map<String, Object> payload) {
        return call(action, payload, null);
    }

    static Reply call(String action, Map<String, Object> payload, String key) {
        Map<String, Object> env = new Map<String, Object>{
            'contract_version' => '1.1', 'action' => action, 'request_id' => 'r' + Crypto.getRandomInteger(),
            'payload' => payload
        };
        if (key != null) {
            env.put('idempotency_key', key);
        }
        RestRequest req = new RestRequest();
        req.requestURI = '/services/apexrest/minutex/v1/';
        req.httpMethod = 'POST';
        req.requestBody = Blob.valueOf(JSON.serialize(env));
        RestContext.request = req;
        RestContext.response = new RestResponse();
        MinuteXSiteReceiver.handlePost();
        Reply r = new Reply();
        r.status = RestContext.response.statusCode;
        r.body = (Map<String, Object>) JSON.deserializeUntyped(RestContext.response.responseBody.toString());
        return r;
    }

    static Map<String, Object> taskPayload(Map<String, Object> task) {
        return new Map<String, Object>{ 'task' => task };
    }

    static Map<String, Object> baseTask(String relType, Id relId) {
        return new Map<String, Object>{
            'subject' => 'Follow-up Call',
            'description' => 'From MinuteX',
            'due_date' => '2026-10-01',
            'status' => 'Not Started',
            'priority' => 'High',
            'assigned_user_id' => UserInfo.getUserId(),
            'related_object_type' => relType,
            'related_record_id' => relId
        };
    }

    static List<Map<String, Object>> allObjects() {
        List<Map<String, Object>> out = new List<Map<String, Object>>();
        String cursor = '';
        for (Integer i = 0; i < 20; i++) {
            Reply r = call('object_schema', new Map<String, Object>{
                'mode' => 'objects', 'cursor' => cursor, 'page_size' => 250 });
            System.assertEquals(200, r.status, JSON.serialize(r.body));
            for (Object o : (List<Object>) r.result().get('objects')) {
                out.add((Map<String, Object>) o);
            }
            if (r.result().get('done') == true) {
                return out;
            }
            cursor = (String) r.result().get('next_cursor');
        }
        System.assert(false, 'object walk did not finish');
        return out;
    }

    static Map<String, Map<String, Object>> byName(List<Map<String, Object>> objects) {
        Map<String, Map<String, Object>> m = new Map<String, Map<String, Object>>();
        for (Map<String, Object> o : objects) {
            m.put((String) o.get('api_name'), o);
        }
        return m;
    }

    static Map<String, Map<String, Object>> fieldsOf(Map<String, Object> obj) {
        Map<String, Map<String, Object>> m = new Map<String, Map<String, Object>>();
        for (Object f : (List<Object>) obj.get('fields')) {
            m.put((String) ((Map<String, Object>) f).get('api_name'), (Map<String, Object>) f);
        }
        return m;
    }

    // ---- boundary, auth and integration user ------------------------------

    @IsTest
    static void everyResponseCarriesTheOrgId() {
        Reply r = call('health', new Map<String, Object>());
        System.assertEquals(UserInfo.getOrganizationId(), r.body.get('org_id'));
        System.assertEquals(true, r.result().get('integration_user_configured'));
        System.assert(((List<Object>) r.result().get('actions')).contains('create_task'));
        System.assert(((List<Object>) r.result().get('actions')).contains('upsert_meeting'));
    }

    @IsTest
    static void pluggableAuthStillAppliesToOrgActions() {
        MinuteX_Site_Settings__c s = MinuteX_Site_Settings__c.getOrgDefaults();
        s.Auth_Mode__c = 'hmac_sha256';
        s.Shared_Secret__c = 'site-secret';
        update s;
        Reply r = call('org_info', new Map<String, Object>());
        System.assertEquals(401, r.status, 'unsigned request refused in hmac mode');
        s.Auth_Mode__c = 'something-new';
        update s;
        System.assertEquals('receiver_misconfigured', call('org_info', new Map<String, Object>()).error().get('code'));
        System.assert(MinuteXSiteAuth.VERIFIERS.containsKey('none'));
        System.assert(MinuteXSiteAuth.VERIFIERS.containsKey('hmac_sha256'));
    }

    @IsTest
    static void orgActionsNeedAConfiguredActiveIntegrationUser() {
        integrationUser(null);
        Reply r = call('org_info', new Map<String, Object>());
        System.assertEquals(409, r.status);
        System.assertEquals('integration_user_not_configured', r.error().get('code'));
        integrationUser(inactiveUser().Username);
        System.assertEquals('integration_user_invalid', call('org_info', new Map<String, Object>()).error().get('code'));
        // Meeting delivery and people sync do not depend on it.
        System.assertEquals(200, call('people_sync', new Map<String, Object>{ 'page_size' => 5 }).status);
    }

    // ---- org_info ----------------------------------------------------------

    @IsTest
    static void orgInfoReturnsTheOrgTheUserAndTaskRelations() {
        Reply r = call('org_info', new Map<String, Object>());
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assertEquals(UserInfo.getOrganizationId(), ((Map<String, Object>) r.result().get('org')).get('org_id'));
        System.assertEquals(UserInfo.getUserId(), ((Map<String, Object>) r.result().get('user')).get('id'));
        Map<String, Object> task = (Map<String, Object>) r.result().get('task');
        System.assertEquals(true, task.get('createable'));
        List<Object> what = (List<Object>) task.get('what_objects');
        System.assert(what.contains('Account'));
        System.assert(what.contains('MinuteX_Site_Meeting__c'), 'activity-enabled custom object');
        System.assert(!what.contains('MinuteX_Site_Request__c'), 'activities are disabled on it');
        System.assert(((List<Object>) task.get('who_objects')).contains('Contact'));
    }

    @IsTest
    static void limitedIntegrationUserCannotCreateTasks() {
        integrationUser(limitedUser().Username);
        Map<String, Object> task = (Map<String, Object>) call('org_info', new Map<String, Object>()).result().get('task');
        System.assertEquals(false, task.get('createable'));
    }

    // ---- object discovery --------------------------------------------------

    @IsTest
    static void objectDiscoveryReturnsStandardAndCustomObjectsWithPermissions() {
        Map<String, Map<String, Object>> objects = byName(allObjects());
        Map<String, Object> acct = objects.get('Account');
        System.assertNotEquals(null, acct);
        System.assertEquals(false, acct.get('is_custom'));
        System.assertEquals(true, acct.get('queryable'));
        System.assertEquals(true, acct.get('createable'));
        System.assertEquals('001', acct.get('key_prefix'));
        System.assertEquals(true, acct.get('mru_enabled'), 'a business record object');
        System.assertEquals(true, objects.get('MinuteX_Site_Meeting__c').get('is_custom'));
        System.assert(!objects.containsKey('AccountShare'));
        System.assert(!objects.containsKey('AccountHistory'));
        System.assert(!objects.containsKey('MinuteX_Site_Settings__c'));
    }

    @IsTest
    static void limitedUserOnlyDiscoversWhatItIsGranted() {
        integrationUser(limitedUser().Username);
        Map<String, Map<String, Object>> objects = byName(allObjects());
        System.assert(!objects.containsKey('Account'), 'Minimum Access cannot read Account');
        System.assert(!objects.containsKey('Lead'));
        System.assertEquals(false, objects.get('Task').get('createable'), 'no Edit Tasks');
        Reply r = call('object_schema', new Map<String, Object>{ 'mode' => 'objects', 'page_size' => 250 });
        System.assert((Integer) r.result().get('skipped_inaccessible') > 0);
    }

    @IsTest
    static void pagingNeverSkipsOrRepeatsObjects() {
        Set<String> seen = new Set<String>();
        String cursor = '';
        for (Integer i = 0; i < 400; i++) {
            Reply r = call('object_schema', new Map<String, Object>{ 'mode' => 'objects', 'cursor' => cursor, 'page_size' => 97 });
            for (Object o : (List<Object>) r.result().get('objects')) {
                System.assert(seen.add((String) ((Map<String, Object>) o).get('api_name')), 'repeated object');
            }
            if (r.result().get('done') == true) {
                break;
            }
            cursor = (String) r.result().get('next_cursor');
        }
        System.assertEquals(byName(allObjects()).keySet(), seen);
    }

    @IsTest
    static void fieldDiscoveryIncludesCustomFieldsPicklistsReferencesAndWritability() {
        Reply r = call('object_schema', new Map<String, Object>{
            'mode' => 'fields', 'objects' => new List<Object>{ 'Account', 'MinuteX_Site_Meeting__c', 'Task', 'Contact', 'Nope__c' } });
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        Map<String, Map<String, Object>> objects = new Map<String, Map<String, Object>>();
        for (Object o : (List<Object>) r.result().get('objects')) {
            objects.put((String) ((Map<String, Object>) o).get('api_name'), (Map<String, Object>) o);
        }
        Map<String, Map<String, Object>> acct = fieldsOf(objects.get('Account'));
        System.assertEquals(true, acct.get('Name').get('name_field'));
        System.assertEquals(true, acct.get('Name').get('required'));
        System.assertEquals(true, acct.get('Name').get('createable'));
        System.assertEquals(false, acct.get('CreatedDate').get('createable'), 'audit field');
        System.assertEquals(false, acct.get('Id').get('updateable'));
        System.assert(((List<Object>) acct.get('OwnerId').get('reference_to')).contains('User'));
        System.assertEquals(false, fieldsOf(objects.get('Contact')).get('Name').get('createable'), 'compound name');
        System.assertEquals(true, fieldsOf(objects.get('MinuteX_Site_Meeting__c')).get('Meeting_Id__c').get('is_custom'));
        Map<String, Map<String, Object>> task = fieldsOf(objects.get('Task'));
        System.assert(!((List<Object>) task.get('Status').get('picklist_values')).isEmpty());
        System.assert(((List<Object>) task.get('WhatId').get('reference_to')).contains('Account'));
        System.assert(((List<Object>) r.result().get('unknown')).contains('Nope__c'));
    }

    @IsTest
    static void limitedUserCannotDescribeInaccessibleObjects() {
        integrationUser(limitedUser().Username);
        Reply r = call('object_schema', new Map<String, Object>{ 'mode' => 'fields', 'objects' => new List<Object>{ 'Account' } });
        System.assert(((List<Object>) r.result().get('inaccessible')).contains('Account'));
        System.assert(((List<Object>) r.result().get('objects')).isEmpty());
    }

    @IsTest
    static void fieldModeValidatesItsInput() {
        System.assertEquals(400, call('object_schema', new Map<String, Object>{ 'mode' => 'fields', 'objects' => new List<Object>() }).status);
        System.assertEquals(400, call('object_schema', new Map<String, Object>{ 'mode' => 'everything' }).status);
    }

    // ---- record search -----------------------------------------------------

    @IsTest
    static void recordSearchFindsByNameAndById() {
        Reply r = call('record_search', new Map<String, Object>{ 'object' => 'account', 'query' => 'mumbai' });
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assertEquals('Account', r.result().get('object'));
        List<Object> recs = (List<Object>) r.result().get('records');
        System.assertEquals(1, recs.size());
        System.assertEquals('Acme Mumbai', ((Map<String, Object>) recs[0]).get('name'));
        Id beta = account('Beta Pune').Id;
        r = call('record_search', new Map<String, Object>{ 'object' => 'Account', 'query' => String.valueOf(beta) });
        System.assertEquals(String.valueOf(beta), ((Map<String, Object>) ((List<Object>) r.result().get('records'))[0]).get('id'));
        r = call('record_search', new Map<String, Object>{ 'object' => 'Account', 'query' => '%' });
        System.assertEquals(1, ((List<Object>) r.result().get('records')).size(), 'wildcards are escaped');
    }

    @IsTest
    static void recordSearchOnCustomObject() {
        Reply r = call('record_search', new Map<String, Object>{ 'object' => 'MinuteX_Site_Meeting__c', 'query' => 'SV-1' });
        System.assertEquals(1, ((List<Object>) r.result().get('records')).size());
    }

    @IsTest
    static void recordSearchRespectsPermissionsAndRejectsUnknownObjects() {
        System.assertEquals('invalid_object',
            call('record_search', new Map<String, Object>{ 'object' => 'Account; DELETE' }).error().get('code'));
        integrationUser(limitedUser().Username);
        Reply r = call('record_search', new Map<String, Object>{ 'object' => 'Account', 'query' => 'a' });
        System.assertEquals(403, r.status);
        System.assertEquals('permission_denied', r.error().get('code'));
    }

    // ---- create_task -------------------------------------------------------

    @IsTest
    static void createsTaskOnAStandardObjectWithAssigneeAndDueDate() {
        Id acct = account('Acme Mumbai').Id;
        Reply r = call('create_task', taskPayload(baseTask('Account', acct)), 'task-k1');
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assertEquals('created', r.result().get('operation'));
        Task t = [SELECT Subject, WhatId, WhoId, OwnerId, ActivityDate, Priority
                  FROM Task WHERE Id = :(Id) r.result().get('task_id')];
        System.assertEquals('Follow-up Call', t.Subject);
        System.assertEquals(acct, t.WhatId);
        System.assertEquals(UserInfo.getUserId(), t.OwnerId);
        System.assertEquals(Date.newInstance(2026, 10, 1), t.ActivityDate);
        System.assertEquals('High', t.Priority);
    }

    @IsTest
    static void unassignedTaskBelongsToTheIntegrationUser() {
        Map<String, Object> t = baseTask('Account', account('Acme Mumbai').Id);
        t.remove('assigned_user_id');
        Reply r = call('create_task', taskPayload(t), 'task-owner');
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assertEquals(UserInfo.getUserId(), [SELECT OwnerId FROM Task WHERE Id = :(Id) r.result().get('task_id')].OwnerId);
    }

    @IsTest
    static void createsTaskOnACustomObject() {
        Id meeting = [SELECT Id FROM MinuteX_Site_Meeting__c LIMIT 1].Id;
        Reply r = call('create_task', taskPayload(baseTask('MinuteX_Site_Meeting__c', meeting)), 'task-k2');
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assertEquals(meeting, [SELECT WhatId FROM Task WHERE Id = :(Id) r.result().get('task_id')].WhatId);
    }

    @IsTest
    static void contactRelationUsesWhoId() {
        Id c = [SELECT Id FROM Contact LIMIT 1].Id;
        Reply r = call('create_task', taskPayload(baseTask('Contact', c)), 'task-k3');
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assertEquals(c, [SELECT WhoId FROM Task WHERE Id = :(Id) r.result().get('task_id')].WhoId);
    }

    static Map<String, Object> rel(String objectType, Id recordId) {
        return new Map<String, Object>{ 'object_type' => objectType, 'record_id' => recordId };
    }

    @IsTest
    static void relatedRecordsSetWhoAndWhatTogether() {
        Id acct = account('Acme Mumbai').Id;
        Id c = [SELECT Id FROM Contact LIMIT 1].Id;
        Map<String, Object> t = baseTask('Account', acct);
        t.put('related_records', new List<Object>{ rel('Contact', c), rel('Account', acct) });
        Reply r = call('create_task', taskPayload(t), 'task-who-what');
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assert(String.isNotBlank((String) r.result().get('last_modified_at')));
        Task created = [SELECT WhoId, WhatId FROM Task WHERE Id = :(Id) r.result().get('task_id')];
        System.assertEquals(c, created.WhoId);
        System.assertEquals(acct, created.WhatId);

        // related_records IS the relationship: an update naming only the
        // Contact clears the What.
        Map<String, Object> u = new Map<String, Object>{
            'salesforce_task_id' => r.result().get('task_id'),
            'related_records' => new List<Object>{ rel('Contact', c) }
        };
        Reply upd = call('update_task', taskPayload(u), 'task-who-what-u1');
        System.assertEquals(200, upd.status, JSON.serialize(upd.body));
        Task updated = [SELECT WhoId, WhatId FROM Task WHERE Id = :(Id) r.result().get('task_id')];
        System.assertEquals(c, updated.WhoId);
        System.assertEquals(null, updated.WhatId);
    }

    @IsTest
    static void relatedRecordsRefuseTwoForOneFieldAndLeadWithWhat() {
        Id mumbai = account('Acme Mumbai').Id;
        Id pune = account('Beta Pune').Id;
        Map<String, Object> t = baseTask('Account', mumbai);
        t.put('related_records', new List<Object>{ rel('Account', mumbai), rel('Account', pune) });
        Reply r = call('create_task', taskPayload(t), 'task-two-whats');
        System.assertEquals(422, r.status);
        System.assertEquals('invalid_relationship', r.error().get('code'));

        Lead l = new Lead(LastName = 'Lead', Company = 'Lead Co');
        insert l;
        t.put('related_records', new List<Object>{ rel('Lead', l.Id), rel('Account', mumbai) });
        r = call('create_task', taskPayload(t), 'task-lead-what');
        System.assertEquals(422, r.status);
        System.assertEquals('invalid_relationship', r.error().get('code'));
        System.assertEquals(0, [SELECT COUNT() FROM Task]);
    }

    @IsTest
    static void duplicateRequestCreatesOneTask() {
        Map<String, Object> payload = taskPayload(baseTask('Account', account('Acme Mumbai').Id));
        Reply first = call('create_task', payload, 'same-task-key');
        Reply again = call('create_task', payload, 'same-task-key');
        System.assertEquals(true, again.body.get('duplicate'));
        System.assertEquals(first.result().get('task_id'), again.result().get('task_id'));
        System.assertEquals(1, [SELECT COUNT() FROM Task]);
    }

    @IsTest
    static void createTaskRejectsInvalidInput() {
        Id acct = account('Acme Mumbai').Id;
        Id contactId = [SELECT Id FROM Contact LIMIT 1].Id;
        Account gone = new Account(Name = 'Gone');
        insert gone;
        Id goneId = gone.Id;
        delete gone;
        Map<String, Object> t;

        t = baseTask('Account', contactId);
        System.assertEquals('invalid_record', call('create_task', taskPayload(t), 'v1').error().get('code'));
        t = baseTask('Account', goneId);
        System.assertEquals('invalid_record', call('create_task', taskPayload(t), 'v2').error().get('code'));
        t = baseTask('Account', acct);
        t.put('related_record_id', 'not-an-id');
        System.assertEquals('invalid_record', call('create_task', taskPayload(t), 'v3').error().get('code'));
        t = baseTask('Nope__c', acct);
        System.assertEquals('invalid_object', call('create_task', taskPayload(t), 'v4').error().get('code'));
        MinuteX_Site_Request__c log = new MinuteX_Site_Request__c(Idempotency_Key__c = 'x-log');
        insert log;
        t = baseTask('MinuteX_Site_Request__c', log.Id);
        System.assertEquals('object_not_task_relatable', call('create_task', taskPayload(t), 'v5').error().get('code'));
        t = baseTask('Account', acct);
        t.put('status', 'Dreaming');
        System.assertEquals('invalid_status', call('create_task', taskPayload(t), 'v6').error().get('code'));
        t = baseTask('Account', acct);
        t.put('assigned_user_id', inactiveUser().Id);
        System.assertEquals('invalid_assignee', call('create_task', taskPayload(t), 'v7').error().get('code'));
        t = baseTask('Account', acct);
        t.put('assigned_user_id', acct);
        System.assertEquals('invalid_assignee', call('create_task', taskPayload(t), 'v8').error().get('code'));
        t = baseTask('Account', acct);
        t.put('extra_fields', new Map<String, Object>{ 'Nope__c' => 'x' });
        System.assertEquals('invalid_field', call('create_task', taskPayload(t), 'v9').error().get('code'));
        t = baseTask('Account', acct);
        t.put('extra_fields', new Map<String, Object>{ 'WhatId' => contactId });
        System.assertEquals('invalid_field', call('create_task', taskPayload(t), 'v10').error().get('code'));
        t = baseTask('Account', acct);
        t.remove('related_object_type');
        t.remove('related_record_id');
        System.assertEquals('record_required', call('create_task', taskPayload(t), 'v11').error().get('code'));
        t = baseTask('Account', acct);
        t.put('subject', '');
        System.assertEquals(422, call('create_task', taskPayload(t), 'v12').status);
        t = baseTask('Account', acct);
        t.put('due_date', '2026-02-31');
        System.assertEquals(422, call('create_task', taskPayload(t), 'v13').status);
        System.assertEquals(400, call('create_task', taskPayload(baseTask('Account', acct)), null).status, 'key required');
        System.assertEquals(0, [SELECT COUNT() FROM Task], 'nothing was written');
    }

    @IsTest
    static void unrelatedTaskAllowedOnlyWhenTheMappingSaysSo() {
        Map<String, Object> t = baseTask('Account', account('Acme Mumbai').Id);
        t.remove('related_object_type');
        t.remove('related_record_id');
        t.put('record_required', false);
        System.assertEquals(200, call('create_task', taskPayload(t), 'free-task').status);
    }

    @IsTest
    static void extraFieldsAreValidatedAndSet() {
        Map<String, Object> t = baseTask('Account', account('Acme Mumbai').Id);
        t.put('extra_fields', new Map<String, Object>{ 'CallDisposition' => 'Left voicemail' });
        Reply r = call('create_task', taskPayload(t), 'extra-ok');
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assertEquals('Left voicemail', [SELECT CallDisposition FROM Task WHERE Id = :(Id) r.result().get('task_id')].CallDisposition);
    }

    @IsTest
    static void limitedIntegrationUserIsRefusedWithAClearError() {
        Id acct = account('Acme Mumbai').Id;
        integrationUser(limitedUser().Username);
        Reply r = call('create_task', taskPayload(baseTask('Account', acct)), 'limited-1');
        System.assertEquals(403, r.status);
        System.assertEquals('permission_denied', r.error().get('code'));
        System.assertEquals(MinuteXSiteTasks.DENIED_CREATE, r.error().get('message'));
        System.assertEquals(0, [SELECT COUNT() FROM Task]);
    }

    // ---- update_task -------------------------------------------------------

    @IsTest
    static void updatesAnExistingTask() {
        Id acct = account('Acme Mumbai').Id;
        Id taskId = (Id) call('create_task', taskPayload(baseTask('Account', acct)), 'upd-create').result().get('task_id');
        Reply r = call('update_task', taskPayload(new Map<String, Object>{
            'salesforce_task_id' => taskId, 'subject' => 'Follow-up Call (moved)', 'due_date' => '2026-10-08',
            'status' => 'Completed' }), 'upd-1');
        System.assertEquals(200, r.status, JSON.serialize(r.body));
        System.assertEquals('updated', r.result().get('operation'));
        Task t = [SELECT Subject, ActivityDate, WhatId FROM Task WHERE Id = :taskId];
        System.assertEquals('Follow-up Call (moved)', t.Subject);
        System.assertEquals(Date.newInstance(2026, 10, 8), t.ActivityDate);
        System.assertEquals(acct, t.WhatId, 'untouched fields keep their value');
        Reply dup = call('update_task', taskPayload(new Map<String, Object>{
            'salesforce_task_id' => taskId, 'subject' => 'ignored' }), 'upd-1');
        System.assertEquals(true, dup.body.get('duplicate'));
        System.assertEquals('Follow-up Call (moved)', [SELECT Subject FROM Task WHERE Id = :taskId].Subject);
    }

    @IsTest
    static void updateRejectsUnknownOrForeignIdsAndLimitedUsers() {
        Id acct = account('Acme Mumbai').Id;
        System.assertEquals('invalid_task', call('update_task', taskPayload(new Map<String, Object>{
            'salesforce_task_id' => acct }), 'u1').error().get('code'));
        Id taskId = (Id) call('create_task', taskPayload(baseTask('Account', acct)), 'u-create').result().get('task_id');
        delete new Task(Id = taskId);
        System.assertEquals(404, call('update_task', taskPayload(new Map<String, Object>{
            'salesforce_task_id' => taskId, 'subject' => 'x' }), 'u2').status);
        integrationUser(limitedUser().Username);
        Reply r = call('update_task', taskPayload(new Map<String, Object>{
            'salesforce_task_id' => taskId, 'subject' => 'x' }), 'u3');
        System.assertEquals(MinuteXSiteTasks.DENIED_UPDATE, r.error().get('message'));
    }
}
