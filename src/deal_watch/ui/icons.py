"""Locally authored SVG line icons; no downloads, emoji, or icon font."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

_DRAWINGS = {
    "monitor": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4M6 11h3l2-4 3 7 2-3h2"/>',
    "keywords": '<path d="M3 4h7l11 11-6 6L3 9z"/><circle cx="7" cy="7" r="1"/>',
    "records": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 7h6M9 11h6M9 15h4"/>',
    "settings": '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    "add": '<path d="M12 5v14M5 12h14"/>',
    "more": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    "preview": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "chevron": '<path d="m9 5 7 7-7 7"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4M12 17h.01"/>',
    "close": '<path d="m6 6 12 12M6 18 18 6"/>',
    "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "play": '<path d="m8 4 12 8-12 8z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "moon": '<path d="M20.5 13A8.6 8.6 0 0 1 11 3.5 8.6 8.6 0 1 0 20.5 13z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    "system": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
}
_CACHE: dict[tuple, QIcon] = {}


def icon(name: str, color: str | QColor | None = None, size: int = 20) -> QIcon:
    if name not in _DRAWINGS:
        raise ValueError(f"Unknown icon: {name}")
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication is required for icon rendering")
    chosen = QColor(color) if color is not None else app.palette().color(QPalette.ColorRole.Text)
    size = max(12, min(int(size), 128))
    key = (name, chosen.name(QColor.NameFormat.HexArgb), size)
    if key not in _CACHE:
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
               f'fill="none" stroke="{chosen.name()}" stroke-width="1.7" stroke-linecap="round" '
               f'stroke-linejoin="round">{_DRAWINGS[name]}</svg>')
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        result = QIcon()
        # Cache several pixel densities, independent of whichever monitor Qt calls
        # primary. Moving the app onto DISPLAY1 must not upscale a 1x-only icon.
        for ratio in (1.0, 1.5, 2.0, 3.0):
            pixmap = QPixmap(round(size * ratio), round(size * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter, QRectF(0, 0, size, size))
            painter.end()
            result.addPixmap(pixmap)
        _CACHE[key] = result
        if len(_CACHE) > 256:
            oldest = next(iter(_CACHE))
            if oldest != key:
                _CACHE.pop(oldest)
    return QIcon(_CACHE[key])
