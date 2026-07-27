"""L5 main window (R3 Surface): a roomy, resizable window for long results /
code / results with artifacts. Opened by the Surface router's L5 decision."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.acrylic import enable_acrylic, glass_opacity, glass_theme_changed, white_acrylic_color
from pillow_assistant.ui.panels.base_panel import _CornerGrip

def _surface_qss(opacity: int) -> str:
    alpha = round(opacity * 2.55)
    panel_alpha = min(225, alpha + 18)
    return f"""
QWidget#surfaceRoot {{ background: rgba(255,255,255,{alpha}); border: 1px solid rgba(255,255,255,190); border-radius: 18px; }}
QLabel {{ color: #18202A; background: transparent; }}
QLabel#sTitle {{ font-size: 14px; font-weight: bold; }}
QPlainTextEdit, QListWidget {{
    background: rgba(255,255,255,{panel_alpha}); color: #18202A;
    selection-background-color: rgba(40,110,220,150);
    border: 1px solid rgba(70,80,95,35); border-radius: 10px;
}}
QPushButton {{ color:#25303D; background: rgba(255,255,255,125); border:1px solid rgba(70,80,95,28); border-radius:9px; padding:5px 10px; }}
QPushButton:hover {{ background: rgba(255,255,255,210); }}
"""


class SurfaceMainWindow(QWidget):
    def __init__(self, body: str, artifacts: list, workspace: str, title: str = "", parent=None) -> None:
        super().__init__(parent)
        self._workspace = workspace
        self._drag_offset = None  # left-drag anywhere on the frame moves the window
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setObjectName("surfaceRoot")
        self._glass_opacity = glass_opacity()
        self.setStyleSheet(_surface_qss(self._glass_opacity))
        glass_theme_changed.opacity_changed.connect(self._apply_glass_opacity)
        self.setMinimumSize(420, 300)
        self.resize(760, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        title_label = QLabel(title or t("surface.title"), self)
        title_label.setObjectName("sTitle")
        header.addWidget(title_label, 1)
        close_btn = QPushButton("×", self)
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn, 0, Qt.AlignTop)
        outer.addLayout(header)

        view = QPlainTextEdit(self)
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
        view.setPlainText(body or t("surface.empty"))
        outer.addWidget(view, 1)

        if artifacts:
            outer.addWidget(QLabel(t("surface.artifacts"), self))
            lst = QListWidget(self)
            lst.addItems([str(a) for a in artifacts])
            lst.setFixedHeight(96)
            outer.addWidget(lst)
            btn = QPushButton(t("surface.open_folder"), self)
            btn.clicked.connect(self._open_workspace)
            outer.addWidget(btn, 0, Qt.AlignLeft)

        self._grip = _CornerGrip(self)
        self._grip.move(self.width() - self._grip.width() - 6, self.height() - self._grip.height() - 6)

    def _apply_glass_opacity(self, opacity: int) -> None:
        self._glass_opacity = max(10, min(95, int(opacity)))
        self.setStyleSheet(_surface_qss(self._glass_opacity))
        if self.isVisible():
            enable_acrylic(self, white_acrylic_color(self._glass_opacity))
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_glass_opacity(glass_opacity())

    def _open_workspace(self) -> None:
        if self._workspace:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._workspace))))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "_grip", None) is not None:
            self._grip.move(self.width() - self._grip.width() - 6, self.height() - self._grip.height() - 6)
            self._grip.raise_()

    # -- left-drag anywhere on the frame moves the window --------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
