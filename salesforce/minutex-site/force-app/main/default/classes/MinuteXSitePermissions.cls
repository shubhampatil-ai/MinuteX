/**
 * The effective Salesforce permissions of the configured INTEGRATION USER
 * (MinuteX_Site_Settings__c.Integration_Username__c).
 *
 * WHY THIS EXISTS. The receiver is called through a public Salesforce Site,
 * so Apex runs as the Site's guest user, and every describe flag and
 * USER_MODE check would answer for the GUEST, not for anyone meaningful.
 * Instead, the receiver works within the permissions of one named user the
 * customer chooses, computed from Salesforce's own permission data:
 *
 *   object access   ObjectPermissions over the user's permission sets
 *                   (the profile's permission set included), plus
 *                   Modify All Data / View All Data
 *   field access    FieldPermissions (fields that are not permissionable
 *                   follow the object)
 *   Task access     the "Edit Tasks" user permission
 *   record access   UserRecordAccess (sharing), per record
 *
 * Nothing is assumed: a user with no grant on an object cannot see it here,
 * however the org is configured otherwise.
 *
 * LIMITS OF THIS APPROACH (documented in the README): writes are performed by
 * the receiver in system mode AFTER these checks pass. Salesforce does not
 * re-enforce them at DML time for another user. Validation rules and triggers
 * still run. Permission-set-group muting is applied through the group's
 * aggregate permission set.
 */
public without sharing class MinuteXSitePermissions {

    public class ObjectAccess {
        public Boolean read = false;
        public Boolean create = false;
        public Boolean edit = false;
        public Boolean del = false;
    }

    public Id userId;
    public String username;
    public String name;
    Boolean modifyAllData = false;
    Boolean viewAllData = false;
    Boolean editTask = false;
    Set<Id> permissionSetIds = new Set<Id>();
    Map<String, ObjectAccess> objects = new Map<String, ObjectAccess>();
    Map<String, Map<String, Boolean[]>> fieldCache = new Map<String, Map<String, Boolean[]>>();

    static MinuteXSitePermissions cached;

    @TestVisible
    static void reset() {
        cached = null;
    }

    /** The configured integration user's permissions, or a clear refusal. */
    public static MinuteXSitePermissions forIntegrationUser() {
        if (cached != null) {
            return cached;
        }
        MinuteX_Site_Settings__c s = MinuteXSiteAuth.settings();
        String username = s == null || s.Integration_Username__c == null ? '' : s.Integration_Username__c.trim();
        if (String.isBlank(username)) {
            throw MinuteXSiteException.of(409, 'integration_user_not_configured',
                'Set MinuteX Site Settings > Integration Username to the Salesforce user MinuteX should work as.');
        }
        List<User> users = [SELECT Id, Username, Name, IsActive FROM User WHERE Username = :username LIMIT 1];
        if (users.isEmpty() || !users[0].IsActive) {
            throw MinuteXSiteException.of(409, 'integration_user_invalid',
                'The configured integration user does not exist or is inactive.');
        }
        cached = new MinuteXSitePermissions(users[0]);
        return cached;
    }

    MinuteXSitePermissions(User u) {
        userId = u.Id;
        username = u.Username;
        name = u.Name;
        for (PermissionSetAssignment a : [SELECT PermissionSetId FROM PermissionSetAssignment WHERE AssigneeId = :userId]) {
            permissionSetIds.add(a.PermissionSetId);
        }
        for (PermissionSet p : [SELECT PermissionsModifyAllData, PermissionsViewAllData, PermissionsEditTask
                                FROM PermissionSet WHERE Id IN :permissionSetIds]) {
            modifyAllData |= p.PermissionsModifyAllData;
            viewAllData |= p.PermissionsViewAllData;
            editTask |= p.PermissionsEditTask;
        }
        for (ObjectPermissions op : [SELECT SobjectType, PermissionsRead, PermissionsCreate, PermissionsEdit,
                                            PermissionsDelete, PermissionsViewAllRecords, PermissionsModifyAllRecords
                                     FROM ObjectPermissions WHERE ParentId IN :permissionSetIds]) {
            String key = op.SobjectType.toLowerCase();
            ObjectAccess a = objects.containsKey(key) ? objects.get(key) : new ObjectAccess();
            a.read |= op.PermissionsRead || op.PermissionsViewAllRecords;
            a.create |= op.PermissionsCreate;
            a.edit |= op.PermissionsEdit || op.PermissionsModifyAllRecords;
            a.del |= op.PermissionsDelete || op.PermissionsModifyAllRecords;
            objects.put(key, a);
        }
    }

    public Boolean canEditTasks() {
        return editTask || modifyAllData;
    }

    /** Object access, with the org-wide system permissions applied. */
    public ObjectAccess obj(String apiName) {
        String key = apiName.toLowerCase();
        ObjectAccess out = new ObjectAccess();
        ObjectAccess granted = objects.get(key);
        if (granted != null) {
            out.read = granted.read;
            out.create = granted.create;
            out.edit = granted.edit;
            out.del = granted.del;
        }
        if (key == 'task' || key == 'event') {
            // Activities are governed by "Edit Tasks", not ObjectPermissions.
            out.read = true;
            out.create = canEditTasks();
            out.edit = canEditTasks();
            out.del = canEditTasks();
        } else if (key == 'user') {
            out.read = true;
        }
        out.read |= viewAllData || modifyAllData;
        out.create |= modifyAllData;
        out.edit |= modifyAllData;
        out.del |= modifyAllData;
        return out;
    }

    Map<String, Boolean[]> fieldGrants(String apiName) {
        String key = apiName.toLowerCase();
        if (!fieldCache.containsKey(key)) {
            Map<String, Boolean[]> grants = new Map<String, Boolean[]>();
            for (FieldPermissions fp : [SELECT Field, PermissionsRead, PermissionsEdit FROM FieldPermissions
                                        WHERE ParentId IN :permissionSetIds AND SobjectType = :apiName]) {
                String f = fp.Field.substringAfter('.').toLowerCase();
                Boolean[] g = grants.containsKey(f) ? grants.get(f) : new Boolean[]{ false, false };
                g[0] = g[0] || fp.PermissionsRead;
                g[1] = g[1] || fp.PermissionsEdit;
                grants.put(f, g);
            }
            fieldCache.put(key, grants);
        }
        return fieldCache.get(key);
    }

    public Boolean fieldReadable(String objectApiName, DescribeFieldResult f) {
        if (!obj(objectApiName).read) {
            return false;
        }
        if (!f.isPermissionable() || modifyAllData || viewAllData) {
            return true;
        }
        Boolean[] g = fieldGrants(objectApiName).get(f.getName().toLowerCase());
        return g != null && g[0];
    }

    public Boolean fieldEditable(String objectApiName, DescribeFieldResult f) {
        if (!f.isPermissionable() || modifyAllData) {
            return true;
        }
        Boolean[] g = fieldGrants(objectApiName).get(f.getName().toLowerCase());
        return g != null && g[1];
    }

    /** Record ids (of those given, at most 200) the user can read under sharing. */
    public Set<Id> readableRecords(List<Id> recordIds) {
        Set<Id> out = new Set<Id>();
        if (recordIds.isEmpty()) {
            return out;
        }
        if (modifyAllData || viewAllData) {
            out.addAll(recordIds);
            return out;
        }
        for (UserRecordAccess a : [SELECT RecordId, HasReadAccess FROM UserRecordAccess
                                   WHERE UserId = :userId AND RecordId IN :recordIds]) {
            if (a.HasReadAccess) {
                out.add(a.RecordId);
            }
        }
        return out;
    }

    public Boolean canRead(Id recordId) {
        return readableRecords(new List<Id>{ recordId }).contains(recordId);
    }
}
