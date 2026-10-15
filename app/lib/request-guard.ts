// lib/request-guard.ts — the safety rules lib/api.ts request() applies to
// every call: a timeout, a per-route circuit breaker, and the development-only
// call counter. Pure (no React Native imports) so node tests can pin them.
//
// WHY THIS EXISTS. On 8 Oct 2026 a client loop sent ~200 POST /me/push-tokens
// a second. The account's 10 Lambda slots filled, every other request got a
// 503, and nobody could see which route it was. These rules make the next
// loop cheap (the breaker stops it hitting the network), finite (a hung call
// ends), and visible on the developer's desk (the counter names the route).

/** API Gateway's HTTP API ends every integration at 30 s; past that no answer
 *  can arrive, so waiting longer only holds a spinner. A little over 30 s so
 *  a slow-but-valid AI call (they can take 20–29 s) is never cut short. */
export const REQUEST_TIMEOUT_MS = 35_000;

/** Overload answers in this many calls within the window open the breaker. */
export const BREAKER_THRESHOLD = 3;
export const BREAKER_WINDOW_MS = 10_000;
/** How long an opened breaker refuses calls, unless Retry-After says longer;
 *  doubled each time a trial call fails, never beyond the max. */
export const BREAKER_OPEN_MS = 30_000;
export const BREAKER_MAX_OPEN_MS = 5 * 60_000;

/** Development only: one route called more often than this in one counter
 *  window is reported as a probable loop. */
export const CALL_COUNTER_WINDOW_MS = 10_000;
export const CALL_COUNTER_WARN_AT = 50;

/** Refuse this call before the network because the phone is offline? Only
 *  writes, and only when the phone is KNOWN to be offline: reads go ahead
 *  (the cache answers them), and an unknown state never blocks anything. */
export function refusedOffline(method: string, knownOffline: boolean): boolean {
  return knownOffline && (method || "GET").toUpperCase() !== "GET";
}

/** The answers that mean "the server is overloaded", not "your request is
 *  wrong": throttled (429), and the gateway's 502/503/504. */
export function isOverloadStatus(status: number): boolean {
  return status === 429 || status === 502 || status === 503 || status === 504;
}

/**
 * The route a call belongs to, with ids removed: "GET /recordings/:id".
 * Used to key the breaker and the counter. A segment counts as an id when it
 * holds a digit, an encoded character (recording keys are encodeURIComponent'd,
 * so "/" inside one arrives as %2F), "@", or is long (tokens).
 */
export function routeKey(method: string, path: string): string {
  const clean = String(path || "").split("?")[0].split("#")[0];
  const segs = clean.split("/").filter(Boolean).map((s) =>
    /[0-9%@]/.test(s) || s.length >= 24 ? ":id" : s);
  return `${(method || "GET").toUpperCase()} /${segs.join("/")}`;
}

/** A Retry-After header (seconds, or an HTTP date) as milliseconds from
 *  `now`; null when absent or unreadable. */
export function retryAfterMs(header: string | null | undefined, now: number): number | null {
  const v = String(header ?? "").trim();
  if (!v) return null;
  if (/^\d+(\.\d+)?$/.test(v)) return Math.round(Number(v) * 1000);
  const at = Date.parse(v);
  return Number.isFinite(at) ? Math.max(0, at - now) : null;
}

/** What one finished call says about the server's health. */
export type CallOutcome =
  | "ok"        // it answered normally (any status that is not overload or 5xx)
  | "overload"  // 429/502/503/504, or no answer within REQUEST_TIMEOUT_MS
  | "neutral";  // says nothing either way (offline, a 500 from a bug)

type RouteState = {
  failures: number[];   // timestamps of recent overload outcomes
  openUntil: number;    // 0 = closed
  openMs: number;       // the last open duration, for doubling
  trial: boolean;       // a half-open trial call is in flight
};

/**
 * One breaker per route. CLOSED: calls pass; BREAKER_THRESHOLD overload
 * outcomes within BREAKER_WINDOW_MS open it. OPEN: calls are refused without
 * touching the network. When the open time passes, ONE trial call goes
 * through (half-open): success closes the breaker, an overload re-opens it for
 * twice as long. Per route, so one broken endpoint never blocks the others.
 */
export class CircuitBreaker {
  private routes = new Map<string, RouteState>();

  private state(key: string): RouteState {
    let s = this.routes.get(key);
    if (!s) { s = { failures: [], openUntil: 0, openMs: 0, trial: false }; this.routes.set(key, s); }
    return s;
  }

  /** May a call to `key` go out now? When not, how long until it may. */
  check(key: string, now: number): { open: false } | { open: true; retryInMs: number } {
    const s = this.routes.get(key);
    if (!s || !s.openUntil) return { open: false };
    if (now < s.openUntil) return { open: true, retryInMs: s.openUntil - now };
    // Open time is over: let exactly one trial through.
    if (s.trial) return { open: true, retryInMs: 1000 };
    s.trial = true;
    return { open: false };
  }

  /** Report how a call that check() let through ended. */
  record(key: string, now: number, outcome: CallOutcome, retryAfter: number | null = null): void {
    const s = this.state(key);
    const wasTrial = s.trial;
    s.trial = false;
    if (outcome === "ok") {
      this.routes.delete(key);
      return;
    }
    if (outcome === "neutral") return;
    s.failures = s.failures.filter((t) => now - t < BREAKER_WINDOW_MS);
    s.failures.push(now);
    if (wasTrial || s.failures.length >= BREAKER_THRESHOLD) {
      const base = wasTrial && s.openMs ? s.openMs * 2 : BREAKER_OPEN_MS;
      s.openMs = Math.min(Math.max(base, retryAfter ?? 0), BREAKER_MAX_OPEN_MS);
      s.openUntil = now + s.openMs;
      s.failures = [];
    }
  }

  /** Test/diagnostic view of a route. */
  isOpen(key: string, now: number): boolean {
    const s = this.routes.get(key);
    return !!s && s.openUntil > now;
  }
}

/**
 * Run `fn` over `items` with at most `limit` calls in flight, results in input
 * order. A screen that needs N per-item requests must not fire them all at
 * once: the Calendar's 12 parallel highlight calls alone outnumbered the
 * account's 10 Lambda slots. A rejected call rejects the whole batch, like
 * Promise.all — callers that want best-effort catch inside `fn`.
 */
export async function mapLimit<T, R>(
  items: readonly T[], limit: number, fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const out = new Array<R>(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  };
  const n = Math.max(1, Math.min(Math.floor(limit) || 1, items.length));
  await Promise.all(Array.from({ length: n }, worker));
  return out;
}

/**
 * The development-only call counter's summary for one window:
 * "12× GET /me | 3× GET /recordings" (busiest first), and the routes over
 * CALL_COUNTER_WARN_AT — the probable loops.
 */
export function summarizeCalls(counts: Map<string, number>): { line: string; hot: string[] } {
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  return {
    line: rows.map(([k, n]) => `${n}× ${k}`).join(" | "),
    hot: rows.filter(([, n]) => n > CALL_COUNTER_WARN_AT).map(([k, n]) => `${n}× ${k}`),
  };
}
