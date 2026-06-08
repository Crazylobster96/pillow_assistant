from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType, RequestKind
from storage import Storage


class SearchDialog(QDialog):
    """Capture text input and route it to a configured model via the event bus."""

    def __init__(self, storage: Storage, bus=None, parent=None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.bus = bus
        self.setWindowTitle("文本输入")
        self.setModal(True)
        self.resize(440, 380)

        self._active_id: Optional[str] = None
        self.models = self.storage.list_model_configs()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("选择模型", self))
        self.model_combo = QComboBox(self)
        for row in self.models:
            name = f"{row['display_name']} ({row['provider']})"
            self.model_combo.addItem(name, userData=row["display_name"])
        layout.addWidget(self.model_combo)

        layout.addWidget(QLabel("输入提示词", self))
        self.prompt_edit = QLineEdit(self)
        self.prompt_edit.setPlaceholderText("在此输入，回车发送")
        layout.addWidget(self.prompt_edit)

        self.history_view = QPlainTextEdit(self)
        self.history_view.setReadOnly(True)
        layout.addWidget(self.history_view)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.prompt_edit.returnPressed.connect(self._on_submit)

        if self.bus is not None:
            self.bus.event.connect(self._on_event)

        if not self.models:
            self.prompt_edit.setEnabled(False)
            self.history_view.setPlainText("尚未配置模型 API 信息。")
        elif self.bus is None:
            self.prompt_edit.setEnabled(False)
            self.history_view.setPlainText("执行层未就绪（事件总线缺失）。")

    def _on_submit(self) -> None:
        text = self.prompt_edit.text().strip()
        if not text:
            return
        if not self.models:
            QMessageBox.warning(self, "缺少配置", "请先配置模型 API 信息。")
            return
        if self.bus is None or self._active_id is not None:
            return

        model_ref = self.model_combo.currentData()
        request = AppRequest(kind=RequestKind.TEXT, prompt=text, model_ref=model_ref)
        self._active_id = request.id

        self.history_view.appendPlainText(f"> {text}")
        self.history_view.appendPlainText("")  # line the response will grow into
        self.prompt_edit.clear()
        self.prompt_edit.setEnabled(False)

        self.bus.submit(request)

    def _on_event(self, event: AgentEvent) -> None:
        if event.request_id != self._active_id:
            return
        if event.type == EventType.TOKEN:
            self.history_view.moveCursor(self.history_view.textCursor().End)
            self.history_view.insertPlainText(event.text)
        elif event.type == EventType.ERROR:
            self.history_view.appendPlainText(f"[错误] {event.text}")
            self._finish()
        elif event.type == EventType.DONE:
            self.history_view.appendPlainText("")
            self._finish()

    def _finish(self) -> None:
        self._active_id = None
        self.prompt_edit.setEnabled(True)
        self.prompt_edit.setFocus()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.bus is not None:
            try:
                self.bus.event.disconnect(self._on_event)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(event)
