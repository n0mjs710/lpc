#!/usr/bin/env python3
"""
Pre-render TMS5100 vocabulary to WAV files.

Synthesizes built-in words, or the external vocabulary files specified with
--load, to exact 8 kHz 16-bit PCM mono WAV files. Optional speed adjustment
resamples the PCM so fixed-rate playback sounds like a variable speech clock.

Usage:
  python3 render_vocab.py [--out DIR] [--load PATH ...] [--speed FACTOR]

Options:
  --out DIR        Output directory (default: ../vocab_pcm/ relative to this script)
  --load PATH      Replace built-ins with a .cpp vocabulary file
  --speed FACTOR   Resample output to emulate speech-clock speed/pitch
"""

import argparse
import sys
from pathlib import Path

# Pull in the primary interpolated synthesizer and vocabulary machinery.
sys.path.insert(0, str(Path(__file__).parent))
from lpc_audio import normalize_peak, resample_clock_speed, save_wav, validate_speed
import tms_speak as tms

_DEFAULT_OUT = Path(__file__).parent.parent / "vocab_pcm"


def render_all(out_dir: Path, load_paths: list[str], speed: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    if load_paths:
        n = tms.load_vocab_sources(load_paths)
        print(f"Loaded {n} external words from {', '.join(load_paths)}")
    else:
        print(f"Rendering built-in words only ({len(tms.vocab)} words)")
    if speed != 1.0:
        clock_rate = round(tms.SAMPLE_RATE * speed)
        print(
            f"Applying PCM speed/pitch factor {speed:g} "
            f"(sounds like {clock_rate} Hz LPC playback)"
        )

    words = sorted(tms.vocab)
    ok = 0
    errors = 0

    for i, word in enumerate(words, 1):
        label = f"[{i:4d}/{len(words)}]  {word:<22}"
        try:
            audio = tms.synthesize(tms.vocab[word])

            if audio.size == 0:
                print(f"  {label}  EMPTY — skipping")
                errors += 1
                continue

            # Per-word peak normalization to 0.9 (consistent with speak_words())
            audio = normalize_peak(audio)
            audio = resample_clock_speed(audio, speed)

            out_path = out_dir / f"{word}.wav"
            save_wav(out_path, audio, tms.SAMPLE_RATE)
            dur_ms = round(len(audio) / tms.SAMPLE_RATE * 1000)
            print(f"  {label}  {dur_ms:5d} ms  → {out_path.name}")
            ok += 1

        except Exception as exc:
            print(f"  {label}  ERROR: {exc}")
            errors += 1

    print(f"\nDone.  {ok} WAV files written to {out_dir}/")
    if errors:
        print(f"       {errors} error(s) — see lines marked EMPTY or ERROR above.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out",
        metavar="DIR",
        default=str(_DEFAULT_OUT),
        help=f"output directory (default: {_DEFAULT_OUT})",
    )
    parser.add_argument(
        "--load",
        "-l",
        action="append",
        default=[],
        metavar="PATH",
        help="replace built-ins with one or more .cpp vocabulary files",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="PCM speed/pitch multiplier, e.g. 0.97 or 1.03 (WAV rate stays 8000 Hz)",
    )
    args = parser.parse_args()
    try:
        speed = validate_speed(args.speed, tms.MIN_PLAYBACK_SPEED, tms.MAX_PLAYBACK_SPEED)
    except ValueError as e:
        parser.error(str(e))
    render_all(Path(args.out), args.load, speed)


if __name__ == "__main__":
    main()
