"""Interactive calibration: how many LEDs, and where they sit on the screen."""

from __future__ import annotations

import re
import shutil
import subprocess
import time

from . import config as cfgmod
from . import layout
from .device import Skydimo

PROBE_MAX = 600  # generous upper bound for the binary search


def _ask(prompt: str, options: dict[str, str], default: str) -> str:
    keys = "/".join(options)
    while True:
        print()
        for k, v in options.items():
            print(f"  {k}) {v}")
        try:
            ans = input(f"{prompt} [{keys}] (default {default}): ").strip().lower()
        except EOFError:
            # Piped/non-interactive input ran out; take the default.
            print(f"(no input - using {default})")
            return default
        if not ans:
            return default
        if ans in options:
            return ans
        print(f"  ...please answer with one of: {keys}")


def _yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        try:
            ans = input(f"{prompt} [{d}]: ").strip().lower()
        except EOFError:
            print(f"(no input - using {'yes' if default else 'no'})")
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  ...please answer y or n.")


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def screen_geometry() -> tuple[int, int] | None:
    """Primary display size in pixels, or None if it can't be determined."""
    if not shutil.which("kscreen-doctor"):
        return None
    try:
        out = subprocess.run(
            ["kscreen-doctor", "-o"],
            capture_output=True, text=True, timeout=6, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    plain = _ANSI.sub("", out)
    # "Geometry: 0,0 3440x1440" is unambiguous; the Modes list is not.
    m = re.search(r"Geometry:\s*[-\d]+,[-\d]+\s+(\d+)x(\d+)", plain)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w > 0 and h > 0:
            return w, h
    return None


def _screen_aspect() -> float:
    """Best-effort primary-display aspect ratio, for splitting LEDs per edge."""
    geo = screen_geometry()
    return geo[0] / geo[1] if geo else 16 / 9


def ask_count(dev: Skydimo, preset: int | None = None) -> int:
    """Ask the user how many LEDs the strip has.

    Typing the number is far more reliable than probing: the strip's length is on
    the product listing, and a probe can only ever ask "is the far end lit?",
    which is easy to misjudge on a strip that runs behind a monitor.
    """
    print("\n--- Step 1: how many LEDs? ---")
    if preset:
        print(f"  Using {preset} (from --leds).")
        return preset
    print(
        "  Enter the number of LEDs on your strip. It is usually on the box or\n"
        "  the product page (common counts: 30, 60, 88, 96, 120, 144).\n"
        "  Not sure? Press Enter and I'll help you narrow it down instead."
    )
    while True:
        try:
            raw = input("  LED count: ").strip()
        except EOFError:
            raise SystemExit(
                "no LED count given; pass --leds N for non-interactive use"
            )
        raw = raw.strip()
        if not raw:
            return probe_count(dev)
        if raw.isdigit() and 1 <= int(raw) <= PROBE_MAX:
            return int(raw)
        print(f"  ...enter a whole number from 1 to {PROBE_MAX}, or press Enter.")


def probe_count(dev: Skydimo) -> int:
    """Fallback: narrow the count down by bisection.

    The firmware drives exactly the number of LEDs named in the frame header, so
    lighting N and asking "is the far end lit?" brackets the true length.
    """
    print(
        "\n  I'll light a run of LEDs from the start of the strip. Each time,\n"
        "  say whether the *very last* LED on the strip is lit.\n"
    )
    lo, hi = 1, PROBE_MAX

    dev.set_leds(PROBE_MAX)
    dev.fill(0, 0, 0)
    time.sleep(0.2)

    while lo < hi:
        mid = (lo + hi) // 2
        dev.set_leds(PROBE_MAX)
        px = [(0, 0, 0)] * PROBE_MAX
        for i in range(mid):
            px[i] = (70, 70, 70)
        dev.write(px)
        time.sleep(0.25)
        if _yesno(f"  Lighting {mid} LEDs - is the END of the strip lit?", True):
            hi = mid
        else:
            lo = mid + 1
    return lo


def verify_count(dev: Skydimo, n: int) -> bool:
    dev.set_leds(n)
    dev.fill(0, 0, 0)
    time.sleep(0.2)
    dev.fill(70, 70, 70)
    time.sleep(0.3)
    return _yesno(f"\n  {n} LEDs: is the whole strip lit, end to end?", True)


def find_start(dev: Skydimo, n: int, sides: int) -> str:
    print("\n--- Step 2: where does the strip start? ---")
    marker = max(3, n // 12)
    px = [(0, 0, 0)] * n
    for i in range(marker):
        px[i] = (120, 0, 0)          # red = first LEDs
    for i in range(n - marker, n):
        px[i] = (0, 0, 120)          # blue = last LEDs
    dev.set_leds(n)
    dev.write(px)
    print(
        f"  The first {marker} LEDs are RED and the last {marker} are BLUE.\n"
        "  Looking at the FRONT of your monitor:"
    )
    corners = {
        "1": "top-left",
        "2": "top-right",
        "3": "bottom-right",
        "4": "bottom-left",
    }
    if sides == 3:
        corners = {"3": "bottom-right", "4": "bottom-left"}
        default = "4"
    else:
        default = "4"
    choice = _ask("  Which corner is the RED end nearest?", corners, default)
    return {"1": "tl", "2": "tr", "3": "br", "4": "bl"}[choice]


def find_direction(dev: Skydimo, n: int) -> bool:
    print("\n--- Step 3: which way does it wind? ---")
    marker = max(3, n // 12)
    px = [(0, 0, 0)] * n
    for i in range(marker):
        px[i] = (120, 0, 0)
    for i in range(marker, min(n, marker * 2)):
        px[i] = (0, 120, 0)
    dev.write(px)
    print(
        "  RED is the start, GREEN is just after it.\n"
        "  Trace from red to green around the monitor."
    )
    ans = _ask(
        "  Does the strip run clockwise or counter-clockwise?",
        {"c": "clockwise", "a": "counter-clockwise (anti-clockwise)"},
        "c",
    )
    return ans == "c"


def chase(dev: Skydimo, n: int, delay: float = 0.03) -> None:
    px = [(0, 0, 0)] * n
    dev.set_leds(n)
    for i in range(n):
        px[i] = (140, 140, 0)
        if i:
            px[i - 1] = (0, 0, 0)
        dev.write(px)
        time.sleep(delay)
    dev.fill(0, 0, 0)


def run(cfg: dict, leds: int | None = None) -> None:
    dev = Skydimo(
        port=None if cfg["port"] == "auto" else cfg["port"],
        leds=PROBE_MAX,
        color_order=cfg["color_order"],
        white_cal=cfg.get("white_cal"),
    )
    print(f"Adapter: {dev.port}  id={dev.device_id.decode(errors='replace')}")

    try:
        n = ask_count(dev, preset=leds)
        while leds is None and not verify_count(dev, n):
            raw = input(
                "  Enter the correct LED count "
                "(or press Enter to narrow it down): "
            ).strip()
            if raw.isdigit() and 1 <= int(raw) <= PROBE_MAX:
                n = int(raw)
            else:
                n = probe_count(dev)
        print(f"\n  LED count: {n}")

        sides_choice = _ask(
            "Does the strip cover all four sides of the monitor, "
            "or three (nothing along the bottom)?",
            {"4": "all four sides", "3": "three sides, bottom is bare"},
            "4",
        )
        sides = int(sides_choice)

        start = find_start(dev, n, sides)
        clockwise = find_direction(dev, n) if sides == 4 else (start == "br")

        aspect = _screen_aspect()
        edges = layout.edge_sequence(sides, start, clockwise)
        counts = layout.split_counts(n, edges, aspect)

        print(f"\n  Display aspect ratio: {aspect:.3f}")
        print("  Proposed LEDs per edge (in strip order):")
        for (name, _, _), c in zip(edges, counts):
            print(f"    {name:<7} {c}")
        if not _yesno("  Use these?", True):
            while True:
                raw = input(
                    "  Enter counts in strip order, comma separated "
                    f"({len(edges)} numbers summing to {n}): "
                ).strip()
                try:
                    vals = [int(x) for x in raw.split(",")]
                except ValueError:
                    print("  ...numbers only, please.")
                    continue
                if len(vals) != len(edges):
                    print(f"  ...need exactly {len(edges)} numbers.")
                    continue
                if sum(vals) != n:
                    print(f"  ...they must sum to {n} (got {sum(vals)}).")
                    continue
                counts = vals
                break

        zones = layout.build_zones(
            n,
            sides=sides,
            start=start,
            clockwise=clockwise,
            aspect=aspect,
            depth=float(cfg["zone_depth"]),
            counts=counts,
        )

        geo = screen_geometry()
        if geo:
            cfg["capture_width"] = 160
            cfg["capture_height"] = max(32, int(round(160 * geo[1] / geo[0])))
        cfg["led_count"] = n
        cfg["zones"] = zones
        cfg["layout"] = {
            "sides": sides,
            "start": start,
            "clockwise": clockwise,
            "aspect": round(aspect, 5),
            "counts": counts,
            "description": layout.describe(sides, start, clockwise, counts, edges),
        }
        cfgmod.save(cfg)

        print(f"\nSaved to {cfgmod.CONFIG_PATH}")
        print(f"  {cfg['layout']['description']}")
        if _yesno("\nRun a chase to sanity-check the order?", True):
            chase(dev, n)
        print("\nDone. Try:  skydimo ambilight    or    skydimo music")
    finally:
        dev.close()
