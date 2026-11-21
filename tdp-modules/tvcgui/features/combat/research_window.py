"""Standalone access to the existing read-only Research recorder."""
from __future__ import annotations

import tkinter as tk

from tvcgui.core.tk_host import tk_call
from .research_capture_panel import ResearchCapturePanel

_window = None


def open_research_recorder(hud_provider=None):
    """Open or focus the single recorder on the shared Tk host."""
    def create(master):
        global _window
        if _window is not None:
            try:
                if _window.winfo_exists():
                    _window.lift()
                    _window.focus_force()
                    return
            except tk.TclError:
                pass
        window = tk.Toplevel(master)
        window.title('Research Recorder')
        window.configure(bg='#0E1728')
        window.geometry('1120x700')
        panel = ResearchCapturePanel(window, 'P1-C1', hud_provider)
        _window = window

        def close():
            global _window
            panel.close()
            _window = None
            window.destroy()

        window.protocol('WM_DELETE_WINDOW', close)

    tk_call(create)
