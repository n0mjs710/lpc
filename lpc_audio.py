#!/usr/bin/env python3
"""Shared PCM audio helpers for LPC speech tools."""

from fractions import Fraction
import wave
from pathlib import Path

import numpy as np


def validate_clock(clock_hz: float, min_hz: float = 4000, max_hz: float = 16000) -> float:
    """Validate and return a simulated LPC sample clock in Hz."""
    if not min_hz <= clock_hz <= max_hz:
        raise ValueError(f"clock must be between {min_hz:g} and {max_hz:g} Hz")
    return clock_hz


# Analog output stage defaults: off, so the tools give the raw chip output.
# DEFAULT_LPF_POLES applies once a low-pass cutoff is set.
DEFAULT_LPF_HZ = 0
DEFAULT_LPF_POLES = 4
DEFAULT_DC_HZ = 0

# The DAC and analog stage are modeled at this multiple of the chip clock
OVERSAMPLE = 6


def validate_filter(lpf_hz: float, poles: int, dc_hz: float) -> None:
    """Validate output filter settings; a cutoff of 0 disables that stage."""
    if lpf_hz != 0 and not 500 <= lpf_hz <= 10000:
        raise ValueError("lpf must be 0 (off) or between 500 and 10000 Hz")
    if not 1 <= poles <= 8:
        raise ValueError("poles must be between 1 and 8")
    if dc_hz != 0 and not 1 <= dc_hz <= 300:
        raise ValueError("dc must be 0 (off) or between 1 and 300 Hz")


def describe_filter(lpf_hz: float, poles: int, dc_hz: float) -> str:
    """Return a one-line description of the output filter settings."""
    if not lpf_hz and not dc_hz:
        return "off (raw DAC samples)"
    dc = f"DC block {dc_hz:g} Hz" if dc_hz else "DC block off"
    lpf = f"{poles}-pole {lpf_hz:g} Hz low-pass" if lpf_hz else "low-pass off"
    return f"{dc}, {lpf}"


def analog_output(
    samples: np.ndarray,
    clock_hz: float,
    lpf_hz: float = DEFAULT_LPF_HZ,
    poles: int = DEFAULT_LPF_POLES,
    dc_hz: float = DEFAULT_DC_HZ,
) -> tuple[np.ndarray, float]:
    """
    Model the chip's DAC and the analog stage after it; return (audio, rate).

    With both filter stages off, the raw DAC samples are returned unchanged at
    clock_hz. Otherwise the DAC's zero-order hold (one chip clock period per
    sample, with its sin(x)/x treble droop and images above clock_hz/2) is
    built at OVERSAMPLE x clock_hz, then passed through a 1-pole high-pass
    standing in for the output coupling capacitor (DC blocker) and a
    Butterworth low-pass. Cutoffs are in real Hz.
    """
    if not lpf_hz and not dc_hz:
        return samples.astype(np.float32, copy=False), clock_hz

    from scipy.signal import butter, sosfilt

    fs = clock_hz * OVERSAMPLE
    out = np.repeat(samples.astype(np.float64), OVERSAMPLE)
    if dc_hz > 0:
        out = sosfilt(butter(1, dc_hz, "highpass", fs=fs, output="sos"), out)
    if lpf_hz > 0:
        cutoff = min(lpf_hz, 0.45 * fs)
        out = sosfilt(butter(poles, cutoff, "lowpass", fs=fs, output="sos"), out)
    return out.astype(np.float32), fs


def normalize_peak(samples: np.ndarray, peak: float = 0.9) -> np.ndarray:
    """Normalize a float PCM array to the requested peak level."""
    if samples.size == 0:
        return samples.astype(np.float32, copy=False)
    max_sample = float(np.max(np.abs(samples)))
    if max_sample <= 0.0:
        return samples.astype(np.float32, copy=False)
    return (samples * (peak / max_sample)).astype(np.float32)


def resample(samples: np.ndarray, in_hz: float, out_hz: float) -> np.ndarray:
    """
    Resample PCM from in_hz to out_hz.

    Also used to emulate a variable speech clock: samples from a chip running
    at clock_hz, resampled from clock_hz to 8000, play back from an 8 kHz WAV
    with the pitch and duration of that clock, matching the behavior of
    RC-clock-adjusted speech hardware.
    """
    samples = samples.astype(np.float32, copy=False)
    if samples.size == 0 or in_hz == out_hz:
        return samples

    ratio = Fraction(in_hz).limit_denominator(1000) / Fraction(out_hz).limit_denominator(1000)
    out_len = max(1, int(round(samples.size / ratio)))
    if out_len == samples.size:
        return samples.copy()

    try:
        return _resample_scipy(samples, ratio, out_len)
    except ImportError:
        return _resample_linear(samples, float(ratio), out_len)


def _resample_scipy(samples: np.ndarray, ratio: Fraction, out_len: int) -> np.ndarray:
    """Use SciPy's polyphase resampler when the optional dependency is installed."""
    from scipy.signal import resample_poly

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


def _resample_linear(samples: np.ndarray, ratio: float, out_len: int) -> np.ndarray:
    """Fallback resampler for environments missing SciPy."""
    positions = np.arange(out_len, dtype=np.float64) * ratio
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
