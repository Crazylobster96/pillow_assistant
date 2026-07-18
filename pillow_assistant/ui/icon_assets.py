"""Programmatically drawn icon assets for the floating assistant."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)


def create_pillow_icon(size: int) -> QPixmap:
    """Draw a soft plush pillow with a gentle cool gradient."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)

    rect = QRectF(size * 0.13, size * 0.15, size * 0.74, size * 0.70)
    radius = size * 0.30
    center = rect.center()

    glow = QRadialGradient(center, size * 0.52)
    glow.setColorAt(0.0, QColor(125, 155, 205, 70))
    glow.setColorAt(1.0, QColor(125, 155, 205, 0))
    painter.setBrush(glow)
    painter.drawEllipse(QRectF(0, 0, size, size))

    shadow = QPainterPath()
    shadow.addRoundedRect(rect.translated(0, size * 0.045), radius, radius)
    shadow_gradient = QRadialGradient(QPointF(center.x(), center.y() + size * 0.05), radius * 1.4)
    shadow_gradient.setColorAt(0.0, QColor(30, 50, 80, 95))
    shadow_gradient.setColorAt(1.0, QColor(30, 50, 80, 0))
    painter.setBrush(shadow_gradient)
    painter.drawPath(shadow)

    body = QPainterPath()
    body.addRoundedRect(rect, radius, radius)
    body_gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    body_gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
    body_gradient.setColorAt(0.55, QColor(228, 238, 250, 252))
    body_gradient.setColorAt(1.0, QColor(186, 208, 238, 246))
    painter.setBrush(body_gradient)
    painter.drawPath(body)

    gloss = rect.adjusted(size * 0.06, size * 0.06, -size * 0.06, -size * 0.40)
    if gloss.height() > 0:
        gloss_path = QPainterPath()
        gloss_path.addRoundedRect(gloss, radius * 0.7, radius * 0.5)
        gloss_gradient = QLinearGradient(gloss.topLeft(), gloss.bottomLeft())
        gloss_gradient.setColorAt(0.0, QColor(255, 255, 255, 215))
        gloss_gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(gloss_gradient)
        painter.drawPath(gloss_path)

    seam = rect.adjusted(size * 0.095, size * 0.095, -size * 0.095, -size * 0.095)
    seam_pen = QPen(QColor(150, 182, 222, 150))
    seam_pen.setWidthF(max(1.0, size * 0.016))
    seam_pen.setStyle(Qt.DotLine)
    painter.setPen(seam_pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(seam, radius * 0.6, radius * 0.6)

    border_pen = QPen(QColor(150, 182, 216, 200))
    border_pen.setWidthF(max(1.0, size * 0.026))
    painter.setPen(border_pen)
    painter.drawPath(body)

    painter.setPen(QColor(96, 128, 190, 240))
    for fx, fy, font_scale, char in (
        (0.34, 0.55, 0.15, "z"),
        (0.47, 0.47, 0.20, "z"),
        (0.60, 0.38, 0.27, "Z"),
    ):
        font = QFont("Segoe UI", max(6, int(size * font_scale)))
        font.setBold(True)
        font.setItalic(True)
        painter.setFont(font)
        painter.drawText(QPointF(size * fx, size * fy), char)

    painter.end()
    return pixmap


def create_microphone_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    center = pixmap.rect().center()

    body_rect = pixmap.rect().adjusted(size * 0.35, size * 0.2, -size * 0.35, -size * 0.2)
    path = QPainterPath()
    path.addRoundedRect(body_rect, size * 0.2, size * 0.2)
    painter.fillPath(path, QColor(255, 255, 255, 220))
    painter.setPen(QColor(255, 255, 255, 220))
    painter.drawLine(QPoint(center.x(), int(size * 0.7)), QPoint(center.x(), int(size * 0.9)))

    base_rect = pixmap.rect().adjusted(size * 0.35, int(size * 0.88), -size * 0.35, -int(size * 0.05))
    base_path = QPainterPath()
    base_path.addRoundedRect(base_rect, size * 0.1, size * 0.1)
    painter.fillPath(base_path, QColor(255, 255, 255, 220))
    painter.end()
    return pixmap


def create_keyboard_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    base_rect = pixmap.rect().adjusted(
        int(size * 0.08), int(size * 0.28), -int(size * 0.08), -int(size * 0.2)
    )
    base_radius = size * 0.18
    base_path = QPainterPath()
    base_path.addRoundedRect(base_rect, base_radius, base_radius)

    base_gradient = QLinearGradient(QPointF(base_rect.topLeft()), QPointF(base_rect.bottomLeft()))
    base_gradient.setColorAt(0.0, QColor(235, 240, 247, 245))
    base_gradient.setColorAt(1.0, QColor(194, 206, 224, 245))
    painter.setPen(Qt.NoPen)
    painter.setBrush(base_gradient)
    painter.drawPath(base_path)

    top_glow = QLinearGradient(QPointF(base_rect.topLeft()), QPointF(base_rect.bottomLeft()))
    top_glow.setColorAt(0.0, QColor(255, 255, 255, 160))
    top_glow.setColorAt(0.6, QColor(255, 255, 255, 0))
    painter.setBrush(top_glow)
    painter.drawPath(base_path)

    base_border = QPen(QColor(122, 142, 170, 220))
    base_border.setWidthF(max(1.0, size * 0.025))
    painter.setPen(base_border)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(base_path)

    key_radius = size * 0.08
    key_width = size * 0.15
    key_height = size * 0.17
    spacing_x = size * 0.06
    spacing_y = size * 0.08
    start_x = base_rect.left() + size * 0.12
    start_y = base_rect.top() + size * 0.12
    key_border = QPen(QColor(120, 140, 170, 220))
    key_border.setWidthF(max(0.9, size * 0.018))

    for row in range(2):
        for col in range(4):
            key_rect = QRectF(
                start_x + col * (key_width + spacing_x),
                start_y + row * (key_height + spacing_y),
                key_width,
                key_height,
            )
            key_path = QPainterPath()
            key_path.addRoundedRect(key_rect, key_radius, key_radius)
            key_gradient = QLinearGradient(
                QPointF(key_rect.topLeft()), QPointF(key_rect.bottomLeft())
            )
            key_gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
            key_gradient.setColorAt(1.0, QColor(205, 212, 228, 255))
            painter.setPen(Qt.NoPen)
            painter.setBrush(key_gradient)
            painter.drawPath(key_path)
            painter.setPen(key_border)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(key_path)

    space_rect = QRectF(
        base_rect.left() + size * 0.22,
        base_rect.bottom() - key_height - size * 0.2,
        base_rect.width() - size * 0.44,
        key_height * 0.85,
    )
    space_path = QPainterPath()
    space_path.addRoundedRect(space_rect, key_radius, key_radius)
    space_gradient = QLinearGradient(
        QPointF(space_rect.topLeft()), QPointF(space_rect.bottomLeft())
    )
    space_gradient.setColorAt(0.0, QColor(140, 170, 210, 255))
    space_gradient.setColorAt(1.0, QColor(90, 130, 190, 255))
    painter.setPen(Qt.NoPen)
    painter.setBrush(space_gradient)
    painter.drawPath(space_path)

    space_border = QPen(QColor(70, 100, 150, 230))
    space_border.setWidthF(max(1.0, size * 0.02))
    painter.setPen(space_border)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(space_path)
    painter.end()
    return pixmap


def create_close_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    rect = pixmap.rect().adjusted(
        int(size * 0.08), int(size * 0.08), -int(size * 0.08), -int(size * 0.08)
    )
    radius = rect.width() / 2
    center = QPointF(rect.center())
    background = QLinearGradient(rect.topLeft(), rect.bottomRight())
    background.setColorAt(0.0, QColor(255, 140, 150, 245))
    background.setColorAt(1.0, QColor(220, 70, 100, 255))
    painter.setPen(Qt.NoPen)
    painter.setBrush(background)
    painter.drawEllipse(rect)

    ring_pen = QPen(QColor(255, 255, 255, 120))
    ring_pen.setWidthF(max(1.2, size * 0.04))
    painter.setPen(ring_pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(
        rect.adjusted(
            int(size * 0.05), int(size * 0.05), -int(size * 0.05), -int(size * 0.05)
        )
    )

    cross_pen = QPen(QColor(255, 255, 255, 240))
    cross_pen.setWidthF(max(2.2, size * 0.14))
    cross_pen.setCapStyle(Qt.RoundCap)
    painter.setPen(cross_pen)
    offset = radius * 0.55
    painter.drawLine(
        QPointF(center.x() - offset, center.y() - offset),
        QPointF(center.x() + offset, center.y() + offset),
    )
    painter.drawLine(
        QPointF(center.x() - offset, center.y() + offset),
        QPointF(center.x() + offset, center.y() - offset),
    )
    painter.end()
    return pixmap


def is_supported_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
