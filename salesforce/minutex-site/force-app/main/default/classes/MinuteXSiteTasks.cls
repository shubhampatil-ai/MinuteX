/**
 * create_task / update_task: one Salesforce Task, related to the record a
 * person explicitly selected, assigned to the Salesforce User they chose.
 *
 * PERMISSIONS ARE THE INTEGRATION USER'S (MinuteXSitePermissions): Task
 * create/update ("Edit Tasks"), each field's field-level security, the
 * related object's read access, the related record's visibility under
 * sharing, and the assignee being an active User. Only when all of them pass
 * does the receiver write — in system mode, because the Site's guest user can
 * write nothing itself. Org validation rules and triggers still run on that
 * write.
 *
 * RELATIONSHIP. The related object must be one Task.WhatId or Task.WhoId can
 * point at, per this org's schema; the record id must belong to that object
 * and be visible to the user. A mapping that requires a record never produces
 * an unrelated Task.
 *
 * WHO AND WHAT (contract 1.2). `related_records` carries up to one record for
 * each field: a person (Contact/Lead -> WhoId) and a business record
 * (Account, Opportunity, custom... -> WhatId). Which field an object uses is
 * this org's Task schema, never its name. When present it IS the Task's
 * relationship (an update clears the field it does not name); when absent the
 * single related_object_type/related_record_id pair is used, as in 1.1.
 * Salesforce's own rule is enforced up front: a Task whose Who is a Lead
 * cannot have a What.
 *
 * IDEMPOTENT per idempotency_key (see MinuteXSiteIdempotency).
 */
public without sharing class MinuteXSiteTasks {
    public static final String DENIED_CREATE =
        'Salesforce permissions do not allow Task creation for this object/user.';
    public static final String DENIED_UPDATE =
        'Salesforce permissions do not allow Task updates for this user.';
    static final Pattern ISO_DATE = Pattern.compile('[0-9]{4}-[0-9]{2}-[0-9]{2}');
    // Fields this service sets itself; extra_fields may not name them.
    static final Set<String> MANAGED = new Set<String>{
        'subject', 'description', 'activitydate', 'status', 'priority', 'ownerid', 'whatid', 'whoid'
    };
    static final Set<String> EXTRA_TYPES = new Set<String>{
        'string', 'textarea', 'picklist', 'boolean', 'date', 'double', 'integer', 'percent',
        'currency', 'email', 'phone', 'url'
    };

    public static Map<String, Object> createTask(Map<String, Object> envelope) {
        String key = MinuteXSiteIdempotency.requireKey(envelope);
        Map<String, Object> replayed = MinuteXSiteIdempotency.replay(key);
        if (replayed != null) {
            replayed.put('duplicate', true);
            return replayed;
        }
        if (!perms().obj('Task').create) {
            throw MinuteXSiteException.of(403, 'permission_denied', DENIED_CREATE);
        }
        Map<String, Object> spec = taskSpec(envelope);
        Task t = new Task();

        String relType = MinuteXSiteReceiver.str(spec.get('related_object_type'));
        String relId = MinuteXSiteReceiver.str(spec.get('related_record_id'));
        List<Object> relations = MinuteXSiteReceiver.optList(spec.get('related_records'),
            'payload.task.related_records');
        if (!relations.isEmpty()) {
            relateAll(t, relations, true);
        } else if (String.isNotBlank(relType) || String.isNotBlank(relId)) {
            relate(t, relType, relId, true);
        } else if (spec.get('record_required') != false) {
            throw MinuteXSiteException.forField(422, 'record_required', 'payload.task.related_record_id',
                'This Task must be related to a Salesforce record.');
        }
        if (String.isBlank(MinuteXSiteReceiver.str(spec.get('subject')))) {
            throw MinuteXSiteException.forField(422, 'validation_failed', 'payload.task.subject',
                'payload.task.subject is required.');
        }
        apply(t, spec, true);
        return save(envelope, key, t, true);
    }

    public static Map<String, Object> updateTask(Map<String, Object> envelope) {
        String key = MinuteXSiteIdempotency.requireKey(envelope);
        Map<String, Object> replayed = MinuteXSiteIdempotency.replay(key);
        if (replayed != null) {
            replayed.put('duplicate', true);
            return replayed;
        }
        if (!perms().obj('Task').edit) {
            throw MinuteXSiteException.of(403, 'permission_denied', DENIED_UPDATE);
        }
        Map<String, Object> spec = taskSpec(envelope);
        Id taskId = idOf(MinuteXSiteReceiver.str(spec.get('salesforce_task_id')), Task.SObjectType,
            'invalid_task', 'payload.task.salesforce_task_id');
        List<Task> existing = Database.queryWithBinds('SELECT Id FROM Task WHERE Id = :tid LIMIT 1',
            new Map<String, Object>{ 'tid' => taskId }, AccessLevel.SYSTEM_MODE);
        if (existing.isEmpty() || !perms().canRead(taskId)) {
            throw MinuteXSiteException.forField(404, 'task_not_found', 'payload.task.salesforce_task_id',
                'The Task does not exist or is not visible to this user.');
        }
        Task t = new Task(Id = taskId);
        List<Object> relations = MinuteXSiteReceiver.optList(spec.get('related_records'),
            'payload.task.related_records');
        if (!relations.isEmpty()) {
            relateAll(t, relations, false);
        } else if (spec.containsKey('related_object_type') || spec.containsKey('related_record_id')) {
            relate(t, MinuteXSiteReceiver.str(spec.get('related_object_type')),
                MinuteXSiteReceiver.str(spec.get('related_record_id')), false);
        }
        apply(t, spec, false);
        return save(envelope, key, t, false);
    }

    static MinuteXSitePermissions perms() {
        return MinuteXSitePermissions.forIntegrationUser();
    }

    static Map<String, Object> taskSpec(Map<String, Object> envelope) {
        Map<String, Object> payload = MinuteXSiteReceiver.obj(envelope.get('payload'), 'payload');
        return MinuteXSiteReceiver.obj(payload.get('task'), 'payload.task');
    }

    static Map<String, Object> save(Map<String, Object> envelope, String key, Task t, Boolean creating) {
        Savepoint sp = Database.setSavepoint();
        if (creating) {
            insert t;
        } else {
            update t;
        }
        Map<String, Object> result = new Map<String, Object>{
            'operation' => creating ? 'created' : 'updated',
            'task_id' => String.valueOf(t.Id),
            // Salesforce's stamp for THIS write. MinuteX keeps it so a future
            // Salesforce -> MinuteX sync can recognise its own echo.
            'last_modified_at' => lastModified(t.Id)
        };
        try {
            MinuteXSiteIdempotency.record(key, MinuteXSiteReceiver.str(envelope.get('request_id')),
                creating ? 'create_task' : 'update_task', String.valueOf(t.Id),
                creating ? 'created' : 'updated', result);
        } catch (DmlException e) {
            if (e.getDmlType(0) != StatusCode.DUPLICATE_VALUE) {
                throw e;
            }
            // A concurrent request with this key committed first: undo ours,
            // answer with theirs. Exactly one Task exists for the key.
            Database.rollback(sp);
            Map<String, Object> winner = MinuteXSiteIdempotency.replay(key);
            winner.put('duplicate', true);
            return winner;
        }
        return result;
    }

    static String lastModified(Id taskId) {
        List<Task> rows = Database.queryWithBinds(
            'SELECT SystemModstamp FROM Task WHERE Id = :tid LIMIT 1',
            new Map<String, Object>{ 'tid' => taskId }, AccessLevel.SYSTEM_MODE);
        return rows.isEmpty() || rows[0].SystemModstamp == null
            ? '' : rows[0].SystemModstamp.formatGmt('yyyy-MM-dd\'T\'HH:mm:ss.SSS\'Z\'');
    }

    // ---- relationship ---------------------------------------------------------

    /** related_records: at most one record per relationship field. */
    static void relateAll(Task t, List<Object> relations, Boolean creating) {
        if (relations.size() > 2) {
            throw MinuteXSiteException.forField(422, 'invalid_relationship', 'payload.task.related_records',
                'A Task relates to at most one person and one record.');
        }
        Set<SObjectField> used = new Set<SObjectField>();
        Boolean whoIsLead = false;
        for (Object raw : relations) {
            Map<String, Object> r = MinuteXSiteReceiver.obj(raw, 'payload.task.related_records[]');
            SObjectField target = relate(t, MinuteXSiteReceiver.str(r.get('object_type')),
                MinuteXSiteReceiver.str(r.get('record_id')), creating);
            if (used.contains(target)) {
                throw MinuteXSiteException.forField(422, 'invalid_relationship', 'payload.task.related_records',
                    'A Task has one ' + (target == Task.WhoId ? 'Name (Who)' : 'Related To (What)')
                    + ' field; two records were sent for it.');
            }
            used.add(target);
            if (target == Task.WhoId) {
                whoIsLead = ((Id) t.get(Task.WhoId)).getSObjectType() == Lead.SObjectType;
            }
        }
        if (whoIsLead && used.contains(Task.WhatId)) {
            throw MinuteXSiteException.forField(422, 'invalid_relationship', 'payload.task.related_records',
                'A Task related to a Lead cannot also be related to another record.');
        }
        // related_records IS the relationship: on an update, the field it
        // does not name is cleared rather than left pointing at the old record.
        if (!creating) {
            for (SObjectField f : new List<SObjectField>{ Task.WhoId, Task.WhatId }) {
                if (!used.contains(f)) {
                    requireFieldAccess(f, creating);
                    t.put(f, null);
                }
            }
        }
    }

    static SObjectField relate(Task t, String relType, String relId, Boolean creating) {
        Map<String, SObjectType> gd = Schema.getGlobalDescribe();
        SObjectType st = String.isBlank(relType) ? null : gd.get(relType.toLowerCase());
        if (st == null) {
            throw MinuteXSiteException.forField(422, 'invalid_object', 'payload.task.related_object_type',
                'This Salesforce org has no object named ' + relType.left(80) + '.');
        }
        DescribeSObjectResult rd = st.getDescribe();
        Id recordId = idOf(relId, st, 'invalid_record', 'payload.task.related_record_id');

        SObjectField target;
        if (Task.WhatId.getDescribe().getReferenceTo().contains(st)) {
            target = Task.WhatId;
        } else if (Task.WhoId.getDescribe().getReferenceTo().contains(st)) {
            target = Task.WhoId;
        } else {
            throw MinuteXSiteException.forField(422, 'object_not_task_relatable', 'payload.task.related_object_type',
                'Tasks cannot be related to ' + rd.getLabelPlural() + ' in this org (activities are not enabled for it).');
        }
        if (!perms().obj(rd.getName()).read) {
            throw MinuteXSiteException.of(403, 'permission_denied',
                'Salesforce permissions do not allow reading ' + rd.getLabelPlural() + ' for the integration user.');
        }
        requireFieldAccess(target, creating);
        if (!MinuteXSiteRecords.isVisible(rd, recordId, perms())) {
            throw MinuteXSiteException.forField(422, 'invalid_record', 'payload.task.related_record_id',
                'The ' + rd.getLabel() + ' record does not exist or is not visible to the integration user.');
        }
        t.put(target, recordId);
        return target;
    }

    static Id idOf(String raw, SObjectType expected, String code, String field) {
        Id value;
        try {
            value = Id.valueOf(raw);
        } catch (Exception e) {
            throw MinuteXSiteException.forField(422, code, field, field + ' is not a Salesforce record id.');
        }
        if (value.getSObjectType() != expected) {
            throw MinuteXSiteException.forField(422, code, field,
                field + ' is not a ' + expected.getDescribe().getLabel() + ' id.');
        }
        return value;
    }

    // ---- fields ---------------------------------------------------------------

    static void apply(Task t, Map<String, Object> spec, Boolean creating) {
        if (spec.containsKey('subject')) {
            setText(t, Task.Subject, spec.get('subject'), 255, creating, 'payload.task.subject');
        }
        if (spec.containsKey('description')) {
            setText(t, Task.Description, spec.get('description'), 32000, creating, 'payload.task.description');
        }
        if (spec.containsKey('due_date')) {
            String due = MinuteXSiteReceiver.str(spec.get('due_date'));
            requireFieldAccess(Task.ActivityDate, creating);
            if (String.isBlank(due)) {
                t.ActivityDate = null;
            } else if (!ISO_DATE.matcher(due).matches()) {
                throw MinuteXSiteException.forField(422, 'validation_failed', 'payload.task.due_date',
                    'payload.task.due_date must be YYYY-MM-DD.');
            } else {
                try {
                    t.ActivityDate = Date.valueOf(due);
                } catch (Exception e) {
                    throw MinuteXSiteException.forField(422, 'validation_failed', 'payload.task.due_date',
                        'payload.task.due_date is not a real date.');
                }
            }
        }
        setPicklist(t, Task.Status, spec, 'status', creating);
        setPicklist(t, Task.Priority, spec, 'priority', creating);
        if (creating || spec.containsKey('assigned_user_id')) {
            assign(t, MinuteXSiteReceiver.str(spec.get('assigned_user_id')), creating);
        }
        Map<String, Object> extra = MinuteXSiteReceiver.optObj(spec.get('extra_fields'), 'payload.task.extra_fields');
        for (String name : extra.keySet()) {
            setExtra(t, name, extra.get(name), creating);
        }
    }

    static void requireFieldAccess(SObjectField f, Boolean creating) {
        DescribeFieldResult d = f.getDescribe();
        if (!MinuteXSiteSchema.writableByNature('Task', d) || !perms().fieldEditable('Task', d)) {
            throw MinuteXSiteException.of(403, 'permission_denied',
                'Salesforce permissions do not allow setting Task.' + d.getName() + ' for the integration user.');
        }
    }

    static void setText(Task t, SObjectField f, Object raw, Integer max, Boolean creating, String field) {
        requireFieldAccess(f, creating);
        String v = MinuteXSiteReceiver.str(raw);
        if (v.length() > max) {
            throw MinuteXSiteException.forField(422, 'field_too_large', field,
                field + ' is ' + v.length() + ' characters; the maximum is ' + max + '.');
        }
        t.put(f, String.isBlank(v) ? null : v);
    }

    static void setPicklist(Task t, SObjectField f, Map<String, Object> spec, String key, Boolean creating) {
        if (!spec.containsKey(key) || String.isBlank(MinuteXSiteReceiver.str(spec.get(key)))) {
            return; // Salesforce applies the picklist default on create
        }
        requireFieldAccess(f, creating);
        String v = MinuteXSiteReceiver.str(spec.get(key));
        if (!activeValues(f.getDescribe()).contains(v)) {
            throw MinuteXSiteException.forField(422, 'invalid_' + key, 'payload.task.' + key,
                v.left(40) + ' is not an active Task ' + key + ' value in this org.');
        }
        t.put(f, v);
    }

    static Set<String> activeValues(DescribeFieldResult d) {
        Set<String> values = new Set<String>();
        for (Schema.PicklistEntry p : d.getPicklistValues()) {
            if (p.isActive()) {
                values.add(p.getValue());
            }
        }
        return values;
    }

    static void assign(Task t, String raw, Boolean creating) {
        if (String.isBlank(raw)) {
            if (creating) {
                // The running user is the Site guest, who cannot own records:
                // an unassigned Task belongs to the integration user.
                t.OwnerId = perms().userId;
                return;
            }
            throw MinuteXSiteException.forField(422, 'invalid_assignee', 'payload.task.assigned_user_id',
                'A Task must keep an assignee.');
        }
        Id userId = idOf(raw, User.SObjectType, 'invalid_assignee', 'payload.task.assigned_user_id');
        requireFieldAccess(Task.OwnerId, creating);
        List<User> users = Database.queryWithBinds('SELECT Id, IsActive FROM User WHERE Id = :uid LIMIT 1',
            new Map<String, Object>{ 'uid' => userId }, AccessLevel.SYSTEM_MODE);
        if (users.isEmpty() || !users[0].IsActive) {
            throw MinuteXSiteException.forField(422, 'invalid_assignee', 'payload.task.assigned_user_id',
                'The assignee is not an active Salesforce User.');
        }
        t.OwnerId = userId;
    }

    static void setExtra(Task t, String name, Object raw, Boolean creating) {
        String field = 'payload.task.extra_fields.' + name.left(80);
        SObjectField f = Task.SObjectType.getDescribe().fields.getMap().get(name.toLowerCase());
        if (f == null || MANAGED.contains(name.toLowerCase())) {
            throw MinuteXSiteException.forField(422, 'invalid_field', field,
                'Task has no settable field named ' + name.left(80) + '.');
        }
        DescribeFieldResult d = f.getDescribe();
        String type = String.valueOf(d.getType()).toLowerCase();
        if (!perms().fieldReadable('Task', d) || !EXTRA_TYPES.contains(type)) {
            throw MinuteXSiteException.forField(422, 'invalid_field', field,
                'Task.' + d.getName() + ' cannot be set by MinuteX.');
        }
        requireFieldAccess(f, creating);
        if (raw == null) {
            t.put(f, null);
            return;
        }
        try {
            if (type == 'boolean') {
                t.put(f, raw == true);
            } else if (type == 'date') {
                t.put(f, Date.valueOf(MinuteXSiteReceiver.str(raw)));
            } else if (type == 'double' || type == 'percent' || type == 'currency') {
                t.put(f, Decimal.valueOf(MinuteXSiteReceiver.str(raw)));
            } else if (type == 'integer') {
                t.put(f, Integer.valueOf(MinuteXSiteReceiver.str(raw)));
            } else {
                String v = MinuteXSiteReceiver.str(raw);
                if (type == 'picklist' && !activeValues(d).contains(v)) {
                    throw MinuteXSiteException.forField(422, 'invalid_field', field,
                        v.left(40) + ' is not an active value of Task.' + d.getName() + '.');
                }
                if (v.length() > d.getLength() && d.getLength() > 0) {
                    throw MinuteXSiteException.forField(422, 'field_too_large', field,
                        'Task.' + d.getName() + ' allows at most ' + d.getLength() + ' characters.');
                }
                t.put(f, v);
            }
        } catch (MinuteXSiteException e) {
            throw e;
        } catch (Exception e) {
            throw MinuteXSiteException.forField(422, 'invalid_field', field,
                'The value is not valid for Task.' + d.getName() + ' (' + type + ').');
        }
    }
}
