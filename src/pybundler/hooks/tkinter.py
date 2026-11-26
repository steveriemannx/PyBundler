"""Built-in hook: ship Tcl/Tk when the application uses tkinter.

By default the runtime collector drops ``tkinter`` and the Tcl/Tk libraries to
keep bundles small.  Importing tkinter (statically or through a hidden import)
switches the runtime feature on.
"""

PACKAGES = ["tkinter", "_tkinter"]

HIDDEN_IMPORTS = ["tkinter", "_tkinter"]


def hook(ctx):
    ctx.feature("tcl-tk")
