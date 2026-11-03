"""Scan a QGIS project XML document to embed external image files and find fonts.

Working on the project XML (rather than walking symbol objects) means every place
QGIS can reference a file is covered in one pass: layer symbols, sub-symbols,
label backgrounds, layout pictures, annotations and so on.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

from qgis.core import QgsPathResolver, QgsSymbolLayerUtils
from qgis.PyQt.QtXml import QDomDocument, QDomElement

IMAGE_EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

# Keys whose value can be a path to an SVG or raster image.
# <Option name="KEY" value="..."/> and legacy <prop k="KEY" v="..."/>
OPTION_PATH_KEYS = {"name", "imagefile", "svgfile", "file", "shapesvgfile", "picturefile"}
# Plain element attributes, e.g. <background shapeSVGFile="..."/>, <LayoutItem file="...">
ATTRIBUTE_PATH_KEYS = {"shapesvgfile", "file", "svgfile", "imagefile", "picturefile"}

# Data-defined properties that override file paths or fonts and therefore cannot
# be embedded/bundled automatically.
DATA_DEFINED_FILE_KEYS = {"name", "file", "shapesvgfile", "svgfile", "imagefile"}
DATA_DEFINED_FONT_KEYS = {"fontfamily", "family", "font"}


@dataclass
class ScanResult:
    embedded: dict = field(default_factory=dict)       # original path -> size in bytes
    missing: list = field(default_factory=list)        # referenced files that could not be found
    remote: list = field(default_factory=list)         # http(s) references left untouched
    fonts: dict = field(default_factory=dict)          # family -> set of places used
    data_defined: list = field(default_factory=list)   # descriptions of data-defined paths/fonts


def _elements(root: QDomElement):
    """Iterate over all elements in document order (non-recursive)."""
    stack = [root]
    while stack:
        el = stack.pop()
        yield el
        child = el.lastChildElement()
        children = []
        while not child.isNull():
            children.append(child)
            child = child.previousSiblingElement()
        stack.extend(children)


def _layer_name(el: QDomElement) -> str:
    """Name of the map layer (or layout) an element belongs to, for reporting."""
    node = el
    while not node.isNull():
        if node.tagName() == "maplayer":
            name = node.firstChildElement("layername")
            return name.text() if not name.isNull() else "unnamed layer"
        if node.tagName() == "Layout":
            return "layout '%s'" % node.attribute("name")
        node = node.parentNode().toElement()
    return "project"


class ProjectScanner:
    def __init__(self, project_file_path: str):
        self.resolver = QgsPathResolver(project_file_path)
        self.cache: dict = {}  # resolved path -> "base64:..." or None

    # -- images -------------------------------------------------------------

    def _resolve(self, value: str) -> str | None:
        if value.lower().endswith(".svg"):
            path = QgsSymbolLayerUtils.svgSymbolNameToPath(value, self.resolver)
        else:
            path = self.resolver.readPath(value)
        return path if path and os.path.isfile(path) else None

    def _embedded_value(self, value: str, result: ScanResult) -> str | None:
        """Return the base64 replacement for a path value, or None to leave it alone."""
        stripped = value.strip()
        lower = stripped.lower()
        if not stripped or lower.startswith("base64:"):
            return None
        if not lower.endswith(IMAGE_EXTENSIONS):
            return None
        if lower.startswith(("http://", "https://")):
            result.remote.append(stripped)
            return None
        path = self._resolve(stripped)
        if path is None:
            if stripped not in result.missing:
                result.missing.append(stripped)
            return None
        if path not in self.cache:
            with open(path, "rb") as fh:
                data = fh.read()
            self.cache[path] = "base64:" + base64.b64encode(data).decode("ascii")
            result.embedded[path] = len(data)
        return self.cache[path]

    # -- main pass ----------------------------------------------------------

    def scan(self, doc: QDomDocument, embed: bool = True) -> ScanResult:
        result = ScanResult()
        for el in _elements(doc.documentElement()):
            tag = el.tagName()

            # Skip layer sources and the layer tree: a raster layer whose file is a
            # .png must never be turned into base64.
            if tag in ("datasource", "layer-tree-layer", "layer-tree-group"):
                continue

            name_attr = el.attribute("name") if el.hasAttribute("name") else None
            if tag == "Option" and name_attr is not None and el.hasAttribute("value"):
                key = name_attr.lower()
                if key in OPTION_PATH_KEYS and embed:
                    new = self._embedded_value(el.attribute("value"), result)
                    if new is not None:
                        el.setAttribute("value", new)
                if key == "font" and el.attribute("value"):
                    self._add_font(result, el.attribute("value"), el)
            elif tag == "prop" and el.hasAttribute("k") and el.hasAttribute("v"):
                key = el.attribute("k").lower()
                if key in OPTION_PATH_KEYS and embed:
                    new = self._embedded_value(el.attribute("v"), result)
                    if new is not None:
                        el.setAttribute("v", new)
                if key == "font" and el.attribute("v"):
                    self._add_font(result, el.attribute("v"), el)

            attrs = el.attributes()
            for i in range(attrs.count()):
                attr = attrs.item(i).toAttr()
                key = attr.name().lower()
                if key in ATTRIBUTE_PATH_KEYS and embed and tag != "Option":
                    new = self._embedded_value(attr.value(), result)
                    if new is not None:
                        el.setAttribute(attr.name(), new)
                elif key == "fontfamily" and attr.value():
                    self._add_font(result, attr.value(), el)
                elif key == "description" and "font" in tag.lower() and attr.value():
                    # QFont::toString() -> "Family,size,..."
                    self._add_font(result, attr.value().split(",")[0], el)

            self._check_data_defined(el, result)
        return result

    @staticmethod
    def _add_font(result: ScanResult, family: str, el: QDomElement):
        family = family.strip()
        if family:
            result.fonts.setdefault(family, set()).add(_layer_name(el))

    @staticmethod
    def _check_data_defined(el: QDomElement, result: ScanResult):
        """Detect active data-defined overrides of file paths or fonts.

        Stored as <Option name="KEY" type="Map"><Option name="active" value="true"/>...
        inside a 'properties' map.
        """
        if el.tagName() != "Option" or el.attribute("type") != "Map":
            return
        key = el.attribute("name").lower()
        if key not in DATA_DEFINED_FILE_KEYS | DATA_DEFINED_FONT_KEYS:
            return
        parent = el.parentNode().toElement()
        if parent.isNull() or parent.attribute("name") != "properties":
            return
        active = False
        expression = ""
        child = el.firstChildElement("Option")
        while not child.isNull():
            n = child.attribute("name")
            if n == "active":
                active = child.attribute("value") == "true"
            elif n in ("expression", "field"):
                expression = expression or child.attribute("value")
            child = child.nextSiblingElement("Option")
        if active:
            kind = "font" if key in DATA_DEFINED_FONT_KEYS else "file path"
            result.data_defined.append(
                f"{_layer_name(el)}: {kind} set by expression '{expression}'")


def load_project_xml(path: str) -> QDomDocument:
    doc = QDomDocument()
    with open(path, "rb") as fh:
        content = fh.read()
    parsed = doc.setContent(content)
    ok = parsed[0] if isinstance(parsed, tuple) else bool(parsed)
    if not ok:
        raise RuntimeError(f"Could not parse project XML: {path}")
    return doc


def save_project_xml(doc: QDomDocument, path: str):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc.toString(1))
