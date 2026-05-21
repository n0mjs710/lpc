# TI LPC Speech Tools

Python tools for listening to, rendering, converting, and combining LPC speech
data for the TI TMS5100/TMS5220/TMS53C30 family of speech synthesizers.

The project uses Talkie-compatible `.cpp` vocabulary files as the working
vocabulary format. ASM source files can be converted into `.cpp`; the speak and
render tools load `.cpp` only.

Source vocabulary files from other projects live under `sources/`. The project
root is reserved for the ready-to-use generated `.cpp` vocabularies.

## Setup

These scripts need the packages listed in `requirements.txt`. The recommended
way to install them is in a Python virtual environment, usually called a
`venv`. A virtual environment keeps this project's Python packages separate
from the rest of your system.

Create the virtual environment once:

```sh
python3 -m venv venv
```

Activate it before working in the project:

```sh
source venv/bin/activate
```

After activation, your shell prompt usually shows `(venv)`. Install the
requirements into that active environment:

```sh
python -m pip install -r requirements.txt
```

Run the tools with `python` while the environment is active:

```sh
python tms_speak.py ZERO ONE TWO
```

When you are done, you can leave the environment:

```sh
deactivate
```

The local `venv/` directory is intentionally ignored by git. If you open a new
terminal later, run `source venv/bin/activate` again before using the tools.

## Main Scripts

`tms_speak.py` is the primary playback tool. It uses the higher-quality renderer
with 8-step frame interpolation, built-in TI number words by default, external
`.cpp` vocabulary loading, and live playback speed/pitch adjustment.

```sh
python tms_speak.py
python tms_speak.py ZERO ONE TWO
python tms_speak.py --speed 1.03 ZERO ONE
python tms_speak.py --load Vocab_FF800.cpp WZERO
python tms_speak.py --load Vocab_FF800.cpp list 'RE*'
```

In interactive mode, `list` accepts shell-style wildcard filters such as
`list RE*`, `list *ING`, or `list W?ERO`. Tab completion is enabled for command
names, vocabulary words, and paths after `load` when Python's `readline`
support is available.

`tms_speak_orig.py` is the historical baseline synthesizer. It keeps the older
no-interpolation synthesis path for comparison, but uses the same `.cpp`
vocabulary loading rule.

`render_vocab.py` renders the active vocabulary to exact 8 kHz, 16-bit, mono
WAV files. By default it renders at the normal LPC timing. The optional
`--speed` value resamples the PCM while keeping the WAV sample rate at exactly
8 kHz, so `--speed 1.03` sounds like LPC playback at 8240 Hz but still produces
standard 8 kHz PCM. The default output directory is `vocab_pcm/` in this repo.

```sh
python render_vocab.py --out vocab_pcm
python render_vocab.py --load Vocab_FF800.cpp --out vocab_pcm
python render_vocab.py --load Vocab_FF800.cpp --out vocab_pcm_fast --speed 1.03
```

The `--speed` option emulates repeater controllers that varied the speech chip
clock with an RC oscillator adjustment. Pitch and duration change together; this
is not a modern pitch-only shifter. The default `--speed 1.0` path leaves the
rendered PCM timing unchanged. When speed shifting is requested, the renderer
uses SciPy's polyphase resampler so the WAV file still has a clean 8 kHz sample
rate for target systems.

`pcm_speak.py` plays rendered WAV vocabularies by word name, using the WAV
filename stem as the word. Use it to compare rendered PCM against LPC playback
or to audition PCM output before loading it into a target system. By default it
loads `vocab_pcm/` in this repo.

```sh
python pcm_speak.py RED
python pcm_speak.py --dir vocab_pcm ZERO ONE TWO
python pcm_speak.py --dir vocab_pcm
python pcm_speak.py --dir vocab_pcm list 'RE*'
```

The PCM player supports the same interactive `list` wildcard filters and tab
completion for vocabulary words.

`pcm_phrase.py` builds a single WAV phrase from rendered PCM vocabulary words.
It uses the same filename-stem word names as `pcm_speak.py`, inserts a short
silence gap between words, and writes one mono WAV file. It also defaults to
the repo-local `vocab_pcm/` directory.

```sh
python pcm_phrase.py --out red-alert.wav RED ALERT
python pcm_phrase.py --dir vocab_pcm --out phrase.wav ZERO ONE TWO
python pcm_phrase.py --dir vocab_pcm --gap-ms 90 --out callsign.wav THIS IS WZERO
python pcm_phrase.py --amplitude-scale 0.75 --out quieter.wav RED ALERT
```

`build_vocab.py` builds `.cpp` vocabulary files from `.asm` sources, `.cpp`
sources, or a mix of both. Directories are accepted as inputs. ASM `FCB` bytes
are bit-reversed into the byte order expected by the Python/Talkie LPC reader.
It detects duplicate word names and can either prompt for each duplicate or use
a fixed policy.

```sh
python build_vocab.py sources/asm/ff800 -o Vocab_FF800.cpp
python build_vocab.py sources/cpp/talkie/Vocab_US_Large.cpp sources/cpp/talkie/Vocab_US_Clock.cpp -o Vocab_Talkie.cpp --duplicates first
python build_vocab.py sources/asm/ff800 sources/cpp/talkie/Vocab_US_Large.cpp sources/cpp/talkie/Vocab_US_Clock.cpp -o Vocab_Combined.cpp --duplicates first --sort
```

Duplicate handling is controlled with `--duplicates`:

- `prompt` is the default. For each duplicate, the tool shows the existing
  source, incoming source, byte count, and whether the LPC bytes are the same or
  different. Choose `1` to keep the existing entry, `2` to replace it with the
  incoming entry, or `b` to keep both.
- When you choose `b` interactively, the tool suggests a new name based on the
  incoming word and source filename. Press Enter to accept that generated name,
  or type your own replacement name. The name is normalized into the safe
  vocabulary symbol style used by the project. If the name is already in use,
  the tool asks again.
- `first` always keeps the first copy of a duplicate name and ignores later
  copies.
- `last` always replaces the earlier copy with the later copy.
- `both` keeps both entries automatically, using generated names for duplicate
  incoming entries.
- `error` stops as soon as a duplicate name is found. This is useful when you
  expect a source set to be internally unique.

Interactive duplicate handling looks like this:

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

Use `--duplicates prompt` when curating a vocabulary by hand. Use `first`,
`last`, `both`, or `error` for repeatable scripted builds, continuous
integration, or any shell where interactive input is not available.

Input order controls duplicate priority for `--duplicates first` and
`--duplicates last`. Files inside a directory input are processed
alphabetically, so use explicit file paths when duplicate priority matters.
Bare output filenames are written in the project root/current directory. Use
`--out-dir DIR` or pass a path to `-o` to write somewhere else.

`lpc_vocab.py` contains shared vocabulary parsing, ASM conversion, byte
formatting, and `.cpp` writing helpers used by the command-line tools.

`lpc_audio.py` contains shared PCM helpers for WAV loading/writing, peak
normalization, and PCM resampling for clock-style speed/pitch changes.

## Vocabulary Files

The original source vocabularies are kept under `sources/`:

- `sources/asm/ff800/` contains the FF-800 ASM files. These filenames are
  prefixed with `FF800_` so their origin stays clear.
- `sources/cpp/talkie/` contains the Talkie `.cpp` vocabulary files.

The ready-to-use generated vocabularies live in the project root:

- `Vocab_FF800.cpp` is generated from `sources/asm/ff800/`.
- `Vocab_Combined.cpp` is generated from the FF-800 ASM sources plus the Talkie
  `.cpp` sources.

To rebuild the ASM-derived vocabulary from source:

```sh
python build_vocab.py sources/asm/ff800 -o Vocab_FF800.cpp
```

To rebuild the combined vocabulary:

```sh
python build_vocab.py sources/asm/ff800 sources/cpp/talkie/Vocab_US_Large.cpp sources/cpp/talkie/Vocab_US_Clock.cpp -o Vocab_Combined.cpp --duplicates first --sort
```

## Default Vocabulary Rule

The speak and render tools use built-in words only when no external vocabulary
file is specified. When `--load` or interactive `load <file.cpp>` is used, the
external `.cpp` vocabulary replaces the built-ins.

## Attribution

This project stands on work from several people and communities:

- The `sources/cpp/talkie/Vocab_US_Large.cpp` and
  `sources/cpp/talkie/Vocab_US_Clock.cpp` files come from the Arduino Talkie
  library. Their source headers credit Peter Knight, copyright 2011, and state
  that the code is released under the GPLv2 license.
- Those Talkie vocabulary headers also credit Armin Joachimsmeyer for converting
  the vocabulary data to `.c`/`.h` form and making the names unique in 2018.
- The Talkie vocabulary data is derived from TI VM61002/VM61003/VM61004/VM61005
  speech ROMs. The synthesizer code uses coefficient tables compatible with the
  TI TMS5100/TMS5220 family.
- The frame interpolation behavior in `tms_speak.py` was informed by the
  real chip behavior and by MAME's TMS speech synthesizer emulation.
- The ASM vocabulary files in `sources/asm/ff800/` were contributed by Joe
  Haas, KE0FF, from his FF-800 repeater controller project. This project
  benefits directly from his pioneering work from roughly 30 years ago and from
  his support in making that material usable here.

Preserve the relevant upstream notices and license terms when redistributing
the bundled vocabulary data.
