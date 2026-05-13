#!/usr/bin/env python3
"""
Build Talkie-compatible .cpp vocabulary files from ASM and/or .cpp sources.

Examples:
  python3 build_vocab.py sources/asm/ff800 -o Vocab_FF800.cpp
  python3 build_vocab.py sources/cpp/talkie -o Vocab_Talkie.cpp --duplicates first
  python3 build_vocab.py sources/asm/ff800 sources/cpp/talkie -o Vocab_Combined.cpp --duplicates first --sort
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lpc_vocab import (
    VocabEntry,
    c_identifier_fragment,
    clean_vocab_name,
    parse_asm_file,
    parse_cpp_file,
    source_suffix,
    write_cpp_file,
)

SUPPORTED_SUFFIXES = {".asm", ".cpp"}


def normalized_lookup_name(name: str) -> str:
    return c_identifier_fragment(clean_vocab_name(name))


def with_name(entry: VocabEntry, name: str) -> VocabEntry:
    return VocabEntry(
        name=normalized_lookup_name(name),
        data=entry.data,
        source=entry.source,
        raw_name=entry.raw_name,
    )


def unused_name(base: str, used: set[str]) -> str:
    base = normalized_lookup_name(base)
    if base not in used:
        return base

    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


def duplicate_default_name(entry: VocabEntry, used: set[str]) -> str:
    return unused_name(f"{entry.name}_{source_suffix(entry.source)}", used)


def describe_entry(entry: VocabEntry) -> str:
    raw = f" ({entry.raw_name})" if entry.raw_name and entry.raw_name != entry.name else ""
    return f"{entry.source}{raw}, {len(entry.data)} bytes"


def prompt_duplicate(existing: VocabEntry, incoming: VocabEntry, used: set[str]) -> tuple[str, str | None]:
    same = "same LPC bytes" if existing.data == incoming.data else "different LPC bytes"
    print()
    print(f"Duplicate word: {incoming.name} ({same})")
    print(f"  existing: {describe_entry(existing)}")
    print(f"  incoming: {describe_entry(incoming)}")
    print("  [1] keep existing")
    print("  [2] use incoming")
    print("  [b] keep both")

    while True:
        choice = input("choice [1/2/b]: ").strip().lower()
        if choice in ("", "1", "e", "existing", "first"):
            return "first", None
        if choice in ("2", "i", "incoming", "new", "last"):
            return "last", None
        if choice in ("b", "both"):
            default = duplicate_default_name(incoming, used)
            while True:
                answer = input(f"name for incoming duplicate [{default}]: ").strip()
                name = normalized_lookup_name(answer or default)
                if name in used:
                    print(f"  {name} already exists; choose a different name")
                    continue
                return "both", name
        print("  enter 1, 2, or b")


def resolve_duplicate(
    result: list[VocabEntry],
    by_name: dict[str, int],
    used: set[str],
    incoming: VocabEntry,
    policy: str,
) -> str:
    existing = result[by_name[incoming.name]]

    if policy == "error":
        raise ValueError(
            f"duplicate word {incoming.name}: {describe_entry(existing)} and {describe_entry(incoming)}"
        )

    choice = policy
    new_name: str | None = None
    if policy == "prompt":
        if not sys.stdin.isatty():
            raise ValueError(
                f"duplicate word {incoming.name}; rerun with --duplicates first, last, both, or error"
            )
        choice, new_name = prompt_duplicate(existing, incoming, used)

    if choice == "first":
        return "kept existing"

    if choice == "last":
        result[by_name[incoming.name]] = incoming
        return "used incoming"

    if choice == "both":
        if new_name is None:
            new_name = duplicate_default_name(incoming, used)
        renamed = with_name(incoming, new_name)
        by_name[renamed.name] = len(result)
        used.add(renamed.name)
        result.append(renamed)
        return f"kept both as {renamed.name}"

    raise ValueError(f"unsupported duplicate policy: {policy}")


def supported_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(
            child
            for child in path.rglob("*")
            if child.is_file() and child.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if not files:
            raise ValueError(f"no .asm or .cpp vocabulary files found in directory: {path}")
        return files

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"expected a .asm or .cpp vocabulary file: {path}")
    return [path]


def parse_source_file(path: Path) -> list[VocabEntry]:
    suffix = path.suffix.lower()
    if suffix == ".asm":
        return parse_asm_file(path)
    if suffix == ".cpp":
        return parse_cpp_file(path)
    raise ValueError(f"expected a .asm or .cpp vocabulary file: {path}")


def resolve_output_path(out_name: str, out_dir: str) -> Path:
    out_path = Path(out_name)
    if out_path.is_absolute() or out_path.parent != Path("."):
        return out_path
    return Path(out_dir) / out_path


def combine_entries(
    inputs: list[str],
    duplicate_policy: str,
    sort_entries: bool,
) -> tuple[list[VocabEntry], int, list[str]]:
    result: list[VocabEntry] = []
    by_name: dict[str, int] = {}
    used: set[str] = set()
    duplicates = 0
    decisions: list[str] = []

    source_files: list[Path] = []
    for item in inputs:
        source_files.extend(supported_files(Path(item)))

    for path in source_files:
        entries = parse_source_file(path)
        print(f"{path.name}: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")

        for entry in entries:
            entry = with_name(entry, entry.name)
            if entry.name not in by_name:
                by_name[entry.name] = len(result)
                used.add(entry.name)
                result.append(entry)
                continue

            duplicates += 1
            action = resolve_duplicate(result, by_name, used, entry, duplicate_policy)
            decisions.append(f"{entry.name}: {action} ({entry.source})")

    if sort_entries:
        result.sort(key=lambda entry: entry.name)

    return result, duplicates, decisions


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a Talkie-compatible .cpp vocabulary from .asm and/or .cpp sources.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="one or more .asm/.cpp vocabulary files or directories",
    )
    parser.add_argument(
        "-o",
        "--out",
        required=True,
        help="output .cpp filename or path",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="directory for bare output filenames (default: project root/current directory)",
    )
    parser.add_argument(
        "--duplicates",
        choices=("prompt", "first", "last", "both", "error"),
        default="prompt",
        help="how to resolve duplicate word names (default: prompt)",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="sort output entries by vocabulary name",
    )
    parser.add_argument(
        "--symbol-prefix",
        default="sp_",
        help="C symbol prefix to write; stripped again by project loaders (default: sp_)",
    )
    args = parser.parse_args()

    try:
        out_path = resolve_output_path(args.out, args.out_dir)
        entries, duplicates, decisions = combine_entries(
            inputs=args.inputs,
            duplicate_policy=args.duplicates,
            sort_entries=args.sort,
        )
        write_cpp_file(
            entries=entries,
            out_path=out_path,
            title="TI LPC vocabulary",
            prefix=args.symbol_prefix,
        )
    except Exception as exc:
        parser.exit(1, f"error: {exc}\n")

    print()
    print(f"Wrote {len(entries)} entries to {out_path}")
    print(f"Duplicate names found: {duplicates}")
    for decision in decisions:
        print(f"  {decision}")


if __name__ == "__main__":
    main()
