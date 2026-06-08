from __future__ import annotations

from typing import List, Optional

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

from pillow_assistant.core.i18n import t
from storage import Storage


class ModelConfigDialog(QDialog):
    """Collects model API configuration details and persists them.

    Since R0 the API key is stored in the OS keychain (``vault``) keyed by
    display name, never in the database.
    """

    PROVIDERS = ["OpenAI", "Anthropic", "vLLM", "Ollama", t("config.custom_provider")]
    MODEL_TYPES = ["llm", "vlm"]

    def __init__(self, storage: Storage, vault=None, parent=None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.vault = vault
        self.setWindowTitle(t("config.title"))
        self.resize(640, 560)

        # In-memory working copy; api_key is hydrated from the vault on demand.
        self.configs: List[dict] = [dict(row) for row in self.storage.list_model_configs()]
        for cfg in self.configs:
            cfg.setdefault("model", "")
            if self.vault is not None:
                cfg["api_key"] = self.vault.get_secret(cfg.get("display_name", "")) or ""

        self._build_ui()
        self._refresh_table()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form_group = QFormLayout()
        form_group.setLabelAlignment(Qt.AlignRight)

        self.provider_combo = QComboBox(self)
        self.provider_combo.addItems(self.PROVIDERS)
        form_group.addRow(t("config.provider"), self.provider_combo)

        self.model_type_combo = QComboBox(self)
        self.model_type_combo.addItems(self.MODEL_TYPES)
        form_group.addRow(t("config.model_type"), self.model_type_combo)

        self.display_name_edit = QLineEdit(self)
        self.display_name_edit.setPlaceholderText(t("config.display_name_ph"))
        form_group.addRow(t("config.display_name"), self.display_name_edit)

        self.model_edit = QLineEdit(self)
        self.model_edit.setPlaceholderText(t("config.model_name_ph"))
        form_group.addRow(t("config.model_name"), self.model_edit)

        self.base_url_edit = QLineEdit(self)
        self.base_url_edit.setPlaceholderText(t("config.base_url_ph"))
        form_group.addRow(t("config.base_url"), self.base_url_edit)

        self.api_key_edit = QLineEdit(self)
        self.api_key_edit.setPlaceholderText(t("config.api_key_ph"))
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        form_group.addRow("API Key", self.api_key_edit)

        self.extra_edit = QTextEdit(self)
        self.extra_edit.setPlaceholderText(t("config.extra_ph"))
        self.extra_edit.setFixedHeight(70)
        form_group.addRow(t("config.extra"), self.extra_edit)

        layout.addLayout(form_group)

        button_row = QHBoxLayout()
        self.add_button = QPushButton(t("config.add"), self)
        self.remove_button = QPushButton(t("config.remove"), self)
        self.default_button = QPushButton(t("config.set_default"), self)
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.remove_button)
        button_row.addWidget(self.default_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(
            [t("config.display_name"), t("config.provider"), t("config.model_type"),
             t("config.model_name"), t("config.base_url")]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        hint = QLabel(t("config.hint"))
        hint.setStyleSheet("color: gray; font-size: 12px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.add_button.clicked.connect(self._add_or_update_config)
        self.remove_button.clicked.connect(self._remove_selected_config)
        self.default_button.clicked.connect(self._set_default_chat_model)
        self.table.itemSelectionChanged.connect(self._populate_from_selection)

    def _set_default_chat_model(self) -> None:
        """Persist the selected model as the chat-role default (model_roles)."""
        rows = {item.row() for item in self.table.selectedItems()}
        if not rows:
            return
        name = self.configs[sorted(rows)[0]]["display_name"]
        from pillow_assistant.core.model_roles import assign
        assign("chat", name)
        self._refresh_table()
        QMessageBox.information(self, t("config.set_default"),
                                t("config.default_done", name=name))

    def _add_or_update_config(self) -> None:
        display_name = self.display_name_edit.text().strip()
        if not display_name:
            QMessageBox.warning(self, t("config.missing_title"), t("config.missing_name"))
            return

        payload = {
            "provider": self.provider_combo.currentText(),
            "model_type": self.model_type_combo.currentText(),
            "display_name": display_name,
            "model": self.model_edit.text().strip() or None,
            "base_url": self.base_url_edit.text().strip() or None,
            "api_key": self.api_key_edit.text().strip() or None,
            "extra": self.extra_edit.toPlainText().strip() or None,
        }

        for idx, existing in enumerate(self.configs):
            if existing["display_name"] == display_name:
                # Preserve an existing key if the field was left blank.
                if not payload["api_key"]:
                    payload["api_key"] = existing.get("api_key")
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
        try:
            from pillow_assistant.core.model_roles import load_roles
            default_chat = load_roles().get("chat")
        except Exception:
            default_chat = None
        self.table.setRowCount(len(self.configs))
        for row_idx, cfg in enumerate(self.configs):
            name = cfg["display_name"]
            tag = t("config.default_tag") if name == default_chat else ""
            self.table.setItem(row_idx, 0, QTableWidgetItem(name + tag))
            self.table.setItem(row_idx, 1, QTableWidgetItem(cfg["provider"]))
            self.table.setItem(row_idx, 2, QTableWidgetItem(cfg["model_type"]))
            self.table.setItem(row_idx, 3, QTableWidgetItem(cfg.get("model") or ""))
            self.table.setItem(row_idx, 4, QTableWidgetItem(cfg.get("base_url") or ""))

    def _clear_form(self) -> None:
        self.display_name_edit.clear()
        self.model_edit.clear()
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
        self.model_edit.setText(cfg.get("model") or "")
        self.base_url_edit.setText(cfg.get("base_url") or "")
        self.api_key_edit.setText(cfg.get("api_key") or "")
        self.extra_edit.setPlainText(cfg.get("extra") or "")

    def accept(self) -> None:
        if not self.configs:
            QMessageBox.warning(self, t("config.need_one_title"), t("config.need_one"))
            return
        self.storage.replace_model_configs(self.configs, self.vault)
        super().accept()
