"""Audio -> LED strip.

Taps the default output's monitor source through PipeWire's PulseAudio layer
(`parec`), so it reacts to whatever the machine is actually playing rather than
to a microphone.

Modes
-----
spectrum  a mirrored frequency spectrum: bass at the strip's centre, treble at
          the ends, hue following the band
vu        a level meter that fills outward from the centre, green -> red
pulse     the whole strip in one colour; brightness follows loudness, hue
          follows the spectral centroid
"""

from __future__ import annotations

import shutil
import subprocess
import time

import numpy as np

from . import config as cfgmod
from .device import Skydimo
from .render import Shaper

RATE = 44100
CHANNELS = 2
CHUNK = 1024  # frames per read -> ~23ms, ~43 updates/sec

# Perceptually spaced band edges (Hz) used by the spectrum/pulse modes.
F_MIN, F_MAX = 40.0, 14000.0


def _monitor_source() -> str:
    """Name of the default sink's monitor source."""
    if shutil.which("pactl"):
        try:
            sink = subprocess.run(
                ["pactl", "get-default-sink"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()
            if sink:
                return f"{sink}.monitor"
        except (subprocess.SubprocessError, OSError):
            pass
    return "@DEFAULT_MONITOR@"


def _open_stream(device: str) -> subprocess.Popen:
    if not shutil.which("parec"):
        raise SystemExit(
            "`parec` not found. Install pulseaudio-utils "
            "(PipeWire's pulse layer provides it)."
        )
    return subprocess.Popen(
        [
            "parec", "--raw",
            f"--device={device}",
            "--format=s16le",
            f"--rate={RATE}",
            f"--channels={CHANNELS}",
            "--latency-msec=30",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _hsv_to_rgb(h: np.ndarray, s: float, v: np.ndarray) -> np.ndarray:
    """Vectorised HSV->RGB. h in 0..1, returns (n, 3) float 0..255."""
    h = np.asarray(h, dtype=np.float64) % 1.0
    v = np.asarray(v, dtype=np.float64)
    i = np.floor(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i.astype(int) % 6
    out = np.empty((h.size, 3), dtype=np.float64)
    for idx, (r, g, b) in enumerate(
        [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)]
    ):
        m = i == idx
        if m.any():
            out[m, 0] = np.broadcast_to(r, h.shape)[m]
            out[m, 1] = np.broadcast_to(g, h.shape)[m]
            out[m, 2] = np.broadcast_to(b, h.shape)[m]
    return out * 255.0


class Analyser:
    def __init__(self, bands: int, gain: float = 1.0, smoothing: float = 0.25):
        self.bands = bands
        self.gain = gain
        self.smoothing = float(np.clip(smoothing, 0.0, 0.95))
        self.window = np.hanning(CHUNK)
        freqs = np.fft.rfftfreq(CHUNK, 1.0 / RATE)
        # Log-spaced band edges -> index ranges into the FFT bins.
        edges = np.geomspace(F_MIN, F_MAX, bands + 1)
        self.slices = []
        for k in range(bands):
            lo = np.searchsorted(freqs, edges[k], side="left")
            hi = max(lo + 1, np.searchsorted(freqs, edges[k + 1], side="right"))
            self.slices.append((lo, min(hi, freqs.size)))
        self._levels = np.zeros(bands)
        self._agc = 1e-3  # running peak for auto-gain

    def __call__(self, pcm: np.ndarray):
        """pcm: mono float array of length CHUNK. Returns (levels, rms)."""
        spec = np.abs(np.fft.rfft(pcm * self.window)) / CHUNK
        raw = np.array([spec[lo:hi].mean() for lo, hi in self.slices])
        # Tilt up the high end, which is naturally much quieter.
        raw *= np.linspace(1.0, 3.5, self.bands)

        peak = raw.max()
        self._agc = max(peak, self._agc * 0.995, 1e-4)
        norm = np.clip(raw / self._agc * self.gain, 0.0, 1.0)
        norm = np.power(norm, 0.6)  # perceptual lift

        a = 1.0 - self.smoothing
        # Fast attack, smoothed release: makes beats snap.
        self._levels = np.where(
            norm > self._levels, norm, self._levels * self.smoothing + norm * a
        )
        rms = float(np.sqrt(np.mean(pcm**2)))
        return self._levels.copy(), rms


def _frame_spectrum(levels: np.ndarray, leds: int) -> np.ndarray:
    """Mirror the bands outward from the strip's centre."""
    half = (leds + 1) // 2
    idx = np.linspace(0, levels.size - 1, half)
    v = np.interp(idx, np.arange(levels.size), levels)
    hue = np.linspace(0.0, 0.72, half)  # red (bass) -> violet (treble)
    right = _hsv_to_rgb(hue, 1.0, v)
    left = right[::-1]
    return np.vstack([left, right])[:leds]


def _frame_vu(rms: float, leds: int, agc: float) -> np.ndarray:
    level = float(np.clip(rms / max(agc, 1e-4), 0.0, 1.0)) ** 0.5
    half = (leds + 1) // 2
    lit = int(round(level * half))
    hue = np.linspace(0.33, 0.0, half)  # green -> red
    v = np.zeros(half)
    v[:lit] = 1.0
    right = _hsv_to_rgb(hue, 1.0, v)
    return np.vstack([right[::-1], right])[:leds]


def _frame_pulse(levels: np.ndarray, leds: int) -> np.ndarray:
    energy = float(levels.mean())
    centroid = float(
        (levels * np.arange(levels.size)).sum() / max(levels.sum(), 1e-6)
    ) / max(levels.size - 1, 1)
    hue = np.full(leds, 0.66 - 0.66 * centroid)  # blue (bass) -> red (treble)
    v = np.full(leds, np.clip(energy * 1.6, 0.0, 1.0))
    return _hsv_to_rgb(hue, 1.0, v)


def run(cfg: dict, mode: str | None = None, stop_check=None) -> str | None:
    """React to playing audio. Returns "restart" on a structural config change."""
    leds = int(cfg["led_count"])
    mode = mode or cfg.get("music_mode", "spectrum")
    if mode not in ("spectrum", "vu", "pulse"):
        raise SystemExit(f"unknown music mode {mode!r}")

    device = _monitor_source()
    proc = _open_stream(device)

    bands = max(8, min(64, leds // 2))
    analyser = Analyser(
        bands,
        gain=float(cfg.get("music_gain", 1.0)),
        smoothing=float(cfg.get("music_smoothing", 0.25)),
    )
    shaper = Shaper(
        leds,
        brightness=cfg["brightness"],
        gamma=cfg["gamma"],
        saturation=1.0,          # the modes already emit saturated colour
        smoothing=0.0,           # the analyser does its own smoothing
        min_level=0,
    )
    dev = Skydimo(
        port=None if cfg["port"] == "auto" else cfg["port"],
        leds=leds,
        color_order=cfg["color_order"],
        white_cal=cfg.get("white_cal"),
    )
    print(
        f"Music mode '{mode}' running on {dev.port} ({leds} LEDs), "
        f"listening to {device}. Ctrl-C to stop."
    )

    nbytes = CHUNK * CHANNELS * 2
    rms_agc = 1e-3
    last_mtime = cfgmod.mtime()
    next_cfg_poll = time.monotonic() + 0.5
    result = None
    try:
        assert proc.stdout is not None
        while True:
            if stop_check is not None and stop_check():
                break
            now = time.monotonic()
            if now >= next_cfg_poll:
                next_cfg_poll = now + 0.5
                m = cfgmod.mtime()
                if m != last_mtime:
                    last_mtime = m
                    new = cfgmod.load()
                    if any(new.get(k) != cfg.get(k) for k in cfgmod.STRUCTURAL):
                        result = "restart"
                        break
                    shaper.update(brightness=new["brightness"], gamma=new["gamma"])
                    analyser.gain = float(new.get("music_gain", 1.0))
                    analyser.smoothing = float(
                        np.clip(new.get("music_smoothing", 0.25), 0.0, 0.95)
                    )
                    cfg = new
            raw = proc.stdout.read(nbytes)
            if not raw or len(raw) < nbytes:
                break
            pcm = (
                np.frombuffer(raw, dtype="<i2")
                .reshape(-1, CHANNELS)
                .mean(axis=1)
                / 32768.0
            )
            levels, rms = analyser(pcm)
            rms_agc = max(rms, rms_agc * 0.995, 1e-4)

            if mode == "spectrum":
                frame = _frame_spectrum(levels, leds)
            elif mode == "vu":
                frame = _frame_vu(rms, leds, rms_agc)
            else:
                frame = _frame_pulse(levels, leds)

            dev.write(shaper(frame))
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        dev.close()
    return result
