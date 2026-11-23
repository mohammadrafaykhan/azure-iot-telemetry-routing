"""Zip the function app for a Flex Consumption remote build: ``python infra/package.py functions out.zip``.

Entries use forward slashes (Compress-Archive on Windows PowerShell 5.1 writes
backslashes, which Linux hosts do not treat as directories) and exclude caches
and local settings.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

EXCLUDED_DIRS = {"__pycache__", ".venv", ".python_packages", ".pytest_cache"}
EXCLUDED_FILES = {"local.settings.json"}
EXCLUDED_SUFFIXES = {".pyc"}
REQUIRED = ("function_app.py", "host.json", "requirements.txt")


def package(source: Path, target: Path) -> list[str]:
    missing = [name for name in REQUIRED if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"{source} is missing {', '.join(missing)}")
    names: list[str] = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if (
                path.is_dir()
                or EXCLUDED_DIRS.intersection(relative.parts)
                or path.name in EXCLUDED_FILES
                or path.suffix in EXCLUDED_SUFFIXES
            ):
                continue
            archive.write(path, relative.as_posix())
            names.append(relative.as_posix())
    return names


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python infra/package.py <functions-dir> <output.zip>")
    files = package(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"packaged {len(files)} files into {sys.argv[2]}")
