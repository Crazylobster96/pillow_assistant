from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from pillow_assistant.core.bus import EventBus
from pillow_assistant.core.orchestrator import Orchestrator
from pillow_assistant.core.project_manager import ProjectManager
from pillow_assistant.core.session import Session
from pillow_assistant.core.undo import UndoManager
from pillow_assistant.ui.config_dialog import ModelConfigDialog
from pillow_assistant.ui.floating_widget import FloatingAssistant
from storage import Storage, Vault
from storage.projects import ProjectStore


class PillowAssistantApplication:
    """Application bootstrap wiring QApplication, storage, vault, event bus, and UI."""

    def __init__(self) -> None:
        self.base_path = Path(__file__).resolve().parent
        self.data_path = (self.base_path.parent / "data").resolve()
        self.data_path.mkdir(parents=True, exist_ok=True)

        self.storage = Storage(self.data_path / "assistant.db")
        self.storage.ensure_schema()

        # Credential vault + one-time migration of any legacy plaintext keys.
        self.vault = Vault()
        self.storage.migrate_plaintext_keys(self.vault)

        self.qt_app = QApplication(sys.argv)
        self.qt_app.setApplicationName("Pillow Assistant")
        self.qt_app.setQuitOnLastWindowClosed(False)
        self.qt_app.setWindowIcon(QIcon())

        # Current session holds referenced files/folders + the bound project.
        self.session = Session()

        # Projects live under ~/.pillow/projects; the session binds to one (R1).
        self.project_store = ProjectStore(Path.home() / ".pillow" / "projects")
        self.project_manager = ProjectManager(self.project_store, self.session)

        # Shared 5-second undo manager (R3) — used by tools and the UI toast.
        self.undo_manager = UndoManager()

        # Ask broker: lets the Agent ask the user mid-task and await an answer.
        from pillow_assistant.core.ask import AskBroker
        self.ask_broker = AskBroker()

        # Execution core: event bus driving the Agent orchestrator (R1).
        self.bus = EventBus(Orchestrator(self.storage, self.vault, self.project_manager,
                                         undo_manager=self.undo_manager,
                                         ask_broker=self.ask_broker))
        self.qt_app.aboutToQuit.connect(self.bus.shutdown)

        if self.storage.is_first_run() or not self.storage.has_model_configs():
            self._prompt_for_initial_config()

        self.assistant = FloatingAssistant(
            storage=self.storage, bus=self.bus, session=self.session, vault=self.vault,
            project_store=self.project_store, undo_manager=self.undo_manager,
            ask_broker=self.ask_broker,
        )

    def _prompt_for_initial_config(self) -> None:
        dialog = ModelConfigDialog(storage=self.storage, vault=self.vault)
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
