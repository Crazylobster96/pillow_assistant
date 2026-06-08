from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
)

from pillow_assistant.contracts import AgentEvent, AppRequest, EventType, RequestKind
from storage import Storage


class ImagePreviewDialog(QDialog):
    """Display a dropped image alongside a prompt box; route to a VLM via the bus."""

    def __init__(self, image_path: str | Path, storage: Storage, bus=None, parent=None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.bus = bus
        self.image_path = Path(image_path)
        self.setWindowTitle("图像分析")
        self.resize(600, 560)

        self._active_id: Optional[str] = None

        pixmap = QPixmap(str(self.image_path))
        if pixmap.isNull():
            QMessageBox.critical(self, "加载失败", f"无法加载图像：{self.image_path}")
            self.reject()
            return

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel(f"图像: {self.image_path.name}", self))

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        img_label = QLabel(self)
        img_label.setAlignment(Qt.AlignCenter)
        img_label.setPixmap(pixmap.scaledToWidth(520, Qt.SmoothTransformation))
        scroll.setWidget(img_label)
        layout.addWidget(scroll)

        layout.addWidget(QLabel("选择多模态模型", self))
        self.model_combo = QComboBox(self)
        vl_models = [
            row for row in self.storage.list_model_configs() if row["model_type"].lower() == "vlm"
        ]
        for row in vl_models:
            name = f"{row['display_name']} ({row['provider']})"
            self.model_combo.addItem(name, userData=row["display_name"])
        layout.addWidget(self.model_combo)

        layout.addWidget(QLabel("向模型提问", self))
        self.prompt_edit = QLineEdit(self)
        self.prompt_edit.setPlaceholderText("请输入与图像相关的问题，回车发送")
        layout.addWidget(self.prompt_edit)

        self.history_view = QPlainTextEdit(self)
        self.history_view.setReadOnly(True)
        layout.addWidget(self.history_view)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.has_models = bool(vl_models)
        if self.bus is not None:
            self.bus.event.connect(self._on_event)

        if not self.has_models:
            self.history_view.setPlainText("尚未配置多模态模型，无法发送图像查询。")
            self.prompt_edit.setEnabled(False)
        elif self.bus is None:
            self.history_view.setPlainText("执行层未就绪（事件总线缺失）。")
            self.prompt_edit.setEnabled(False)

        self.prompt_edit.returnPressed.connect(self._submit_question)

    def _submit_question(self) -> None:
        text = self.prompt_edit.text().strip()
        if not text:
            return
        if not self.has_models:
            QMessageBox.warning(self, "缺少模型", "请先配置多模态模型。")
            return
        if self.bus is None or self._active_id is not None:
            return

        model_ref = self.model_combo.currentData()
        request = AppRequest(
            kind=RequestKind.IMAGE, prompt=text, model_ref=model_ref, image_path=str(self.image_path)
        )
        self._active_id = request.id

        self.history_view.appendPlainText(f"> {text}")
        self.history_view.appendPlainText("")
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
