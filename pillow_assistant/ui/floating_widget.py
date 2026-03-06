from __future__ import annotations

from pathlib import Path
from typing import Optional, cast

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from pillow_assistant.ui.audio_dialog import AudioRecorderDialog
from pillow_assistant.ui.image_preview import ImagePreviewDialog
from pillow_assistant.ui.search_dialog import SearchDialog
from storage import Storage


class FloatingAssistant(QWidget):
    """Floating translucent pillow button exposing quick actions."""

    def __init__(self, storage: Storage, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.storage = storage

        # Keep the window visible even when another app (e.g. Finder) owns the drag.
        flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        self.setWindowFlags(flags)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self._drag_active = False
        self._drag_offset = QPoint()

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._maybe_hide_menu)

        self._build_ui()

    # Pillow drag handling --------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            local_pos = event.position().toPoint()
            if self.pillow_button.geometry().contains(local_pos):
                self._drag_active = True
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_active and event.buttons() & Qt.LeftButton:
            target = event.globalPosition().toPoint() - self._drag_offset
            self.move(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._drag_active:
            self._drag_active = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.pillow_button:
            if event.type() == QEvent.Type.MouseButtonPress:
                mouse_event = cast(QMouseEvent, event)
                if mouse_event.button() == Qt.LeftButton:
                    self._drag_active = True
                    self.hide_timer.stop()
                    self.menu_frame.show()
                    self._drag_offset = mouse_event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    mouse_event.accept()
                    return True
            if event.type() == QEvent.Type.MouseMove:
                mouse_event = cast(QMouseEvent, event)
                if self._drag_active and mouse_event.buttons() & Qt.LeftButton:
                    target = mouse_event.globalPosition().toPoint() - self._drag_offset
                    self.move(target)
                    mouse_event.accept()
                    return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                mouse_event = cast(QMouseEvent, event)
                if mouse_event.button() == Qt.LeftButton and self._drag_active:
                    self._drag_active = False
                    mouse_event.accept()
                    return True
        return super().eventFilter(obj, event)

    def _build_ui(self) -> None:
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(12)

        self.pillow_button = QPushButton(self)
        self.pillow_button.setIcon(QIcon(create_pillow_icon(96)))
        self.pillow_button.setIconSize(QSize(72, 72))
        self.pillow_button.setFixedSize(96, 96)
        self.pillow_button.setFlat(True)
        self.pillow_button.setCursor(Qt.PointingHandCursor)
        self.pillow_button.setStyleSheet(
            """
            QPushButton {
                border: none;
                background: rgba(255, 255, 255, 120);
                border-radius: 38px;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 180);
            }
            """
        )
        main_layout.addWidget(self.pillow_button)
        self.pillow_button.installEventFilter(self)

        self.menu_frame = build_menu_frame(self)
        self.menu_frame.hide()
        main_layout.addWidget(self.menu_frame)
        main_layout.setAlignment(self.menu_frame, Qt.AlignVCenter)

        mic_button = self.menu_frame.findChild(QPushButton, "micButton")
        mic_button.clicked.connect(self._handle_mic_clicked)

        keyboard_button = self.menu_frame.findChild(QPushButton, "keyboardButton")
        keyboard_button.clicked.connect(self._handle_keyboard_clicked)

        close_button = self.menu_frame.findChild(QPushButton, "closeButton")
        if close_button is not None:
            close_button.clicked.connect(self._handle_close_clicked)

    # Hover events -----------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self.menu_frame.show()
        self.hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._drag_active:
            self.hide_timer.start(500)
        super().leaveEvent(event)

    def _maybe_hide_menu(self) -> None:
        if self._drag_active:
            return
        if not (
            self.rect().contains(self.mapFromGlobal(self.cursor().pos()))
            or self.menu_frame.rect().contains(self.menu_frame.mapFromGlobal(self.cursor().pos()))
        ):
            self.menu_frame.hide()

    # Drag & drop ------------------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and is_supported_image(url.toLocalFile()):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            local = url.toLocalFile()
            if is_supported_image(local):
                self._open_image_preview(local)
                break
        event.acceptProposedAction()

    # Action handlers --------------------------------------------------------------
    def _handle_mic_clicked(self) -> None:
        dialog = AudioRecorderDialog(parent=self, storage=self.storage)
        dialog.exec()

    def _handle_keyboard_clicked(self) -> None:
        dialog = SearchDialog(parent=self, storage=self.storage)
        dialog.exec()

    def _open_image_preview(self, image_path: str | Path) -> None:
        dialog = ImagePreviewDialog(image_path=image_path, storage=self.storage, parent=self)
        dialog.exec()

    def _handle_close_clicked(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()


# Helper builders -----------------------------------------------------------------
def build_menu_frame(parent: QWidget) -> QFrame:
    frame = QFrame(parent)
    frame.setObjectName("menuFrame")
    frame.setStyleSheet(
        """
        QFrame#menuFrame {
            background: rgba(30, 30, 30, 160);
            border-radius: 18px;
        }
        QPushButton {
            border: none;
            color: white;
            background: transparent;
        }
        QPushButton:hover {
            background: rgba(255, 255, 255, 40);
        }
        """
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)

    mic_button = QPushButton(frame)
    mic_button.setObjectName("micButton")
    mic_button.setIcon(QIcon(create_microphone_icon(48)))
    mic_button.setIconSize(QSize(48, 48))
    mic_button.setFixedSize(64, 64)
    mic_button.setCursor(Qt.PointingHandCursor)
    mic_button.setToolTip("语音输入")
    layout.addWidget(mic_button)
    layout.setAlignment(mic_button, Qt.AlignCenter)

    keyboard_button = QPushButton(frame)
    keyboard_button.setObjectName("keyboardButton")
    keyboard_button.setIcon(QIcon(create_keyboard_icon(48)))
    keyboard_button.setIconSize(QSize(48, 48))
    keyboard_button.setFixedSize(64, 64)
    keyboard_button.setCursor(Qt.PointingHandCursor)
    keyboard_button.setToolTip("搜索输入")
    layout.addWidget(keyboard_button)
    layout.setAlignment(keyboard_button, Qt.AlignCenter)

    close_button = QPushButton(frame)
    close_button.setObjectName("closeButton")
    close_button.setIcon(QIcon(create_close_icon(42)))
    close_button.setIconSize(QSize(42, 42))
    close_button.setFixedSize(56, 56)
    close_button.setCursor(Qt.PointingHandCursor)
    close_button.setToolTip("关闭助手")
    layout.addWidget(close_button)
    layout.setAlignment(close_button, Qt.AlignCenter)

    return frame


def create_pillow_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    rect = pixmap.rect().adjusted(int(size * 0.1), int(size * 0.1), -int(size * 0.1), -int(size * 0.1))
    radius = size * 0.4

    # Soft drop shadow for depth.
    shadow_rect = rect.translated(0, int(size * 0.06))
    shadow_path = QPainterPath()
    shadow_path.addRoundedRect(shadow_rect, radius, radius)
    shadow_gradient = QRadialGradient(QPointF(shadow_rect.center()), radius * 1.2)
    shadow_gradient.setColorAt(0.0, QColor(40, 60, 90, 80))
    shadow_gradient.setColorAt(1.0, QColor(40, 60, 90, 0))
    painter.setPen(Qt.NoPen)
    painter.setBrush(shadow_gradient)
    painter.drawPath(shadow_path)

    # Main pillow body with gentle gradient.
    body_path = QPainterPath()
    body_path.addRoundedRect(rect, radius, radius)
    body_gradient = QLinearGradient(QPointF(rect.topLeft()), QPointF(rect.bottomRight()))
    body_gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
    body_gradient.setColorAt(1.0, QColor(192, 215, 240, 235))
    painter.setBrush(body_gradient)
    painter.drawPath(body_path)

    # Highlight across the top half.
    highlight_rect = rect.adjusted(int(size * 0.14), int(size * 0.14), -int(size * 0.14), -int(size * 0.42))
    if highlight_rect.height() > 0:
        highlight_path = QPainterPath()
        highlight_path.addRoundedRect(highlight_rect, radius * 0.6, radius * 0.4)
        highlight_gradient = QLinearGradient(
            QPointF(highlight_rect.topLeft()), QPointF(highlight_rect.bottomLeft())
        )
        highlight_gradient.setColorAt(0.0, QColor(255, 255, 255, 200))
        highlight_gradient.setColorAt(1.0, QColor(255, 255, 255, 40))
        painter.setBrush(highlight_gradient)
        painter.drawPath(highlight_path)

    # Subtle stitched seam near the edge.
    seam_rect = rect.adjusted(int(size * 0.12), int(size * 0.12), -int(size * 0.12), -int(size * 0.12))
    seam_pen = QPen(QColor(210, 230, 245, 180))
    seam_pen.setWidthF(max(1.0, size * 0.02))
    seam_pen.setStyle(Qt.DotLine)
    painter.setPen(seam_pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(seam_rect, radius * 0.55, radius * 0.55)

    # Body border for crisp edges.
    border_pen = QPen(QColor(148, 184, 214, 210))
    border_pen.setWidthF(max(1.2, size * 0.035))
    painter.setPen(border_pen)
    painter.drawPath(body_path)

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

    stem_top = QPoint(center.x(), int(size * 0.7))
    painter.setPen(QColor(255, 255, 255, 220))
    painter.drawLine(stem_top, QPoint(center.x(), int(size * 0.9)))

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

    base_rect = pixmap.rect().adjusted(int(size * 0.08), int(size * 0.28), -int(size * 0.08), -int(size * 0.2))
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

    # Key layout
    key_radius = size * 0.08
    key_w = size * 0.15
    key_h = size * 0.17
    spacing_x = size * 0.06
    spacing_y = size * 0.08
    start_x = base_rect.left() + size * 0.12
    start_y = base_rect.top() + size * 0.12

    key_border = QPen(QColor(120, 140, 170, 220))
    key_border.setWidthF(max(0.9, size * 0.018))

    for row in range(2):
        for col in range(4):
            x = start_x + col * (key_w + spacing_x)
            y = start_y + row * (key_h + spacing_y)
            key_rect = QRectF(x, y, key_w, key_h)
            key_path = QPainterPath()
            key_path.addRoundedRect(key_rect, key_radius, key_radius)

            key_gradient = QLinearGradient(QPointF(key_rect.topLeft()), QPointF(key_rect.bottomLeft()))
            key_gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
            key_gradient.setColorAt(1.0, QColor(205, 212, 228, 255))

            painter.setPen(Qt.NoPen)
            painter.setBrush(key_gradient)
            painter.drawPath(key_path)

            painter.setPen(key_border)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(key_path)

    # Space bar with accent.
    space_rect = QRectF(
        base_rect.left() + size * 0.22,
        base_rect.bottom() - key_h - size * 0.2,
        base_rect.width() - size * 0.44,
        key_h * 0.85,
    )
    space_path = QPainterPath()
    space_path.addRoundedRect(space_rect, key_radius, key_radius)
    space_gradient = QLinearGradient(QPointF(space_rect.topLeft()), QPointF(space_rect.bottomLeft()))
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

    rect = pixmap.rect().adjusted(int(size * 0.08), int(size * 0.08), -int(size * 0.08), -int(size * 0.08))
    radius = rect.width() / 2
    center = QPointF(rect.center())

    # Soft dual-tone background.
    bg_gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    bg_gradient.setColorAt(0.0, QColor(255, 140, 150, 245))
    bg_gradient.setColorAt(1.0, QColor(220, 70, 100, 255))

    painter.setPen(Qt.NoPen)
    painter.setBrush(bg_gradient)
    painter.drawEllipse(rect)

    # Inner glow ring.
    ring_pen = QPen(QColor(255, 255, 255, 120))
    ring_pen.setWidthF(max(1.2, size * 0.04))
    painter.setPen(ring_pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(rect.adjusted(int(size * 0.05), int(size * 0.05), -int(size * 0.05), -int(size * 0.05)))

    # Cross mark.
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
    suffix = Path(path).suffix.lower()
    return suffix in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
