from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from pillow_assistant.ui.config_dialog import ModelConfigDialog
from pillow_assistant.ui.floating_widget import FloatingAssistant
from storage import Storage


class PillowAssistantApplication:
    """Application bootstrap wiring QApplication, storage, and top-level UI."""

    def __init__(self) -> None:
        self.base_path = Path(__file__).resolve().parent
        self.data_path = (self.base_path.parent / "data").resolve()
        self.data_path.mkdir(parents=True, exist_ok=True)

        self.storage = Storage(self.data_path / "assistant.db")
        self.storage.ensure_schema()

        self.qt_app = QApplication(sys.argv)
        self.qt_app.setApplicationName("Pillow Assistant")
        self.qt_app.setQuitOnLastWindowClosed(False)
        # Optional placeholder icon.
        self.qt_app.setWindowIcon(QIcon())

        if self.storage.is_first_run() or not self.storage.has_model_configs():
            self._prompt_for_initial_config()

        self.assistant = FloatingAssistant(storage=self.storage)

    def _prompt_for_initial_config(self) -> None:
        dialog = ModelConfigDialog(storage=self.storage)
        dialog.setWindowTitle("模型 API 配置")
        dialog.exec()
        self.storage.mark_initialized()

    def run(self) -> int:
        self.assistant.show()
        return self.qt_app.exec()


def main() -> int:
    app = PillowAssistantApplication()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())

