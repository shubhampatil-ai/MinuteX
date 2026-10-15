/**
 * Idempotency log for the Task write actions (create_task,
 * update_task). Same MinuteX_Site_Request__c rows upsert_meeting uses.
 *
 * WITHOUT SHARING: the receiver's own log object, written whoever is calling.
 * Nothing here touches customer records; the calling service checks the
 * integration user's permissions before any customer write.
 *
 * Only successful requests are logged. A failed request leaves no row, so
 * fixing the input and retrying with the same key is processed normally.
 */
public without sharing class MinuteXSiteIdempotency {

    /** The stored result for this key, or null if it was never processed. */
    public static Map<String, Object> replay(String idempotencyKey) {
        List<MinuteX_Site_Request__c> prior = [
            SELECT Response_JSON__c FROM MinuteX_Site_Request__c
            WHERE Idempotency_Key__c = :idempotencyKey LIMIT 1
        ];
        if (prior.isEmpty()) {
            return null;
        }
        return String.isBlank(prior[0].Response_JSON__c)
            ? new Map<String, Object>()
            : (Map<String, Object>) JSON.deserializeUntyped(prior[0].Response_JSON__c);
    }

    /** Throws DmlException(DUPLICATE_VALUE) when a concurrent request with the
     *  same key committed first; the caller rolls back and replays. */
    public static void record(String idempotencyKey, String requestId, String action,
            String recordId, String outcome, Map<String, Object> result) {
        insert new MinuteX_Site_Request__c(
            Idempotency_Key__c = idempotencyKey,
            Request_Id__c = requestId == null ? null : requestId.left(64),
            Action__c = action,
            Salesforce_Record_Id__c = recordId,
            Outcome__c = outcome,
            Response_JSON__c = JSON.serialize(result),
            Processed_At__c = Datetime.now()
        );
    }

    public static String requireKey(Map<String, Object> envelope) {
        String key = MinuteXSiteReceiver.str(envelope.get('idempotency_key'));
        if (String.isBlank(key) || key.length() > 255) {
            throw MinuteXSiteException.forField(400, 'invalid_request', 'idempotency_key',
                'idempotency_key is required (at most 255 characters).');
        }
        return key;
    }
}
