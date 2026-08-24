#!/usr/bin/env python3
"""_stt_webhook_policy.py — the IAM policy userApi needs for the STT webhook.

Two genuinely new permissions, both verified as MISSING against the live role
before this was written:

  s3:PutObject on transcripts/*
      The webhook stores each delivered transcript as an S3 object (never in the
      DynamoDB item — a real 45-minute meeting's transcript + timestamps was 96%
      of the 400 KB limit). The pre-existing userApi-upload-presign policy grants
      PutObject only on recordings/*, so without this EVERY webhook delivery
      would fail to persist its transcript.

  secretsmanager:GetSecretValue on the webhook secret
      The HMAC signing secret is what makes a forged transcript impossible. It
      lives in Secrets Manager rather than a Lambda env var for the same reason
      the Salesforce client secret does: an env var is readable by anyone who can
      read the function's configuration.

lambda:InvokeFunction on transcribeRecording is deliberately NOT repeated here —
the userApi-invoke-transcribe policy already grants it for the reprocess route,
and the webhook's async hand-off uses exactly that.

    argv[1]  bucket name
    argv[2]  the webhook secret's ARN
"""
import json
import sys


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: _stt_webhook_policy.py <bucket> <secret_arn>")
    bucket, secret_arn = sys.argv[1], sys.argv[2]
    print(json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "WriteDeliveredTranscripts",
             "Effect": "Allow",
             "Action": ["s3:PutObject"],
             "Resource": f"arn:aws:s3:::{bucket}/transcripts/*"},
            {"Sid": "ReadWebhookSigningSecret",
             "Effect": "Allow",
             "Action": ["secretsmanager:GetSecretValue"],
             "Resource": secret_arn},
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
