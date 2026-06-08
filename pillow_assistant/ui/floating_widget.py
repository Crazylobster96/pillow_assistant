from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional, cast

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QWidget

from pillow_assistant.contracts import EventType, SurfaceLevel
from pillow_assistant.core import asr
from pillow_assistant.core.i18n import t
from pillow_assistant.ui.config_dialog import ModelConfigDialog
from pillow_assistant.ui.quick_input import QuickInputBar
from pillow_assistant.ui.radial_menu import RadialMenu
from pillow_assistant.ui.undo_toast import UndoToast
from pillow_assistant.ui.voice_capture import VoiceCapture
from storage import Storage


# Input FSM states.
IDLE = "idle"
PRESSED = "pressed"
RECORDING = "recording"
DRAGGING_SELF = "dragging_self"

LONG_PRESS_MS = 400
DRAG_THRESHOLD_PX = 8


class FloatingAssistant(QWidget):
    """Floating pillow icon driven by an input FSM.

    Gestures (needs doc FR-1/2/3/3R/4):
      * left click            -> text input bar
      * left long-press       -> voice capture (ASR lands in R1)
      * left drag             -> move the icon
      * right click           -> radial (fan) function menu
      * drop file(s)/folder   -> attach as session references + open input bar
    """

    # Emitted from the ASR worker thread with the transcribed text (or "").
    transcribed = Signal(str)
    # Emitted from the capture worker thread with the saved WAV path (or None).
    voice_saved = Signal(object)

    def __init__(self, storage: Storage, bus=None, session=None, vault=None,
                 project_store=None, undo_manager=None, ask_broker=None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.bus = bus
        self.session = session
        self.vault = vault
        self.project_store = project_store
        self.undo_manager = undo_manager
        self._projects_win = None
        self._undo_toast = None
        self._surface_win = None
        self._multi_wins: list = []  # windows opened by the present_windows tool
        self.ask_broker = ask_broker
        self._ask_dialog = None
        self.transcribed.connect(self._on_transcribed)
        self.voice_saved.connect(self._on_voice_saved)
        if bus is not None:
            bus.event.connect(self._on_bus_event)

        flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        self.setWindowFlags(flags)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)

        # FSM state.
        self._state = IDLE
        self._press_global = QPoint()
        self._drag_offset = QPoint()
        self._long_timer = QTimer(self)
        self._long_timer.setSingleShot(True)
        self._long_timer.timeout.connect(self._on_long_press)

        rec_dir = Path(self.storage.db_path).parent / "recordings"
        self._voice = VoiceCapture(rec_dir)
        # Probe the ASR backend now, off the GUI thread: the first available()
        # check imports funasr/torch (seconds) and used to freeze the UI when a
        # quick click registered as a long-press recording.
        threading.Thread(target=asr.warmup, name="pillow-asr-warmup", daemon=True).start()

        # Keep references to transient popups so they are not garbage-collected.
        self._quick: Optional[QuickInputBar] = None
        self._radial: Optional[RadialMenu] = None
        self._panel = None
        self._last_pos = None
        self._follow_offsets = None  # set while the icon itself is being dragged

        self._build_ui()

    # -- UI -----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.pillow_button = QPushButton(self)
        self.pillow_button.setIcon(QIcon(create_pillow_icon(96)))
        self.pillow_button.setIconSize(QSize(72, 72))
        self.pillow_button.setFixedSize(96, 96)
        self.pillow_button.setFlat(True)
        self.pillow_button.setCursor(Qt.PointingHandCursor)
        self.pillow_button.setToolTip(t("icon.tooltip"))
        self._apply_idle_style()
        layout.addWidget(self.pillow_button)
        self.pillow_button.installEventFilter(self)

    def _apply_idle_style(self) -> None:
        # Transparent backing so the drawn pillow icon stands on its own; a faint
        # ring appears only on hover.
        self.pillow_button.setStyleSheet(
            """
            QPushButton { border: none; background: transparent; border-radius: 38px; }
            QPushButton:hover { background: rgba(255,255,255,45); border-radius: 38px; }
            """
        )

    def _apply_recording_style(self) -> None:
        self.pillow_button.setStyleSheet(
            """
            QPushButton { border: 3px solid rgba(235,90,110,235); background: rgba(255,210,215,170);
                border-radius: 38px; }
            """
        )

    # -- FSM event handling -------------------------------------------------
    def eventFilter(self, obj, event):  # noqa: N802
        if obj is not self.pillow_button:
            return super().eventFilter(obj, event)

        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            me = cast(QMouseEvent, event)
            if me.button() == Qt.RightButton:
                self._open_radial_menu()
                return True
            if me.button() == Qt.LeftButton:
                self._state = PRESSED
                self._press_global = me.globalPosition().toPoint()
                self._drag_offset = self._press_global - self.frameGeometry().topLeft()
                self._long_timer.start(LONG_PRESS_MS)
                return True

        elif etype == QEvent.Type.MouseMove:
            me = cast(QMouseEvent, event)
            if not (me.buttons() & Qt.LeftButton):
                return False
            gpos = me.globalPosition().toPoint()
            if self._state == PRESSED:
                if (gpos - self._press_global).manhattanLength() > DRAG_THRESHOLD_PX:
                    self._long_timer.stop()
                    self._state = DRAGGING_SELF
                    self._capture_follow_offsets()
            if self._state == DRAGGING_SELF:
                self.move(gpos - self._drag_offset)
                return True

        elif etype == QEvent.Type.MouseButtonRelease:
            me = cast(QMouseEvent, event)
            if me.button() != Qt.LeftButton:
                return super().eventFilter(obj, event)
            self._long_timer.stop()
            if self._state == PRESSED:
                # Released quickly without moving -> a click. Toggle: if the
                # dialog bar is open, a click closes everything (back to the
                # bare icon); otherwise it opens the single dialog.
                self._state = IDLE
                self._close_radial()
                quick_open = False
                try:
                    quick_open = self._quick is not None and self._quick.isVisible()
                except RuntimeError:
                    quick_open = False
                if quick_open:
                    self._close_displays()
                else:
                    self._open_quick_input()
            elif self._state == RECORDING:
                self._state = IDLE
                self._finish_voice()
            else:  # DRAGGING_SELF
                self._state = IDLE
                self._follow_offsets = None  # back to delta mode when idle
            return True

        return super().eventFilter(obj, event)

    def _on_long_press(self) -> None:
        if self._state != PRESSED:
            return
        self._state = RECORDING
        self._close_radial()
        # Opening the audio device can block (PortAudio init / device scan) and
        # would freeze the whole UI if done on the GUI thread — do it in a worker.
        self._apply_recording_style()
        threading.Thread(target=self._voice.start, name="pillow-rec-start", daemon=True).start()

    def _finish_voice(self) -> None:
        self._apply_idle_style()

        # Stopping also concatenates buffers and writes the WAV; keep it off the
        # GUI thread, then hand the path back via a signal (runs on GUI thread).
        def stop_work() -> None:
            try:
                path = self._voice.stop()
            except Exception:
                path = None
            self.voice_saved.emit(path)

        threading.Thread(target=stop_work, name="pillow-rec-stop", daemon=True).start()

    def _on_voice_saved(self, path) -> None:
        bar = self._open_quick_input()
        if bar is None:
            return
        if not path:
            bar.prompt_edit.setPlaceholderText(t("voice.none"))
            return
        bar.prompt_edit.setPlaceholderText(t("voice.transcribing"))

        # available() may import funasr/torch (seconds) — keep it OFF the GUI
        # thread together with the transcription itself.
        def work() -> None:
            try:
                if not asr.available():
                    self.transcribed.emit("__NO_ASR__")
                    return
                text = asr.transcribe(path)
            except Exception:
                text = ""
            self.transcribed.emit(text)

        threading.Thread(target=work, name="pillow-asr", daemon=True).start()

    def _on_bus_event(self, ev) -> None:
        et = getattr(ev, "type", None)
        if et == EventType.UNDO:
            self._on_undo_event(ev)
        elif et == EventType.SURFACE:
            self._handle_surface(ev)
        elif et == EventType.ASK:
            self._handle_ask(ev)

    def _handle_ask(self, ev) -> None:
        if self.ask_broker is None:
            return
        meta = getattr(ev, "meta", None) or {}
        ask_id = meta.get("ask_id")
        if not ask_id:
            return
        from pillow_assistant.ui.ask_dialog import AskDialog

        if self._ask_dialog is not None:
            try:
                self._ask_dialog.close()
            except RuntimeError:
                pass

        def on_answer(result, _id=ask_id):
            self.ask_broker.resolve(_id, result)

        dlg = AskDialog(meta, on_answer)
        dlg.destroyed.connect(lambda *_, d=dlg: setattr(self, "_ask_dialog", None)
                              if self._ask_dialog is d else None)
        self._ask_dialog = dlg
        self._place_near_icon(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _request_has_view(self, request_id) -> bool:
        """True when an open view (quick input bar / file panel) is already
        streaming this request — that view IS the surface, so no extra window."""
        if not request_id:
            return False
        for w in (self._quick, self._panel):
            if w is None:
                continue
            try:
                if getattr(w, "_active_id", None) == request_id and w.isVisible():
                    return True
            except RuntimeError:  # underlying C++ widget already deleted
                continue
        return False

    def _handle_surface(self, ev) -> None:
        surface = getattr(ev, "surface", None)
        if surface is None:
            return
        # Explicit multi-window display (present_windows tool): always honor,
        # even while the answer streams in a view — the user asked for it.
        if getattr(surface, "kind", "") == "multi":
            self._open_multi_windows(getattr(surface, "payload", None) or {})
            return
        # The answer is already on screen in the bar/panel that sent the
        # request — opening an L5 window too would show it twice.
        if self._request_has_view(getattr(ev, "request_id", None)):
            return
        level = getattr(surface, "level", None)
        if level == SurfaceLevel.L1:
            self._flash_icon()
        elif level == SurfaceLevel.L5:
            self._open_surface_window(surface)

    def _open_multi_windows(self, payload: dict) -> None:
        """Open several display windows tiled side-by-side (row) or stacked
        (column). text items get a SurfaceMainWindow; path items get their
        type-adaptive preview panel. All are left-drag movable."""
        from pillow_assistant.ui.surface_window import SurfaceMainWindow
        from pillow_assistant.ui.viewer_registry import resolve

        views = payload.get("views") or []
        layout = payload.get("layout") or "row"
        if not views:
            return
        for w in self._multi_wins:  # replace the previous set
            try:
                w.close()
            except RuntimeError:
                pass
        self._multi_wins = []

        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        n = len(views)
        gap = 8
        bar_space = 170  # room kept at the bottom for the single shared dialog bar
        if layout == "column":
            w = int(avail.width() * 0.62)
            h = (avail.height() - bar_space - gap * (n - 1)) // n
            x0 = avail.x() + (avail.width() - w) // 2
            rects = [QRect(x0, avail.y() + i * (h + gap), w, h) for i in range(n)]
            bar_y = avail.y() + n * (h + gap) + 4
        else:
            w = (avail.width() - gap * (n - 1)) // n
            h = int(avail.height() * 0.72)
            y0 = avail.y() + max(0, (avail.height() - bar_space - h) // 2)
            rects = [QRect(avail.x() + i * (w + gap), y0, w, h) for i in range(n)]
            bar_y = y0 + h + gap

        for view, rect in zip(views, rects):
            path = view.get("path")
            try:
                if path:
                    panel_cls = resolve([path])
                    # Preview-only: the group shares ONE dialog bar (below).
                    win = panel_cls([path], self.storage, self.bus, self.session,
                                    show_dialog=False)
                else:
                    win = SurfaceMainWindow(view.get("text", "") or "", [], "",
                                            title=view.get("title") or t("multi.content_title"))
            except Exception as exc:
                # Never drop a window silently — show what failed instead.
                try:
                    win = SurfaceMainWindow(t("multi.open_failed", path=path or "", err=exc), [], "",
                                            title=view.get("title") or t("multi.open_failed_title"))
                except Exception:
                    continue
            win.destroyed.connect(lambda *_, w=win: self._on_multi_destroyed(w))
            win.setGeometry(rect)
            win.show()
            win.raise_()
            self._multi_wins.append(win)

        # One shared dialog bar for the whole group.
        if self.bus is not None and self._multi_wins:
            if self._quick is not None:
                try:
                    self._quick.close()
                except RuntimeError:
                    pass
            anchor = QPoint(avail.x() + (avail.width() - 560) // 2, bar_y)
            bar = QuickInputBar(self.storage, self.bus, self.session, anchor_global=anchor,
                                open_reference=lambda p: self._open_file_panel([p]),
                                history=self._recent_history())
            self._quick = bar
            bar.show()
            bar.raise_()
            bar.activateWindow()
            bar.prompt_edit.setFocus()

    def _on_multi_destroyed(self, w) -> None:
        self._multi_wins = [x for x in self._multi_wins if x is not w]

    def _flash_icon(self) -> None:
        self.pillow_button.setStyleSheet(
            "QPushButton { border: 3px solid rgba(120,200,140,235); background: rgba(210,255,220,150);"
            " border-radius: 38px; }"
        )
        QTimer.singleShot(600, self._apply_idle_style)

    def _open_surface_window(self, surface) -> None:
        from pillow_assistant.ui.surface_window import SurfaceMainWindow

        if self._surface_win is not None:
            try:
                self._surface_win.close()
            except RuntimeError:
                pass
        payload = getattr(surface, "payload", None) or {}
        win = SurfaceMainWindow(getattr(surface, "body", "") or "",
                                payload.get("artifacts", []), payload.get("workspace", ""))
        win.destroyed.connect(lambda *_, w=win: setattr(self, "_surface_win", None) if self._surface_win is w else None)
        self._surface_win = win
        self._place_near_icon(win)
        win.show()
        win.raise_()
        win.activateWindow()

    def _on_undo_event(self, ev) -> None:
        if getattr(ev, "type", None) != EventType.UNDO or self.undo_manager is None:
            return
        token = (getattr(ev, "meta", None) or {}).get("token")
        if not token:
            return
        if self._undo_toast is not None:
            try:
                self._undo_toast.close()
            except RuntimeError:
                pass
        toast = UndoToast(ev.text or t("undo.default_label"),
                          lambda tok=token: self.undo_manager.undo(tok))
        self._undo_toast = toast
        self._place_near_icon(toast)
        toast.show()
        toast.raise_()

    def _on_transcribed(self, text: str) -> None:
        if self._quick is None:
            return
        if text == "__NO_ASR__":
            self._quick.prompt_edit.setPlaceholderText(t("voice.no_asr"))
        elif text:
            self._quick.prompt_edit.setText(text)
            self._quick.prompt_edit.setFocus()
        else:
            self._quick.prompt_edit.setPlaceholderText(t("voice.failed"))

    # -- positioning --------------------------------------------------------
    def _followers(self) -> list:
        return [self._quick, self._panel, self._radial, self._projects_win,
                self._surface_win, self._undo_toast, self._ask_dialog] + list(self._multi_wins)

    def _place_near_icon(self, w) -> None:
        """Place a transient window (undo toast / result window / ask dialog)
        beside the icon: stacked below earlier ones instead of overlapping,
        flipped to the left side when the right edge has no room, and clamped
        on-screen."""
        w.adjustSize()
        fg = self.frameGeometry()
        scr = QGuiApplication.screenAt(fg.center()) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        x = fg.right() + 12
        if x + w.width() > g.right():
            x = max(g.left(), fg.left() - w.width() - 12)
        y = fg.top() + 4
        for other in (self._undo_toast, self._ask_dialog, self._surface_win):
            if other is None or other is w:
                continue
            try:
                if other.isVisible():
                    og = other.frameGeometry()
                    if abs(og.x() - x) < max(og.width(), w.width()):
                        y = max(y, og.bottom() + 8)  # stack below, don't overlap
            except RuntimeError:
                continue
        x = min(max(g.left(), x), max(g.left(), g.right() - w.width()))
        y = min(max(g.top(), y), max(g.top(), g.bottom() - w.height()))
        w.move(x, y)

    def _capture_follow_offsets(self) -> None:
        """Pin each popup's offset to the icon at drag start. During the drag we
        re-place popups at icon_pos + offset (absolute), instead of accumulating
        per-event deltas — deltas go wrong across monitors with different DPI
        (the OS remaps window geometry mid-transition, producing huge jumps)."""
        offsets = []
        for w in self._followers():
            if w is None:
                continue
            try:
                if w.isVisible():
                    offsets.append((w, w.pos() - self.pos()))
            except RuntimeError:
                continue
        self._follow_offsets = offsets

    def moveEvent(self, event):  # noqa: N802
        # Move every open popup with the icon, so the icon and all its windows
        # (input bar / file panel / radial menu / projects) drag together and
        # keep their relative layout.
        super().moveEvent(event)
        new_pos = self.pos()
        old_pos = self._last_pos
        self._last_pos = new_pos
        # Anchored mode (during an icon drag): absolute offsets, DPI-safe.
        if getattr(self, "_follow_offsets", None):
            for w, off in self._follow_offsets:
                try:
                    if w.isVisible():
                        w.move(new_pos + off)
                except RuntimeError:
                    continue
            return
        if old_pos is None:
            return
        delta = new_pos - old_pos
        if delta.isNull():
            return
        followers = self._followers()
        for w in followers:
            if w is None:
                continue
            try:
                if w.isVisible():
                    w.move(w.pos() + delta)
            except RuntimeError:
                # A followed popup was torn down mid-drag; ignore.
                continue

    # -- popups -------------------------------------------------------------
    def _anchor_point(self) -> QPoint:
        return self.frameGeometry().bottomLeft() + QPoint(0, 6)

    def _recent_history(self, max_turns: int = 8) -> list:
        """Recent turns of the conversation this session is bound to: the
        project session if one is active, else the one-off chat history."""
        try:
            pid = getattr(self.session, "project_id", None)
            if pid and self.project_store is not None:
                project = self.project_store.get(pid)
                if project is not None:
                    sid = getattr(self.session, "session_id", None)
                    return self.project_store.load_history(project, sid, max_turns=max_turns)
            turns = []
            path = Path.home() / ".pillow" / "chat" / "history.jsonl"
            for line in path.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("role") and obj.get("content") is not None:
                    turns.append({"role": obj["role"], "content": str(obj["content"])})
            return turns[-max_turns:]
        except (OSError, ValueError):
            return []
        except Exception:
            return []

    def _open_quick_input(self) -> Optional[QuickInputBar]:
        if self.bus is None:
            return None
        # A click on the icon restores the single-dialog view: close every
        # display window (panels / result windows / multi-window sets) first.
        self._close_displays()
        bar = QuickInputBar(self.storage, self.bus, self.session, anchor_global=self._anchor_point(),
                            open_reference=lambda p: self._open_file_panel([p]),
                            history=self._recent_history())
        self._quick = bar
        bar.show()
        bar.raise_()
        bar.activateWindow()
        bar.prompt_edit.setFocus()
        return bar

    def _open_radial_menu(self) -> None:
        center = self.frameGeometry().center()
        items = [
            (t("menu.projects"), self._open_projects),
            (t("menu.config"), self._open_config),
            (t("menu.close_displays"), self._close_displays),
            (t("menu.clear_refs"), self._clear_references),
            (t("menu.quit"), self._quit),
        ]
        if self._radial is not None:
            self._radial.close()
        # If the input bar is open, fan away from it so the buttons don't overlap.
        avoid = self._quick.frameGeometry() if (self._quick is not None and self._quick.isVisible()) else None
        self._radial = RadialMenu(items, center, avoid_rect=avoid,
                                  exclude_rect_getter=lambda: self.frameGeometry(),
                                  make_room=self._make_room_for_menu)
        self._radial.show()
        self._radial.raise_()

    def _make_room_for_menu(self):
        """Last-resort collision fix: shift the input bar out of the fan's ring
        around the icon, then return its new frameGeometry (or None)."""
        bar = self._quick
        if bar is None or not bar.isVisible():
            return None
        center = self.frameGeometry().center()
        need = RadialMenu.RADIUS + RadialMenu.BTN + 24  # clearance ring radius
        scr = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry()
        g = bar.frameGeometry()
        ny = center.y() + need  # prefer pushing the bar below the ring
        if ny + g.height() > avail.bottom():
            ny = max(avail.top(), center.y() - need - g.height())  # else above
        nx = min(max(g.x(), avail.left()), max(avail.left(), avail.right() - g.width()))
        bar.move(nx, ny)
        return bar.frameGeometry()

    def _close_radial(self) -> None:
        if self._radial is not None:
            self._radial.close()
            self._radial = None

    def _close_displays(self) -> None:
        """Close every open display window (file panels, result windows,
        multi-window sets and the input bar) in one click."""
        wins = [self._panel, self._surface_win, self._quick] + list(self._multi_wins)
        self._panel = None
        self._surface_win = None
        self._quick = None
        self._multi_wins = []
        for w in wins:
            if w is None:
                continue
            try:
                w.close()
            except RuntimeError:
                pass

    def _open_projects(self) -> None:
        if self.project_store is None:
            return
        from pillow_assistant.ui.projects_panel import ProjectsPanel

        if self._projects_win is not None:
            try:
                self._projects_win.close()
            except RuntimeError:
                pass
        win = ProjectsPanel(self.project_store)
        win.destroyed.connect(lambda *_, w=win: self._on_projects_destroyed(w))
        self._projects_win = win
        win.move(self.frameGeometry().center())
        win.show()
        win.raise_()
        win.activateWindow()

    def _on_projects_destroyed(self, win) -> None:
        if self._projects_win is win:
            self._projects_win = None

    def _open_config(self) -> None:
        ModelConfigDialog(storage=self.storage, vault=self.vault, parent=self).exec()

    def _clear_references(self) -> None:
        if self.session is not None:
            self.session.clear()
        if self._quick is not None:
            self._quick._refresh_chips()

    def _quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # -- drag & drop: any file/folder becomes a session reference ----------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        event.acceptProposedAction()
        if paths:
            self._open_file_panel(paths)

    def _open_file_panel(self, paths: list[str]) -> None:
        # Pick a type-adaptive preview panel; it adds the paths to the session
        # references and carries them to the Agent on submit (R2).
        from pillow_assistant.ui.viewer_registry import resolve

        if self._quick is not None:
            self._quick.close()
            self._quick = None
        old = self._panel
        self._panel = None
        if old is not None:
            try:
                old.close()
            except RuntimeError:
                pass
        panel_cls = resolve(paths)
        panel = panel_cls(paths, self.storage, self.bus, self.session,
                          anchor_global=self._anchor_point(),
                          history=self._recent_history())
        # Clear our ref only if THIS panel is still the current one when it dies
        # (an old panel's async destroyed() must not null a newer panel).
        panel.destroyed.connect(lambda *_, p=panel: self._on_panel_destroyed(p))
        self._panel = panel
        panel.show()
        panel.raise_()
        panel.activateWindow()
        panel.prompt_edit.setFocus()

    def _on_panel_destroyed(self, panel) -> None:
        if self._panel is panel:
            self._panel = None


# Helper builders -----------------------------------------------------------------
def create_pillow_icon(size: int) -> QPixmap:
    """A soft plush pillow with a gentle cool gradient and a sleepy "zzz"."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)

    rect = QRectF(size * 0.13, size * 0.15, size * 0.74, size * 0.70)
    radius = size * 0.30
    center = rect.center()

    # Soft ambient glow.
    glow = QRadialGradient(center, size * 0.52)
    glow.setColorAt(0.0, QColor(125, 155, 205, 70))
    glow.setColorAt(1.0, QColor(125, 155, 205, 0))
    p.setBrush(glow)
    p.drawEllipse(QRectF(0, 0, size, size))

    # Drop shadow beneath the pillow.
    shadow = QPainterPath()
    shadow.addRoundedRect(rect.translated(0, size * 0.045), radius, radius)
    sg = QRadialGradient(QPointF(center.x(), center.y() + size * 0.05), radius * 1.4)
    sg.setColorAt(0.0, QColor(30, 50, 80, 95))
    sg.setColorAt(1.0, QColor(30, 50, 80, 0))
    p.setBrush(sg)
    p.drawPath(shadow)

    # Plush body with a cool top-down gradient.
    body = QPainterPath()
    body.addRoundedRect(rect, radius, radius)
    bg = QLinearGradient(rect.topLeft(), rect.bottomRight())
    bg.setColorAt(0.0, QColor(255, 255, 255, 255))
    bg.setColorAt(0.55, QColor(228, 238, 250, 252))
    bg.setColorAt(1.0, QColor(186, 208, 238, 246))
    p.setBrush(bg)
    p.drawPath(body)

    # Top glossy highlight.
    gloss = rect.adjusted(size * 0.06, size * 0.06, -size * 0.06, -size * 0.40)
    if gloss.height() > 0:
        gp = QPainterPath()
        gp.addRoundedRect(gloss, radius * 0.7, radius * 0.5)
        gg = QLinearGradient(gloss.topLeft(), gloss.bottomLeft())
        gg.setColorAt(0.0, QColor(255, 255, 255, 215))
        gg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(gg)
        p.drawPath(gp)

    # Dotted stitched seam.
    seam = rect.adjusted(size * 0.095, size * 0.095, -size * 0.095, -size * 0.095)
    seam_pen = QPen(QColor(150, 182, 222, 150))
    seam_pen.setWidthF(max(1.0, size * 0.016))
    seam_pen.setStyle(Qt.DotLine)
    p.setPen(seam_pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(seam, radius * 0.6, radius * 0.6)

    # Crisp soft border.
    border_pen = QPen(QColor(150, 182, 216, 200))
    border_pen.setWidthF(max(1.0, size * 0.026))
    p.setPen(border_pen)
    p.drawPath(body)

    # Sleepy "z z Z" rising to the upper-right.
    p.setPen(QColor(96, 128, 190, 240))
    for fx, fy, fs, ch in ((0.34, 0.55, 0.15, "z"), (0.47, 0.47, 0.20, "z"), (0.60, 0.38, 0.27, "Z")):
        font = QFont("Segoe UI", max(6, int(size * fs)))
        font.setBold(True)
        font.setItalic(True)
        p.setFont(font)
        p.drawText(QPointF(size * fx, size * fy), ch)

    p.end()
    return pixmap


def create_microphone_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    center = pixmap.rect().center()

    body_rect = pixmap.rect().adjusted(size * 0.35, size * 0.2, -size * 0.35, -size * 0.2)
    path = QPainterPath()
    path.addRoundedRect(body_rect, size * 0.2, size * 0.2)
    painter.fillPath(path, QColor(255, 255, 255, 220))

    stem_top = QPoint(center.x(), int(size * 0.7))
    painter.setPen(QColor(255, 255, 255, 220))
    painter.drawLine(stem_top, QPoint(center.x(), int(size * 0.9)))

    base_rect = pixmap.rect().adjusted(size * 0.35, int(size * 0.88), -size * 0.35, -int(size * 0.05))
    base_path = QPainterPath()
    base_path.addRoundedRect(base_rect, size * 0.1, size * 0.1)
    painter.fillPath(base_path, QColor(255, 255, 255, 220))

    painter.end()
    return pixmap


def create_keyboard_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    base_rect = pixmap.rect().adjusted(int(size * 0.08), int(size * 0.28), -int(size * 0.08), -int(size * 0.2))
    base_radius = size * 0.18
    base_path = QPainterPath()
    base_path.addRoundedRect(base_rect, base_radius, base_radius)

    base_gradient = QLinearGradient(QPointF(base_rect.topLeft()), QPointF(base_rect.bottomLeft()))
    base_gradient.setColorAt(0.0, QColor(235, 240, 247, 245))
    base_gradient.setColorAt(1.0, QColor(194, 206, 224, 245))
    painter.setPen(Qt.NoPen)
    painter.setBrush(base_gradient)
    painter.drawPath(base_path)

    top_glow = QLinearGradient(QPointF(base_rect.topLeft()), QPointF(base_rect.bottomLeft()))
    top_glow.setColorAt(0.0, QColor(255, 255, 255, 160))
    top_glow.setColorAt(0.6, QColor(255, 255, 255, 0))
    painter.setBrush(top_glow)
    painter.drawPath(base_path)

    base_border = QPen(QColor(122, 142, 170, 220))
    base_border.setWidthF(max(1.0, size * 0.025))
    painter.setPen(base_border)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(base_path)

    # Key layout
    key_radius = size * 0.08
    key_w = size * 0.15
    key_h = size * 0.17
    spacing_x = size * 0.06
    spacing_y = size * 0.08
    start_x = base_rect.left() + size * 0.12
    start_y = base_rect.top() + size * 0.12

    key_border = QPen(QColor(120, 140, 170, 220))
    key_border.setWidthF(max(0.9, size * 0.018))

    for row in range(2):
        for col in range(4):
            x = start_x + col * (key_w + spacing_x)
            y = start_y + row * (key_h + spacing_y)
            key_rect = QRectF(x, y, key_w, key_h)
            key_path = QPainterPath()
            key_path.addRoundedRect(key_rect, key_radius, key_radius)

            key_gradient = QLinearGradient(QPointF(key_rect.topLeft()), QPointF(key_rect.bottomLeft()))
            key_gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
            key_gradient.setColorAt(1.0, QColor(205, 212, 228, 255))

            painter.setPen(Qt.NoPen)
            painter.setBrush(key_gradient)
            painter.drawPath(key_path)

            painter.setPen(key_border)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(key_path)

    # Space bar with accent.
    space_rect = QRectF(
        base_rect.left() + size * 0.22,
        base_rect.bottom() - key_h - size * 0.2,
        base_rect.width() - size * 0.44,
        key_h * 0.85,
    )
    space_path = QPainterPath()
    space_path.addRoundedRect(space_rect, key_radius, key_radius)
    space_gradient = QLinearGradient(QPointF(space_rect.topLeft()), QPointF(space_rect.bottomLeft()))
    space_gradient.setColorAt(0.0, QColor(140, 170, 210, 255))
    space_gradient.setColorAt(1.0, QColor(90, 130, 190, 255))

    painter.setPen(Qt.NoPen)
    painter.setBrush(space_gradient)
    painter.drawPath(space_path)

    space_border = QPen(QColor(70, 100, 150, 230))
    space_border.setWidthF(max(1.0, size * 0.02))
    painter.setPen(space_border)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(space_path)

    painter.end()
    return pixmap


def create_close_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    rect = pixmap.rect().adjusted(int(size * 0.08), int(size * 0.08), -int(size * 0.08), -int(size * 0.08))
    radius = rect.width() / 2
    center = QPointF(rect.center())

    # Soft dual-tone background.
    bg_gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    bg_gradient.setColorAt(0.0, QColor(255, 140, 150, 245))
    bg_g