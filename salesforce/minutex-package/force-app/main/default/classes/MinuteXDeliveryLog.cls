/**
 * One MinuteX Delivery Log row per meeting event or file MinuteX delivered:
 * what it was, when, and whether it worked (with the error when it didn't).
 * Written by MinuteXReceiver after the request is handled, so a rolled-back
 * failure is logged too. Never fails the delivery itself. Rows older than
 * RETENTION_DAYS are removed as new ones arrive.
 */
public without sharing class MinuteXDeliveryLog {
    public static final Integer RETENTION_DAYS = 90;

    public static void record(String route, String raw, Integer status, Map<String, Object> body) {
        try {
            Map<String, Object> env = raw == null || raw == '' ? new Map<String, Object>()
                : (Map<String, Object>) JSON.deserializeUntyped(raw);
            Map<String, Object> err = body == null || !(body.get('error') instanceof Map<String, Object>)
                ? new Map<String, Object>() : (Map<String, Object>) body.get('error');
            String meetingId = str(env.get('meeting_id')).left(40);
            Boolean ok = status >= 200 && status < 300;
            Boolean duplicate = ok && body != null && body.get('duplicate') == true;
            String kind = route == 'files' ? 'file' : str(env.get('event'));
            MinuteX_Delivery_Log__c row = new MinuteX_Delivery_Log__c(
                Name = (kind + ' ' + meetingId).trim().left(80),
                MinuteX_Meeting_Id__c = meetingId,
                Kind__c = kind.left(40),
                Event_Id__c = str(env.get('event_id')).left(64),
                Detail__c = (route == 'files' ? str(env.get('filename')) : '').left(255),
                Sequence__c = env.get('sequence') instanceof Integer ? (Integer) env.get('sequence') : null,
                Status__c = duplicate ? 'Duplicate' : ok ? 'Delivered' : 'Failed',
                Http_Status__c = status,
                Error_Code__c = str(err.get('code')).left(80),
                Error_Message__c = str(err.get('message')).left(4000),
                Received_At__c = Datetime.now());
            if (meetingId != '') {
                for (MinuteX_Meeting__c m : [SELECT Id FROM MinuteX_Meeting__c
                                             WHERE MinuteX_Meeting_Id__c = :meetingId LIMIT 1]) {
                    row.MinuteX_Meeting__c = m.Id;
                }
            }
            insert row;
            Datetime cutoff = Datetime.now().addDays(-RETENTION_DAYS);
            delete [SELECT Id FROM MinuteX_Delivery_Log__c WHERE Received_At__c < :cutoff LIMIT 200];
        } catch (Exception e) {
            System.debug(LoggingLevel.WARN, 'MinuteX: delivery not logged: ' + e.getMessage());
        }
    }

    static String str(Object v) {
        return v == null ? '' : String.valueOf(v).trim();
    }
}
