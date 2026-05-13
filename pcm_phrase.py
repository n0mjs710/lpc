#!/usr/bin/env python3
"""
Build a single WAV phrase from rendered LPC PCM vocabulary files.

Loads a directory of mono WAV files by filename stem, concatenates the requested
words with a configurable gap, and writes one mono PCM WAV file.

Usage:
  python pcm_phrase.py --dir vocab_pcm --out phrase.wav ZERO ONE TWO
  python pcm_phrase.py -d vocab_pcm -o phrase.wav THIS IS A TEST
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from lpc_audio import load_wav, save_wav

SILENCE_MS = 60
_DEFAULT_DIR = Path(__file__).parent / "vocab_pcm"


@dataclass(frozen=True)
class PcmWord:
    path: Path
    samples: np.ndarray
    rate: int


def load_pcm_dir(path: Path) -> dict[str, PcmWord]:
    """Load all WAV words from a directory, keyed by uppercase filename stem."""
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
    return words


def build_phrase(
    vocab: dict[str, PcmWord],
    words: list[str],
    gap_ms: int = SILENCE_MS,
) -> tuple[np.ndarray, int]:
    """Concatenate word WAVs into one phrase, inserting silence between words."""
    if not words:
        raise ValueError("at least one word is required")
    if gap_ms < 0:
        raise ValueError("gap must be zero or greater")

    entries = []
    missing = []
    for word in words:
        key = word.upper()
        if key in vocab:
            entries.append(vocab[key])
        else:
            missing.append(word)

    if missing:
        raise ValueError(
            "unknown word(s): "
            + ", ".join(repr(word) for word in missing)
            + "  (use pcm_speak.py list to see available words)"
        )

    rates = {entry.rate for entry in entries}
    if len(rates) != 1:
        rate_list = ", ".join(str(rate) for rate in sorted(rates))
        raise ValueError(f"phrase words use mixed sample rates: {rate_list}")

    rate = entries[0].rate
    silence = np.zeros(int(rate * gap_ms / 1000), dtype=np.float32)
    chunks = []
    for index, entry in enumerate(entries):
        chunks.append(entry.samples)
        if index < len(entries) - 1 and silence.size:
            chunks.append(silence)

    return np.concatenate(chunks).astype(np.float32), rate


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build one WAV phrase from rendered LPC PCM vocabulary words.",
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
        required=True,
        metavar="WAV",
        help="output WAV file",
    )
    parser.add_argument(
        "--gap-ms",
        type=int,
        default=SILENCE_MS,
        metavar="MS",
        help=f"silence between words in milliseconds (default: {SILENCE_MS})",
    )
    parser.add_argument("words", nargs="+", help="words to concatenate")
    args = parser.parse_args()

    try:
        vocab = load_pcm_dir(Path(args.dir))
        phrase, rate = build_phrase(vocab, args.words, args.gap_ms)
        out_path = Path(args.out)
        save_wav(out_path, phrase, rate)
    except Exception as exc:
        parser.error(str(exc))

    dur_ms = round(len(phrase) / rate * 1000)
    print(
        f"Wrote {out_path} "
        f"({len(args.words)} word(s), {dur_ms} ms, {rate} Hz)"
    )


if __name__ == "__main__":
    main()
