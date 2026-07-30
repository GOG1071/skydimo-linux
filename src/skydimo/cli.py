"""skydimo command line."""

from __future__ import annotations

import argparse
import signal
import sys
import time

from . import config as cfgmod

COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "white": (255, 255, 255), "warm": (255, 170, 90), "cool": (200, 220, 255),
    "yellow": (255, 220, 0), "orange": (255, 110, 0), "purple": (160, 0, 255),
    "pink": (255, 60, 140), "cyan": (0, 220, 255), "off": (0, 0, 0),
}


def _parse_color(text: str) -> tuple[int, int, int]:
    t = text.strip().lower()
    if t in COLORS:
        return COLORS[t]
    h = t.lstrip("#")
    if len(h) == 6:
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            pass
    if "," in t:
        parts = [p.strip() for p in t.split(",")]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            v = [max(0, min(255, int(p))) for p in parts]
            return (v[0], v[1], v[2])
    raise SystemExit(
        f"Cannot read colour {text!r}. Use a name ({', '.join(sorted(COLORS))}), "
        "#rrggbb, or r,g,b."
    )


def _device(cfg: dict, leds: int | None = None):
    from .device import Skydimo
    return Skydimo(
        port=None if cfg["port"] == "auto" else cfg["port"],
        leds=leds or int(cfg["led_count"]),
        color_order=cfg["color_order"],
        white_cal=cfg.get("white_cal"),
    )


class _PortHandover:
    """Pause the engine so an interactive command can hold the serial port.

    Only for the guided flows (calibrate, test) where taking the strip over is
    obviously what the user wants. `color`/`off` deliberately error out instead,
    since a running engine would just overwrite them on the next frame.
    """

    def __init__(self):
        self.was_active = False

    def __enter__(self):
        from . import service
        self.was_active = service.is_active()
        if self.was_active:
            print("Pausing the engine to take the strip...")
            service.stop()
            time.sleep(0.8)
        return self

    def __exit__(self, *exc):
        from . import service
        if self.was_active:
            print("Resuming the engine.")
            service.start()
        return False


def cmd_info(args, cfg):
    from . import service
    from .device import BAUD, find_port, Skydimo

    try:
        port = find_port() if cfg["port"] == "auto" else cfg["port"]
    except FileNotFoundError as exc:
        port = "not found"
        print(f"port        {port}  ({exc})")
    else:
        print(f"port        {port}")

    # Reading the device id needs the port, which the engine may be holding.
    # That is not a reason to withhold everything else.
    dev_id = "-"
    if port != "not found":
        try:
            dev = Skydimo(port=port, leds=int(cfg["led_count"]),
                          color_order=cfg["color_order"],
                          white_cal=cfg.get("white_cal"), attempts=1)
        except Exception as exc:
            dev_id = ("in use by the engine"
                      if "lock" in str(exc).lower() else f"unavailable ({exc})")
        else:
            dev_id = dev.device_id.decode(errors="replace")
            dev.close(blank=False)

    lay = cfg.get("layout")
    print(f"device id   {dev_id}")
    print(f"baud        {BAUD}")
    print(f"led count   {cfg['led_count']}")
    print(f"colour ord  {cfg['color_order']}")
    print(f"zones       {len(cfg.get('zones') or [])}")
    print(f"layout      {lay['description'] if lay else 'not calibrated'}")
    print(f"mode        {cfg.get('mode', 'ambilight')}")
    print(f"config      {cfgmod.CONFIG_PATH}")
    print(f"service     {service.status_line()}")


def cmd_test(args, cfg):
    with _PortHandover():
        _do_test(args, cfg)


def _do_test(args, cfg):
    dev = _device(cfg, leds=args.leds or int(cfg["led_count"]))
    try:
        print(f"Adapter {dev.port}, id={dev.device_id.decode(errors='replace')}, "
              f"{dev.leds} LEDs")
        for name, rgb in [("red", (120, 0, 0)), ("green", (0, 120, 0)),
                          ("blue", (0, 0, 120)), ("white", (90, 90, 90))]:
            print(f"  {name}")
            dev.fill(*rgb)
            time.sleep(args.dwell)
        print("  chase")
        px = [(0, 0, 0)] * dev.leds
        for i in range(dev.leds):
            px[i] = (150, 150, 0)
            if i:
                px[i - 1] = (0, 0, 0)
            dev.write(px)
            time.sleep(0.02)
        print("Test complete.")
    finally:
        dev.close()


def cmd_color(args, cfg):
    r, g, b = _parse_color(args.color)
    if args.brightness is not None:
        s = max(0.0, min(1.0, args.brightness))
        r, g, b = int(r * s), int(g * s), int(b * s)
    dev = _device(cfg)
    try:
        dev.fill(r, g, b)
        print(f"Set {dev.leds} LEDs to ({r}, {g}, {b}).")
        if args.hold:
            print("Holding (Ctrl-C to release)...")
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
    finally:
        # Leaving the colour set is the point, so don't blank on the way out
        # unless we were holding the strip live.
        dev.close(blank=args.hold)


def cmd_off(args, cfg):
    dev = _device(cfg)
    try:
        dev.fill(0, 0, 0)
        print("LEDs off.")
    finally:
        dev.close(blank=False)


def cmd_calibrate(args, cfg):
    from . import calibrate
    with _PortHandover():
        calibrate.run(cfg, leds=args.leds)


def cmd_run(args, cfg):
    """What the systemd service runs: whichever mode the config asks for."""
    from . import ambilight, music
    while True:
        cfg = cfgmod.load()
        mode = args.mode or cfg.get("mode", "ambilight")
        if mode == "music":
            result = music.run(cfg)
        elif mode == "ambilight":
            result = ambilight.run(cfg)
        else:
            raise SystemExit(f"unknown mode {mode!r} in config")
        if result != "restart":
            return
        print("config changed - restarting engine", flush=True)
        time.sleep(0.5)


def cmd_gui(args, cfg):
    from . import gui
    return gui.main([])


def cmd_ambilight(args, cfg):
    from . import ambilight
    if args.fps:
        cfg["fps"] = args.fps
    if args.brightness is not None:
        cfg["brightness"] = args.brightness
    ambilight.run(cfg)


def cmd_music(args, cfg):
    from . import music
    if args.brightness is not None:
        cfg["brightness"] = args.brightness
    if args.gain is not None:
        cfg["music_gain"] = args.gain
    music.run(cfg, mode=args.mode)


def cmd_set(args, cfg):
    import json
    if not args.assignments:
        for k in sorted(cfg):
            if k not in ("zones",):
                print(f"{k} = {json.dumps(cfg[k])}")
        return
    for item in args.assignments:
        if "=" not in item:
            raise SystemExit(f"expected key=value, got {item!r}")
        k, v = item.split("=", 1)
        k = k.strip()
        if k not in cfgmod.DEFAULTS:
            raise SystemExit(
                f"unknown setting {k!r}. Known: {', '.join(sorted(cfgmod.DEFAULTS))}"
            )
        try:
            val = json.loads(v)
        except json.JSONDecodeError:
            val = v.strip()
        cfg[k] = val
        print(f"{k} = {json.dumps(val)}")
    cfgmod.save(cfg)
    print(f"Saved {cfgmod.CONFIG_PATH}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skydimo",
        description="Drive a Skydimo 'Rainbow Music LED' USB adapter on Linux.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="show adapter and config state")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("test", help="colour sweep + chase to confirm wiring")
    s.add_argument("--leds", type=int, help="override LED count for this run")
    s.add_argument("--dwell", type=float, default=1.5, help="seconds per colour")
    s.set_defaults(func=cmd_test)

    s = sub.add_parser("color", aliases=["colour"], help="set a static colour")
    s.add_argument("color", help="name, #rrggbb, or r,g,b")
    s.add_argument("--brightness", type=float, help="0..1 scale")
    s.add_argument("--hold", action="store_true",
                   help="keep the process alive holding the colour")
    s.set_defaults(func=cmd_color)

    s = sub.add_parser("off", help="blank the strip")
    s.set_defaults(func=cmd_off)

    s = sub.add_parser("calibrate", help="interactive LED count + layout wizard")
    s.add_argument("--leds", type=int,
                   help="LED count, to skip the question")
    s.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("run", help="run the mode named in the config (used by the service)")
    s.add_argument("--mode", choices=["ambilight", "music"],
                   help="override the configured mode")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("gui", help="open the configuration window")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("ambilight", help="mirror the screen edges onto the strip")
    s.add_argument("--fps", type=int)
    s.add_argument("--brightness", type=float)
    s.set_defaults(func=cmd_ambilight)

    s = sub.add_parser("music", help="react to whatever is playing")
    s.add_argument("--mode", choices=["spectrum", "vu", "pulse"])
    s.add_argument("--brightness", type=float)
    s.add_argument("--gain", type=float)
    s.set_defaults(func=cmd_music)

    s = sub.add_parser("set", help="read or write config values")
    s.add_argument("assignments", nargs="*", metavar="key=value")
    s.set_defaults(func=cmd_set)

    return p


def _install_term_handler() -> None:
    """Turn SIGTERM into KeyboardInterrupt.

    systemd stops us with SIGTERM, whose default action kills the process
    outright -- no finally blocks, so the strip would keep its last frame and
    the serial port would only be freed by the kernel. Raising instead lets the
    engines shut down properly.
    """
    def handler(_sig, _frm):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        pass  # not the main thread; nothing to do


def main(argv=None) -> int:
    _install_term_handler()
    args = build_parser().parse_args(argv)
    cfg = cfgmod.load()
    try:
        args.func(args, cfg)
    except KeyboardInterrupt:
        print()
        return 130
    except (SystemExit,) as exc:
        if isinstance(exc.code, str):
            print(f"error: {exc.code}", file=sys.stderr)
            return 1
        raise
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
