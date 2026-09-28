/**
 * org_info and object_schema: what this org contains, as the configured
 * INTEGRATION USER may use it (see MinuteXSitePermissions).
 *
 * Object and field API names, labels, types, references and picklist values
 * come from this org's own describe; nothing is inferred by MinuteX. Every
 * permission flag is the integration user's: an object it cannot read is not
 * returned at all (only counted), and a field it cannot read is not returned
 * (only counted).
 *
 * Describe flags such as isCreateable() are NOT used: through a Site they
 * answer for the guest user. Field writability is the field's own nature
 * (not a formula, not an auto-number, not a system/audit field, not a
 * compound person name) combined with the integration user's field-level
 * security and object permissions.
 */
public without sharing class MinuteXSiteSchema {
    public static final Integer DEFAULT_OBJECT_PAGE = 200;
    public static final Integer MAX_OBJECT_PAGE = 250;
    public static final Integer MAX_FIELD_OBJECTS = 10;
    public static final Integer MAX_PICKLIST_VALUES = 200;
    static final Set<String> SYSTEM_FIELDS = new Set<String>{
        'id', 'isdeleted', 'createddate', 'createdbyid', 'lastmodifieddate', 'lastmodifiedbyid',
        'systemmodstamp', 'lastactivitydate', 'lastvieweddate', 'lastreferenceddate', 'masterrecordid'
    };
    static final Set<String> COMPOUND_NAME_OBJECTS = new Set<String>{ 'contact', 'lead', 'user' };

    // ---- org_info -----------------------------------------------------------

    public static Map<String, Object> orgInfo() {
        MinuteXSitePermissions p = MinuteXSitePermissions.forIntegrationUser();
        Map<String, Object> org = new Map<String, Object>{
            'org_id' => UserInfo.getOrganizationId(),
            'org_name' => UserInfo.getOrganizationName()
        };
        try {
            Organization o = [SELECT IsSandbox, OrganizationType, InstanceName FROM Organization LIMIT 1];
            org.put('is_sandbox', o.IsSandbox);
            org.put('edition', o.OrganizationType);
            org.put('instance_name', o.InstanceName);
        } catch (Exception e) {
            // Identity above suffices.
        }
        MinuteXSitePermissions.ObjectAccess taskAccess = p.obj('Task');
        return new Map<String, Object>{
            'org' => org,
            'user' => new Map<String, Object>{
                'id' => String.valueOf(p.userId), 'username' => p.username, 'name' => p.name
            },
            'task' => new Map<String, Object>{
                'accessible' => taskAccess.read,
                'createable' => taskAccess.create,
                'updateable' => taskAccess.edit,
                // What a Task may relate to, per this org's own Task schema.
                'what_objects' => referenceNames(Task.WhatId.getDescribe()),
                'who_objects' => referenceNames(Task.WhoId.getDescribe())
            }
        };
    }

    public static List<String> referenceNames(DescribeFieldResult f) {
        List<String> names = new List<String>();
        for (SObjectType t : f.getReferenceTo()) {
            names.add(t.getDescribe(SObjectDescribeOptions.DEFERRED).getName());
        }
        names.sort();
        return names;
    }

    // ---- object_schema ------------------------------------------------------

    public static Map<String, Object> objectSchema(Map<String, Object> envelope) {
        Map<String, Object> payload = MinuteXSiteReceiver.optObj(envelope.get('payload'), 'payload');
        String mode = MinuteXSiteReceiver.str(payload.get('mode'));
        if (mode == 'objects') {
            return objects(payload);
        }
        if (mode == 'fields') {
            return fields(payload);
        }
        throw MinuteXSiteException.forField(400, 'invalid_request', 'payload.mode',
            'payload.mode must be objects or fields.');
    }

    /** One page of the objects the integration user can read, in API-name order. */
    static Map<String, Object> objects(Map<String, Object> payload) {
        MinuteXSitePermissions p = MinuteXSitePermissions.forIntegrationUser();
        Integer pageSize = boundedInt(payload.get('page_size'), DEFAULT_OBJECT_PAGE, 1, MAX_OBJECT_PAGE, 'payload.page_size');
        String cursor = MinuteXSiteReceiver.str(payload.get('cursor')).toLowerCase();

        Map<String, SObjectType> gd = Schema.getGlobalDescribe();
        List<String> names = new List<String>(gd.keySet());
        names.sort();

        List<Map<String, Object>> out = new List<Map<String, Object>>();
        Integer inaccessible = 0;
        Integer excluded = 0;
        Integer examined = 0;
        String last = '';
        Boolean more = false;
        for (String name : names) {
            // compareTo is the ordering List.sort() used, so a page boundary
            // can never skip or repeat an object.
            if (String.isNotBlank(cursor) && name.compareTo(cursor) <= 0) {
                continue;
            }
            if (examined == pageSize) {
                more = true;
                break;
            }
            examined++;
            last = name;
            DescribeSObjectResult d = gd.get(name).getDescribe(SObjectDescribeOptions.DEFERRED);
            if (isExcluded(d, gd)) {
                excluded++;
                continue;
            }
            MinuteXSitePermissions.ObjectAccess a = p.obj(d.getName());
            if (!a.read) {
                inaccessible++;
                continue;
            }
            out.add(new Map<String, Object>{
                'api_name' => d.getName(),
                'label' => d.getLabel(),
                'label_plural' => d.getLabelPlural(),
                'key_prefix' => d.getKeyPrefix(),
                'is_custom' => d.isCustom(),
                // Object property: do its records appear in "Recently
                // Viewed"? True for business record objects, false for
                // platform machinery (events, config, logs). MinuteX uses it
                // to keep technical STANDARD objects out of its catalog.
                'mru_enabled' => d.isMruEnabled(),
                'queryable' => a.read,
                'searchable' => a.read,
                'createable' => a.create,
                'updateable' => a.edit,
                'deletable' => a.del
            });
        }
        return new Map<String, Object>{
            'objects' => out,
            'next_cursor' => more ? last : '',
            'done' => !more,
            'skipped_inaccessible' => inaccessible,
            'skipped_excluded' => excluded
        };
    }

    /** Plumbing objects no one maps a Task to: settings, metadata, events,
     *  and the Share/History/Feed companions of other objects. */
    @TestVisible
    static Boolean isExcluded(DescribeSObjectResult d, Map<String, SObjectType> gd) {
        String name = d.getName();
        if (d.isCustomSetting() || d.isDeprecatedAndHidden()) {
            return true;
        }
        for (String suffix : new List<String>{ '__mdt', '__e', '__b', 'ChangeEvent', '__Share', '__History', '__Feed', '__Tag' }) {
            if (name.endsWith(suffix)) {
                return true;
            }
        }
        for (String suffix : new List<String>{ 'Share', 'History', 'Feed' }) {
            if (name.endsWith(suffix) && name.length() > suffix.length()
                    && gd.containsKey(name.left(name.length() - suffix.length()).toLowerCase())) {
                return true;
            }
        }
        return false;
    }

    /** Field describe for up to MAX_FIELD_OBJECTS objects. */
    static Map<String, Object> fields(Map<String, Object> payload) {
        MinuteXSitePermissions p = MinuteXSitePermissions.forIntegrationUser();
        List<Object> requested = MinuteXSiteReceiver.optList(payload.get('objects'), 'payload.objects');
        if (requested.isEmpty() || requested.size() > MAX_FIELD_OBJECTS) {
            throw MinuteXSiteException.forField(400, 'invalid_request', 'payload.objects',
                'payload.objects must list 1-' + MAX_FIELD_OBJECTS + ' object API names.');
        }
        Map<String, SObjectType> gd = Schema.getGlobalDescribe();
        List<Map<String, Object>> described = new List<Map<String, Object>>();
        List<String> unknown = new List<String>();
        List<String> inaccessible = new List<String>();
        for (Object o : requested) {
            String name = MinuteXSiteReceiver.str(o);
            SObjectType t = String.isBlank(name) ? null : gd.get(name.toLowerCase());
            if (t == null) {
                unknown.add(name.left(80));
                continue;
            }
            DescribeSObjectResult d = t.getDescribe();
            if (!p.obj(d.getName()).read) {
                inaccessible.add(d.getName());
                continue;
            }
            described.add(describeFields(d, p));
        }
        return new Map<String, Object>{
            'objects' => described,
            'unknown' => unknown,
            'inaccessible' => inaccessible
        };
    }

    public static Map<String, Object> describeFields(DescribeSObjectResult d, MinuteXSitePermissions p) {
        List<String> names = new List<String>(d.fields.getMap().keySet());
        names.sort();
        List<Map<String, Object>> out = new List<Map<String, Object>>();
        Integer hidden = 0;
        for (String n : names) {
            DescribeFieldResult f = d.fields.getMap().get(n).getDescribe();
            if (!p.fieldReadable(d.getName(), f)) {
                hidden++;
                continue;
            }
            out.add(describeField(d.getName(), f, p));
        }
        return new Map<String, Object>{
            'api_name' => d.getName(),
            'fields' => out,
            'inaccessible_field_count' => hidden
        };
    }

    /** Can a value ever be written to this field (ignoring who is writing)? */
    public static Boolean writableByNature(String objectApiName, DescribeFieldResult f) {
        if (f.isCalculated() || f.isAutoNumber() || f.getType() == Schema.DisplayType.ID) {
            return false;
        }
        if (SYSTEM_FIELDS.contains(f.getName().toLowerCase())) {
            return false;
        }
        return !(f.isNameField() && COMPOUND_NAME_OBJECTS.contains(objectApiName.toLowerCase()));
    }

    public static Map<String, Object> describeField(String objectApiName, DescribeFieldResult f, MinuteXSitePermissions p) {
        String type = String.valueOf(f.getType()).toLowerCase();
        MinuteXSitePermissions.ObjectAccess a = p.obj(objectApiName);
        Boolean writable = writableByNature(objectApiName, f) && p.fieldEditable(objectApiName, f);
        Map<String, Object> m = new Map<String, Object>{
            'api_name' => f.getName(),
            'label' => f.getLabel(),
            'type' => type,
            'createable' => writable && a.create,
            'updateable' => writable && a.edit,
            'nillable' => f.isNillable(),
            'defaulted_on_create' => f.isDefaultedOnCreate(),
            // Must be supplied on insert: Salesforce's own definition.
            'required' => writableByNature(objectApiName, f) && !f.isNillable()
                && !f.isDefaultedOnCreate() && type != 'boolean',
            'is_custom' => f.isCustom(),
            'name_field' => f.isNameField(),
            'filterable' => f.isFilterable(),
            'length' => f.getLength(),
            'reference_to' => referenceNames(f),
            'relationship_name' => f.getRelationshipName()
        };
        if (type == 'picklist' || type == 'multipicklist') {
            List<Map<String, Object>> values = new List<Map<String, Object>>();
            for (Schema.PicklistEntry pe : f.getPicklistValues()) {
                if (!pe.isActive()) {
                    continue;
                }
                values.add(new Map<String, Object>{
                    'value' => pe.getValue(), 'label' => pe.getLabel(), 'default' => pe.isDefaultValue()
                });
                if (values.size() == MAX_PICKLIST_VALUES) {
                    break;
                }
            }
            m.put('picklist_values', values);
        }
        return m;
    }

    static Integer boundedInt(Object raw, Integer dflt, Integer lo, Integer hi, String field) {
        if (raw == null) {
            return dflt;
        }
        if (!(raw instanceof Integer) && !(raw instanceof Long) && !(raw instanceof Decimal)) {
            throw MinuteXSiteException.forField(400, 'invalid_request', field, field + ' must be a number.');
        }
        return Math.max(lo, Math.min(hi, Integer.valueOf(raw)));
    }
}
