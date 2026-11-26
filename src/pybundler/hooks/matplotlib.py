"""Built-in hook: detect a GUI backend that needs tkinter.

Matplotlib chooses its backend at runtime.  When the TkAgg backend was imported
explicitly but tkinter was excluded, the resulting bundle fails only when the
first figure is drawn, which is hard to debug.  Catch it at build time instead.
"""

PACKAGES = ["matplotlib"]


def hook(ctx):
    if not ctx.included("matplotlib.backends.backend_tkagg"):
        return
    if ctx.included("tkinter"):
        ctx.feature("tcl-tk")
        return
    ctx.hidden_import("tkinter")
    ctx.warn(
        "the TkAgg backend is used but tkinter was not included; "
        "adding it automatically (use `exclude = [\"tkinter\"]` to override)"
    )
