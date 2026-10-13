"""Generate the application and tray icons from geometry.

Writes, next to this file:
  stellaris-fan-control.svg       color application icon (window, menu, Windows executable)
  stellaris-fan-control.png       256 px render of it
  stellaris-fan-control.ico       Windows executable icon, 16-256 px, PNG-compressed
  stellaris-fan-control-tray.svg  light monochrome tray icon for the GNOME top bar

Every shape is computed here: blades are bounded by two circular arcs and the hub circle,
the gauge is an arc of the housing circle. Run with the repository's virtual environment:
  ./.venv/bin/python icon/make_icon.py
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "stellaris-fan-control"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def polar(cx: float, cy: float, radius: float, degrees: float) -> tuple[float, float]:
    """Point on a circle; 0 degrees points up and angles grow clockwise."""
    angle = math.radians(degrees)
    return cx + radius * math.sin(angle), cy - radius * math.cos(angle)


def fmt(point: tuple[float, float]) -> str:
    return f"{point[0]:.3f} {point[1]:.3f}"


def fan_blades(cx: float, cy: float, hub: float, tip: float, count: int,
               root_width: float, tip_width: float, sweep: float, bend: float) -> str:
    """Path for `count` swept blades.

    Each blade is bounded by four circular arcs: the hub circle (`root_width` degrees), the
    tip circle (`tip_width` degrees, centred `sweep` degrees ahead of the root), and two side
    edges of radius `bend` * tip that bow the same way, so the blade curves like a fan blade.
    """
    edge = bend * tip
    parts = []
    for index in range(count):
        base = index * 360 / count
        root_trail = polar(cx, cy, hub, base - root_width / 2)
        root_lead = polar(cx, cy, hub, base + root_width / 2)
        tip_lead = polar(cx, cy, tip, base + sweep + tip_width / 2)
        tip_trail = polar(cx, cy, tip, base + sweep - tip_width / 2)
        parts.append(
            f"M {fmt(root_trail)} "
            f"A {hub:.3f} {hub:.3f} 0 0 1 {fmt(root_lead)} "
            f"A {edge:.3f} {edge:.3f} 0 0 0 {fmt(tip_lead)} "
            f"A {tip:.3f} {tip:.3f} 0 0 0 {fmt(tip_trail)} "
            f"A {edge:.3f} {edge:.3f} 0 0 1 {fmt(root_trail)} Z"
        )
    return " ".join(parts)


def arc(cx: float, cy: float, radius: float, start: float, end: float) -> str:
    """Clockwise arc path from `start` to `end` degrees."""
    large = 1 if (end - start) % 360 > 180 else 0
    return (f"M {fmt(polar(cx, cy, radius, start))} "
            f"A {radius:.3f} {radius:.3f} 0 {large} 1 {fmt(polar(cx, cy, radius, end))}")


def app_svg() -> str:
    c = 128
    blades = fan_blades(c, c, hub=24, tip=86, count=5, root_width=40, tip_width=44, sweep=40,
                        bend=0.75)
    gauge = arc(c, c, 106, -120, 120)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <defs>
    <radialGradient id="housing" cx="0.5" cy="0.38" r="0.62">
      <stop offset="0" stop-color="#353b42"/>
      <stop offset="1" stop-color="#1c2025"/>
    </radialGradient>
    <linearGradient id="gauge" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#2ec27e"/>
      <stop offset="0.5" stop-color="#f6d32d"/>
      <stop offset="1" stop-color="#e01b24"/>
    </linearGradient>
    <linearGradient id="blade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#4fe0c0"/>
      <stop offset="1" stop-color="#1fa38a"/>
    </linearGradient>
  </defs>
  <circle cx="{c}" cy="{c}" r="122" fill="url(#housing)"/>
  <circle cx="{c}" cy="{c}" r="118" fill="none" stroke="#4a525b" stroke-width="3"/>
  <path d="{gauge}" fill="none" stroke="#2a2f35" stroke-width="12" stroke-linecap="round"/>
  <path d="{gauge}" fill="none" stroke="url(#gauge)" stroke-width="8" stroke-linecap="round"/>
  <path d="{blades}" fill="url(#blade)"/>
  <circle cx="{c}" cy="{c}" r="22" fill="#262b31" stroke="#4fe0c0" stroke-width="5"/>
  <circle cx="{c}" cy="{c}" r="7" fill="#4fe0c0"/>
</svg>
"""


def tray_svg() -> str:
    # Designed on the 16 px grid of GNOME's symbolic icons; light so it reads on the dark top bar.
    c = 8
    # Blades start 1 px outside the hub so the hub stays a separate dot at 16 px.
    blades = fan_blades(c, c, hub=3.0, tip=7.6, count=5, root_width=34, tip_width=40, sweep=40,
                        bend=0.75)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <path d="{blades}" fill="#f2f2f2"/>
  <circle cx="{c}" cy="{c}" r="1.9" fill="#f2f2f2"/>
</svg>
"""


def render_png(svg: str, size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage, QPainter, Qt
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg.encode()))
    if not renderer.isValid():
        raise ValueError("generated SVG is not valid")
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data.data())


def build_ico(pngs: dict[int, bytes]) -> bytes:
    """ICO container holding one PNG per size (supported since Windows Vista)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = len(header) + 16 * len(pngs)
    entries, images = b"", b""
    for size, png in sorted(pngs.items()):
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset)
        images += png
        offset += len(png)
    return header + entries + images


def main() -> int:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1] + ["-platform", "offscreen"])
    app_icon, tray_icon = app_svg(), tray_svg()
    (HERE / f"{APP_NAME}.svg").write_text(app_icon, encoding="ascii")
    (HERE / f"{APP_NAME}-tray.svg").write_text(tray_icon, encoding="ascii")
    (HERE / f"{APP_NAME}.png").write_bytes(render_png(app_icon, 256))
    (HERE / f"{APP_NAME}.ico").write_bytes(
        build_ico({size: render_png(app_icon, size) for size in ICO_SIZES}))
    for name in (f"{APP_NAME}.svg", f"{APP_NAME}-tray.svg", f"{APP_NAME}.png", f"{APP_NAME}.ico"):
        print(HERE / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
