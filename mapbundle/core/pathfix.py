"""Keep MapBundles working after the .gpkg file is renamed.

A project saved inside a GeoPackage refers to its own layers by file name
(e.g. "./roads.gpkg|layername=01_roads"). If a recipient renames the file to
"roads (1).gpkg", those references break, or worse, point at a different bundle
that still has the original name. While the plugin is loaded, this path
pre-processor redirects such references to the GeoPackage being opened.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from qgis.core import QgsPathResolver, QgsProject

from .bundle import parse_project_uri, read_info

_preprocessor_id = None
_context: list = []  # stack of (gpkg path, project name) for projects read in the background


@contextmanager
def reading(gpkg_path: str, project_name: str):
    """Use while reading a bundle into a project other than QgsProject.instance()."""
    _context.append((gpkg_path, project_name))
    try:
        yield
    finally:
        _context.pop()


def _current_bundle() -> tuple[str, str] | None:
    if _context:
        return _context[-1]
    return parse_project_uri(QgsProject.instance().fileName())


def _redirect(file_part: str, original: str, current: str) -> str | None:
    if os.path.basename(file_part.replace("\\", "/")) != original:
        return None
    return current


def preprocess(path: str) -> str:
    if not path or ".gpkg" not in path.lower():
        return path
    bundle = _current_bundle()
    if not bundle:
        return path
    current, project_name = bundle
    original = read_info(current, project_name).get("original_filename")
    if not original or os.path.basename(current) == original:
        return path

    if path.upper().startswith("GPKG:"):
        # GPKG:<file>:<table>  (file may contain a drive letter colon)
        body = path[5:]
        file_part, sep, table = body.rpartition(":")
        if not sep:
            file_part, table = body, ""
        new = _redirect(file_part, original, current)
        if new:
            return f"GPKG:{new}:{table}" if sep else f"GPKG:{new}"
        return path

    file_part, sep, rest = path.partition("|")
    new = _redirect(file_part, original, current)
    return new + sep + rest if new else path


def install():
    global _preprocessor_id
    if _preprocessor_id is None:
        _preprocessor_id = QgsPathResolver.setPathPreprocessor(preprocess)


def uninstall():
    global _preprocessor_id
    if _preprocessor_id is not None:
        QgsPathResolver.removePathPreprocessor(_preprocessor_id)
        _preprocessor_id = None
