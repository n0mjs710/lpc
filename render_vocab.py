#!/usr/bin/env python3
"""
Pre-render TMS5100 vocabulary to WAV files.

Synthesizes built-in words, or the external vocabulary files specified with
--load, to exact 8 kHz 16-bit PCM mono WAV files.

Usage:
  python3 render_vocab.py [--out DIR] [--load PATH ...]

Options:
  --out DIR        Output directory (default: ../vocab_pcm/ relative to this script)
  --load PATH      Replace built-ins with a .cpp vocabulary file
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

# Pull in the interpolated synthesizer and vocabulary machinery.
sys.path.insert(0, str(Path(__file__).parent))
import tms_speak_interp as tms

_DEFAULT_OUT = Path(__file__).parent.parent / "vocab_pcm"


def save_wav(path: Path, samples: np.ndarray, rate: int = tms.SAMPLE_RATE) -> None:
    """Write a float32 array to a 16-bit mono WAV file."""
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)      # 16-bit
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


def render_all(out_dir: Path, load_paths: list[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    if load_paths:
        n = tms.load_vocab_sources(load_paths)
        print(f"Loaded {n} external words from {', '.join(load_paths)}")
    else:
        print(f"Rendering built-in words only ({len(tms.vocab)} words)")

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
            peak = float(np.max(np.abs(audio)))
            if peak > 0.0:
                audio = audio * (0.9 / peak)

            out_path = out_dir / f"{word}.wav"
            save_wav(out_path, audio)
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
    args = parser.parse_args()
    render_all(Path(args.out), args.load)


if __name__ == "__main__":
    main()
