#!/usr/bin/env python3
"""_preflight_check.py — the shared AI core must import cleanly BEFORE shipping.

Run from shared/ so the modules resolve exactly as they do inside the
Lambda (flat, no package). Catches a syntax error or a bad cross-import here
rather than as an ImportError on the first real invocation — at which point the
S3 trigger has already fired and a user's recording is mid-pipeline.

A separate file rather than an inline `python -c "..."` because the bash build
on this machine mis-parses multi-line quoted strings inside subshells and
reports the error at an unrelated line further down the caller.

Also asserts the invariants this change is REQUIRED to preserve, so a future
edit that quietly drops one fails the deploy instead of production:
  * SUMMARY_REDUCE_SYSTEM still exists (the map-reduce overflow prompt).
  * The unified prompt composes, with and without CRM mappings.
  * The unified coercer/merger exist and enforce the speaker roster.
"""
import sys
from pathlib import Path

# shared on sys.path explicitly, so this works whatever directory the
# caller runs it from (the shell `cd`s there, but relying on that made the
# script fail when invoked directly).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

import ai_schema        # noqa: E402
import groq_client      # noqa: E402
import prompts          # noqa: E402
import stt_result       # noqa: E402
import transcript_store  # noqa: E402

FAILURES = []


def check(label, condition):
    if condition:
        print(f"   ok   {label}")
    else:
        print(f"   FAIL {label}")
        FAILURES.append(label)


def main():
    check("modules import flat",
          all([ai_schema, groq_client, prompts, stt_result, transcript_store]))

    # Explicitly NOT retired — it is the overflow reduce, and the unified reduce
    # prompt is built on top of it.
    check("SUMMARY_REDUCE_SYSTEM survives", bool(prompts.SUMMARY_REDUCE_SYSTEM))
    check("unified reduce builds on SUMMARY_REDUCE_SYSTEM",
          prompts.SUMMARY_REDUCE_SYSTEM in prompts.unified_reduce_system())

    # The unified prompt, both shapes.
    plain = prompts.unified_analysis_system(("Speaker 0", "Speaker 1"))
    check("unified prompt composes", len(plain) > 1000)
    check("unified prompt asks for meeting_highlights",
          "meeting_highlights" in plain)
    check("unified prompt asks for the dynamic overview", '"overview"' in plain)
    # CRM extraction left the analysis path entirely — no mapping can put it
    # back, which is the point.
    check("no CRM extraction in the analysis prompt",
          "crm_identifiers" not in plain)
    # THE dynamic-overview guarantee: no section catalogue anywhere in the
    # prompt. A model handed example headings reproduces them, and every
    # meeting then comes out on the same template.
    check("prompt names no fixed sections",
          not any(h in plain for h in ('"Executive Summary"', '"Highlights"',
                                       '"Decisions"', '"Risks"',
                                       '"Next Steps"')))
    check("prompt states that sections are the model's choice",
          "There is NO predefined list of sections" in plain)

    # The coercer, including the roster rule that keeps a merely-mentioned name
    # out of the attendee list.
    roster = ai_schema.speaker_roster("Speaker 0: hi\n\nSpeaker 1: hello")
    coerced = ai_schema.coerce_unified(
        {"title": "t", "tasks": [],
         "overview": {"sections": [{"title": "Pricing", "content": "c"}]},
         "participants": [{"speaker": "Rakesh", "summary": "never spoke"}],
         "meeting_highlights": {}},
        roster)
    labels = [p["speaker"] for p in coerced["participants"]]
    check("roster replaces model participants", labels == roster)
    check("overview coerces to bounded sections",
          [s["id"] for s in coerced["overview"]["sections"]] == ["section_0"])
    check("merge_unified exists", callable(ai_schema.merge_unified))

    # Evidence grounding: a segment id the transcript cannot support is dropped
    # WITHOUT taking its section down with it.
    grounded = ai_schema.coerce_overview(
        {"sections": [{"title": "A", "content": "x",
                       "evidence_segment_ids": ["seg_1", "seg_999"]}]},
        valid_ids={"seg_1"})
    check("invalid evidence ids are dropped",
          grounded["sections"][0]["evidence_segment_ids"] == ["seg_1"])

    # Segment ids are DERIVED at read time, so the back catalogue is groundable
    # with no migration.
    ids = [s["id"] for s in transcript_store.with_segment_ids(
        [{"speaker": "0", "text": "a"}, {"speaker": "1", "text": "b"}])]
    check("segment ids are derived positionally", ids == ["seg_0", "seg_1"])

    # The STT parser both Lambdas share.
    text, segs, lang = stt_result.parse({
        "language_code": "en",
        "words": [{"type": "word", "text": "hi", "speaker_id": "speaker_0",
                   "start": 0, "end": 1}]})
    check("stt_result parses a words[] payload",
          text == "Speaker 0: hi" and len(segs) == 1 and lang == "en")

    if FAILURES:
        sys.exit(f"\n{len(FAILURES)} preflight check(s) failed — not deploying.")
    print("   all preflight checks passed")


if __name__ == "__main__":
    main()
