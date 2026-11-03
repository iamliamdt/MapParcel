"""MapBundle file format helpers.

A MapBundle is a GeoPackage holding the layers plus a QGIS project (table
``qgis_projects``). Extra content travels as project attachments inside the
project archive, so it never shows up as a layer:

  <id>_mapbundle.json        bundle info (original file name, format version)
  <id>_mapbundle_fonts.zip   font files + manifest.json
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import zipfile

from qgis.PyQt.QtCore import QUrl, QUrlQuery

SCHEME = "geopackage"
FORMAT_VERSION = 2
INFO_ATTACHMENT = "mapbundle.json"
FONTS_ATTACHMENT = "mapbundle_fonts.zip"

_archive_cache: dict = {}


# ------------------------------------------------------------- project URIs

def project_uri(gpkg_path: str, project_name: str) -> str:
    """URI QGIS uses for a project stored in a GeoPackage
    (mirrors QgsGeoPackageProjectStorage::encodeUri())."""
    url = QUrl()
    url.setScheme(SCHEME)
    url.setPath(gpkg_path.replace("\\", "/"))
    query = QUrlQuery()
    query.addQueryItem("projectName", project_name)
    url.setQuery(query)
    return bytes(url.toEncoded()).decode("utf-8")


def parse_project_uri(uri: str) -> tuple[str, str] | None:
    """(gpkg path, project name) for a geopackage: URI, else None."""
    if not uri or not uri.lower().startswith(SCHEME + ":"):
        return None
    url = QUrl.fromEncoded(uri.encode("utf-8"))
    path = url.path()
    if re.match(r"^/[A-Za-z]:", path):  # Windows: "/C:/x.gpkg" -> "C:/x.gpkg"
        path = path[1:]
    name = QUrlQuery(url).queryItemValue("projectName", QUrl.ComponentFormattingOption.FullyDecoded)
    return path, name


# ------------------------------------------------------- reading the archive

def _connect_ro(gpkg_path: str):
    return sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)


def list_projects(gpkg_path: str) -> list[str]:
    try:
        con = _connect_ro(gpkg_path)
    except sqlite3.Error:
        return []
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qgis_projects'").fetchone():
            return []
        return [r[0] for r in con.execute("SELECT name FROM qgis_projects ORDER BY name")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _archive_members(gpkg_path: str, project_name: str) -> dict:
    """Attachment name -> bytes for the bundle files in a stored project (cached)."""
    try:
        stamp = os.path.getmtime(gpkg_path)
    except OSError:
        return {}
    key = (os.path.normcase(os.path.abspath(gpkg_path)), project_name)
    cached = _archive_cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    members = {}
    try:
        con = _connect_ro(gpkg_path)
        try:
            row = con.execute("SELECT content FROM qgis_projects WHERE name = ?", (project_name,)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            content = row[0]
            data = bytes.fromhex(content) if isinstance(content, str) else bytes(content)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    for wanted in (INFO_ATTACHMENT, FONTS_ATTACHMENT):
                        if name.endswith("_" + wanted) or name == wanted:
                            members[wanted] = zf.read(name)
    except (sqlite3.Error, zipfile.BadZipFile, ValueError, OSError):
        members = {}
    _archive_cache[key] = (stamp, members)
    return members


def read_info(gpkg_path: str, project_name: str) -> dict:
    raw = _archive_members(gpkg_path, project_name).get(INFO_ATTACHMENT)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}


def read_fonts_zip(gpkg_path: str, project_name: str) -> bytes | None:
    return _archive_members(gpkg_path, project_name).get(FONTS_ATTACHMENT)


def is_mapbundle(gpkg_path: str, project_name: str) -> bool:
    return bool(read_info(gpkg_path, project_name))
