"""Dialogs: export options and the export report."""

from __future__ import annotations

import os

from qgis.core import QgsIconUtils, QgsLayerTreeGroup, QgsLayerTreeLayer
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

ID_ROLE = Qt.ItemDataRole.UserRole


class LayerChooser(QTreeWidget):
    """Checkable copy of the Layers panel tree. Ticking a group ticks its layers."""

    def __init__(self, root, checked_ids: set):
        super().__init__()
        self.setHeaderHidden(True)
        self.setMinimumHeight(180)
        self._layer_items = []
        self._visible = set()
        self._add_children(self.invisibleRootItem(), root, checked_ids)
        self.expandAll()

    def _add_children(self, parent_item, group, checked_ids):
        for node in group.children():
            item = QTreeWidgetItem(parent_item, [node.name()])
            flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            if isinstance(node, QgsLayerTreeGroup):
                item.setFlags(flags | Qt.ItemFlag.ItemIsAutoTristate)
                self._add_children(item, node, checked_ids)
            elif isinstance(node, QgsLayerTreeLayer) and node.layer() is not None:
                item.setFlags(flags)
                item.setData(0, ID_ROLE, node.layerId())
                item.setIcon(0, QgsIconUtils.iconForLayer(node.layer()))
                item.setCheckState(0, Qt.CheckState.Checked if node.layerId() in checked_ids
                                   else Qt.CheckState.Unchecked)
                self._layer_items.append(item)
                if node.isVisible():
                    self._visible.add(node.layerId())
            else:
                parent_item.removeChild(item)

    def set_checked(self, ids: set):
        for item in self._layer_items:
            item.setCheckState(0, Qt.CheckState.Checked if item.data(0, ID_ROLE) in ids
                               else Qt.CheckState.Unchecked)

    def all_ids(self) -> set:
        return {i.data(0, ID_ROLE) for i in self._layer_items}

    def visible_ids(self) -> set:
        return set(self._visible)

    def checked_ids(self) -> set:
        return {i.data(0, ID_ROLE) for i in self._layer_items
                if i.checkState(0) == Qt.CheckState.Checked}

from .core.packager import TILE_FORMATS


class ExportDialog(QDialog):
    def __init__(self, parent, default_path: str, default_name: str, layer_root,
                 checked_ids: set, panel_selection: set):
        super().__init__(parent)
        self.setWindowTitle("Export MapBundle")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Creates one GeoPackage containing the chosen layers (numbered in drawing order), "
            "their styles, embedded SVG/image symbols, the fonts used, and the project itself. "
            "Your open project is not changed.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.file_widget = QgsFileWidget()
        self.file_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.file_widget.setFilter("GeoPackage (*.gpkg)")
        self.file_widget.setConfirmOverwrite(True)
        self.file_widget.setFilePath(default_path)
        form.addRow("Output file", self.file_widget)
        self.name_edit = QLineEdit(default_name)
        form.addRow("Bundle name", self.name_edit)
        layout.addLayout(form)

        content = QGroupBox("Layers")
        cform = QVBoxLayout(content)
        self.layer_chooser = LayerChooser(layer_root, checked_ids)
        cform.addWidget(self.layer_chooser)
        quick = QHBoxLayout()
        for label, ids in (("All", self.layer_chooser.all_ids()),
                           ("Visible only", self.layer_chooser.visible_ids()),
                           ("Selected in Layers panel", panel_selection)):
            button = QPushButton(label)
            button.setEnabled(bool(ids))
            button.clicked.connect(lambda _checked=False, ids=ids: self.layer_chooser.set_checked(ids))
            quick.addWidget(button)
        quick.addStretch()
        cform.addLayout(quick)
        hint = QLabel("Layers that a chosen layer depends on (joins, relations) are added automatically.")
        hint.setWordWrap(True)
        cform.addWidget(hint)
        self.extent_check = QCheckBox("Only include data within the current map view")
        self.extent_check.setToolTip(
            "Keeps features that intersect the visible area and clips rasters to it. "
            "Reduces file size for large datasets.")
        cform.addWidget(self.extent_check)
        layout.addWidget(content)

        raster = QGroupBox("Raster layers")
        rform = QFormLayout(raster)
        self.tile_combo = QComboBox()
        for key, label in TILE_FORMATS.items():
            self.tile_combo.addItem(label, key)
        rform.addRow("Tile format", self.tile_combo)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(10, 100)
        self.quality_spin.setValue(80)
        rform.addRow("JPEG/WEBP quality", self.quality_spin)
        layout.addWidget(raster)

        fonts = QGroupBox("Fonts")
        fform = QVBoxLayout(fonts)
        self.fonts_check = QCheckBox("Bundle fonts with an open licence (e.g. Google Fonts)")
        self.fonts_check.setChecked(True)
        self.restricted_check = QCheckBox("Also bundle fonts whose licence is unknown or restrictive")
        self.restricted_check.setToolTip(
            "Only tick this if you are allowed to share these fonts. Many fonts installed with "
            "Windows or macOS (e.g. Arial, Calibri) may not be redistributed.")
        self.fonts_check.toggled.connect(self.restricted_check.setEnabled)
        fform.addWidget(self.fonts_check)
        fform.addWidget(self.restricted_check)
        layout.addWidget(fonts)

        extras = QGroupBox("Extras")
        eform = QVBoxLayout(extras)
        self.assets_check = QCheckBox("Also save a zip of the SVGs, images and rasters used")
        self.assets_check.setToolTip(
            "Writes <name>_assets.zip next to the GeoPackage, for reusing or editing the files. "
            "The GeoPackage does not need it.")
        eform.addWidget(self.assets_check)
        layout.addWidget(extras)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        if not self.file_widget.filePath().strip():
            self.file_widget.setFocus()
            return
        if not self.name_edit.text().strip():
            self.name_edit.setFocus()
            return
        if not self.layer_chooser.checked_ids():
            self.layer_chooser.setFocus()
            return
        self.accept()

    @property
    def layer_ids(self) -> set | None:
        """Chosen layer ids, or None when every layer is chosen."""
        checked = self.layer_chooser.checked_ids()
        return None if checked == self.layer_chooser.all_ids() else checked

    @property
    def output_path(self) -> str:
        path = self.file_widget.filePath().strip()
        return path if path.lower().endswith(".gpkg") else path + ".gpkg"


class ReportDialog(QDialog):
    def __init__(self, parent, report):
        super().__init__(parent)
        self.setWindowTitle("MapBundle")
        self.resize(640, 480)
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setHtml(report.to_html())
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        if report.success:
            folder = QPushButton("Show in folder")
            folder.clicked.connect(lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(os.path.dirname(report.output_path))))
            buttons.addButton(folder, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)
