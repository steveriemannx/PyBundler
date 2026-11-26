from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from greet import greeting


def resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    banner_path = resource_root() / "assets" / "banner.txt"
    data = {
        "greeting": greeting("PyBundler"),
        "argv": arguments,
        "argv0": sys.argv[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "mode": (getattr(sys, "pybundler", {}) or {}).get("mode"),
        "banner": banner_path.read_text(encoding="utf-8").strip(),
        "python": sys.version.split()[0],
        "stdlib": json.__file__ is not None,
        "env": os.environ.get("PYBUNDLER_EXAMPLE"),
    }
    print(json.dumps(data, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
