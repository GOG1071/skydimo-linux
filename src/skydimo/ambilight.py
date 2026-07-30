"""Screen -> LED strip. Samples the edges of the captured frame per LED zone."""

from __future__ import annotations

import time

import numpy as np

from . import config as cfgmod
from .device import Skydimo
from .render import Shaper

# `capture` is imported inside run() rather than here: it pulls in GStreamer via
# PyGObject, and the zone-sampling helpers below are pure numpy. Keeping the
# import lazy means they can be used (and tested) without a desktop session.


def zone_slices(zones, width: int, height: int):
    """Precompute pixel index ranges for each normalized zone."""
    out = []
    for x0, y0, x1, y1 in zones:
        px0 = int(np.floor(x0 * width))
        px1 = int(np.ceil(x1 * width))
        py0 = int(np.floor(y0 * height))
        py1 = int(np.ceil(y1 * height))
        px0 = max(0, min(px0, width - 1))
        py0 = max(0, min(py0, height - 1))
        px1 = max(px0 + 1, min(px1, width))
        py1 = max(py0 + 1, min(py1, height))
        out.append((py0, py1, px0, px1))
    return out


def sample(frame: np.ndarray, slices) -> np.ndarray:
    """Average each zone. `frame` is BGRx; returns float (leds, 3) as RGB."""
    out = np.empty((len(slices), 3), dtype=np.float64)
    for i, (y0, y1, x0, x1) in enumerate(slices):
        patch = frame[y0:y1, x0:x1, :3]           # BGR
        mean = patch.reshape(-1, 3).mean(axis=0)
        out[i] = mean[::-1]                        # -> RGB
    return out


def _shaping(cfg: dict) -> dict:
    return {
        "brightness": cfg["brightness"], "gamma": cfg["gamma"],
        "saturation": cfg["saturation"], "smoothing": cfg["smoothing"],
        "min_level": cfg["min_level"],
    }


def run(cfg: dict, stop_check=None) -> str | None:
    """Drive the strip from the screen.

    Returns "restart" when a structural config change (LED count, layout, mode,
    capture size) means the engine has to be rebuilt; None on a clean stop.
    """
    zones = cfg.get("zones") or []
    leds = int(cfg["led_count"])
    if len(zones) != leds:
        raise SystemExit(
            f"Config has {len(zones)} zones but {leds} LEDs. "
            "Run `skydimo calibrate` first."
        )

    from .capture import ScreenCapture

    cap = ScreenCapture(
        width=int(cfg["capture_width"]),
        height=int(cfg["capture_height"]),
        fps=int(cfg["fps"]),
        restore_token=cfg.get("restore_token"),
    )
    cap.start()

    # Persist the portal token so the permission dialog is a one-time event.
    if cap.new_restore_token and cap.new_restore_token != cfg.get("restore_token"):
        cfg["restore_token"] = cap.new_restore_token
        cfgmod.save(cfg)

    shaper = Shaper(
        leds,
        brightness=cfg["brightness"],
        gamma=cfg["gamma"],
        saturation=cfg["saturation"],
        smoothing=cfg["smoothing"],
        min_level=cfg["min_level"],
    )

    dev = Skydimo(
        port=None if cfg["port"] == "auto" else cfg["port"],
        leds=leds,
        color_order=cfg["color_order"],
        white_cal=cfg.get("white_cal"),
    )
    print(f"Ambilight running on {dev.port} ({leds} LEDs). Ctrl-C to stop.")

    slices = None
    period = 1.0 / max(1, int(cfg["fps"]))
    last_mtime = cfgmod.mtime()
    next_cfg_poll = time.monotonic() + 0.5
    result = None
    try:
        while True:
            if stop_check is not None and stop_check():
                break
            t0 = time.monotonic()

            # Pick up config edits without a restart where we can.
            if t0 >= next_cfg_poll:
                next_cfg_poll = t0 + 0.5
                m = cfgmod.mtime()
                if m != last_mtime:
                    last_mtime = m
                    new = cfgmod.load()
                    if any(new.get(k) != cfg.get(k) for k in cfgmod.STRUCTURAL):
                        result = "restart"
                        break
                    shaper.update(**_shaping(new))
                    cfg = new

            frame = cap.read(timeout=1.0)
            if frame is None:
                continue
            if slices is None:
                h, w = frame.shape[:2]
                slices = zone_slices(zones, w, h)
            dev.write(shaper(sample(frame, slices)))
            dt = period - (time.monotonic() - t0)
            if dt > 0:
                time.sleep(dt)
    except KeyboardInterrupt:
        pass
    finally:
        cap.stop()
        dev.close()
    return result
