/**
 * The MinuteX Meetings component (lwc/minutexRecordMeetings) on any record
 * page: the meetings linked to that record. It finds them by the record id
 * every MinuteX Meeting Link stores, so it works on any object, standard or
 * custom, with no lookup field per object.
 *
 * Runs as the person viewing the page: their sharing and field access apply.
 */
public with sharing class MinuteXRecordMeetings {
    static final Integer MAX_ROWS = 50;

    @AuraEnabled(cacheable=true)
    public static Map<String, Object> getMeetings(Id recordId) {
        if (recordId == null) {
            return new Map<String, Object>{'total' => 0, 'meetings' => new List<Object>()};
        }
        String key = String.valueOf(recordId);
        List<Object> meetings = new List<Object>();
        try {
            for (MinuteX_Meeting_Link__c l : [
                    SELECT Id, Name, MinuteX_Meeting__c, Meeting_Date__c, Primary__c, Summary__c
                    FROM MinuteX_Meeting_Link__c
                    WHERE Record_Id__c = :key
                    WITH USER_MODE
                    ORDER BY Meeting_Date__c DESC NULLS LAST, CreatedDate DESC
                    LIMIT :MAX_ROWS]) {
                meetings.add(new Map<String, Object>{
                    'id' => l.Id, 'meetingId' => l.MinuteX_Meeting__c, 'name' => l.Name,
                    'date' => l.Meeting_Date__c, 'primary' => l.Primary__c == true,
                    'summary' => l.Summary__c});
            }
            Integer total = meetings.size() < MAX_ROWS ? meetings.size()
                : [SELECT COUNT() FROM MinuteX_Meeting_Link__c WHERE Record_Id__c = :key WITH USER_MODE];
            return new Map<String, Object>{'total' => total, 'meetings' => meetings};
        } catch (QueryException e) {
            String text = 'You don\'t have access to MinuteX meetings. Ask your admin for the MinuteX Meeting Viewer permission set.';
            AuraHandledException a = new AuraHandledException(text);
            a.setMessage(text);
            throw a;
        }
    }
}
