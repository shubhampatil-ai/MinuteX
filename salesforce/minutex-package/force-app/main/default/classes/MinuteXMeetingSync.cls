/**
 * Applies one MinuteX meeting event to this org. Called by MinuteXReceiver
 * after it has checked the signature. Depends on nothing else, so it can be
 * pasted (and saved) BEFORE MinuteXReceiver.
 *
 * WHAT ONE EVENT WRITES
 *   MinuteX Meeting      one record per meeting (upsert on MinuteX Meeting Id)
 *   MinuteX Participants one row per person in the meeting, linked to the
 *                        Salesforce Contact or User they are; a person who
 *                        leaves the meeting is marked Removed, not deleted
 *   (nothing else)       MinuteX never creates or changes YOUR records:
 *                        existing Contacts and Users are only looked up, and
 *                        a person who isn't in Salesforce keeps just a name
 *                        and email on the MinuteX Participant row
 *   Transcript           as text, in the MinuteX Meeting's Transcript
 *                        fields: part 1 in Transcript, longer ones go on in
 *                        Transcript (part 2) to (part 4), split at line
 *                        ends. Never a File; an older MinuteX transcript
 *                        File of the meeting is removed
 *   Files                the MoM and generated documents as PDFs when the
 *                        meeting includes them, on the MinuteX Meeting; a
 *                        changed file becomes a new version of the same File
 *   Meeting Links        one MinuteX Meeting Link per Salesforce record the
 *                        meeting is linked to (up to 5), so the meeting shows
 *                        in that record's "MinuteX Meetings" related list,
 *                        and the meeting's Files are shared to the record.
 *                        A record that is unlinked loses its row and shares.
 *                        The linked record itself is never changed.
 *   Tasks                one Salesforce Task per MinuteX task, ON THE
 *                        CUSTOMER'S RECORD: the record the task was pointed
 *                        at in MinuteX, else the meeting's main record (a
 *                        Lead or Contact as the Task's Name, any other
 *                        record as Related To). A meeting with no linked
 *                        record keeps its Tasks on the MinuteX Meeting.
 *                        Upsert on MinuteX Task Id; a task that leaves the
 *                        meeting is set to Deferred
 *
 * WHO A TASK BELONGS TO
 *   1. the assignee is a Salesforce USER (tagged as one in MinuteX, or their
 *      email matches an active User)        -> Assigned To = that User
 *   2. otherwise, the assignee has a Contact (tagged, or matched by their
 *      email)                                -> Name = that Contact
 *   3. otherwise                             -> only their name is kept, in
 *                                               MinuteX Assignee Name
 *   The Task's Name falls back to the meeting's Contact when (2) doesn't apply.
 *
 * SAFE TO RECEIVE TWICE. Every event is a full snapshot with a `sequence`;
 * the meeting keeps the sequence it holds, so a retried or older event
 * changes nothing. Any failure rolls everything back (MinuteXReceiver), and
 * MinuteX's retry applies the event again.
 */
public without sharing class MinuteXMeetingSync {
    /** Link only: MinuteX never adds Contacts to your org. A person who isn't
     *  in Salesforce yet is kept by name and email on the participant row. */
    @TestVisible
    static Boolean createContacts = false;
    static final Set<String> EVENTS = new Set<String>{'meeting.created', 'meeting.updated', 'meeting.deleted'};
    static final Pattern EMAIL = Pattern.compile('[^@\\s]+@[^@\\s]+\\.[^@\\s]+');

    public class SyncException extends Exception {
        public Integer status = 400;
        public String code = 'invalid_event';
    }

    /** One person in the meeting, and who they are in Salesforce. */
    public class Person {
        public String key;
        public String speakerId = '';
        public String name = '';
        public String email = '';
        public String role = '';
        public Id contactId;
        public Id userId;
    }

    public static Map<String, Object> apply(Map<String, Object> env) {
        String eventType = str(env.get('event'));
        String meetingId = str(env.get('meeting_id'));
        Integer seq = intOf(env.get('sequence'));
        if (String.isBlank(meetingId) || meetingId.length() > 40 || seq < 1 || !EVENTS.contains(eventType)) {
            SyncException e = new SyncException('Not a valid MinuteX event.');
            throw e;
        }
        List<MinuteX_Meeting__c> found = [
            SELECT Id, MinuteX_Sequence__c FROM MinuteX_Meeting__c
            WHERE MinuteX_Meeting_Id__c = :meetingId LIMIT 1 FOR UPDATE
        ];
        if (!found.isEmpty() && found[0].MinuteX_Sequence__c != null && found[0].MinuteX_Sequence__c >= seq) {
            return new Map<String, Object>{'ok' => true, 'duplicate' => true, 'result' => result(found[0].Id)};
        }
        Map<String, Object> data = asMap(env.get('data'));
        Map<String, Object> meeting = asMap(data.get('meeting'));
        Map<String, Object> record = asMap(env.get('record_context'));
        // A meeting linked to several records (MinuteX contract 3.0): this
        // starter keeps the MAIN record in the same fields as a single record.
        for (Object l : asList(env.get('links'))) {
            Map<String, Object> link = asMap(l);
            if (link.get('primary') == true) {
                record = new Map<String, Object>{
                    'record_id' => link.get('id'), 'display_name' => link.get('name')};
            }
        }
        Boolean deleted = eventType == 'meeting.deleted';

        MinuteX_Meeting__c rec = new MinuteX_Meeting__c(
            MinuteX_Meeting_Id__c = meetingId,
            MinuteX_Sequence__c = seq,
            Last_Event_Id__c = str(env.get('event_id')).left(40),
            // The business record (optional) and the people are SEPARATE:
            // clearing one never touches the other.
            Record_Id__c = blankToNull(str(record.get('record_id')).left(64)),
            Record_Name__c = blankToNull(str(record.get('display_name')).left(255)));
        List<Person> people = new List<Person>();
        if (deleted) {
            rec.Lifecycle__c = 'deleted';
            if (found.isEmpty()) {
                rec.Name = 'MinuteX meeting';
            }
        } else {
            people = resolvePeople(meetingId, asList(data.get('participants')));
            String title = str(meeting.get('title'));
            rec.Name = String.isBlank(title) ? 'MinuteX meeting' : title.left(80);
            rec.Lifecycle__c = String.isBlank(str(meeting.get('lifecycle'))) ? 'active' : str(meeting.get('lifecycle')).left(20);
            rec.Started_At__c = str(meeting.get('started_at')).left(40);
            rec.Summary__c = str(asMap(data.get('summary')).get('text')).left(32768);
            Map<String, Object> transcript = asMap(data.get('transcript'));
            List<Object> parts = asList(transcript.get('parts'));
            if (parts.isEmpty() && String.isNotBlank(str(transcript.get('text')))) {
                parts = new List<Object>{transcript.get('text')};
            }
            rec.Transcript__c = transcriptPart(parts, 0);
            rec.Transcript_2__c = transcriptPart(parts, 1);
            rec.Transcript_3__c = transcriptPart(parts, 2);
            rec.Transcript_4__c = transcriptPart(parts, 3);
            String audio = str(asMap(data.get('audio')).get('url'));
            rec.Audio_URL__c = audio.startsWith('https://') ? audio.left(1000) : null;
            rec.Contact__c = meetingContact(people);
        }
        upsert rec MinuteX_Meeting__c.MinuteX_Meeting_Id__c;
        if (!deleted) {
            removeTranscriptFile(meetingId);
            syncParticipants(rec.Id, people);
            List<MinuteX_Meeting_Link__c> linked =
                syncLinks(rec, meetingId, asList(env.get('links')), str(meeting.get('started_at')));
            syncTasks(rec.Id, meetingId, asList(data.get('action_items')), people, rec.Contact__c, linked);
        }
        return new Map<String, Object>{'ok' => true, 'duplicate' => false, 'result' => result(rec.Id)};
    }

    // -- people --------------------------------------------------------------

    /** Who each participant is in Salesforce: the Contact or User MinuteX was
     *  told (tagging), else matched by email (a User first, then a Contact),
     *  else a NEW Contact for an external person with an email. */
    @TestVisible
    static List<Person> resolvePeople(String meetingId, List<Object> participants) {
        List<Person> out = new List<Person>();
        Set<String> contactIds = new Set<String>();
        Set<String> userIds = new Set<String>();
        for (Object o : participants) {
            Map<String, Object> p = asMap(o);
            Person x = new Person();
            x.speakerId = str(p.get('speaker_id')).left(40);
            x.name = str(p.get('name')).left(80);
            x.email = cleanEmail(str(p.get('email')));
            x.role = str(p.get('identity_role')).toLowerCase().left(20);
            Map<String, Object> ident = asMap(p.get('external_identity'));
            String externalId = str(ident.get('external_id'));
            if (str(ident.get('provider')) == 'salesforce') {
                if (str(ident.get('type')) == 'contact' && isRecordId(externalId, '003')) {
                    x.contactId = Id.valueOf(externalId);
                    contactIds.add(externalId);
                } else if (str(ident.get('type')) == 'user' && isRecordId(externalId, '005')) {
                    x.userId = Id.valueOf(externalId);
                    userIds.add(externalId);
                }
            }
            String who = String.isNotBlank(x.speakerId) ? 'spk:' + x.speakerId
                : String.isNotBlank(x.email) ? 'email:' + x.email
                : String.isNotBlank(x.name) ? 'name:' + x.name.toLowerCase() : '';
            if (who == '') {
                continue;
            }
            x.key = (meetingId + '|' + who).left(120);
            out.add(x);
        }
        // Tagged ids must exist here (a Contact from another org, or a
        // deleted one, would fail the lookup write).
        Set<Id> liveContacts = new Map<Id, Contact>([SELECT Id FROM Contact WHERE Id IN :contactIds]).keySet();
        Set<Id> liveUsers = new Map<Id, User>([SELECT Id FROM User WHERE Id IN :userIds AND IsActive = true]).keySet();
        Map<String, String> nameByEmail = new Map<String, String>();
        for (Person x : out) {
            if (x.contactId != null && !liveContacts.contains(x.contactId)) {
                x.contactId = null;
            }
            if (x.userId != null && !liveUsers.contains(x.userId)) {
                x.userId = null;
            }
            if (x.contactId == null && x.userId == null && String.isNotBlank(x.email)) {
                nameByEmail.put(x.email, x.name);
            }
        }
        Map<String, Id> users = usersByEmail(nameByEmail.keySet());
        Map<String, String> toContact = new Map<String, String>();
        for (Person x : out) {
            if (x.contactId == null && x.userId == null && String.isNotBlank(x.email)) {
                if (users.containsKey(x.email)) {
                    x.userId = users.get(x.email);
                } else if (x.role != 'internal') {
                    // An internal colleague who is not a Salesforce User is
                    // never turned into a customer Contact.
                    toContact.put(x.email, x.name);
                }
            }
        }
        Map<String, Id> contacts = contactsByEmail(toContact, createContacts);
        for (Person x : out) {
            if (x.contactId == null && x.userId == null && contacts.containsKey(x.email)) {
                x.contactId = contacts.get(x.email);
            }
        }
        return out;
    }

    /** The meeting's Contact: the first EXTERNAL person with a Contact. */
    static Id meetingContact(List<Person> people) {
        for (Person x : people) {
            if (x.contactId != null && x.role != 'internal') {
                return x.contactId;
            }
        }
        for (Person x : people) {
            if (x.contactId != null) {
                return x.contactId;
            }
        }
        return null;
    }

    static void syncParticipants(Id meetingRecordId, List<Person> people) {
        List<MinuteX_Participant__c> rows = new List<MinuteX_Participant__c>();
        Set<String> keys = new Set<String>();
        for (Person x : people) {
            keys.add(x.key);
            rows.add(new MinuteX_Participant__c(
                Participant_Key__c = x.key,
                MinuteX_Meeting__c = meetingRecordId,
                Name = String.isNotBlank(x.name) ? x.name : (String.isNotBlank(x.email) ? x.email.left(80) : 'Participant'),
                Speaker_Id__c = blankToNull(x.speakerId),
                Email__c = blankToNull(x.email),
                Role__c = blankToNull(x.role),
                Contact__c = x.contactId,
                User__c = x.userId,
                Removed__c = false));
        }
        if (!rows.isEmpty()) {
            upsert rows MinuteX_Participant__c.Participant_Key__c;
        }
        List<MinuteX_Participant__c> gone = [
            SELECT Id FROM MinuteX_Participant__c
            WHERE MinuteX_Meeting__c = :meetingRecordId AND Removed__c = false AND Participant_Key__c NOT IN :keys
        ];
        for (MinuteX_Participant__c g : gone) {
            g.Removed__c = true;
        }
        update gone;
    }

    // -- linked records ------------------------------------------------------

    /**
     * Object -> its lookup on MinuteX Meeting Link: every lookup the link
     * has, so a lookup the admin adds to their own object (say Property)
     * gives that object the "MinuteX Meetings" related list too. Field
     * tokens from describe: right with or without the package namespace.
     */
    public static Map<String, Schema.SObjectField> lookupFields() {
        if (lookupCache != null) {
            return lookupCache;
        }
        lookupCache = new Map<String, Schema.SObjectField>();
        for (Schema.SObjectField f : Schema.SObjectType.MinuteX_Meeting_Link__c.fields.getMap().values()) {
            Schema.DescribeFieldResult d = f.getDescribe();
            if (d.getType() != Schema.DisplayType.REFERENCE || !d.isCustom()) {
                continue;
            }
            List<Schema.SObjectType> targets = d.getReferenceTo();
            if (targets.size() != 1 || targets[0] == MinuteX_Meeting__c.SObjectType) {
                continue;
            }
            String target = targets[0].getDescribe().getName();
            if (!lookupCache.containsKey(target)) {
                lookupCache.put(target, f);
            }
        }
        return lookupCache;
    }
    static Map<String, Schema.SObjectField> lookupCache;

    /**
     * One MinuteX Meeting Link per linked record (MinuteX contract 3.0), and
     * the meeting's Files shared to each. A Lead, Contact, Account or
     * Opportunity gets a lookup, so the meeting appears in its "MinuteX
     * Meetings" related list; any other object keeps the record id only (the
     * MinuteX Meetings component finds its meetings by that id).
     * A record that no longer exists here is skipped (a lookup to it would
     * fail the whole event). Unlinked records lose their row and the shares.
     */
    @TestVisible
    static List<MinuteX_Meeting_Link__c> syncLinks(MinuteX_Meeting__c meeting, String meetingId,
                                                   List<Object> links, String startedAt) {
        Map<String, MinuteX_Meeting_Link__c> rows = new Map<String, MinuteX_Meeting_Link__c>();
        Map<String, Set<Id>> idsByObject = new Map<String, Set<Id>>();
        for (Object o : links) {
            Map<String, Object> l = asMap(o);
            String raw = str(l.get('id'));
            if (!isRecordId(raw, '')) {
                continue;
            }
            Id recordId;
            String objectName;
            try {
                recordId = Id.valueOf(raw);
                objectName = recordId.getSobjectType().getDescribe().getName();
            } catch (Exception e) {
                continue;   // not an id in this org
            }
            String key = (meetingId + '|' + String.valueOf(recordId)).left(120);
            rows.put(key, new MinuteX_Meeting_Link__c(
                Link_Key__c = key,
                Name = String.isBlank(meeting.Name) ? 'MinuteX meeting' : meeting.Name.left(80),
                MinuteX_Meeting__c = meeting.Id,
                Record_Id__c = String.valueOf(recordId),
                Record_Name__c = blankToNull(str(l.get('name')).left(255)),
                Object__c = objectName.left(80),
                Primary__c = l.get('primary') == true,
                Summary__c = blankToNull(str(meeting.Summary__c).normalizeSpace().left(255)),
                Meeting_Date__c = meetingDate(startedAt)));
            if (lookupFields().containsKey(objectName)) {
                if (!idsByObject.containsKey(objectName)) {
                    idsByObject.put(objectName, new Set<Id>());
                }
                idsByObject.get(objectName).add(recordId);
            }
        }
        // Only records that exist here get their lookup set.
        Set<Id> live = new Set<Id>();
        for (String objectName : idsByObject.keySet()) {
            Set<Id> ids = idsByObject.get(objectName);
            for (SObject r : Database.query('SELECT Id FROM ' + objectName + ' WHERE Id IN :ids')) {
                live.add(r.Id);
            }
        }
        List<MinuteX_Meeting_Link__c> keep = new List<MinuteX_Meeting_Link__c>();
        for (MinuteX_Meeting_Link__c row : rows.values()) {
            Schema.SObjectField field = lookupFields().get(row.Object__c);
            if (field != null) {
                if (!live.contains(Id.valueOf(row.Record_Id__c))) {
                    continue;   // deleted, or not in this org
                }
                row.put(field, Id.valueOf(row.Record_Id__c));
            }
            keep.add(row);
        }
        if (!keep.isEmpty()) {
            upsert keep MinuteX_Meeting_Link__c.Link_Key__c;
        }
        Set<String> keys = new Set<String>();
        Set<Id> linked = new Set<Id>();
        for (MinuteX_Meeting_Link__c row : keep) {
            keys.add(row.Link_Key__c);
            linked.add(Id.valueOf(row.Record_Id__c));
        }
        List<MinuteX_Meeting_Link__c> gone = [
            SELECT Id, Record_Id__c FROM MinuteX_Meeting_Link__c
            WHERE MinuteX_Meeting__c = :meeting.Id AND Link_Key__c NOT IN :keys];
        Set<Id> unlinked = new Set<Id>();
        for (MinuteX_Meeting_Link__c g : gone) {
            if (isRecordId(g.Record_Id__c, '')) {
                unlinked.add(Id.valueOf(g.Record_Id__c));
            }
        }
        delete gone;
        shareFiles(meeting.Id, linked, unlinked);
        return keep;
    }

    /** The meeting's Files on each linked record's Files list (and off the
     *  records no longer linked). Best effort per record: an org that
     *  doesn't allow a share keeps the File on the MinuteX Meeting. */
    static void shareFiles(Id meetingRecordId, Set<Id> linked, Set<Id> unlinked) {
        Set<Id> documents = new Set<Id>();
        for (ContentDocumentLink l : [SELECT ContentDocumentId FROM ContentDocumentLink
                                      WHERE LinkedEntityId = :meetingRecordId]) {
            documents.add(l.ContentDocumentId);
        }
        if (documents.isEmpty()) {
            return;
        }
        if (!unlinked.isEmpty()) {
            Database.delete([SELECT Id FROM ContentDocumentLink
                             WHERE ContentDocumentId IN :documents AND LinkedEntityId IN :unlinked], false);
        }
        if (linked.isEmpty()) {
            return;
        }
        Set<String> have = new Set<String>();
        for (ContentDocumentLink l : [SELECT ContentDocumentId, LinkedEntityId FROM ContentDocumentLink
                                      WHERE ContentDocumentId IN :documents AND LinkedEntityId IN :linked]) {
            have.add(String.valueOf(l.ContentDocumentId) + String.valueOf(l.LinkedEntityId));
        }
        List<ContentDocumentLink> add = new List<ContentDocumentLink>();
        for (Id doc : documents) {
            for (Id recordId : linked) {
                if (!have.contains(String.valueOf(doc) + String.valueOf(recordId))) {
                    add.add(new ContentDocumentLink(ContentDocumentId = doc, LinkedEntityId = recordId,
                                                    ShareType = 'V', Visibility = 'AllUsers'));
                }
            }
        }
        for (Database.SaveResult r : Database.insert(add, false)) {
            if (!r.isSuccess()) {
                System.debug(LoggingLevel.WARN, 'MinuteX: file not shared: ' + r.getErrors()[0].getMessage());
            }
        }
    }

    /** Part i of the transcript for one Transcript field ('' when none). */
    static String transcriptPart(List<Object> parts, Integer i) {
        return i < parts.size() ? str(parts[i]).left(131072) : '';
    }

    /** The transcript now lives in the Transcript fields: delete the .txt
     *  File an older MinuteX sent for this meeting (MinuteX's own File, found
     *  by its tag). Best effort: a File that can't be deleted stays. */
    static void removeTranscriptFile(String meetingId) {
        String tag = ('MinuteX file ' + meetingId + ':transcript').left(255);
        Set<Id> documents = new Set<Id>();
        for (ContentVersion v : [SELECT ContentDocumentId FROM ContentVersion
                                 WHERE Description = :tag AND IsLatest = true]) {
            documents.add(v.ContentDocumentId);
        }
        if (!documents.isEmpty()) {
            Database.delete(new List<Id>(documents), false);
        }
    }

    /** "2026-10-08T05:12:00Z" -> a Datetime (GMT); null when unreadable. */
    @TestVisible
    static Datetime meetingDate(String iso) {
        if (String.isBlank(iso) || iso.length() < 19) {
            return null;
        }
        try {
            return Datetime.valueOfGmt(iso.left(19).replace('T', ' '));
        } catch (Exception e) {
            return null;
        }
    }

    // -- tasks ---------------------------------------------------------------

    /** Objects a Task's Related To can point at in this org (the ones with
     *  Allow Activities on). The Setup page reads it too. */
    public static Set<String> relatable() {
        Set<String> out = new Set<String>();
        for (Schema.SObjectType t : Schema.SObjectType.Task.fields.WhatId.getReferenceTo()) {
            out.add(t.getDescribe().getName());
        }
        return out;
    }

    /** The customer's record a task goes to: the one it was pointed at in
     *  MinuteX (when that record is linked), else the main record; null
     *  when the meeting has no linked record. */
    @TestVisible
    static Id taskTarget(Map<String, Object> item, List<MinuteX_Meeting_Link__c> links) {
        Id primary;
        Set<Id> ids = new Set<Id>();
        for (MinuteX_Meeting_Link__c l : links) {
            Id r = Id.valueOf(l.Record_Id__c);
            ids.add(r);
            if (l.Primary__c == true) {
                primary = r;
            }
        }
        String raw = str(asMap(item.get('related')).get('id'));
        if (isRecordId(raw, '')) {
            Id chosen = Id.valueOf(raw);   // 15 or 18 characters: Id compares either
            if (ids.contains(chosen)) {
                return chosen;
            }
        }
        return primary;
    }

    static void syncTasks(Id meetingRecordId, String meetingId, List<Object> items, List<Person> people,
                          Id meetingContactId, List<MinuteX_Meeting_Link__c> links) {
        Set<String> canRelate = links.isEmpty() ? new Set<String>() : relatable();
        Map<String, Person> bySpeaker = new Map<String, Person>();
        for (Person x : people) {
            if (String.isNotBlank(x.speakerId)) {
                bySpeaker.put(x.speakerId, x);
            }
        }
        // Assignee emails not already answered by a tagged speaker.
        Map<String, String> nameByEmail = new Map<String, String>();
        Set<String> keys = new Set<String>();
        for (Object o : items) {
            Map<String, Object> item = asMap(o);
            String taskKey = str(item.get('id')).left(64);
            if (taskKey == '') {
                continue;
            }
            keys.add(taskKey);
            Map<String, Object> assignee = asMap(item.get('assignee'));
            Person spoken = bySpeaker.get(str(assignee.get('speaker_id')));
            String email = cleanEmail(str(assignee.get('email')));
            if ((spoken == null || (spoken.userId == null && spoken.contactId == null)) && email != '') {
                nameByEmail.put(email, str(assignee.get('name')));
            }
        }
        Map<String, Id> users = usersByEmail(nameByEmail.keySet());
        Map<String, String> needContact = new Map<String, String>();
        for (String email : nameByEmail.keySet()) {
            if (!users.containsKey(email)) {
                needContact.put(email, nameByEmail.get(email));
            }
        }
        Map<String, Id> contacts = contactsByEmail(needContact, createContacts);

        Map<String, Task> existing = new Map<String, Task>();
        for (Task t : [SELECT Id, MinuteX_Task_Id__c FROM Task WHERE MinuteX_Task_Id__c IN :keys]) {
            existing.put(t.MinuteX_Task_Id__c, t);
        }
        List<Task> inserts = new List<Task>();
        List<Task> updates = new List<Task>();
        for (Object o : items) {
            Map<String, Object> item = asMap(o);
            String taskKey = str(item.get('id')).left(64);
            if (taskKey == '') {
                continue;
            }
            Map<String, Object> assignee = asMap(item.get('assignee'));
            String assigneeName = str(assignee.get('name'));
            String email = cleanEmail(str(assignee.get('email')));
            Person spoken = bySpeaker.get(str(assignee.get('speaker_id')));
            Id ownerId;
            Id whoId;
            if (spoken != null && spoken.userId != null) {
                ownerId = spoken.userId;                    // 1. a Salesforce User
            } else if (email != '' && users.containsKey(email)) {
                ownerId = users.get(email);
            } else if (spoken != null && spoken.contactId != null) {
                whoId = spoken.contactId;                   // 2. a Salesforce Contact
            } else if (email != '' && contacts.containsKey(email)) {
                whoId = contacts.get(email);
            }                                               // 3. the name only
            if (whoId == null) {
                whoId = meetingContactId;
            }
            // On the customer's record. A Lead or Contact is the Task's Name
            // (a Lead can't have a Related To); any other record that takes
            // activities is its Related To. No linked record: the meeting.
            Boolean assigneeFound = ownerId != null || whoId != null;
            Id whatId = meetingRecordId;
            Id target = taskTarget(item, links);
            if (target != null) {
                String kind = target.getSobjectType().getDescribe().getName();
                if (kind == 'Lead') {
                    whoId = target;
                    whatId = null;
                } else if (kind == 'Contact') {
                    whoId = target;
                } else if (canRelate.contains(kind)) {
                    whatId = target;
                }
            }
            String description = str(item.get('description'));
            if (!assigneeFound && assigneeName != '') {
                description = ('Assignee: ' + assigneeName + '\n\n' + description).trim();
            }
            Task t = new Task(
                Subject = String.isBlank(str(item.get('title'))) ? 'MinuteX task' : str(item.get('title')).left(255),
                Description = description.left(32000),
                ActivityDate = dueDate(str(item.get('due_date_normalized'))),
                Priority = priority(str(item.get('priority'))),
                Status = status(str(item.get('status'))),
                WhatId = whatId,
                WhoId = whoId,
                MinuteX_Task_Id__c = taskKey,
                MinuteX_Meeting_Id__c = meetingId.left(40),
                MinuteX_Assignee_Name__c = blankToNull(assigneeName.left(255)));
            if (ownerId != null) {
                t.OwnerId = ownerId;
            }
            if (existing.containsKey(taskKey)) {
                t.Id = existing.get(taskKey).Id;
                updates.add(t);
            } else {
                inserts.add(t);
            }
        }
        insert inserts;
        update updates;
        // A task that is no longer in the meeting (deleted or cancelled in
        // MinuteX) is set aside, never deleted.
        List<Task> gone = [
            SELECT Id FROM Task
            WHERE (MinuteX_Meeting_Id__c = :meetingId OR WhatId = :meetingRecordId)
              AND MinuteX_Task_Id__c != null
              AND MinuteX_Task_Id__c NOT IN :keys AND IsClosed = false AND Status != 'Deferred'
        ];
        for (Task g : gone) {
            g.Status = 'Deferred';
        }
        update gone;
    }

    @TestVisible
    static String status(String minutexStatus) {
        String s = minutexStatus.toLowerCase();
        if (s == 'completed' || s == 'done') {
            return 'Completed';
        }
        if (s == 'in progress') {
            return 'In Progress';
        }
        if (s == 'cancelled' || s == 'canceled') {
            return 'Deferred';
        }
        return 'Not Started';
    }

    @TestVisible
    static String priority(String minutexPriority) {
        String p = minutexPriority.toLowerCase();
        return p == 'high' ? 'High' : p == 'low' ? 'Low' : 'Normal';
    }

    static Date dueDate(String iso) {
        if (String.isBlank(iso) || iso.length() < 10) {
            return null;
        }
        try {
            return Date.valueOf(iso.left(10));
        } catch (Exception e) {
            return null;
        }
    }

    // -- lookups by email ------------------------------------------------------

    static Map<String, Id> usersByEmail(Set<String> emails) {
        Map<String, Id> out = new Map<String, Id>();
        if (emails.isEmpty()) {
            return out;
        }
        for (User u : [SELECT Id, Email FROM User
                       WHERE Email IN :emails AND IsActive = true AND UserType = 'Standard']) {
            out.put(u.Email.toLowerCase(), u.Id);
        }
        return out;
    }

    /** Existing Contacts by email and, when `create`, a new Contact for each
     *  email that has none (one per email — never a duplicate). */
    static Map<String, Id> contactsByEmail(Map<String, String> nameByEmail, Boolean create) {
        Map<String, Id> out = new Map<String, Id>();
        if (nameByEmail.isEmpty()) {
            return out;
        }
        Set<String> emails = nameByEmail.keySet();
        for (Contact c : [SELECT Id, Email FROM Contact WHERE Email IN :emails ORDER BY CreatedDate ASC]) {
            String e = c.Email.toLowerCase();
            if (!out.containsKey(e)) {
                out.put(e, c.Id);
            }
        }
        if (!create) {
            return out;
        }
        Map<String, Contact> fresh = new Map<String, Contact>();
        for (String email : emails) {
            if (!out.containsKey(email)) {
                fresh.put(email, newContact(nameByEmail.get(email), email));
            }
        }
        insert fresh.values();
        for (String email : fresh.keySet()) {
            out.put(email, fresh.get(email).Id);
        }
        return out;
    }

    @TestVisible
    static Contact newContact(String fullName, String email) {
        String name = fullName == null ? '' : fullName.trim();
        if (name == '') {
            name = email.substringBefore('@');
        }
        Integer space = name.lastIndexOf(' ');
        Contact c = new Contact(Email = email, Description = 'Created by MinuteX from a meeting.');
        if (space > 0) {
            c.FirstName = name.left(space).left(40);
            c.LastName = name.substring(space + 1).left(80);
        } else {
            c.LastName = name.left(80);
        }
        return c;
    }

    // -- helpers -------------------------------------------------------------

    // -- files ---------------------------------------------------------------

    /**
     * One meeting file (POST .../minutex/v2/files), sent BEFORE the meeting's
     * event. Stored as a Salesforce File on the MinuteX Meeting (created
     * empty if the event hasn't arrived yet) and shared to the meeting's main
     * record when that's possible. The file's MinuteX key is kept in the
     * File's Description, so a changed file becomes a new version of the
     * same File instead of a duplicate.
     */
    public static Map<String, Object> applyFile(Map<String, Object> req) {
        String meetingId = str(req.get('meeting_id'));
        String key = str(req.get('key'));
        String filename = str(req.get('filename')).left(255);
        if (String.isBlank(meetingId) || meetingId.length() > 40 || String.isBlank(key)
                || key.length() > 200 || String.isBlank(filename)) {
            SyncException e = new SyncException('Not a valid MinuteX file.');
            e.code = 'invalid_file';
            throw e;
        }
        Blob content = EncodingUtil.base64Decode(str(req.get('content_base64')));
        String digest = EncodingUtil.convertToHex(Crypto.generateDigest('SHA-256', content));
        if (digest != str(req.get('sha256')).toLowerCase()) {
            SyncException e = new SyncException('The file was damaged on the way. MinuteX will send it again.');
            e.code = 'hash_mismatch';
            throw e;
        }

        List<MinuteX_Meeting__c> found = [
            SELECT Id, Record_Id__c FROM MinuteX_Meeting__c WHERE MinuteX_Meeting_Id__c = :meetingId LIMIT 1];
        MinuteX_Meeting__c meeting;
        if (found.isEmpty()) {
            // The meeting's event follows this file; it fills the rest in.
            meeting = new MinuteX_Meeting__c(MinuteX_Meeting_Id__c = meetingId, Name = 'MinuteX meeting');
            insert meeting;
        } else {
            meeting = found[0];
        }

        String tag = ('MinuteX file ' + key).left(255);
        List<ContentVersion> existing = [
            SELECT ContentDocumentId, Checksum FROM ContentVersion
            WHERE Description = :tag AND IsLatest = true LIMIT 1];
        ContentVersion v = new ContentVersion(
            Title = filename, PathOnClient = filename, VersionData = content, Description = tag);
        if (existing.isEmpty()) {
            v.FirstPublishLocationId = meeting.Id;
        } else {
            v.ContentDocumentId = existing[0].ContentDocumentId;
        }
        insert v;
        Id documentId = [SELECT ContentDocumentId FROM ContentVersion WHERE Id = :v.Id].ContentDocumentId;

        // Also on the main record's Files list, when this org allows it.
        if (isRecordId(meeting.Record_Id__c, '')) {
            try {
                Id recordId = Id.valueOf(meeting.Record_Id__c);
                if ([SELECT COUNT() FROM ContentDocumentLink
                     WHERE ContentDocumentId = :documentId AND LinkedEntityId = :recordId] == 0) {
                    insert new ContentDocumentLink(ContentDocumentId = documentId,
                        LinkedEntityId = recordId, ShareType = 'V', Visibility = 'AllUsers');
                }
            } catch (Exception e) {
                System.debug(LoggingLevel.WARN, 'MinuteX: file not shared to the main record: ' + e.getMessage());
            }
        }
        return new Map<String, Object>{'ok' => true, 'content_document_id' => String.valueOf(documentId)};
    }

    static Map<String, Object> result(Id recordId) {
        return new Map<String, Object>{
            'record_id' => String.valueOf(recordId),
            'url' => System.Url.getOrgDomainUrl().toExternalForm() + '/' + recordId
        };
    }

    static String cleanEmail(String raw) {
        String e = raw == null ? '' : raw.trim().toLowerCase();
        return e.length() <= 80 && EMAIL.matcher(e).matches() ? e : '';
    }

    static Boolean isRecordId(String value, String prefix) {
        return value != null && value.startsWith(prefix) && (value.length() == 15 || value.length() == 18)
            && value.isAlphanumeric();
    }

    static Map<String, Object> asMap(Object v) {
        return v instanceof Map<String, Object> ? (Map<String, Object>) v : new Map<String, Object>();
    }

    static List<Object> asList(Object v) {
        return v instanceof List<Object> ? (List<Object>) v : new List<Object>();
    }

    static String str(Object v) {
        return v == null ? '' : String.valueOf(v).trim();
    }

    static String blankToNull(String v) {
        return String.isBlank(v) ? null : v;
    }

    static Integer intOf(Object v) {
        if (v instanceof Integer) {
            return (Integer) v;
        }
        if (v instanceof Long) {
            return ((Long) v).intValue();
        }
        if (v instanceof Decimal) {
            return ((Decimal) v).intValue();
        }
        return 0;
    }
}
