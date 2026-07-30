"""Config file handling for skydimo."""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "skydimo"
CONFIG_PATH = CONFIG_DIR / "config.json"

# Changing any of these needs the engine restarted; everything else is picked
# up live by a running ambilight/music loop.
STRUCTURAL = (
    "mode", "port", "led_count", "color_order", "white_cal", "zones",
    "capture_width", "capture_height", "fps", "music_mode",
)

DEFAULTS: dict = {
    "port": "auto",
    # what `skydimo run` (and the systemd service) does: "ambilight" or "music"
    "mode": "ambilight",
    "led_count": 60,
    "color_order": "rgb",
    # (limit, r, g, b) to enable the adapter's white-channel calibration,
    # or null to leave it off (matches the vendor default).
    "white_cal": None,
    # --- picture shaping -------------------------------------------------
    "brightness": 1.0,      # 0..1 global scale
    "gamma": 1.8,           # perceptual correction; 1.0 disables
    "saturation": 1.25,     # >1 boosts colour
    "smoothing": 0.35,      # EMA factor, 0 = none, 0.9 = very slow
    "min_level": 0,         # floor so dark scenes stay faintly lit
    "fps": 45,
    # --- capture ---------------------------------------------------------
    "capture_width": 160,
    "capture_height": 90,
    "zone_depth": 0.12,     # how far inward from the edge to sample (0..0.5)
    "restore_token": None,  # portal token so permission is asked only once
    # --- geometry --------------------------------------------------------
    # One normalized [x0, y0, x1, y1] sampling rect per LED, in strip order.
    # Written by `skydimo calibrate`.
    "zones": [],
    "layout": None,         # human-readable record of the calibration
    # --- music mode ------------------------------------------------------
    "music_mode": "spectrum",   # "spectrum" | "pulse" | "vu"
    "music_gain": 1.0,
    "music_smoothing": 0.25,
}


def mtime() -> float:
    """Config file mtime, or 0.0 if it does not exist yet."""
    try:
        return CONFIG_PATH.stat().st_mtime
    except OSError:
        return 0.0


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"Could not read {CONFIG_PATH}: {exc}")
    return cfg


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)
