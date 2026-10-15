/**
 * The MinuteX Setup tab (Lightning component minutexSetup): which Salesforce
 * objects MinuteX users may link meetings to, and what each output does for
 * each object. Saved as MinuteX Linkable Object rows; MinuteXReceiver reads
 * them (configuredObjects) and MinuteX picks the change up by itself.
 *
 * Output modes, per object:
 *   none    not sent for this object
 *   off     the user may tick it; unticked by default
 *   on      the user may untick it; ticked by default
 *   always  always sent; the user can't untick it
 *
 * Only users with the MinuteX Admin custom permission (the package's
 * MinuteX Admin permission set) may read or save it.
 */
public with sharing class MinuteXSetupController {
    public static final Set<String> MODE_NAMES = new Set<String>{'none', 'off', 'on', 'always'};
    static final Integer MAX_OBJECTS = 20;

    static final Map<String, String> OUTPUT_LABELS = new Map<String, String>{
        'overview' => 'Overview', 'mom' => 'MoM (PDF)', 'tasks' => 'Tasks',
        'participants' => 'Participants', 'participant_contributions' => 'What each person said',
        'transcript' => 'Transcript', 'recording' => 'Recording link',
        'documents' => 'Generated documents (PDF)'};

    /** Setup objects, never business records. */
    static final Set<String> NOT_LINKABLE = new Set<String>{
        'User', 'Group', 'Profile', 'PermissionSet', 'Organization', 'UserRole'};

    /** Standard objects worth offering; any custom object is offered too. */
    static final List<String> STANDARD_OBJECTS = new List<String>{
        'Account', 'Contact', 'Lead', 'Opportunity', 'Case', 'Campaign', 'Contract', 'Order'};

    // ------------------------------------------------------------------ read by the receiver

    /** The admin's saved choices as the receiver's objects; empty when
     *  nothing is saved (the receiver then uses its defaults). */
    public static List<MinuteXReceiver.LinkableObject> configuredObjects() {
        List<MinuteXReceiver.LinkableObject> out = new List<MinuteXReceiver.LinkableObject>();
        for (MinuteX_Linkable_Object__c row : [
                SELECT Object_Name__c, Can_Be_Primary__c, Outputs__c FROM MinuteX_Linkable_Object__c
                ORDER BY Sort_Order__c NULLS LAST, Object_Name__c LIMIT :MAX_OBJECTS]) {
            Map<String, String> modes = parseModes(row.Outputs__c);
            MinuteXReceiver.LinkableObject o = new MinuteXReceiver.LinkableObject();
            o.objectName = row.Object_Name__c;
            o.canBePrimary = row.Can_Be_Primary__c == true;
            o.outputsAllowed = new List<String>();
            o.outputsDefaultOn = new List<String>();
            o.outputsUserCanChange = new List<String>();
            for (String output : MinuteXReceiver.LINK_OUTPUTS) {
                String mode = modes.get(output);
                if (mode == null || mode == 'none') {
                    continue;
                }
                o.outputsAllowed.add(output);
                if (mode == 'on' || mode == 'always') {
                    o.outputsDefaultOn.add(output);
                }
                if (mode == 'off' || mode == 'on') {
                    o.outputsUserCanChange.add(output);
                }
            }
            out.add(o);
        }
        return out;
    }

    /** "overview:on,mom:always" -> {overview: on, mom: always}; unknown parts are ignored. */
    @TestVisible
    static Map<String, String> parseModes(String encoded) {
        Map<String, String> out = new Map<String, String>();
        for (String part : (encoded == null ? '' : encoded).split(',')) {
            String output = part.substringBefore(':').trim();
            String mode = part.substringAfter(':').trim();
            if (MinuteXReceiver.LINK_OUTPUTS.contains(output) && MODE_NAMES.contains(mode)) {
                out.put(output, mode);
            }
        }
        return out;
    }

    @TestVisible
    static String encodeModes(Map<String, String> modes) {
        List<String> parts = new List<String>();
        for (String output : MinuteXReceiver.LINK_OUTPUTS) {
            String mode = modes.get(output);
            if (mode != null && mode != 'none') {
                parts.add(output + ':' + mode);
            }
        }
        return String.join(parts, ',');
    }

    // ------------------------------------------------------------------ the Setup tab

    @AuraEnabled
    public static Map<String, Object> getSetup() {
        requireAdmin();
        return state();
    }

    /**
     * Save the objects, in order: [{objectName, canBePrimary, modes: {output: mode}}].
     * Replaces what was saved. Refuses an object that doesn't exist or is
     * MinuteX's own, an unknown mode, and a setup with no possible main record.
     */
    @AuraEnabled
    public static Map<String, Object> saveSetup(String payloadJson) {
        requireAdmin();
        List<Object> items;
        try {
            items = (List<Object>) JSON.deserializeUntyped(payloadJson);
        } catch (Exception e) {
            throw problem('The setup could not be read. Reload the page and try again.');
        }
        if (items.size() > MAX_OBJECTS) {
            throw problem('At most ' + MAX_OBJECTS + ' objects can be linked.');
        }
        List<MinuteX_Linkable_Object__c> rows = new List<MinuteX_Linkable_Object__c>();
        Set<String> seen = new Set<String>();
        Boolean anyPrimary = false;
        Integer order = 0;
        for (Object o : items) {
            Map<String, Object> item = (Map<String, Object>) o;
            Schema.DescribeSObjectResult d = linkable(String.valueOf(item.get('objectName')));
            if (d == null) {
                throw problem(String.valueOf(item.get('objectName')) + ' can\'t be linked to meetings.');
            }
            if (!seen.add(d.getName())) {
                continue;
            }
            Map<String, String> modes = new Map<String, String>();
            Map<String, Object> given = item.get('modes') instanceof Map<String, Object>
                ? (Map<String, Object>) item.get('modes') : new Map<String, Object>();
            for (String output : given.keySet()) {
                String mode = String.valueOf(given.get(output));
                if (!MinuteXReceiver.LINK_OUTPUTS.contains(output) || !MODE_NAMES.contains(mode)) {
                    throw problem('Unknown setting for ' + d.getLabel() + ': ' + output + ' = ' + mode + '.');
                }
                modes.put(output, mode);
            }
            Boolean primary = item.get('canBePrimary') == true;
            anyPrimary = anyPrimary || primary;
            rows.add(new MinuteX_Linkable_Object__c(
                Name = d.getLabel().left(80), Object_Name__c = d.getName(),
                Can_Be_Primary__c = primary, Outputs__c = encodeModes(modes), Sort_Order__c = order++));
        }
        if (!rows.isEmpty() && !anyPrimary) {
            throw problem('At least one object must be allowed as the main record.');
        }
        delete [SELECT Id FROM MinuteX_Linkable_Object__c];
        insert rows;
        return state();
    }

    // ------------------------------------------------------------------ helpers

    static Map<String, Object> state() {
        Context ctx = new Context();
        List<MinuteXReceiver.LinkableObject> current = configuredObjects();
        Boolean saved = !current.isEmpty();
        if (!saved) {
            current = MinuteXReceiver.defaultObjects();
        }
        List<Object> objects = new List<Object>();
        for (MinuteXReceiver.LinkableObject o : current) {
            Schema.DescribeSObjectResult d = linkable(o.objectName);
            if (d == null) {
                continue;
            }
            Map<String, String> modes = new Map<String, String>();
            for (String output : MinuteXReceiver.LINK_OUTPUTS) {
                Boolean allowed = o.outputsAllowed.contains(output);
                Boolean onByDefault = o.outputsDefaultOn.contains(output);
                Boolean changeable = o.outputsUserCanChange.contains(output);
                modes.put(output, !allowed ? 'none' : (onByDefault && !changeable) ? 'always'
                                  : onByDefault ? 'on' : 'off');
            }
            objects.add(objectState(d, o.canBePrimary, modes, ctx));
        }
        List<Object> outputs = new List<Object>();
        for (String output : MinuteXReceiver.LINK_OUTPUTS) {
            outputs.add(new Map<String, Object>{
                'value' => output, 'label' => OUTPUT_LABELS.get(output),
                'defaultMode' => MinuteXReceiver.LINK_DEFAULTS.contains(output) ? 'on' : 'off'});
        }
        return new Map<String, Object>{
            'objects' => objects, 'outputs' => outputs, 'available' => available(ctx),
            'saved' => saved, 'connection' => connection()};
    }

    static Map<String, Object> objectState(Schema.DescribeSObjectResult d, Boolean canBePrimary,
                                           Map<String, String> modes, Context ctx) {
        return new Map<String, Object>{
            'objectName' => d.getName(), 'label' => d.getLabel(), 'canBePrimary' => canBePrimary,
            'modes' => modes, 'relatedListReady' => ctx.lookups.containsKey(d.getName()),
            'checks' => checks(d, ctx)};
    }

    /** What the Setup page works out once per load: the packaged lookups, the
     *  objects Tasks can attach to, and what the integration user can read. */
    public class Context {
        public Map<String, Schema.SObjectField> lookups = MinuteXMeetingSync.lookupFields();
        public Set<String> relatable = MinuteXMeetingSync.relatable();
        public IntegrationAccess access = integrationAccess();
    }

    public class IntegrationAccess {
        public Boolean known = false;
        public Boolean hasUser = false;
        public Boolean readsAll = false;
        public Set<String> objects = new Set<String>();
    }

    /** The active users with the MinuteX Integration permission set, and the
     *  objects their permission sets and profile let them read. Not known
     *  when the admin can't see permission assignments. */
    @TestVisible
    static IntegrationAccess integrationAccess() {
        IntegrationAccess a = new IntegrationAccess();
        try {
            String ns = ownPrefix().removeEnd('__');
            ns = String.isBlank(ns) ? null : ns;
            Set<Id> users = new Set<Id>();
            for (PermissionSetAssignment p : [
                    SELECT AssigneeId FROM PermissionSetAssignment
                    WHERE PermissionSet.Name = 'MinuteX_Integration' AND PermissionSet.NamespacePrefix = :ns
                    AND Assignee.IsActive = true LIMIT 20]) {
                users.add(p.AssigneeId);
            }
            a.known = true;
            a.hasUser = !users.isEmpty();
            if (!a.hasUser) {
                return a;
            }
            Set<Id> sets = new Set<Id>();
            for (PermissionSetAssignment p : [SELECT PermissionSetId FROM PermissionSetAssignment
                                              WHERE AssigneeId IN :users]) {
                sets.add(p.PermissionSetId);
            }
            a.readsAll = [SELECT COUNT() FROM PermissionSet WHERE Id IN :sets
                          AND (PermissionsViewAllData = true OR PermissionsModifyAllData = true)] > 0;
            for (ObjectPermissions o : [SELECT SobjectType FROM ObjectPermissions
                                        WHERE ParentId IN :sets AND PermissionsRead = true]) {
                a.objects.add(o.SobjectType);
            }
        } catch (Exception e) {
            a.known = false;
        }
        return a;
    }

    /** What an object still needs, in words: where its meetings show, whether
     *  tasks can attach to it, whether MinuteX can search it. */
    @TestVisible
    static List<Object> checks(Schema.DescribeSObjectResult d, Context ctx) {
        String name = d.getName();
        String label = d.getLabel();
        String plural = d.getLabelPlural();
        List<Object> out = new List<Object>();
        out.add(ctx.lookups.containsKey(name)
            ? check('show', 'ok', 'Meetings show on ' + plural + ' in the MinuteX Meetings component or the '
                    + 'MinuteX Meeting Links related list.')
            : check('show', 'info', 'To show meetings on ' + plural + ': open a ' + label
                    + ' record, click the gear, then Edit Page, and drag the MinuteX Meetings component onto the page.'));
        out.add(name == 'Lead' || name == 'Contact' || ctx.relatable.contains(name)
            ? check('tasks', 'ok', 'Tasks can be attached to ' + plural + '.')
            : check('tasks', 'warn', 'Tasks can\'t be attached to ' + plural + ': turn on Allow Activities '
                    + '(Object Manager, ' + label + ', Edit). Until then they stay on the MinuteX meeting.'));
        IntegrationAccess a = ctx.access;
        if (a.known) {
            out.add(!a.hasUser
                ? check('search', 'warn', 'No user has the MinuteX Integration permission set yet, so MinuteX '
                        + 'can\'t search ' + plural + '.')
                : a.readsAll || a.objects.contains(name)
                ? check('search', 'ok', 'The MinuteX integration user can read ' + plural + ', so the app can find them.')
                : check('search', 'warn', 'The MinuteX integration user can\'t read ' + plural + ', so the app won\'t '
                        + 'find them. Give that user Read access to ' + label + '.'));
        }
        return out;
    }

    static Map<String, Object> check(String key, String status, String text) {
        return new Map<String, Object>{'key' => key, 'status' => status, 'text' => text};
    }

    /** Objects an admin may add: the usual standard ones and any custom object. */
    static List<Object> available(Context ctx) {
        List<String> names = new List<String>(STANDARD_OBJECTS);
        for (String key : Schema.getGlobalDescribe().keySet()) {
            if (key.endsWith('__c')) {
                names.add(key);
            }
        }
        List<Object> out = new List<Object>();
        Set<String> seen = new Set<String>();
        for (String name : names) {
            Schema.DescribeSObjectResult d = linkable(name);
            if (d != null && seen.add(d.getName())) {
                out.add(new Map<String, Object>{'value' => d.getName(), 'label' => d.getLabel() + ' (' + d.getName() + ')',
                                                'relatedListReady' => ctx.lookups.containsKey(d.getName()),
                                                'checks' => checks(d, ctx)});
            }
        }
        return out;
    }

    /** A queryable business object that isn't MinuteX's own or a setting. */
    @TestVisible
    static Schema.DescribeSObjectResult linkable(String name) {
        if (String.isBlank(name)) {
            return null;
        }
        Schema.SObjectType t = Schema.getGlobalDescribe().get(name);
        if (t == null) {
            return null;
        }
        Schema.DescribeSObjectResult d = t.getDescribe();
        String own = ownPrefix();
        if (NOT_LINKABLE.contains(d.getName())
                || !d.isQueryable() || d.isCustomSetting() || !d.isCreateable()
                || d.getName().startsWith(own + 'MinuteX_') || d.getName().endsWith('__mdt')) {
            return null;
        }
        return d;
    }

    /** "DevMinuteX__" in the package, "" unpackaged. */
    static String ownPrefix() {
        return MinuteX_Meeting__c.SObjectType.getDescribe().getName().substringBefore('MinuteX_Meeting__c');
    }

    static Map<String, Object> connection() {
        MinuteX_Webhook_Settings__c s = MinuteX_Webhook_Settings__c.getOrgDefaults();
        Boolean connected = s != null && s.Enabled__c == true && String.isNotBlank(s.Shared_Secret__c);
        List<MinuteX_Delivery_Log__c> last = [SELECT Received_At__c, Status__c FROM MinuteX_Delivery_Log__c
                                              ORDER BY Received_At__c DESC LIMIT 1];
        String text = !connected
            ? 'Not connected yet. In MinuteX, an owner or manager opens Salesforce and taps Connect.'
            : last.isEmpty() ? 'Connected to MinuteX. No meetings received yet.'
            : 'Connected to MinuteX. Last delivery ' + last[0].Received_At__c.format() + ' (' + last[0].Status__c + ').';
        return new Map<String, Object>{'connected' => connected, 'statusText' => text};
    }

    static void requireAdmin() {
        if (!FeatureManagement.checkPermission('MinuteX_Admin')) {
            throw problem('You need the MinuteX Admin permission set to change MinuteX settings.');
        }
    }

    static AuraHandledException problem(String message) {
        AuraHandledException e = new AuraHandledException(message);
        e.setMessage(message);
        return e;
    }
}
