#!/usr/bin/env python3
"""_merge_env.py — merge KEY=VALUE pairs into a Lambda's LIVE env var map.

Used by scripts/28_deploy_async_stt.sh. A separate file rather than an inline
heredoc because `merged="$(python - <<'PY' ... PY)"` — command substitution
wrapping a heredoc, as scripts/21 does it — is mis-parsed by the bash build on
this machine: it warns "unterminated here-document" and then reports a syntax
error at an unrelated line further down the script.

MERGES rather than replaces, so the values already on the deployed function
(BUCKET_NAME, ELEVENLABS_API_KEY, JWT_SECRET_ARN, …) survive a deploy that only
means to add or change one variable.

    argv[1]  the current env as JSON (aws --query 'Environment.Variables'),
             or "null"/"" when the function has none
    argv[2:] KEY=VALUE pairs to set

Prints {"Variables": {...}} for `aws lambda update-function-configuration
--environment`.
"""
import json
import sys


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    env = json.loads(raw) if raw not in ("null", "", "None") else {}
    if not isinstance(env, dict):
        env = {}
    for pair in sys.argv[2:]:
        key, sep, value = pair.partition("=")
        if not sep:
            # A bare word is almost certainly a quoting mistake in the caller;
            # silently dropping it would deploy a missing env var.
            sys.exit(f"expected KEY=VALUE, got {pair!r}")
        env[key] = value
    print(json.dumps({"Variables": env}))


if __name__ == "__main__":
    main()
