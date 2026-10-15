/**
 * The MinuteX Site receiver: ONE Apex REST endpoint that MinuteX POSTs to
 * through a Salesforce Site.
 *
 *   POST https://<your-site-domain>/services/apexrest/minutex/v1/
 *
 * Request envelope (JSON):
 *   {
 *     "contract_version": "1.0",
 *     "action": "health" | "upsert_meeting" | "people_sync",
 *     "request_id": "...",          unique per HTTP attempt, for tracing
 *     "idempotency_key": "...",     required for upsert_meeting
 *     "sent_at": "ISO 8601",
 *     "payload": { ... }
 *   }
 *
 * Response (always JSON):
 *   { "ok": true,  "action", "request_id", "duplicate", "receiver_version", "result": {...} }
 *   { "ok": false, "action", "request_id", "receiver_version",
 *     "error": { "code", "message", "field" } }
 *
 * HTTP status is the retry signal MinuteX reads: 2xx done; 400/401/403/404/
 * 413/422 will not succeed on retry; 429/5xx may. See MinuteXSiteException.
 *
 * ONE BOUNDARY. Every action arrives through the public Site and is
 * authenticated by MinuteXSiteAuth (pluggable: auth_mode none for
 * development, hmac_sha256 available, others can be registered). The org
 * actions (org_info, object_schema, record_search, create_task, update_task)
 * work within the permissions of the configured integration user — see
 * MinuteXSitePermissions — never the Site guest's and never "everything".
 *
 * Every response carries this org's id, so MinuteX can refuse an answer from
 * an org other than the one the workspace connected.
 *
 * ANY failure rolls back every write made by the request: a request either
 * fully succeeds or leaves the org untouched.
 */
@RestResource(urlMapping='/minutex/v1/*')
global without sharing class MinuteXSiteReceiver {
    public static final String RECEIVER_VERSION = '1.4.0';
    public static final String CONTRACT_MAJOR = '1.';
    public static final Integer MAX_BODY_CHARS = 3000000;
    static final Pattern REQUEST_ID = Pattern.compile('[A-Za-z0-9_\\-]{1,64}');
    public static final List<String> ACTIONS = new List<String>{
        'health', 'upsert_meeting', 'people_sync',
        'org_info', 'object_schema', 'record_search', 'create_task', 'update_task'
    };

    @HttpPost
    global static void handlePost() {
        RestRequest req = RestContext.request;
        RestResponse res = RestContext.response;
        String action = '';
        String requestId = '';
        Integer status = 200;
        Map<String, Object> body;
        Savepoint sp = Database.setSavepoint();
        try {
            String raw = req.requestBody == null ? '' : req.requestBody.toString();
            if (raw.length() > MAX_BODY_CHARS) {
                throw MinuteXSiteException.of(413, 'payload_too_large',
                    'The request body exceeds ' + MAX_BODY_CHARS + ' characters.');
            }
            // Authenticate BEFORE parsing, so an unauthenticated caller
            // learns nothing about what a valid request looks like.
            MinuteXSiteAuth.verify(req, raw);

            Map<String, Object> envelope = parseEnvelope(raw);
            action = str(envelope.get('action'));
            requestId = str(envelope.get('request_id'));
            body = success(action, requestId, dispatch(action, envelope));
        } catch (MinuteXSiteException e) {
            Database.rollback(sp);
            status = e.statusCode;
            body = failure(action, requestId, e.code, e.getMessage(), e.field);
        } catch (DmlException e) {
            Database.rollback(sp);
            StatusCode dmlStatus = e.getDmlType(0);
            if (PERMISSION_CODES.contains(dmlStatus)) {
                // The platform refused the write (e.g. a lookup to a record
                // the owner cannot see).
                status = 403;
                body = failure(action, requestId, 'permission_denied',
                    'Salesforce permissions do not allow this for this user: ' + e.getDmlMessage(0), '');
            } else if (dmlStatus == StatusCode.UNABLE_TO_LOCK_ROW) {
                status = 503;
                body = failure(action, requestId, 'record_locked',
                    'The record is locked by another operation; retry.', '');
            } else {
                // A validation rule, trigger or required field in THIS org
                // refused the write. Retrying the same data cannot succeed.
                status = 422;
                body = failure(action, requestId, 'salesforce_rejected',
                    String.valueOf(dmlStatus) + ': ' + e.getDmlMessage(0), '');
            }
        } catch (SecurityException e) {
            Database.rollback(sp);
            status = 403;
            body = failure(action, requestId, 'permission_denied',
                'Salesforce permissions do not allow this for this user: ' + e.getMessage(), '');
        } catch (Exception e) {
            Database.rollback(sp);
            System.debug(LoggingLevel.ERROR, 'MinuteX receiver failure: ' + e.getTypeName()
                + ': ' + e.getMessage() + '\n' + e.getStackTraceString());
            status = 500;
            body = failure(action, requestId, 'internal_error',
                'The receiver could not process the request.', '');
        }
        res.statusCode = status;
        res.addHeader('Content-Type', 'application/json');
        res.addHeader('Cache-Control', 'no-store');
        res.responseBody = Blob.valueOf(JSON.serialize(body));
    }

    static Map<String, Object> parseEnvelope(String raw) {
        Object parsed;
        try {
            parsed = JSON.deserializeUntyped(raw);
        } catch (Exception e) {
            throw MinuteXSiteException.of(400, 'malformed_json', 'The request body is not valid JSON.');
        }
        if (!(parsed instanceof Map<String, Object>)) {
            throw MinuteXSiteException.of(400, 'malformed_request', 'The request body must be a JSON object.');
        }
        Map<String, Object> envelope = (Map<String, Object>) parsed;

        String action = str(envelope.get('action'));
        if (!ACTIONS.contains(action)) {
            throw MinuteXSiteException.forField(400, 'unsupported_action', 'action',
                'action must be one of ' + String.join(ACTIONS, ', ') + '.');
        }
        String version = str(envelope.get('contract_version'));
        if (String.isNotBlank(version) && !version.startsWith(CONTRACT_MAJOR)) {
            throw MinuteXSiteException.forField(422, 'unsupported_contract_version', 'contract_version',
                'This receiver supports contract version 1.x; received ' + version.left(20) + '.');
        }
        String requestId = str(envelope.get('request_id'));
        if (action != 'health' && !REQUEST_ID.matcher(requestId).matches()) {
            throw MinuteXSiteException.forField(400, 'invalid_request', 'request_id',
                'request_id is required (1-64 characters: letters, digits, _ or -).');
        }
        return envelope;
    }

    static final Set<StatusCode> PERMISSION_CODES = new Set<StatusCode>{
        StatusCode.INSUFFICIENT_ACCESS_OR_READONLY,
        StatusCode.INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY,
        StatusCode.CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY
    };

    static Map<String, Object> dispatch(String action, Map<String, Object> envelope) {
        if (action == 'health') {
            MinuteX_Site_Settings__c s = MinuteXSiteAuth.settings();
            return new Map<String, Object>{
                'status' => 'ok',
                'receiver' => 'minutex-site',
                'receiver_version' => RECEIVER_VERSION,
                'contract_versions' => new List<String>{ '1.0', '1.1', '1.2' },
                'actions' => ACTIONS,
                'org_id' => UserInfo.getOrganizationId(),
                'integration_user_configured' => s != null && String.isNotBlank(s.Integration_Username__c),
                'auth_mode' => MinuteXSiteAuth.mode(),
                'max_transcript_chars' => MinuteXSiteMeetings.MAX_TRANSCRIPT_CHARS
            };
        }
        if (action == 'org_info') {
            return MinuteXSiteSchema.orgInfo();
        }
        if (action == 'object_schema') {
            return MinuteXSiteSchema.objectSchema(envelope);
        }
        if (action == 'record_search') {
            return MinuteXSiteRecords.search(envelope);
        }
        if (action == 'create_task') {
            return MinuteXSiteTasks.createTask(envelope);
        }
        if (action == 'update_task') {
            return MinuteXSiteTasks.updateTask(envelope);
        }
        if (action == 'upsert_meeting') {
            MinuteXSiteMeetings.Outcome out = MinuteXSiteMeetings.upsertMeeting(envelope);
            Map<String, Object> result = out.result.clone();
            result.put('duplicate', out.duplicate);
            return result;
        }
        return MinuteXSitePeople.sync(envelope);
    }

    static Map<String, Object> success(String action, String requestId, Map<String, Object> result) {
        Boolean duplicate = result.get('duplicate') == true;
        result.remove('duplicate');
        return new Map<String, Object>{
            'ok' => true,
            'action' => action,
            'request_id' => requestId,
            'duplicate' => duplicate,
            'receiver_version' => RECEIVER_VERSION,
            // Not a secret: lets MinuteX confirm every answer came from the
            // org this workspace connected.
            'org_id' => UserInfo.getOrganizationId(),
            'result' => result
        };
    }

    static Map<String, Object> failure(String action, String requestId, String code, String message, String field) {
        return new Map<String, Object>{
            'ok' => false,
            'action' => action,
            'request_id' => requestId,
            'receiver_version' => RECEIVER_VERSION,
            'error' => new Map<String, Object>{
                'code' => code,
                'message' => message,
                'field' => field == null ? '' : field
            }
        };
    }

    // ---- small JSON helpers shared by the services ------------------------

    public static String str(Object value) {
        if (value == null) {
            return '';
        }
        if (value instanceof String) {
            return ((String) value).trim();
        }
        return String.valueOf(value).trim();
    }

    public static Map<String, Object> obj(Object value, String field) {
        if (!(value instanceof Map<String, Object>)) {
            throw MinuteXSiteException.forField(400, 'malformed_request', field, field + ' must be a JSON object.');
        }
        return (Map<String, Object>) value;
    }

    public static Map<String, Object> optObj(Object value, String field) {
        return value == null ? new Map<String, Object>() : obj(value, field);
    }

    public static List<Object> optList(Object value, String field) {
        if (value == null) {
            return new List<Object>();
        }
        if (!(value instanceof List<Object>)) {
            throw MinuteXSiteException.forField(400, 'malformed_request', field, field + ' must be a JSON array.');
        }
        return (List<Object>) value;
    }
}
