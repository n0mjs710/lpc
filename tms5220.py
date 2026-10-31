#!/usr/bin/env python3
"""
TMS5220 speech synthesis core.

A Python port of MAME's tms5220 core: ROM tables and sample-level synthesis.
docs/TMS5220_MODEL.md specifies the model; docs/tms5220_ref.c is a
bit-identical C version.
"""

import numpy as np

from lpc_vocab import reverse_byte as _rev

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

SAMPLE_RATE    = 8000                            # nominal chip sample clock, Hz
MIN_CLOCK_HZ   = 4000                            # simulated clock limits
MAX_CLOCK_HZ   = 16000


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


def validate_clock(clock_hz: float) -> float:
    """Validate and return a simulated chip sample clock in Hz."""
    if not MIN_CLOCK_HZ <= clock_hz <= MAX_CLOCK_HZ:
        raise ValueError(f"clock must be between {MIN_CLOCK_HZ:g} and {MAX_CLOCK_HZ:g} Hz")
    return clock_hz
