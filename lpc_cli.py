#!/usr/bin/env python3
"""Shared command-line options and interactive shell for the LPC speech tools."""

import argparse
import fnmatch
import glob
from collections.abc import Callable, Iterable
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

import numpy as np

from lpc_audio import DEFAULT_DC_HZ, DEFAULT_LPF_HZ, DEFAULT_LPF_POLES, validate_filter
from tms5220 import SAMPLE_RATE, validate_clock

WORD_GAP_MS = 60   # silence between words in milliseconds


# ─────────────────────────────────────────────────────────────────────────────
# Command-line options
# ─────────────────────────────────────────────────────────────────────────────

def add_synth_args(parser: argparse.ArgumentParser) -> None:
    """Add the vocabulary, clock, and output-stage options."""
    parser.add_argument(
        "--load",
        "-l",
        action="append",
        default=[],
        metavar="PATH",
        help="replace built-ins with one or more .cpp vocabulary files",
    )
    parser.add_argument(
        "--clock",
        type=float,
        default=SAMPLE_RATE,
        metavar="HZ",
        help=f"simulated chip sample clock in Hz, e.g. 7760 or 8240 (default: {SAMPLE_RATE})",
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


def check_synth_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Validate the options added by add_synth_args()."""
    try:
        validate_clock(args.clock)
        validate_filter(args.lpf, args.poles, args.dc)
    except ValueError as e:
        parser.error(str(e))


def add_phrase_args(parser: argparse.ArgumentParser) -> None:
    """Add the options for joining words into a phrase."""
    parser.add_argument(
        "--gap-ms",
        type=int,
        default=WORD_GAP_MS,
        metavar="MS",
        help=f"silence between words in milliseconds (default: {WORD_GAP_MS})",
    )
    parser.add_argument(
        "--amplitude-scale",
        "--scale",
        type=float,
        default=1.0,
        metavar="FACTOR",
        help="multiply output amplitude by this factor (default: 1.0)",
    )


def check_phrase_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Validate the options added by add_phrase_args()."""
    if args.gap_ms < 0:
        parser.error("gap must be zero or greater")
    if not np.isfinite(args.amplitude_scale) or args.amplitude_scale < 0.0:
        parser.error("amplitude scale must be finite and zero or greater")


# ─────────────────────────────────────────────────────────────────────────────
# Word lists
# ─────────────────────────────────────────────────────────────────────────────

def words_matching(words: Iterable[str], patterns: list[str] | None = None) -> list[str]:
    """Return sorted words matching shell-style wildcard patterns."""
    if not patterns:
        return sorted(words)
    normalized = [pattern.upper() for pattern in patterns]
    return sorted(
        {
            word
            for word in words
            for pattern in normalized
            if fnmatch.fnmatchcase(word, pattern)
        }
    )


def print_words(words: Iterable[str], patterns: list[str] | None = None) -> None:
    """Print words in columns, optionally filtered by wildcards."""
    matches = words_matching(words, patterns)
    if not matches:
        print(f"  no words match: {' '.join(patterns or [])}")
        return

    cols = 6
    rows = (len(matches) + cols - 1) // cols
    for r in range(rows):
        line = [matches[r + c * rows] for c in range(cols) if r + c * rows < len(matches)]
        print("  " + "  ".join(f"{w:<14}" for w in line))
    suffix = f" matching {' '.join(patterns)}" if patterns else " total"
    print(f"\n  {len(matches)} words{suffix}")


# ─────────────────────────────────────────────────────────────────────────────
# Interactive shell
# ─────────────────────────────────────────────────────────────────────────────

_BASE_COMMANDS = ("list", "help", "quit", "exit", "q")


def _install_completion(commands: list[str], get_words: Callable[[], Iterable[str]]) -> None:
    if readline is None:
        return

    def word_choices(text: str) -> list[str]:
        prefix = text.upper()
        return [f"{word} " for word in sorted(get_words()) if word.startswith(prefix)]

    def path_choices(text: str) -> list[str]:
        matches = []
        for match in glob.glob(f"{text}*"):
            suffix = "/" if Path(match).is_dir() else " "
            matches.append(f"{match}{suffix}")
        return sorted(matches)

    def complete(text: str, state: int) -> str | None:
        prior_parts = readline.get_line_buffer()[:readline.get_begidx()].split()
        if not prior_parts:
            choices = [f"{cmd} " for cmd in commands if cmd.startswith(text.lower())]
            choices.extend(word_choices(text))
        elif prior_parts[0].lower() == "load":
            choices = path_choices(text)
        else:
            choices = word_choices(text)
        return choices[state] if state < len(choices) else None

    readline.set_completer(complete)
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


def run_shell(
    prompt: str,
    banner: str,
    help_text: str,
    commands: dict[str, Callable[[list[str]], None]],
    get_words: Callable[[], Iterable[str]],
    speak: Callable[[list[str]], None],
) -> None:
    """
    Run an interactive loop. Handles list/help/quit; other first words go to
    commands[name](args), and anything else is passed to speak(words).
    """
    _install_completion(list(_BASE_COMMANDS) + list(commands), get_words)
    print(banner)
    while True:
        try:
            line = input(prompt).strip()
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
            print(help_text)
        elif cmd == "list":
            print_words(get_words(), parts[1:])
        elif cmd in commands:
            commands[cmd](parts[1:])
        else:
            speak(parts)
