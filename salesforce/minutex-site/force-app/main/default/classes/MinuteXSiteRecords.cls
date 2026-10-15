/**
 * record_search: find records of ONE object so a person can pick the one a
 * Task belongs to.
 *
 * Only objects the integration user can read are searchable, and only
 * records it can read under sharing (UserRecordAccess) are returned — the
 * query runs in system mode (the Site guest user can see nothing) and the
 * results are then filtered to that user's visibility.
 *
 * INJECTION. The object and field names in the SOQL come from this org's
 * describe (the canonical getName()), never from the request text; the search
 * term is a bind variable with LIKE wildcards escaped.
 */
public without sharing class MinuteXSiteRecords {
    public static final Integer DEFAULT_LIMIT = 20;
    public static final Integer MAX_LIMIT = 50;
    // Candidates fetched before the visibility filter (UserRecordAccess takes
    // at most 200 ids per query).
    static final Integer CANDIDATES = 200;
    static final Pattern SF_ID = Pattern.compile('[A-Za-z0-9]{15}([A-Za-z0-9]{3})?');

    public static Map<String, Object> search(Map<String, Object> envelope) {
        MinuteXSitePermissions p = MinuteXSitePermissions.forIntegrationUser();
        Map<String, Object> payload = MinuteXSiteReceiver.optObj(envelope.get('payload'), 'payload');
        DescribeSObjectResult d = readableObject(MinuteXSiteReceiver.str(payload.get('object')), p);
        String term = MinuteXSiteReceiver.str(payload.get('query'));
        if (term.length() > 100) {
            throw MinuteXSiteException.forField(400, 'invalid_request', 'payload.query',
                'payload.query must be at most 100 characters.');
        }
        Integer lim = DEFAULT_LIMIT;
        if (payload.get('limit') != null) {
            lim = Math.max(1, Math.min(MAX_LIMIT, Integer.valueOf(payload.get('limit'))));
        }

        String nameField = nameField(d, p);
        String soql = 'SELECT Id' + (nameField == 'Id' ? '' : ', ' + nameField) + ' FROM ' + d.getName();
        Map<String, Object> binds = new Map<String, Object>{ 'lim' => CANDIDATES };
        if (SF_ID.matcher(term).matches() && term.startsWith(d.getKeyPrefix() == null ? '~' : d.getKeyPrefix())) {
            soql += ' WHERE Id = :recId';
            binds.put('recId', term);
        } else if (String.isNotBlank(term) && nameField != 'Id') {
            soql += ' WHERE ' + nameField + ' LIKE :pattern';
            binds.put('pattern', '%' + escapeLike(term) + '%');
        }
        DescribeFieldResult nf = nameField == 'Id' ? null : d.fields.getMap().get(nameField).getDescribe();
        if (nf != null && nf.isSortable()) {
            soql += ' ORDER BY ' + nameField;
        }
        soql += ' LIMIT :lim';

        List<SObject> rows;
        try {
            rows = Database.queryWithBinds(soql, binds, AccessLevel.SYSTEM_MODE);
        } catch (QueryException e) {
            throw MinuteXSiteException.of(422, 'not_searchable',
                d.getLabel() + ' records cannot be searched by ' + nameField + ': ' + e.getMessage());
        }
        List<Id> ids = new List<Id>();
        for (SObject r : rows) {
            ids.add(r.Id);
        }
        Set<Id> visible = p.readableRecords(ids);
        List<Map<String, Object>> records = new List<Map<String, Object>>();
        for (SObject r : rows) {
            if (!visible.contains(r.Id)) {
                continue;
            }
            records.add(new Map<String, Object>{
                'id' => String.valueOf(r.Id),
                'name' => nameField == 'Id' ? String.valueOf(r.Id) : String.valueOf(r.get(nameField))
            });
            if (records.size() == lim) {
                break;
            }
        }
        return new Map<String, Object>{
            'object' => d.getName(),
            'name_field' => nameField,
            'records' => records
        };
    }

    /** The object's describe, or a refusal: unknown -> 422, no read -> 403. */
    public static DescribeSObjectResult readableObject(String apiName, MinuteXSitePermissions p) {
        SObjectType t = String.isBlank(apiName) ? null : Schema.getGlobalDescribe().get(apiName.toLowerCase());
        if (t == null) {
            throw MinuteXSiteException.forField(422, 'invalid_object', 'payload.object',
                'This Salesforce org has no object named ' + apiName.left(80) + '.');
        }
        DescribeSObjectResult d = t.getDescribe();
        if (!p.obj(d.getName()).read) {
            throw MinuteXSiteException.of(403, 'permission_denied',
                'Salesforce permissions do not allow reading ' + d.getLabelPlural() + ' for the integration user.');
        }
        return d;
    }

    /** The object's name field, if the integration user can read it; otherwise Id. */
    public static String nameField(DescribeSObjectResult d, MinuteXSitePermissions p) {
        for (SObjectField f : d.fields.getMap().values()) {
            DescribeFieldResult fd = f.getDescribe();
            if (fd.isNameField() && p.fieldReadable(d.getName(), fd)) {
                return fd.getName();
            }
        }
        return 'Id';
    }

    /** True when the record exists and the integration user can read it. */
    public static Boolean isVisible(DescribeSObjectResult d, Id recordId, MinuteXSitePermissions p) {
        List<SObject> rows = Database.queryWithBinds(
            'SELECT Id FROM ' + d.getName() + ' WHERE Id = :recId LIMIT 1',
            new Map<String, Object>{ 'recId' => recordId }, AccessLevel.SYSTEM_MODE);
        return !rows.isEmpty() && p.canRead(recordId);
    }

    @TestVisible
    static String escapeLike(String term) {
        return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_');
    }
}
