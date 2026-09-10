#!/usr/bin/env python3
"""Rewrite WebKitGTK's compile-time helper directory to a same-length /tmp path.

Release WebKitGTK builds ignore WEBKIT_EXEC_PATH, so AppRun cannot redirect
WebKitNetworkProcess with an environment variable. The baked-in prefix
``/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1`` is replaced with
``/tmp/.dc/x86_64-linux-gnu/webkit2gtk-4.1`` (same byte length). AppRun then
symlinks that location to the helpers inside the AppImage.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

OLD_LIBEXEC = b"/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1"
NEW_LIBEXEC = b"/tmp/.dc/x86_64-linux-gnu/webkit2gtk-4.1"

# Keep a 4.0 variant for Ubuntu builders that only ship WebKit 4.0.
OLD_LIBEXEC_40 = b"/usr/lib/x86_64-linux-gnu/webkit2gtk-4.0"
NEW_LIBEXEC_40 = b"/tmp/.dc/x86_64-linux-gnu/webkit2gtk-4.0"


def _replace_same_length(blob: bytes, old: bytes, new: bytes) -> tuple[bytes, int]:
    if len(old) != len(new):
        raise ValueError(f"replacement length {len(new)} != original {len(old)}")
    hits = blob.count(old)
    if hits == 0:
        return blob, 0
    return blob.replace(old, new), hits


def relocate_webkit_library(path: Path) -> int:
    blob = path.read_bytes()
    total = 0
    for old, new in ((OLD_LIBEXEC, NEW_LIBEXEC), (OLD_LIBEXEC_40, NEW_LIBEXEC_40)):
        blob, hits = _replace_same_length(blob, old, new)
        total += hits
    if total == 0:
        raise SystemExit(
            f"relocate-webkit: no WebKit libexec path in {path} "
            "(expected /usr/lib/x86_64-linux-gnu/webkit2gtk-4.1 or 4.0)"
        )
    path.write_bytes(blob)
    print(f"relocate-webkit: patched {total} path(s) in {path}")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path, help="bundled libwebkit2gtk-*.so.0")
    args = parser.parse_args(argv)
    if not args.library.is_file():
        raise SystemExit(f"relocate-webkit: not a file: {args.library}")
    relocate_webkit_library(args.library)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
