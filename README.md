# TI LPC Speech Tools

Python tools for playing, rendering, converting, and combining LPC speech data
for the TI TMS5220 and compatible speech synthesizers (TMS5220C, TMS53C30).

Vocabularies use the Talkie-compatible `.cpp` format. ASM sources can be
converted to `.cpp`; the speak and render tools load `.cpp` only. Source
vocabularies from other projects live under `sources/`; generated, ready-to-use
vocabularies live in the project root.

## Setup

Install the requirements into a virtual environment once:

```sh
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

Run `source venv/bin/activate` in each new terminal before using the tools.
`venv/` is ignored by git.

## Synthesis Model

The synthesizer in `tms_speak.py` is a Python port of MAME's `tms5220` core
(BSD-3-Clause). It uses the TMS5220 energy, pitch, K1-K10, and chirp ROM tables
(verified by MAME against decapped TMS5220 and TMS5220C dies) and reproduces
the chip's sample-level behavior: shift-based parameter interpolation,
interpolation inhibit on voiced/unvoiced/silence transitions, K5-K10 zeroing
on unvoiced frames, chirp and 13-bit LFSR excitation, 10-bit x 14-bit lattice
arithmetic, and 8-bit DAC output.

As on the chip, each word starts with about 25 ms of silence (the first frame
only sets the starting point) and ends with a 25 ms ramp to zero after the stop
frame. Loud words can clip at the DAC.

With no filter options, both tools output these raw DAC samples.
[docs/TMS5220_MODEL.md](docs/TMS5220_MODEL.md) specifies the model, lists what
changed from the earlier Talkie-based model, and is the reference for ports.
[docs/tms5220_ref.c](docs/tms5220_ref.c) is an integer-only C version whose
output is bit-identical to the Python core.

### Clock

`--clock HZ` sets the simulated chip sample clock (nominal 8000, range
4000-16000). It emulates controllers that trimmed the speech chip's RC
oscillator: pitch and duration change together. Useful values are usually
within a few hundred Hz of nominal, roughly 7760-8240.

### Output Stage

`--lpf` and `--dc` model the chip's DAC and the analog circuitry after it on
real boards. Both are off by default; setting either one enables the model. The
DAC holds each sample for one clock period, so the model builds that stepped
output at 6x the chip clock (48 kHz at 8000 Hz) and filters it there.

| Option | Filter | Range |
|---|---|---|
| `--dc HZ` | 1-pole high-pass (output coupling capacitor / DC blocker) | 1-300 Hz, `0` = off |
| `--lpf HZ` | Butterworth low-pass | 500-10000 Hz, `0` = off |
| `--poles N` | Low-pass order | 1-8, default 4 |

Cutoffs are in real Hz and do not move with `--clock`. Typical board-style
settings are `--dc 30 --lpf 4500`. Live playback plays the 6x oversampled
signal directly. `render_vocab.py` resamples it to the WAV rate. An 8 kHz WAV
keeps the response below 4 kHz only; `--rate 48000` keeps the full response and
matches live playback exactly.

## Tools

### tms_speak.py

Plays words through the synthesizer. It uses built-in TI number words unless a
vocabulary is loaded.

```sh
python tms_speak.py                                   # interactive
python tms_speak.py ZERO ONE TWO
python tms_speak.py --clock 8240 ZERO ONE
python tms_speak.py --dc 30 --lpf 4500 ZERO ONE
python tms_speak.py --load Vocab_FF800.cpp WZERO
python tms_speak.py --load Vocab_FF800.cpp list 'RE*'
```

Interactive commands: words to speak, `list [PATTERN ...]` (shell-style
wildcards such as `RE*`, `*ING`, `W?ERO`), `load <file.cpp>`, `builtin`,
`clock <hz>`, `lpf <hz> [poles]`, `dc <hz>`, `help`, `quit`. Tab completion
covers commands, words, and paths after `load` when `readline` is available.

### render_vocab.py

Renders every word in the active vocabulary to a 16-bit mono WAV file
(default `vocab_pcm/`, 8 kHz). It accepts the same `--load`, `--clock`, `--lpf`,
`--poles`, and `--dc` options as `tms_speak.py`, plus `--rate HZ` (8000-96000)
for the WAV sample rate. A non-default clock is resampled so the WAV rate stays
fixed.

```sh
python render_vocab.py --load Vocab_FF800.cpp --out vocab_pcm
python render_vocab.py --load Vocab_FF800.cpp --out vocab_pcm_fast --clock 8240
python render_vocab.py --load Vocab_FF800.cpp --out vocab_pcm_hifi --rate 48000 --dc 30 --lpf 4500
```

### pcm_speak.py

Plays rendered WAV vocabularies by word name (the WAV filename stem), for
auditioning PCM output. Defaults to `vocab_pcm/` and supports the same `list`
filters and tab completion.

```sh
python pcm_speak.py RED
python pcm_speak.py --dir vocab_pcm ZERO ONE TWO
python pcm_speak.py --dir vocab_pcm list 'RE*'
```

### pcm_phrase.py

Joins rendered WAV words into one phrase WAV with a silence gap between words.
Defaults to `vocab_pcm/`.

```sh
python pcm_phrase.py --out red-alert.wav RED ALERT
python pcm_phrase.py --dir vocab_pcm --gap-ms 90 --out callsign.wav THIS IS WZERO
python pcm_phrase.py --amplitude-scale 0.75 --out quieter.wav RED ALERT
```

### build_vocab.py

Builds `.cpp` vocabularies from `.asm` sources, `.cpp` sources, or both.
Directories are accepted and processed alphabetically. ASM `FCB` bytes are
bit-reversed into the byte order the LPC reader expects. Bare output filenames
are written to the current directory; use `--out-dir DIR` or a path in `-o` to
write elsewhere.

```sh
python build_vocab.py sources/asm/ff800 -o Vocab_FF800.cpp
python build_vocab.py sources/asm/ff800 sources/cpp/talkie/Vocab_US_Large.cpp sources/cpp/talkie/Vocab_US_Clock.cpp -o Vocab_Combined.cpp --duplicates first --sort
```

`--duplicates` controls duplicate word names. Input order sets priority for
`first` and `last`.

| Policy | Behavior |
|---|---|
| `prompt` (default) | Ask per duplicate: `1` keep existing, `2` use incoming, `b` keep both (with a suggested or typed new name) |
| `first` | Keep the first copy |
| `last` | Keep the last copy |
| `both` | Keep both, auto-naming the incoming copy |
| `error` | Stop at the first duplicate |

```text
Duplicate word: ONE (different LPC bytes)
  existing: Vocab_US_Large.cpp, 68 bytes
  incoming: FF800_FFWRDAr.asm (ONE), 72 bytes
  [1] keep existing
  [2] use incoming
  [b] keep both
choice [1/2/b]: b
name for incoming duplicate [ONE_FF800_FFWRDAR]:
```

### Other Files

- `tms_speak_orig.py`: the original no-interpolation synthesizer, kept for
  comparison.
- `lpc_vocab.py`: vocabulary parsing, ASM conversion, and `.cpp` writing.
- `lpc_audio.py`: WAV I/O, normalization, resampling, and the output stage
  model.

## Vocabulary Files

- `sources/asm/ff800/`: FF-800 ASM files, prefixed `FF800_`.
- `sources/cpp/talkie/`: Talkie `.cpp` vocabularies.
- `Vocab_FF800.cpp`: generated from `sources/asm/ff800/`.
- `Vocab_Combined.cpp`: generated from the FF-800 and Talkie sources (rebuild
  command above).

The speak and render tools use the built-in words only when no vocabulary is
loaded. `--load` or interactive `load <file.cpp>` replaces the built-ins.

## Attribution

- The synthesis core and its ROM tables are ported from MAME's `tms5220`
  emulation (`src/devices/sound/tms5220.cpp`, `tms5110r.hxx`), BSD-3-Clause,
  copyright holders Frank Palazzolo, Aaron Giles, Jonathan Gevaryahu, Raphael
  Nabet, Couriersud, and Michael Zapf.
- `sources/cpp/talkie/Vocab_US_Large.cpp` and `Vocab_US_Clock.cpp` come from
  the Arduino Talkie library (Peter Knight, 2011, GPLv2), converted to `.c`/`.h`
  form with unique names by Armin Joachimsmeyer in 2018. The data derives from
  TI VM61002-VM61005 speech ROMs.
- The ASM vocabularies in `sources/asm/ff800/` were contributed by Joe Haas,
  KE0FF, from his FF-800 repeater controller project. This project benefits
  directly from his pioneering work from roughly 30 years ago and from his
  support in making that material usable here.

Preserve the relevant upstream notices and license terms when redistributing
the bundled vocabulary data.
