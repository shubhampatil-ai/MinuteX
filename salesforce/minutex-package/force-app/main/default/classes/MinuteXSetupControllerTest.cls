/**
 * The MinuteX Setup tab: only MinuteX admins; the defaults until saved; a
 * save drives what the receiver offers MinuteX; bad input is refused.
 */
@IsTest
private class MinuteXSetupControllerTest {
    static User admin() {
        Profile p = [SELECT Id FROM Profile WHERE Name = 'Standard User' LIMIT 1];
        User u = new User(Alias = 'mxadm', Email = 'adm@minutex.test', EmailEncodingKey = 'UTF-8',
            LastName = 'Admin', LanguageLocaleKey = 'en_US', LocaleSidKey = 'en_US', ProfileId = p.Id,
            TimeZoneSidKey = 'Asia/Kolkata', Username = 'adm' + Datetime.now().getTime() + '@minutex.test');
        System.runAs(new User(Id = UserInfo.getUserId())) {
            insert u;
            PermissionSet ps = new PermissionSet(Name = 'MX_Test_Admin', Label = 'MX Test Admin');
            insert ps;
            insert new SetupEntityAccess(ParentId = ps.Id, SetupEntityId =
                [SELECT Id FROM CustomPermission WHERE DeveloperName = 'MinuteX_Admin'].Id);
            insert new PermissionSetAssignment(AssigneeId = u.Id, PermissionSetId = ps.Id);
        }
        return u;
    }

    static String payload(List<Object> items) {
        return JSON.serialize(items);
    }

    static Map<String, Object> item(String objectName, Boolean primary, Map<String, Object> modes) {
        return new Map<String, Object>{'objectName' => objectName, 'canBePrimary' => primary, 'modes' => modes};
    }

    @IsTest
    static void onlyMinuteXAdminsCanOpenIt() {
        try {
            MinuteXSetupController.getSetup();
            System.assert(false, 'should refuse');
        } catch (AuraHandledException e) {
            System.assert(e.getMessage().contains('MinuteX Admin'), e.getMessage());
        }
    }

    @IsTest
    static void theDefaultsShowUntilSaved() {
        System.runAs(admin()) {
            Map<String, Object> s = MinuteXSetupController.getSetup();
            System.assertEquals(false, s.get('saved'));
            List<Object> objects = (List<Object>) s.get('objects');
            System.assertEquals(4, objects.size(), 'Lead, Contact, Account, Opportunity');
            Map<String, Object> lead = (Map<String, Object>) objects[0];
            System.assertEquals(true, lead.get('relatedListReady'));
            Map<String, Object> modes = (Map<String, Object>) lead.get('modes');
            System.assertEquals('on', modes.get('overview'));
            System.assertEquals('off', modes.get('transcript'));
            System.assert(!((List<Object>) s.get('available')).isEmpty());
            System.assertNotEquals(null, ((Map<String, Object>) s.get('connection')).get('statusText'));
        }
    }

    @IsTest
    static void aSaveIsWhatTheReceiverOffers() {
        System.runAs(admin()) {
            MinuteXSetupController.saveSetup(payload(new List<Object>{
                item('Opportunity', true, new Map<String, Object>{'overview' => 'on', 'mom' => 'always', 'transcript' => 'off'}),
                item('Account', false, new Map<String, Object>{'overview' => 'on'})}));
        }
        List<MinuteXReceiver.LinkableObject> offered = MinuteXReceiver.linkableObjects();
        System.assertEquals(2, offered.size());
        MinuteXReceiver.LinkableObject opp = offered[0];
        System.assertEquals('Opportunity', opp.objectName);
        System.assertEquals(new List<String>{'overview', 'mom', 'transcript'}, opp.outputsAllowed);
        System.assertEquals(new List<String>{'overview', 'mom'}, opp.outputsDefaultOn);
        System.assertEquals(new List<String>{'overview', 'transcript'}, opp.outputsUserCanChange,
                            'an "always" output can\'t be unticked');
        System.assertEquals(false, offered[1].canBePrimary);
        System.assertEquals('overview:on,mom:always,transcript:off',
            [SELECT Outputs__c FROM MinuteX_Linkable_Object__c WHERE Object_Name__c = 'Opportunity'].Outputs__c);
    }

    @IsTest
    static void badSetupsAreRefused() {
        System.runAs(admin()) {
            for (String bad : new List<String>{
                    payload(new List<Object>{item('No_Such_Thing__c', true, new Map<String, Object>())}),
                    payload(new List<Object>{item('Lead', true, new Map<String, Object>{'overview' => 'loud'})}),
                    payload(new List<Object>{item('Lead', false, new Map<String, Object>{'overview' => 'on'})}),
                    payload(new List<Object>{item('User', true, new Map<String, Object>())}),
                    'not json'}) {
                try {
                    MinuteXSetupController.saveSetup(bad);
                    System.assert(false, 'should refuse: ' + bad);
                } catch (AuraHandledException e) {
                    System.assert(String.isNotBlank(e.getMessage()));
                }
            }
        }
        System.assertEquals(0, [SELECT COUNT() FROM MinuteX_Linkable_Object__c]);
    }

    static String status(List<Object> checks, String key) {
        for (Object o : checks) {
            Map<String, Object> c = (Map<String, Object>) o;
            if (c.get('key') == key) {
                return (String) c.get('status');
            }
        }
        return null;
    }

    @IsTest
    static void eachObjectSaysWhatItStillNeeds() {
        MinuteXSetupController.Context ctx = new MinuteXSetupController.Context();
        List<Object> lead = MinuteXSetupController.checks(Lead.SObjectType.getDescribe(), ctx);
        System.assertEquals('ok', status(lead, 'show'), 'Lead has its packaged lookup');
        System.assertEquals('ok', status(lead, 'tasks'), 'a Lead is a Task\'s Name');
        // No packaged lookup and Allow Activities off: the component, and Allow Activities.
        ctx.relatable = new Set<String>();
        List<Object> product = MinuteXSetupController.checks(Product2.SObjectType.getDescribe(), ctx);
        System.assertEquals('info', status(product, 'show'));
        System.assertEquals('warn', status(product, 'tasks'));
        for (Object o : product) {
            System.assert(String.isNotBlank((String) ((Map<String, Object>) o).get('text')));
        }
    }

    @IsTest
    static void theIntegrationUsersAccessIsChecked() {
        MinuteXSetupController.IntegrationAccess none = MinuteXSetupController.integrationAccess();
        System.assertEquals(true, none.known);
        System.assertEquals(false, none.hasUser);
        MinuteXSetupController.Context ctx = new MinuteXSetupController.Context();
        System.assertEquals('warn', status(MinuteXSetupController.checks(Lead.SObjectType.getDescribe(), ctx), 'search'),
                            'no integration user yet');

        Profile p = [SELECT Id FROM Profile WHERE Name = 'Standard User' LIMIT 1];
        User u = new User(Alias = 'mxint', Email = 'int@minutex.test', EmailEncodingKey = 'UTF-8',
            LastName = 'Integration', LanguageLocaleKey = 'en_US', LocaleSidKey = 'en_US', ProfileId = p.Id,
            TimeZoneSidKey = 'Asia/Kolkata', Username = 'int' + Datetime.now().getTime() + '@minutex.test');
        System.runAs(new User(Id = UserInfo.getUserId())) {
            insert u;
            insert new PermissionSetAssignment(AssigneeId = u.Id, PermissionSetId =
                [SELECT Id FROM PermissionSet WHERE Name = 'MinuteX_Integration' LIMIT 1].Id);
        }
        MinuteXSetupController.IntegrationAccess a = MinuteXSetupController.integrationAccess();
        System.assertEquals(true, a.hasUser);
        System.assert(a.objects.contains('Lead'), 'the permission set reads Leads');
        ctx = new MinuteXSetupController.Context();
        System.assertEquals('ok', status(MinuteXSetupController.checks(Lead.SObjectType.getDescribe(), ctx), 'search'));
    }

    @IsTest
    static void modesRoundTrip() {
        Map<String, String> modes = MinuteXSetupController.parseModes('overview:on, mom:always,junk:on,transcript:loud');
        System.assertEquals(new Map<String, String>{'overview' => 'on', 'mom' => 'always'}, modes);
        System.assertEquals('overview:on,mom:always', MinuteXSetupController.encodeModes(modes));
        System.assertEquals(null, MinuteXSetupController.linkable('MinuteX_Meeting__c'), 'MinuteX\'s own objects are not linkable');
    }
}
