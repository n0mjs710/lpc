#!/usr/bin/env python3
"""
Convert TI LPC ASM FCB vocabulary files to Talkie-compatible .cpp files.

Examples:
  python3 convert_asm_cpp.py "ASM Files" -o Vocab_ASM_Combined.cpp
  python3 convert_asm_cpp.py "ASM Files/VM71003r.asm" -o Vocab_VM71003.cpp
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from lpc_vocab import VocabEntry, collect_asm_entries, source_suffix, unique_name, write_cpp_file


def rename_duplicate_entries(entries: list[VocabEntry], policy: str) -> list[VocabEntry]:
    counts = Counter(entry.name for entry in entries)
    if all(count == 1 for count in counts.values()):
        return entries

    duplicate_names = sorted(name for name, count in counts.items() if count > 1)
    if policy == "error":
        names = ", ".join(duplicate_names)
        raise ValueError(f"duplicate ASM labels found: {names}; use --duplicates suffix")

    seen: dict[str, int] = {}
    used_names: set[str] = set()
    renamed: list[VocabEntry] = []
    for entry in entries:
        if counts[entry.name] == 1:
            used_names.add(entry.name)
            renamed.append(entry)
            continue

        seen[entry.name] = seen.get(entry.name, 0) + 1
        if seen[entry.name] == 1:
            used_names.add(entry.name)
            renamed.append(entry)
            continue

        new_name = unique_name(f"{entry.name}_{source_suffix(entry.source)}", used_names)
        renamed.append(
            VocabEntry(
                name=new_name,
                data=entry.data,
                source=entry.source,
                raw_name=entry.raw_name,
            )
        )
    return renamed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert TI LPC ASM FCB data into a Talkie-compatible .cpp vocabulary.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="one or more .asm files or directories containing .asm files",
    )
    parser.add_argument(
        "-o",
        "--out",
        required=True,
        help="output .cpp vocabulary file",
    )
    parser.add_argument(
        "--symbol-prefix",
        default="spasm_",
        help="C symbol prefix to write; stripped again by project loaders (default: spasm_)",
    )
    parser.add_argument(
        "--duplicates",
        choices=("error", "suffix"),
        default="error",
        help="how to handle duplicate ASM labels in the conversion input",
    )
    args = parser.parse_args()

    try:
        entries = collect_asm_entries(args.inputs)
        entries = rename_duplicate_entries(entries, args.duplicates)
        write_cpp_file(
            entries=entries,
            out_path=args.out,
            title="TI LPC ASM vocabulary converted to Talkie byte order",
            prefix=args.symbol_prefix,
        )
    except Exception as exc:
        parser.exit(1, f"error: {exc}\n")

    print(f"Wrote {len(entries)} entries to {Path(args.out).name}")
    print(f"Input sources: {', '.join(args.inputs)}")
    if args.duplicates == "suffix":
        print("Duplicate labels were preserved with source-derived suffixes")


if __name__ == "__main__":
    main()
