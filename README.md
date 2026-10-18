# skydimo-linux

Linux driver, ambilight and music-reactive engine for the **Skydimo "Rainbow
Music LED"** USB LED adapter (`SKYDIMO'ND RAINBOW MUSIC LED`).

The vendor ships Windows software only, and the adapter speaks a protocol that
stock [Hyperion](https://github.com/hyperion-project/hyperion.ng) and
[HyperHDR](https://github.com/awawa-dev/HyperHDR) do not support — its Windows
app is a rebranded HyperHDR fork carrying a private LED driver
(`DriverSerialSkydimondUsb`). This project reimplements that protocol, so the
strip works on Linux.

Tested on Bazzite 44 (Fedora/KDE Plasma 6, Wayland) with a 3440×1440 display.
The design is distribution-agnostic; screen capture uses the standard
`xdg-desktop-portal` ScreenCast API, so it works on any Wayland compositor that
implements it, and on X11.

## What you get

- **Ambilight** — screen edges mirrored onto the strip, ~45 fps
- **Music-reactive** — spectrum, VU meter, or pulse, following whatever is playing
- **A GTK4/libadwaita GUI** for every setting, with hands-on wiring checks
- **A systemd user service** that follows your config and starts at login
- **A CLI** for static colours, tests, calibration and scripting

## Install

```bash
git clone https://github.com/GOG1071/skydimo-linux.git
cd skydimo-linux
./install.sh
```

The installer works out what your distribution calls the handful of system
packages it needs, offers to install them, then sets up an isolated virtualenv
under `~/.local/share/skydimo`. Nothing is installed as root unless you ask for
the optional udev rule.

```bash
./install.sh --check       # just report what is missing
./install.sh --udev        # also install the ModemManager udev rule (sudo)
./install.sh --no-deps     # skip the system-package step
./install.sh --uninstall   # remove it again (your config is kept)
```

Then set the LED count — the one setting that has to be right:

```bash
skydimo gui                    # or launch "Skydimo" from your app menu
# ...or headless:
skydimo calibrate --leds 96
systemctl --user enable --now skydimo
```

### Requirements

Python 3.10+, and these from your distribution (the installer names them for
you): PyGObject with the **Gst**, **GstApp**, **Gtk 4.0** and **Adw 1**
typelibs, GStreamer base plugins, the GStreamer **PipeWire** plugin
(`pipewiresrc`), and `parec` from pulseaudio-utils.

`numpy` and `pyserial` are installed into the virtualenv by pip. PyGObject is
deliberately *not* — it cannot be reliably installed from pip, so the venv is
created with `--system-site-packages` and borrows the system copy.

On image-based systems (Silverblue, Kinoite, Bazzite) the installer prints the
`rpm-ostree install` line rather than running it, since layering needs a reboot.

## Commands

| Command | What it does |
|---|---|
| `skydimo gui` | open the configuration window |
| `skydimo info` | adapter port, device id, LED count, layout, service state |
| `skydimo test` | red/green/blue/white sweep, then a chase — proves wiring |
| `skydimo color <c>` | static colour: a name, `#rrggbb`, or `r,g,b` |
| `skydimo off` | blank the strip |
| `skydimo calibrate` | LED-count + layout wizard (`--leds N` to skip the question) |
| `skydimo run` | run whichever mode the config names — what the service runs |
| `skydimo ambilight` | mirror the screen edges onto the strip |
| `skydimo music` | react to playing audio (`--mode spectrum\|vu\|pulse`) |
| `skydimo set k=v` | read or write config values |

`calibrate` and `test` pause the engine automatically while they hold the strip,
then resume it. `color` and `off` do not — a running engine would just overwrite
them on the next frame, so they tell you to stop the service instead.

## The GUI

- **Service** — start/stop, start-at-login, ambilight/music mode
- **Strip** — LED count, colour order
- **Layout** — sides covered, first-LED corner, winding direction, LEDs per edge
  (auto-split from the detected display aspect ratio, or type your own), sample depth
- **Picture** — brightness, gamma, saturation, smoothing, minimum level, fps
- **Music mode** — style and gain
- **Hands-on checks** — identify the first LED, chase, colour sweep, static
  colour, all-off

Edits save themselves after a short pause.

## Tuning from the terminal

```bash
skydimo set                    # list everything
skydimo set gamma=1.5          # lower = brighter dark scenes (1.0 = off)
skydimo set saturation=1.4     # colour punch
skydimo set smoothing=0.5      # higher = lazier fades
skydimo set brightness=0.7     # global cap
skydimo set min_level=8        # faint floor so the strip never goes fully dark
skydimo set zone_depth=0.18    # sample further in from the screen edge
skydimo set fps=60
skydimo set mode=music
```

Config lives at `~/.config/skydimo/config.json`.

`gamma` defaults to 1.8. Screen pixels are already sRGB-encoded, so a full 2.2
double-corrects and makes dark desktops look nearly off; 1.8 is the usual
compromise. Drop toward 1.2–1.5 for a brighter, flatter look.

## How config changes reach a running engine

One unit runs whichever mode the config names, so switching modes does not mean
switching services.

- **Picture settings** — brightness, gamma, saturation, smoothing, minimum
  level, music gain — are re-read live, about twice a second. No restart, no gap.
- **Structural settings** — LED count, layout, colour order, mode, frame rate,
  capture size — need the engine rebuilt. It notices, tears down and re-enters
  itself in-process; the service keeps running throughout.

Editing the config is always enough. You never have to restart anything.

```bash
systemctl --user enable --now skydimo
journalctl --user -u skydimo -f
```

## Screen capture on Wayland

There is no "read the root window" on Wayland, so capture goes through the
`xdg-desktop-portal` ScreenCast API and a PipeWire stream consumed by GStreamer.
The first run asks permission once; the portal's restore token is saved to the
config so it does not ask again. To re-pick the monitor:

```bash
skydimo set restore_token=null
```

## Optional hardening

ModemManager probes CH340 serial bridges with AT commands for a few seconds
after plug-in, which can collide with the handshake. The driver retries, but
telling ModemManager to leave the port alone is cleaner — `./install.sh --udev`,
or by hand:

```bash
sudo cp packaging/70-skydimo.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Protocol

The reverse-engineered wire protocol — 1 000 000 baud, the DTR reset, the `r`/`m`
handshake, the `Tn?` frame header and the Fletcher trailer — is written up in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md), along with how it was recovered.

The short version of why the strip appears dead on Linux: **the adapter latches
into data mode and stops answering commands until the MCU is reset**, so the
driver has to pulse DTR low before handshaking. Windows' serial stack happens to
toggle DTR on open; pyserial and Hyperion do not.

## Layout of an install

```
~/.local/share/skydimo/venv/        virtualenv (numpy, pyserial, skydimo)
~/.local/bin/skydimo                launcher
~/.config/skydimo/config.json       settings
~/.config/systemd/user/skydimo.service
~/.local/share/applications/skydimo.desktop
```

## Troubleshooting

**No adapter found** — check `lsusb` for `1a86:7523`. Pin it explicitly with
`skydimo set port=/dev/ttyUSB0`.

**"Could not exclusively lock port"** — the engine already has it.
`systemctl --user stop skydimo`, or use the GUI's hands-on buttons, which do the
handover for you.

**Handshake fails repeatedly** — something else holds the port. Stop the engine
and install the udev rule above.

**Wrong number of LEDs lit, or the pattern wraps early** — the LED count is
wrong. Fix it in the GUI (**Strip → LED count**), which rebuilds the zones.

**Colours land on the wrong edges** — use the GUI's **Identify the first LED**
button, set **First LED corner** and **Winding direction** to match, then
**Auto-split**. If the per-edge counts still look off, type them into
**LEDs per edge**.

**Red shows up as green** — `skydimo set color_order=grb` (valid: `rgb bgr rbg
brg gbr grb`).

**Strip too dim** — `skydimo set gamma=1.3`, and/or `skydimo set min_level=6`.

**Music mode stays dark** — it listens to the default output's monitor source,
so something has to be playing. Try `skydimo music --gain 2.0` if your output is
quiet.

**Ambilight never starts / no permission dialog** — check `gst-inspect-1.0
pipewiresrc` works and that `xdg-desktop-portal` plus the backend for your
desktop are installed. `SKYDIMO_DEBUG=1 skydimo ambilight` logs each step of the
portal negotiation.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with Skydimo. HyperHDR is © its authors; no HyperHDR code is
included here.
