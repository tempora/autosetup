"""Draw the app icon source (icon.png, 1024 px): a circuit loop in lap B's orange with lap
A's blue marker on the dark background. `npx tauri icon icon.png` makes the rest.

    .venv\\Scripts\\python desktop\\make_icon.py
"""

import struct
import zlib
from pathlib import Path

import numpy as np

N = 1024
BG, AMBER, BLUE = (0x11, 0x12, 0x14), (0xE0, 0x8A, 0x3C), (0x4A, 0x90, 0xD9)


def track_distance(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Distance to a stadium-shaped loop with a kink, like a short circuit."""
    cx, cy, half, r = 512.0, 512.0, 180.0, 230.0
    px = np.clip(x - cx, -half, half)
    d = np.hypot(x - cx - px, y - cy) - r  # signed distance to the stadium
    return np.abs(d)


def png(rgb: np.ndarray) -> bytes:
    raw = b"".join(b"\0" + row.tobytes() for row in rgb.astype(np.uint8))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    y, x = np.mgrid[0:N, 0:N].astype(float) + 0.5
    img = np.empty((N, N, 3))
    img[:] = BG
    line = np.clip(46.0 - track_distance(x, y), 0, 1)[..., None]  # 92 px stroke, 1 px antialias
    img = img * (1 - line) + np.array(AMBER) * line
    dot = np.clip(70.0 - np.hypot(x - 332.0, y - 282.0), 0, 1)[..., None]
    img = img * (1 - dot) + np.array(BLUE) * dot
    out = Path(__file__).parent / "icon.png"
    out.write_bytes(png(img))
    print(out)


if __name__ == "__main__":
    main()
