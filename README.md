# TI LPC Speech Tools

Python tools for listening to, rendering, converting, and combining LPC speech
data for the TI TMS5100/TMS5220/TMS53C30 family of speech synthesizers.

The project uses Talkie-compatible `.cpp` vocabulary files as the working
vocabulary format. ASM source files can be converted into `.cpp`; the speak and
render tools load `.cpp` only.

## Setup

Use the project virtual environment when running the scripts:

```sh
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

The local `venv/` directory is intentionally ignored by git.

## Main Scripts

`tms_speak_interp.py` is the primary playback tool. It uses the higher-quality
renderer with 8-step frame interpolation, built-in TI number words by default,
external `.cpp` vocabulary loading, and live playback speed/pitch adjustment.

```sh
venv/bin/python tms_speak_interp.py
venv/bin/python tms_speak_interp.py ZERO ONE TWO
venv/bin/python tms_speak_interp.py --speed 1.03 ZERO ONE
venv/bin/python tms_speak_interp.py --load Vocab_FF800.cpp WZERO
```

`tms_speak_orig.py` is the historical baseline synthesizer. It keeps the older
no-interpolation synthesis path for comparison, but uses the same `.cpp`
vocabulary loading rule.

`render_vocab.py` renders the active vocabulary to exact 8 kHz, 16-bit, mono
WAV files. Playback speed/pitch changes are intentionally not applied here so
rendered PCM remains stable for target systems.

```sh
venv/bin/python render_vocab.py --out vocab_pcm
venv/bin/python render_vocab.py --load Vocab_FF800.cpp --out vocab_pcm
```

`convert_asm_cpp.py` converts one or more TI-style ASM `FCB` vocabulary files,
or directories of `.asm` files, into Talkie-compatible `.cpp`. The ASM bytes are
bit-reversed into the byte order expected by the Python/Talkie LPC reader.

```sh
venv/bin/python convert_asm_cpp.py "ASM Files" -o Vocab_ASM_Combined.cpp
venv/bin/python convert_asm_cpp.py "ASM Files/VM71003r.asm" -o Vocab_VM71003.cpp
```

`build_vocab.py` combines `.cpp` vocabulary files. It detects duplicate word
names and can prompt for each duplicate, keep the first copy, use the last copy,
keep both with a generated name, or stop with an error.

```sh
venv/bin/python build_vocab.py Vocab_US_Large.cpp Vocab_US_Clock.cpp -o Vocab_Combined.cpp
venv/bin/python build_vocab.py Vocab_FF800.cpp Vocab_US_Large.cpp -o Vocab_All.cpp --duplicates both
venv/bin/python build_vocab.py Vocab_FF800.cpp Vocab_US_Large.cpp -o Vocab_All.cpp --duplicates first --sort
```

`lpc_vocab.py` contains shared vocabulary parsing, ASM conversion, byte
formatting, and `.cpp` writing helpers used by the command-line tools.

## Vocabulary Files

`Vocab_US_Large.cpp` and `Vocab_US_Clock.cpp` are Talkie-compatible vocabulary
sets. `Vocab_ASM_Combined.cpp` is the reproducible converted output from the
ASM sources in `ASM Files/`. `Vocab_FF800.cpp` is the earlier converted snapshot
of the same ASM vocabulary, retained for comparison. `Vocab_Combined.cpp` is a
combined `.cpp` vocabulary file built from repo-local `.cpp` inputs.

To rebuild the ASM-derived vocabulary from source:

```sh
venv/bin/python convert_asm_cpp.py "ASM Files" -o Vocab_ASM_Combined.cpp
```

To make a combined vocabulary from any `.cpp` inputs:

```sh
venv/bin/python build_vocab.py Vocab_ASM_Combined.cpp Vocab_US_Large.cpp Vocab_US_Clock.cpp -o Vocab_Combined.cpp --duplicates first --sort
```

## Default Vocabulary Rule

The speak and render tools use built-in words only when no external vocabulary
file is specified. When `--load` or interactive `load <file.cpp>` is used, the
external `.cpp` vocabulary replaces the built-ins.

## Notes

Some bundled vocabulary data is derived from the Arduino Talkie vocabulary
files, as noted in source comments. Preserve the relevant upstream licensing
and attribution when redistributing vocabulary data.
