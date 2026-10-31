#!/usr/bin/env python3
"""
Play rendered WAV vocabulary words, or join them into one phrase WAV.

Loads a directory of mono WAV files (as written by tms_speak.py --render) and uses
each filename stem as a word. Without --out, words are played; with --out,
they are joined with --gap-ms of silence and written to one WAV file.

Usage:
  python pcm_speak.py --dir vocab_pcm ZERO ONE TWO
  python pcm_speak.py --dir vocab_pcm                       # interactive mode
  python pcm_speak.py --out phrase.wav --gap-ms 90 THISIS WFIVE
"""

import argparse
from pathlib import Path

import numpy as np

from lpc_audio import load_wav, save_wav
from lpc_cli import WORD_GAP_MS, add_phrase_args, check_phrase_args, print_words, run_shell

_DEFAULT_DIR = Path(__file__).parent / "vocab_pcm"

vocab: dict[str, tuple[np.ndarray, int]] = {}
gap_ms = WORD_GAP_MS
amplitude_scale = 1.0


def load_pcm_dir(path: Path) -> dict[str, tuple[np.ndarray, int]]:
    """Load all WAV words from a directory, keyed by uppercase filename stem."""
    path = path.expanduser()
    if not path.is_dir():
        raise FileNotFoundError(f"{path} is not a directory")

    words = {}
    for wav_path in sorted(path.glob("*.wav")):
        key = wav_path.stem.upper()
        if key in words:
            raise ValueError(f"duplicate WAV word name: {key}")
        words[key] = load_wav(wav_path)

    if not words:
        raise ValueError(f"{path} contains no .wav files")
    return words


def build_phrase(words: list[str]) -> tuple[np.ndarray, int]:
    """Join words from the active vocabulary with gap_ms of silence between them."""
    if not words:
        raise ValueError("at least one word is required")
    missing = [word for word in words if word.upper() not in vocab]
    if missing:
        raise ValueError(
            "unknown word(s): " + ", ".join(repr(word) for word in missing)
            + "  (try 'list' to see available words)"
        )

    entries = [vocab[word.upper()] for word in words]
    rates = {rate for _, rate in entries}
    if len(rates) != 1:
        raise ValueError(f"words use mixed sample rates: {', '.join(map(str, sorted(rates)))}")

    rate = entries[0][1]
    silence = np.zeros(int(rate * gap_ms / 1000), dtype=np.float32)
    chunks = []
    for index, (samples, _) in enumerate(entries):
        if index:
            chunks.append(silence)
        chunks.append(samples)
    return (np.concatenate(chunks) * amplitude_scale).astype(np.float32), rate


def speak_words(words: list[str]) -> None:
    """Play words from the active vocabulary."""
    import sounddevice as sd

    try:
        phrase, rate = build_phrase(words)
    except ValueError as e:
        print(f"  {e}")
        return
    sd.play(phrase, samplerate=rate)
    sd.wait()


def cmd_load(args: list[str]) -> None:
    if not args:
        print("  usage: load <path/to/wav-directory>")
        return
    path = Path(" ".join(args))
    try:
        words = load_pcm_dir(path)
    except Exception as e:
        print(f"  error loading WAV directory: {e}")
        return
    vocab.clear()
    vocab.update(words)
    print(f"  loaded {len(vocab)} WAV word(s) from {path}")


BANNER = """\
PCM Vocabulary Player
Type words to play, 'list' to see vocabulary, or 'load <dir>' to load rendered WAV files.
"""

HELP = """\
Commands:
  <WORD> [WORD ...]   play one or more words  (e.g. FIVE POINT THREE)
  list [PATTERN ...]  list words, optionally filtered with wildcards (e.g. RE*)
  load <dir>          replace vocabulary with WAV files from a directory
  help                show this message
  quit / exit / q     exit
"""


def main() -> None:
    global gap_ms, amplitude_scale
    parser = argparse.ArgumentParser(
        description="Play rendered WAV vocabulary words, or join them into one phrase WAV.",
    )
    parser.add_argument(
        "--dir",
        "-d",
        default=str(_DEFAULT_DIR),
        metavar="DIR",
        help=f"directory containing WAV files (default: {_DEFAULT_DIR})",
    )
    parser.add_argument(
        "--out",
        "-o",
        metavar="WAV",
        help="write the words as one phrase WAV instead of playing them",
    )
    add_phrase_args(parser)
    parser.add_argument("words", nargs="*", help="words to play, or 'list [PATTERN ...]'")
    args = parser.parse_args()

    check_phrase_args(parser, args)
    if args.out and not args.words:
        parser.error("--out requires words")
    gap_ms, amplitude_scale = args.gap_ms, args.amplitude_scale

    listing = bool(args.words) and args.words[0].lower() == "list"
    try:
        vocab.update(load_pcm_dir(Path(args.dir)))
    except Exception as e:
        if args.words and not listing:
            parser.error(f"error loading WAV directory: {e}")
        print(f"No PCM vocabulary loaded: {e}")

    if listing:
        print_words(vocab, args.words[1:])
    elif args.out:
        try:
            phrase, rate = build_phrase(args.words)
            save_wav(Path(args.out), phrase, rate)
        except Exception as e:
            parser.error(str(e))
        dur_ms = round(len(phrase) / rate * 1000)
        print(f"Wrote {args.out} ({len(args.words)} word(s), {dur_ms} ms, {rate} Hz)")
    elif args.words:
        speak_words(args.words)
    else:
        if vocab:
            print(f"Loaded {len(vocab)} WAV word(s) from {args.dir}")
        run_shell("pcm> ", BANNER, HELP, {"load": cmd_load}, lambda: vocab, speak_words)


if __name__ == "__main__":
    main()
