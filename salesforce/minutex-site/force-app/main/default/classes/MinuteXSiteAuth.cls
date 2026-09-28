/**
 * Request authentication for the MinuteX Site receiver. PLUGGABLE: each
 * auth mode is a Verifier registered in VERIFIERS; adding a production mode
 * (for example mTLS-fronted or token-based) is one new Verifier plus one
 * registry entry, with no change to the receiver or the actions.
 *
 * The receiver is reachable anonymously through a Salesforce Site, so this
 * class is the only thing between the internet and the org's Contacts, Users
 * and meeting records. It FAILS CLOSED: with no MinuteX_Site_Settings__c org
 * default, or with Enabled__c unchecked, every request is refused.
 *
 * Modes (MinuteX_Site_Settings__c.Auth_Mode__c):
 *   none         Development only. Anyone who knows the Site URL can call
 *                every action, including people_sync. Never use it in an org
 *                holding real data.
 *   hmac_sha256  MinuteX signs "{timestamp}.{raw body}" with the shared
 *                secret and sends
 *                  X-MinuteX-Timestamp: <unix seconds>
 *                  X-MinuteX-Signature: v1=<lowercase hex HMAC-SHA256>
 *                A request outside the clock-skew window is refused, which
 *                bounds how long a captured request can be replayed.
 */
public without sharing class MinuteXSiteAuth {
    public static final String MODE_NONE = 'none';
    public static final String MODE_HMAC = 'hmac_sha256';
    public static final String TIMESTAMP_HEADER = 'X-MinuteX-Timestamp';
    public static final String SIGNATURE_HEADER = 'X-MinuteX-Signature';
    public static final Integer DEFAULT_MAX_SKEW_SECONDS = 300;

    @TestVisible
    static MinuteX_Site_Settings__c settingsOverride;

    public interface Verifier {
        void verify(RestRequest req, String rawBody, MinuteX_Site_Settings__c s);
    }

    /** Development only: every request is accepted once the receiver is enabled. */
    public class NoneVerifier implements Verifier {
        public void verify(RestRequest req, String rawBody, MinuteX_Site_Settings__c s) {
            return;
        }
    }

    public class HmacVerifier implements Verifier {
        public void verify(RestRequest req, String rawBody, MinuteX_Site_Settings__c s) {
            verifyHmac(req, rawBody, s);
        }
    }

    public static final Map<String, Verifier> VERIFIERS = new Map<String, Verifier>{
        MODE_NONE => new NoneVerifier(),
        MODE_HMAC => new HmacVerifier()
    };

    public static MinuteX_Site_Settings__c settings() {
        if (settingsOverride != null) {
            return settingsOverride;
        }
        return MinuteX_Site_Settings__c.getOrgDefaults();
    }

    public static String mode() {
        MinuteX_Site_Settings__c s = settings();
        return s == null || s.Auth_Mode__c == null ? '' : s.Auth_Mode__c.trim().toLowerCase();
    }

    /** Throws MinuteXSiteException unless the request may proceed. */
    public static void verify(RestRequest req, String rawBody) {
        MinuteX_Site_Settings__c s = settings();
        // getOrgDefaults() returns an empty, unsaved record when nothing is
        // configured, so an absent Id means "never configured" -> refuse.
        Boolean neverConfigured = s == null || (s.Id == null && settingsOverride == null);
        if (neverConfigured || s.Enabled__c != true) {
            throw MinuteXSiteException.of(403, 'receiver_disabled',
                'The MinuteX receiver is not enabled in this Salesforce org.');
        }
        Verifier v = VERIFIERS.get(mode());
        if (v == null) {
            throw MinuteXSiteException.of(403, 'receiver_misconfigured',
                'The MinuteX receiver has no valid authentication mode configured.');
        }
        v.verify(req, rawBody, s);
    }

    static void verifyHmac(RestRequest req, String rawBody, MinuteX_Site_Settings__c s) {
        if (String.isBlank(s.Shared_Secret__c)) {
            throw MinuteXSiteException.of(403, 'receiver_misconfigured',
                'The MinuteX receiver has no shared secret configured.');
        }

        String ts = header(req, TIMESTAMP_HEADER);
        String sig = header(req, SIGNATURE_HEADER);
        if (String.isBlank(ts) || String.isBlank(sig) || !ts.isNumeric()) {
            throw MinuteXSiteException.of(401, 'auth_failed', 'Missing or invalid request signature.');
        }
        Long sent = Long.valueOf(ts);
        Long now = Datetime.now().getTime() / 1000;
        Integer skew = s.Max_Clock_Skew_Seconds__c == null
            ? DEFAULT_MAX_SKEW_SECONDS : s.Max_Clock_Skew_Seconds__c.intValue();
        if (Math.abs(now - sent) > skew) {
            throw MinuteXSiteException.of(401, 'auth_failed', 'Request timestamp is outside the allowed window.');
        }
        String expected = 'v1=' + sign(s.Shared_Secret__c, ts, rawBody);
        if (!constantTimeEquals(expected, sig.trim())) {
            throw MinuteXSiteException.of(401, 'auth_failed', 'Missing or invalid request signature.');
        }
    }

    public static String sign(String secret, String ts, String rawBody) {
        Blob mac = Crypto.generateMac('hmacSHA256',
            Blob.valueOf(ts + '.' + (rawBody == null ? '' : rawBody)),
            Blob.valueOf(secret));
        return EncodingUtil.convertToHex(mac);
    }

    /** Header lookup that ignores case: proxies and the platform differ. */
    public static String header(RestRequest req, String name) {
        if (req == null || req.headers == null) {
            return null;
        }
        if (req.headers.containsKey(name)) {
            return req.headers.get(name);
        }
        String wanted = name.toLowerCase();
        for (String k : req.headers.keySet()) {
            if (k != null && k.toLowerCase() == wanted) {
                return req.headers.get(k);
            }
        }
        return null;
    }

    /** Compares every character so timing does not reveal the prefix match. */
    @TestVisible
    static Boolean constantTimeEquals(String a, String b) {
        if (a == null || b == null || a.length() != b.length()) {
            return false;
        }
        Integer diff = 0;
        for (Integer i = 0; i < a.length(); i++) {
            diff |= a.charAt(i) ^ b.charAt(i);
        }
        return diff == 0;
    }
}
