"""Project browser (R1+), opened on demand from the radial menu's 「项目」 entry.

Lists projects; selecting one shows its conversation history and offers to open
its workspace folder (where artifacts live) in the OS file manager.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices

from pillow_assistant.core.i18n import t
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

PANEL_QSS = """
QWidget#projectsRoot { background: rgba(18, 22, 28, 235); border: 1px solid rgba(255,255,255,45); border-radius: 14px; }
QLabel { color: #F2F6FA; background: transparent; }
QLabel#projTitle { font-size: 15px; font-weight: bold; }
QListWidget, QPlainTextEdit {
    background: rgba(10, 14, 20, 235); color: #F4F8FC;
    border: 1px solid rgba(255,255,255,40); border-radius: 8px;
}
QPushButton { color:#FFFFFF; background: rgba(255,255,255,35); border:none; border-radius:8px; padding:6px 10px; }
QPushButton:hover { background: rgba(90,140,200,235); }
"""


class ProjectsPanel(QWidget):
    def __init__(self, store, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self._armed = False  # auto-close on deactivate, enabled shortly after show
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setObjectName("projectsRoot")
        self.setStyleSheet(PANEL_QSS)
        self.resize(720, 480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel(t("projects.title"), self)
        title.setObjectName("projTitle")
        header.addWidget(title, 1)
        close_btn = QPushButton("×", self)
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn, 0, Qt.AlignTop)
        outer.addLayout(header)

        body = QHBoxLayout()
        self.list = QListWidget(self)
        self.list.setFixedWidth(240)
        self.list.itemSelectionChanged.connect(self._on_select)
        body.addWidget(self.list)

        right = QVBoxLayout()
        self.history_view = QPlainTextEdit(self)
        self.history_view.setReadOnly(True)
        right.addWidget(self.history_view, 1)
        self.open_btn = QPushButton(t("surface.open_folder"), self)
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_workspace)
        right.addWidget(self.open_btn, 0, Qt.AlignLeft)
        body.addLayout(right, 1)
        outer.addLayout(body, 1)

        self._projects = self.store.list()
        self._chat_path = Path.home() / ".pillow" / "chat" / "history.jsonl"
        self._has_chat = self._chat_path.exists()
        if self._has_chat:
            when = time.strftime("%m-%d %H:%M", time.localtime(self._chat_path.stat().st_mtime))
            QListWidgetItem(f"{t('projects.chat_entry')}\n{when}", self.list)
        for p in self._projects:
            when = time.strftime("%m-%d %H:%M", time.localtime(p.updated_at))
            QListWidgetItem(f"{p.name}\n{when}", self.list)
        if not self._projects and not self._has_chat:
            self.history_view.setPlainText(t("projects.empty"))
        else:
            self.list.setCurrentRow(0)

    @property
    def _current(self):
        """Selected project, or None (nothing / the chat entry is selected)."""
        row = self.list.currentRow()
        if self._has_chat:
            row -= 1  # row 0 is the chat pseudo-entry
        return self._projects[row] if 0 <= row < len(self._projects) else None

    def _chat_selected(self) -> bool:
        return self._has_chat and self.list.currentRow() == 0

    def _on_select(self) -> None:
        if self._chat_selected():
            self.open_btn.setEnabled(True)
            self._show_chat()
            return
        project = self._current
        if project is None:
            return
        self.open_btn.setEnabled(True)
        turns = self.store.load_history(project, max_turns=200)
        if not turns:
            self.history_view.setPlainText(
                t("projects.project", name=project.name) + "\n" + t("projects.no_history"))
            return
        lines = [t("projects.project", name=project.name),
                 t("projects.dir", path=project.root), ""]
        for turn in turns:
            who = t("role.user") if turn["role"] == "user" else t("role.assistant")
            lines.append(f"【{who}】{turn['content']}")
        self.history_view.setPlainText("\n".join(lines))

    def _show_chat(self) -> None:
        lines = [t("projects.chat_header"), t("projects.dir", path=self._chat_path.parent), ""]
        try:
            for raw in self._chat_path.read_text("utf-8").splitlines()[-400:]:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                who = t("role.user") if obj.get("role") == "user" else t("role.assistant")
                lines.append(f"【{who}】{obj.get('content', '')}")
        except (OSError, ValueError):
            lines.append(t("projects.no_chat"))
        self.history_view.setPlainText("\n".join(lines))

    def _open_workspace(self) -> None:
        if self._chat_selected():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._chat_path.parent / "workspace")))
            return
        project = self._current
        if project is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(project.workspace)))

    # -- auto-dismiss -------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        # Arm a moment later so the activation that happens during show()
        # doesn't immediately close the panel.
        QTimer.singleShot(350, lambda: setattr(self, "_armed", True))

    def changeEvent(self, event) -> None:  # noqa: N802
        # Close when the user clicks another window / app (lost activation).
        if event.type() == QEvent.ActivationChange and self._armed and not self.isActiveWindow():
            self.close()
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
