"""Reading a MapParcel: installing its fonts, and adding its layers to a project."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from qgis.core import (
    QgsLayerDefinition,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsPathResolver,
    QgsProject,
    QgsReadWriteContext,
)
from qgis.PyQt.QtXml import QDomDocument

from . import fonts as fontlib
from . import pathfix
from .parcel import list_projects, project_uri, read_fonts_zip

GROUP_PREFIX = "map_parcel"


@dataclass
class ParcelInfo:
    path: str
    project_name: str
    bundled_fonts: list = field(default_factory=list)   # StoredFont
    missing_fonts: list = field(default_factory=list)   # StoredFont not installed here

    @property
    def missing_families(self) -> list:
        return sorted({f.family for f in self.missing_fonts})

    @property
    def uri(self) -> str:
        return project_uri(self.path, self.project_name)


def projects_in(gpkg_path: str) -> list:
    return list_projects(gpkg_path)


def inspect_parcel(gpkg_path: str, project_name: str) -> ParcelInfo:
    info = ParcelInfo(gpkg_path, project_name)
    info.bundled_fonts = fontlib.read_fonts_zip(read_fonts_zip(gpkg_path, project_name))
    if info.bundled_fonts:
        info.missing_fonts = fontlib.missing_fonts(info.bundled_fonts)
    return info


def install_missing_fonts(info: ParcelInfo) -> tuple[list, list]:
    """Install the bundled fonts that are not available yet. Returns (families, errors)."""
    if not info.missing_fonts:
        return [], []
    installed, errors = fontlib.install_fonts(info.missing_fonts)
    info.missing_fonts = fontlib.missing_fonts(info.bundled_fonts)
    return installed, errors


def next_group_name(project: QgsProject, prefix: str = GROUP_PREFIX) -> str:
    """'<prefix>_N' with N one higher than any existing group of that name."""
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    highest = 0
    for group in project.layerTreeRoot().findGroups(True):
        match = pattern.match(group.name())
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}_{highest + 1}"


def add_to_project(info: ParcelInfo, target: QgsProject | None = None,
                   group_name: str | None = None) -> tuple[QgsLayerTreeGroup | None, str]:
    """Add the parcel's layers, with styles and drawing order, to ``target`` as a new
    group at the bottom of the layer tree. Returns (group, error message)."""
    target = target or QgsProject.instance()
    source = QgsProject()
    try:
        with pathfix.reading(info.path, info.project_name):
            if not source.read(info.uri):
                return None, f"Could not read the MapParcel project: {source.error()}"

        # Nodes to copy. If the parcel uses a custom drawing order, follow that order.
        src_root = source.layerTreeRoot()
        staging = None
        if src_root.hasCustomLayerOrder():
            staging = QgsLayerTreeGroup("staging")
            for layer in src_root.customLayerOrder():
                original = src_root.findLayer(layer.id())
                node = QgsLayerTreeLayer(layer)
                if original is not None:
                    node.setName(original.name())
                    node.setItemVisibilityChecked(original.isVisible())
                staging.addChildNode(node)
            nodes = staging.children()
        else:
            nodes = src_root.children()
        if not nodes:
            return None, "The MapParcel contains no layers."

        context = QgsReadWriteContext()
        context.setPathResolver(QgsPathResolver(""))  # absolute paths to the parcel file
        doc = QDomDocument("qgis-layer-definition")
        ok, error = QgsLayerDefinition.exportLayerDefinition(doc, nodes, context)
        if not ok:
            return None, f"Could not read the layers: {error}"

        was_empty = not target.mapLayers()
        root = target.layerTreeRoot()
        previous_order = list(root.customLayerOrder()) if root.hasCustomLayerOrder() else None
        group = root.insertGroup(len(root.children()), group_name or next_group_name(target))
        ok, error = QgsLayerDefinition.loadLayerDefinition(doc, target, group, context)
        if not ok:
            root.removeChildNode(group)
            return None, f"Could not add the layers: {error}"
        group.setExpanded(False)

        if previous_order is not None:
            # Project uses its own drawing order: draw the new layers underneath.
            new_layers = [n.layer() for n in group.findLayers() if n.layer() is not None]
            root.setCustomLayerOrder(previous_order + new_layers)
        if was_empty and source.crs().isValid():
            target.setCrs(source.crs())
        return group, ""
    finally:
        source.clear()
