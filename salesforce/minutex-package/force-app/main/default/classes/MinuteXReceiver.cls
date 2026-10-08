/**
 * MinuteX starter receiver — gives a Salesforce org the "incoming webhook
 * address" MinuteX sends meetings to (MinuteX Webhook Contract v2,
 * docs/webhook-contract-v2.md). Paste MinuteXMeetingSync first, then this.
 *
 *   POST .../minutex/v2/events          -> MinuteXMeetingSync (meeting,
 *                                          participants, contacts, tasks)
 *   POST .../minutex/v2/configuration   -> the meeting types in meetingTypes()
 *   POST .../minutex/v2/records/search  -> records for a type that uses one
 *   POST .../minutex/v2/people          -> Contacts and Users, for tagging
 *   POST .../minutex/v2/people/search   -> live search of Contacts, Leads
 *                                          and Users as a MinuteX user types
 *   POST .../minutex/v2/files           -> a meeting file (transcript, MoM PDF)
 *   POST .../minutex/v2/people/related  -> the people on the records a
 *                                          meeting is linked to
 *
 *   POST .../minutex/v2/setup/secret    -> MinuteX sets its signing secret
 *                                          (the integration user only; the
 *                                          one call that isn't signed)
 *
 * Every other request is checked against the signing secret BEFORE it is read.
 * A failure rolls the whole request back and answers 500, so MinuteX's
 * retry processes it again.
 *
 * Needs (created by clicks — see the setup guide): the MinuteX Meeting and
 * MinuteX Participant objects, two Task fields, and the custom setting
 * MinuteX_Webhook_Settings__c (Enabled__c, Shared_Secret__c,
 * Previous_Secret__c).
 */
@RestResource(urlMapping='/minutex/v2/*')
global without sharing class MinuteXReceiver {
    static final Integer MAX_SKEW_SECONDS = 300;
    static final Pattern TOKEN = Pattern.compile('\\{([A-Za-z0-9_]+)\\}');

    /** One meeting type MinuteX users choose from. */
    public class MeetingType {
        public String typeId;
        public String name;
        public Boolean recordEnabled = false;
        public Boolean recordRequired = false;
        public String recordLabel = '';
        public String placeholder = '';
        public String searchObject;
        public String searchFields;
        public String displayFormat;
        public String secondaryFormat;
    }

    // THE MEETING TYPES. Edit this list to change what MinuteX offers.
    //   plain(id, name)                    -> no Salesforce record to pick
    //   withRecord(plain(...), required, label, placeholder, object,
    //              search fields, display, second line)
    //                                      -> MinuteX shows a record search
    // The id is permanent (letters, digits, _ . : -): never change it once
    // used. Object and field names are API names, without any namespace.
    public static List<MeetingType> meetingTypes() {
        if (typesOverride != null) {
            return typesOverride;
        }
        // The package ships no customer-specific types (no custom objects).
        return new List<MeetingType>{
            plain('general_meeting', 'General Meeting'),
            plain('client_meeting', 'Client Meeting')
        };
    }

    /** One kind of record MinuteX users can link a meeting to. */
    public class LinkableObject {
        public String objectName;
        public String label;
        public Boolean canBePrimary = true;
        public List<String> outputsAllowed;
        public List<String> outputsDefaultOn;
        public List<String> outputsUserCanChange;
    }

    // Every output MinuteX can send for a linked meeting. The transcript
    // goes as text, in the meeting's Transcript field; it's allowed but off by default.
    // 'documents': the meeting's generated documents, each as a PDF File.
    static final List<String> LINK_OUTPUTS = new List<String>{
        'overview', 'mom', 'tasks', 'participants', 'participant_contributions', 'transcript', 'recording',
        'documents'};
    static final List<String> LINK_DEFAULTS = new List<String>{'overview', 'mom', 'tasks', 'participants', 'documents'};

    /**
     * EDIT HERE: the records MinuteX users can link a meeting to, and what
     * each gets. An object that doesn't exist in this org is skipped.
     */
    public static List<LinkableObject> linkableObjects() {
        if (linkableOverride != null) {
            return linkableOverride;
        }
        return new List<LinkableObject>{
            linkable('Lead', true), linkable('Contact', true), linkable('Account', true),
            linkable('Opportunity', true)
        };
    }

    @TestVisible
    static LinkableObject linkable(String objectName, Boolean canBePrimary) {
        LinkableObject o = new LinkableObject();
        o.objectName = objectName;
        o.canBePrimary = canBePrimary;
        o.outputsAllowed = LINK_OUTPUTS;
        o.outputsDefaultOn = LINK_DEFAULTS;
        o.outputsUserCanChange = LINK_OUTPUTS;
        return o;
    }

    @TestVisible
    static List<LinkableObject> linkableOverride;

    @TestVisible
    static List<MeetingType> typesOverride;
    @TestVisible
    static Long nowOverride;
    @TestVisible
    static MinuteX_Webhook_Settings__c settingsOverride;

    public static MeetingType plain(String typeId, String name) {
        MeetingType t = new MeetingType();
        t.typeId = typeId;
        t.name = name;
        return t;
    }

    public static MeetingType withRecord(MeetingType t, Boolean required, String label, String placeholder,
                                         String objectName, String fields, String display, String secondary) {
        t.recordEnabled = true;
        t.recordRequired = required;
        t.recordLabel = label;
        t.placeholder = placeholder;
        t.searchObject = objectName;
        t.searchFields = fields;
        t.displayFormat = display;
        t.secondaryFormat = secondary;
        return t;
    }

    public class ReceiverException extends Exception {
        public Integer status = 400;
        public String code = 'invalid_request';
    }

    static ReceiverException fail(Integer status, String code, String message) {
        ReceiverException e = new ReceiverException(message);
        e.status = status;
        e.code = code;
        return e;
    }

    @HttpPost
    global static void handlePost() {
        RestRequest req = RestContext.request;
        RestResponse res = RestContext.response;
        Integer status = 200;
        Map<String, Object> body;
        Savepoint sp = Database.setSavepoint();
        try {
            String raw = req.requestBody == null ? '' : req.requestBody.toString();
            String route = routeOf(req.requestURI);
            if (route == 'setup/secret') {
                body = setupSecret(raw);   // NOT signed: see setupSecret
            } else {
                verify(req, raw);  // BEFORE parsing anything
                body = dispatch(route, parse(raw));
            }
        } catch (ReceiverException e) {
            Database.rollback(sp);
            status = e.status;
            body = error(e.code, e.getMessage());
        } catch (MinuteXMeetingSync.SyncException e) {
            Database.rollback(sp);
            status = e.status;
            body = error(e.code, e.getMessage());
        } catch (Exception e) {
            Database.rollback(sp);
            System.debug(LoggingLevel.ERROR, 'MinuteX receiver: ' + e.getTypeName() + ': ' + e.getMessage());
            status = 500;  // MinuteX retries
            body = error('receiver_failed', e.getTypeName() + ': ' + e.getMessage().left(300));
        }
        res.statusCode = status;
        res.addHeader('Content-Type', 'application/json');
        res.addHeader('Cache-Control', 'no-store');
        res.responseBody = Blob.valueOf(JSON.serialize(body));
    }

    /** A signed request, by route and body type. */
    static Map<String, Object> dispatch(String route, Map<String, Object> env) {
        String bodyType = str(env.get('type'));
        if (route == 'events' && bodyType == 'event') {
            return MinuteXMeetingSync.apply(env);
        } else if (route == 'files' && bodyType == 'file.upload') {
            return MinuteXMeetingSync.applyFile(env);
        } else if (route == 'configuration' && bodyType == 'configuration.request') {
            return configuration();
        } else if (route == 'records/search' && bodyType == 'records.search'
                   && env.get('objects') instanceof List<Object>) {
            return linkSearch(str(env.get('query')).left(100), stringsOf(env.get('objects'), 50),
                              intOf(env.get('limit'), 10));
        } else if (route == 'records/search' && bodyType == 'records.search') {
            return search(str(env.get('meeting_type_id')), str(env.get('query')).left(100),
                          intOf(env.get('limit'), 10));
        } else if (route == 'people' && bodyType == 'people.sync') {
            return people(str(env.get('cursor')), intOf(env.get('page_size'), 200));
        } else if (route == 'people/related' && bodyType == 'people.related') {
            return peopleRelated(stringsOf(env.get('record_ids'), 5), intOf(env.get('limit'), 50));
        } else if (route == 'people/search' && bodyType == 'people.search') {
            return peopleSearch(str(env.get('query')).left(100), typesOf(env.get('types')),
                                intOf(env.get('limit'), 20));
        } else {
            throw fail(404, 'unknown_endpoint', 'This receiver does not handle that request.');
        }
    }

    // -- setup ---------------------------------------------------------------

    /**
     * The signing secret, set by MinuteX itself right after its Salesforce
     * login: the package's settings are protected, so no admin pastes it.
     * This call can't be signed (there is no secret yet), so it is allowed
     * only for a signed-in user who is not the Site guest and holds the
     * MinuteX Integration custom permission (the package's MinuteX
     * Integration permission set). The secret it replaces keeps working as
     * the previous one, so a rotation drops nothing in flight.
     */
    @TestVisible
    static Map<String, Object> setupSecret(String raw) {
        if (UserInfo.getUserType() == 'Guest' || !FeatureManagement.checkPermission('MinuteX_Integration')) {
            throw fail(403, 'not_allowed', 'Only the MinuteX integration user can set up MinuteX.');
        }
        Map<String, Object> env = parse(raw);
        String secret = str(env.get('secret'));
        if (str(env.get('type')) != 'setup.secret' || !secret.startsWith('mxwhsec_')
                || secret.length() < 32 || secret.length() > 255) {
            throw fail(400, 'invalid_secret', 'Not a MinuteX signing secret.');
        }
        MinuteX_Webhook_Settings__c s = MinuteX_Webhook_Settings__c.getOrgDefaults();
        if (s == null || s.Id == null) {
            s = new MinuteX_Webhook_Settings__c(SetupOwnerId = UserInfo.getOrganizationId());
        }
        String old = s.Shared_Secret__c == null ? '' : s.Shared_Secret__c.trim();
        if (old != '' && old != secret) {
            s.Previous_Secret__c = old;
        }
        s.Shared_Secret__c = secret;
        s.Enabled__c = true;
        upsert s;
        return new Map<String, Object>{'ok' => true, 'namespace' => namespacePrefix()};
    }

    // -- signature -----------------------------------------------------------

    static void verify(RestRequest req, String raw) {
        MinuteX_Webhook_Settings__c s = settingsOverride != null
            ? settingsOverride : MinuteX_Webhook_Settings__c.getOrgDefaults();
        if (s == null || s.Enabled__c != true || String.isBlank(s.Shared_Secret__c)) {
            throw fail(403, 'receiver_disabled', 'The MinuteX receiver is not enabled in this org.');
        }
        String ts = header(req, 'X-MinuteX-Timestamp');
        String sig = header(req, 'X-MinuteX-Signature');
        if (String.isBlank(ts) || String.isBlank(sig) || !ts.trim().isNumeric()) {
            throw fail(401, 'bad_signature', 'Missing or invalid signature.');
        }
        ts = ts.trim();
        Long now = nowOverride != null ? nowOverride : Datetime.now().getTime() / 1000;
        if (Math.abs(now - Long.valueOf(ts)) > MAX_SKEW_SECONDS) {
            throw fail(401, 'bad_signature', 'The request is too old or from the future.');
        }
        // During a secret rotation MinuteX sends "v1=<new>,v1=<old>".
        List<String> secrets = new List<String>{s.Shared_Secret__c.trim()};
        if (String.isNotBlank(s.Previous_Secret__c)) {
            secrets.add(s.Previous_Secret__c.trim());
        }
        for (String part : sig.split(',')) {
            String given = part.trim();
            if (!given.startsWith('v1=')) {
                continue;
            }
            given = given.substring(3);
            for (String secret : secrets) {
                String want = EncodingUtil.convertToHex(Crypto.generateMac('hmacSHA256',
                    Blob.valueOf(ts + '.' + raw), Blob.valueOf(secret)));
                if (sameText(want, given)) {
                    return;
                }
            }
        }
        throw fail(401, 'bad_signature', 'Missing or invalid signature.');
    }

    /** Compares every character, so timing reveals nothing. */
    @TestVisible
    static Boolean sameText(String a, String b) {
        if (a == null || b == null || a.length() != b.length()) {
            return false;
        }
        Integer diff = 0;
        for (Integer i = 0; i < a.length(); i++) {
            diff |= a.charAt(i) ^ b.charAt(i);
        }
        return diff == 0;
    }

    // -- configuration -------------------------------------------------------

    static Map<String, Object> configuration() {
        List<Object> types = new List<Object>();
        for (MeetingType t : meetingTypes()) {
            types.add(new Map<String, Object>{
                'id' => t.typeId, 'name' => t.name,
                'record_selection' => new Map<String, Object>{
                    'enabled' => t.recordEnabled, 'required' => t.recordEnabled && t.recordRequired,
                    'label' => t.recordLabel, 'placeholder' => t.placeholder}
            });
        }
        List<Object> linkables = new List<Object>();
        for (LinkableObject o : linkableObjects()) {
            Schema.SObjectType objectType = objectNamed(o.objectName);
            if (objectType == null) {
                continue;  // not in this org
            }
            Schema.DescribeSObjectResult d = objectType.getDescribe();
            linkables.add(new Map<String, Object>{
                'object' => d.getName(), 'label' => String.isBlank(o.label) ? d.getLabel() : o.label,
                'can_be_primary' => o.canBePrimary, 'outputs_allowed' => o.outputsAllowed,
                'outputs_default_on' => o.outputsDefaultOn,
                'outputs_user_can_change' => o.outputsUserCanChange});
        }
        // Changes by itself whenever meetingTypes() or linkableObjects() change.
        String version = EncodingUtil.convertToHex(Crypto.generateDigest('SHA-256',
            Blob.valueOf(JSON.serialize(types) + JSON.serialize(linkables)))).left(12);
        return new Map<String, Object>{
            'contract_version' => '2.0', 'configuration_version' => version, 'meeting_types' => types,
            'linkable_objects' => linkables};
    }

    // -- record search -------------------------------------------------------

    /** Records of the meeting type's object whose search fields contain the
     *  query (or whose Id is the query). Returns ONLY the display text and
     *  the id: the fields named on the meeting type are the access list. */
    static Map<String, Object> search(String typeId, String query, Integer limitCount) {
        MeetingType t;
        for (MeetingType candidate : meetingTypes()) {
            if (candidate.typeId == typeId) {
                t = candidate;
            }
        }
        if (t == null || !t.recordEnabled) {
            throw fail(422, 'unknown_meeting_type', 'This meeting type does not use a record.');
        }
        Schema.SObjectType objectType = objectNamed(t.searchObject);
        if (objectType == null) {
            throw fail(500, 'search_misconfigured', 'Object ' + t.searchObject + ' does not exist in this org.');
        }
        Schema.DescribeSObjectResult d = objectType.getDescribe();
        Map<String, Schema.SObjectField> fields = d.fields.getMap();
        List<String> searchable = new List<String>();
        for (String f : t.searchFields.split(',')) {
            Schema.SObjectField field = fieldNamed(fields, f.trim());
            if (field == null) {
                throw fail(500, 'search_misconfigured', 'Field ' + f.trim() + ' does not exist on ' + t.searchObject + '.');
            }
            searchable.add(field.getDescribe().getName());
        }
        Set<String> selected = new Set<String>{'Id'};
        selected.addAll(searchable);
        Map<String, String> tokenField = new Map<String, String>();
        for (String fmt : new List<String>{t.displayFormat, t.secondaryFormat}) {
            Matcher m = TOKEN.matcher(fmt == null ? '' : fmt);
            while (m.find()) {
                Schema.SObjectField field = fieldNamed(fields, m.group(1));
                if (field != null) {
                    tokenField.put(m.group(1), field.getDescribe().getName());
                    selected.add(field.getDescribe().getName());
                }
            }
        }
        Integer lim = Math.max(1, Math.min(25, limitCount));
        Map<String, Object> binds = new Map<String, Object>{'lim' => lim};
        String whereClause = '';
        String q = query == null ? '' : query.trim();
        if (q != '') {
            List<String> ors = new List<String>();
            binds.put('q', '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%');
            for (String f : searchable) {
                ors.add(f + ' LIKE :q');
            }
            if (q.isAlphanumeric() && (q.length() == 15 || q.length() == 18)) {
                binds.put('qid', q);
                ors.add('Id = :qid');
            }
            whereClause = ' WHERE (' + String.join(ors, ' OR ') + ')';
        }
        String soql = 'SELECT ' + String.join(new List<String>(selected), ', ') + ' FROM ' + d.getName()
            + whereClause + ' ORDER BY LastModifiedDate DESC LIMIT :lim';
        List<Object> records = new List<Object>();
        for (SObject rec : Database.queryWithBinds(soql, binds, AccessLevel.SYSTEM_MODE)) {
            String display = render(t.displayFormat, rec, tokenField).left(200);
            if (String.isBlank(display)) {
                continue;
            }
            records.add(new Map<String, Object>{
                'id' => String.valueOf(rec.Id), 'display_name' => display,
                'secondary_text' => render(t.secondaryFormat, rec, tokenField).left(200),
                'object_type' => d.getName()});
        }
        return new Map<String, Object>{'records' => records, 'next_cursor' => null};
    }

    /** This org's namespace prefix, if any — read from this class's own name
     *  (no query, so no extra permission for the Site guest user). */
    @TestVisible
    static String namespacePrefix() {
        String full = MinuteXReceiver.class.getName();
        return full.contains('.') ? full.substringBefore('.') : '';
    }

    static Schema.SObjectType objectNamed(String name) {
        Map<String, Schema.SObjectType> all = Schema.getGlobalDescribe();
        Schema.SObjectType found = all.get(name);
        if (found == null && namespacePrefix() != '') {
            found = all.get(namespacePrefix() + '__' + name);
        }
        if (found == null) {
            // Resolves a name the way SOQL does (including this org's own
            // namespace); throws for an object that does not exist.
            try {
                found = Schema.describeSObjects(new List<String>{name})[0].getSObjectType();
            } catch (Exception e) {
                found = null;
            }
        }
        return found;
    }

    static Schema.SObjectField fieldNamed(Map<String, Schema.SObjectField> fields, String name) {
        Schema.SObjectField found = fields.get(name);
        if (found == null && namespacePrefix() != '') {
            found = fields.get(namespacePrefix() + '__' + name);
        }
        return found;
    }

    @TestVisible
    static String render(String format, SObject rec, Map<String, String> tokenField) {
        if (String.isBlank(format)) {
            return '';
        }
        String out = format;
        Matcher m = TOKEN.matcher(format);
        while (m.find()) {
            String apiName = tokenField.get(m.group(1));
            Object v = apiName == null ? null : rec.get(apiName);
            out = out.replace(m.group(0), v == null ? '' : String.valueOf(v));
        }
        return out.trim().removeEnd('—').removeEnd('-').trim();
    }

    // -- people --------------------------------------------------------------

    static Map<String, Object> people(String cursor, Integer pageSize) {
        Integer size = Math.max(1, Math.min(500, pageSize));
        String c = cursor == null ? '' : cursor.trim();
        List<Object> out = new List<Object>();
        if (c == '' || c.startsWith('contact:')) {
            String after = c.startsWith('contact:') ? c.substringAfter('contact:') : '';
            List<Contact> rows = String.isBlank(after)
                ? [SELECT Id, Name, Email, Phone, Title, Account.Name FROM Contact ORDER BY Id LIMIT :size]
                : [SELECT Id, Name, Email, Phone, Title, Account.Name FROM Contact
                   WHERE Id > :after ORDER BY Id LIMIT :size];
            for (Contact r : rows) {
                out.add(person('contact', r.Id, r.Name, r.Email, r.Phone, r.Title,
                               r.Account == null ? null : r.Account.Name, true));
            }
            String next = rows.size() == size ? 'contact:' + rows[rows.size() - 1].Id : 'user:';
            return new Map<String, Object>{'people' => out, 'next_cursor' => next, 'done' => false};
        }
        String after = c.substringAfter('user:');
        List<User> rows = String.isBlank(after)
            ? [SELECT Id, Name, Email, Phone, Title, CompanyName, IsActive FROM User
               WHERE UserType = 'Standard' ORDER BY Id LIMIT :size]
            : [SELECT Id, Name, Email, Phone, Title, CompanyName, IsActive FROM User
               WHERE UserType = 'Standard' AND Id > :after ORDER BY Id LIMIT :size];
        for (User r : rows) {
            out.add(person('user', r.Id, r.Name, r.Email, r.Phone, r.Title, r.CompanyName, r.IsActive));
        }
        Boolean more = rows.size() == size;
        return new Map<String, Object>{
            'people' => out, 'next_cursor' => more ? 'user:' + rows[rows.size() - 1].Id : '', 'done' => !more};
    }

    // -- search across linkable objects ---------------------------------------

    /**
     * Records of the linkable objects whose name (or email) contains what a
     * MinuteX user typed, newest first. A regular query, not Salesforce text
     * search: text search finds nothing for a Site guest user and lags behind
     * new records. Only objects listed in linkableObjects() are searched;
     * that list is what you allow MinuteX to see.
     */
    static Map<String, Object> linkSearch(String query, List<String> wanted, Integer limitCount) {
        String term = query == null ? '' : query.trim();
        if (term.length() < 2) {
            throw fail(400, 'query_too_short', 'Type at least 2 characters.');
        }
        Integer size = Math.max(1, Math.min(25, limitCount));
        Set<String> allowed = new Set<String>();
        for (LinkableObject o : linkableObjects()) {
            Schema.SObjectType t = objectNamed(o.objectName);
            if (t != null) {
                allowed.add(t.getDescribe().getName().toLowerCase());
            }
        }
        String pattern = '%' + likeEscape(term) + '%';
        List<Object> out = new List<Object>();
        for (String w : wanted) {
            if (out.size() >= size) {
                break;
            }
            Schema.SObjectType t = objectNamed(w);
            if (t == null) {
                continue;
            }
            Schema.DescribeSObjectResult d = t.getDescribe();
            if (!allowed.contains(d.getName().toLowerCase())) {
                continue;
            }
            Map<String, Schema.SObjectField> fieldMap = d.fields.getMap();
            String nameApi = null;
            for (Schema.SObjectField f : fieldMap.values()) {
                if (f.getDescribe().isNameField()) {
                    nameApi = f.getDescribe().getName();
                    break;
                }
            }
            if (nameApi == null) {
                continue;
            }
            String filter = nameApi + ' LIKE :pattern';
            if (fieldMap.containsKey('email')) {
                filter += ' OR Email LIKE :pattern';
            }
            String extra = d.getName() == 'Lead' ? ' AND IsConverted = false' : '';
            Integer remaining = size - out.size();
            List<SObject> rows = Database.query('SELECT Id, ' + nameApi + ' FROM ' + d.getName()
                + ' WHERE (' + filter + ')' + extra + ' ORDER BY LastModifiedDate DESC LIMIT :remaining');
            for (SObject r : rows) {
                Object name = r.get(nameApi);
                out.add(new Map<String, Object>{
                    'id' => String.valueOf(r.Id),
                    'display_name' => name == null ? String.valueOf(r.Id) : String.valueOf(name),
                    'secondary_text' => d.getLabel(), 'object_type' => d.getName()});
            }
        }
        return new Map<String, Object>{'records' => out, 'next_cursor' => null};
    }

    /** A term safe inside a LIKE pattern: % and _ match themselves. */
    @TestVisible
    static String likeEscape(String term) {
        return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_');
    }

    // -- live people search ----------------------------------------------------

    /**
     * Contacts, Leads and Users whose name or email contains what a MinuteX
     * user is typing. A regular query, like linkSearch (text search finds
     * nothing for a Site guest user). Converted Leads and inactive or
     * non-standard Users are left out.
     */
    static Map<String, Object> peopleSearch(String query, Set<String> kinds, Integer limitCount) {
        String term = query == null ? '' : query.trim();
        if (term.length() < 2) {
            throw fail(400, 'query_too_short', 'Type at least 2 characters.');
        }
        Integer size = Math.max(1, Math.min(20, limitCount));
        String pattern = '%' + likeEscape(term) + '%';
        List<Object> out = new List<Object>();
        if (kinds.contains('contact')) {
            for (Contact c : [SELECT Id, Name, Email, Phone, Title, Account.Name FROM Contact
                              WHERE Name LIKE :pattern OR Email LIKE :pattern
                              ORDER BY LastModifiedDate DESC LIMIT :size]) {
                out.add(person('contact', c.Id, c.Name, c.Email, c.Phone, c.Title,
                               c.Account == null ? null : c.Account.Name, true));
            }
        }
        if (kinds.contains('lead') && out.size() < size) {
            Integer remaining = size - out.size();
            for (Lead l : [SELECT Id, Name, Email, Phone, Title, Company FROM Lead
                           WHERE (Name LIKE :pattern OR Email LIKE :pattern) AND IsConverted = false
                           ORDER BY LastModifiedDate DESC LIMIT :remaining]) {
                out.add(person('lead', l.Id, l.Name, l.Email, l.Phone, l.Title, l.Company, true));
            }
        }
        if (kinds.contains('user') && out.size() < size) {
            Integer remaining = size - out.size();
            for (User u : [SELECT Id, Name, Email, Phone, Title, CompanyName FROM User
                           WHERE (Name LIKE :pattern OR Email LIKE :pattern)
                           AND IsActive = true AND UserType = 'Standard'
                           ORDER BY Name LIMIT :remaining]) {
                out.add(person('user', u.Id, u.Name, u.Email, u.Phone, u.Title, u.CompanyName, true));
            }
        }
        return new Map<String, Object>{'people' => out};
    }

    // -- people on the meeting's records ---------------------------------------

    /**
     * The people on the records a meeting is linked to, so MinuteX can
     * suggest them before anyone searches: a linked Lead or Contact itself,
     * an Account's Contacts, an Opportunity's contact roles and its Account's
     * Contacts, and on any other object (a Property, a Site Visit) the
     * Contacts, Leads and Accounts its lookup fields point to. Same guest-user
     * visibility as live search.
     */
    static Map<String, Object> peopleRelated(List<String> recordIds, Integer limitCount) {
        Integer size = Math.max(1, Math.min(50, limitCount));
        Set<Id> leadIds = new Set<Id>();
        Set<Id> linkedContactIds = new Set<Id>();
        Set<Id> accountIds = new Set<Id>();
        Set<Id> opportunityIds = new Set<Id>();
        for (String raw : recordIds) {
            Id recId;
            try {
                recId = Id.valueOf(raw);
            } catch (Exception e) {
                continue;  // not a record id: nothing to suggest from it
            }
            Schema.SObjectType objType = recId.getSObjectType();
            if (objType == Lead.SObjectType) {
                leadIds.add(recId);
            } else if (objType == Contact.SObjectType) {
                linkedContactIds.add(recId);
            } else if (objType == Account.SObjectType) {
                accountIds.add(recId);
            } else if (objType == Opportunity.SObjectType) {
                opportunityIds.add(recId);
            } else {
                addLookups(recId, objType, leadIds, linkedContactIds, accountIds);
            }
        }

        Set<Id> roleContactIds = new Set<Id>();
        if (!opportunityIds.isEmpty() && Schema.sObjectType.Opportunity.isAccessible()) {
            for (Opportunity o : [SELECT AccountId FROM Opportunity WHERE Id IN :opportunityIds]) {
                if (o.AccountId != null) {
                    accountIds.add(o.AccountId);
                }
            }
            if (Schema.sObjectType.OpportunityContactRole.isAccessible()) {
                for (OpportunityContactRole r : [SELECT ContactId FROM OpportunityContactRole
                                                 WHERE OpportunityId IN :opportunityIds LIMIT 50]) {
                    roleContactIds.add(r.ContactId);
                }
            }
        }

        List<Object> out = new List<Object>();
        if (!leadIds.isEmpty() && Schema.sObjectType.Lead.isAccessible()) {
            for (Lead l : [SELECT Id, Name, Email, Phone, Title, Company FROM Lead
                           WHERE Id IN :leadIds AND IsConverted = false LIMIT :size]) {
                out.add(related(person('lead', l.Id, l.Name, l.Email, l.Phone, l.Title, l.Company, true),
                                'Linked Lead'));
            }
        }
        if (Schema.sObjectType.Contact.isAccessible()
                && !(linkedContactIds.isEmpty() && roleContactIds.isEmpty() && accountIds.isEmpty())) {
            for (Contact c : [SELECT Id, Name, Email, Phone, Title, AccountId, Account.Name FROM Contact
                              WHERE Id IN :linkedContactIds OR Id IN :roleContactIds OR AccountId IN :accountIds
                              ORDER BY Name LIMIT :size]) {
                String accountName = c.Account == null ? null : c.Account.Name;
                String why = linkedContactIds.contains(c.Id) ? 'Linked Contact'
                    : roleContactIds.contains(c.Id) ? 'Opportunity contact role'
                    : 'Contact at ' + (accountName == null ? 'the linked account' : accountName);
                out.add(related(person('contact', c.Id, c.Name, c.Email, c.Phone, c.Title, accountName, true), why));
            }
        }
        while (out.size() > size) {
            out.remove(out.size() - 1);
        }
        return new Map<String, Object>{'people' => out};
    }

    /** The Contacts, Leads and Accounts a record's lookup fields point to. */
    static void addLookups(Id recId, Schema.SObjectType objType, Set<Id> leadIds,
                           Set<Id> linkedContactIds, Set<Id> accountIds) {
        Schema.DescribeSObjectResult d = objType.getDescribe();
        if (!d.isAccessible()) {
            return;
        }
        List<String> names = new List<String>();
        for (Schema.SObjectField f : d.fields.getMap().values()) {
            Schema.DescribeFieldResult fd = f.getDescribe();
            if (fd.getType() != Schema.DisplayType.REFERENCE || !fd.isAccessible()) {
                continue;
            }
            for (Schema.SObjectType target : fd.getReferenceTo()) {
                if (target == Lead.SObjectType || target == Contact.SObjectType || target == Account.SObjectType) {
                    names.add(fd.getName());
                    break;
                }
            }
            if (names.size() >= 10) {
                break;
            }
        }
        if (names.isEmpty()) {
            return;
        }
        List<SObject> rows = Database.query('SELECT ' + String.join(names, ', ') + ' FROM '
            + d.getName() + ' WHERE Id = :recId LIMIT 1');
        if (rows.isEmpty()) {
            return;
        }
        for (String name : names) {
            Object v = rows[0].get(name);
            if (!(v instanceof Id)) {
                continue;
            }
            Id target = (Id) v;
            Schema.SObjectType targetType = target.getSObjectType();
            if (targetType == Lead.SObjectType) {
                leadIds.add(target);
            } else if (targetType == Contact.SObjectType) {
                linkedContactIds.add(target);
            } else if (targetType == Account.SObjectType) {
                accountIds.add(target);
            }
        }
    }

    static Map<String, Object> related(Map<String, Object> p, String why) {
        p.put('relation', why);
        return p;
    }

    /** Up to `max` non-blank strings from a JSON list. */
    static List<String> stringsOf(Object raw, Integer max) {
        List<String> out = new List<String>();
        if (raw instanceof List<Object>) {
            for (Object v : (List<Object>) raw) {
                String s = str(v);
                if (String.isNotBlank(s) && out.size() < max) {
                    out.add(s);
                }
            }
        }
        return out;
    }

    /** The people types asked for; all three when none are named. */
    static Set<String> typesOf(Object raw) {
        Set<String> out = new Set<String>();
        if (raw instanceof List<Object>) {
            for (Object v : (List<Object>) raw) {
                out.add(str(v).toLowerCase());
            }
        }
        return out.isEmpty() ? new Set<String>{'contact', 'lead', 'user'} : out;
    }

    /** A search term with SOSL's reserved characters escaped. */
    @TestVisible
    static String escapeSosl(String term) {
        String reserved = '?&|!{}[]()^~*:\\"\'+-';
        String out = '';
        for (Integer i = 0; i < term.length(); i++) {
            String ch = term.substring(i, i + 1);
            out += (reserved.contains(ch) ? '\\' : '') + ch;
        }
        return out;
    }

    static Map<String, Object> person(String kind, Id recordId, String name, String email, String phone,
                                      String title, String company, Boolean active) {
        return new Map<String, Object>{
            'provider' => 'salesforce', 'type' => kind, 'external_id' => String.valueOf(recordId),
            'name' => name, 'email' => email, 'phone' => phone, 'title' => title,
            'company' => company, 'status' => active == false ? 'inactive' : 'active'};
    }

    // -- helpers -------------------------------------------------------------

    @TestVisible
    static String routeOf(String uri) {
        String path = uri == null ? '' : uri;
        Integer at = path.toLowerCase().indexOf('/minutex/v2/');
        return at < 0 ? '' : path.substring(at + 12).removeEnd('/').toLowerCase();
    }

    static Map<String, Object> parse(String raw) {
        try {
            Object parsed = JSON.deserializeUntyped(raw);
            if (parsed instanceof Map<String, Object>) {
                return (Map<String, Object>) parsed;
            }
        } catch (Exception e) {
            // falls through
        }
        throw fail(400, 'invalid_json', 'The request body is not a JSON object.');
    }

    static String header(RestRequest req, String name) {
        if (req == null || req.headers == null) {
            return null;
        }
        for (String k : req.headers.keySet()) {
            if (k != null && k.equalsIgnoreCase(name)) {
                return req.headers.get(k);
            }
        }
        return null;
    }

    static String str(Object v) {
        return v == null ? '' : String.valueOf(v).trim();
    }

    static Integer intOf(Object v, Integer fallback) {
        if (v instanceof Integer) {
            return (Integer) v;
        }
        if (v instanceof Long) {
            return ((Long) v).intValue();
        }
        if (v instanceof Decimal) {
            return ((Decimal) v).intValue();
        }
        return fallback;
    }

    static Map<String, Object> error(String code, String message) {
        return new Map<String, Object>{
            'ok' => false, 'error' => new Map<String, Object>{'code' => code, 'message' => message}};
    }
}
