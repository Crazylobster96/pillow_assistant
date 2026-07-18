"""Lifecycle, positioning, and navigation for windows around the floating icon."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from pillow_assistant.core.i18n import t
from pillow_assistant.ui.config_dialog import ModelConfigDialog
from pillow_assistant.ui.quick_input import QuickInputBar
from pillow_assistant.ui.radial_menu import RadialMenu


class WindowCoordinator:
    """Own transient window references and keep them anchored to one widget."""

    def __init__(self, anchor, storage, bus=None, session=None, vault=None, project_store=None) -> None:
        self.anchor = anchor
        self.storage = storage
        self.bus = bus
        self.session = session
        self.vault = vault
        self.project_store = project_store
        self._slots = {
            "quick": None,
            "panel": None,
            "radial": None,
            "projects": None,
            "memory": None,
            "surface": None,
            "undo": None,
            "ask": None,
        }
        self.multi_windows: list = []
        self._last_anchor_pos = None
        self._follow_offsets = None

    def window(self, name: str):
        return self._slots.get(name)

    @property
    def quick(self):
        return self.window("quick")

    @property
    def panel(self):
        return self.window("panel")

    def set_window(self, name: str, window) -> None:
        self._slots[name] = window

    def clear_window(self, name: str, expected=None) -> None:
        if expected is None or self._slots.get(name) is expected:
            self._slots[name] = None

    def close_window(self, name: str) -> None:
        window = self._slots.get(name)
        self._slots[name] = None
        if window is not None:
            try:
                window.close()
            except RuntimeError:
                pass

    def add_multi_window(self, window) -> None:
        self.multi_windows.append(window)

    def remove_multi_window(self, window) -> None:
        self.multi_windows = [item for item in self.multi_windows if item is not window]

    def close_multi_windows(self) -> None:
        windows = list(self.multi_windows)
        self.multi_windows = []
        for window in windows:
            try:
                window.close()
            except RuntimeError:
                pass

    def followers(self) -> list:
        # Preserve the original follow set. The conversation-memory browser is
        # intentionally independent, like a normal management window.
        return [
            self.window("quick"),
            self.window("panel"),
            self.window("radial"),
            self.window("projects"),
            self.window("surface"),
            self.window("undo"),
            self.window("ask"),
            *self.multi_windows,
        ]

    def place_near_icon(self, window) -> None:
        window.adjustSize()
        width, height = window.width(), window.height()
        anchor_geometry = self.anchor.frameGeometry()
        screen = QGuiApplication.screenAt(anchor_geometry.center()) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()

        obstacles = []
        for other in [
            self.window("quick"), self.window("panel"), self.window("undo"),
            self.window("ask"), self.window("surface"), *self.multi_windows,
        ]:
            if other is None or other is window:
                continue
            try:
                if other.isVisible():
                    obstacles.append(other.frameGeometry())
            except RuntimeError:
                continue

        def clear_y(x: int) -> int:
            y = anchor_geometry.top() + 4
            for _ in range(len(obstacles) + 1):
                candidate = QRect(x, y, width, height)
                collision = next((item for item in obstacles if candidate.intersects(item)), None)
                if collision is None:
                    return y
                y = collision.bottom() + 8
            return y

        right_x = anchor_geometry.right() + 12
        left_x = anchor_geometry.left() - width - 12
        candidates = []
        if right_x + width <= available.right():
            candidates.append(right_x)
        if left_x >= available.left():
            candidates.append(left_x)
        candidates.append(max(available.left(), min(right_x, available.right() - width)))

        x = candidates[0]
        y = clear_y(x)
        for candidate_x in candidates:
            candidate_y = clear_y(candidate_x)
            if candidate_y + height <= available.bottom():
                x, y = candidate_x, candidate_y
                break
        x = min(max(available.left(), x), max(available.left(), available.right() - width))
        y = min(max(available.top(), y), max(available.top(), available.bottom() - height))
        window.move(x, y)

    def capture_follow_offsets(self) -> None:
        offsets = []
        for window in self.followers():
            if window is None:
                continue
            try:
                if window.isVisible():
                    offsets.append((window, window.pos() - self.anchor.pos()))
            except RuntimeError:
                continue
        self._follow_offsets = offsets

    def finish_anchor_drag(self) -> None:
        self._follow_offsets = None

    def follow_anchor_move(self, new_position: QPoint) -> None:
        old_position = self._last_anchor_pos
        self._last_anchor_pos = new_position
        if self._follow_offsets:
            for window, offset in self._follow_offsets:
                try:
                    if window.isVisible():
                        window.move(new_position + offset)
                except RuntimeError:
                    continue
            return
        if old_position is None:
            return
        delta = new_position - old_position
        if delta.isNull():
            return
        for window in self.followers():
            if window is None:
                continue
            try:
                if window.isVisible():
                    window.move(window.pos() + delta)
            except RuntimeError:
                continue

    def anchor_point(self) -> QPoint:
        return self.anchor.frameGeometry().bottomLeft() + QPoint(0, 6)

    def recent_history(self, max_turns: int = 8) -> list:
        try:
            project_id = getattr(self.session, "project_id", None)
            if project_id and self.project_store is not None:
                project = self.project_store.get(project_id)
                if project is not None:
                    session_id = getattr(self.session, "session_id", None)
                    return self.project_store.load_history(project, session_id, max_turns=max_turns)
            turns = []
            path = Path.home() / ".pillow" / "chat" / "history.jsonl"
            for line in path.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if isinstance(item, dict) and item.get("role") and item.get("content") is not None:
                    turns.append({"role": item["role"], "content": str(item["content"])})
            return turns[-max_turns:]
        except Exception:
            return []

    def quick_is_open(self) -> bool:
        try:
            return self.quick is not None and self.quick.isVisible()
        except RuntimeError:
            return False

    def open_quick_input(self, anchor_global=None, *, close_displays: bool = True) -> Optional[QuickInputBar]:
        if self.bus is None:
            return None
        if close_displays:
            self.close_displays()
        else:
            self.close_window("quick")
        bar = QuickInputBar(
            self.storage,
            self.bus,
            self.session,
            anchor_global=anchor_global or self.anchor_point(),
            open_reference=lambda path: self.open_file_panel([path]),
            history=self.recent_history(),
        )
        self.set_window("quick", bar)
        bar.destroyed.connect(lambda *_, item=bar: self.clear_window("quick", item))
        bar.show()
        bar.raise_()
        bar.activateWindow()
        bar.prompt_edit.setFocus()
        return bar

    def open_radial_menu(self) -> None:
        center = self.anchor.frameGeometry().center()
        items = [
            (t("menu.projects"), self.open_projects),
            ("Memory", self.open_conversation_memory),
            (t("menu.config"), self.open_config),
            (t("menu.close_displays"), self.close_displays),
            (t("menu.clear_refs"), self.clear_references),
            (t("menu.quit"), self.quit),
        ]
        self.close_window("radial")
        quick = self.quick
        avoid = quick.frameGeometry() if quick is not None and quick.isVisible() else None
        menu = RadialMenu(
            items,
            center,
            avoid_rect=avoid,
            exclude_rect_getter=lambda: self.anchor.frameGeometry(),
            make_room=self.make_room_for_menu,
        )
        self.set_window("radial", menu)
        menu.destroyed.connect(lambda *_, item=menu: self.clear_window("radial", item))
        menu.show()
        menu.raise_()

    def make_room_for_menu(self):
        bar = self.quick
        if bar is None or not bar.isVisible():
            return None
        center = self.anchor.frameGeometry().center()
        clearance = RadialMenu.RADIUS + RadialMenu.BTN + 24
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        geometry = bar.frameGeometry()
        y = center.y() + clearance
        if y + geometry.height() > available.bottom():
            y = max(available.top(), center.y() - clearance - geometry.height())
        x = min(max(geometry.x(), available.left()), max(available.left(), available.right() - geometry.width()))
        bar.move(x, y)
        return bar.frameGeometry()

    def close_radial(self) -> None:
        self.close_window("radial")

    def close_displays(self) -> None:
        self.close_window("panel")
        self.close_window("surface")
        self.close_window("quick")
        self.close_multi_windows()

    def open_projects(self) -> None:
        if self.project_store is None:
            return
        from pillow_assistant.ui.projects_panel import ProjectsPanel

        self.close_window("projects")
        current_id = getattr(self.session, "project_id", None) if self.session is not None else None
        window = ProjectsPanel(
            self.project_store,
            on_switch=self.switch_to_project,
            current_project_id=current_id,
            on_delete=self.on_project_deleted,
        )
        self.set_window("projects", window)
        window.destroyed.connect(lambda *_, item=window: self.clear_window("projects", item))
        window.move(self.anchor.frameGeometry().center())
        window.show()
        window.raise_()
        window.activateWindow()

    def switch_to_project(self, project) -> None:
        if self.session is not None:
            self.session.clear()
            if project is None:
                self.session.project_id = None
                self.session.session_id = None
            else:
                try:
                    sessions = self.project_store.list_sessions(project)
                except Exception:
                    sessions = []
                self.session.project_id = project.id
                self.session.session_id = (
                    sessions[0]["id"] if sessions else self.project_store.new_session_id()
                )
        self.open_quick_input()

    def on_project_deleted(self, project_id) -> None:
        if self.session is not None and getattr(self.session, "project_id", None) == project_id:
            self.session.project_id = None
            self.session.session_id = None

    def open_conversation_memory(self) -> None:
        from pillow_assistant.ui.conversation_memory_panel import ConversationMemoryPanel

        self.close_window("memory")
        window = ConversationMemoryPanel(self.storage.db_path)
        self.set_window("memory", window)
        window.destroyed.connect(lambda *_, item=window: self.clear_window("memory", item))
        window.move(self.anchor.frameGeometry().center())
        window.show()
        window.raise_()
        window.activateWindow()

    def open_config(self) -> None:
        ModelConfigDialog(storage=self.storage, vault=self.vault, parent=self.anchor).exec()

    def clear_references(self) -> None:
        if self.session is not None:
            self.session.clear()
        if self.quick is not None:
            self.quick._refresh_chips()

    def quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def open_file_panel(self, paths: list[str]) -> None:
        from pillow_assistant.ui.viewer_registry import resolve

        self.close_window("quick")
        self.close_window("panel")
        panel_class = resolve(paths)
        panel = panel_class(
            paths,
            self.storage,
            self.bus,
            self.session,
            anchor_global=self.anchor_point(),
            history=self.recent_history(),
        )
        self.set_window("panel", panel)
        panel.destroyed.connect(lambda *_, item=panel: self.clear_window("panel", item))
        panel.show()
        panel.raise_()
        panel.activateWindow()
        panel.prompt_edit.setFocus()
