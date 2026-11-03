"""QGIS GUI integration for MapBundle."""

from __future__ import annotations

import os
import re

from qgis.core import Qgis, QgsFeedback, QgsProject
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QProgressDialog,
)

try:
    from qgis.PyQt.QtGui import QAction  # Qt 6 (QGIS 4)
except ImportError:
    from qgis.PyQt.QtWidgets import QAction  # Qt 5

from .core import importer, pathfix
from .core.bundle import is_mapbundle, parse_project_uri
from .core.packager import ExportOptions, export_bundle
from .dialogs import ExportDialog, ReportDialog

MENU = "&MapBundle"
CONTEXT_LAYER_TYPES = [t for t in (getattr(Qgis.LayerType, n, None) for n in
                       ("Vector", "Raster", "VectorTile", "Mesh", "PointCloud")) if t is not None]
TITLE = "MapBundle"


class MapBundlePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self._declined_fonts = set()  # bundles whose fonts the user chose not to install
        self._busy = False

    # ----------------------------------------------------------------- setup

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        win = self.iface.mainWindow()
        export_action = QAction(icon, "Export MapBundle…", win)
        export_action.setToolTip("Package this project's layers, styles, symbols and fonts into one GeoPackage")
        export_action.triggered.connect(lambda: self.export_bundle())
        add_action = QAction(icon, "Add MapBundle to Project…", win)
        add_action.setToolTip("Add a MapBundle's layers to the current project as a group")
        add_action.triggered.connect(self.add_bundle)
        open_action = QAction("Open MapBundle as Project…", win)
        open_action.setToolTip("Replace the current project with the MapBundle's full project (incl. layouts)")
        open_action.triggered.connect(self.open_bundle_as_project)
        for action in (export_action, add_action, open_action):
            self.iface.addPluginToMenu(MENU, action)
            self.actions.append(action)
        self.iface.addToolBarIcon(export_action)
        self.iface.addToolBarIcon(add_action)

        # Right-click on layers in the Layers panel
        self.context_action = QAction(icon, "Export Selected Layers as MapBundle…", win)
        self.context_action.triggered.connect(lambda: self.export_bundle(from_selection=True))
        for layer_type in CONTEXT_LAYER_TYPES:
            self.iface.addCustomActionForLayerType(self.context_action, "", layer_type, True)

        pathfix.install()
        QgsProject.instance().readProject.connect(self._on_project_read)

    def unload(self):
        try:
            QgsProject.instance().readProject.disconnect(self._on_project_read)
        except TypeError:
            pass
        pathfix.uninstall()
        if getattr(self, "context_action", None) is not None:
            self.iface.removeCustomActionForLayerType(self.context_action)
            self.context_action = None
        for action in self.actions:
            self.iface.removePluginMenu(MENU, action)
            self.iface.removeToolBarIcon(action)
        self.actions = []

    # ---------------------------------------------------------------- export

    def _ensure_saved(self) -> str | None:
        project = QgsProject.instance()
        if project.fileName() and not project.isDirty():
            return project.fileName()
        answer = QMessageBox.question(
            self.iface.mainWindow(), "Export MapBundle",
            "The MapBundle is built from the saved project. Save the project now?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save)
        if answer != QMessageBox.StandardButton.Save:
            return None
        self.iface.actionSaveProject().trigger()
        if project.fileName() and not project.isDirty():
            return project.fileName()
        return None

    def _panel_selection(self) -> list:
        view = self.iface.layerTreeView()
        return [l for l in view.selectedLayersRecursive() if l is not None] if view else []

    def export_bundle(self, from_selection: bool = False):
        selection = self._panel_selection()
        source = self._ensure_saved()
        if not source:
            return
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        all_ids = {n.layerId() for n in root.findLayers()}
        selected_ids = {l.id() for l in selection} & all_ids
        subset = from_selection and selected_ids and selected_ids != all_ids
        checked = selected_ids if subset else all_ids

        base = project.baseName() or "map"
        default_name = project.title() or base
        if subset:
            first = next(n for n in root.findLayers() if n.layerId() in selected_ids)
            default_name = first.name() if len(selected_ids) == 1 else f"{default_name} ({len(selected_ids)} layers)"
            base = re.sub(r"[^\w-]+", "_", first.name()).strip("_") or base
        folder = project.absolutePath() or os.path.expanduser("~")
        if folder.startswith("geopackage:") or not os.path.isdir(folder):
            folder = os.path.expanduser("~")
        dialog = ExportDialog(self.iface.mainWindow(),
                              os.path.join(folder, f"{base}_mapbundle.gpkg"),
                              default_name, root, checked, selected_ids)
        if not dialog.exec():
            return

        options = ExportOptions(
            output_path=dialog.output_path,
            project_name=dialog.name_edit.text().strip(),
            layer_ids=dialog.layer_ids,
            raster_tile_format=dialog.tile_combo.currentData(),
            raster_quality=dialog.quality_spin.value(),
            bundle_fonts=dialog.fonts_check.isChecked(),
            bundle_restricted_fonts=dialog.restricted_check.isChecked(),
            assets_zip=dialog.assets_check.isChecked(),
        )
        if dialog.extent_check.isChecked():
            canvas = self.iface.mapCanvas()
            options.extent = canvas.extent()
            options.extent_crs = canvas.mapSettings().destinationCrs()

        feedback = QgsFeedback()
        progress = QProgressDialog("Building MapBundle…", "Cancel", 0, 100, self.iface.mainWindow())
        progress.setWindowTitle("Export MapBundle")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.canceled.connect(feedback.cancel)

        def on_progress(value):
            progress.setValue(int(value))
            QApplication.processEvents()

        feedback.progressChanged.connect(on_progress)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = export_bundle(source, options, feedback, live_project=project)
        finally:
            QApplication.restoreOverrideCursor()
            progress.close()

        level = Qgis.MessageLevel.Success if report.success else Qgis.MessageLevel.Critical
        self.iface.messageBar().pushMessage(
            TITLE, f"Exported to {report.output_path}" if report.success else "Export failed", level, 8)
        ReportDialog(self.iface.mainWindow(), report).exec()

    # ------------------------------------------------------------- importing

    def _choose_bundle(self, title: str):
        """Ask for a .gpkg and (if needed) which project in it. Returns BundleInfo or None."""
        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(), title, os.path.expanduser("~"), "MapBundle / GeoPackage (*.gpkg)")
        if not path:
            return None
        projects = importer.projects_in(path)
        if not projects:
            QMessageBox.information(
                self.iface.mainWindow(), title,
                "This GeoPackage does not contain a QGIS project, so it is not a MapBundle.\n\n"
                "Its layers can still be added from the Browser panel; any styles saved in it "
                "load automatically.")
            return None
        name = projects[0]
        if len(projects) > 1:
            name, ok = QInputDialog.getItem(self.iface.mainWindow(), title,
                                            "This GeoPackage contains several projects:",
                                            projects, 0, False)
            if not ok:
                return None
        return importer.inspect_bundle(path, name)

    def _confirm_fonts(self, info) -> bool:
        lines = []
        for family in info.missing_families:
            licences = sorted({f.licence for f in info.missing_fonts if f.family == family})
            lines.append(f"• {family} ({', '.join(licences)})")
        answer = QMessageBox.question(
            self.iface.mainWindow(), "MapBundle fonts",
            "This MapBundle uses fonts that are not installed on this computer:\n\n"
            + "\n".join(lines)
            + "\n\nInstall them for QGIS now? (Only QGIS uses them; no admin rights needed.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        return answer == QMessageBox.StandardButton.Yes

    def _handle_fonts(self, info):
        if not info.missing_fonts:
            return
        if not self._confirm_fonts(info):
            self._declined_fonts.add(os.path.normcase(info.path))
            return
        families, errors = importer.install_missing_fonts(info)
        if errors:
            QMessageBox.warning(self.iface.mainWindow(), "MapBundle fonts",
                                "Some fonts could not be installed:\n\n" + "\n".join(errors))
        elif families:
            self.iface.messageBar().pushMessage(
                TITLE, "Installed fonts: " + ", ".join(families), Qgis.MessageLevel.Info, 5)

    def add_bundle(self):
        info = self._choose_bundle("Add MapBundle to Project")
        if info is None:
            return
        self._handle_fonts(info)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            group, error = importer.add_to_project(info, QgsProject.instance())
        finally:
            QApplication.restoreOverrideCursor()
        if group is None:
            QMessageBox.warning(self.iface.mainWindow(), "Add MapBundle to Project", error)
            return
        self.iface.messageBar().pushMessage(
            TITLE, f"Added '{info.project_name}' as group '{group.name()}'", Qgis.MessageLevel.Success, 5)
        if len(QgsProject.instance().mapLayers()) == len(group.findLayers()):
            self.iface.mapCanvas().zoomToFullExtent()

    def open_bundle_as_project(self):
        info = self._choose_bundle("Open MapBundle as Project")
        if info is None:
            return
        if not self.iface.newProject(True):
            return
        self._handle_fonts(info)
        self._busy = True
        try:
            self.iface.addProject(info.uri)
        finally:
            self._busy = False

    def _on_project_read(self, *_args):
        """Offer bundled fonts when a MapBundle project is opened another way (e.g. Browser)."""
        if self._busy:
            return
        uri = QgsProject.instance().fileName()
        parsed = parse_project_uri(uri)
        if not parsed or os.path.normcase(parsed[0]) in self._declined_fonts:
            return
        if not is_mapbundle(*parsed):
            return
        info = importer.inspect_bundle(*parsed)
        if info.missing_fonts:
            QTimer.singleShot(0, lambda: self._offer_fonts_and_reload(info, uri))

    def _offer_fonts_and_reload(self, info, uri):
        self._handle_fonts(info)
        if info.missing_fonts:  # declined or failed
            return
        # Labels look fonts up when the project is read, so reload it.
        QgsProject.instance().setDirty(False)
        self._busy = True
        try:
            self.iface.addProject(uri)
        finally:
            self._busy = False
