from __future__ import annotations

from pathlib import Path

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

from storage import Storage


class ImagePreviewDialog(QDialog):
    """Display dropped image alongside a prompt input box."""

    def __init__(self, image_path: str | Path, storage: Storage, parent=None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.image_path = Path(image_path)
        self.setWindowTitle("图像分析")
        self.resize(600, 540)

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
            self.model_combo.addItem(name, userData=row)
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

        if not vl_models:
            self.history_view.setPlainText("尚未配置多模态模型，无法发送图像查询。")
            self.prompt_edit.setEnabled(False)

        self.prompt_edit.returnPressed.connect(self._submit_question)

    def _submit_question(self) -> None:
        text = self.prompt_edit.text().strip()
        if not text:
            return
        if not self.prompt_edit.isEnabled():
            QMessageBox.warning(self, "缺少模型", "请先配置多模态模型。")
            return

        selected = self.model_combo.currentData()
        self.history_view.appendPlainText(f"> {text}")
        self.history_view.appendPlainText(
            f"[{selected['display_name']}] 暂未集成图像理解调用逻辑，请在此接入。"
        )
        self.history_view.appendPlainText("")
        self.prompt_edit.clear()

