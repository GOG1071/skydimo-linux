"""Screen capture on Wayland via xdg-desktop-portal ScreenCast + PipeWire.

KDE on Wayland has no equivalent of X11's "just read the root window", so the
only sanctioned path is the ScreenCast portal: it hands back a PipeWire node id
plus a file descriptor for the PipeWire remote, which GStreamer's `pipewiresrc`
then consumes.

We ask the portal for a persistent token (persist_mode=2) and store it in the
config, so the "share your screen?" dialog appears once rather than every start.
"""

from __future__ import annotations

import os
import random
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
# GstApp must be imported for appsink's pull API to be bound onto the element.
from gi.repository import Gio, GLib, Gst, GstApp  # noqa: E402,F401

DEBUG = os.environ.get("SKYDIMO_DEBUG") == "1"


def _dbg(msg: str) -> None:
    if DEBUG:
        print(f"[capture] {msg}", file=sys.stderr, flush=True)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# SelectSources source types / cursor modes (portal spec).
SOURCE_MONITOR = 1
CURSOR_HIDDEN = 1
PERSIST_UNTIL_REVOKED = 2


class CaptureError(RuntimeError):
    pass


def _token() -> str:
    return f"skydimo{random.randint(0, 2**31)}"


class PortalScreenCast:
    """Negotiates a ScreenCast session and exposes the PipeWire node + fd."""

    def __init__(self, restore_token: str | None = None, show_cursor: bool = False):
        self.restore_token = restore_token
        self.show_cursor = show_cursor
        self.node_id: int | None = None
        self.pipewire_fd: int | None = None
        self.new_restore_token: str | None = None
        self._session: str | None = None
        self._loop = GLib.MainLoop()
        self._error: str | None = None
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._proxy = Gio.DBusProxy.new_sync(
            self._bus,
            Gio.DBusProxyFlags.NONE,
            None,
            PORTAL_BUS,
            PORTAL_PATH,
            SCREENCAST_IFACE,
            None,
        )
        self._unique = (
            self._bus.get_unique_name().lstrip(":").replace(".", "_")
        )

    # -- request plumbing ------------------------------------------------

    def _await_request(self, handle_token: str, on_response):
        """Subscribe to the Response signal for a portal Request."""
        path = f"{PORTAL_PATH}/request/{self._unique}/{handle_token}"

        def handler(_conn, _sender, _path, _iface, _signal, params):
            code, results = params.unpack()
            if code != 0:
                self._error = (
                    "screen capture request was cancelled or denied "
                    f"(portal response code {code})"
                )
                self._loop.quit()
                return
            try:
                on_response(results)
            except Exception as exc:  # surface into the caller's thread
                self._error = str(exc)
                self._loop.quit()

        return self._bus.signal_subscribe(
            PORTAL_BUS,
            REQUEST_IFACE,
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            handler,
        )

    def _call(self, method: str, args: GLib.Variant):
        return self._proxy.call_sync(
            method, args, Gio.DBusCallFlags.NONE, -1, None
        )

    # -- the three-step portal dance -------------------------------------

    def start(self, timeout: float = 300.0) -> None:
        session_token = _token()
        create_token = _token()

        self._await_request(create_token, self._on_session_created)
        self._call(
            "CreateSession",
            GLib.Variant(
                "(a{sv})",
                (
                    {
                        "handle_token": GLib.Variant("s", create_token),
                        "session_handle_token": GLib.Variant("s", session_token),
                    },
                ),
            ),
        )

        # Bail out rather than hang forever if the user ignores the dialog.
        timer = GLib.timeout_add_seconds(int(timeout), self._on_timeout)
        self._loop.run()
        if GLib.MainContext.default().find_source_by_id(timer) is not None:
            GLib.source_remove(timer)

        if self._error:
            raise CaptureError(self._error)
        if self.node_id is None or self.pipewire_fd is None:
            raise CaptureError("portal did not return a PipeWire stream")

    def _on_timeout(self) -> bool:
        self._error = (
            "timed out waiting for the screen-share permission dialog. "
            "Approve the KDE dialog (it may be behind another window), "
            "then run this again."
        )
        self._loop.quit()
        return False

    def _on_session_created(self, results: dict) -> None:
        _dbg("session created")
        self._session = results["session_handle"]
        select_token = _token()
        self._await_request(select_token, self._on_sources_selected)
        opts = {
            "handle_token": GLib.Variant("s", select_token),
            "types": GLib.Variant("u", SOURCE_MONITOR),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", CURSOR_HIDDEN),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        if self.restore_token:
            opts["restore_token"] = GLib.Variant("s", self.restore_token)
        _dbg(f"calling SelectSources (restore_token={bool(self.restore_token)})")
        self._call(
            "SelectSources",
            GLib.Variant("(oa{sv})", (self._session, opts)),
        )

    def _on_sources_selected(self, _results: dict) -> None:
        _dbg("sources selected; calling Start "
             "(the screen-share dialog appears now unless a token was reused)")
        start_token = _token()
        self._await_request(start_token, self._on_started)
        self._call(
            "Start",
            GLib.Variant(
                "(osa{sv})",
                (
                    self._session,
                    "",
                    {"handle_token": GLib.Variant("s", start_token)},
                ),
            ),
        )

    def _on_started(self, results: dict) -> None:
        _dbg("Start returned")
        streams = results.get("streams") or []
        if not streams:
            raise CaptureError("portal returned no screen streams")
        self.node_id = streams[0][0]
        self.new_restore_token = results.get("restore_token")

        reply = self._proxy.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self._session, {})),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
        # reply is (Variant, Gio.UnixFDList)
        variant, fd_list = reply[0], reply[1]
        self.pipewire_fd = fd_list.steal_fds()[variant.unpack()[0]]
        _dbg(f"pipewire node={self.node_id} fd={self.pipewire_fd}")
        self._loop.quit()


class ScreenCapture:
    """Delivers downscaled BGRx frames from the shared monitor."""

    def __init__(
        self,
        width: int = 160,
        height: int = 90,
        fps: int = 45,
        restore_token: str | None = None,
    ):
        self.width = width
        self.height = height
        self.fps = fps
        self.restore_token = restore_token
        self.new_restore_token: str | None = None
        self._pipeline = None
        self._sink = None
        self._lock = threading.Lock()

    def start(self) -> None:
        Gst.init(None)
        portal = PortalScreenCast(self.restore_token)
        portal.start()
        self.new_restore_token = portal.new_restore_token

        desc = (
            f"pipewiresrc fd={portal.pipewire_fd} path={portal.node_id} "
            f"do-timestamp=true keepalive-time=1000 resend-last=true "
            f"! videorate ! video/x-raw,framerate={self.fps}/1 "
            # method=0 is nearest-neighbour: we are downscaling to a handful of
            # pixels per zone, so interpolation quality buys nothing.
            f"! videoconvert ! videoscale method=0 "
            f"! video/x-raw,format=BGRx,width={self.width},height={self.height} "
            f"! appsink name=out max-buffers=1 drop=true sync=false"
        )
        self._pipeline = Gst.parse_launch(desc)
        self._sink = self._pipeline.get_by_name("out")
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise CaptureError("could not start the GStreamer capture pipeline")

    def read(self, timeout: float = 1.0):
        """Return an (height, width, 4) BGRx numpy view, or None on timeout."""
        import numpy as np

        if self._sink is None:
            raise RuntimeError("capture not started")
        sample = self._sink.try_pull_sample(int(timeout * Gst.SECOND))
        if sample is None:
            return None
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            # Rows may be padded, so honour the real stride.
            stride = len(info.data) // h
            arr = np.frombuffer(info.data, dtype=np.uint8, count=h * stride)
            return arr.reshape(h, stride // 4, 4)[:, :w, :].copy()
        finally:
            buf.unmap(info)

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._sink = None
