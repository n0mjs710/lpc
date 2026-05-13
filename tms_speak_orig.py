#!/usr/bin/env python3
"""
TMS5100/TMS2550 LPC Speech Synthesizer — ORIGINAL (no interpolation, no normalisation)
Emulates the Texas Instruments speech chip used in Speak & Spell.

Vocabulary format is compatible with the Arduino Talkie library (GPLv2).
Built-in words: numbers 0-12 from TI VM61002 ROM (US male voice).

Usage:
  python tms_speak_orig.py             # interactive mode
  python tms_speak_orig.py ZERO ONE TWO # speak words directly

Commands in interactive mode:
  <WORD> [WORD ...]   speak one or more words
  list [PATTERN ...]  list available words, optionally filtered with wildcards
  load <file.cpp>     replace vocabulary with a .cpp vocabulary file
  builtin             reset to built-in words only
  quit / exit / q     exit
"""

import fnmatch
import glob
import sys
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

import numpy as np
import sounddevice as sd

from lpc_vocab import load_cpp_sources, reverse_byte as _rev

# ─────────────────────────────────────────────────────────────────────────────
# TMS5100 LPC coefficient tables (8-bit mode)
# Source: Arduino Talkie library, TalkieLPC.h
# ─────────────────────────────────────────────────────────────────────────────

ENERGY = [0, 2, 3, 4, 5, 7, 10, 15, 20, 32, 41, 57, 81, 114, 161, 255]

PERIOD = [
     0, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
    31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 45, 47, 49,
    51, 53, 54, 57, 59, 61, 63, 66, 69, 71, 73, 77, 79, 81, 85, 87,
    92, 95, 99,102,106,110,115,119,123,128,133,138,143,149,154,160,
]

# K1–K10 quantization tables; index by coefficient number 0–9
K = [
    # K1 (5-bit index, 32 entries)
    [-125,-124,-124,-124,-123,-123,-122,-120,-119,-118,-117,-116,-115,-113,
     -111,-109,-103, -95, -84, -72, -57, -39, -20,   0,  20,  39,  57,  72,
       85,  95, 103, 109],
    # K2 (5-bit index, 32 entries)
    [ -82, -75, -68, -61, -52, -44, -34, -24, -14,  -4,   6,  16,  26,  36,
       45,  54,  62,  70,  77,  83,  89,  94,  98, 102, 106, 109, 112, 114,
      116, 118, 119, 127],
    # K3 (4-bit index, 16 entries)
    [-110, -97, -83, -70, -56, -43, -29, -16,  -2,  11,  25,  38,  52,  65,  79,  92],
    # K4 (4-bit index, 16 entries)
    [ -82, -68, -54, -40, -26, -12,   1,  15,  29,  43,  57,  71,  85,  99, 113, 126],
    # K5 (4-bit index, 16 entries)
    [ -82, -70, -59, -47, -35, -24, -12,  -1,  11,  23,  34,  46,  57,  69,  81,  92],
    # K6 (4-bit index, 16 entries)
    [ -64, -53, -42, -31, -20,  -9,   3,  14,  25,  36,  47,  58,  69,  80,  91, 102],
    # K7 (4-bit index, 16 entries)
    [ -77, -65, -53, -41, -29, -17,  -5,   7,  19,  31,  43,  55,  67,  79,  90, 102],
    # K8 (3-bit index, 8 entries)
    [-64, -40, -16,   7,  31,  55,  79, 102],
    # K9 (3-bit index, 8 entries)
    [-64, -44, -24,  -4,  16,  37,  57,  77],
    # K10 (3-bit index, 8 entries)
    [-51, -33, -15,   4,  22,  32,  59,  77],
]

K_BITS = [5, 5, 4, 4, 4, 4, 4, 3, 3, 3]

# Voiced excitation waveform (one glottal pulse cycle)
CHIRP = [0x00, 0x03, 0x0F, 0x28, 0x4C, 0x6C, 0x71, 0x50,
         0x25, 0x26, 0x4C, 0x44, 0x1A, 0x32, 0x3B, 0x13,
         0x37, 0x1A, 0x25, 0x1F, 0x1D]

SAMPLE_RATE   = 8000   # Hz
FRAME_SAMPLES = 200    # 25 ms per LPC frame


# ─────────────────────────────────────────────────────────────────────────────
# Bit reader
# ─────────────────────────────────────────────────────────────────────────────

class BitReader:
    def __init__(self, data):
        self.data = bytes(data)
        self.byte_pos = 0
        self.bit_pos  = 0

    def read(self, n):
        """Extract n bits from the stream (MSB-first, with per-byte reversal)."""
        hi = _rev(self.data[self.byte_pos]) << 8
        if self.bit_pos + n > 8 and self.byte_pos + 1 < len(self.data):
            hi |= _rev(self.data[self.byte_pos + 1])
        hi <<= self.bit_pos
        value = (hi >> (16 - n)) & ((1 << n) - 1)
        self.bit_pos += n
        if self.bit_pos >= 8:
            self.bit_pos  -= 8
            self.byte_pos += 1
        return value

    @property
    def done(self):
        return self.byte_pos >= len(self.data)


# ─────────────────────────────────────────────────────────────────────────────
# LPC synthesizer
# ─────────────────────────────────────────────────────────────────────────────

def synthesize(data):
    """
    Decode a TMS5100 LPC byte array and return float32 audio at SAMPLE_RATE Hz.
    Values are normalised to the range [-1.0, 1.0].
    Original version — no frame interpolation.
    """
    reader  = BitReader(data)
    x       = [0] * 10   # lattice filter state (10 delay elements)
    rand    = 1           # 16-bit LFSR for unvoiced excitation
    pc      = 0           # pitch period counter
    k_cur   = [0] * 10   # current K coefficients
    samples = []

    while not reader.done:
        e_idx = reader.read(4)

        if e_idx == 15:          # stop frame — end of word
            break
        if e_idx == 0:           # silent frame
            samples.extend([0.0] * FRAME_SAMPLES)
            continue

        energy = ENERGY[e_idx]
        repeat = reader.read(1)
        p_idx  = reader.read(6)
        pitch  = PERIOD[p_idx]

        if not repeat:
            k_cur[0] = K[0][reader.read(K_BITS[0])]
            k_cur[1] = K[1][reader.read(K_BITS[1])]
            k_cur[2] = K[2][reader.read(K_BITS[2])]
            k_cur[3] = K[3][reader.read(K_BITS[3])]
            if pitch != 0:
                for i in range(4, 10):
                    k_cur[i] = K[i][reader.read(K_BITS[i])]

        for _ in range(FRAME_SAMPLES):
            if pitch == 0:
                rand = (rand >> 1) ^ (0xB800 if rand & 1 else 0)
                exc  = energy if (rand & 0x8000) else -energy
            else:
                exc = (CHIRP[pc] * energy) >> 8 if pc < len(CHIRP) else 0
                pc  = (pc + 1) % pitch

            u = [0] * 11
            u[10] = exc
            for i in range(9, -1, -1):
                u[i] = u[i + 1] - ((k_cur[i] * x[i]) >> 7)
            u[0] = max(-512, min(511, u[0]))

            for i in range(9, 0, -1):
                x[i] = x[i - 1] + ((k_cur[i - 1] * u[i - 1]) >> 7)
            x[0] = u[0]

            samples.append(u[0] / 512.0)

    return np.array(samples, dtype=np.float32)


def play(samples, block=True):
    """Play a float32 numpy array through the default audio output."""
    sd.play(samples, samplerate=SAMPLE_RATE)
    if block:
        sd.wait()


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary — built-in words (TI VM61002 ROM, US male voice)
# Source: Arduino Talkie library, Vocab_US_Large.cpp (GPLv2)
# ─────────────────────────────────────────────────────────────────────────────

_BUILTIN_VOCAB = {
    "ZERO":   bytes([0x69,0xFB,0x59,0xDD,0x51,0xD5,0xD7,0xB5,0x6F,0x0A,0x78,0xC0,0x52,0x01,0x0F,0x50,
                     0xAC,0xF6,0xA8,0x16,0x15,0xF2,0x7B,0xEA,0x19,0x47,0xD0,0x64,0xEB,0xAD,0x76,0xB5,
                     0xEB,0xD1,0x96,0x24,0x6E,0x62,0x6D,0x5B,0x1F,0x0A,0xA7,0xB9,0xC5,0xAB,0xFD,0x1A,
                     0x62,0xF0,0xF0,0xE2,0x6C,0x73,0x1C,0x73,0x52,0x1D,0x19,0x94,0x6F,0xCE,0x7D,0xED,
                     0x6B,0xD9,0x82,0xDC,0x48,0xC7,0x2E,0x71,0x8B,0xBB,0xDF,0xFF,0x1F]),
    "ONE":    bytes([0x66,0x4E,0xA8,0x7A,0x8D,0xED,0xC4,0xB5,0xCD,0x89,0xD4,0xBC,0xA2,0xDB,0xD1,0x27,
                     0xBE,0x33,0x4C,0xD9,0x4F,0x9B,0x4D,0x57,0x8A,0x76,0xBE,0xF5,0xA9,0xAA,0x2E,0x4F,
                     0xD5,0xCD,0xB7,0xD9,0x43,0x5B,0x87,0x13,0x4C,0x0D,0xA7,0x75,0xAB,0x7B,0x3E,0xE3,
                     0x19,0x6F,0x7F,0xA7,0xA7,0xF9,0xD0,0x30,0x5B,0x1D,0x9E,0x9A,0x34,0x44,0xBC,0xB6,
                     0x7D,0xFE,0x1F]),
    "TWO":    bytes([0x06,0xB8,0x59,0x34,0x00,0x27,0xD6,0x38,0x60,0x58,0xD3,0x91,0x55,0x2D,0xAA,0x65,
                     0x9D,0x4F,0xD1,0xB8,0x39,0x17,0x67,0xBF,0xC5,0xAE,0x5A,0x1D,0xB5,0x7A,0x06,0xF6,
                     0xA9,0x7D,0x9D,0xD2,0x6C,0x55,0xA5,0x26,0x75,0xC9,0x9B,0xDF,0xFC,0x6E,0x0E,0x63,
                     0x3A,0x34,0x70,0xAF,0x3E,0xFF,0x1F]),
    "THREE":  bytes([0x0C,0xE8,0x2E,0x94,0x01,0x4D,0xBA,0x4A,0x40,0x03,0x16,0x68,0x69,0x36,0x1C,0xE9,
                     0xBA,0xB8,0xE5,0x39,0x70,0x72,0x84,0xDB,0x51,0xA4,0xA8,0x4E,0xA3,0xC9,0x77,0xB1,
                     0xCA,0xD6,0x52,0xA8,0x71,0xED,0x2A,0x7B,0x4B,0xA6,0xE0,0x37,0xB7,0x5A,0xDD,0x48,
                     0x8E,0x94,0xF1,0x64,0xCE,0x6D,0x19,0x55,0x91,0xBC,0x6E,0xD7,0xAD,0x1E,0xF5,0xAA,
                     0x77,0x7A,0xC6,0x70,0x22,0xCD,0xC7,0xF9,0x89,0xCF,0xFF,0x03]),
    "FOUR":   bytes([0x08,0x68,0x21,0x0D,0x03,0x04,0x28,0xCE,0x92,0x03,0x23,0x4A,0xCA,0xA6,0x1C,0xDA,
                     0xAD,0xB4,0x70,0xED,0x19,0x64,0xB7,0xD3,0x91,0x45,0x51,0x35,0x89,0xEA,0x66,0xDE,
                     0xEA,0xE0,0xAB,0xD3,0x29,0x4F,0x1F,0xFA,0x52,0xF6,0x90,0x52,0x3B,0x25,0x7F,0xDD,
                     0xCB,0x9D,0x72,0x72,0x8C,0x79,0xCB,0x6F,0xFA,0xD2,0x10,0x9E,0xB4,0x2C,0xE1,0x4F,
                     0x25,0x70,0x3A,0xDC,0xBA,0x2F,0x6F,0xC1,0x75,0xCB,0xF2,0xFF]),
    "FIVE":   bytes([0x08,0x68,0x4E,0x9D,0x02,0x1C,0x60,0xC0,0x8C,0x69,0x12,0xB0,0xC0,0x28,0xAB,0x8C,
                     0x9C,0xC0,0x2D,0xBB,0x38,0x79,0x31,0x15,0xA3,0xB6,0xE4,0x16,0xB7,0xDC,0xF5,0x6E,
                     0x57,0xDF,0x54,0x5B,0x85,0xBE,0xD9,0xE3,0x5C,0xC6,0xD6,0x6D,0xB1,0xA5,0xBF,0x99,
                     0x5B,0x3B,0x5A,0x30,0x09,0xAF,0x2F,0xED,0xEC,0x31,0xC4,0x5C,0xBE,0xD6,0x33,0xDD,
                     0xAD,0x88,0x87,0xE2,0xD2,0xF2,0xF4,0xE0,0x16,0x2A,0xB2,0xE3,0x63,0x1F,0xF9,0xF0,
                     0xE7,0xFF,0x01]),
    "SIX":    bytes([0x04,0xF8,0xAD,0x4C,0x02,0x16,0xB0,0x80,0x06,0x56,0x35,0x5D,0xA8,0x2A,0x6D,0xB9,
                     0xCD,0x69,0xBB,0x2B,0x55,0xB5,0x2D,0xB7,0xDB,0xFD,0x9C,0x0D,0xD8,0x32,0x8A,0x7B,
                     0xBC,0x02,0x00,0x03,0x0C,0xB1,0x2E,0x80,0xDF,0xD2,0x35,0x20,0x01,0x0E,0x60,0xE0,
                     0xFF,0x01]),
    "SEVEN":  bytes([0x0C,0xF8,0x5E,0x4C,0x01,0xBF,0x95,0x7B,0xC0,0x02,0x16,0xB0,0xC0,0xC8,0xBA,0x36,
                     0x4D,0xB7,0x27,0x37,0xBB,0xC5,0x29,0xBA,0x71,0x6D,0xB7,0xB5,0xAB,0xA8,0xCE,0xBD,
                     0xD4,0xDE,0xA6,0xB2,0x5A,0xB1,0x34,0x6A,0x1D,0xA7,0x35,0x37,0xE5,0x5A,0xAE,0x6B,
                     0xEE,0xD2,0xB6,0x26,0x4C,0x37,0xF5,0x4D,0xB9,0x9A,0x34,0x39,0xB7,0xC6,0xE1,0x1E,
                     0x81,0xD8,0xA2,0xEC,0xE6,0xC7,0x7F,0xFE,0xFB,0x7F]),
    "EIGHT":  bytes([0x65,0x69,0x89,0xC5,0x73,0x66,0xDF,0xE9,0x8C,0x33,0x0E,0x41,0xC6,0xEA,0x5B,0xEF,
                     0x7A,0xF5,0x33,0x25,0x50,0xE5,0xEA,0x39,0xD7,0xC5,0x6E,0x08,0x14,0xC1,0xDD,0x45,
                     0x64,0x03,0x00,0x80,0x00,0xAE,0x70,0x33,0xC0,0x73,0x33,0x1A,0x10,0x40,0x8F,0x2B,
                     0x14,0xF8,0x7F]),
    "NINE":   bytes([0xE6,0xA8,0x1A,0x35,0x5D,0xD6,0x9A,0x35,0x4B,0x8C,0x4E,0x6B,0x1A,0xD6,0xA6,0x51,
                     0xB2,0xB5,0xEE,0x58,0x9A,0x13,0x4F,0xB5,0x35,0x67,0x68,0x26,0x3D,0x4D,0x97,0x9C,
                     0xBE,0xC9,0x75,0x2F,0x6D,0x7B,0xBB,0x5B,0xDF,0xFA,0x36,0xA7,0xEF,0xBA,0x25,0xDA,
                     0x16,0xDF,0x69,0xAC,0x23,0x05,0x45,0xF9,0xAC,0xB9,0x8F,0xA3,0x97,0x20,0x73,0x9F,
                     0x54,0xCE,0x1E,0x45,0xC2,0xA2,0x4E,0x3E,0xD3,0xD5,0x3D,0xB1,0x79,0x24,0x0D,0xD7,
                     0x48,0x4C,0x6E,0xE1,0x2C,0xDE,0xFF,0x0F]),
    "TEN":    bytes([0x0E,0x38,0x3C,0x2D,0x00,0x5F,0xB6,0x19,0x60,0xA8,0x90,0x93,0x36,0x2B,0xE2,0x99,
                     0xB3,0x4E,0xD9,0x7D,0x89,0x85,0x2F,0xBE,0xD5,0xAD,0x4F,0x3F,0x64,0xAB,0xA4,0x3E,
                     0xBA,0xD3,0x59,0x9A,0x2E,0x75,0xD5,0x39,0x6D,0x6B,0x0A,0x2D,0x3C,0xEC,0xE5,0xDD,
                     0x1F,0xFE,0xB0,0xE7,0xFF,0x03]),
    "ELEVEN": bytes([0xA5,0xEF,0xD6,0x50,0x3B,0x67,0x8F,0xB9,0x3B,0x23,0x49,0x7F,0x33,0x87,0x31,0x0C,
                     0xE9,0x22,0x49,0x7D,0x56,0xDF,0x69,0xAA,0x39,0x6D,0x59,0xDD,0x82,0x56,0x92,0xDA,
                     0xE5,0x74,0x9D,0xA7,0xA6,0xD3,0x9A,0x53,0x37,0x99,0x56,0xA6,0x6F,0x4F,0x59,0x9D,
                     0x7B,0x89,0x2F,0xDD,0xC5,0x28,0xAA,0x15,0x4B,0xA3,0xD6,0xAE,0x8C,0x8A,0xAD,0x54,
                     0x3B,0xA7,0xA9,0x3B,0xB3,0x54,0x5D,0x33,0xE6,0xA6,0x5C,0xCB,0x75,0xCD,0x5E,0xC6,
                     0xDA,0xA4,0xCA,0xB9,0x35,0xAE,0x67,0xB8,0x46,0x40,0xB6,0x28,0xBB,0xF1,0xF6,0xB7,
                     0xB9,0x47,0x20,0xB6,0x28,0xBB,0xFF,0x0F]),
    "TWELVE": bytes([0x09,0x98,0xDA,0x22,0x01,0x37,0x78,0x1A,0x20,0x85,0xD1,0x50,0x3A,0x33,0x11,0x81,
                     0x5D,0x5B,0x95,0xD4,0x44,0x04,0x76,0x9D,0xD5,0xA9,0x3A,0xAB,0xF0,0xA1,0x3E,0xB7,
                     0xBA,0xD5,0xA9,0x2B,0xEB,0xCC,0xA0,0x3E,0xB7,0xBD,0xC3,0x5A,0x3B,0xC8,0x69,0x67,
                     0xBD,0xFB,0xE8,0x67,0xBF,0xCA,0x9D,0xE9,0x74,0x08,0xE7,0xCE,0x77,0x78,0x06,0x89,
                     0x32,0x57,0xD6,0xF1,0xF1,0x8F,0x7D,0xFE,0x1F]),
    # Short silence pad useful for separating words
    "PAUSE":  bytes([0x08,0x14,0xC1,0xDD,0x45,0x64,0x03,0x00,0xFC,0x4A,0x56,0x26,0x3A,0x06,0x0A]),
}


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary management
# ─────────────────────────────────────────────────────────────────────────────

# The live vocabulary dict; starts with built-ins
vocab: dict[str, bytes] = dict(_BUILTIN_VOCAB)


def use_builtin_vocab() -> int:
    """Reset the active vocabulary to built-in words only."""
    vocab.clear()
    vocab.update(_BUILTIN_VOCAB)
    return len(vocab)


def load_vocab_sources(paths: list[str]) -> int:
    """Replace the active vocabulary with one or more external .cpp sources."""
    loaded_vocab, loaded = load_cpp_sources(paths)
    vocab.clear()
    vocab.update(loaded_vocab)
    return loaded


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

SILENCE_MS  = 60   # gap between words in milliseconds
_silence    = np.zeros(int(SAMPLE_RATE * SILENCE_MS / 1000), dtype=np.float32)


def speak_words(words: list[str]):
    """Synthesize and play a list of word names (must exist in vocab)."""
    chunks = []
    for word in words:
        key = word.upper()
        if key not in vocab:
            print(f"  unknown word: {word!r}  (try 'list' to see available words)")
            return
        chunks.append(synthesize(vocab[key]))
        chunks.append(_silence)

    if chunks:
        audio = np.concatenate(chunks)
        peak = np.max(np.abs(audio))
        if peak > 0:
            audio = audio * (0.9 / peak)
        play(audio)


def vocab_words_matching(patterns: list[str] | None = None) -> list[str]:
    """Return sorted vocabulary words matching shell-style wildcard patterns."""
    if not patterns:
        return sorted(vocab)

    normalized = [pattern.upper() for pattern in patterns]
    return sorted(
        {
            word
            for word in vocab
            for pattern in normalized
            if fnmatch.fnmatchcase(word, pattern)
        }
    )


def print_vocab(patterns: list[str] | None = None):
    """Print available words in columns, optionally filtered by wildcards."""
    words = vocab_words_matching(patterns)
    if not words:
        print(f"  no words match: {' '.join(patterns or [])}")
        return

    cols  = 6
    rows  = (len(words) + cols - 1) // cols
    for r in range(rows):
        line = [words[r + c * rows] for c in range(cols) if r + c * rows < len(words)]
        print("  " + "  ".join(f"{w:<14}" for w in line))
    suffix = f" matching {' '.join(patterns)}" if patterns else " total"
    print(f"\n  {len(words)} words{suffix}")


COMMANDS = ("list", "load", "builtin", "help", "quit", "exit", "q")


def _word_completions(text: str) -> list[str]:
    prefix = text.upper()
    return [f"{word} " for word in sorted(vocab) if word.startswith(prefix)]


def _path_completions(text: str) -> list[str]:
    matches = []
    for match in glob.glob(f"{text}*"):
        path = Path(match)
        suffix = "/" if path.is_dir() else " "
        matches.append(f"{match}{suffix}")
    return sorted(matches)


def install_completion() -> None:
    """Install readline tab completion for interactive command entry."""
    if readline is None:
        return

    def complete(text: str, state: int) -> str | None:
        line = readline.get_line_buffer()
        begidx = readline.get_begidx()
        prior_parts = line[:begidx].split()
        first = prior_parts[0].lower() if prior_parts else ""

        if not prior_parts:
            choices = [f"{cmd} " for cmd in COMMANDS if cmd.startswith(text.lower())]
            choices.extend(_word_completions(text))
        elif first == "load":
            choices = _path_completions(text)
        else:
            choices = _word_completions(text)

        return choices[state] if state < len(choices) else None

    readline.set_completer(complete)
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

BANNER = """\
TMS5100 LPC Speech Synthesizer
Type words to speak, 'list' to see vocabulary, 'load <file.cpp>' to use an external vocabulary.
Default vocabulary: built-in TI VM61002 numbers only.
"""

HELP = """\
Commands:
  <WORD> [WORD ...]   speak one or more words  (e.g. FIVE POINT THREE)
  list [PATTERN ...]  list words, optionally filtered with wildcards (e.g. RE*)
  load <file.cpp>     replace vocabulary with a .cpp vocabulary file
  builtin             reset to built-in words only
  help                show this message
  quit / exit / q     exit
"""


def run_interactive():
    install_completion()
    print(BANNER)
    while True:
        try:
            line = input("speak> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split()
        cmd   = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "help":
            print(HELP)
        elif cmd == "list":
            print_vocab(parts[1:])
        elif cmd == "load":
            if len(parts) < 2:
                print("  usage: load <path/to/vocab.cpp>")
            else:
                path = " ".join(parts[1:])
                try:
                    n = load_vocab_sources([path])
                    print(f"  loaded {n} word(s) from {path}; active vocabulary replaced")
                except FileNotFoundError:
                    print(f"  file not found: {path}")
                except Exception as e:
                    print(f"  error loading file: {e}")
        elif cmd == "builtin":
            n = use_builtin_vocab()
            print(f"  reset to {n} built-in word(s)")
        else:
            speak_words(parts)


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "list":
        print_vocab(sys.argv[2:])
    elif len(sys.argv) > 1:
        speak_words(sys.argv[1:])
    else:
        print(f"Using {len(vocab)} built-in word(s)")
        run_interactive()


if __name__ == "__main__":
    main()
