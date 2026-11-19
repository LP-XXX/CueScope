"""Rebuild CueScope PNG/ICO resources locally from the authored vector master."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> None:
    from PIL import Image
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication([])
    assets = Path(__file__).resolve().parents[1] / "src/deal_watch/assets/brand"
    renderer = QSvgRenderer(str(assets / "cuescope.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid CueScope SVG")
    png = assets / "cuescope.png"
    canvas = QImage(512, 512, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(0)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, 512, 512))
    painter.end()
    if not canvas.save(str(png)):
        raise RuntimeError("PNG export failed")
    with Image.open(png) as image:
        image.save(assets / "cuescope.ico", sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print(f"Rendered {png.name} and cuescope.ico")
    del app


if __name__ == "__main__":
    main()
