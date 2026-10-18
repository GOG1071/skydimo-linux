"""Tests for the parts that do not need hardware or a desktop session.

Runs in CI without GTK, GStreamer, a display or the adapter. `skydimo.gui` is
never imported, and `skydimo.ambilight` imports `skydimo.capture` lazily inside
`run()` so its sampling helpers stay usable here.
"""

import unittest

import numpy as np

from skydimo import layout, music
from skydimo.device import COLOR_ORDERS, Skydimo
from skydimo.render import Shaper, gamma_lut


def bare_device(leds, color_order="rgb", white_cal=None):
    """A Skydimo instance with no serial port attached."""
    d = Skydimo.__new__(Skydimo)
    d.leds = leds
    d.color_order = color_order
    d.white_cal = tuple(white_cal) if white_cal else None
    d.s = None
    d._header = d._build_header()
    return d


class TestFrameFormat(unittest.TestCase):
    def test_header_plain(self):
        # 3 LEDs, no white calibration -> 'T','n','h', count-1 big-endian, xor check
        d = bare_device(3)
        self.assertEqual(d._header, bytes([0x54, 0x6E, 0x68, 0x00, 0x02, 0x57]))

    def test_header_white_cal(self):
        d = bare_device(300, white_cal=(255, 255, 255, 255))
        n = 299
        hi, lo = (n >> 8) & 0xFF, n & 0xFF
        self.assertEqual(
            d._header, bytes([0x54, 0x6E, 0x54, hi, lo, hi ^ lo ^ 0x55])
        )

    def test_header_count_boundaries(self):
        for leds in (1, 2, 256, 257, 512, 600):
            d = bare_device(leds)
            n = leds - 1
            hi, lo = (n >> 8) & 0xFF, n & 0xFF
            self.assertEqual(d._header[3], hi)
            self.assertEqual(d._header[4], lo)
            self.assertEqual(d._header[5], hi ^ lo ^ 0x55)

    def test_fletcher_matches_reference(self):
        d = bare_device(1)
        for body in (b"", bytes(range(0, 255, 7)), bytes(256), bytes(range(256))):
            f1 = f2 = ext = 0
            for i, b in enumerate(body):
                ext = (ext + (b ^ (i & 0xFF))) % 255
                f1 = (f1 + b) % 255
                f2 = (f2 + f1) % 255
            expect = bytes((f1, f2, ext if ext != 0x41 else 0xAA))
            self.assertEqual(d._trailer(body), expect)

    def test_fletcher_reserves_0x41(self):
        """0x41 must never appear as the third trailer byte."""
        d = bare_device(1)
        found = False
        for length in range(1, 400):
            body = bytes((i * 3) % 256 for i in range(length))
            ext = 0
            for i, b in enumerate(body):
                ext = (ext + (b ^ (i & 0xFF))) % 255
            trailer = d._trailer(body)
            if ext == 0x41:
                found = True
                self.assertEqual(trailer[2], 0xAA)
            else:
                self.assertEqual(trailer[2], ext)
        self.assertTrue(found, "no sample produced ext == 0x41; test is not exercising it")

    def test_frame_length(self):
        d = bare_device(10)
        sent = {}
        d.write_raw = lambda b: sent.__setitem__("b", bytes(b))
        d.write([(1, 2, 3)] * 10)
        self.assertEqual(len(sent["b"]), 30)

    def test_write_raw_rejects_wrong_length(self):
        d = bare_device(4)
        with self.assertRaises(ValueError):
            d.write_raw(b"\x00" * 11)


class TestColorOrder(unittest.TestCase):
    def test_all_orders(self):
        expected = {
            "rgb": (10, 20, 30), "rbg": (10, 30, 20), "grb": (20, 10, 30),
            "gbr": (20, 30, 10), "brg": (30, 10, 20), "bgr": (30, 20, 10),
        }
        self.assertEqual(set(expected), set(COLOR_ORDERS))
        for order, want in expected.items():
            d = bare_device(1, color_order=order)
            sent = {}
            d.write_raw = lambda b: sent.__setitem__("b", bytes(b))
            d.write([(10, 20, 30)])
            self.assertEqual(sent["b"], bytes(want), order)

    def test_rejects_unknown_order(self):
        with self.assertRaises(ValueError):
            Skydimo(port="/dev/null", color_order="xyz", open_now=False)


class TestLayout(unittest.TestCase):
    TOPOLOGIES = [
        (4, "bl", True), (4, "bl", False), (4, "tl", True),
        (4, "tr", False), (4, "br", True), (3, "bl", True), (3, "br", True),
    ]

    def test_zone_count_and_bounds(self):
        for sides, start, cw in self.TOPOLOGIES:
            for n in (1, 2, 30, 96, 118, 133, 240, 600):
                for aspect in (16 / 9, 2.389, 1.0, 4 / 3):
                    z = layout.build_zones(
                        n, sides=sides, start=start, clockwise=cw, aspect=aspect
                    )
                    self.assertEqual(len(z), n, (sides, start, cw, n))
                    for x0, y0, x1, y1 in z:
                        self.assertTrue(0.0 <= x0 < x1 <= 1.0, (x0, x1))
                        self.assertTrue(0.0 <= y0 < y1 <= 1.0, (y0, y1))

    def test_counts_sum_to_total(self):
        for sides, start, cw in self.TOPOLOGIES:
            edges = layout.edge_sequence(sides, start, cw)
            for n in (1, 2, 3, 4, 5, 30, 97, 118, 599):
                for aspect in (1.0, 16 / 9, 2.389, 10.0):
                    counts = layout.split_counts(n, edges, aspect)
                    self.assertEqual(sum(counts), n, (sides, start, n, counts))
                    self.assertEqual(len(counts), len(edges))
                    self.assertTrue(all(c >= 0 for c in counts), counts)
                    # With plenty of LEDs to go round, no edge should be starved.
                    if n >= 8 * len(edges):
                        self.assertTrue(all(c >= 1 for c in counts), counts)

    def test_split_terminates_when_total_below_edge_count(self):
        """Regression: the old drift-correction loop spun forever here."""
        for sides, start, cw in self.TOPOLOGIES:
            edges = layout.edge_sequence(sides, start, cw)
            for n in (1, 2, 3):
                counts = layout.split_counts(n, edges, 16 / 9)
                self.assertEqual(sum(counts), n)
                self.assertEqual(len(layout.build_zones(
                    n, sides=sides, start=start, clockwise=cw,
                    aspect=16 / 9, counts=counts)), n)

    def test_long_edges_get_more_leds(self):
        edges = layout.edge_sequence(4, "bl", True)
        counts = dict(
            zip((e[0] for e in edges), layout.split_counts(200, edges, 2.389))
        )
        self.assertGreater(counts["top"], counts["left"])
        self.assertEqual(counts["top"], counts["bottom"])
        self.assertEqual(counts["left"], counts["right"])

    def test_three_sided_skips_bottom(self):
        for start in ("bl", "br"):
            names = [e[0] for e in layout.edge_sequence(3, start, True)]
            self.assertNotIn("bottom", names)
            self.assertEqual(len(names), 3)

    def test_three_sided_rejects_top_start(self):
        for bad in ("tl", "tr"):
            with self.assertRaises(ValueError):
                layout.edge_sequence(3, bad, True)

    def test_edges_are_adjacent_and_unique(self):
        for sides, start, cw in self.TOPOLOGIES:
            edges = layout.edge_sequence(sides, start, cw)
            self.assertEqual(edges[0][1], start)
            self.assertEqual(len({e[0] for e in edges}), len(edges))
            for i in range(len(edges) - 1):
                self.assertEqual(edges[i][2], edges[i + 1][1])

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            layout.build_zones(0)
        with self.assertRaises(ValueError):
            layout.edge_sequence(5, "bl", True)
        with self.assertRaises(ValueError):
            layout.build_zones(10, counts=[10])          # wrong edge count
        with self.assertRaises(ValueError):
            layout.build_zones(10, counts=[1, 1, 1, 1])  # wrong sum


class TestShaper(unittest.TestCase):
    def test_identity(self):
        s = Shaper(4, brightness=1.0, gamma=1.0, saturation=1.0, smoothing=0.0)
        out = s(np.array([[255, 0, 0]] * 4, dtype=float))
        self.assertEqual(out.dtype, np.uint8)
        self.assertEqual(tuple(out[0]), (255, 0, 0))

    def test_brightness_scales(self):
        s = Shaper(1, brightness=0.5, gamma=1.0, saturation=1.0, smoothing=0.0)
        self.assertEqual(s(np.array([[200, 200, 200]], float))[0][0], 100)

    def test_gamma_preserves_endpoints(self):
        lut = gamma_lut(1.8)
        self.assertEqual(lut[0], 0)
        self.assertEqual(lut[255], 255)
        self.assertTrue(np.all(np.diff(lut.astype(int)) >= 0), "LUT must be monotonic")

    def test_min_level_floor(self):
        s = Shaper(2, gamma=1.0, saturation=1.0, smoothing=0.0, min_level=7)
        self.assertTrue(np.all(s(np.zeros((2, 3))) >= 7))

    def test_smoothing_converges(self):
        s = Shaper(1, gamma=1.0, saturation=1.0, smoothing=0.5)
        target = np.array([[200.0, 200.0, 200.0]])
        s(np.zeros((1, 3)))
        for _ in range(60):
            out = s(target)
        self.assertGreater(out[0][0], 190)

    def test_live_update(self):
        s = Shaper(1, brightness=1.0, gamma=1.0, saturation=1.0, smoothing=0.0)
        s.update(brightness=0.25)
        self.assertEqual(s(np.array([[200, 200, 200]], float))[0][0], 50)
        s.update(min_level=9)
        self.assertTrue(np.all(s(np.zeros((1, 3))) >= 9))

    def test_output_always_in_range(self):
        s = Shaper(3, brightness=1.0, gamma=1.8, saturation=2.5, smoothing=0.3)
        for frame in (np.zeros((3, 3)), np.full((3, 3), 255.0),
                      np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255]], float)):
            out = s(frame)
            self.assertEqual(out.dtype, np.uint8)
            self.assertTrue(out.min() >= 0 and out.max() <= 255)


class TestMusic(unittest.TestCase):
    def test_band_tracking_follows_frequency(self):
        n, sr = music.CHUNK, music.RATE
        t = np.arange(n) / sr
        peaks = []
        for freq in (60, 1000, 8000):
            a = music.Analyser(24)
            for _ in range(12):
                levels, _ = a(0.5 * np.sin(2 * np.pi * freq * t))
            peaks.append(int(np.argmax(levels)))
        self.assertEqual(peaks, sorted(peaks), f"peak bands not ascending: {peaks}")
        self.assertLess(peaks[0], peaks[-1])

    def test_silence_is_quiet(self):
        a = music.Analyser(24)
        for _ in range(12):
            levels, rms = a(np.zeros(music.CHUNK))
        self.assertAlmostEqual(rms, 0.0, places=6)
        self.assertLess(levels.mean(), 0.01)

    def test_frame_shapes(self):
        levels = np.linspace(0, 1, 16)
        for leds in (1, 2, 59, 60, 118, 133):
            for frame in (
                music._frame_spectrum(levels, leds),
                music._frame_pulse(levels, leds),
                music._frame_vu(0.4, leds, 1.0),
            ):
                self.assertEqual(frame.shape, (leds, 3), leds)
                self.assertTrue(frame.min() >= 0.0)
                self.assertTrue(frame.max() <= 255.001)

    def test_spectrum_is_mirrored(self):
        levels = np.linspace(0, 1, 16)
        frame = music._frame_spectrum(levels, 60)
        np.testing.assert_allclose(frame[0], frame[-1], atol=1e-9)
        np.testing.assert_allclose(frame[29], frame[30], atol=1e-9)

    def test_hsv_primaries(self):
        rgb = music._hsv_to_rgb(np.array([0.0, 1 / 3, 2 / 3]), 1.0, np.ones(3))
        np.testing.assert_allclose(rgb[0], [255, 0, 0], atol=1)
        np.testing.assert_allclose(rgb[1], [0, 255, 0], atol=1)
        np.testing.assert_allclose(rgb[2], [0, 0, 255], atol=1)


class TestAmbilightSampling(unittest.TestCase):
    def test_zones_map_to_pixels(self):
        from skydimo.ambilight import sample, zone_slices
        zones = layout.build_zones(60, sides=4, start="bl", clockwise=True,
                                   aspect=2.389)
        for w, h in ((160, 67), (64, 36), (8, 8)):
            slices = zone_slices(zones, w, h)
            self.assertEqual(len(slices), 60)
            for y0, y1, x0, x1 in slices:
                self.assertTrue(0 <= y0 < y1 <= h)
                self.assertTrue(0 <= x0 < x1 <= w)
            frame = np.zeros((h, w, 4), dtype=np.uint8)
            frame[:, :, 0] = 200          # blue channel in BGRx
            out = sample(frame, slices)
            self.assertEqual(out.shape, (60, 3))
            np.testing.assert_allclose(out[:, 2], 200)   # -> RGB blue
            np.testing.assert_allclose(out[:, 0], 0)

    def test_sampling_picks_up_edge_position(self):
        """A bright band at the top of the frame must light the top zones only."""
        from skydimo.ambilight import sample, zone_slices
        counts = [10, 30, 10, 30]
        zones = layout.build_zones(80, sides=4, start="bl", clockwise=True,
                                   aspect=3.0, counts=counts)
        w, h = 120, 40
        slices = zone_slices(zones, w, h)
        frame = np.zeros((h, w, 4), dtype=np.uint8)
        frame[0:4, :, 2] = 255                      # red band along the top
        out = sample(frame, slices)
        # strip order is left(10), top(30), right(10), bottom(30)
        top = out[10:40, 0]
        bottom = out[50:80, 0]
        self.assertGreater(top.mean(), bottom.mean() + 10)


class TestFindPort(unittest.TestCase):
    def _find(self, exists=False, comports=(), globbed=()):
        from unittest import mock
        from skydimo import device
        with mock.patch.object(device.os.path, "exists", return_value=exists), \
             mock.patch.object(device.list_ports, "comports", return_value=list(comports)), \
             mock.patch.object(device.glob, "glob",
                               side_effect=lambda pat: [g for g in globbed if g.startswith(pat[:-1])]):
            return device.find_port()

    def test_prefers_udev_symlink(self):
        self.assertEqual(self._find(exists=True, globbed=["/dev/ttyUSB0"]), "/dev/skydimo")

    def test_never_falls_back_to_ttyacm(self):
        # e.g. an LCD panel on ttyACM0 with no Skydimo adapter plugged in
        with self.assertRaises(FileNotFoundError):
            self._find(globbed=["/dev/ttyACM0"])

    def test_falls_back_to_ttyusb(self):
        self.assertEqual(self._find(globbed=["/dev/ttyACM0", "/dev/ttyUSB1"]), "/dev/ttyUSB1")


if __name__ == "__main__":
    unittest.main()
