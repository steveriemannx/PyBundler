"""Tkinter hello world - the GUI example for PyBundler.

This example opens a window and keeps running until it is closed.  It is built
with ``console = false``, so on Windows it starts without a console window.

    cd examples/hello
    pybundler build
    ./dist/hello/hello            (dist\\hello\\hello.exe on Windows)
"""

from __future__ import annotations

import platform
import sys
import tkinter as tk
from tkinter import ttk


def bundle_info() -> str:
    """Describe how this process was started (source tree vs frozen bundle)."""
    frozen = bool(getattr(sys, "frozen", False))
    mode = (getattr(sys, "pybundler", {}) or {}).get("mode", "source")
    return f"frozen={frozen}   mode={mode}   Python {platform.python_version()}"


class HelloApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PyBundler Hello")
        self.geometry("500x260")
        self.minsize(440, 240)
        self.clicks = 0

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Hello from PyBundler + Tkinter!",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(frame, text=bundle_info()).pack(anchor="w", pady=(8, 18))
        self.counter = ttk.Label(frame, text="Clicks: 0")
        self.counter.pack(anchor="w", pady=(0, 14))

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="Click me", command=self.on_click).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="Quit", command=self.destroy).pack(side="left")

    def on_click(self) -> None:
        self.clicks += 1
        self.counter.configure(text=f"Clicks: {self.clicks}")


def main() -> int:
    HelloApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
