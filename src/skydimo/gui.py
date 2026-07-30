"""GTK4 / libadwaita configuration GUI for skydimo.

Design notes
------------
The engine and the GUI cannot both hold the serial port, so:

* Picture settings (brightness, gamma, saturation, smoothing, floor) are written
  to the config and picked up **live** by a running engine -- no restart, no
  port handover.
* Structural settings (LED count, layout, mode, capture size, fps) need the
  engine rebuilt, so saving them restarts the service if it is running.
* The hands-on buttons (test, chase, identify, static colour) need the port
  directly, so they stop the service for the duration and put it back as it was.

Device work happens on a worker thread; only `GLib.idle_add` touches widgets.
"""

from __future__ import annotations

import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import config as cfgmod
from . import layout as layoutmod
from . import service
from .calibrate import screen_geometry
from .device import COLOR_ORDERS, Skydimo

APP_ID = "org.theonestudio.Skydimo"

CORNER_LABELS = ["Top-left", "Top-right", "Bottom-right", "Bottom-left"]
CORNER_KEYS = ["tl", "tr", "br", "bl"]
ORDER_KEYS = ["rgb", "bgr", "rbg", "brg", "gbr", "grb"]
MODE_KEYS = ["ambilight", "music"]
MODE_LABELS = ["Ambilight (follow the screen)", "Music (follow the audio)"]
MUSIC_KEYS = ["spectrum", "vu", "pulse"]
MUSIC_LABELS = ["Spectrum", "VU meter", "Pulse"]


def _spin(lower, upper, step, value, digits=0, page=None):
    adj = Gtk.Adjustment(
        lower=lower, upper=upper, step_increment=step,
        page_increment=page or step * 10, value=value,
    )
    return Adw.SpinRow(adjustment=adj, digits=digits)


class SkydimoWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Skydimo", default_width=620,
                         default_height=800)
        self.cfg = cfgmod.load()
        self._loading = True
        self._save_timer: int | None = None
        self._busy = False

        self.toasts = Adw.ToastOverlay()
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        header = Adw.HeaderBar()
        self.status_lbl = Gtk.Label(label="", css_classes=["dim-label"])
        header.set_title_widget(
            Adw.WindowTitle(title="Skydimo", subtitle="LED strip control")
        )
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Reload config and service state")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_end(refresh)

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.set_content(self.toasts)
        self.set_content(tv)

        self._build_service(page)
        self._build_strip(page)
        self._build_layout(page)
        self._build_picture(page)
        self._build_music(page)
        self._build_actions(page)

        self._populate()
        self._loading = False
        GLib.timeout_add_seconds(3, self._tick_status)

    # ------------------------------------------------------------- building

    def _build_service(self, page):
        g = Adw.PreferencesGroup(title="Service")
        self.svc_row = Adw.ActionRow(title="Engine", subtitle="checking...")
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.btn_toggle = Gtk.Button(label="Start")
        self.btn_toggle.connect("clicked", self._on_toggle_service)
        box.append(self.btn_toggle)
        self.svc_row.add_suffix(box)
        g.add(self.svc_row)

        self.sw_autostart = Adw.SwitchRow(
            title="Start at login",
            subtitle="Run the engine automatically when you log in",
        )
        self.sw_autostart.connect("notify::active", self._on_autostart)
        g.add(self.sw_autostart)

        self.row_mode = Adw.ComboRow(
            title="Mode", model=Gtk.StringList.new(MODE_LABELS)
        )
        self.row_mode.connect("notify::selected", self._on_change_structural)
        g.add(self.row_mode)
        page.add(g)

    def _build_strip(self, page):
        g = Adw.PreferencesGroup(
            title="Strip",
            description="Set the LED count to match your strip. "
                        "This is the single most important setting.",
        )
        self.row_leds = _spin(1, 600, 1, 60)
        self.row_leds.set_title("LED count")
        self.row_leds.set_subtitle("How many LEDs are on the strip")
        self.row_leds.connect("notify::value", self._on_leds_changed)
        g.add(self.row_leds)

        self.row_order = Adw.ComboRow(
            title="Colour order",
            subtitle="Change this if red shows up as green",
            model=Gtk.StringList.new([k.upper() for k in ORDER_KEYS]),
        )
        self.row_order.connect("notify::selected", self._on_change_structural)
        g.add(self.row_order)
        page.add(g)

    def _build_layout(self, page):
        geo = screen_geometry()
        desc = "Where the strip sits around the monitor."
        if geo:
            desc += f" Detected display: {geo[0]}x{geo[1]}."
        g = Adw.PreferencesGroup(title="Layout", description=desc)

        self.row_sides = Adw.ComboRow(
            title="Sides covered",
            model=Gtk.StringList.new(
                ["All four sides", "Three sides (bottom bare)"]
            ),
        )
        self.row_sides.connect("notify::selected", self._on_layout_changed)
        g.add(self.row_sides)

        self.row_start = Adw.ComboRow(
            title="First LED corner",
            subtitle="Where LED #1 sits, seen from the front",
            model=Gtk.StringList.new(CORNER_LABELS),
        )
        self.row_start.connect("notify::selected", self._on_layout_changed)
        g.add(self.row_start)

        self.row_dir = Adw.ComboRow(
            title="Winding direction",
            model=Gtk.StringList.new(["Clockwise", "Counter-clockwise"]),
        )
        self.row_dir.connect("notify::selected", self._on_layout_changed)
        g.add(self.row_dir)

        self.row_counts = Adw.EntryRow(title="LEDs per edge (strip order)")
        self.row_counts.set_show_apply_button(True)
        self.row_counts.connect("apply", self._on_counts_apply)
        g.add(self.row_counts)

        self.row_depth = _spin(0.02, 0.5, 0.01, 0.12, digits=2)
        self.row_depth.set_title("Sample depth")
        self.row_depth.set_subtitle("How far in from the screen edge to sample")
        self.row_depth.connect("notify::value", self._on_layout_changed)
        g.add(self.row_depth)

        self.lbl_edges = Adw.ActionRow(title="Current split", subtitle="-")
        auto = Gtk.Button(label="Auto-split", valign=Gtk.Align.CENTER,
                          tooltip_text="Recompute from the display's aspect ratio")
        auto.connect("clicked", lambda *_: self._auto_counts(save=True))
        self.lbl_edges.add_suffix(auto)
        g.add(self.lbl_edges)
        page.add(g)

    def _build_picture(self, page):
        g = Adw.PreferencesGroup(
            title="Picture",
            description="These apply instantly, even while the engine runs.",
        )
        self.row_bright = _spin(0.0, 1.0, 0.05, 1.0, digits=2)
        self.row_bright.set_title("Brightness")
        self.row_gamma = _spin(1.0, 3.0, 0.1, 1.8, digits=2)
        self.row_gamma.set_title("Gamma")
        self.row_gamma.set_subtitle("Lower looks brighter on dark scenes")
        self.row_sat = _spin(0.0, 3.0, 0.05, 1.25, digits=2)
        self.row_sat.set_title("Saturation")
        self.row_smooth = _spin(0.0, 0.95, 0.05, 0.35, digits=2)
        self.row_smooth.set_title("Smoothing")
        self.row_smooth.set_subtitle("Higher fades more slowly")
        self.row_floor = _spin(0, 64, 1, 0)
        self.row_floor.set_title("Minimum level")
        self.row_floor.set_subtitle("Keeps the strip faintly lit on black")
        for r in (self.row_bright, self.row_gamma, self.row_sat,
                  self.row_smooth, self.row_floor):
            r.connect("notify::value", self._on_change_live)
            g.add(r)

        self.row_fps = _spin(5, 120, 5, 45)
        self.row_fps.set_title("Frame rate")
        self.row_fps.connect("notify::value", self._on_change_structural)
        g.add(self.row_fps)
        page.add(g)

    def _build_music(self, page):
        g = Adw.PreferencesGroup(title="Music mode")
        self.row_music = Adw.ComboRow(
            title="Style", model=Gtk.StringList.new(MUSIC_LABELS)
        )
        self.row_music.connect("notify::selected", self._on_change_structural)
        g.add(self.row_music)
        self.row_gain = _spin(0.1, 5.0, 0.1, 1.0, digits=2)
        self.row_gain.set_title("Gain")
        self.row_gain.set_subtitle("Raise if your output is quiet")
        self.row_gain.connect("notify::value", self._on_change_live)
        g.add(self.row_gain)
        page.add(g)

    def _build_actions(self, page):
        g = Adw.PreferencesGroup(
            title="Hands-on checks",
            description="These take over the strip briefly, "
                        "then hand it back to the engine.",
        )
        row = Adw.ActionRow(
            title="Identify the first LED",
            subtitle="Lights the start red and the end blue",
        )
        b = Gtk.Button(label="Identify", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self._run_device(self._act_identify))
        row.add_suffix(b)
        g.add(row)

        row = Adw.ActionRow(title="Chase", subtitle="Runs a dot along the strip")
        b = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self._run_device(self._act_chase))
        row.add_suffix(b)
        g.add(row)

        row = Adw.ActionRow(title="Colour sweep",
                            subtitle="Red, green, blue, white")
        b = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self._run_device(self._act_sweep))
        row.add_suffix(b)
        g.add(row)

        row = Adw.ActionRow(title="Static colour",
                            subtitle="Hold one colour on the strip")
        self.color_btn = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(), valign=Gtk.Align.CENTER
        )
        self.color_btn.connect("notify::rgba", self._on_static_color)
        row.add_suffix(self.color_btn)
        off = Gtk.Button(label="Off", valign=Gtk.Align.CENTER)
        off.connect("clicked", lambda *_: self._run_device(self._act_off,
                                                          blank=False))
        row.add_suffix(off)
        g.add(row)
        page.add(g)

    # ------------------------------------------------------------ populate

    def _populate(self):
        c = self.cfg
        lay = c.get("layout") or {}
        self.row_leds.set_value(int(c["led_count"]))
        self.row_order.set_selected(
            ORDER_KEYS.index(c["color_order"])
            if c["color_order"] in ORDER_KEYS else 0
        )
        self.row_mode.set_selected(
            MODE_KEYS.index(c.get("mode", "ambilight"))
            if c.get("mode") in MODE_KEYS else 0
        )
        self.row_sides.set_selected(0 if int(lay.get("sides", 4)) == 4 else 1)
        start = lay.get("start", "bl")
        self.row_start.set_selected(
            CORNER_KEYS.index(start) if start in CORNER_KEYS else 3
        )
        self.row_dir.set_selected(0 if lay.get("clockwise", True) else 1)
        self.row_counts.set_text(
            ",".join(str(x) for x in lay.get("counts", []))
        )
        self.row_depth.set_value(float(c["zone_depth"]))
        self.row_bright.set_value(float(c["brightness"]))
        self.row_gamma.set_value(float(c["gamma"]))
        self.row_sat.set_value(float(c["saturation"]))
        self.row_smooth.set_value(float(c["smoothing"]))
        self.row_floor.set_value(int(c["min_level"]))
        self.row_fps.set_value(int(c["fps"]))
        mm = c.get("music_mode", "spectrum")
        self.row_music.set_selected(
            MUSIC_KEYS.index(mm) if mm in MUSIC_KEYS else 0
        )
        self.row_gain.set_value(float(c.get("music_gain", 1.0)))
        self._refresh_edge_label()
        self._tick_status()

    def reload(self):
        self._loading = True
        self.cfg = cfgmod.load()
        self._populate()
        self._loading = False
        self._toast("Reloaded")

    def _refresh_edge_label(self):
        lay = self.cfg.get("layout") or {}
        counts = lay.get("counts") or []
        sides = int(lay.get("sides", 4))
        start = lay.get("start", "bl")
        cw = bool(lay.get("clockwise", True))
        if not counts:
            self.lbl_edges.set_subtitle("not set - use Auto-split")
            return
        try:
            edges = layoutmod.edge_sequence(sides, start, cw)
        except ValueError:
            self.lbl_edges.set_subtitle("invalid layout")
            return
        self.lbl_edges.set_subtitle(
            ", ".join(f"{n}={c}" for (n, _, _), c in zip(edges, counts))
            + f"  (total {sum(counts)})"
        )

    # -------------------------------------------------------------- status

    def _tick_status(self):
        line = service.status_line()
        self.svc_row.set_subtitle(line)
        active = "running" in line
        self.btn_toggle.set_label("Stop" if active else "Start")
        if self.sw_autostart.get_active() != service.is_enabled():
            self._loading, keep = True, self._loading
            self.sw_autostart.set_active(service.is_enabled())
            self._loading = keep
        return True

    def _toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # ------------------------------------------------------------ handlers

    def _current_layout(self):
        sides = 4 if self.row_sides.get_selected() == 0 else 3
        start = CORNER_KEYS[self.row_start.get_selected()]
        cw = self.row_dir.get_selected() == 0
        if sides == 3 and start not in ("bl", "br"):
            start = "bl"
            self._loading, keep = True, self._loading
            self.row_start.set_selected(CORNER_KEYS.index("bl"))
            self._loading = keep
            self._toast("A three-sided strip has to start at a bottom corner")
        if sides == 3:
            cw = start == "br"
        return sides, start, cw

    def _auto_counts(self, save: bool):
        n = int(self.row_leds.get_value())
        sides, start, cw = self._current_layout()
        geo = screen_geometry()
        aspect = geo[0] / geo[1] if geo else 16 / 9
        edges = layoutmod.edge_sequence(sides, start, cw)
        counts = layoutmod.split_counts(n, edges, aspect)
        self._loading, keep = True, self._loading
        self.row_counts.set_text(",".join(str(x) for x in counts))
        self._loading = keep
        if save:
            self._apply_layout(counts)

    def _apply_layout(self, counts=None):
        n = int(self.row_leds.get_value())
        sides, start, cw = self._current_layout()
        geo = screen_geometry()
        aspect = geo[0] / geo[1] if geo else 16 / 9
        edges = layoutmod.edge_sequence(sides, start, cw)
        if counts is None:
            counts = layoutmod.split_counts(n, edges, aspect)
        if len(counts) != len(edges) or sum(counts) != n:
            counts = layoutmod.split_counts(n, edges, aspect)
            self._loading, keep = True, self._loading
            self.row_counts.set_text(",".join(str(x) for x in counts))
            self._loading = keep
        self.cfg["led_count"] = n
        self.cfg["zone_depth"] = float(self.row_depth.get_value())
        self.cfg["zones"] = layoutmod.build_zones(
            n, sides=sides, start=start, clockwise=cw, aspect=aspect,
            depth=self.cfg["zone_depth"], counts=counts,
        )
        if geo:
            self.cfg["capture_width"] = 160
            self.cfg["capture_height"] = max(
                32, int(round(160 * geo[1] / geo[0]))
            )
        self.cfg["layout"] = {
            "sides": sides, "start": start, "clockwise": cw,
            "aspect": round(aspect, 5), "counts": counts,
            "description": layoutmod.describe(sides, start, cw, counts, edges),
        }
        self._refresh_edge_label()
        self._save(structural=True)

    def _on_leds_changed(self, *_):
        if self._loading:
            return
        self._auto_counts(save=True)

    def _on_layout_changed(self, *_):
        if self._loading:
            return
        self._auto_counts(save=True)

    def _on_counts_apply(self, *_):
        if self._loading:
            return
        raw = self.row_counts.get_text().strip()
        try:
            counts = [int(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            self._toast("Edge counts must be whole numbers")
            return
        n = int(self.row_leds.get_value())
        if sum(counts) != n:
            self._toast(f"Edge counts must add up to {n} (got {sum(counts)})")
            return
        self._apply_layout(counts)
        self._toast("Layout updated")

    def _gather_scalars(self):
        self.cfg["color_order"] = ORDER_KEYS[self.row_order.get_selected()]
        self.cfg["mode"] = MODE_KEYS[self.row_mode.get_selected()]
        self.cfg["brightness"] = round(self.row_bright.get_value(), 3)
        self.cfg["gamma"] = round(self.row_gamma.get_value(), 3)
        self.cfg["saturation"] = round(self.row_sat.get_value(), 3)
        self.cfg["smoothing"] = round(self.row_smooth.get_value(), 3)
        self.cfg["min_level"] = int(self.row_floor.get_value())
        self.cfg["fps"] = int(self.row_fps.get_value())
        self.cfg["music_mode"] = MUSIC_KEYS[self.row_music.get_selected()]
        self.cfg["music_gain"] = round(self.row_gain.get_value(), 3)

    def _on_change_live(self, *_):
        if self._loading:
            return
        self._save(structural=False)

    def _on_change_structural(self, *_):
        if self._loading:
            return
        self._save(structural=True)

    def _save(self, structural: bool):
        """Debounced write; a structural change also restarts the engine."""
        self._gather_scalars()
        if self._save_timer is not None:
            GLib.source_remove(self._save_timer)

        def flush():
            self._save_timer = None
            cfgmod.save(self.cfg)
            if structural:
                threading.Thread(target=self._restart_engine,
                                 daemon=True).start()
            return False

        self._save_timer = GLib.timeout_add(400, flush)

    def _restart_engine(self):
        if service.is_active():
            service.restart()
            GLib.idle_add(self._toast, "Applied - engine restarted")
        GLib.idle_add(self._tick_status)

    def _on_toggle_service(self, *_):
        def work():
            if service.is_active():
                service.stop()
                GLib.idle_add(self._toast, "Engine stopped")
            else:
                if not service.start():
                    GLib.idle_add(
                        self._toast,
                        "Could not start - see: journalctl --user -u skydimo",
                    )
                else:
                    GLib.idle_add(self._toast, "Engine started")
            GLib.idle_add(self._tick_status)
        threading.Thread(target=work, daemon=True).start()

    def _on_autostart(self, *_):
        if self._loading:
            return
        want = self.sw_autostart.get_active()
        threading.Thread(
            target=lambda: (service.enable() if want else service.disable(),
                            GLib.idle_add(self._tick_status)),
            daemon=True,
        ).start()

    def _on_static_color(self, *_):
        if self._loading:
            return
        rgba = self.color_btn.get_rgba()
        rgb = (int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))
        self._run_device(lambda dev: dev.fill(*rgb), blank=False,
                         restore_service=False)

    # ------------------------------------------------------- device access

    def _run_device(self, action, blank=True, restore_service=True):
        """Take the port from the engine, run `action(dev)`, hand it back."""
        if self._busy:
            self._toast("Still busy with the last check")
            return
        self._busy = True

        def work():
            was_active = service.is_active()
            if was_active:
                service.stop()
                time.sleep(0.7)
            try:
                dev = Skydimo(
                    port=None if self.cfg["port"] == "auto" else self.cfg["port"],
                    leds=int(self.cfg["led_count"]),
                    color_order=self.cfg["color_order"],
                    white_cal=self.cfg.get("white_cal"),
                )
            except Exception as exc:
                GLib.idle_add(self._toast, f"Adapter error: {exc}")
                self._busy = False
                if was_active and restore_service:
                    service.start()
                GLib.idle_add(self._tick_status)
                return
            try:
                action(dev)
            except Exception as exc:
                GLib.idle_add(self._toast, f"Failed: {exc}")
            finally:
                dev.close(blank=blank)
                self._busy = False
                if was_active and restore_service:
                    service.start()
                GLib.idle_add(self._tick_status)

        threading.Thread(target=work, daemon=True).start()

    # -- the actions themselves (worker thread, no widget access) ---------

    def _act_identify(self, dev):
        n = dev.leds
        marker = max(3, n // 12)
        px = [(0, 0, 0)] * n
        for i in range(marker):
            px[i] = (140, 0, 0)
        for i in range(max(marker, n - marker), n):
            px[i] = (0, 0, 140)
        dev.write(px)
        time.sleep(6.0)

    def _act_chase(self, dev):
        n = dev.leds
        px = [(0, 0, 0)] * n
        for i in range(n):
            px[i] = (150, 150, 0)
            if i:
                px[i - 1] = (0, 0, 0)
            dev.write(px)
            time.sleep(0.025)

    def _act_sweep(self, dev):
        for rgb in ((130, 0, 0), (0, 130, 0), (0, 0, 130), (100, 100, 100)):
            dev.fill(*rgb)
            time.sleep(1.0)

    def _act_off(self, dev):
        dev.fill(0, 0, 0)


class SkydimoApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.props.active_window or SkydimoWindow(self)
        win.present()


def main(argv=None) -> int:
    return SkydimoApp().run(argv or [])
