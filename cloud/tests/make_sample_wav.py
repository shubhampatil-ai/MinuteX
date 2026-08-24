#!/usr/bin/env python3
# =============================================================
# make_sample_wav.py — generate tests/sample.wav.
#
# Produces a tiny (~0.25s) mono 16-bit 8 kHz WAV containing a soft
# sine tone, using only the stdlib `wave` module. This is just a
# valid payload for the upload tests — not real speech. (For a real
# transcription smoke test in Stage 2, point transcribe.py at an
# actual recording.)
#
#   python tests/make_sample_wav.py
# =============================================================
import math
import struct
import wave
from pathlib import Path

OUT = Path(__file__).parent / "sample.wav"

SAMPLE_RATE = 8000       # Hz
DURATION_S  = 0.25       # seconds
FREQ_HZ     = 440.0      # A4 tone
AMPLITUDE   = 0.3        # 0..1


def main() -> None:
    n = int(SAMPLE_RATE * DURATION_S)
    frames = bytearray()
    for i in range(n):
        sample = AMPLITUDE * math.sin(2 * math.pi * FREQ_HZ * (i / SAMPLE_RATE))
        frames += struct.pack("<h", int(sample * 32767))

    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)      # mono
        w.setsampwidth(2)      # 16-bit
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))

    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
