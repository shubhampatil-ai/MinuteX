"""groq_client — the ONE Groq client for the whole backend.

Extracted verbatim (behaviour-for-behaviour) from the transcribeRecording
Lambda, which has been running this code in production since 2026-08-04. It is
NOT a new provider and NOT a rewrite: the request shape, the 429 policy, the
TPM chunk budgeting and the char-per-token estimate are all the originals. The
only change is that the code now lives in a module both Lambdas vendor, instead
of only inside the S3-triggered one.

Why a shared module rather than a second implementation:
  * transcribeRecording (S3 trigger) needs it for the staged summary/highlights.
  * userApi (JWT) needs it for on-demand documents, Quick AI and chat.
Duplicating the retry/TPM logic in both would guarantee they drift, and the
TPM budget is a per-ACCOUNT quota — both callers spend from the same bucket, so
the pacing rules have to be identical to be correct.

Zero dependencies (stdlib urllib), matching every other Lambda here: nothing to
bundle beyond this file. Secrets come from the environment, never hardcoded.

Rate limits (see MEMORY / the transcribe Lambda's own notes): the free
`on_demand` tier allows 12k TOKENS PER MINUTE. That is a quota, not a context
window — llama-3.3-70b has 131k of context but a single 45-minute transcript
(~20k tokens) still 429s. Everything about chunking here exists for the TPM
quota. Raise GROQ_TPM_LIMIT after a plan upgrade and long inputs get faster
with no code change.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Account tokens-per-minute quota, NOT the model context window. See above.
GROQ_TPM_LIMIT = int(os.environ.get("GROQ_TPM_LIMIT", "12000"))

# Tokens per chunk, leaving room for the system prompt and the model's own
# reply inside one TPM window. Deliberately conservative: exceeding the limit
# costs a full retry cycle, a slightly small chunk costs nothing.
GROQ_CHUNK_TOKENS = max(1000, int(GROQ_TPM_LIMIT * 0.45))

# Chars per token. English prose is ~4; transcripts run denser (speaker labels,
# disfluencies, no long words), so 3.5 errs toward OVER-estimating the token
# count, which is the safe direction.
CHARS_PER_TOKEN = 3.5

# ---------------------------------------------------------------------------
# Single-pass budget — the model's OWN context window, not the account's TPM
# quota. These two constraints are independent:
#   TPM     is a per-account, per-minute QUOTA. Raising the Groq plan raises
#           this with no code change (see GROQ_TPM_LIMIT above).
#   CONTEXT is the model's fixed input+output token ceiling. No plan upgrade
#           changes this — only switching models does.
# On the Developer plan (300K TPM) for llama-3.3-70b-versatile, the context
# window (131,072 tokens) is now the SMALLER of the two, so it — not TPM — is
# what actually bounds how large a transcript can be analyzed in one call.
# Verified against Groq's published model table (2026-08-10):
#   llama-3.3-70b-versatile: context_window=131072, max_completion_tokens=32768
# GROQ_CONTEXT_TOKENS is model-specific and deliberately NOT derived from
# GROQ_MODEL by name matching (fragile) — set it via env if GROQ_MODEL changes
# to a model with a different window.
GROQ_CONTEXT_TOKENS = int(os.environ.get("GROQ_CONTEXT_TOKENS", "131072"))

# Reserve for the model's own reply. The single-pass analysis response is a
# JSON object with a prose summary, a handful of highlights and a task list —
# comfortably under this, but reserved generously since a response that gets
# cut off mid-JSON is unparseable, not just short.
SINGLE_PASS_OUTPUT_RESERVE_TOKENS = 6000

# Headroom below the hard ceiling. est_tokens()/CHARS_PER_TOKEN already err
# high, but a second, explicit margin here means a future prompt tweak (a few
# more instruction lines) can't silently walk the budget right up to the wall.
SINGLE_PASS_SAFETY_MARGIN = 0.85


def single_pass_budget_tokens(system_prompt):
    """Tokens of transcript that fit ONE call alongside `system_prompt`,
    respecting BOTH the model's context window and the account's TPM quota —
    whichever is smaller actually binds. See the constants above.
    """
    sys_tokens = est_tokens(system_prompt)
    context_budget = GROQ_CONTEXT_TOKENS - sys_tokens - SINGLE_PASS_OUTPUT_RESERVE_TOKENS
    tpm_budget = GROQ_TPM_LIMIT - sys_tokens - SINGLE_PASS_OUTPUT_RESERVE_TOKENS
    return max(500, int(min(context_budget, tpm_budget) * SINGLE_PASS_SAFETY_MARGIN))


def single_pass_budget_chars(system_prompt):
    """single_pass_budget_tokens(), converted to characters — what callers
    actually compare a transcript's length against."""
    return int(single_pass_budget_tokens(system_prompt) * CHARS_PER_TOKEN)

# 429 retry policy. Groq states the wait in its own error text; when it doesn't,
# fall back to these sleeps.
GROQ_MAX_RETRIES = int(os.environ.get("GROQ_MAX_RETRIES", "3"))
GROQ_RETRY_BACKOFF = (8, 20, 35)  # seconds, per successive 429

# Per-request HTTP timeout. Kept below any caller's Lambda timeout so a hung
# connection surfaces as our error rather than as an invocation kill.
GROQ_HTTP_TIMEOUT = int(os.environ.get("GROQ_HTTP_TIMEOUT", "120"))


class GroqError(RuntimeError):
    """A Groq call that could not be completed.

    `status` is the HTTP status when there was one (0 for transport errors) and
    `retryable` says whether trying again could plausibly succeed — a 429 or a
    5xx can, a 400/401 as a rule cannot. Callers use this to decide between
    "show Retry" and "show the real problem".

    The rule's one real exception is a json_validate_failed 400, which reports
    a failed GENERATION rather than a malformed request — see
    _is_transient_generation_failure below. It stays retryable=False here,
    because the transport loop must not blind-retry every 400, and is
    recognized by the one caller that knows its payload is worth resending.
    """

    def __init__(self, message, status=0, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# Groq answers 400 with code "json_validate_failed" when the model, in
# json_object mode, produced output that is not valid JSON — classically a raw
# line break inside a string value, and sometimes (as seen on a 5-speaker
# Marathi transcript in production) an EMPTY generation, where
# `failed_generation` comes back as "".
#
# It is a 400, so it is not retryable as a REQUEST — resending a genuinely
# malformed request is pointless and the transport loop is right to refuse.
# But the request here was fine; the sampled tokens were not, and the same
# payload sent again gets a fresh sample. Measured at roughly 1 first attempt
# in 6 on the prose-bearing analysis prompt (see
# test_the_summary_field_states_its_json_string_format), which is far too
# often to answer by abandoning the single-pass path.
_GENERATION_FAILURE_MARKERS = ("json_validate_failed", "failed_generation")


def _is_transient_generation_failure(err):
    """True for a 400 that reports a failed generation rather than a bad
    request — the one 400 worth sending again unchanged."""
    if getattr(err, "status", 0) != 400:
        return False
    text = str(err).lower()
    return any(m in text for m in _GENERATION_FAILURE_MARKERS)


# ---------------------------------------------------------------------------
# Token estimation + transcript splitting. Both are budget arithmetic against
# the TPM quota, and both are the transcribe Lambda's originals.
# ---------------------------------------------------------------------------
def est_tokens(text):
    """Cheap token estimate. No tokenizer ships in the Lambda runtime, and
    being slightly high is safe (a smaller chunk) while being low is not (429)."""
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def split_text(text, budget_tokens):
    """Split text into chunks of at most `budget_tokens`.

    Splits on line boundaries so a "Speaker N:" turn is never cut in half — the
    prompts rely on those labels to attribute points and action items. A single
    line longer than the budget (one very long uninterrupted turn) is hard-split
    on character count as a last resort.
    """
    # -1 mirrors the +1 in est_tokens(), so a full chunk estimates at exactly
    # budget_tokens rather than one over it.
    budget_chars = max(1, int((budget_tokens - 1) * CHARS_PER_TOKEN))
    chunks, cur, cur_len = [], [], 0

    for line in (text or "").splitlines(keepends=True):
        # Pathological single line — break it up rather than blow the budget.
        while len(line) > budget_chars:
            if cur:
                chunks.append("".join(cur))
                cur, cur_len = [], 0
            chunks.append(line[:budget_chars])
            line = line[budget_chars:]
        if cur_len + len(line) > budget_chars and cur:
            chunks.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line)

    if cur:
        chunks.append("".join(cur))
    return [c for c in chunks if c.strip()]


def chunk_budget(system_prompt):
    """Tokens available for the USER content of one call, given the system
    prompt that will ride along with it."""
    return max(500, GROQ_CHUNK_TOKENS - est_tokens(system_prompt))


def pace_seconds(text):
    """How long to wait before spending `text`'s tokens, so a burst of calls
    doesn't re-exhaust the same TPM window the chunking exists to respect."""
    return 60.0 * est_tokens(text) / max(GROQ_TPM_LIMIT, 1)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _post_json(url, headers, payload, timeout):
    """POST a JSON body and return (status, parsed_json_or_text)."""
    data = json.dumps(payload).encode("utf-8")
    # Groq sits behind Cloudflare, which blocks the default "Python-urllib/x.y"
    # User-Agent with a 403 (error 1010). Send a normal UA so it's accepted.
    headers = {"User-Agent": "minutex-ai/1.0", **headers}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Non-2xx: surface status + body so the caller can decide.
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Transport-level failure (DNS, TLS, timeout). Retryable — nothing
        # about the request itself was rejected.
        return 0, f"transport error: {e}"


def api_key():
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise GroqError("GROQ_API_KEY not set", status=0, retryable=False)
    return key


def _retry_wait(attempt, body):
    """Seconds to wait after a 429. Groq states the exact wait in its error
    body ("try again in 6.5s") — prefer that over our own guess."""
    wait = GROQ_RETRY_BACKOFF[min(attempt, len(GROQ_RETRY_BACKOFF) - 1)]
    m = re.search(r"try again in ([0-9.]+)s", str(body))
    if m:
        try:
            return min(60.0, float(m.group(1)) + 1.0)
        except ValueError:
            pass
    return wait


def complete(system_prompt, user_content, label="groq", json_mode=True,
             temperature=0.2, key=None, max_tokens=None, history=None,
             deadline=None):
    """One Groq chat completion, with the 429 backoff policy.

    json_mode forces response_format=json_object so a parse is reliable; pass
    False for the prose generations (documents, chat replies) where JSON would
    only be a wrapper to strip off again.

    `history` is an optional list of prior {role, content} messages inserted
    between the system prompt and `user_content` — used by chat, ignored
    elsewhere.

    `deadline` is a time.monotonic() value past which we must NOT still be
    sleeping. It exists because a caller behind API Gateway has a hard 29s
    ceiling, and a 429 backoff of 8-35s blows straight through it: the
    invocation is killed mid-sleep and the client gets API Gateway's own
    "Internal Server Error" instead of a usable response. Observed in
    production before this parameter existed — a 45k-char transcript spent 22s
    on chunk 1, then a 429 retry ran the clock out at exactly 29000ms.
    With a deadline we give up while there is still time to answer honestly.

    Returns the raw assistant text. Raises GroqError on a non-retryable failure
    or once the retries are exhausted; the caller decides how loud that is.
    """
    messages = [{"role": "system", "content": system_prompt}]
    for m in (history or []):
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)

    # The request/retry loop lives in _chat_once (see the tool-calling section
    # below) so the 429 backoff, the deadline rule and the socket-timeout cap
    # are shared with the agent path rather than written twice.
    message = _chat_once(payload, label, key=key, deadline=deadline)
    return (message.get("content", "") or "").strip()


# ---------------------------------------------------------------------------
# Tool calling — the transport half of the AI agent loop.
#
# complete() above answers with prose; this answers with EITHER prose or a list
# of tool calls the caller is expected to execute and feed back. The retry /
# 429 / deadline policy is deliberately NOT duplicated: _chat_once() is the one
# request loop, and complete() now goes through it too, so a change to the
# backoff rules can never apply to one path and miss the other.
#
# The tool schema is OpenAI-compatible, which is what Groq's API speaks.
# ---------------------------------------------------------------------------
def _chat_once(payload, label, key=None, deadline=None):
    """POST one chat completion with the 429/backoff/deadline policy.

    Returns the raw `message` object (not just its text) so a caller that
    cares about tool_calls can see them. Raises GroqError exactly as
    complete() does.
    """
    headers = {"Authorization": f"Bearer {key or api_key()}",
               "Content-Type": "application/json"}

    last = ""
    for attempt in range(GROQ_MAX_RETRIES + 1):
        timeout = GROQ_HTTP_TIMEOUT
        if deadline is not None:
            timeout = max(1.0, min(timeout, deadline - time.monotonic()))
        status, data = _post_json(GROQ_URL, headers, payload, timeout)
        if status == 200:
            return (data.get("choices") or [{}])[0].get("message", {}) or {}
        last = str(data)[:500]
        if status in (429, 0) or 500 <= status < 600:
            if attempt < GROQ_MAX_RETRIES:
                wait = _retry_wait(attempt, data) if status == 429 else                     GROQ_RETRY_BACKOFF[min(attempt, len(GROQ_RETRY_BACKOFF) - 1)]
                if deadline is not None and time.monotonic() + wait >= deadline:
                    raise GroqError(
                        f"Groq {status} on {label}; no time left to retry "
                        f"(needed {wait}s)", status=status, retryable=True)
                print(f"[groq] {status or 'transport'} on {label} — retrying in "
                      f"{wait}s (attempt {attempt + 1}/{GROQ_MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise GroqError(f"Groq {status} on {label} after "
                            f"{GROQ_MAX_RETRIES} retries: {last}",
                            status=status, retryable=True)
        raise GroqError(f"Groq {status} on {label}: {last}",
                        status=status, retryable=False)

    raise GroqError(f"Groq exhausted retries on {label}: {last}",
                    status=429, retryable=True)


def complete_with_tools(messages, tools, label="agent", key=None,
                        temperature=0.2, max_tokens=None, deadline=None,
                        tool_choice="auto"):
    """One tool-enabled turn. Returns the assistant `message` dict, which has
    EITHER `content` (a final answer) OR `tool_calls` (work for the caller).

    `messages` is the full conversation INCLUDING the system turn and any
    prior tool results — this function is stateless, so the agent loop that
    owns the conversation stays in the caller where the authorization context
    lives. Tools are never executed here: this module has no database access
    and must not grow any.
    """
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    return _chat_once(payload, label, key=key, deadline=deadline)


def complete_json(system_prompt, user_content, label="groq", key=None,
                  temperature=0.2, deadline=None):
    """complete() in JSON mode, parsed. A model that somehow answers non-JSON
    yields {"_raw": text} rather than an exception — the caller's coercer then
    defaults every field, which beats losing the whole generation."""
    text = complete(system_prompt, user_content, label=label, json_mode=True,
                    temperature=temperature, key=key, deadline=deadline)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": text}


# ---------------------------------------------------------------------------
# analyze — the PRIMARY entry point. One direct call for a transcript that
# fits the single-pass budget (see single_pass_budget_chars above); map_reduce
# below is now a FALLBACK for the rare transcript that genuinely exceeds it,
# not the default path.
#
# This exists because chunking was introduced when the account was on the
# free 12K-TPM tier, where almost every real transcript had to be split. On
# the Developer plan (300K TPM), the MODEL'S CONTEXT WINDOW is the binding
# constraint, not TPM — and the vast majority of meetings fit it in one call.
# Splitting a transcript that didn't need splitting was the direct cause of a
# production failure: 3 map chunks succeeded, the reduce call filled in
# title/decisions/action_items but returned an EMPTY summary — a distinct
# model-level quirk of asking it to re-synthesize prose from partial JSONs
# rather than from the original transcript. A single-pass call never puts the
# model in that position at all.
# ---------------------------------------------------------------------------
def analyze(text, map_prompt, reduce_prompt, merge, coerce,
           deadline_seconds, label="analysis", key=None, is_usable=None):
    """Analyze `text` in ONE call when it fits; map_reduce only when it doesn't.

    Same signature and return shape as map_reduce((result, covered, total)) so
    every existing caller works unchanged — this is a drop-in replacement for
    a direct map_reduce() call, not a new contract.

    `is_usable(result) -> bool`, when given, gates a coerced single-pass
    result: a result the caller considers unusable (e.g. a summarize() call
    whose "summary" field came back empty despite a 200) gets ONE retry
    before falling back to map_reduce. This is deliberately the CALLER's
    call — groq_client has no opinion on what "empty" means for a schema it
    doesn't know the shape of — but it lives here rather than duplicated in
    every caller because the retry-then-fallback POLICY (how many attempts,
    what happens after) is shared.
    """
    gkey = key or api_key()
    budget_chars = single_pass_budget_chars(map_prompt)

    fits = len(text or "") <= budget_chars
    reason = (f"{len(text or '')} chars > {budget_chars} single-pass budget"
              if not fits else "")

    if fits:
        deadline = time.monotonic() + deadline_seconds
        for attempt in range(2):
            try:
                result = coerce(complete_json(map_prompt, text, label=label,
                                              key=gkey, deadline=deadline))
            except GroqError as err:
                # A single-pass call can still legitimately fail (429 despite
                # the higher quota, a transient 5xx, a deadline that was
                # already tight for other reasons) — map_reduce's chunk-level
                # retry gives one more real chance rather than failing the
                # whole analysis outright.
                #
                # EXCEPT for a json_validate_failed 400, which is STOCHASTIC:
                # the model emitted a raw line break inside a JSON string (or
                # nothing at all) on this attempt and very likely will not on
                # the next. _chat_once cannot retry it, because a 400 is
                # normally a permanently malformed request — but here the
                # request is well-formed and only the generation failed, so
                # the retry decision belongs to this loop, which knows the
                # payload is worth sending again unchanged. Falling through to
                # map_reduce instead would take the ONE path this function
                # exists to avoid (a reduce over partial JSONs, whose measured
                # failure mode is an empty overview) in response to a fault a
                # plain resend usually clears.
                if _is_transient_generation_failure(err) and attempt == 0:
                    print(f"[groq] {label}: single-pass generation failed "
                          f"({err}) — resending once before falling back")
                    continue
                reason = f"single-pass failed ({err})"
                break
            if is_usable is None or is_usable(result):
                return result, 1, 1
            if attempt == 0:
                print(f"[groq] {label}: single-pass result failed the "
                      f"usability check, retrying once before falling back")
                continue
            reason = "retry also failed the usability check"

    # ONE fall-through for three distinct reasons (overflow, a failed call, a
    # failed usability check). It used to report the overflow arithmetic
    # unconditionally, so a 18k-char transcript that failed for either of the
    # other two reasons logged "18712 chars > 352789 single-pass budget" — a
    # comparison that is plainly false and sent a production investigation
    # after a budget bug that did not exist. Say which reason actually fired.
    print(f"[groq] {label}: falling back to map_reduce ({reason})")
    return map_reduce(text, map_prompt, reduce_prompt, merge, coerce,
                      deadline_seconds, label=label, key=gkey)


# ---------------------------------------------------------------------------
# map_reduce — the long-input FALLBACK strategy, generalized from the
# transcribe Lambda's original summarize(). Callers supply the two prompts
# and the merge, because what "combine two partials" means is domain-specific;
# the TPM pacing, the deadline and the partial-result honesty are not, and
# live here. Call analyze() above instead of this directly unless you
# specifically want to force chunking.
# ---------------------------------------------------------------------------
def _reduce_one_call(partials, reduce_prompt, coerce, label, gkey, deadline):
    """Attempt exactly ONE reduce call over `partials` if they fit the
    window; None if they don't fit, the call failed, or time ran out. Never
    recurses — that is _reduce_group's job."""
    if time.monotonic() >= deadline:
        return None
    # default=str is REQUIRED, not belt-and-braces: a coerced partial can
    # legitimately contain a Decimal (ai_schema stores confidence_score and
    # segment timings that way, because DynamoDB rejects Python floats) and
    # json.dumps raises TypeError on it. Without this, any transcript large
    # enough to reach the reduce step crashed the whole analysis instead of
    # merging — and only on the overflow path, so it would have escaped every
    # normal-sized test.
    reduce_input = json.dumps(partials, ensure_ascii=False, default=str)
    if est_tokens(reduce_input) > chunk_budget(reduce_prompt):
        return None
    try:
        time.sleep(max(0.0, min(pace_seconds(reduce_input),
                                deadline - time.monotonic())))
        return coerce(complete_json(reduce_prompt, reduce_input,
                                    label=f"{label} reduce", key=gkey,
                                    deadline=deadline))
    except GroqError as err:
        print(f"[groq] {label}: reduce failed: {err}")
        return None


def _reduce_group(partials, reduce_prompt, coerce, label, gkey, deadline):
    """Reduce a list of partials into one, recursing if the group itself is
    too large for one TPM window rather than giving up on reducing at all.

    Splits the group in half, reduces each half (recursively, so a group
    that is STILL too large after one split keeps halving), then makes ONE
    attempt to combine the two halved results. That combine step calls
    _reduce_one_call directly rather than _reduce_group again: two ALREADY
    -reduced halves that still don't fit one window can never be shrunk
    further by more halving (each is already a single item), so retrying
    the same halve-then-combine cycle on the same pair forever is an
    infinite loop, not progress — this was caught by a test with a
    deliberately tiny reduce budget before it could reach production.
    Returns None (never raises) on any failure or if there is no time
    left — callers fall back to `merge()` in that case, exactly as before
    this function existed.
    """
    if time.monotonic() >= deadline:
        return None
    if len(partials) == 1:
        return partials[0]
    if len(partials) == 2:
        return _reduce_one_call(partials, reduce_prompt, coerce, label, gkey, deadline)

    direct = _reduce_one_call(partials, reduce_prompt, coerce, label, gkey, deadline)
    if direct is not None:
        return direct

    # Too large for one window — halve and reduce each half first, so a
    # meeting with many chunks still ends up as ONE coherent result instead
    # of falling straight back to concatenation the first time it grows past
    # a single reduce call.
    mid = len(partials) // 2
    left = _reduce_group(partials[:mid], reduce_prompt, coerce, label, gkey, deadline)
    right = _reduce_group(partials[mid:], reduce_prompt, coerce, label, gkey, deadline)
    if left is None or right is None:
        return None
    return _reduce_one_call([left, right], reduce_prompt, coerce, label, gkey, deadline)


def map_reduce(text, map_prompt, reduce_prompt, merge, coerce,
               deadline_seconds, label="analysis", key=None):
    """Analyze `text` in TPM-sized chunks, then reduce the partials into one.

    merge(partials)  -> a single result, de-duplicated (also the fallback if
                        the reduce call fails or won't fit one window)
    coerce(obj)      -> strict schema coercion of one model response

    Returns (result, covered, total). covered < total means the deadline cut
    the tail off — the caller MUST tell the user, because a brief that silently
    covers 60% of a meeting is worse than one that admits it.
    """
    gkey = key or api_key()
    budget = chunk_budget(map_prompt)
    total_tokens = est_tokens(text)

    # The deadline is established up front and handed to EVERY call below, so
    # a 429 backoff inside one chunk can't overrun the caller's ceiling.
    deadline = time.monotonic() + deadline_seconds

    # Short input: one call, no chunking.
    if total_tokens <= budget:
        return coerce(complete_json(map_prompt, text, label=label, key=gkey,
                                    deadline=deadline)), 1, 1

    chunks = split_text(text, budget)
    print(f"[groq] {label}: ~{total_tokens} tokens > {budget} budget — "
          f"map-reduce over {len(chunks)} chunks (TPM {GROQ_TPM_LIMIT})")

    partials = []
    for i, chunk in enumerate(chunks, 1):
        # Stop early rather than let TPM pacing run into the Lambda timeout.
        # Chunks are in order, so an early stop drops the END of the input.
        # The FIRST chunk always runs: some result beats "everything failed".
        if partials and time.monotonic() >= deadline:
            print(f"[groq] {label}: deadline reached after {i - 1}/{len(chunks)} "
                  f"chunks — returning a partial result")
            break
        if i > 1:
            time.sleep(max(0.0, min(pace_seconds(chunk),
                                    deadline - time.monotonic())))
        try:
            partials.append(coerce(complete_json(
                map_prompt, chunk, label=f"{label} chunk {i}/{len(chunks)}",
                key=gkey, deadline=deadline)))
        except GroqError as err:
            # A transient failure (429/5xx/transport) gets ONE retry before
            # we give up on this chunk — map_reduce previously skipped a
            # failed chunk immediately, so a single retry-able hiccup on
            # e.g. chunk 3/6 permanently dropped that slice of the meeting
            # even though complete()'s own backoff had already been
            # exhausted for unrelated reasons (a slow neighbor call eating
            # the deadline, not this chunk being bad). Only retry when
            # there's genuinely time left and the error says trying again
            # could work; a non-retryable error (bad request, no API key)
            # would just fail identically a second time.
            if err.retryable and time.monotonic() < deadline:
                print(f"[groq] {label}: chunk {i}/{len(chunks)} failed, "
                      f"retrying once: {err}")
                try:
                    partials.append(coerce(complete_json(
                        map_prompt, chunk,
                        label=f"{label} chunk {i}/{len(chunks)} retry",
                        key=gkey, deadline=deadline)))
                    continue
                except GroqError as err2:
                    err = err2
            # One bad segment shouldn't lose the rest of the input.
            print(f"[groq] {label}: chunk {i}/{len(chunks)} failed, skipping: {err}")

    if not partials:
        raise GroqError(f"all {len(chunks)} chunks failed for {label}",
                        status=429, retryable=True)

    covered = len(partials)
    stitched = merge(partials)
    if covered == 1:
        return stitched, covered, len(chunks)

    # Reduce so the result reads as one document rather than concatenated
    # segments. _reduce_group recurses (halving) when the partials don't fit
    # one TPM window in a single call, so a meeting with many chunks still
    # gets a real reduce instead of falling straight back to `merge()`'s
    # plain concatenation the first time it grows past one window.
    final = _reduce_group(partials, reduce_prompt, coerce, label, gkey, deadline)
    if final is None:
        return stitched, covered, len(chunks)
    return final, covered, len(chunks)
