"""Linux on-screen keyboard (Squeekboard) support for touchscreen HMI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

OSK_DBUS_NAME = "sm.puri.OSK0"
OSK_DBUS_PATH = "/sm/puri/OSK0"
OSK_DBUS_IFACE = "sm.puri.OSK0"


def osk_enabled() -> bool:
    """True when running on Linux and OSK has not been disabled via env."""
    if not sys.platform.startswith("linux"):
        return False
    flag = os.environ.get("STABATHA_OSK", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _ensure_squeekboard_running() -> None:
    if shutil.which("squeekboard") is None:
        return
    try:
        subprocess.run(
            ["pgrep", "-x", "squeekboard"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        subprocess.Popen(
            ["squeekboard"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _dbus_set_visible(visible: bool) -> bool:
    try:
        import dbus  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        bus = dbus.SessionBus()
        proxy = bus.get_object(OSK_DBUS_NAME, OSK_DBUS_PATH)
        iface = dbus.Interface(proxy, OSK_DBUS_IFACE)
        iface.SetVisible(visible)
        return True
    except Exception:
        return False


def _busctl_set_visible(visible: bool) -> bool:
    busctl = shutil.which("busctl")
    if not busctl:
        return False
    try:
        subprocess.run(
            [
                busctl,
                "call",
                "--user",
                OSK_DBUS_NAME,
                OSK_DBUS_PATH,
                OSK_DBUS_IFACE,
                "SetVisible",
                "b",
                "true" if visible else "false",
            ],
            check=True,
            timeout=2,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def show_osk() -> None:
    """Start Squeekboard if needed and show the keyboard panel."""
    if not osk_enabled():
        return
    _ensure_squeekboard_running()
    if not _dbus_set_visible(True):
        _busctl_set_visible(True)


def _on_text_widget_activate(_event=None) -> str | None:
    show_osk()
    return None


def bind_osk(widget: tk.Misc) -> None:
    """Show Squeekboard when the widget is clicked or receives focus."""
    if not osk_enabled():
        return
    widget.bind("<Button-1>", _on_text_widget_activate, add="+")
    widget.bind("<FocusIn>", _on_text_widget_activate, add="+")


def _is_editable_text_widget(widget: tk.Misc) -> bool:
    if isinstance(widget, (ttk.Entry, ttk.Spinbox)):
        return True
    if isinstance(widget, tk.Text):
        try:
            return str(widget.cget("state")) in ("normal", "")
        except tk.TclError:
            return False
    return False


def bind_osk_tree(root: tk.Misc) -> None:
    """Bind OSK handlers to all editable Entry, Spinbox, and Text widgets under root."""
    if not osk_enabled():
        return
    stack = [root]
    while stack:
        widget = stack.pop()
        if _is_editable_text_widget(widget):
            bind_osk(widget)
        try:
            stack.extend(widget.winfo_children())
        except tk.TclError:
            pass
