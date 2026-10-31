#!/usr/bin/env python3
"""
TMS5220 LPC Speech Synthesizer
Emulates the Texas Instruments TMS5220 speech chip, using the ROM tables and
sample-level synthesis behavior of MAME's tms5220 core.

Vocabulary format is compatible with the Arduino Talkie library (GPLv2).
Built-in words: numbers 0-12 from TI VM61002 ROM (US male voice).

Usage:
  python tms_speak.py                         # interactive mode
  python tms_speak.py ZERO ONE TWO            # speak words directly
  python tms_speak.py --clock 8240 ONE TWO    # slightly higher pitch
  python tms_speak.py --load Vocab_FF800.cpp WORD

Commands in interactive mode:
  <WORD> [WORD ...]   speak one or more words
  list [PATTERN ...]  list available words, optionally filtered with wildcards
  load <file.cpp>     replace vocabulary with a .cpp vocabulary file
  clock <hz>          set simulated LPC clock (default 8000)
  lpf <hz> [poles]    set output low-pass filter (0 = off)
  dc <hz>             set output DC blocker (0 = off)
  quit / exit / q     exit
"""

import argparse
import fnmatch
import glob
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

import numpy as np
import sounddevice as sd

from lpc_audio import (
    DEFAULT_DC_HZ,
    DEFAULT_LPF_HZ,
    DEFAULT_LPF_POLES,
    analog_output,
    describe_filter,
    normalize_peak,
    validate_filter,
)
from lpc_vocab import load_cpp_sources, reverse_byte as _rev

# ─────────────────────────────────────────────────────────────────────────────
# TMS5220 ROM tables
# Source: MAME src/devices/sound/tms5110r.hxx (tms5220_coeff), BSD-3-Clause,
# copyright-holders Frank Palazzolo, Couriersud, Jonathan Gevaryahu.
# MAME verified the LPC and chirp tables against TMS5220NL/TMS5220CNL decaps.
# ─────────────────────────────────────────────────────────────────────────────

ENERGY = [0, 1, 2, 3, 4, 6, 8, 11, 16, 23, 33, 47, 63, 85, 114, 0]

PERIOD = [
      0,  15,  16,  17,  18,  19,  20,  21,  22,  23,  24,  25,  26,  27,  28,  29,
     30,  31,  32,  33,  34,  35,  36,  37,  38,  39,  40,  41,  42,  44,  46,  48,
     50,  52,  53,  56,  58,  60,  62,  65,  68,  70,  72,  76,  78,  80,  84,  86,
     91,  94,  98, 101, 105, 109, 114, 118, 122, 127, 132, 137, 142, 148, 153, 159,
]

# K1–K10 reflection coefficients (10-bit signed); index by coefficient number 0–9
K = [
    # K1 (5-bit index, 32 entries)
    [-501, -498, -497, -495, -493, -491, -488, -482, -478, -474, -469, -464,
     -459, -452, -445, -437, -412, -380, -339, -288, -227, -158,  -81,   -1,
       80,  157,  226,  287,  337,  379,  411,  436],
    # K2 (5-bit index, 32 entries)
    [-328, -303, -274, -244, -211, -175, -138,  -99,  -59,  -18,   24,   64,
      105,  143,  180,  215,  248,  278,  306,  331,  354,  374,  392,  408,
      422,  435,  445,  455,  463,  470,  476,  506],
    # K3 (4-bit index, 16 entries)
    [-441, -387, -333, -279, -225, -171, -117,  -63,   -9,   45,   98,  152,  206,  260,  314,  368],
    # K4 (4-bit index, 16 entries)
    [-328, -273, -217, -161, -106,  -50,    5,   61,  116,  172,  228,  283,  339,  394,  450,  506],
    # K5 (4-bit index, 16 entries)
    [-328, -282, -235, -189, -142,  -96,  -50,   -3,   43,   90,  136,  182,  229,  275,  322,  368],
    # K6 (4-bit index, 16 entries)
    [-256, -212, -168, -123,  -79,  -35,   10,   54,   98,  143,  187,  232,  276,  320,  365,  409],
    # K7 (4-bit index, 16 entries)
    [-308, -260, -212, -164, -117,  -69,  -21,   27,   75,  122,  170,  218,  266,  314,  361,  409],
    # K8 (3-bit index, 8 entries)
    [-256, -161,  -66,   29,  124,  219,  314,  409],
    # K9 (3-bit index, 8 entries)
    [-256, -176,  -96,  -15,   65,  146,  226,  307],
    # K10 (3-bit index, 8 entries)
    [-205, -132,  -59,   14,   87,  160,  234,  307],
]

K_BITS = [5, 5, 4, 4, 4, 4, 4, 3, 3, 3]

# Voiced excitation chirp ROM (52 entries; the address counter stops at 51)
CHIRP = [0x00, 0x03, 0x0F, 0x28, 0x4C, 0x6C, 0x71, 0x50,
         0x25, 0x26, 0x4C, 0x44, 0x1A, 0x32, 0x3B, 0x13,
         0x37, 0x1A, 0x25, 0x1F, 0x1D] + [0x00] * 31

# Interpolation shift per IP count; IP 0 lands exactly on the frame target
INTERP_SHIFT = [0, 3, 3, 3, 2, 2, 1, 1]

SAMPLE_RATE    = 8000                            # Hz
MIN_CLOCK_HZ   = 4000                            # simulated LPC clock limits
MAX_CLOCK_HZ   = 16000
playback_clock = SAMPLE_RATE
lpf_hz         = DEFAULT_LPF_HZ                  # output filter after the DAC
lpf_poles      = DEFAULT_LPF_POLES
dc_hz          = DEFAULT_DC_HZ


def _lfsr_step_table(steps: int = 20) -> list[int]:
    """Map each 13-bit LFSR state to its state after `steps` shifts."""
    table = []
    for rng in range(0x2000):
        for _ in range(steps):
            bit = ((rng >> 12) ^ (rng >> 3) ^ (rng >> 2) ^ rng) & 1
            rng = ((rng << 1) | bit) & 0x1FFF
        table.append(rng)
    return table


# The chip clocks its noise LFSR once per T-cycle, 20 times per sample
_LFSR_NEXT = _lfsr_step_table()


# ─────────────────────────────────────────────────────────────────────────────
# Bit reader
# ─────────────────────────────────────────────────────────────────────────────

class BitReader:
    def __init__(self, data):
        self.data = bytes(data)
        self.byte_pos = 0
        self.bit_pos  = 0

    def read(self, n):
        """Extract n bits (MSB-first, with per-byte reversal); zeros past the end."""
        if self.done:
            return 0
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
    Decode a TMS5220 LPC byte array and return float32 audio at SAMPLE_RATE Hz.
    Values are the chip's 8-bit analog output scaled to the range [-1.0, 1.0).

    Ported from MAME's tms5220 core (BSD-3-Clause). Each 25 ms frame is 8
    interpolation periods (IP) of 25 samples, sequenced by the chip's PC and
    subcycle counters:
      - parameters step toward the frame target by shifts of 3,3,3,2,2,1,1 and
        land on it at IP 0, one parameter per PC slot;
      - interpolation is inhibited on voiced/unvoiced and silence transitions;
      - unvoiced frames zero K5–K10;
      - excitation is the 52-entry chirp ROM or a 13-bit LFSR at +/-64;
      - the lattice uses 10-bit K x 14-bit multiplies with >> 9, the output is
        wrapped to 14 bits, then clipped and truncated to the 8-bit DAC.
    """
    reader = BitReader(data)

    # Frame indices start in the chip's idle state after a SPEAK command
    e_idx = 0
    p_idx = 0
    k_idx = [0, 0, 0, 0, 0xF, 0xF, 0xF, 0x7, 0x7, 0x7]

    # Current (interpolated) parameters
    energy = prev_energy = pitch = 0
    k = [0] * 10
    zpar = True       # zero all parameters until the first frame is parsed
    uv_zpar = True    # zero K5–K10 (unvoiced or idle)
    old_e = True      # OLDE: previous frame was silence
    old_p = True      # OLDP: previous frame was unvoiced
    inhibit = True
    pitch_zero = False
    talk = True

    # Timing counters and excitation state
    ip, pc, subcycle = 0, 0, 1
    pitch_count = 0
    rng = 0x1FFF

    # Lattice filter state
    u = [0] * 10
    x = [0] * 10

    samples = []

    while True:
        if ip == 0 and pc == 12 and subcycle == 1:
            # Parse a new frame
            zpar = uv_zpar = False
            e_idx = 15 if reader.done else reader.read(4)
            if e_idx == 15:              # stop frame — finish ramping, then halt
                talk = False
            elif e_idx != 0:             # silence frames keep pitch and K indices
                repeat = reader.read(1)
                p_idx = reader.read(6)
                uv_zpar = p_idx == 0
                if not repeat:           # repeat frames reuse the previous K indices
                    for i in range(4):
                        k_idx[i] = reader.read(K_BITS[i])
                    if p_idx != 0:       # voiced: decode K5–K10 too
                        for i in range(4, 10):
                            k_idx[i] = reader.read(K_BITS[i])

            new_unvoiced = p_idx == 0
            new_silence = e_idx == 0
            inhibit = (
                (not old_p and new_unvoiced)
                or (old_p and not new_unvoiced)
                or (old_e and not new_silence)
                or (old_p and new_silence)
            )
        elif subcycle == 2:
            # Interpolate one parameter per PC slot, on the B subcycle
            shift = INTERP_SHIFT[ip]
            hold = inhibit and ip != 0
            if pc == 0:
                if ip == 0:
                    pitch_zero = False
                step = 0 if hold else (ENERGY[e_idx] - energy) >> shift
                energy = 0 if zpar else energy + step
            elif pc == 1:
                step = 0 if hold else (PERIOD[p_idx] - pitch) >> shift
                pitch = 0 if zpar else pitch + step
            else:
                i = pc - 2
                step = 0 if hold else (K[i][k_idx[i]] - k[i]) >> shift
                k[i] = 0 if (zpar if i < 4 else uv_zpar) else k[i] + step

        # Excitation source
        if old_p:
            exc = -64 if rng & 1 else 64
        else:
            exc = CHIRP[min(pitch_count, 51)]
        rng = _LFSR_NEXT[rng]

        # 10-stage lattice filter; operands wrap to 14 bits like the chip's
        # multiplier inputs. (energy * (exc << 6)) >> 9 == (energy * exc) >> 3.
        y = (prev_energy * exc) >> 3
        for i in range(9, -1, -1):
            y -= (k[i] * (((x[i] + 16384) & 0x7FFF) - 16384)) >> 9
            u[i] = y
        for i in range(9, 0, -1):
            x[i] = x[i - 1] + ((k[i - 1] * (((u[i - 1] + 16384) & 0x7FFF) - 16384)) >> 9)
        x[0] = u[0]
        prev_energy = energy

        # Wrap to 14 bits, then clip and keep the top 8 bits for the DAC
        out = ((u[0] + 16384) & 0x7FFF) - 16384
        out = max(-2048, min(2047, out)) & ~0xF
        samples.append(out)

        # Advance the PC/subcycle/IP counters (25 samples per IP)
        subcycle += 1
        if subcycle == 2 and pc == 12:
            if ip == 7:
                if inhibit:
                    pitch_zero = True
                old_e = e_idx == 0
                old_p = p_idx == 0
                if not talk:
                    break
            subcycle = 1
            pc = 0
            ip = (ip + 1) & 7
        elif subcycle == 3:
            subcycle = 1
            pc += 1

        pitch_count += 1
        if pitch_count >= pitch or pitch_zero:
            pitch_count = 0
        pitch_count &= 0x1FF

    return np.array(samples, dtype=np.float32) / 2048.0


def play(samples, block=True, clock_hz=None):
    """Play raw synthesizer output at a chip clock, through any output filter."""
    if clock_hz is None:
        clock_hz = playback_clock
    audio, rate = analog_output(samples, clock_hz, lpf_hz, lpf_poles, dc_hz)
    sd.play(normalize_peak(audio), samplerate=int(round(rate)))
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


def set_playback_clock(clock_hz: float) -> None:
    """Set the simulated LPC clock in Hz used by play()."""
    global playback_clock
    if not MIN_CLOCK_HZ <= clock_hz <= MAX_CLOCK_HZ:
        raise ValueError(
            f"clock must be between {MIN_CLOCK_HZ:g} and {MAX_CLOCK_HZ:g} Hz"
        )
    playback_clock = clock_hz


def set_output_filter(lpf=None, poles=None, dc=None) -> None:
    """Set the output low-pass and DC blocker used by play()."""
    global lpf_hz, lpf_poles, dc_hz
    new_lpf = lpf_hz if lpf is None else lpf
    new_poles = lpf_poles if poles is None else poles
    new_dc = dc_hz if dc is None else dc
    validate_filter(new_lpf, new_poles, new_dc)
    lpf_hz, lpf_poles, dc_hz = new_lpf, new_poles, new_dc


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
        play(np.concatenate(chunks))


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


COMMANDS = ("list", "load", "builtin", "clock", "lpf", "dc", "help", "quit", "exit", "q")


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
TMS5220 LPC Speech Synthesizer
Type words to speak, 'list' to see vocabulary, 'load <file.cpp>' to use an external vocabulary.
Default vocabulary: built-in TI VM61002 numbers only.
"""

HELP = """\
Commands:
  <WORD> [WORD ...]   speak one or more words  (e.g. FIVE POINT THREE)
  list [PATTERN ...]  list words, optionally filtered with wildcards (e.g. RE*)
  load <file.cpp>     replace vocabulary with a .cpp vocabulary file
  builtin             reset to built-in words only
  clock <hz>          set simulated LPC clock, e.g. 7760 or 8240 (default 8000)
  lpf <hz> [poles]    set output low-pass, e.g. lpf 3000 2  (0 = off)
  dc <hz>             set output DC blocker cutoff  (0 = off)
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
        elif cmd == "clock":
            if len(parts) != 2:
                print(f"  current clock: {playback_clock:g} Hz")
                print("  usage: clock <hz>  (example: clock 8240)")
            else:
                try:
                    set_playback_clock(float(parts[1]))
                    print(f"  clock set to {playback_clock:g} Hz")
                except ValueError as e:
                    print(f"  {e}")
        elif cmd == "lpf":
            if len(parts) not in (2, 3):
                print(f"  current filter: {describe_filter(lpf_hz, lpf_poles, dc_hz)}")
                print("  usage: lpf <hz> [poles]  (example: lpf 3000 2; lpf 0 for off)")
            else:
                try:
                    poles = int(parts[2]) if len(parts) == 3 else None
                    set_output_filter(lpf=float(parts[1]), poles=poles)
                    print(f"  filter set to {describe_filter(lpf_hz, lpf_poles, dc_hz)}")
                except ValueError as e:
                    print(f"  {e}")
        elif cmd == "dc":
            if len(parts) != 2:
                print(f"  current filter: {describe_filter(lpf_hz, lpf_poles, dc_hz)}")
                print("  usage: dc <hz>  (example: dc 30; dc 0 for off)")
            else:
                try:
                    set_output_filter(dc=float(parts[1]))
                    print(f"  filter set to {describe_filter(lpf_hz, lpf_poles, dc_hz)}")
                except ValueError as e:
                    print(f"  {e}")
        else:
            speak_words(parts)


def main():
    parser = argparse.ArgumentParser(
        description="TMS5220 LPC speech synthesizer (MAME-derived core).",
    )
    parser.add_argument(
        "--clock",
        type=float,
        default=SAMPLE_RATE,
        metavar="HZ",
        help=f"simulated LPC sample clock in Hz, e.g. 7760 or 8240 (default: {SAMPLE_RATE})",
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
    parser.add_argument("words", nargs="*", help="words to speak")
    args = parser.parse_args()

    try:
        set_playback_clock(args.clock)
        set_output_filter(args.lpf, args.poles, args.dc)
    except ValueError as e:
        parser.error(str(e))

    if args.load:
        try:
            loaded = load_vocab_sources(args.load)
            print(f"Loaded {loaded} external word(s) from {', '.join(args.load)}")
        except Exception as e:
            parser.error(f"error loading vocabulary: {e}")

    if args.words and args.words[0].lower() == "list":
        print_vocab(args.words[1:])
    elif args.words:
        speak_words(args.words)
    else:
        if not args.load:
            print(f"Using {len(vocab)} built-in word(s)")
        if playback_clock != SAMPLE_RATE:
            print(f"Playback clock: {playback_clock:g} Hz")
        print(f"Output filter: {describe_filter(lpf_hz, lpf_poles, dc_hz)}")
        run_interactive()


if __name__ == "__main__":
    main()
