"""Map LED index -> screen sampling rectangle.

An ambilight strip runs around the monitor's perimeter. To sample the screen we
need to know, for each LED in *strip order*, where it sits on that perimeter.
`build_zones` turns a small human-answerable description (how many sides the
strip covers, which corner LED #1 sits at, and which way it winds) into one
normalized [x0, y0, x1, y1] rect per LED.

Screen coordinates are normalized with the origin at the top-left, so y grows
downward: tl=(0,0), tr=(1,0), br=(1,1), bl=(0,1).
"""

from __future__ import annotations

CORNERS = {
    "tl": (0.0, 0.0),
    "tr": (1.0, 0.0),
    "br": (1.0, 1.0),
    "bl": (0.0, 1.0),
}

# Clockwise and counter-clockwise corner rings, as seen on screen.
_CW = ["tl", "tr", "br", "bl"]
_CCW = ["tl", "bl", "br", "tr"]


def _edge_name(a: str, b: str) -> str:
    s = {a, b}
    if s == {"tl", "tr"}:
        return "top"
    if s == {"tr", "br"}:
        return "right"
    if s == {"br", "bl"}:
        return "bottom"
    if s == {"bl", "tl"}:
        return "left"
    raise ValueError(f"{a} and {b} are not adjacent corners")


def edge_sequence(sides: int, start: str, clockwise: bool) -> list[tuple[str, str, str]]:
    """Ordered (edge_name, from_corner, to_corner) tuples in strip order.

    sides=4 walks the full perimeter from `start`.
    sides=3 skips the bottom edge, so `start` must be a bottom corner.
    """
    if start not in CORNERS:
        raise ValueError(f"start must be one of {sorted(CORNERS)}")

    if sides == 3:
        if start == "bl":
            path = ["bl", "tl", "tr", "br"]
        elif start == "br":
            path = ["br", "tr", "tl", "bl"]
        else:
            raise ValueError(
                "a 3-sided strip must start at a bottom corner ('bl' or 'br')"
            )
        return [
            (_edge_name(path[i], path[i + 1]), path[i], path[i + 1])
            for i in range(3)
        ]

    if sides != 4:
        raise ValueError("sides must be 3 or 4")

    ring = _CW if clockwise else _CCW
    i = ring.index(start)
    ring = ring[i:] + ring[:i]
    return [
        (_edge_name(ring[k], ring[(k + 1) % 4]), ring[k], ring[(k + 1) % 4])
        for k in range(4)
    ]


def split_counts(total: int, edges, aspect: float) -> list[int]:
    """Distribute `total` LEDs across `edges` proportionally to edge length.

    A uniform-density strip has more LEDs along the long edges. `aspect` is
    width/height of the display.

    Uses largest-remainder apportionment: floor every exact share, then hand the
    leftovers to the edges with the biggest fractional parts. That sums to
    `total` by construction, for any total -- including a total smaller than the
    number of edges, where some edges legitimately get zero. (An earlier version
    seeded every edge with at least one LED and corrected the drift in a loop,
    which could not terminate in exactly that case.)
    """
    lengths = [aspect if name in ("top", "bottom") else 1.0 for name, _, _ in edges]
    span = sum(lengths)
    exact = [total * L / span for L in lengths]
    counts = [int(x) for x in exact]
    leftover = total - sum(counts)
    # Ties broken by longer edge first, so the split is deterministic.
    order = sorted(
        range(len(counts)),
        key=lambda i: (-(exact[i] - counts[i]), -lengths[i], i),
    )
    for k in range(leftover):
        counts[order[k % len(order)]] += 1
    return counts


def build_zones(
    total: int,
    sides: int = 4,
    start: str = "bl",
    clockwise: bool = True,
    aspect: float = 16 / 9,
    depth: float = 0.12,
    counts: list[int] | None = None,
) -> list[list[float]]:
    """One normalized sampling rect per LED, in strip order."""
    if total < 1:
        raise ValueError("total must be >= 1")
    edges = edge_sequence(sides, start, clockwise)
    if counts is None:
        counts = split_counts(total, edges, aspect)
    if len(counts) != len(edges):
        raise ValueError(f"expected {len(edges)} edge counts, got {len(counts)}")
    if sum(counts) != total:
        raise ValueError(f"edge counts sum to {sum(counts)}, expected {total}")

    zones: list[list[float]] = []
    for (name, a, b), n in zip(edges, counts):
        ax, ay = CORNERS[a]
        bx, by = CORNERS[b]
        for k in range(n):
            # Centre of this LED's slice of the edge.
            t = (k + 0.5) / n
            cx = ax + (bx - ax) * t
            cy = ay + (by - ay) * t
            # Widen the sample a little past the LED's own slice so adjacent
            # zones overlap slightly; that smooths the result noticeably.
            half = (1.0 / n) * 0.75
            t0, t1 = max(0.0, t - half), min(1.0, t + half)
            if name in ("top", "bottom"):
                x0 = min(ax + (bx - ax) * t0, ax + (bx - ax) * t1)
                x1 = max(ax + (bx - ax) * t0, ax + (bx - ax) * t1)
                if name == "top":
                    y0, y1 = 0.0, depth
                else:
                    y0, y1 = 1.0 - depth, 1.0
            else:
                y0 = min(ay + (by - ay) * t0, ay + (by - ay) * t1)
                y1 = max(ay + (by - ay) * t0, ay + (by - ay) * t1)
                if name == "left":
                    x0, x1 = 0.0, depth
                else:
                    x0, x1 = 1.0 - depth, 1.0
            _ = (cx, cy)  # centre kept for readability of the geometry above
            zones.append([round(x0, 5), round(y0, 5), round(x1, 5), round(y1, 5)])
    return zones


def describe(sides: int, start: str, clockwise: bool, counts: list[int], edges) -> str:
    parts = [f"{name}={n}" for (name, _, _), n in zip(edges, counts)]
    spin = "clockwise" if clockwise else "counter-clockwise"
    return f"{sides}-sided, starts at {start}, {spin}: " + ", ".join(parts)
