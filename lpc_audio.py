#!/usr/bin/env python3
"""Shared PCM audio helpers for LPC speech tools."""

from fractions import Fraction
import wave
from pathlib import Path

import numpy as np


def validate_speed(speed: float, min_speed: float = 0.50, max_speed: float = 2.00) -> float:
    """Validate and return a clock speed/pitch multiplier."""
    if not min_speed <= speed <= max_speed:
        raise ValueError(f"speed must be between {min_speed:g} and {max_speed:g}")
    return speed


def normalize_peak(samples: np.ndarray, peak: float = 0.9) -> np.ndarray:
    """Normalize a float PCM array to the requested peak level."""
    if samples.size == 0:
        return samples.astype(np.float32, copy=False)
    max_sample = float(np.max(np.abs(samples)))
    if max_sample <= 0.0:
        return samples.astype(np.float32, copy=False)
    return (samples * (peak / max_sample)).astype(np.float32)


def resample_clock_speed(samples: np.ndarray, speed: float) -> np.ndarray:
    """
    Resample PCM so fixed-rate playback sounds like a variable speech clock.

    A speed of 1.03 shortens the sample data by about 3%, so a normal 8 kHz WAV
    sounds like the LPC stream was played at 8240 Hz. Pitch and duration change
    together, matching the behavior of RC-clock-adjusted speech hardware.
    """
    validate_speed(speed)
    samples = samples.astype(np.float32, copy=False)
    if samples.size == 0 or speed == 1.0:
        return samples

    out_len = max(1, int(round(samples.size / speed)))
    if out_len == samples.size:
        return samples.copy()

    try:
        return _resample_clock_speed_scipy(samples, speed, out_len)
    except ImportError:
        return _resample_clock_speed_linear(samples, speed, out_len)


def _resample_clock_speed_scipy(samples: np.ndarray, speed: float, out_len: int) -> np.ndarray:
    """Use SciPy's polyphase resampler when the optional dependency is installed."""
    from scipy.signal import resample_poly

    ratio = Fraction(speed).limit_denominator(1000)
    resampled = resample_poly(
        samples,
        up=ratio.denominator,
        down=ratio.numerator,
        window=("kaiser", 8.6),
    ).astype(np.float32)

    if resampled.size > out_len:
        return resampled[:out_len]
    if resampled.size < out_len:
        return np.pad(resampled, (0, out_len - resampled.size)).astype(np.float32)
    return resampled


def _resample_clock_speed_linear(samples: np.ndarray, speed: float, out_len: int) -> np.ndarray:
    """Fallback resampler for environments missing SciPy."""
    positions = np.arange(out_len, dtype=np.float64) * speed
    source_positions = np.arange(samples.size, dtype=np.float64)
    return np.interp(positions, source_positions, samples).astype(np.float32)


def save_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    """Write a float32 array to a 16-bit mono WAV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """Load an uncompressed mono WAV file as float32 PCM plus sample rate."""
    with wave.open(str(path), "rb") as wf:
        if wf.getcomptype() != "NONE":
            raise ValueError(f"{path} is compressed; only PCM WAV files are supported")
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if channels != 1:
        raise ValueError(f"{path} has {channels} channels; expected mono")

    if width == 1:
        raw = np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
        samples = (raw - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"{path} uses {width * 8}-bit samples; expected 8, 16, or 32 bit")

    return samples.astype(np.float32), rate
