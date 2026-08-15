"""Windows acrylic (frosted-glass) blur-behind for frameless popups.

Qt has no cross-platform blur-behind, so on Windows 10/11 we call the
(undocumented but widely used) ``SetWindowCompositionAttribute`` with
``ACCENT_ENABLE_ACRYLICBLURBEHIND``. The window must be frameless and have a
translucent background for the blur to show through.

``enable_acrylic`` is a no-op that returns False on non-Windows or on any
failure, so callers can fall back to a solid theme.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Signal


class _GlassThemeSignals(QObject):
    opacity_changed = Signal(int)


glass_theme_changed = _GlassThemeSignals()


def notify_glass_opacity(opacity: int) -> None:
    """Notify open UI windows after the setting is persisted."""
    glass_theme_changed.opacity_changed.emit(opacity)

def glass_opacity() -> int:
    """Return the persisted white-glass opacity percentage."""
    from pillow_assistant.core.settings import load_settings
    try:
        return max(10, min(95, int(load_settings().get("surface_glass_opacity", 68))))
    except (TypeError, ValueError):
        return 68


def white_acrylic_color(opacity: int) -> int:
    """Convert an opacity percentage to Windows AABBGGRR white tint."""
    # Keep the compositor tint subtle: Qt paints the visible glass layer.
    # Using the full opacity here would stack two white layers.
    tint_alpha = max(1, round(max(10, min(95, opacity)) * 0.12))
    return (tint_alpha << 24) | 0x00FFFFFF

def enable_acrylic(widget, gradient_color: int = 0x99201A16) -> bool:
    """Enable acrylic blur behind ``widget``. Returns True on success.

    ``gradient_color`` is 0xAABBGGRR: alpha + blue + green + red. The default is
    a dark tint at ~60% alpha, which keeps light text readable over the blur.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = int(widget.winId())

        class ACCENTPOLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.POINTER(ACCENTPOLICY)),
                ("SizeOfData", ctypes.c_size_t),
            ]

        accent = ACCENTPOLICY()
        accent.AccentState = 4  # ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.AccentFlags = 2  # draw the gradient tint
        accent.GradientColor = gradient_color & 0xFFFFFFFF

        data = WINCOMPATTRDATA()
        data.Attribute = 19  # WCA_ACCENT_POLICY
        data.SizeOfData = ctypes.sizeof(accent)
        data.Data = ctypes.pointer(accent)

        set_wca = ctypes.windll.user32.SetWindowCompositionAttribute
        set_wca.argtypes = [wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
        set_wca(wintypes.HWND(hwnd), ctypes.byref(data))
        return True
    except Exception:
        return False
