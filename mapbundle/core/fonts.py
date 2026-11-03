"""Font discovery, licence detection, and bundling of font files.

QGIS cannot embed fonts in a project, so the font files are zipped and attached to
the project stored in the MapBundle. On import they are installed as QGIS user
fonts (no admin rights needed).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import zipfile
import sys
from dataclasses import dataclass, field

FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc")

# Substrings (lower case) in a font's licence/copyright text that indicate a
# licence allowing redistribution.
OPEN_LICENCE_MARKERS = (
    ("open font license", "SIL Open Font License"),
    ("openfontlicense.org", "SIL Open Font License"),
    ("scripts.sil.org/ofl", "SIL Open Font License"),
    ("apache license", "Apache License"),
    ("apache.org/licenses", "Apache License"),
    ("ubuntu font licence", "Ubuntu Font Licence"),
    ("ubuntu font license", "Ubuntu Font Licence"),
    ("gnu general public license", "GNU GPL"),
    ("gnu.org/licenses", "GNU GPL"),
    ("bitstream vera", "Bitstream Vera License"),
    ("gust font license", "GUST Font License"),
    ("public domain", "Public domain"),
    ("creativecommons.org", "Creative Commons"),
    ("creative commons", "Creative Commons"),
    ("mit license", "MIT License"),
)


@dataclass
class FontFile:
    path: str
    families: set = field(default_factory=set)
    licence_text: str = ""

    @property
    def licence_name(self) -> str | None:
        """Name of a recognised open licence, or None if unknown/restrictive."""
        text = self.licence_text.lower()
        for marker, name in OPEN_LICENCE_MARKERS:
            if marker in text:
                return name
        return None


# ---------------------------------------------------------------------------
# Minimal OpenType/TrueType 'name' table reader (no external dependencies)
# ---------------------------------------------------------------------------

def _decode_name(platform_id: int, raw: bytes) -> str:
    try:
        if platform_id in (0, 3):
            return raw.decode("utf-16-be", errors="replace")
        return raw.decode("mac_roman", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return ""


def _read_name_table(fh, font_offset: int) -> tuple[set, str]:
    fh.seek(font_offset)
    header = fh.read(12)
    if len(header) < 12:
        return set(), ""
    num_tables = struct.unpack(">H", header[4:6])[0]
    records = fh.read(16 * num_tables)
    name_offset = None
    for i in range(num_tables):
        tag, _checksum, offset, _length = struct.unpack(">4sLLL", records[i * 16:(i + 1) * 16])
        if tag == b"name":
            name_offset = offset
            break
    if name_offset is None:
        return set(), ""

    fh.seek(name_offset)
    _fmt, count, string_offset = struct.unpack(">HHH", fh.read(6))
    entries = fh.read(12 * count)
    families, licence_parts = set(), []
    for i in range(count):
        platform_id, _enc, _lang, name_id, length, offset = struct.unpack(
            ">HHHHHH", entries[i * 12:(i + 1) * 12])
        # 1 = family, 16 = typographic family, 0 = copyright,
        # 13 = licence description, 14 = licence URL
        if name_id not in (0, 1, 13, 14, 16):
            continue
        fh.seek(name_offset + string_offset + offset)
        text = _decode_name(platform_id, fh.read(length)).strip()
        if not text:
            continue
        if name_id in (1, 16):
            families.add(text)
        else:
            licence_parts.append(text)
    return families, "\n".join(dict.fromkeys(licence_parts))


def read_font_file(path: str) -> FontFile | None:
    """Return family names and licence text for a font file, or None if unreadable."""
    try:
        with open(path, "rb") as fh:
            tag = fh.read(4)
            if tag == b"ttcf":
                fh.seek(8)
                num_fonts = struct.unpack(">L", fh.read(4))[0]
                offsets = struct.unpack(">%dL" % num_fonts, fh.read(4 * num_fonts))
            elif tag in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                offsets = (0,)
            else:
                return None
            result = FontFile(path)
            texts = []
            for offset in offsets:
                families, licence = _read_name_table(fh, offset)
                result.families |= families
                texts.append(licence)
            result.licence_text = "\n".join(dict.fromkeys(t for t in texts if t))
            return result
    except (OSError, struct.error):
        return None


# ---------------------------------------------------------------------------
# Locating font files on this computer
# ---------------------------------------------------------------------------

def system_font_directories() -> list[str]:
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs = [os.path.join(windir, "Fonts")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        dirs = ["/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library", "Fonts")]
    else:
        dirs = ["/usr/share/fonts", "/usr/local/share/fonts",
                os.path.join(home, ".fonts"), os.path.join(home, ".local", "share", "fonts")]
    # Fonts that ship with QGIS itself (e.g. conda / OSGeo4W installs)
    prefix = sys.prefix
    dirs += [os.path.join(prefix, "share", "fonts"), os.path.join(prefix, "fonts")]
    return [d for d in dirs if os.path.isdir(d)]


class FontIndex:
    """Lazily built map of family name (lower case) -> font files."""

    def __init__(self, extra_files: list[str] | None = None, extra_dirs: list[str] | None = None):
        self._extra_files = extra_files or []
        self._extra_dirs = extra_dirs or []
        self._index: dict[str, list[FontFile]] | None = None

    def _add(self, path: str, seen: set):
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        info = read_font_file(path)
        if not info:
            return
        for fam in info.families:
            self._index.setdefault(fam.lower(), []).append(info)

    def _build(self):
        self._index = {}
        seen: set = set()
        for path in self._extra_files:
            self._add(path, seen)
        for directory in self._extra_dirs + system_font_directories():
            for root, _dirs, files in os.walk(directory):
                for name in files:
                    if name.lower().endswith(FONT_EXTENSIONS):
                        self._add(os.path.join(root, name), seen)

    def files_for_family(self, family: str) -> list[FontFile]:
        """All distinct font files of a family (identical copies in several folders count once)."""
        if self._index is None:
            self._build()
        unique, hashes = [], set()
        for font in self._index.get(family.lower(), []):
            try:
                with open(font.path, "rb") as fh:
                    digest = hashlib.sha1(fh.read()).hexdigest()
            except OSError:
                continue
            if digest not in hashes:
                hashes.add(digest)
                unique.append(font)
        return unique


def qgis_user_font_files() -> list[str]:
    """Font files installed through QGIS's own font manager (user fonts)."""
    try:
        from qgis.core import QgsApplication
        return list(QgsApplication.fontManager().userFontToFamilyMap().keys())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Storage: a zip (fonts + manifest) attached to the project inside the GeoPackage
# ---------------------------------------------------------------------------

@dataclass
class StoredFont:
    family: str
    filename: str
    licence: str
    data: bytes


def build_fonts_zip(fonts: list[tuple[str, FontFile]]) -> tuple[bytes, int]:
    """Zip the given (family, FontFile) pairs. Returns (zip bytes, total font bytes)."""
    buf = io.BytesIO()
    manifest, used_names, total = [], set(), 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for family, font in fonts:
            name = os.path.basename(font.path)
            stem, ext = os.path.splitext(name)
            i = 2
            while name.lower() in used_names:
                name = f"{stem}_{i}{ext}"
                i += 1
            used_names.add(name.lower())
            with open(font.path, "rb") as fh:
                data = fh.read()
            total += len(data)
            zf.writestr(name, data)
            manifest.append({"family": family, "filename": name,
                             "licence": font.licence_name or "unknown"})
        zf.writestr("manifest.json", json.dumps(manifest, indent=1))
    return buf.getvalue(), total


def read_fonts_zip(data: bytes | None) -> list[StoredFont]:
    if not data:
        return []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            return [StoredFont(m["family"], m["filename"], m.get("licence", ""), zf.read(m["filename"]))
                    for m in manifest]
    except (zipfile.BadZipFile, KeyError, ValueError):
        return []


def installed_families() -> set:
    """Lower-cased font family names currently available to Qt."""
    from qgis.PyQt.QtGui import QFontDatabase
    try:
        families = QFontDatabase.families()  # Qt 6 (static)
    except TypeError:
        families = QFontDatabase().families()  # Qt 5
    return {f.lower() for f in families}


def missing_fonts(stored: list[StoredFont]) -> list[StoredFont]:
    have = installed_families()
    return [f for f in stored if f.family.lower() not in have]


def install_fonts(fonts: list[StoredFont]) -> tuple[list[str], list[str]]:
    """Install fonts as QGIS user fonts. Returns (installed families, errors)."""
    from qgis.core import QgsApplication
    manager = QgsApplication.fontManager()
    installed, errors = [], []
    for font in fonts:
        ext = os.path.splitext(font.filename)[1].lstrip(".").lower() or "ttf"
        ok, error, families, _licence = manager.installFontsFromData(font.data, font.filename, ext)
        if ok:
            installed.extend(families or [font.family])
        else:
            errors.append(f"{font.filename}: {error}")
    return sorted(set(installed)), errors
