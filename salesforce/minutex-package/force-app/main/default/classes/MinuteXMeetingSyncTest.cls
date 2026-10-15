/**
 * What one MinuteX event writes (MinuteXMeetingSync): the meeting, its
 * participants, new Contacts, and Salesforce Tasks — and the rules users
 * rely on:
 *   a task goes to the assignee's Salesforce USER; else their Contact (found
 *   or created from their email); else only their name is kept;
 *   an external participant with a new email becomes a Contact (once), an
 *   internal colleague never does;
 *   a replayed or older event changes nothing; a task or person that leaves
 *   the meeting is set aside, never deleted; clearing the record keeps the
 *   Contact.
 */
@IsTest
private class MinuteXMeetingSyncTest {
    static final String MEETING = 'mxm_' + '1'.repeat(32);

    static User owner() {
        Profile p = [SELECT Id FROM Profile WHERE Name = 'Standard User' LIMIT 1];
        User u = new User(Alias = 'mxown', Email = 'owner@minutex.test', EmailEncodingKey = 'UTF-8',
            LastName = 'Owner', LanguageLocaleKey = 'en_US', LocaleSidKey = 'en_US', ProfileId = p.Id,
            TimeZoneSidKey = 'Asia/Kolkata', Username = 'owner' + Datetime.now().getTime() + '@minutex.test');
        // A User and data rows cannot be written in one transaction.
        System.runAs(new User(Id = UserInfo.getUserId())) {
            insert u;
        }
        return u;
    }

    static Map<String, Object> person(String speakerId, String name, String email, String role,
                                      String kind, Id sfId) {
        Map<String, Object> p = new Map<String, Object>{
            'speaker_id' => speakerId, 'name' => name, 'email' => email, 'identity_role' => role};
        if (sfId != null) {
            p.put('external_identity', new Map<String, Object>{
                'provider' => 'salesforce', 'type' => kind, 'external_id' => String.valueOf(sfId)});
        }
        return p;
    }

    static Map<String, Object> item(String taskId, String title, String name, String email, String speakerId) {
        return new Map<String, Object>{
            'id' => taskId, 'title' => title, 'description' => 'Details', 'status' => 'Open',
            'priority' => 'High', 'due_date_normalized' => '2026-10-15',
            'assignee' => new Map<String, Object>{'name' => name, 'email' => email, 'speaker_id' => speakerId}};
    }

    static Map<String, Object> envelope(String kind, Integer seq, String recordId, List<Object> people,
                                     List<Object> tasks) {
        return new Map<String, Object>{
            'type' => 'event', 'event' => kind, 'meeting_id' => MEETING, 'sequence' => seq,
            'event_id' => 'evt_' + String.valueOf(seq).leftPad(32, '0'),
            'meeting_type' => new Map<String, Object>{'id' => 'site_visit', 'name' => 'Site Visit'},
            'record_context' => recordId == null ? null
                : new Map<String, Object>{'record_id' => recordId, 'display_name' => 'PV-1 — Mumbai Heights'},
            'data' => new Map<String, Object>{
                'meeting' => new Map<String, Object>{'title' => 'Site walk', 'lifecycle' => 'active'},
                'participants' => people, 'action_items' => tasks,
                'summary' => new Map<String, Object>{'text' => 'Summary'},
                'minutes' => new Map<String, Object>{'markdown' => '## Minutes'},
                'transcript' => new Map<String, Object>{'text' => 'Speaker 0: hello'}}};
    }

    /** A MoM PDF (the transcript is no longer a File: see transcriptFileReq). */
    static Map<String, Object> fileReq(String body) {
        Blob content = Blob.valueOf(body);
        return new Map<String, Object>{
            'type' => 'file.upload', 'meeting_id' => MEETING, 'kind' => 'mom_pdf',
            'key' => MEETING + ':mom_pdf', 'filename' => 'MoM – Site walk.pdf',
            'content_base64' => EncodingUtil.base64Encode(content),
            'sha256' => EncodingUtil.convertToHex(Crypto.generateDigest('SHA-256', content))};
    }

    @IsTest
    static void aFileArrivesBeforeTheEventAndAChangeIsANewVersion() {
        Map<String, Object> first = MinuteXMeetingSync.applyFile(fileReq('Speaker 0: hello'));
        MinuteX_Meeting__c m = [SELECT Id FROM MinuteX_Meeting__c];
        System.assertEquals(1, [SELECT COUNT() FROM ContentDocumentLink WHERE LinkedEntityId = :m.Id]);
        MinuteXMeetingSync.apply(envelope('meeting.created', 1, null, new List<Object>(), new List<Object>()));
        Map<String, Object> second = MinuteXMeetingSync.applyFile(fileReq('Speaker 0: hello again'));
        System.assertEquals(first.get('content_document_id'), second.get('content_document_id'),
            'same File, new version');
        Id documentId = (Id) first.get('content_document_id');
        System.assertEquals(2, [SELECT COUNT() FROM ContentVersion WHERE ContentDocumentId = :documentId]);
        System.assertEquals(1, [SELECT COUNT() FROM MinuteX_Meeting__c]);
    }

    static Map<String, Object> linked(Integer seq, List<Object> links) {
        Map<String, Object> env = envelope('meeting.updated', seq, null, new List<Object>(), new List<Object>());
        env.put('links', links);
        ((Map<String, Object>) ((Map<String, Object>) env.get('data')).get('meeting'))
            .put('started_at', '2026-10-08T05:12:00Z');
        return env;
    }

    static Map<String, Object> link(SObject r, String name, Boolean primary) {
        return new Map<String, Object>{'object' => String.valueOf(r.getSObjectType()), 'id' => String.valueOf(r.Id),
                                       'name' => name, 'primary' => primary};
    }

    @IsTest
    static void eachLinkedRecordShowsTheMeetingAndItsFiles() {
        Lead lead = new Lead(LastName = 'Sharma', FirstName = 'Rahul', Company = 'Sharma Traders');
        Account acct = new Account(Name = 'Sharma Family');
        insert new List<SObject>{lead, acct};
        MinuteXMeetingSync.applyFile(fileReq('Speaker 0: hello'));
        MinuteXMeetingSync.apply(linked(1, new List<Object>{
            link(lead, 'Rahul Sharma', true), link(acct, 'Sharma Family', false)}));

        MinuteX_Meeting__c m = [SELECT Id FROM MinuteX_Meeting__c];
        List<MinuteX_Meeting_Link__c> rows = [
            SELECT Lead__c, Account__c, Primary__c, Object__c, Name, Summary__c, Meeting_Date__c, MinuteX_Meeting__c
            FROM MinuteX_Meeting_Link__c ORDER BY Object__c];
        System.assertEquals(2, rows.size());
        System.assertEquals(acct.Id, rows[0].Account__c);
        System.assertEquals(false, rows[0].Primary__c);
        System.assertEquals(lead.Id, rows[1].Lead__c);
        System.assertEquals(true, rows[1].Primary__c);
        System.assertEquals('Site walk', rows[1].Name);
        System.assertEquals('Summary', rows[1].Summary__c);
        System.assertEquals(m.Id, rows[1].MinuteX_Meeting__c);
        System.assertEquals(Datetime.newInstanceGmt(2026, 10, 8, 5, 12, 0), rows[1].Meeting_Date__c);
        // The transcript is on both records' Files lists too.
        System.assertEquals(1, [SELECT COUNT() FROM ContentDocumentLink WHERE LinkedEntityId = :lead.Id]);
        System.assertEquals(1, [SELECT COUNT() FROM ContentDocumentLink WHERE LinkedEntityId = :acct.Id]);
        // Link only: the Lead itself is untouched.
        System.assertEquals('Sharma Traders', [SELECT Company FROM Lead WHERE Id = :lead.Id].Company);
    }

    @IsTest
    static void unlinkingRemovesTheRowAndTheShareButNeverTheRecord() {
        Lead lead = new Lead(LastName = 'Sharma', Company = 'Traders');
        Account acct = new Account(Name = 'Sharma Family');
        insert new List<SObject>{lead, acct};
        MinuteXMeetingSync.applyFile(fileReq('Speaker 0: hello'));
        MinuteXMeetingSync.apply(linked(1, new List<Object>{link(lead, 'Sharma', true), link(acct, 'Family', false)}));
        MinuteXMeetingSync.apply(linked(2, new List<Object>{link(acct, 'Family', true)}));

        List<MinuteX_Meeting_Link__c> rows = [SELECT Account__c, Primary__c FROM MinuteX_Meeting_Link__c];
        System.assertEquals(1, rows.size());
        System.assertEquals(acct.Id, rows[0].Account__c);
        System.assertEquals(true, rows[0].Primary__c, 'the new main record');
        System.assertEquals(0, [SELECT COUNT() FROM ContentDocumentLink WHERE LinkedEntityId = :lead.Id]);
        System.assertEquals(1, [SELECT COUNT() FROM Lead WHERE Id = :lead.Id]);
    }

    @IsTest
    static void aRecordThatNoLongerExistsIsSkippedNotAFailure() {
        Lead gone = new Lead(LastName = 'Gone', Company = 'X');
        Account acct = new Account(Name = 'Here');
        insert new List<SObject>{gone, acct};
        delete gone;
        MinuteXMeetingSync.apply(linked(1, new List<Object>{link(gone, 'Gone', true), link(acct, 'Here', false)}));
        List<MinuteX_Meeting_Link__c> rows = [SELECT Account__c FROM MinuteX_Meeting_Link__c];
        System.assertEquals(1, rows.size());
        System.assertEquals(acct.Id, rows[0].Account__c);
    }

    static Map<String, Object> withTasks(Integer seq, List<Object> links, List<Object> tasks) {
        Map<String, Object> env = linked(seq, links);
        ((Map<String, Object>) env.get('data')).put('action_items', tasks);
        return env;
    }

    @IsTest
    static void tasksGoOnTheCustomersRecordNotTheMeeting() {
        Lead lead = new Lead(LastName = 'Sharma', Company = 'Traders');
        Account acct = new Account(Name = 'Sharma Family');
        insert new List<SObject>{lead, acct};
        Map<String, Object> toAccount = item('t2', 'Send the quote', '', '', '');
        toAccount.put('related', new Map<String, Object>{'object' => 'Account', 'id' => String.valueOf(acct.Id)});
        MinuteXMeetingSync.apply(withTasks(1,
            new List<Object>{link(lead, 'Sharma', true), link(acct, 'Family', false)},
            new List<Object>{item('t1', 'Call back', '', '', ''), toAccount}));

        Task onLead = [SELECT WhoId, WhatId, MinuteX_Meeting_Id__c FROM Task WHERE MinuteX_Task_Id__c = 't1'];
        System.assertEquals(lead.Id, onLead.WhoId, 'the main record (a Lead) is the Name');
        System.assertEquals(null, onLead.WhatId, 'a Lead task has no Related To');
        System.assertEquals(MEETING, onLead.MinuteX_Meeting_Id__c);
        System.assertEquals(acct.Id, [SELECT WhatId FROM Task WHERE MinuteX_Task_Id__c = 't2'].WhatId,
            'a task pointed at the Account is Related To it');
        MinuteX_Meeting__c m = [SELECT Id FROM MinuteX_Meeting__c];
        System.assertEquals(0, [SELECT COUNT() FROM Task WHERE WhatId = :m.Id]);
    }

    @IsTest
    static void aNewMainRecordMovesTheTasksAndALeftTaskIsSetAside() {
        Lead lead = new Lead(LastName = 'Sharma', Company = 'Traders');
        Account acct = new Account(Name = 'Sharma Family');
        insert new List<SObject>{lead, acct};
        MinuteXMeetingSync.apply(withTasks(1, new List<Object>{link(lead, 'Sharma', true)},
            new List<Object>{item('t1', 'Call back', '', '', ''), item('t2', 'Quote', '', '', '')}));
        MinuteXMeetingSync.apply(withTasks(2, new List<Object>{link(acct, 'Family', true)},
            new List<Object>{item('t1', 'Call back', '', '', '')}));
        Task t1 = [SELECT WhoId, WhatId FROM Task WHERE MinuteX_Task_Id__c = 't1'];
        System.assertEquals(acct.Id, t1.WhatId);
        System.assertEquals(null, t1.WhoId, 'no longer on the Lead');
        System.assertEquals('Deferred', [SELECT Status FROM Task WHERE MinuteX_Task_Id__c = 't2'].Status);
    }

    @IsTest
    static void theTranscriptIsTextAndAnOldTranscriptFileIsRemoved() {
        // An older MinuteX sent the transcript as a .txt File.
        Map<String, Object> old = fileReq('Speaker 0: hello');
        old.put('kind', 'transcript');
        old.put('key', MEETING + ':transcript');
        old.put('filename', 'Transcript – Site walk.txt');
        MinuteXMeetingSync.applyFile(old);
        Map<String, Object> env = linked(1, new List<Object>());
        ((Map<String, Object>) ((Map<String, Object>) env.get('data')).get('transcript'))
            .put('text', '[00:00:05] Amit: hello');
        MinuteXMeetingSync.apply(env);
        System.assertEquals('[00:00:05] Amit: hello', [SELECT Transcript__c FROM MinuteX_Meeting__c].Transcript__c);
        System.assertEquals(0, [SELECT COUNT() FROM ContentDocument], 'the old transcript File is gone');
    }

    @IsTest
    static void aLongTranscriptFillsTheNextFields() {
        Map<String, Object> env = linked(1, new List<Object>());
        ((Map<String, Object>) ((Map<String, Object>) env.get('data')).get('transcript')).putAll(
            new Map<String, Object>{'text' => 'part one', 'parts' => new List<Object>{'part one', 'part two', 'part three'}});
        MinuteXMeetingSync.apply(env);
        MinuteX_Meeting__c m = [SELECT Transcript__c, Transcript_2__c, Transcript_3__c, Transcript_4__c FROM MinuteX_Meeting__c];
        System.assertEquals('part one', m.Transcript__c);
        System.assertEquals('part two', m.Transcript_2__c);
        System.assertEquals('part three', m.Transcript_3__c);
        System.assertEquals(null, m.Transcript_4__c);
        // A shorter transcript later clears the parts it no longer needs.
        Map<String, Object> shorter = linked(2, new List<Object>());
        ((Map<String, Object>) ((Map<String, Object>) shorter.get('data')).get('transcript'))
            .put('text', 'just one');                       // an older MinuteX: text only
        MinuteXMeetingSync.apply(shorter);
        m = [SELECT Transcript__c, Transcript_2__c FROM MinuteX_Meeting__c];
        System.assertEquals('just one', m.Transcript__c);
        System.assertEquals(null, m.Transcript_2__c);
    }

    @IsTest
    static void everyLookupOnTheLinkGivesARelatedList() {
        Map<String, Schema.SObjectField> f = MinuteXMeetingSync.lookupFields();
        System.assert(f.keySet().containsAll(new Set<String>{'Lead', 'Contact', 'Account', 'Opportunity'}), f.keySet());
        System.assert(!f.containsKey(MinuteX_Meeting__c.SObjectType.getDescribe().getName()),
                      'the meeting itself is not a linked record');
    }

    @IsTest
    static void meetingDates() {
        System.assertEquals(Datetime.newInstanceGmt(2026, 10, 8, 5, 12, 0),
                            MinuteXMeetingSync.meetingDate('2026-10-08T05:12:00.123Z'));
        System.assertEquals(null, MinuteXMeetingSync.meetingDate(''));
        System.assertEquals(null, MinuteXMeetingSync.meetingDate('not a date at all!!'));
    }

    @IsTest
    static void aDamagedFileIsRefused() {
        Map<String, Object> req = fileReq('x');
        req.put('sha256', 'deadbeef');
        try {
            MinuteXMeetingSync.applyFile(req);
            System.assert(false, 'should refuse');
        } catch (MinuteXMeetingSync.SyncException e) {
            System.assertEquals('hash_mismatch', e.code);
        }
    }

    @IsTest
    static void byDefaultNoContactIsCreated() {
        List<Object> people = new List<Object>{
            person('0', 'Asha Rao', 'asha@newco.com', 'external', null, null)};
        List<Object> tasks = new List<Object>{item('t1', 'Share plan', 'Asha Rao', 'asha@newco.com', '')};
        MinuteXMeetingSync.apply(envelope('meeting.created', 1, null, people, tasks));
        System.assertEquals(0, [SELECT COUNT() FROM Contact], 'link only: no Contact is added');
        MinuteX_Participant__c p = [SELECT Email__c, Contact__c FROM MinuteX_Participant__c];
        System.assertEquals('asha@newco.com', p.Email__c);
        System.assertEquals(null, p.Contact__c);
        Task t = [SELECT WhoId, MinuteX_Assignee_Name__c FROM Task];
        System.assertEquals(null, t.WhoId);
        System.assertEquals('Asha Rao', t.MinuteX_Assignee_Name__c);
    }

    @IsTest
    static void oneEventWritesTheMeetingPeopleContactsAndTasks() {
        MinuteXMeetingSync.createContacts = true;  // the optional behaviour
        User rep = owner();
        Contact john = new Contact(FirstName = 'John', LastName = 'Smith', Email = 'john@client.com');
        insert john;
        List<Object> people = new List<Object>{
            person('0', 'John Smith', 'john@client.com', 'external', 'contact', john.Id),
            person('1', 'Rep Owner', 'owner@minutex.test', 'internal', 'user', rep.Id),
            person('2', 'Asha Rao', 'asha@newco.com', 'external', null, null),     // -> new Contact
            person('3', 'Dev Colleague', 'dev@ourco.com', 'internal', null, null)   // -> never a Contact
        };
        List<Object> tasks = new List<Object>{
            item('t1', 'Send deck', 'Rep Owner', '', '1'),              // tagged User
            item('t2', 'Share plan', 'Asha Rao', 'asha@newco.com', ''), // Contact from email
            item('t3', 'Call bank', 'Ravi', '', ''),                    // name only
            item('t4', 'Book van', 'Pat', 'owner@minutex.test', '')     // User by email
        };
        Map<String, Object> out = MinuteXMeetingSync.apply(envelope('meeting.created', 1, 'a0P000000000001', people, tasks));
        System.assertEquals(false, out.get('duplicate'));

        MinuteX_Meeting__c m = [SELECT Id, Contact__c, Record_Id__c FROM MinuteX_Meeting__c];
        System.assertEquals(john.Id, m.Contact__c);
        System.assertEquals('a0P000000000001', m.Record_Id__c);

        Contact asha = [SELECT Id, FirstName, LastName FROM Contact WHERE Email = 'asha@newco.com'];
        System.assertEquals('Asha', asha.FirstName);
        System.assertEquals(0, [SELECT COUNT() FROM Contact WHERE Email = 'dev@ourco.com'],
            'an internal colleague is never made a customer Contact');
        System.assertEquals(4, [SELECT COUNT() FROM MinuteX_Participant__c WHERE MinuteX_Meeting__c = :m.Id]);
        System.assertEquals(rep.Id, [SELECT User__c FROM MinuteX_Participant__c
                                     WHERE Speaker_Id__c = '1'].User__c);

        Map<String, Task> byKey = new Map<String, Task>();
        for (Task t : [SELECT MinuteX_Task_Id__c, OwnerId, WhoId, WhatId, Priority, Status, ActivityDate,
                              MinuteX_Assignee_Name__c, Description FROM Task]) {
            byKey.put(t.MinuteX_Task_Id__c, t);
        }
        System.assertEquals(4, byKey.size());
        System.assertEquals(rep.Id, byKey.get('t1').OwnerId, 'a Salesforce User owns the task');
        System.assertEquals(rep.Id, byKey.get('t4').OwnerId, 'matched by email');
        System.assertEquals(asha.Id, byKey.get('t2').WhoId, 'otherwise the assignee Contact');
        System.assertEquals('Ravi', byKey.get('t3').MinuteX_Assignee_Name__c, 'otherwise just the name');
        System.assertEquals(john.Id, byKey.get('t3').WhoId, 'falls back to the meeting Contact');
        System.assertEquals(m.Id, byKey.get('t1').WhatId);
        System.assertEquals('High', byKey.get('t1').Priority);
        System.assertEquals('Not Started', byKey.get('t1').Status);
        System.assertEquals(Date.newInstance(2026, 10, 15), byKey.get('t1').ActivityDate);
    }

    @IsTest
    static void anUpdateKeepsOneOfEachAndSetsAsideWhatLeft() {
        MinuteXMeetingSync.createContacts = true;
        Contact john = new Contact(LastName = 'Smith', Email = 'john@client.com');
        insert john;
        List<Object> people = new List<Object>{
            person('0', 'John Smith', 'john@client.com', 'external', 'contact', john.Id),
            person('2', 'Asha Rao', 'asha@newco.com', 'external', null, null)};
        MinuteXMeetingSync.apply(envelope('meeting.created', 1, 'a0P000000000001', people,
            new List<Object>{item('t1', 'Send deck', 'Asha Rao', 'asha@newco.com', ''),
                             item('t2', 'Call bank', 'Ravi', '', '')}));
        // Asha leaves, t1 is removed, the record is cleared, t2 is renamed.
        MinuteXMeetingSync.apply(envelope('meeting.updated', 2, null,
            new List<Object>{people[0]}, new List<Object>{item('t2', 'Call the bank', 'Ravi', '', '')}));

        System.assertEquals(1, [SELECT COUNT() FROM MinuteX_Meeting__c]);
        System.assertEquals(1, [SELECT COUNT() FROM Contact WHERE Email = 'asha@newco.com'], 'never a duplicate');
        MinuteX_Meeting__c m = [SELECT Contact__c, Record_Id__c FROM MinuteX_Meeting__c];
        System.assertEquals(null, m.Record_Id__c);
        System.assertEquals(john.Id, m.Contact__c, 'clearing the record keeps the Contact');
        System.assertEquals(true, [SELECT Removed__c FROM MinuteX_Participant__c WHERE Speaker_Id__c = '2'].Removed__c);
        System.assertEquals('Deferred', [SELECT Status FROM Task WHERE MinuteX_Task_Id__c = 't1'].Status);
        System.assertEquals('Call the bank', [SELECT Subject FROM Task WHERE MinuteX_Task_Id__c = 't2'].Subject);
        System.assertEquals(2, [SELECT COUNT() FROM Task]);
    }

    @IsTest
    static void aReplayOrAnOlderEventChangesNothing() {
        MinuteXMeetingSync.apply(envelope('meeting.created', 5, null, new List<Object>(),
            new List<Object>{item('t1', 'v5', 'Ravi', '', '')}));
        Map<String, Object> again = MinuteXMeetingSync.apply(envelope('meeting.created', 5, null,
            new List<Object>(), new List<Object>{item('t1', 'v5', 'Ravi', '', '')}));
        System.assertEquals(true, again.get('duplicate'));
        MinuteXMeetingSync.apply(envelope('meeting.updated', 4, null, new List<Object>(),
            new List<Object>{item('t1', 'older', 'Ravi', '', '')}));
        System.assertEquals('v5', [SELECT Subject FROM Task].Subject);
        System.assertEquals(1, [SELECT COUNT() FROM Task]);
    }

    @IsTest
    static void deletedMarksTheMeetingAndLeavesItsTasks() {
        MinuteXMeetingSync.apply(envelope('meeting.created', 1, null, new List<Object>(),
            new List<Object>{item('t1', 'Send deck', 'Ravi', '', '')}));
        MinuteXMeetingSync.apply(envelope('meeting.deleted', 2, null, new List<Object>(), new List<Object>()));
        System.assertEquals('deleted', [SELECT Lifecycle__c FROM MinuteX_Meeting__c].Lifecycle__c);
        System.assertEquals('Not Started', [SELECT Status FROM Task].Status);
    }

    @IsTest
    static void anInvalidEventIsRefused() {
        try {
            MinuteXMeetingSync.apply(new Map<String, Object>{'event' => 'meeting.exploded',
                'meeting_id' => MEETING, 'sequence' => 1});
            System.assert(false, 'expected a SyncException');
        } catch (MinuteXMeetingSync.SyncException e) {
            System.assertEquals(400, e.status);
        }
    }

    @IsTest
    static void mappings() {
        System.assertEquals('Completed', MinuteXMeetingSync.status('Completed'));
        System.assertEquals('In Progress', MinuteXMeetingSync.status('In Progress'));
        System.assertEquals('Deferred', MinuteXMeetingSync.status('Cancelled'));
        System.assertEquals('Not Started', MinuteXMeetingSync.status('Open'));
        System.assertEquals('Normal', MinuteXMeetingSync.priority('Medium'));
        System.assertEquals('Low', MinuteXMeetingSync.priority('low'));
        Contact c = MinuteXMeetingSync.newContact('', 'priya@x.com');
        System.assertEquals('priya', c.LastName);
        c = MinuteXMeetingSync.newContact('Mary Ann Lee', 'm@x.com');
        System.assertEquals('Mary Ann', c.FirstName);
        System.assertEquals('Lee', c.LastName);
    }
}
