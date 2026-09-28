/**
 * A request the MinuteX Site receiver refuses on purpose, carrying the HTTP
 * status and the stable machine-readable `code` MinuteX branches on.
 *
 * MinuteX classifies retries from the STATUS (5xx/429 retried, 4xx not), so
 * the status chosen at each throw site is a retry decision, not decoration.
 */
public class MinuteXSiteException extends Exception {
    public Integer statusCode = 400;
    public String code = 'invalid_request';
    public String field = '';

    public static MinuteXSiteException of(Integer statusCode, String code, String message) {
        MinuteXSiteException e = new MinuteXSiteException(message);
        e.statusCode = statusCode;
        e.code = code;
        return e;
    }

    public static MinuteXSiteException forField(Integer statusCode, String code, String field, String message) {
        MinuteXSiteException e = of(statusCode, code, message);
        e.field = field;
        return e;
    }
}
