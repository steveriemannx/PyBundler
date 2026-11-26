# Hooks

PyBundler copies imported packages **whole**, so most packages need no hook at
all. Hooks are for the cases static analysis cannot know about:

* modules imported through computed names (`importlib.import_module(name)`),
* files that live outside the package directory (shared data, plugins),
* native libraries that are loaded by name at runtime,
* optional runtime features (`tkinter` → Tcl/Tk),
* namespace packages where only imported files are copied by default.

## Loading

* **Built-in hooks** live in `src/pybundler/hooks/` and apply when their
  filename matches an included top-level package (`tkinter.py`,
  `matplotlib.py`).
* **User hooks** are listed in the spec:

  ```toml
  [collect]
  hooks = ["build/hooks/acme.py"]
  ```

  or discovered with `--hook-dir build/hooks` (every `*.py` in that directory).
  A user hook applies to the package names in its `PACKAGES` list, or to its
  filename stem when `PACKAGES` is absent. User hooks run after built-in ones.

Hooks are imported from their file path — they do not need to be a package and
must not rely on relative imports. An exception inside a hook fails the build
with the hook path in the message.

## Declarative form

```python
# build/hooks/acme.py
PACKAGES = ["acme"]

HIDDEN_IMPORTS = [
    "acme.transports.udp",       # chosen from a string in config
    "acme.transports.unix",
]

DATA = [
    ("acme/data", "acme/data"),  # relative to the package directory
]

BINARIES = [
    ("acme/lib/libacme.dylib", "lib/libacme.dylib"),
]
```

`DATA`/`BINARIES` entries are either `"src"` (destination mirrors the source) or
`("src", "dest")`. Missing paths produce a warning rather than a hard failure,
because data may be optional on some platforms.

## Function form

```python
# build/hooks/acme.py
PACKAGES = ["acme"]

def hook(ctx):
    # Add imports that only appear in a config file or a database.
    ctx.hidden_import("acme.transports.udp")

    # Ship a font that the package reads from sys.prefix/share.
    ctx.add_data("/usr/local/share/acme/fonts", "acme/fonts")

    # Request an optional runtime feature.
    ctx.feature("tcl-tk")

    # Import every submodule of a namespace package.
    ctx.collect_all("acme.plugins")

    # Exclude something discovered late.
    ctx.exclude("acme.tests")

    if not ctx.included("acme.speedups"):
        ctx.warn("acme is running without its C speedups")
```

### `HookContext` API

| Member | Description |
| --- | --- |
| `ctx.hidden_import(*names)` | Enqueue modules for analysis. |
| `ctx.collect_all(package=None)` | Enqueue every submodule of a package (defaults to the hooked package). |
| `ctx.exclude(*names)` | Drop modules (same patterns as `collect.exclude`). |
| `ctx.has(module)` | True if the module exists in the build environment or stdlib. |
| `ctx.included(module)` | True if the module/top-level package is already in the graph. |
| `ctx.package_dir(package=None)` | Absolute path of a package; raises if missing. |
| `ctx.add_data(source, dest=None)` | Ship a file/directory. Relative sources resolve against the package directory. |
| `ctx.add_binary(source, dest=None)` | Ship a native file (default destination `bin/<name>`). |
| `ctx.feature(name)` | Enable a runtime feature. Currently `tcl-tk`. |
| `ctx.warn(message)` | Add a build warning. |

`HookContext` methods only record requests; they are committed after the hook
function returns. Declarative attributes (`HIDDEN_IMPORTS`, `DATA`,
`BINARIES`) are applied before `hook()` runs.

## Built-in hooks

| Hook | Why it exists |
| --- | --- |
| `tkinter.py` | Enables the `tcl-tk` feature so the runtime collector keeps `tkinter`, Tcl/Tk libraries and the Tcl data directory. Runs automatically when `tkinter`/`_tkinter` is imported. |
| `matplotlib.py` | Matplotlib selects a backend at runtime. If the TkAgg backend is included but `tkinter` was not, the hook adds it and warns instead of failing when the first figure is drawn. |

## Debugging a hook

```bash
pybundler inspect --missing          # unresolved imports and who imported them
pybundler -v build                   # shows "running hook ... for ..." lines
pybundler inspect --files            # every planned bundle path
```

Hooks run after the initial graph walk, so a hook only executes if its package
is already included. To force a package in, add it to `collect.packages`; that
also applies the matching hook.

## Conventions

* Keep hooks small and explain *why* with a comment or `ctx.warn`.
* Prefer `ctx.collect_all()` over listing submodules by hand.
* Do not hard-code absolute paths; use `ctx.package_dir()` and `ctx.add_data`.
* If a hook becomes generically useful, contribute it as a built-in and add a
  test in `tests/`.
