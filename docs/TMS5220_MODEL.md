# TMS5220 Synthesis Model

This document specifies the synthesis model in `tms5220.py` (`synthesize()`)
and the output-stage options in `lpc_audio.py`, for porting to other
implementations. The purpose of the model is to reproduce a real TMS5220
accurately, with filter and rendering options to match a variety of real-world
implementations of the original hardware.

- Source of tables and behavior: MAME `src/devices/sound/tms5220.cpp` and
  `tms5110r.hxx` (BSD-3-Clause).
- C reference: [tms5220_ref.c](tms5220_ref.c). It is integer-only, written for
  16-bit `int` targets (AVR), and compiles for `atmega328p` with no warnings.
  Its output is bit-identical to `synthesize()` for all 1081 words in
  `Vocab_FF800.cpp`, `Vocab_US_Large.cpp`, `Vocab_US_Clock.cpp`, and the
  built-in vocabulary.

## Changes From the Previous Model

The previous model was based on the Arduino Talkie synthesizer.

| Area | Previous | Current |
|---|---|---|
| Energy table | `0,2,3,4,5,7,10,15,20,32,41,57,81,114,161,255` | TMS5220 ROM `0,1,2,3,4,6,8,11,16,23,33,47,63,85,114,0` |
| Pitch table | 5220 values offset by +1 (`0,16,17,...,160`) | TMS5220 ROM (`0,15,16,...,159`) |
| K tables | 8-bit, about 1/4 of ROM values | 10-bit ROM values |
| Chirp | 21 entries; `(CHIRP[n] * energy) >> 8` | Same 21 values, zero through entry 51; scaled by energy inside the lattice |
| Interpolation | Linear, 8 equal steps of 25 samples, all parameters at once | Shift steps `3,3,3,2,2,1,1` then exact target; one parameter per PC slot |
| Interpolation inhibit | None | Held on voiced/unvoiced and silence transitions (see below) |
| Silence frame | Energy and pitch targets 0 | Energy index 0; pitch and K indices unchanged |
| Unvoiced frame | K5-K10 keep their previous values | K5-K10 forced to 0 |
| Voiced/unvoiced switch | When the interpolated pitch is 0 | When interpolation ends (OLDP latch at IP 7) |
| Unvoiced excitation | 16-bit Galois LFSR (`0xB800`), 1 step per sample, `+/-energy` | 13-bit LFSR (taps 12,3,2,0), 20 steps per sample, `+/-64` |
| Energy application | Multiplied into excitation | `(previous_energy * excitation) >> 3` into the lattice; energy delayed one sample |
| Lattice | 8-bit K, `>> 7`, output clamped to -512..511 | 10-bit K x 14-bit operand, `>> 9`, operands wrapped to 14 bits |
| Output | `u0 / 512` | Wrap to 14 bits, clamp -2048..2047, keep the top 8 bits (8-bit DAC) |
| Start | Interpolates from 0 toward the first frame | First frame is the interpolation start point; about 25 ms of silence first |
| Stop | Output ends at the stop frame | Energy ramps to 0 over the remaining 7 IPs, then output ends |
| Data past end | Synthesis loop ends; a read past the end raises an error | Bits read as 0; running out at a frame boundary acts as a stop frame |

## Bitstream

- Talkie `.cpp` byte arrays. Bits are consumed LSB-first within each byte, and
  each field is assembled MSB-first. This is the same as reversing each byte
  and reading MSB-first.
- Frame: energy (4 bits). `0` = silence frame (no more fields). `15` = stop
  frame. Otherwise: repeat (1), pitch (6). If repeat = 1, no more fields.
  Otherwise K1-K4 (5,5,4,4 bits). If pitch != 0, also K5-K10 (4,4,4,3,3,3 bits).

## Tables

`ENERGY[16]`, `PERIOD[64]`, `K1`-`K10`, `CHIRP`, and `INTERP_SHIFT[8] =
{0,3,3,3,2,2,1,1}` are listed in `tms5220_ref.c` and `tms5220.py`. The C
reference stores them as plain `const`. On AVR this places them in RAM unless
they are moved to `PROGMEM` and read with `pgm_read_byte`/`pgm_read_word`.

## State

| State | Initial value |
|---|---|
| `e_idx`, `p_idx` | 0 |
| `k_idx[10]` | `0,0,0,0,15,15,15,7,7,7` |
| `energy`, `prev_energy`, `pitch`, `k[10]` | 0 |
| `zpar`, `uv_zpar`, `old_e`, `old_p`, `inhibit`, `talk` | 1 |
| `pitch_zero` | 0 |
| `ip`, `pc`, `subcycle` | 0, 0, 1 |
| `pitch_count` | 0 |
| `rng` | `0x1FFF` |
| `u[10]`, `x[10]` | 0 |

## Per-Sample Sequence

Each sample runs these steps in order.

**1. Frame load** when `ip == 0 && pc == 12 && subcycle == 1`:
- `zpar = uv_zpar = 0`.
- Read `e_idx`; if no data remains, `e_idx = 15`.
- If `e_idx == 15`: `talk = 0`.
- Else if `e_idx != 0`: read repeat and `p_idx`; set `uv_zpar = (p_idx == 0)`;
  if not repeat, read K1-K4 into `k_idx`, and K5-K10 if `p_idx != 0`.
- `inhibit = (!old_p && p_idx == 0) || (old_p && p_idx != 0) || (old_e && e_idx != 0) || (old_p && e_idx == 0)`.

**2. Interpolation** otherwise, when `subcycle == 2`. Here
`shift = INTERP_SHIFT[ip]`, `hold = inhibit && ip != 0`, and
`step(t, c) = hold ? 0 : (t - c) >> shift` (arithmetic shift).
- `pc == 0`: if `ip == 0`, `pitch_zero = 0`. Then
  `energy = zpar ? 0 : energy + step(ENERGY[e_idx], energy)`.
- `pc == 1`: `pitch = zpar ? 0 : pitch + step(PERIOD[p_idx], pitch)`.
- `pc` 2-11: `n = pc - 2`. Then
  `k[n] = Z ? 0 : k[n] + step(K[n][k_idx[n]], k[n])`, where `Z` is `zpar` for
  n < 4 and `uv_zpar` for n >= 4.

**3. Excitation**
- If `old_p`: `exc = (rng & 1) ? -64 : 64`.
- Else: `exc = CHIRP[min(pitch_count, 51)]` (0 for indices 21-51).
- Then clock the LFSR 20 times:
  `bit = (rng>>12 ^ rng>>3 ^ rng>>2 ^ rng) & 1; rng = ((rng << 1) | bit) & 0x1FFF`.

**4. Lattice**, with `wrap14(v) = ((v + 0x4000) & 0x7FFF) - 0x4000` and
`mul(k, v) = (k * wrap14(v)) >> 9`:
```
y = (prev_energy * exc) >> 3
for i = 9..0:  y -= mul(k[i], x[i]);  u[i] = y
for i = 9..1:  x[i] = x[i-1] + mul(k[i-1], u[i-1])
x[0] = u[0]
prev_energy = energy
```

**5. Output**: `out = clamp(wrap14(u[0]), -2048, 2047)`. The DAC value is
`out >> 4` (-128..127). The Python core returns `(out & ~0xF) / 2048.0`.

**6. Counters**
```
subcycle++
if subcycle == 2 && pc == 12:
    if ip == 7:
        if inhibit: pitch_zero = 1
        old_e = (e_idx == 0); old_p = (p_idx == 0)
        if !talk: end after this sample
    subcycle = 1; pc = 0; ip = (ip + 1) & 7
elif subcycle == 3:
    subcycle = 1; pc++
pitch_count++
if pitch_count >= pitch || pitch_zero: pitch_count = 0
pitch_count &= 0x1FF
```

PC runs 0-12; PCs 0-11 take two samples each and PC 12 takes one, giving 25
samples per IP and 200 samples (25 ms at 8 kHz) per frame.

## Integer Widths

- `u`, `x`, and `y` can be stored as wrapping 16-bit values. Every use passes
  through `wrap14` (which depends only on the value mod 2^15), so the result
  equals unbounded arithmetic. Do the additions and subtractions in `uint16_t`
  to avoid signed-overflow undefined behavior.
- `k * wrap14(v)` needs 32 bits (up to 2^23). With 16-bit `int` (AVR), cast
  to `int32_t` before multiplying.
- `prev_energy * exc` fits in 16 bits (at most 114 x 113).
- Right shifts of negative values must be arithmetic (avr-gcc and gcc are).
- `energy`, `pitch`, and `k[]` stay within their table ranges.

## Clock

The sample clock is the rate at which samples are produced and the DAC is
updated, nominally 8000 Hz. The tools' `--clock HZ` changes this rate (range
4000-16000). On an MCU it is the sample timer rate.

## Output Stage (Python Tools Only)

The C reference outputs raw 8-bit DAC values. The Python tools optionally model
the analog stage that follows the DAC on real boards. It is off by default and
enabled by `--lpf` or `--dc`:

1. Zero-order hold: each sample is repeated 6 times (6x the chip clock).
2. DC blocker: 1-pole Butterworth high-pass at `--dc` Hz (1-300).
3. Low-pass: `--poles`-order (1-8, default 4) Butterworth at `--lpf` Hz
   (500-10000, limited to 0.45 x the oversampled rate).

Filters are designed with `scipy.signal.butter` at the oversampled rate and
applied causally (`sosfilt`). Live playback plays the oversampled signal.
`tms_speak.py --render` resamples it to `--rate` (default 8000). On hardware, these
stages correspond to the analog reconstruction filter and coupling capacitor
after the DAC or PWM output.

## Tool Behavior Outside the Chip Model

- Output is peak-normalized to 0.9: per phrase for playback and `--out`, per
  word for `--render`. `--amplitude-scale` is applied after normalization.
- Playback and `--out` insert `--gap-ms` (default 60 ms) of silence between
  words.
- With `--render`, a non-default `--clock` is resampled to the fixed WAV rate.
