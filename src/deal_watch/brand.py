"""CueScope's public identity; legacy storage and process identity stay compatible."""

from __future__ import annotations

from pathlib import Path

from . import __version__

DISPLAY_NAME = "CueScope"
TAGLINE = "窗口文字监测与提醒"
VERSION = __version__
BRAND_COLOR = "#28675D"
_ASSETS = Path(__file__).resolve().parent / "assets" / "brand"


def brand_svg_path() -> Path:
    return _ASSETS / "cuescope.svg"


def brand_icon(size: int = 256, monochrome: bool = False, color=None):
    """Render crisp icons at several densities without depending on a primary display."""
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication is required for brand icon rendering")
    size = max(16, min(int(size), 512))
    source = brand_svg_path().read_text(encoding="utf-8")
    if monochrome or color is not None:
        selected = QColor(color) if color is not None else app.palette().color(QPalette.ColorRole.Text)
        if not selected.isValid():
            raise ValueError("Invalid brand icon color")
        source = source.replace(BRAND_COLOR, selected.name())
    renderer = QSvgRenderer(QByteArray(source.encode("utf-8")))
    if not renderer.isValid():
        raise RuntimeError("CueScope icon resource is invalid")
    result = QIcon()
    for ratio in (1.0, 1.5, 2.0, 3.0):
        pixmap = QPixmap(round(size * ratio), round(size * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        result.addPixmap(pixmap)
    return result
