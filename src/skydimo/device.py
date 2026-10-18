"""Skydimo "Rainbow Music LED" USB adapter driver.

Reverse-engineered from Rainbow-Music-Led-22.9.11-Windows-AMD64.exe, which is a
rebranded HyperHDR-14 fork. The relevant vendor driver is
`sources/led-drivers/serial/DriverSerialSkydimondUsb.cpp` (device type
"skydimond-usb"), riding on `ProviderSerial.cpp`.

Wire protocol
-------------
Link: 1_000_000 baud, 8N1.

The MCU boots into a *command* mode and latches into *data* mode after a
successful handshake. It stops answering commands once latched, so the driver
always pulses DTR low to reset the MCU before handshaking -- this is the step
that makes it work from a cold or half-open state.

    handshake:  ->  b'r'    <-  b'R2A2\\r\\n'   (device id)
                ->  b'm'    <-  b'ok\\r\\n'     (enter data mode)

Frame (Adalight/AWA lineage, vendor magic):

    'T' 'n' X   hi  lo  chk   RGB*N   [wcLimit wcR wcG wcB]   f1 f2 f3

    X    'T' (0x54) when white-channel calibration is active, else 'h' (0x68)
    hi   ((N - 1) >> 8) & 0xFF
    lo    (N - 1)       & 0xFF
    chk  hi ^ lo ^ 0x55
    the four white-channel bytes are present only when calibration is active
    f1/f2/f3  Fletcher triple over the pixel bytes *and* the white-channel
              bytes, if any:
                  f1  = (f1 + b) % 255
                  f2  = (f2 + f1) % 255
                  ext = (ext + (b ^ i)) % 255      i = byte index, & 0xFF
                  f3  = ext, or 0xAA when ext == 0x41
"""

from __future__ import annotations

import glob
import os
import time

import serial
from serial.tools import list_ports

BAUD = 1_000_000
VID_CH340 = 0x1A86
PID_CH340 = 0x7523
UDEV_SYMLINK = "/dev/skydimo"  # created by packaging/70-skydimo.rules

HDR_MAGIC0 = 0x54  # 'T'
HDR_MAGIC1 = 0x6E  # 'n'
HDR_PLAIN = 0x68  # 'h'  -- no white-channel calibration
HDR_WHITE = 0x54  # 'T'  -- white-channel calibration appended
HEADER_SIZE = 6

COLOR_ORDERS = {
    "rgb": (0, 1, 2),
    "rbg": (0, 2, 1),
    "grb": (1, 0, 2),
    "gbr": (1, 2, 0),
    "brg": (2, 0, 1),
    "bgr": (2, 1, 0),
}


class HandshakeError(RuntimeError):
    """The adapter did not complete the r/m handshake."""


def find_port() -> str:
    """Locate the adapter's serial port (CH340 bridge)."""
    # The udev rule names the adapter, so this never has to guess.
    if os.path.exists(UDEV_SYMLINK):
        return UDEV_SYMLINK
    for p in list_ports.comports():
        if p.vid == VID_CH340 and p.pid == PID_CH340:
            return p.device
    # Fall back to USB serial bridges only. The CH340 never enumerates as
    # ttyACM, and ttyACM nodes are other people's CDC devices (LCD panels,
    # microcontrollers): the DTR reset and 1 Mbaud handshake would disrupt them.
    found = sorted(glob.glob("/dev/ttyUSB*"))
    if found:
        return found[0]
    raise FileNotFoundError(
        "No USB serial adapter found. Is the Skydimo adapter plugged in?"
    )


class Skydimo:
    """A connected Skydimo LED adapter."""

    def __init__(
        self,
        port: str | None = None,
        leds: int = 60,
        color_order: str = "rgb",
        white_cal: tuple[int, int, int, int] | None = None,
        settle: float = 2.0,
        attempts: int = 4,
        open_now: bool = True,
    ):
        if leds < 1:
            raise ValueError("leds must be >= 1")
        if color_order not in COLOR_ORDERS:
            raise ValueError(f"color_order must be one of {sorted(COLOR_ORDERS)}")
        self.port = port or find_port()
        self.leds = leds
        self.color_order = color_order
        self.white_cal = tuple(white_cal) if white_cal else None
        self.settle = settle
        self.attempts = attempts
        self.device_id: bytes = b""
        self.s: serial.Serial | None = None
        self._header = self._build_header()
        if open_now:
            self.open()

    # ---------------------------------------------------------------- link

    def open(self) -> None:
        """Open the port, reset the MCU, and complete the handshake."""
        last = None
        for attempt in range(1, self.attempts + 1):
            try:
                self._open_once()
                return
            except (serial.SerialException, HandshakeError, OSError) as exc:
                last = exc
                self._quiet_close()
                # ModemManager or a previous client may still be probing the
                # port right after plug-in; back off and retry.
                time.sleep(0.6 * attempt)
        hint = ""
        if last is not None and "lock" in str(last).lower():
            hint = (
                " Something else already holds the port -- most likely the "
                "engine. Try: systemctl --user stop skydimo"
            )
        raise HandshakeError(
            f"Could not initialise adapter on {self.port} after "
            f"{self.attempts} attempts: {last}.{hint}"
        )

    def _open_once(self) -> None:
        self.s = serial.Serial(
            self.port, BAUD, timeout=1.5, write_timeout=3, exclusive=True
        )
        # Reset the MCU. Without this the adapter stays latched in data mode
        # from a previous session and never answers the handshake.
        self.s.dtr = False
        self.s.rts = False
        time.sleep(0.3)
        self.s.dtr = True
        self.s.rts = True
        time.sleep(self.settle)  # matches the vendor's delayAfterConnect
        self.s.reset_input_buffer()
        self.s.reset_output_buffer()

        self.device_id = self._command(b"r").strip()
        ack = self._command(b"m")
        if b"ok" not in ack:
            raise HandshakeError(
                f'adapter did not acknowledge data mode; "m" returned {ack!r}'
            )

    def _command(self, cmd: bytes) -> bytes:
        assert self.s is not None
        self.s.write(cmd)
        self.s.flush()
        reply = self.s.read_until(b"\n")
        if not reply:
            raise HandshakeError(f"no reply to command {cmd!r}")
        return reply

    def _quiet_close(self) -> None:
        if self.s is not None:
            try:
                self.s.close()
            except Exception:
                pass
            self.s = None

    def close(self, blank: bool = True) -> None:
        if self.s is not None and blank:
            try:
                self.fill(0, 0, 0)
                time.sleep(0.05)
            except Exception:
                pass
        self._quiet_close()

    def __enter__(self) -> "Skydimo":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -------------------------------------------------------------- frames

    def set_leds(self, leds: int) -> None:
        """Change the LED count (rebuilds the frame header)."""
        if leds < 1:
            raise ValueError("leds must be >= 1")
        self.leds = leds
        self._header = self._build_header()

    def _build_header(self) -> bytes:
        n = self.leds - 1
        hi, lo = (n >> 8) & 0xFF, n & 0xFF
        magic2 = HDR_WHITE if self.white_cal else HDR_PLAIN
        return bytes([HDR_MAGIC0, HDR_MAGIC1, magic2, hi, lo, hi ^ lo ^ 0x55])

    def _trailer(self, body: bytes | bytearray) -> bytes:
        f1 = f2 = ext = 0
        for i, b in enumerate(body):
            ext = (ext + (b ^ (i & 0xFF))) % 255
            f1 = (f1 + b) % 255
            f2 = (f2 + f1) % 255
        return bytes((f1, f2, ext if ext != 0x41 else 0xAA))

    def write_raw(self, rgb: bytes | bytearray) -> None:
        """Send one frame. `rgb` must be exactly leds*3 bytes, already ordered."""
        if len(rgb) != self.leds * 3:
            raise ValueError(
                f"expected {self.leds * 3} bytes ({self.leds} LEDs), got {len(rgb)}"
            )
        if self.s is None:
            raise RuntimeError("device is not open")
        body = bytearray(rgb)
        if self.white_cal:
            body += bytes(self.white_cal)
        body += self._trailer(body)
        self.s.write(self._header + bytes(body))
        self.s.flush()

    def write(self, pixels) -> None:
        """Send one frame from an iterable of (r, g, b) triples."""
        i0, i1, i2 = COLOR_ORDERS[self.color_order]
        body = bytearray()
        for px in pixels:
            body += bytes((px[i0] & 0xFF, px[i1] & 0xFF, px[i2] & 0xFF))
        self.write_raw(body)

    def fill(self, r: int, g: int, b: int) -> None:
        self.write([(r, g, b)] * self.leds)
