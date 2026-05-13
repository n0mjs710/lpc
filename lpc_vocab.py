#!/usr/bin/env python3
"""Vocabulary parsing and conversion helpers for TI LPC speech data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


CPP_ARRAY_RE = re.compile(
    r'(?:extern\s+)?const\s+uint8_t\s+(\w+)\s*\[\]\s*(?:PROGMEM\s*)?=\s*\{([^}]+)\}',
    re.DOTALL,
)
PREFIX_RE = re.compile(r'^sp[a-z0-9]*_', re.IGNORECASE)
BYTE_RE = re.compile(r'\$([0-9A-Fa-f]{1,2})|0[xX]([0-9A-Fa-f]{1,2})|\b([0-9]{1,3})\b')
ASM_LABEL_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\b')


@dataclass(frozen=True)
class VocabEntry:
    name: str
    data: bytes
    source: str = ""
    raw_name: str = ""


def reverse_byte(value: int) -> int:
    """Reverse the bit order of one byte."""
    value = ((value >> 4) & 0x0F) | ((value << 4) & 0xF0)
    value = ((value >> 2) & 0x33) | ((value << 2) & 0xCC)
    value = ((value >> 1) & 0x55) | ((value << 1) & 0xAA)
    return value


def clean_vocab_name(symbol: str) -> str:
    """Normalize a C symbol to the word name used by the Python tools."""
    return PREFIX_RE.sub("", symbol).upper()


def strip_c_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def parse_byte_values(text: str) -> bytes | None:
    """Parse byte values written as $FF, 0xFF, or decimal."""
    text = strip_c_comments(text)
    values = []
    for match in BYTE_RE.finditer(text):
        token = next(group for group in match.groups() if group is not None)
        base = 10 if match.group(3) is not None else 16
        value = int(token, base)
        if not 0 <= value <= 255:
            raise ValueError(f"byte value out of range: {value}")
        values.append(value)
    if not values:
        return None
    return bytes(values)


def parse_cpp_file(
    path: str | Path,
    skip_symbol_prefixes: tuple[str, ...] = (),
) -> list[VocabEntry]:
    """Parse a Talkie-compatible .cpp vocabulary file."""
    path = Path(path)
    text = path.read_text(errors="replace")
    entries: list[VocabEntry] = []

    for match in CPP_ARRAY_RE.finditer(text):
        raw_name = match.group(1)
        if any(raw_name.lower().startswith(prefix.lower()) for prefix in skip_symbol_prefixes):
            continue

        data = parse_byte_values(match.group(2))
        if data:
            entries.append(
                VocabEntry(
                    name=clean_vocab_name(raw_name),
                    data=data,
                    source=path.name,
                    raw_name=raw_name,
                )
            )

    return entries


def load_cpp_file(path: str | Path, target: dict[str, bytes]) -> int:
    """Load one .cpp vocabulary file into target and return entry count."""
    path = Path(path)
    if path.suffix.lower() != ".cpp":
        raise ValueError(f"expected a .cpp vocabulary file: {path}")

    loaded = 0
    for entry in parse_cpp_file(path):
        target[entry.name] = entry.data
        loaded += 1
    return loaded


def load_cpp_sources(paths: list[str]) -> tuple[dict[str, bytes], int]:
    """Load one or more .cpp files into a new vocabulary dict."""
    target: dict[str, bytes] = {}
    loaded = 0
    for path in paths:
        loaded += load_cpp_file(path, target)
    return target, loaded


def parse_asm_file(path: str | Path) -> list[VocabEntry]:
    """
    Parse a TI-style ASM file with LABEL / FCB / LABELe blocks.

    The ASM files in this project are marked as bit reversed, so FCB bytes are
    reversed while loading to convert them into Talkie-compatible byte order.
    Voice prefixes such as W are preserved in the entry name.
    """
    path = Path(path)
    entries: list[VocabEntry] = []
    current_name: str | None = None
    current_data: list[int] = []

    def finish_current() -> None:
        nonlocal current_name, current_data
        if current_name and current_data:
            entries.append(
                VocabEntry(
                    name=clean_vocab_name(current_name),
                    data=bytes(current_data),
                    source=path.name,
                    raw_name=current_name,
                )
            )
        current_name = None
        current_data = []

    for raw_line in path.read_text(errors="replace").splitlines():
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            continue

        if "FCB" not in line.upper():
            label_match = ASM_LABEL_RE.match(line)
            if label_match:
                label = label_match.group(1)
                if current_name and label.upper() == f"{current_name}E".upper():
                    finish_current()
                elif not current_name:
                    current_name = label
            continue

        before, after = re.split(r"\bFCB\b", line, maxsplit=1, flags=re.IGNORECASE)
        label_match = ASM_LABEL_RE.match(before)
        if label_match:
            if current_name and current_data:
                finish_current()
            current_name = label_match.group(1)
        elif not current_name:
            raise ValueError(f"FCB data without a label in {path}: {raw_line!r}")

        data = parse_byte_values(after)
        if data:
            current_data.extend(reverse_byte(byte) for byte in data)

    finish_current()
    return entries


def collect_asm_entries(paths: list[str]) -> list[VocabEntry]:
    """Parse one or more ASM files or directories, returning entries in path order."""
    entries: list[VocabEntry] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            asm_files = sorted(path.glob("*.asm"))
        else:
            asm_files = [path]

        for asm_file in asm_files:
            if asm_file.suffix.lower() != ".asm":
                raise ValueError(f"expected an .asm file: {asm_file}")
            entries.extend(parse_asm_file(asm_file))
    return entries


def c_identifier_fragment(name: str) -> str:
    """Return a C-identifier-safe fragment that preserves lookup meaning."""
    fragment = re.sub(r"[^A-Za-z0-9_]", "_", name.upper())
    return fragment or "UNKNOWN"


def c_symbol_for_name(name: str, prefix: str = "sp_") -> str:
    """
    Return a valid C symbol for a vocabulary name.

    The prefix is part of the C symbol only; parse_cpp_file strips sp*-style
    prefixes back off so names like sp_11 still load as 11.
    """
    fragment = c_identifier_fragment(name)
    symbol = f"{prefix}{fragment}"
    if not re.match(r"[A-Za-z_]", symbol):
        symbol = f"sp_{symbol}"
    return symbol


def unique_name(base: str, used: set[str]) -> str:
    """Return base or base_N if needed to avoid a duplicate name."""
    if base not in used:
        used.add(base)
        return base

    n = 2
    while f"{base}_{n}" in used:
        n += 1
    name = f"{base}_{n}"
    used.add(name)
    return name


def source_suffix(source: str) -> str:
    """Return a C/name-safe suffix derived from a source path."""
    suffix = re.sub(r"[^A-Za-z0-9_]", "_", Path(source).stem.upper())
    return suffix or "SOURCE"


def format_byte_lines(data: bytes, indent: str = "    ", columns: int = 16) -> str:
    """Format bytes for a C initializer."""
    lines = []
    for start in range(0, len(data), columns):
        chunk = data[start:start + columns]
        lines.append(indent + ",".join(f"0x{byte:02X}" for byte in chunk))
    return ",\n".join(lines)


def write_cpp_file(
    entries: list[VocabEntry],
    out_path: str | Path,
    title: str,
    prefix: str = "sp_",
) -> None:
    """Write entries as a Talkie-compatible .cpp vocabulary file."""
    out_path = Path(out_path)
    used_symbols: set[str] = set()

    with out_path.open("w") as output:
        output.write(f"// {title}\n")
        output.write(f"// Entries: {len(entries)}\n\n")
        output.write("#include <stdint.h>\n\n")

        for entry in entries:
            symbol = unique_name(c_symbol_for_name(entry.name, prefix), used_symbols)
            if entry.source:
                output.write(f"// {entry.source} -> {entry.name}\n")
            else:
                output.write(f"// {entry.name}\n")
            output.write(f"const uint8_t {symbol}[] = {{\n")
            output.write(format_byte_lines(entry.data))
            output.write("\n};\n\n")
