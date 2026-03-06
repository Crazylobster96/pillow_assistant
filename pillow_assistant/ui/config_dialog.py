from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from storage import Storage


class ModelConfigDialog(QDialog):
    """Collects model API configuration details and persists them."""

    PROVIDERS = ["OpenAI", "vLLM", "Ollama", "自定义"]
    MODEL_TYPES = ["llm", "vlm"]

    def __init__(self, storage: Storage, parent=None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("模型 API 配置")
        self.resize(640, 520)

        self.configs: List[dict] = [dict(row) for row in self.storage.list_model_configs()]

        self._build_ui()
        self._refresh_table()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form_group = QFormLayout()
        form_group.setLabelAlignment(Qt.AlignRight)

        self.provider_combo = QComboBox(self)
        self.provider_combo.addItems(self.PROVIDERS)
        form_group.addRow("服务提供商", self.provider_combo)

        self.model_type_combo = QComboBox(self)
        self.model_type_combo.addItems(self.MODEL_TYPES)
        form_group.addRow("模型类型", self.model_type_combo)

        self.display_name_edit = QLineEdit(self)
        self.display_name_edit.setPlaceholderText("例如：OpenAI GPT-4")
        form_group.addRow("显示名称", self.display_name_edit)

        self.base_url_edit = QLineEdit(self)
        self.base_url_edit.setPlaceholderText("例如：https://api.openai.com/v1")
        form_group.addRow("接口地址", self.base_url_edit)

        self.api_key_edit = QLineEdit(self)
        self.api_key_edit.setPlaceholderText("用于鉴权的 API Key")
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        form_group.addRow("API Key", self.api_key_edit)

        self.extra_edit = QTextEdit(self)
        self.extra_edit.setPlaceholderText("额外参数（JSON 字符串，可选）")
        form_group.addRow("额外参数", self.extra_edit)

        layout.addLayout(form_group)

        button_row = QHBoxLayout()
        self.add_button = QPushButton("添加/更新", self)
        self.remove_button = QPushButton("删除选中", self)
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.remove_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(
            ["显示名称", "提供商", "模型类型", "接口地址", "额外参数"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        hint = QLabel("提示：模型类型用于区分文本模型 (llm) 与多模态模型 (vlm)。")
        hint.setStyleSheet("color: gray; font-size: 12px;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.add_button.clicked.connect(self._add_or_update_config)
        self.remove_button.clicked.connect(self._remove_selected_config)
        self.table.itemSelectionChanged.connect(self._populate_from_selection)

    def _add_or_update_config(self) -> None:
        display_name = self.display_name_edit.text().strip()
        if not display_name:
            QMessageBox.warning(self, "缺少信息", "请填写显示名称。")
            return

        payload = {
            "provider": self.provider_combo.currentText(),
            "model_type": self.model_type_combo.currentText(),
            "display_name": display_name,
            "base_url": self.base_url_edit.text().strip() or None,
            "api_key": self.api_key_edit.text().strip() or None,
            "extra": self.extra_edit.toPlainText().strip() or None,
        }

        for idx, existing in enumerate(self.configs):
            if existing["display_name"] == display_name:
                self.configs[idx] = payload
                break
        else:
            self.configs.append(payload)

        self._refresh_table()
        self._clear_form()

    def _remove_selected_config(self) -> None:
        rows = {item.row() for item in self.table.selectedItems()}
        if not rows:
            return
        for row in sorted(rows, reverse=True):
            del self.configs[row]
        self._refresh_table()
        self._clear_form()

    def _refresh_table(self) -> None:
        self.table.setRowCount(len(self.configs))
        for row_idx, cfg in enumerate(self.configs):
            self.table.setItem(row_idx, 0, QTableWidgetItem(cfg["display_name"]))
            self.table.setItem(row_idx, 1, QTableWidgetItem(cfg["provider"]))
            self.table.setItem(row_idx, 2, QTableWidgetItem(cfg["model_type"]))
            self.table.setItem(row_idx, 3, QTableWidgetItem(cfg.get("base_url") or ""))
            self.table.setItem(row_idx, 4, QTableWidgetItem(cfg.get("extra") or ""))

    def _clear_form(self) -> None:
        self.display_name_edit.clear()
        self.base_url_edit.clear()
        self.api_key_edit.clear()
        self.extra_edit.clear()
        self.provider_combo.setCurrentIndex(0)
        self.model_type_combo.setCurrentIndex(0)

    def _populate_from_selection(self) -> None:
        rows = {item.row() for item in self.table.selectedItems()}
        if len(rows) != 1:
            return
        idx = rows.pop()
        cfg = self.configs[idx]
        self.provider_combo.setCurrentText(cfg["provider"])
        self.model_type_combo.setCurrentText(cfg["model_type"])
        self.display_name_edit.setText(cfg["display_name"])
        self.base_url_edit.setText(cfg.get("base_url") or "")
        self.api_key_edit.setText(cfg.get("api_key") or "")
        self.extra_edit.setPlainText(cfg.get("extra") or "")

    def accept(self) -> None:
        if not self.configs:
            QMessageBox.warning(self, "缺少配置", "请至少添加一个模型配置。")
            return
        self.storage.replace_model_configs(self.configs)
        super().accept()

