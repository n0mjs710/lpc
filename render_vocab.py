#!/usr/bin/env python3
"""
Pre-render TMS5220 vocabulary to WAV files.

Synthesizes built-in words, or the external vocabulary files specified with
--load, to 16-bit PCM mono WAV files (8 kHz unless --rate says otherwise).
An optional simulated LPC clock resamples the PCM so fixed-rate playback
sounds like a variable speech clock. Setting --lpf or --dc passes the output
through a model of the chip's DAC and the analog stage after it: a DC blocker
and a Butterworth low-pass. With neither set, the WAVs hold the raw chip output.

Usage:
  python3 render_vocab.py [--out DIR] [--load PATH ...] [--clock HZ]
                          [--rate HZ] [--lpf HZ] [--poles N] [--dc HZ]

Options:
  --out DIR        Output directory (default: ./vocab_pcm/ relative to this script)
  --load PATH      Replace built-ins with a .cpp vocabulary file
  --clock HZ       Simulated LPC sample clock (default: 8000)
  --rate HZ        WAV sample rate, 8000-96000 (default: 8000)
  --lpf HZ         Output low-pass cutoff, 0 = off (default: off)
  --poles N        Output low-pass poles, 1-8 (default: 4)
  --dc HZ          Output DC blocker cutoff, 0 = off (default: off)
"""

import argparse
import sys
from pathlib import Path

# Pull in the primary interpolated synthesizer and vocabulary machinery.
sys.path.insert(0, str(Path(__file__).parent))
from lpc_audio import (
    DEFAULT_DC_HZ,
    DEFAULT_LPF_HZ,
    DEFAULT_LPF_POLES,
    analog_output,
    describe_filter,
    normalize_peak,
    resample,
    save_wav,
    validate_clock,
    validate_filter,
)
import tms_speak as tms

_DEFAULT_OUT = Path(__file__).parent / "vocab_pcm"


def render_all(
    out_dir: Path,
    load_paths: list[str],
    clock_hz: float,
    rate_hz: int,
    lpf_hz: float,
    poles: int,
    dc_hz: float,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    if load_paths:
        n = tms.load_vocab_sources(load_paths)
        print(f"Loaded {n} external words from {', '.join(load_paths)}")
    else:
        print(f"Rendering built-in words only ({len(tms.vocab)} words)")
    if clock_hz != tms.SAMPLE_RATE:
        print(f"Simulating {clock_hz:g} Hz LPC clock")
    print(f"WAV sample rate: {rate_hz} Hz")
    print(f"Output filter: {describe_filter(lpf_hz, poles, dc_hz)}")

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

            # Run the DAC/analog model at the chip clock, then bring the result
            # to the WAV rate
            audio, model_rate = analog_output(audio, clock_hz, lpf_hz, poles, dc_hz)
            audio = resample(audio, model_rate, rate_hz)
            # Per-word peak normalization to 0.9 (consistent with speak_words())
            audio = normalize_peak(audio)

            out_path = out_dir / f"{word}.wav"
            save_wav(out_path, audio, rate_hz)
            dur_ms = round(len(audio) / rate_hz * 1000)
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
        "--clock",
        type=float,
        default=tms.SAMPLE_RATE,
        metavar="HZ",
        help=f"simulated LPC sample clock in Hz, e.g. 7760 or 8240 "
             f"(default: {tms.SAMPLE_RATE})",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=tms.SAMPLE_RATE,
        metavar="HZ",
        help=f"WAV sample rate in Hz, 8000-96000 (default: {tms.SAMPLE_RATE})",
    )
    parser.add_argument(
        "--lpf",
        type=float,
        default=DEFAULT_LPF_HZ,
        metavar="HZ",
        help="output low-pass cutoff in Hz, 0 = off (default: off)",
    )
    parser.add_argument(
        "--poles",
        type=int,
        default=DEFAULT_LPF_POLES,
        metavar="N",
        help=f"output low-pass poles, 1-8 (default: {DEFAULT_LPF_POLES})",
    )
    parser.add_argument(
        "--dc",
        type=float,
        default=DEFAULT_DC_HZ,
        metavar="HZ",
        help="output DC blocker cutoff in Hz, 0 = off (default: off)",
    )
    args = parser.parse_args()
    try:
        clock_hz = validate_clock(args.clock, tms.MIN_CLOCK_HZ, tms.MAX_CLOCK_HZ)
        validate_filter(args.lpf, args.poles, args.dc)
        if not 8000 <= args.rate <= 96000:
            raise ValueError("rate must be between 8000 and 96000 Hz")
    except ValueError as e:
        parser.error(str(e))
    render_all(
        Path(args.out), args.load, clock_hz, args.rate, args.lpf, args.poles, args.dc
    )


if __name__ == "__main__":
    main()
