"""Right-click radial (fan) menu around the pillow icon — custom-painted.

Custom paint (not QPushButtons) gives full control of the frosted-glass look and
avoids platform styling quirks. ``setMask`` restricts input to the button discs
(plus a small padding) so clicks elsewhere pass through to the icon behind, and
drag-while-open keeps working. A soft shadow + gradient + top gloss are painted
inside the padding so the mask's hard edge falls in fully-transparent pixels
(no jaggies). A light timer (Windows) closes the menu on an off-target click.
"""

from __future__ import annotations

import math
import sys
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
    QRegion,
)
from PySide6.QtWidgets import QWidget


class RadialMenu(QWidget):
    RADIUS = 90    # icon-centre -> button-centre
    BTN = 50       # visible pebble diameter
    PAD = 8        # extra room (in mask) for shadow / soft edge

    def __init__(self, items: list[tuple[str, Callable[[], None]]], center_global: QPoint,
                 avoid_rect=None, exclude_rect_getter: Optional[Callable[[], object]] = None,
                 make_room: Optional[Callable[[], object]] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        self._exclude_rect_getter = exclude_rect_getter
        self._items = items
        self._hover = -1
        self._hit = self.BTN / 2 + self.PAD

        cx, cy = center_global.x(), center_global.y()
        if avoid_rect is not None:
            ac = avoid_rect.center()
            base = math.atan2(cy - ac.y(), cx - ac.x())
        else:
            scr = QGuiApplication.screenAt(center_global) or QGuiApplication.primaryScreen()
            geo = scr.availableGeometry() if scr else None
            base = math.atan2(geo.center().y() - cy, geo.center().x() - cx) if geo else 0.0

        n = max(1, len(items))
        radius, spread = self._layout(n)

        # The base angle only points the fan away from avoid_rect's centre — with
        # many buttons individual discs can still land on it. Verify every disc:
        # rotate the fan (±15°…±90°) to find a clear spot; if no rotation works,
        # ask the caller to reposition the input bar (make_room) and retry; only
        # then fall back to pushing the radius out.
        if avoid_rect is not None:
            margin = int(self.BTN / 2 + self.PAD + 4)

            def fan_overlaps(b: float, r: float, rect) -> bool:
                stp = spread / (n - 1) if n > 1 else 0.0
                for i in range(n):
                    a = b if n == 1 else (b - spread / 2) + stp * i
                    gx, gy = cx + r * math.cos(a), cy + r * math.sin(a)
                    if rect.contains(QPoint(int(gx), int(gy))):
                        return True
                return False

            def try_rotations(b: float, r: float, rect):
                if not fan_overlaps(b, r, rect):
                    return b, True
                for deg in (15, -15, 30, -30, 45, -45, 60, -60, 75, -75, 90, -90):
                    cand = b + math.radians(deg)
                    if not fan_overlaps(cand, r, rect):
                        return cand, True
                return b, False

            inflated = avoid_rect.adjusted(-margin, -margin, margin, margin)
            base, ok = try_rotations(base, radius, inflated)
            if not ok and make_room is not None:
                moved = make_room()  # caller moves the bar; returns its new rect
                if moved is not None:
                    inflated = moved.adjusted(-margin, -margin, margin, margin)
                    base, ok = try_rotations(base, radius, inflated)
            if not ok:
                while radius < 300 and fan_overlaps(base, radius, inflated):
                    radius += 24.0

        self._side = (int(radius) + self.BTN) * 2
        self.resize(self._side, self._side)

        start = base - spread / 2
        step = spread / (n - 1) if n > 1 else 0.0
        c = self._side / 2
        self._centers: list[tuple[float, float]] = []
        for i in range(n):
            angle = base if n == 1 else start + step * i
            self._centers.append((c + radius * math.cos(angle), c + radius * math.sin(angle)))

        region = QRegion()
        for (bx, by) in self._centers:
            r = int(self._hit)
            region += QRegion(int(bx - r), int(by - r), 2 * r, 2 * r, QRegion.Ellipse)
        self.setMask(region)

        self._prev_down = True
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(60)
        self._watch_timer.timeout.connect(self._watch_outside_click)

        self.reposition(center_global)

    def _layout(self, n: int) -> tuple[float, float]:
        """Collision-free fan geometry: pick an angular step large enough that
        adjacent button discs don't overlap at the radius; if the resulting fan
        would wrap too far, grow the radius until it fits.

        Returns ``(radius, total_spread_radians)``.
        """
        if n <= 1:
            return float(self.RADIUS), 0.0
        gap = 12.0  # breathing room beyond each disc's full (shadow) footprint
        required = self.BTN + 2 * self.PAD + gap  # centre-to-centre chord needed
        max_total = math.radians(300)  # leave a gap so the fan never fully wraps

        def min_step(r: float) -> float:
            return 2.0 * math.asin(min(1.0, required / (2.0 * r)))

        radius = float(self.RADIUS)
        step = max(math.radians(44), min_step(radius))
        total = step * (n - 1)
        while total > max_total and radius < 260:
            radius += 12.0
            step = max(math.radians(44), min_step(radius))
            total = step * (n - 1)
        return radius, total

    def reposition(self, center_global: QPoint) -> None:
        side = self._side
        top_left = QPoint(center_global.x() - side // 2, center_global.y() - side // 2)
        scr = QGuiApplication.screenAt(center_global) or QGuiApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            x = max(g.left(), min(top_left.x(), g.right() - side))
            y = max(g.top(), min(top_left.y(), g.bottom() - side))
            top_left = QPoint(x, y)
        self.move(top_left)

    # -- painting -----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(11)
        r = self.BTN / 2

        for i, (bx, by) in enumerate(self._centers):
            hover = i == self._hover

            # Soft drop shadow.
            shadow = QRadialGradient(bx, by + 3, r + self.PAD)
            shadow.setColorAt(0.6, QColor(30, 45, 70, 80))
            shadow.setColorAt(1.0, QColor(30, 45, 70, 0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(shadow)
            painter.drawEllipse(QPointF(bx, by + 2), r + self.PAD - 1, r + self.PAD - 1)

            # Glass body with a vertical gradient.
            rect = QRectF(bx - r, by - r, 2 * r, 2 * r)
            body = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            if hover:
                body.setColorAt(0.0, QColor(150, 188, 244, 215))
                body.setColorAt(1.0, QColor(74, 124, 200, 220))
            else:
                body.setColorAt(0.0, QColor(255, 255, 255, 165))
                body.setColorAt(1.0, QColor(212, 224, 240, 140))
            painter.setBrush(body)
            painter.setPen(QPen(QColor(255, 255, 255, 200), 1.2))
            painter.drawEllipse(rect)

            # Top gloss highlight.
            gloss = QRectF(bx - r * 0.72, by - r * 0.86, r * 1.44, r * 0.95)
            gg = QLinearGradient(gloss.topLeft(), gloss.bottomLeft())
            gg.setColorAt(0.0, QColor(255, 255, 255, 150 if hover else 175))
            gg.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(gg)
            painter.drawEllipse(gloss)

            # Label.
            painter.setPen(QColor(255, 255, 255) if hover else QColor(38, 52, 74))
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, self._items[i][0])

        painter.end()

    # -- interaction --------------------------------------------------------
    def _index_at_local(self, pos) -> int:
        for i, (bx, by) in enumerate(self._centers):
            if math.hypot(pos.x() - bx, pos.y() - by) <= self._hit:
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        idx = self._index_at_local(event.position())
        if idx != self._hover:
            self._hover = idx
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        idx = self._index_at_local(event.position())
        if idx >= 0:
            callback = self._items[idx][1]
            self.close()
            callback()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    # -- outside-click dismissal -------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if sys.platform == "win32":
            self._watch_timer.start()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._watch_timer.stop()
        super().closeEvent(event)

    def _watch_outside_click(self) -> None:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            down = bool(user32.GetAsyncKeyState(0x01) & 0x8000) or bool(
                user32.GetAsyncKeyState(0x02) & 0x8000
            )
            if down and not self._prev_down:
                p = QCursor.pos()
                o = self.pos()
                on_button = any(
                    math.hypot(p.x() - (o.x() + bx), p.y() - (o.y() + by)) <= self._hit
                    for (bx, by) in self._centers
                )
                excl = self._exclude_rect_getter() if self._exclude_rect_getter else None
                on_icon = excl.contains(p) if excl is not None else False
                if not on_button and not on_icon:
                    self.close()
                    return
            self._prev_down = down
        except Exception:
            self._watch_timer.stop()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
