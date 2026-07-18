"""Translate core AgentEvents into transient desktop surfaces."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication

from pillow_assistant.contracts import EventType, SurfaceLevel
from pillow_assistant.core.i18n import t
from pillow_assistant.ui.ask_dialog import AskDialog
from pillow_assistant.ui.surface_window import SurfaceMainWindow
from pillow_assistant.ui.undo_toast import UndoToast


class EventPresenter:
    def __init__(self, coordinator, *, storage, bus=None, session=None,
                 undo_manager=None, ask_broker=None, flash_icon=None) -> None:
        self.coordinator = coordinator
        self.storage = storage
        self.bus = bus
        self.session = session
        self.undo_manager = undo_manager
        self.ask_broker = ask_broker
        self.flash_icon = flash_icon or (lambda: None)

    def handle(self, event) -> None:
        event_type = getattr(event, "type", None)
        if event_type == EventType.UNDO:
            self.show_undo(event)
        elif event_type == EventType.SURFACE:
            self.show_surface(event)
        elif event_type == EventType.ASK:
            self.show_ask(event)

    def show_ask(self, event) -> None:
        if self.ask_broker is None:
            return
        meta = getattr(event, "meta", None) or {}
        ask_id = meta.get("ask_id")
        if not ask_id:
            return
        self.coordinator.close_window("ask")

        def on_answer(result, request_id=ask_id):
            self.ask_broker.resolve(request_id, result)

        dialog = AskDialog(meta, on_answer)
        self.coordinator.set_window("ask", dialog)
        dialog.destroyed.connect(lambda *_, item=dialog: self.coordinator.clear_window("ask", item))
        self.coordinator.place_near_icon(dialog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def request_has_view(self, request_id) -> bool:
        if not request_id:
            return False
        for window in (self.coordinator.quick, self.coordinator.panel):
            if window is None:
                continue
            try:
                if getattr(window, "_active_id", None) == request_id and window.isVisible():
                    return True
            except RuntimeError:
                continue
        return False

    def show_surface(self, event) -> None:
        surface = getattr(event, "surface", None)
        if surface is None:
            return
        if getattr(surface, "kind", "") == "multi":
            self.open_multi_windows(getattr(surface, "payload", None) or {})
            return
        if self.request_has_view(getattr(event, "request_id", None)):
            return
        level = getattr(surface, "level", None)
        if level == SurfaceLevel.L1:
            self.flash_icon()
        elif level == SurfaceLevel.L5:
            self.open_surface_window(surface)

    def open_multi_windows(self, payload: dict) -> None:
        from pillow_assistant.ui.viewer_registry import resolve

        views = payload.get("views") or []
        layout = payload.get("layout") or "row"
        if not views:
            return
        self.coordinator.close_multi_windows()

        anchor_geometry = self.coordinator.anchor.frameGeometry()
        screen = QGuiApplication.screenAt(anchor_geometry.center()) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        count = len(views)
        gap = 8
        bar_space = 170
        if layout == "column":
            width = int(available.width() * 0.62)
            height = (available.height() - bar_space - gap * (count - 1)) // count
            x = available.x() + (available.width() - width) // 2
            geometries = [
                QRect(x, available.y() + index * (height + gap), width, height)
                for index in range(count)
            ]
            bar_y = available.y() + count * (height + gap) + 4
        else:
            width = (available.width() - gap * (count - 1)) // count
            height = int(available.height() * 0.72)
            y = available.y() + max(0, (available.height() - bar_space - height) // 2)
            geometries = [
                QRect(available.x() + index * (width + gap), y, width, height)
                for index in range(count)
            ]
            bar_y = y + height + gap

        for view, geometry in zip(views, geometries):
            path = view.get("path")
            try:
                if path:
                    panel_class = resolve([path])
                    window = panel_class([path], self.storage, self.bus, self.session, show_dialog=False)
                else:
                    window = SurfaceMainWindow(
                        view.get("text", "") or "",
                        [],
                        "",
                        title=view.get("title") or t("multi.content_title"),
                    )
            except Exception as exc:
                try:
                    window = SurfaceMainWindow(
                        t("multi.open_failed", path=path or "", err=exc),
                        [],
                        "",
                        title=view.get("title") or t("multi.open_failed_title"),
                    )
                except Exception:
                    continue
            window.destroyed.connect(
                lambda *_, item=window: self.coordinator.remove_multi_window(item)
            )
            window.setGeometry(geometry)
            window.show()
            window.raise_()
            self.coordinator.add_multi_window(window)

        if self.bus is not None and self.coordinator.multi_windows:
            anchor = QPoint(available.x() + (available.width() - 560) // 2, bar_y)
            self.coordinator.open_quick_input(anchor, close_displays=False)

    def open_surface_window(self, surface) -> None:
        self.coordinator.close_window("surface")
        payload = getattr(surface, "payload", None) or {}
        window = SurfaceMainWindow(
            getattr(surface, "body", "") or "",
            payload.get("artifacts", []),
            payload.get("workspace", ""),
        )
        self.coordinator.set_window("surface", window)
        window.destroyed.connect(
            lambda *_, item=window: self.coordinator.clear_window("surface", item)
        )
        self.coordinator.place_near_icon(window)
        window.show()
        window.raise_()
        window.activateWindow()

    def show_undo(self, event) -> None:
        if getattr(event, "type", None) != EventType.UNDO or self.undo_manager is None:
            return
        token = (getattr(event, "meta", None) or {}).get("token")
        if not token:
            return
        self.coordinator.close_window("undo")
        toast = UndoToast(
            event.text or t("undo.default_label"),
            lambda value=token: self.undo_manager.undo(value),
        )
        self.coordinator.set_window("undo", toast)
        toast.destroyed.connect(lambda *_, item=toast: self.coordinator.clear_window("undo", item))
        self.coordinator.place_near_icon(toast)
        toast.show()
        toast.raise_()
