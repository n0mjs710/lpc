#!/usr/bin/env python3
"""
Compare a candidate LPC vocabulary against a reference (master) vocabulary.

Each word is decoded into its TMS5220 frame parameters and compared two ways:
  * by name   - normalized exact match, then fuzzy match
  * by data   - byte-identical, then DTW distance over decoded frame parameters

Every candidate word ends up in one bucket:
  MATCH      same word, same (or nearly the same) data -> no audit needed
  ALTERNATE  name matches (exactly or nearly) but data differs -> audit as an
             alternate reading of a word we already have
  RENAME     data matches a reference word under a very different name -> audit
  NEW        no name or data match -> audit as a new word

Usage:
  python compare_vocab.py sources/cpp/other/Vocab_HP27201A.cpp --ref Vocab_Master.cpp --out-dir audit_hp
"""

from __future__ import annotations

import argparse
import csv
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from lpc_vocab import parse_cpp_file
from tms5220 import ENERGY, K, K_BITS, PERIOD, BitReader


# ─────────────────────────────────────────────────────────────────────────────
# Frame decoding
# ─────────────────────────────────────────────────────────────────────────────

def decode_frames(data: bytes) -> np.ndarray:
    """
    Decode LPC bytes into one feature row per 25 ms frame:
      [log energy, pitch, K1..K10]  (all roughly scaled to -1..1)
    Silence frames keep only energy; unvoiced frames zero K5-K10 and pitch.
    """
    reader = BitReader(data)
    k_idx = [0] * 10
    p_idx = 0
    rows = []
    while True:
        e_idx = 15 if reader.done else reader.read(4)
        if e_idx == 15:
            break
        if e_idx == 0:
            rows.append([0.0] * 12)
            continue
        repeat = reader.read(1)
        p_idx = reader.read(6)
        if not repeat:
            for i in range(4):
                k_idx[i] = reader.read(K_BITS[i])
            if p_idx != 0:
                for i in range(4, 10):
                    k_idx[i] = reader.read(K_BITS[i])
        ks = [K[i][k_idx[i]] / 512.0 if (i < 4 or p_idx) else 0.0 for i in range(10)]
        rows.append([np.log2(1 + ENERGY[e_idx]) / 7.0, PERIOD[p_idx] / 160.0, *ks])
    return np.array(rows, dtype=np.float64).reshape(-1, 12)


# Weights: energy and pitch matter perceptually; low K's carry most of the formants.
WEIGHTS = np.array([1.5, 1.0, 1.5, 1.5, 1.0, 1.0] + [0.5] * 6)


def dtw_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Length-normalized DTW over weighted frame vectors."""
    if len(a) == 0 or len(b) == 0:
        return float("inf")
    wa, wb = a * WEIGHTS, b * WEIGHTS
    cost = np.sqrt(((wa[:, None, :] - wb[None, :, :]) ** 2).sum(-1))
    n, m = cost.shape
    acc = np.full((n + 1, m + 1), np.inf)
    acc[0, 0] = 0.0
    for i in range(1, n + 1):
        row = acc[i]
        prev = acc[i - 1]
        # best of diagonal / vertical from previous row; horizontal is sequential
        best = np.minimum(prev[:-1], prev[1:]) + cost[i - 1]
        for j in range(1, m + 1):
            row[j] = min(best[j - 1], row[j - 1] + cost[i - 1, j - 1])
    return float(acc[n, m] / (n + m))


def signature(frames: np.ndarray, steps: int = 12) -> np.ndarray:
    """Fixed-length resampled descriptor used to shortlist DTW candidates."""
    if len(frames) == 0:
        return np.zeros(steps * 12 + 1)
    idx = np.linspace(0, len(frames) - 1, steps)
    resampled = np.array([np.interp(idx, np.arange(len(frames)), frames[:, c]) for c in range(12)]).T
    return np.append((resampled * WEIGHTS).ravel(), len(frames) / 20.0)


# ─────────────────────────────────────────────────────────────────────────────
# Names
# ─────────────────────────────────────────────────────────────────────────────

DIGIT_WORDS = dict(zip("0123456789", "zero one two three four five six seven eight nine".split()))


def base_name(name: str) -> str:
    """Normalize a word name: lowercase alnum only, drop a trailing variant digit."""
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    if n in DIGIT_WORDS:
        return DIGIT_WORDS[n]
    stripped = re.sub(r"\d+$", "", n)
    return stripped or n


def is_female(name: str) -> bool:
    return name.upper().startswith("F_")


def name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, base_name(a), base_name(b)).ratio()


# ─────────────────────────────────────────────────────────────────────────────
# Comparison
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Word:
    name: str
    data: bytes
    frames: np.ndarray
    sig: np.ndarray


def load_words(path: str) -> list[Word]:
    words = []
    for e in parse_cpp_file(path):
        f = decode_frames(e.data)
        words.append(Word(e.name, e.data, f, signature(f)))
    return words


def best_data_matches(word: Word, ref: list[Word], ref_sigs: np.ndarray,
                      forced: list[int], shortlist: int = 25) -> list[tuple[float, int]]:
    """DTW distance to the shortlisted reference words (plus any forced indices)."""
    d = np.linalg.norm(ref_sigs - word.sig, axis=1)
    cand = set(np.argsort(d)[:shortlist].tolist()) | set(forced)
    out = []
    for i in cand:
        if ref[i].data == word.data:
            out.append((0.0, i))
        else:
            out.append((dtw_distance(word.frames, ref[i].frames), i))
    return sorted(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidate", help="candidate vocabulary .cpp")
    ap.add_argument("--ref", default="Vocab_Master.cpp", help="reference vocabulary .cpp")
    ap.add_argument("--out-dir", default="audit", help="directory for report files")
    ap.add_argument("--data-threshold", type=float, default=0.06,
                    help="DTW distance at or below which data counts as the same (default 0.06)")
    ap.add_argument("--name-threshold", type=float, default=0.85,
                    help="name similarity at or above which a name counts as almost the same")
    ap.add_argument("--render", action="store_true",
                    help="write audit WAVs: candidate word, pause, then closest master word")
    ap.add_argument("--stats", action="store_true", help="print distance statistics and exit")
    args = ap.parse_args()

    cand = load_words(args.candidate)
    ref = load_words(args.ref)
    ref_sigs = np.array([w.sig for w in ref])
    ref_base: dict[str, list[int]] = {}
    for i, w in enumerate(ref):
        ref_base.setdefault(base_name(w.name), []).append(i)

    rows = []
    for w in cand:
        exact_idx = ref_base.get(base_name(w.name), [])
        near_idx = [i for i, r in enumerate(ref)
                    if base_name(r.name) != base_name(w.name)
                    and name_similarity(w.name, r.name) >= args.name_threshold]
        data = best_data_matches(w, ref, ref_sigs, exact_idx + near_idx)
        dist = dict((i, d) for d, i in data)

        def best(idxs):
            return min(((dist[i], i) for i in idxs), default=(float("inf"), -1))

        n_d, n_i = best(exact_idx)         # best data match among exact-name refs
        a_d, a_i = best(near_idx)          # ... among near-name refs
        g_d, g_i = data[0]                 # best data match overall

        if n_d <= args.data_threshold:
            bucket, why, other = "MATCH", "name+data match", n_i
        elif a_d <= args.data_threshold and not exact_idx:
            bucket, why, other = "AUDIT-ALTERNATE", "near name, data matches", a_i
        elif exact_idx:
            bucket, why, other = "AUDIT-ALTERNATE", "same name, data differs", n_i
        elif near_idx:
            bucket, why, other = "AUDIT-ALTERNATE", "near name, data differs", a_i
        elif g_d <= args.data_threshold:
            bucket, why, other = "AUDIT-RENAME", "data matches under different name", g_i
        else:
            bucket, why, other = "AUDIT-NEW", "no name or data match", g_i
        rows.append({
            "name": w.name, "bucket": bucket, "reason": why,
            "ref_name": ref[other].name if other >= 0 else "",
            "ref_dist": round(dist.get(other, float("inf")), 4) if other >= 0 else "",
            "closest_data_name": ref[g_i].name, "closest_data_dist": round(g_d, 4),
            "frames": len(w.frames), "ref_frames": len(ref[other].frames) if other >= 0 else "",
        })

    if args.stats:
        import collections
        print(collections.Counter(r["bucket"] for r in rows))
        return

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0])
    by_name = {w.name: w for w in cand}
    ref_by_name = {w.name: w for w in ref}

    def write_csv(path: Path, subset: list[dict]) -> None:
        with path.open("w", newline="") as fh:
            wr = csv.DictWriter(fh, cols)
            wr.writeheader()
            wr.writerows(subset)

    write_csv(out / "comparison_all.csv", rows)
    groups = {
        "MATCH": "matched_no_audit.csv",
        "AUDIT-ALTERNATE": "audit_alternates.csv",
        "AUDIT-RENAME": "audit_rename.csv",
        "AUDIT-NEW": "audit_new_words.csv",
    }
    for bucket, fname in groups.items():
        subset = [r for r in rows if r["bucket"] == bucket]
        write_csv(out / fname, subset)
        print(f"{bucket:16s} {len(subset):5d}  -> {out / fname}")

    if args.render:
        from lpc_audio import analog_output, normalize_peak, save_wav
        from tms5220 import synthesize

        def audio(data: bytes):
            a, rate = analog_output(synthesize(data), 8000, 5000, 2, 30)
            return normalize_peak(a), rate

        for r in rows:
            if r["bucket"] == "MATCH":
                continue
            d = out / "audio" / r["bucket"].lower()
            d.mkdir(parents=True, exist_ok=True)
            a, rate = audio(by_name[r["name"]].data)
            parts = [a]
            if r["ref_name"]:
                b, _ = audio(ref_by_name[r["ref_name"]].data)
                parts += [np.zeros(int(rate * 0.6), dtype=a.dtype), b]
            save_wav(d / f"{r['name']}__vs__{r['ref_name'] or 'none'}.wav",
                     np.concatenate(parts), int(round(rate)))
        print(f"Audio under {out / 'audio'} (candidate, pause, closest master word)")


if __name__ == "__main__":
    main()
