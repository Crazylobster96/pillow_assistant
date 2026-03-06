from __future__ import annotations

from PySide6.QtCore import Qt
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

from storage import Storage


class SearchDialog(QDialog):
    """Dialog to capture text input and route to configured models."""

    def __init__(self, storage: Storage, parent=None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("文本输入")
        self.setModal(True)
        self.resize(420, 360)

        self.models = self.storage.list_model_configs()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("选择模型", self))
        self.model_combo = QComboBox(self)
        for row in self.models:
            name = f"{row['display_name']} ({row['provider']})"
            self.model_combo.addItem(name, userData=row)
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

        if not self.models:
            self.prompt_edit.setEnabled(False)
            self.history_view.setPlainText("尚未配置模型 API 信息。")

    def _on_submit(self) -> None:
        text = self.prompt_edit.text().strip()
        if not text:
            return
        if not self.models:
            QMessageBox.warning(self, "缺少配置", "请先配置模型 API 信息。")
            return

        selected = self.model_combo.currentData()
        self.history_view.appendPlainText(f"> {text}")
        self.history_view.appendPlainText(
            f"[{selected['display_name']}] 暂未集成调用逻辑，请在此接入模型请求。\n"
        )
        self.prompt_edit.clear()

