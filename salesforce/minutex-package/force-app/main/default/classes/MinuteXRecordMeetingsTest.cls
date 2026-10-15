/**
 * The MinuteX Meetings component: a record's meetings by its stored record
 * id (any object, no lookup needed), newest first, main record flagged; read
 * as the viewer, so a user without MinuteX access is told so.
 */
@IsTest
private class MinuteXRecordMeetingsTest {
    static User user(String permissionSet) {
        Profile p = [SELECT Id FROM Profile WHERE Name = 'Standard User' LIMIT 1];
        User u = new User(Alias = 'mxview', Email = 'view@minutex.test', EmailEncodingKey = 'UTF-8',
            LastName = 'Viewer', LanguageLocaleKey = 'en_US', LocaleSidKey = 'en_US', ProfileId = p.Id,
            TimeZoneSidKey = 'Asia/Kolkata', Username = 'view' + Datetime.now().getTime() + '@minutex.test');
        System.runAs(new User(Id = UserInfo.getUserId())) {
            insert u;
            if (permissionSet != null) {
                insert new PermissionSetAssignment(AssigneeId = u.Id, PermissionSetId =
                    [SELECT Id FROM PermissionSet WHERE Name = :permissionSet LIMIT 1].Id);
            }
        }
        return u;
    }

    @IsTest
    static void listsTheMeetingsLinkedToARecord() {
        Account a = new Account(Name = 'Acme');
        Account other = new Account(Name = 'Other');
        insert new List<Account>{a, other};
        MinuteX_Meeting__c m1 = new MinuteX_Meeting__c(Name = 'Kickoff', MinuteX_Meeting_Id__c = 'm1');
        MinuteX_Meeting__c m2 = new MinuteX_Meeting__c(Name = 'Review', MinuteX_Meeting_Id__c = 'm2');
        insert new List<MinuteX_Meeting__c>{m1, m2};
        insert new List<MinuteX_Meeting_Link__c>{
            new MinuteX_Meeting_Link__c(Name = 'Kickoff', MinuteX_Meeting__c = m1.Id, Link_Key__c = 'm1|' + a.Id,
                Record_Id__c = a.Id, Object__c = 'Account', Primary__c = true,
                Meeting_Date__c = Datetime.now().addDays(-2), Summary__c = 'Scope agreed'),
            new MinuteX_Meeting_Link__c(Name = 'Review', MinuteX_Meeting__c = m2.Id, Link_Key__c = 'm2|' + a.Id,
                Record_Id__c = a.Id, Object__c = 'Account', Meeting_Date__c = Datetime.now()),
            new MinuteX_Meeting_Link__c(Name = 'Review', MinuteX_Meeting__c = m2.Id, Link_Key__c = 'm2|' + other.Id,
                Record_Id__c = other.Id, Object__c = 'Account')};

        System.runAs(user('MinuteX_Meeting_Viewer')) {
            Map<String, Object> r = MinuteXRecordMeetings.getMeetings(a.Id);
            System.assertEquals(2, r.get('total'));
            List<Object> meetings = (List<Object>) r.get('meetings');
            Map<String, Object> newest = (Map<String, Object>) meetings[0];
            Map<String, Object> older = (Map<String, Object>) meetings[1];
            System.assertEquals('Review', newest.get('name'), 'newest first');
            System.assertEquals(m2.Id, newest.get('meetingId'));
            System.assertEquals(true, older.get('primary'));
            System.assertEquals('Scope agreed', older.get('summary'));
            System.assertEquals(0, MinuteXRecordMeetings.getMeetings(null).get('total'), 'no record, no meetings');
        }
    }

    @IsTest
    static void aRecordWithoutMeetingsIsEmpty() {
        Account a = new Account(Name = 'Quiet');
        insert a;
        System.runAs(user('MinuteX_Meeting_Viewer')) {
            System.assertEquals(0, MinuteXRecordMeetings.getMeetings(a.Id).get('total'));
        }
    }

    @IsTest
    static void aUserWithoutMinuteXAccessIsToldSo() {
        Account a = new Account(Name = 'Locked');
        insert a;
        System.runAs(user(null)) {
            try {
                MinuteXRecordMeetings.getMeetings(a.Id);
                System.assert(false, 'should refuse');
            } catch (AuraHandledException e) {
                System.assert(e.getMessage().contains('MinuteX Meeting Viewer'), e.getMessage());
            }
        }
    }
}
