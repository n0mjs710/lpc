#!/usr/bin/env python3
"""
Play rendered LPC PCM WAV vocabulary files.

Loads a directory of mono WAV files and plays words by filename stem. This is
intended for checking PCM output from render_vocab.py against LPC playback.

Usage:
  python pcm_speak.py --dir vocab_pcm ZERO ONE TWO
  python pcm_speak.py --dir vocab_pcm

Commands in interactive mode:
  <WORD> [WORD ...]   play one or more WAV words
  list                list available WAV words
  load <dir>          replace the active WAV vocabulary directory
  quit / exit / q     exit
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd

from lpc_audio import load_wav

SILENCE_MS = 60
_DEFAULT_DIR = Path(__file__).parent.parent / "vocab_pcm"


@dataclass(frozen=True)
class PcmWord:
    path: Path
    samples: np.ndarray
    rate: int


vocab: dict[str, PcmWord] = {}
active_dir: Path | None = None


def load_pcm_dir(path: Path) -> int:
    """Replace the active vocabulary with all WAV files in a directory."""
    global active_dir
    path = path.expanduser()
    if not path.is_dir():
        raise FileNotFoundError(f"{path} is not a directory")

    words: dict[str, PcmWord] = {}
    for wav_path in sorted(path.glob("*.wav")):
        key = wav_path.stem.upper()
        if key in words:
            raise ValueError(f"duplicate WAV word name: {key}")
        samples, rate = load_wav(wav_path)
        words[key] = PcmWord(wav_path, samples, rate)

    if not words:
        raise ValueError(f"{path} contains no .wav files")

    vocab.clear()
    vocab.update(words)
    active_dir = path
    return len(vocab)


def play(samples: np.ndarray, rate: int) -> None:
    """Play a float32 numpy array through the default audio output."""
    sd.play(samples, samplerate=rate)
    sd.wait()


def speak_words(words: list[str]) -> None:
    """Play a list of word names from the active PCM vocabulary."""
    entries = []
    for word in words:
        key = word.upper()
        if key not in vocab:
            print(f"  unknown word: {word!r}  (try 'list' to see available words)")
            return
        entries.append(vocab[key])

    if not entries:
        return

    rates = {entry.rate for entry in entries}
    if len(rates) == 1:
        rate = entries[0].rate
        silence = np.zeros(int(rate * SILENCE_MS / 1000), dtype=np.float32)
        chunks = []
        for entry in entries:
            chunks.append(entry.samples)
            chunks.append(silence)
        play(np.concatenate(chunks), rate)
        return

    for entry in entries:
        silence = np.zeros(int(entry.rate * SILENCE_MS / 1000), dtype=np.float32)
        play(np.concatenate([entry.samples, silence]), entry.rate)


def print_vocab() -> None:
    """Print all available words in columns."""
    words = sorted(vocab)
    cols = 6
    rows = (len(words) + cols - 1) // cols
    for r in range(rows):
        line = [words[r + c * rows] for c in range(cols) if r + c * rows < len(words)]
        print("  " + "  ".join(f"{w:<14}" for w in line))
    print(f"\n  {len(words)} words total")


BANNER = """\
PCM Vocabulary Player
Type words to play, 'list' to see vocabulary, or 'load <dir>' to load rendered WAV files.
"""

HELP = """\
Commands:
  <WORD> [WORD ...]   play one or more words  (e.g. FIVE POINT THREE)
  list                list all available words
  load <dir>          replace vocabulary with WAV files from a directory
  help                show this message
  quit / exit / q     exit
"""


def run_interactive() -> None:
    print(BANNER)
    if active_dir is not None:
        print(f"Loaded {len(vocab)} WAV word(s) from {active_dir}")
    while True:
        try:
            line = input("pcm> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "help":
            print(HELP)
        elif cmd == "list":
            print_vocab()
        elif cmd == "load":
            if len(parts) < 2:
                print("  usage: load <path/to/wav-directory>")
            else:
                path = Path(" ".join(parts[1:]))
                try:
                    n = load_pcm_dir(path)
                    print(f"  loaded {n} WAV word(s) from {path}")
                except Exception as e:
                    print(f"  error loading WAV directory: {e}")
        else:
            speak_words(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Play rendered LPC PCM WAV vocabulary files.",
    )
    parser.add_argument(
        "--dir",
        "-d",
        default=str(_DEFAULT_DIR),
        metavar="DIR",
        help=f"directory containing WAV files (default: {_DEFAULT_DIR})",
    )
    parser.add_argument("words", nargs="*", help="words to play")
    args = parser.parse_args()

    try:
        loaded = load_pcm_dir(Path(args.dir))
        print(f"Loaded {loaded} WAV word(s) from {args.dir}")
    except Exception as e:
        if args.words:
            parser.error(f"error loading WAV directory: {e}")
        print(f"No PCM vocabulary loaded: {e}")

    if args.words:
        speak_words(args.words)
    else:
        run_interactive()


if __name__ == "__main__":
    main()
