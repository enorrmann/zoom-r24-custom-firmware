"""WAV decoding / fitting helpers — the audio plumbing behind `r24tool paste`.

This is the small part of the old `r24pcm` module (removed) that is still
needed: turn any readable WAV into bare 16-bit little-endian mono PCM at a
chosen rate, and fit a sample stream into an exact number of samples.

It deliberately knows NOTHING about the firmware layout. Locating audio inside
the image, and deciding which sample a given byte belongs to, is
`patch_samples.py`'s job — that tool is bounds-checked per sample and is what
you want for custom drum samples. `r24tool paste` uses this module only to
convert the WAV, and writes wherever --offset says.
"""
from __future__ import annotations

import array
import sys
import wave

SAMPLE_WIDTH = 2          # bytes per mono sample (16-bit)
NATIVE_RATE = 44100       # the R24's primary project rate


class PcmError(Exception):
    """Raised when a WAV cannot be read or converted."""


def read_wav(path: str) -> tuple[array.array, int, int]:
    """Return (mono float samples in -1..1, source rate, source channels).

    Accepts any PCM/float WAV that the stdlib can open, plus 24-bit by hand.
    """
    try:
        with wave.open(path, "rb") as w:
            nch = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise PcmError(f"{path}: not a readable WAV ({exc})") from exc
    except OSError as exc:
        raise PcmError(f"{path}: {exc}") from exc

    if nch < 1:
        raise PcmError(f"{path}: no channels")
    if width == 3:
        # 24-bit little-endian signed, not covered by the wave module's writers
        vals = []
        for i in range(0, len(raw) - 2, 3):
            b0, b1, b2 = raw[i], raw[i + 1], raw[i + 2]
            v = b0 | (b1 << 8) | (b2 << 16)
            if v & 0x800000:
                v -= 0x1000000
            vals.append(v / 8388608.0)
    elif width in (1, 2, 4):
        a = array.array("h")
        if width == 2:
            a.frombytes(raw[: len(raw) - len(raw) % 2])
            if sys.byteorder != "little":
                a.byteswap()
            vals = [x / 32768.0 for x in a]
        elif width == 1:
            vals = [(b - 128) / 128.0 for b in raw]
        else:  # 32-bit: assume signed PCM, take the top 16 bits
            a = array.array("i")
            a.frombytes(raw[: len(raw) - len(raw) % 4])
            if sys.byteorder != "little":
                a.byteswap()
            vals = [x / 2147483648.0 for x in a]
    else:
        raise PcmError(f"{path}: unsupported sample width {width * 8} bit")

    # fold to mono
    if nch > 1:
        vals = [sum(vals[i:i + nch]) / nch
                for i in range(0, len(vals) - nch + 1, nch)]
    return array.array("d", vals), rate, nch


def resample(vals: array.array, src_rate: int, dst_rate: int) -> array.array:
    """Linear-interpolating resampler. Adequate for percussive one-shots."""
    if src_rate == dst_rate or len(vals) == 0:
        return vals
    ratio = dst_rate / float(src_rate)
    n_out = max(1, int(round(len(vals) * ratio)))
    out = array.array("d", bytes(8 * n_out))
    last = len(vals) - 1
    for i in range(n_out):
        pos = i / ratio
        j = int(pos)
        if j >= last:
            out[i] = vals[last]
        else:
            frac = pos - j
            out[i] = vals[j] * (1.0 - frac) + vals[j + 1] * frac
    return out


def to_pcm16(vals: array.array, target_samples: int,
             loop_smooth: bool = True) -> tuple[bytes, dict]:
    """Fit `vals` into exactly `target_samples` 16-bit samples.

    Truncates with a short fade so a cut does not click, then zero-pads. The
    pad is silence, which is exactly what the original bank ends with.

    The float<->int scaling is an exact pair with `read_wav` (divide by 32768 on
    read, multiply by 32768 on write, clipped to the int16 range), so
    extract-then-reimport is bit-exact for material that already fits the slot.
    """
    info = {"truncated": 0, "padded": 0, "faded": 0}
    if loop_smooth and len(vals) > target_samples:
        info["truncated"] = len(vals) - target_samples
        vals = vals[:target_samples]
        fade = min(64, len(vals))
        for i in range(fade):
            vals[len(vals) - fade + i] *= (fade - i) / float(fade)
        info["faded"] = fade

    out = array.array("h", bytes(SAMPLE_WIDTH * target_samples))
    for i, v in enumerate(vals):
        n = int(round(v * 32768.0))
        if n > 32767:
            n = 32767
        elif n < -32768:
            n = -32768
        out[i] = n
    if sys.byteorder != "little":
        out.byteswap()
    info["padded"] = target_samples - len(vals)
    return out.tobytes(), info


def encode_wav_raw(path: str, rate: int = NATIVE_RATE) -> tuple[bytes, dict]:
    """Convert a WAV to bare 16-bit LE mono PCM at `rate`. No target length.

    This is the "no questions asked" path: fold to mono, resample if needed,
    quantise to 16-bit little-endian, and hand back the bytes with no header.
    Nothing is trimmed, padded, or validated — the caller decides where it
    goes and what to do if it is too big.
    """
    vals, src_rate, nch = read_wav(path)
    info = {"source_rate": src_rate, "source_channels": nch,
            "source_samples": len(vals)}
    vals = resample(vals, src_rate, rate)
    info["resampled_samples"] = len(vals)
    # fit to exactly len(vals) samples: no truncation, no padding
    blob, _ = to_pcm16(vals, len(vals), loop_smooth=False)
    info["bytes"] = len(blob)
    info["seconds"] = len(vals) / float(rate)
    return blob, info
