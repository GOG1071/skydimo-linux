"""Shared colour shaping: saturation, gamma, brightness, temporal smoothing."""

from __future__ import annotations

import numpy as np


def gamma_lut(gamma: float) -> np.ndarray:
    x = np.arange(256, dtype=np.float64) / 255.0
    return np.clip(np.power(x, gamma) * 255.0, 0, 255).astype(np.uint8)


class Shaper:
    """Turns raw sampled colours into what gets pushed to the strip.

    Smoothing runs on the pre-gamma values so that fading looks linear rather
    than crushing in the dark end.
    """

    def __init__(
        self,
        leds: int,
        brightness: float = 1.0,
        gamma: float = 2.2,
        saturation: float = 1.25,
        smoothing: float = 0.35,
        min_level: int = 0,
    ):
        self.brightness = float(np.clip(brightness, 0.0, 1.0))
        self.saturation = max(0.0, float(saturation))
        self.smoothing = float(np.clip(smoothing, 0.0, 0.99))
        self.min_level = int(np.clip(min_level, 0, 255))
        self._lut = gamma_lut(gamma) if abs(gamma - 1.0) > 1e-6 else None
        self._state = np.zeros((leds, 3), dtype=np.float64)
        self._primed = False

    def update(
        self,
        brightness: float | None = None,
        gamma: float | None = None,
        saturation: float | None = None,
        smoothing: float | None = None,
        min_level: int | None = None,
    ) -> None:
        """Adjust shaping in place, so a running engine can pick up config edits."""
        if brightness is not None:
            self.brightness = float(np.clip(brightness, 0.0, 1.0))
        if saturation is not None:
            self.saturation = max(0.0, float(saturation))
        if smoothing is not None:
            self.smoothing = float(np.clip(smoothing, 0.0, 0.99))
        if min_level is not None:
            self.min_level = int(np.clip(min_level, 0, 255))
        if gamma is not None:
            self._lut = gamma_lut(gamma) if abs(gamma - 1.0) > 1e-6 else None

    def resize(self, leds: int) -> None:
        self._state = np.zeros((leds, 3), dtype=np.float64)
        self._primed = False

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        """rgb: float array (leds, 3) in 0..255. Returns uint8 (leds, 3)."""
        out = np.asarray(rgb, dtype=np.float64)

        if self.saturation != 1.0:
            luma = (out * (0.2126, 0.7152, 0.0722)).sum(axis=1, keepdims=True)
            out = luma + (out - luma) * self.saturation

        out = np.clip(out, 0.0, 255.0)

        if self.smoothing > 0.0:
            if not self._primed:
                self._state[:] = out
                self._primed = True
            else:
                a = 1.0 - self.smoothing
                self._state *= self.smoothing
                self._state += out * a
            out = self._state.copy()

        out = np.clip(out, 0.0, 255.0).astype(np.uint8)
        if self._lut is not None:
            out = self._lut[out]
        if self.brightness < 1.0:
            out = (out.astype(np.float64) * self.brightness).astype(np.uint8)
        if self.min_level:
            out = np.maximum(out, self.min_level)
        return out
