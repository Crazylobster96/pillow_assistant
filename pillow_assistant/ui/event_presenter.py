"""Translate core AgentEvents into transient desktop surfaces."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication

from pillow_assistant.contracts import EventType, SurfaceLevel
from pillow_assistant.core.i18n import t
from pillow_assistant.ui.ask_dialog import AskDialog
from pillow_assistant.ui.surface_window import SurfaceMainWindow
from pillow_assistant.ui.undo_toast import UndoToast



MULTI_GAP = 8
MULTI_BAR_SPACE = 170  # room under the windows for the question bar


def fit_windows(sizes: list, area: tuple, layout: str = "row") -> tuple:
    """Screen boxes (x, y, w, h) for windows that want ``sizes``, side by side ("row") or
    stacked ("column") in ``area`` (x, y, w, h): each keeps its own size up to its share of
    the screen, except that windows side by side share the tallest height and stacked ones
    the widest width; the group is centred. Also returns the y for the question bar."""
    ax, ay, aw, ah = area
    count = max(1, len(sizes))
    if layout == "column":
        share_w, share_h = int(aw * 0.9), (ah - MULTI_BAR_SPACE - MULTI_GAP * (count - 1)) // count
    else:
        share_w, share_h = (aw - MULTI_GAP * (count - 1)) // count, int(ah * 0.72)
    fitted = [(min(max(w, 320), share_w), min(max(h, 240), share_h)) for w, h in sizes]
    # Side by side they share one height, stacked one width: ragged edges look broken.
    if layout == "column":
        widest = max((w for w, _ in fitted), default=0)
        fitted = [(widest, h) for _, h in fitted]
    else:
        tallest = max((h for _, h in fitted), default=0)
        fitted = [(w, tallest) for w, _ in fitted]
    if layout == "column":
        total = sum(h for _, h in fitted) + MULTI_GAP * (len(fitted) - 1)
        y = ay + max(0, (ah - MULTI_BAR_SPACE - total) // 2)
        boxes = []
        for w, h in fitted:
            boxes.append((ax + (aw - w) // 2, y, w, h))
            y += h + MULTI_GAP
        return boxes, y - MULTI_GAP + 4
    total = sum(w for w, _ in fitted) + MULTI_GAP * (len(fitted) - 1)
    tallest = max((h for _, h in fitted), default=0)
    x = ax + max(0, (aw - total) // 2)
    top = ay + max(0, (ah - MULTI_BAR_SPACE - tallest) // 2)
    boxes = []
    for w, h in fitted:
        boxes.append((x, top + (tallest - h) // 2, w, h))
        x += w + MULTI_GAP
    return boxes, top + tallest + MULTI_GAP


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
        elif event_type in (EventType.DONE, EventType.ERROR):
            self.finish_run(event)

    def finish_run(self, event) -> None:
        """A run ended (or was stopped): drop its open question, flag it if nobody watches."""
        request_id = getattr(event, "request_id", None)
        dialog = self.coordinator.window("ask")
        if dialog is not None and getattr(dialog, "request_id", None) == request_id:
            self.coordinator.close_window("ask")
        if not self.request_has_view(request_id):
            self.flash_icon()

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
        dialog.request_id = getattr(event, "request_id", None)
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

    def open_dropped(self, paths: list[str]) -> None:
        """Files dropped on the icon: several with viewers of their own open in a row,
        anything else in one panel (a single file's viewer, or the listing)."""
        from pillow_assistant.ui.viewer_registry import side_by_side

        if not side_by_side(paths):
            self.coordinator.open_file_panel(paths)
            return
        if self.session is not None:
            for path in paths:
                self.session.add_reference(path)  # all of them go with the next question
        self.open_multi_windows({"views": [{"title": Path(p).name, "path": p} for p in paths], "layout": "row"})

    def open_multi_windows(self, payload: dict) -> None:
        from pillow_assistant.ui.viewer_registry import resolve

        views = payload.get("views") or []
        layout = payload.get("layout") or "row"
        if not views:
            return
        self.coordinator.close_multi_windows()
        if self.session is not None:
            # The files on screen go with the next question, without becoming chips.
            self.session.set_displayed([view.get("path") for view in views if view.get("path")])

        anchor_geometry = self.coordinator.anchor.frameGeometry()
        screen = QGuiApplication.screenAt(anchor_geometry.center()) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()

        windows = []
        for view in views:
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
            window.shown_path = path or None
            windows.append(window)

        # Each window opens at the size its content needs (what it chose for itself),
        # never more than its share of the screen, and the row is centred.
        boxes, bar_y = fit_windows(
            [(w.width(), w.height()) for w in windows],
            (available.x(), available.y(), available.width(), available.height()), layout)
        for window, (x, y, w, h) in zip(windows, boxes):
            window.destroyed.connect(
                lambda *_, item=window: self.coordinator.remove_multi_window(item)
            )
            window.setGeometry(QRect(x, y, w, h))
            window.show()
            window.raise_()
            self.coordinator.add_multi_window(window)

        if self.bus is not None and self.coordinator.multi_windows:
            anchor = QPoint(available.x() + (available.width() - 560) // 2, bar_y)
            self.coordinator.open_quick_input(anchor, close_displays=False)
            self.coordinator.park_quick_for_displays()

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
