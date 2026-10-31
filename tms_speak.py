#!/usr/bin/env python3
"""
Play TI LPC vocabulary words through the TMS5220 synthesis core, write them as
one phrase WAV with --out, or render each word to its own WAV with --render.

Usage:
  python tms_speak.py                                   # interactive mode
  python tms_speak.py ZERO ONE TWO                      # speak words
  python tms_speak.py --clock 8240 ONE TWO              # slightly higher pitch
  python tms_speak.py --dc 30 --lpf 4500 ONE            # with the output stage model
  python tms_speak.py --load Vocab_FF800.cpp WFIVE
  python tms_speak.py --load Vocab_FF800.cpp --out phrase.wav WARNING STOP
  python tms_speak.py --load Vocab_FF800.cpp --render vocab_pcm        # all words
  python tms_speak.py --render out --rate 48000 --dc 30 --lpf 4500 ONE TWO
"""

import argparse
from pathlib import Path

import numpy as np

from lpc_audio import (
    DEFAULT_DC_HZ,
    DEFAULT_LPF_HZ,
    DEFAULT_LPF_POLES,
    analog_output,
    describe_filter,
    normalize_peak,
    resample,
    save_wav,
    validate_filter,
)
from lpc_cli import (
    WORD_GAP_MS,
    add_phrase_args,
    add_synth_args,
    check_phrase_args,
    check_synth_args,
    print_words,
    run_shell,
)
from lpc_vocab import BUILTIN_VOCAB, load_cpp_sources
from tms5220 import SAMPLE_RATE, synthesize, validate_clock

vocab: dict[str, bytes] = dict(BUILTIN_VOCAB)
clock_hz = SAMPLE_RATE
lpf_hz = DEFAULT_LPF_HZ
lpf_poles = DEFAULT_LPF_POLES
dc_hz = DEFAULT_DC_HZ
gap_ms = WORD_GAP_MS
amplitude_scale = 1.0


def filter_desc() -> str:
    return describe_filter(lpf_hz, lpf_poles, dc_hz)


def synth_phrase(words: list[str]) -> tuple[np.ndarray, float]:
    """
    Synthesize words with gap_ms of silence between them and run the output
    stage; return (audio, rate). Raises ValueError for unknown words.
    """
    missing = [word for word in words if word.upper() not in vocab]
    if missing:
        raise ValueError(
            "unknown word(s): " + ", ".join(repr(word) for word in missing)
            + "  (try 'list' to see available words)"
        )
    gap = np.zeros(int(clock_hz * gap_ms / 1000), dtype=np.float32)
    chunks = []
    for index, word in enumerate(words):
        if index:
            chunks.append(gap)
        chunks.append(synthesize(vocab[word.upper()]))
    return analog_output(np.concatenate(chunks), clock_hz, lpf_hz, lpf_poles, dc_hz)


def speak_words(words: list[str]) -> None:
    """Synthesize and play words from the active vocabulary."""
    import sounddevice as sd

    try:
        audio, rate = synth_phrase(words)
    except ValueError as e:
        print(f"  {e}")
        return
    sd.play(normalize_peak(audio) * amplitude_scale, samplerate=int(round(rate)))
    sd.wait()


def write_phrase(words: list[str], out_path: Path, rate_hz: int) -> None:
    """Write words as one phrase WAV at rate_hz, matching playback."""
    audio, rate = synth_phrase(words)
    audio = normalize_peak(resample(audio, rate, rate_hz)) * amplitude_scale
    save_wav(out_path, audio, rate_hz)
    dur_ms = round(len(audio) / rate_hz * 1000)
    print(f"Wrote {out_path} ({len(words)} word(s), {dur_ms} ms, {rate_hz} Hz)")


def render_words(words: list[str], out_dir: Path, rate_hz: int) -> int:
    """Write each word to out_dir/<WORD>.wav at rate_hz; return the error count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    errors = 0
    for i, word in enumerate(words, 1):
        label = f"[{i:4d}/{len(words)}]  {word:<22}"
        try:
            audio = synthesize(vocab[word])
            if audio.size == 0:
                print(f"  {label}  EMPTY — skipping")
                errors += 1
                continue
            # Run the DAC/analog model at the chip clock, then bring the result
            # to the WAV rate
            audio, model_rate = analog_output(audio, clock_hz, lpf_hz, lpf_poles, dc_hz)
            audio = normalize_peak(resample(audio, model_rate, rate_hz)) * amplitude_scale
            out_path = out_dir / f"{word}.wav"
            save_wav(out_path, audio, rate_hz)
            print(f"  {label}  {round(len(audio) / rate_hz * 1000):5d} ms  → {out_path.name}")
        except Exception as exc:
            print(f"  {label}  ERROR: {exc}")
            errors += 1
    print(f"\nDone.  {len(words) - errors} WAV files written to {out_dir}/")
    if errors:
        print(f"       {errors} error(s) — see lines marked EMPTY or ERROR above.")
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Interactive commands
# ─────────────────────────────────────────────────────────────────────────────

def cmd_load(args: list[str]) -> None:
    if not args:
        print("  usage: load <path/to/vocab.cpp>")
        return
    path = " ".join(args)
    try:
        loaded, n = load_cpp_sources([path])
    except FileNotFoundError:
        print(f"  file not found: {path}")
        return
    except Exception as e:
        print(f"  error loading file: {e}")
        return
    vocab.clear()
    vocab.update(loaded)
    print(f"  loaded {n} word(s) from {path}; active vocabulary replaced")


def cmd_builtin(args: list[str]) -> None:
    vocab.clear()
    vocab.update(BUILTIN_VOCAB)
    print(f"  reset to {len(vocab)} built-in word(s)")


def cmd_clock(args: list[str]) -> None:
    global clock_hz
    if len(args) != 1:
        print(f"  current clock: {clock_hz:g} Hz")
        print("  usage: clock <hz>  (example: clock 8240)")
        return
    try:
        clock_hz = validate_clock(float(args[0]))
        print(f"  clock set to {clock_hz:g} Hz")
    except ValueError as e:
        print(f"  {e}")


def _set_filter(lpf: float, poles: int, dc: float) -> None:
    global lpf_hz, lpf_poles, dc_hz
    try:
        validate_filter(lpf, poles, dc)
    except ValueError as e:
        print(f"  {e}")
        return
    lpf_hz, lpf_poles, dc_hz = lpf, poles, dc
    print(f"  filter set to {filter_desc()}")


def cmd_lpf(args: list[str]) -> None:
    if len(args) not in (1, 2):
        print(f"  current filter: {filter_desc()}")
        print("  usage: lpf <hz> [poles]  (example: lpf 4500 4; lpf 0 for off)")
        return
    try:
        _set_filter(float(args[0]), int(args[1]) if len(args) == 2 else lpf_poles, dc_hz)
    except ValueError as e:
        print(f"  {e}")


def cmd_dc(args: list[str]) -> None:
    if len(args) != 1:
        print(f"  current filter: {filter_desc()}")
        print("  usage: dc <hz>  (example: dc 30; dc 0 for off)")
        return
    try:
        _set_filter(lpf_hz, lpf_poles, float(args[0]))
    except ValueError as e:
        print(f"  {e}")


BANNER = """\
TMS5220 LPC Speech Synthesizer
Type words to speak, 'list' to see vocabulary, 'load <file.cpp>' to use an external vocabulary.
"""

HELP = """\
Commands:
  <WORD> [WORD ...]   speak one or more words  (e.g. FIVE POINT THREE)
  list [PATTERN ...]  list words, optionally filtered with wildcards (e.g. RE*)
  load <file.cpp>     replace vocabulary with a .cpp vocabulary file
  builtin             reset to built-in words only
  clock <hz>          set simulated chip clock, e.g. 7760 or 8240 (default 8000)
  lpf <hz> [poles]    set output low-pass, e.g. lpf 4500 4  (0 = off)
  dc <hz>             set output DC blocker cutoff, e.g. dc 30  (0 = off)
  help                show this message
  quit / exit / q     exit
"""

COMMANDS = {
    "load": cmd_load,
    "builtin": cmd_builtin,
    "clock": cmd_clock,
    "lpf": cmd_lpf,
    "dc": cmd_dc,
}


def main() -> None:
    global clock_hz, lpf_hz, lpf_poles, dc_hz, gap_ms, amplitude_scale
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_synth_args(parser)
    add_phrase_args(parser)
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--out",
        "-o",
        metavar="WAV",
        help="write the words as one phrase WAV instead of playing them",
    )
    output.add_argument(
        "--render",
        metavar="DIR",
        help="write words to DIR/<WORD>.wav instead of playing (all words if none given)",
    )
    parser.add_argument(
        "--rate",
        type=int,
        metavar="HZ",
        help=f"WAV sample rate for --out or --render, 8000-96000 (default: {SAMPLE_RATE})",
    )
    parser.add_argument("words", nargs="*", help="words to speak or write, or 'list [PATTERN ...]'")
    args = parser.parse_args()
    check_synth_args(parser, args)
    check_phrase_args(parser, args)
    if args.rate is not None and not (args.out or args.render):
        parser.error("--rate requires --out or --render")
    if args.out and not args.words:
        parser.error("--out requires words")
    rate_hz = SAMPLE_RATE if args.rate is None else args.rate
    if not 8000 <= rate_hz <= 96000:
        parser.error("rate must be between 8000 and 96000 Hz")
    clock_hz, lpf_hz, lpf_poles, dc_hz = args.clock, args.lpf, args.poles, args.dc
    gap_ms, amplitude_scale = args.gap_ms, args.amplitude_scale

    if args.load:
        try:
            loaded, n = load_cpp_sources(args.load)
        except Exception as e:
            parser.error(f"error loading vocabulary: {e}")
        vocab.clear()
        vocab.update(loaded)
        print(f"Loaded {n} external word(s) from {', '.join(args.load)}")

    if args.words and args.words[0].lower() == "list":
        print_words(vocab, args.words[1:])
    elif args.out:
        try:
            write_phrase(args.words, Path(args.out), rate_hz)
        except ValueError as e:
            parser.error(str(e))
    elif args.render:
        missing = [word for word in args.words if word.upper() not in vocab]
        if missing:
            parser.error("unknown word(s): " + ", ".join(missing))
        words = [word.upper() for word in args.words] or sorted(vocab)
        if not args.load:
            print(f"Using {len(vocab)} built-in word(s)")
        if clock_hz != SAMPLE_RATE:
            print(f"Simulating {clock_hz:g} Hz LPC clock")
        print(f"WAV sample rate: {rate_hz} Hz")
        print(f"Output filter: {filter_desc()}")
        render_words(words, Path(args.render), rate_hz)
    elif args.words:
        speak_words(args.words)
    else:
        if not args.load:
            print(f"Using {len(vocab)} built-in word(s)")
        if clock_hz != SAMPLE_RATE:
            print(f"Playback clock: {clock_hz:g} Hz")
        print(f"Output filter: {filter_desc()}")
        run_shell("speak> ", BANNER, HELP, COMMANDS, lambda: vocab, speak_words)


if __name__ == "__main__":
    main()
