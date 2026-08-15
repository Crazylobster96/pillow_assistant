"""Project browser (R1+), opened on demand from the radial menu's 「项目」 entry.

Lists projects as cards (name + time + last-prompt preview); selecting one shows
its conversation history. Clicking "switch" (or double-clicking a card) binds the
current conversation to that project so the next message continues it. Also opens
the project's workspace folder.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.acrylic import (
    disable_acrylic, enable_acrylic, glass_opacity, glass_theme_changed, white_acrylic_color,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


_CONFIRM_QSS = """
QDialog#confirmRoot { background: #F7F9FC; border: 1px solid rgba(255,255,255,55); border-radius: 12px; }
QLabel { color: #18202A; background: transparent; font-size: 13px; }
QLabel#cTitle { font-size: 14px; font-weight: bold; color: #18202A; }
QPushButton { color:#25303D; background: rgba(70,80,95,18); border:none; border-radius:8px; padding:8px 18px; }
QPushButton:hover { background: rgba(255,255,255,52); }
QPushButton#danger { background: rgba(214,69,90,235); font-weight:bold; }
QPushButton#danger:hover { background: rgba(232,86,108,245); }
"""


class _ConfirmDialog(QDialog):
    """Self-styled, reliably-clickable confirm (QMessageBox inherits the
    frameless stays-on-top panel's stylesheet and ends up unclickable)."""

    def __init__(self, title: str, message: str, ok_text: str, cancel_text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowModality(Qt.ApplicationModal)
        self.setObjectName("confirmRoot")
        self.setStyleSheet(_CONFIRM_QSS)
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(QLabel(title, self, objectName="cTitle"))
        msg = QLabel(message, self)
        msg.setWordWrap(True)
        lay.addWidget(msg)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(cancel_text, self)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton(ok_text, self, objectName="danger")
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addLayout(row)
        cancel.setFocus()

def panel_qss(opacity: int) -> str:
    alpha = round(opacity * 2.55)
    control_alpha = 0 if alpha == 0 else min(255, alpha + 18)
    return f"""
QWidget#projectsRoot {{
    background: rgba(255,255,255,{alpha});
    border: 1px solid rgba(255,255,255,195); border-radius: 18px;
}}
QLabel {{ color: #18202A; background: transparent; }}
QLabel#projTitle {{ font-size: 17px; font-weight: bold; color: #18202A; }}
QLabel#projSubtitle {{ font-size: 12px; color: #667282; }}
QLabel#histHeader {{ font-size: 13px; font-weight: bold; color: #37699E; }}
QListWidget {{
    background: rgba(255,255,255,{control_alpha}); color: #18202A;
    border: 1px solid rgba(70,80,95,38); border-radius: 12px;
    padding: 6px; outline: 0;
}}
QListWidget::item {{ border-radius: 10px; padding: 9px 10px; margin: 2px 1px; color: #18202A; }}
QListWidget::item:hover {{ background: rgba(80,120,180,28); }}
QListWidget::item:selected {{ background: rgba(74,124,200,205); color: #FFFFFF; }}
QPlainTextEdit {{
    background: rgba(255,255,255,{control_alpha}); color: #18202A;
    border: 1px solid rgba(70,80,95,38); border-radius: 12px; padding: 10px;
    selection-background-color: rgba(55,120,220,150);
}}
QPushButton {{ color:#25303D; background: rgba(255,255,255,125); border:none; border-radius:9px; padding:8px 14px; }}
QPushButton:hover {{ background: rgba(255,255,255,220); }}
QPushButton#switchBtn {{ background: rgba(74,124,200,220); color:#FFFFFF; font-weight: bold; }}
QPushButton#switchBtn:hover {{ background: rgba(94,148,224,235); }}
QPushButton#switchBtn:disabled {{ background: rgba(70,80,95,20); color: #7B8591; }}
QPushButton#closeBtn {{ background: rgba(255,255,255,125); border-radius:14px; font-size:16px; }}
QPushButton#closeBtn:hover {{ background: rgba(235,90,110,210); color:#FFFFFF; }}
QPushButton#deleteBtn:hover {{ background: rgba(214,69,90,210); color:#FFFFFF; }}
QPushButton#deleteBtn:disabled {{ background: rgba(70,80,95,15); color: #7B8591; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(70,80,95,70); border-radius: 4px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


class ProjectsPanel(QWidget):
    def __init__(self, store, on_switch: Optional[Callable] = None,
                 current_project_id: Optional[str] = None,
                 on_delete: Optional[Callable] = None, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self._on_switch = on_switch
        self._on_delete = on_delete
        self._current_pid = current_project_id
        self._armed = False  # auto-close on deactivate, enabled shortly after show
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setObjectName("projectsRoot")
        self._glass_opacity = glass_opacity()
        self.setStyleSheet(panel_qss(self._glass_opacity))
        glass_theme_changed.opacity_changed.connect(self._apply_glass_opacity)
        self.resize(760, 500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)

        # Header: title + count, subtitle, close button.
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.title = QLabel(t("projects.title"), self)
        self.title.setObjectName("projTitle")
        title_box.addWidget(self.title)
        subtitle = QLabel(t("projects.subtitle"), self)
        subtitle.setObjectName("projSubtitle")
        subtitle.setWordWrap(True)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        close_btn = QPushButton("×", self)
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn, 0, Qt.AlignTop)
        outer.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.list = QListWidget(self)
        self.list.setFixedWidth(280)
        self.list.itemSelectionChanged.connect(self._on_select)
        self.list.itemDoubleClicked.connect(lambda *_: self._do_switch())
        body.addWidget(self.list)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.hist_header = QLabel("", self)
        self.hist_header.setObjectName("histHeader")
        right.addWidget(self.hist_header)
        self.history_view = QPlainTextEdit(self)
        self.history_view.setReadOnly(True)
        right.addWidget(self.history_view, 1)
        btn_row = QHBoxLayout()
        self.switch_btn = QPushButton(t("projects.switch"), self)
        self.switch_btn.setObjectName("switchBtn")
        self.switch_btn.setEnabled(False)
        self.switch_btn.clicked.connect(self._do_switch)
        btn_row.addWidget(self.switch_btn)
        self.open_btn = QPushButton(t("surface.open_folder"), self)
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_workspace)
        btn_row.addWidget(self.open_btn)
        btn_row.addStretch(1)
        self.delete_btn = QPushButton(t("projects.delete"), self)
        self.delete_btn.setObjectName("deleteBtn")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(self.delete_btn)
        right.addLayout(btn_row)
        body.addLayout(right, 1)
        outer.addLayout(body, 1)

        self._load_items()

    # -- list build ---------------------------------------------------------
    def _load_items(self) -> None:
        self._projects = self.store.list()
        self._chat_path = Path.home() / ".pillow" / "chat" / "history.jsonl"
        self._has_chat = self._chat_path.exists()
        title = f"{t('projects.title')} · {t('projects.count', n=len(self._projects))}"
        in_prog = sum(1 for p in self._projects if getattr(p, "unfinished", False))
        if in_prog:
            title += " · " + t("projects.in_progress_count", m=in_prog)
        self.title.setText(title)

        if self._has_chat:
            when = time.strftime("%m-%d %H:%M", time.localtime(self._chat_path.stat().st_mtime))
            it = QListWidgetItem(f"{t('projects.chat_entry')}\n{when}", self.list)
            it.setSizeHint(QSize(0, 52))
        current_row = 0
        for i, p in enumerate(self._projects):
            when = time.strftime("%m-%d %H:%M", time.localtime(p.updated_at))
            badge = "   " + t("projects.current_badge") if p.id == self._current_pid else ""
            if getattr(p, "unfinished", False):
                badge += "   " + t("projects.in_progress_badge")
            preview = (p.last_prompt or "").strip().replace("\n", " ")
            if len(preview) > 26:
                preview = preview[:26] + "…"
            text = f"📁 {p.name}{badge}\n{when}"
            if preview:
                text += f"   ·   {preview}"
            it = QListWidgetItem(text, self.list)
            it.setSizeHint(QSize(0, 60))
            if p.id == self._current_pid:
                it.setForeground(QColor("#37699E"))
                current_row = (1 if self._has_chat else 0) + i

        if not self._projects and not self._has_chat:
            self.history_view.setPlainText(t("projects.empty"))
        else:
            self.list.setCurrentRow(current_row)

    @property
    def _current(self):
        """Selected project, or None (nothing / the chat entry is selected)."""
        row = self.list.currentRow()
        if self._has_chat:
            row -= 1  # row 0 is the chat pseudo-entry
        return self._projects[row] if 0 <= row < len(self._projects) else None

    def _chat_selected(self) -> bool:
        return self._has_chat and self.list.currentRow() == 0

    # -- selection / preview ------------------------------------------------
    def _on_select(self) -> None:
        if self._chat_selected():
            self.open_btn.setEnabled(True)
            self.switch_btn.setEnabled(True)
            self.switch_btn.setText(t("projects.switch_chat"))
            self.delete_btn.setEnabled(False)  # the one-off chat isn't a project
            self.hist_header.setText(t("projects.chat_header"))
            self._show_chat()
            return
        project = self._current
        if project is None:
            self.switch_btn.setEnabled(False)
            self.delete_btn.setEnabled(False)
            return
        self.open_btn.setEnabled(True)
        self.delete_btn.setEnabled(True)
        self.switch_btn.setEnabled(project.id != self._current_pid)
        self.switch_btn.setText(t("projects.switch"))
        turns = self.store.load_history(project, max_turns=200)
        self.hist_header.setText(
            t("projects.project", name=project.name) + "   ·   "
            + t("projects.turns", n=len(turns) // 2))
        if not turns:
            self.history_view.setPlainText(t("projects.no_history"))
            return
        lines = [t("projects.dir", path=project.root), ""]
        for turn in turns:
            who = t("role.user") if turn["role"] == "user" else t("role.assistant")
            lines.append(f"【{who}】{turn['content']}")
        self.history_view.setPlainText("\n".join(lines))

    def _show_chat(self) -> None:
        lines = [t("projects.dir", path=self._chat_path.parent), ""]
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

    # -- actions ------------------------------------------------------------
    def _do_switch(self) -> None:
        if self._on_switch is None:
            return
        target = None if self._chat_selected() else self._current
        if not self._chat_selected() and target is None:
            return
        self._on_switch(target)  # Project or None (= one-off chat)
        self.close()

    def _delete_selected(self) -> None:
        project = self._current
        if project is None or self._chat_selected():
            return
        dlg = _ConfirmDialog(t("projects.delete_confirm_title"),
                             t("projects.delete_confirm", name=project.name),
                             t("projects.delete"), t("ask.cancel"), parent=self)
        dlg.resize(380, 0)
        dlg.move(self.frameGeometry().center().x() - 190, self.frameGeometry().center().y() - 80)
        self._armed = False  # the modal steals activation; don't auto-close behind it
        dlg.raise_()
        dlg.activateWindow()
        try:
            confirmed = dlg.exec() == QDialog.Accepted
        finally:
            QTimer.singleShot(350, lambda: setattr(self, "_armed", True))
        if not confirmed:
            return
        pid, pname = project.id, project.name
        if not self.store.delete(pid):
            warn = _ConfirmDialog(t("projects.delete_confirm_title"),
                                  t("projects.delete_failed", name=pname),
                                  t("ask.submit"), t("ask.cancel"), parent=self)
            warn.exec()
            return
        if self._on_delete is not None:
            self._on_delete(pid)  # let the app unbind the session if it was current
        # Rebuild the list in place.
        self.list.clear()
        self.history_view.clear()
        self.hist_header.clear()
        self.switch_btn.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self._load_items()

    def _open_workspace(self) -> None:
        if self._chat_selected():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._chat_path.parent / "workspace")))
            return
        project = self._current
        if project is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(project.workspace)))

    def _apply_glass_opacity(self, opacity: int) -> None:
        self._glass_opacity = max(0, min(100, int(opacity)))
        self.setStyleSheet(panel_qss(self._glass_opacity))
        if self.isVisible():
            if self._glass_opacity == 0:
                disable_acrylic(self)
            else:
                enable_acrylic(self, white_acrylic_color(self._glass_opacity))
    # -- auto-dismiss -------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_glass_opacity(glass_opacity())
        self.activateWindow()
        self.raise_()
        QTimer.singleShot(350, lambda: setattr(self, "_armed", True))

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.ActivationChange and self._armed and not self.isActiveWindow():
            self.close()
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
