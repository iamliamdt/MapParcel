"""Export a QGIS project as a MapBundle: one self-contained GeoPackage.

Pipeline (the user's open project is never modified):
  1. Load a private copy of the saved project.
  2. Copy every vector layer, and every local raster layer, into the GeoPackage as
     tables numbered in drawing order (01_ = top), and point the layers at them.
  3. Embed every referenced SVG/image file into the project XML as base64.
  4. Save each vector layer's style as its default style inside the GeoPackage.
  5. Attach the font files and bundle info to the project, and save the project
     inside the GeoPackage (relative paths).
  6. Optionally write a separate zip with copies of the SVGs, images and rasters.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass

from osgeo import gdal

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDataProvider,
    QgsFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from . import fonts as fontlib
from .bundle import FONTS_ATTACHMENT, FORMAT_VERSION, INFO_ATTACHMENT, project_uri
from .report import Report, human_size
from .xmlscan import ProjectScanner, load_project_xml, save_project_xml

TILE_FORMATS = {
    "PNG_JPEG": "PNG + JPEG (default)",
    "WEBP": "WEBP (smallest)",
    "PNG": "PNG (lossless)",
    "JPEG": "JPEG (no transparency)",
}

LARGE_FONT_BYTES = 5 * 1024 * 1024


@dataclass
class ExportOptions:
    output_path: str
    project_name: str = "MapBundle"
    layer_ids: set | None = None   # None = every layer; else only these (+ their dependencies)
    extent: QgsRectangle | None = None
    extent_crs: QgsCoordinateReferenceSystem | None = None
    raster_tile_format: str = "PNG_JPEG"
    raster_quality: int = 80
    bundle_fonts: bool = True
    bundle_restricted_fonts: bool = False
    assets_zip: bool = False
    extra_font_dirs: tuple = ()


class ExportCancelled(Exception):
    pass


def _table_name(name: str, used: set, number: int, width: int) -> str:
    """'NN_layer_name': the number is the drawing position (01 = drawn on top), so
    adding all tables of the GeoPackage at once (alphabetical) stacks them correctly."""
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:50] or "layer"
    base = f"{number:0{width}d}_{slug}"
    candidate, i = base, 2
    while candidate in used:
        candidate = f"{base}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def _strip_bogus_tile_format(gpkg_path: str):
    """GDAL 3.x records PNG_JPEG tiles as 'JPEG_PNG' in its metadata and then warns
    "Unsupported value for TILE_FORMAT" every time the raster is opened. The entry is
    informational only, so remove it to keep the QGIS log clean."""
    try:
        con = sqlite3.connect(gpkg_path)
        try:
            con.execute("UPDATE gpkg_metadata SET metadata = "
                        "replace(metadata, '<MDI key=\"TILE_FORMAT\">JPEG_PNG</MDI>', '')")
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


class MapBundleExporter:
    def __init__(self, source_project: str, options: ExportOptions, feedback: QgsFeedback | None = None,
                 live_project: QgsProject | None = None):
        self.source_project = source_project
        # Memory (scratch) layers are not saved in project files, so their features are
        # read from the project currently open in QGIS.
        self.live_project = live_project
        self.options = options
        self.feedback = feedback or QgsFeedback()
        self.report = Report(output_path=options.output_path)
        self._tables: set = set()
        self._gpkg_exists = False
        self._numbers: dict = {}       # layer id -> drawing position
        self._width = 2
        self._raster_files: list = []  # (table, [source files]) for the assets zip

    # ------------------------------------------------------------------ utils

    def _progress(self, value: float):
        self.feedback.setProgress(value)
        if self.feedback.isCanceled():
            raise ExportCancelled()

    def _layer_extent(self, crs: QgsCoordinateReferenceSystem, transform_context) -> QgsRectangle | None:
        """The export extent expressed in ``crs`` (None = no limit)."""
        opts = self.options
        if opts.extent is None or opts.extent.isEmpty():
            return None
        src_crs = opts.extent_crs or crs
        if src_crs == crs:
            return QgsRectangle(opts.extent)
        ct = QgsCoordinateTransform(src_crs, crs, transform_context)
        ct.setBallparkTransformsAreAppropriate(True)
        return ct.transformBoundingBox(opts.extent)

    # ----------------------------------------------------------------- vector

    def _new_table(self, layer) -> str:
        return _table_name(layer.name(), self._tables, self._numbers[layer.id()], self._width)

    def _export_vector(self, layer: QgsVectorLayer, project: QgsProject) -> bool:
        out = self.options.output_path
        table = self._new_table(layer)
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = table
        opts.fileEncoding = "UTF-8"
        opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                     if self._gpkg_exists else
                                     QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
        opts.feedback = self.feedback

        # Only real fields: expression fields and joined fields stay defined in the
        # project and would otherwise appear twice.
        fields = layer.fields()
        keep = [i for i in range(fields.count())
                if fields.fieldOrigin(i) in (Qgis.FieldOrigin.Provider, Qgis.FieldOrigin.Edit)]
        opts.attributes = keep
        layer_options = []
        for i in keep:
            f = fields.at(i)
            ftype = getattr(f.type(), "value", f.type())  # Qt5 int / Qt6 enum
            if f.name().lower() == "fid" and ftype not in (2, 4):  # not Int/LongLong
                layer_options.append("FID=gpkg_fid")
                break
        if layer_options:
            opts.layerOptions = layer_options

        extent = self._layer_extent(layer.crs(), project.transformContext())
        if extent is not None:
            opts.filterExtent = extent

        source = layer
        if layer.providerType() == "memory":
            live = self.live_project.mapLayer(layer.id()) if self.live_project else None
            if isinstance(live, QgsVectorLayer) and live.providerType() == "memory":
                source = live
            else:
                self.report.warn(f"Scratch layer '{layer.name()}' is not open in QGIS, so its "
                                 "features could not be copied.")
            if source.featureCount() == 0:
                self.report.warn(f"Scratch layer '{layer.name()}' has no features (scratch layers "
                                 "are emptied when a project is closed). Save it to a file first.")
        err, msg, _new_file, _new_layer = QgsVectorFileWriter.writeAsVectorFormatV3(
            source, out, project.transformContext(), opts)
        if err != QgsVectorFileWriter.WriterError.NoError:
            self.report.error(f"Could not copy layer '{layer.name()}': {msg}")
            return False
        self._gpkg_exists = True

        subset = layer.subsetString()
        uri = f"{out}|layername={table}"
        layer.setDataSource(uri, layer.name(), "ogr", QgsDataProvider.ProviderOptions())
        if not layer.isValid():
            self.report.error(f"Layer '{layer.name()}' could not be re-opened from the GeoPackage")
            return False
        if subset:
            # The filter was already applied while copying.
            layer.setSubsetString("")
        detail = f"{layer.featureCount()} features → table '{table}'"
        if extent is not None:
            detail += " (limited to extent)"
        self.report.layers.append((layer.name(), detail))
        return True

    # ----------------------------------------------------------------- raster

    def _export_raster(self, layer: QgsRasterLayer, project: QgsProject) -> bool | None:
        """Copy a GDAL raster into the GeoPackage. None = not a local raster (left as is)."""
        if layer.providerType() != "gdal":
            return None
        src = gdal.OpenEx(layer.source(), gdal.OF_RASTER)
        if src is None:
            return None
        out = self.options.output_path
        table = self._new_table(layer)
        source_files = src.GetFileList() or []

        creation = [f"RASTER_TABLE={table}", f"QUALITY={self.options.raster_quality}"]
        if self._gpkg_exists:
            creation.append("APPEND_SUBDATASET=YES")
        band = src.GetRasterBand(1)
        is_byte = band.DataType == gdal.GDT_Byte
        if is_byte:
            fmt = self.options.raster_tile_format
            if band.GetColorTable() is not None and fmt in ("JPEG", "WEBP", "PNG_JPEG"):
                fmt = "PNG"  # paletted rasters must stay lossless
            creation.append(f"TILE_FORMAT={fmt}")
        # Non-byte rasters (e.g. a DEM styled with a colour ramp) keep their values;
        # GDAL stores them as lossless TIFF/PNG tiles.

        kwargs = {}
        extent = self._layer_extent(layer.crs(), project.transformContext())
        if extent is not None:
            extent = extent.intersect(layer.extent())
            if extent.isEmpty():
                self.report.warn(f"Raster '{layer.name()}' is outside the export extent and was skipped")
                src = None
                return False
            kwargs["projWin"] = [extent.xMinimum(), extent.yMaximum(), extent.xMaximum(), extent.yMinimum()]

        def progress(complete, _msg, _data):
            return 0 if self.feedback.isCanceled() else 1

        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            gdal.ErrorReset()
            ds = gdal.Translate(out, src, format="GPKG", creationOptions=creation,
                                callback=progress, **kwargs)
            err = gdal.GetLastErrorMsg()
        finally:
            gdal.PopErrorHandler()
        src = None
        if ds is None:
            self.report.error(f"Could not copy raster '{layer.name()}': {err or 'unknown GDAL error'}")
            return False
        ds = None
        self._gpkg_exists = True

        # Overviews so the basemap draws quickly when zoomed out.
        sub = f"GPKG:{out}:{table}"
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            ds = gdal.OpenEx(sub, gdal.OF_RASTER | gdal.OF_UPDATE)
        finally:
            gdal.PopErrorHandler()
        if ds is not None:
            levels, size = [], max(ds.RasterXSize, ds.RasterYSize)
            factor = 2
            while size / factor >= 256:
                levels.append(factor)
                factor *= 2
            if levels:
                paletted = ds.GetRasterBand(1).GetColorTable() is not None
                ds.BuildOverviews("NEAREST" if paletted or not is_byte else "AVERAGE", levels)
            ds = None

        _strip_bogus_tile_format(out)

        renderer = layer.renderer().clone() if layer.renderer() else None
        layer.setDataSource(sub, layer.name(), "gdal", QgsDataProvider.ProviderOptions())
        if not layer.isValid():
            self.report.error(f"Raster '{layer.name()}' could not be re-opened from the GeoPackage")
            return False
        if renderer is not None:
            layer.setRenderer(renderer)
        detail = f"raster → table '{table}'"
        if is_byte:
            detail += f" ({self.options.raster_tile_format} tiles)"
        self.report.layers.append((layer.name(), detail))
        self._raster_files.append((table, [f for f in source_files if os.path.isfile(f)]))
        return True

    # ------------------------------------------------------------------ fonts

    def _bundle_fonts(self, used_fonts: dict) -> bytes | None:
        """Zip of the font files to attach to the project (None = nothing to bundle)."""
        if not used_fonts:
            return None
        if not self.options.bundle_fonts:
            self.report.warn("Fonts were not bundled: " + ", ".join(sorted(used_fonts)))
            return None
        index = fontlib.FontIndex(extra_files=fontlib.qgis_user_font_files(),
                                  extra_dirs=list(self.options.extra_font_dirs))
        to_store = []
        for family in sorted(used_fonts):
            places = ", ".join(sorted(used_fonts[family]))
            files = index.files_for_family(family)
            if not files:
                self.report.warn(f"Font '{family}' (used in {places}) is not installed on this "
                                 "computer, so it could not be bundled.")
                continue
            licences = {f.licence_name for f in files}
            open_licence = None not in licences
            if not open_licence and not self.options.bundle_restricted_fonts:
                self.report.warn(
                    f"Font '{family}' (used in {places}) was not bundled: its licence does not "
                    "clearly allow sharing. Recipients need it installed, or switch to an open "
                    "font (e.g. from Google Fonts).")
                continue
            licence_label = ", ".join(sorted(l for l in licences if l)) or "licence unknown"
            if not open_licence:
                licence_label += " (bundled at your request: check you may share it)"
            size = sum(os.path.getsize(f.path) for f in files)
            if size > LARGE_FONT_BYTES:
                self.report.warn(f"Font '{family}' adds {human_size(size)} to the file.")
            to_store.extend((family, f) for f in files)
            self.report.fonts_bundled.append((family, len(files), licence_label))
        if not to_store:
            return None
        data, _total = fontlib.build_fonts_zip(to_store)
        return data

    # ----------------------------------------------------------------- assets

    def _write_assets_zip(self, embedded_paths: list) -> None:
        """Copies of the SVGs, images and raster files used, for reuse/editing.
        The GeoPackage does not depend on them."""
        target = os.path.splitext(self.options.output_path)[0] + "_assets.zip"
        entries, used = [], set()

        def add(folder: str, path: str):
            stem, ext = os.path.splitext(os.path.basename(path))
            arc, i = f"{folder}/{stem}{ext}", 2
            while arc.lower() in used:
                arc, i = f"{folder}/{stem}_{i}{ext}", i + 1
            used.add(arc.lower())
            entries.append((arc, path))

        for path in embedded_paths:
            add("svg" if path.lower().endswith(".svg") else "images", path)
        for table, files in self._raster_files:
            for path in files:  # one folder per raster keeps sidecar files (.aux.xml, .tfw) together
                add(f"rasters/{table}", path)
        if not entries:
            self.report.warn("No SVGs, images or raster files to put in the assets zip.")
            return
        if os.path.exists(target):
            os.remove(target)
        readme = ("Copies of the SVG symbols, images and raster files used by "
                  f"{os.path.basename(self.options.output_path)}.\n"
                  "The MapBundle does not need these files: everything is already inside it.\n"
                  "Rasters are the full original files (not clipped to an export extent).\n")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.writestr("README.txt", readme)
            for i, (arc, path) in enumerate(entries):
                self._progress(92 + 8 * i / len(entries))
                zf.write(path, arc)
        self.report.assets_zip = target
        self.report.assets_size = os.path.getsize(target)

    # ---------------------------------------------------------------- subset

    @staticmethod
    def _dependencies(project: QgsProject, layer) -> set:
        """Ids of layers ``layer`` needs to work: join targets, parents of relations,
        and declared layer dependencies."""
        deps = set()
        if isinstance(layer, QgsVectorLayer):
            deps |= {j.joinLayerId() for j in layer.vectorJoins()}
            for rel in project.relationManager().relations().values():
                if rel.referencingLayerId() == layer.id():
                    deps.add(rel.referencedLayerId())
        deps |= {d.layerId() for d in layer.dependencies()}
        return {d for d in deps if d and d != layer.id() and project.mapLayer(d) is not None}

    def _keep_only(self, project: QgsProject, wanted: set):
        wanted = {lid for lid in wanted if project.mapLayer(lid) is not None}
        if not wanted:
            raise RuntimeError("None of the chosen layers are in the saved project. Save the project and try again.")
        todo, added = list(wanted), []
        while todo:  # follow dependencies of dependencies
            for dep in self._dependencies(project, project.mapLayer(todo.pop())):
                if dep not in wanted:
                    wanted.add(dep)
                    todo.append(dep)
                    added.append(dep)
        for lid in added:
            self.report.warn(f"Also included '{project.mapLayer(lid).name()}', because a chosen "
                             "layer depends on it (join or relation).")
        removed = [lid for lid in project.mapLayers() if lid not in wanted]
        if removed:
            project.removeMapLayers(removed)
        self._prune_empty_groups(project.layerTreeRoot())
        if removed and project.layoutManager().layouts():
            self.report.warn("This is a subset of the project: its print layouts were kept but may "
                             "refer to layers that are not included.")

    def _prune_empty_groups(self, group):
        for child in list(group.children()):
            if child.nodeType() == child.NodeType.NodeGroup:
                self._prune_empty_groups(child)
                if not child.findLayers():
                    group.removeChildNode(child)

    # ------------------------------------------------------------------- main

    def run(self) -> Report:
        tmpdir = tempfile.mkdtemp(prefix="mapbundle_")
        projects = []
        try:
            self._run(tmpdir, projects)
            self.report.success = not self.report.errors
        except ExportCancelled:
            self.report.error("Export cancelled")
        except Exception as e:  # report instead of crashing QGIS
            import traceback
            self.report.error(f"Unexpected error: {e}\n{traceback.format_exc()}")
        finally:
            for p in projects:
                p.clear()
            shutil.rmtree(tmpdir, ignore_errors=True)
        if not self.report.success and os.path.exists(self.options.output_path) and self._gpkg_exists:
            try:
                os.remove(self.options.output_path)
            except OSError:
                pass
        if os.path.exists(self.options.output_path):
            self.report.output_size = os.path.getsize(self.options.output_path)
        return self.report

    def _run(self, tmpdir: str, projects: list):
        out = self.options.output_path
        if not out.lower().endswith(".gpkg"):
            out += ".gpkg"
            self.options.output_path = out
            self.report.output_path = out

        # 1. private copy of the project
        project = QgsProject()
        projects.append(project)
        if not project.read(self.source_project):
            raise RuntimeError(f"Could not read project: {project.error()}")
        self._progress(5)

        for layer in project.mapLayers().values():
            src = layer.source().split("|")[0]
            if src and os.path.exists(src) and _norm(src) == _norm(out):
                raise RuntimeError("The output file is used by a layer in this project. "
                                   "Choose a different file name.")
        for suffix in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(out + suffix):
                try:
                    os.remove(out + suffix)
                except OSError:
                    raise RuntimeError(f"Could not replace {out}: the file is in use. Close any "
                                       "project or layer using it (or restart QGIS), or choose "
                                       "another file name.")

        root = project.layerTreeRoot()
        if self.options.layer_ids is not None:
            self._keep_only(project, set(self.options.layer_ids))

        # 2. layers, numbered and exported in drawing order (top first)
        layers = [l for l in root.layerOrder() if l is not None]
        layers += [l for l in project.mapLayers().values() if l not in layers]
        self._width = max(2, len(str(len(layers))))
        self._numbers = {l.id(): n for n, l in enumerate(layers, start=1)}
        for i, layer in enumerate(layers):
            self._progress(5 + 60 * i / max(len(layers), 1))
            if not layer.isValid():
                self.report.warn(f"Layer '{layer.name()}' is broken in the source project and was skipped")
                project.removeMapLayer(layer.id())
                continue
            if layer.type() == Qgis.LayerType.Vector:
                if not self._export_vector(layer, project):
                    project.removeMapLayer(layer.id())
            elif layer.type() == Qgis.LayerType.Raster:
                done = self._export_raster(layer, project)
                if done is None:
                    self.report.warn(f"Layer '{layer.name()}' ({layer.providerType()}) is an online or "
                                     "non-file source; it was kept as a link and needs internet access.")
                elif not done:
                    project.removeMapLayer(layer.id())
            else:
                self.report.warn(f"Layer '{layer.name()}' ({layer.providerType()}) cannot be packaged; "
                                 "it was kept as a link to its original source.")
        if not self._gpkg_exists:
            raise RuntimeError("No layers could be packaged.")
        self._progress(65)

        # 3. embed images in the project XML
        project.setFilePathStorage(Qgis.FilePathType.Absolute)
        stage1 = os.path.join(tmpdir, "stage1.qgs")
        if not project.write(stage1):
            raise RuntimeError(f"Could not write temporary project: {project.error()}")
        doc = load_project_xml(stage1)
        scan = ProjectScanner(stage1).scan(doc, embed=True)
        stage2 = os.path.join(tmpdir, "stage2.qgs")
        save_project_xml(doc, stage2)
        self.report.embedded_files = sorted(scan.embedded.items())
        for path in scan.missing:
            self.report.warn(f"Image not found, so not embedded: {path}")
        for url in scan.remote:
            self.report.warn(f"Image loaded from the web (not embedded): {url}")
        for message in scan.data_defined:
            self.report.warn(f"Cannot embed automatically: {message}")
        project.clear()
        self._progress(75)

        # 4./5. reload the embedded project, save styles and the project into the GeoPackage
        final = QgsProject()
        projects.append(final)
        if not final.read(stage2):
            raise RuntimeError(f"Could not reload project: {final.error()}")
        for layer in final.mapLayers().values():
            if (layer.type() == Qgis.LayerType.Vector and layer.providerType() == "ogr"
                    and _norm(layer.source().split("|")[0]) == _norm(out)):
                if hasattr(layer, "saveStyleToDatabaseV2"):
                    _flags, msg = layer.saveStyleToDatabaseV2(layer.name(), "MapBundle", True, "")
                else:
                    msg = layer.saveStyleToDatabase(layer.name(), "MapBundle", True, "")
                if msg:
                    self.report.warn(f"Style of '{layer.name()}' not stored in the GeoPackage: {msg}")
        self._progress(80)

        # 5. attachments (fonts, bundle info) and the project itself
        fonts_zip = self._bundle_fonts(scan.fonts)
        if fonts_zip:
            with open(final.createAttachedFile(FONTS_ATTACHMENT), "wb") as fh:
                fh.write(fonts_zip)
        info = {"original_filename": os.path.basename(out), "format_version": FORMAT_VERSION,
                "project_name": self.options.project_name}
        with open(final.createAttachedFile(INFO_ATTACHMENT), "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        final.setFilePathStorage(Qgis.FilePathType.Relative)
        if not final.title():
            final.setTitle(self.options.project_name)
        uri = project_uri(out, self.options.project_name)
        if not final.write(uri):
            raise RuntimeError(f"Could not save the project into the GeoPackage: {final.error()}")
        self.report.project_uri = uri
        final.clear()
        self._progress(92)

        # 6. optional assets zip
        if self.options.assets_zip:
            self._write_assets_zip([p for p, _size in self.report.embedded_files])
        self._progress(100)


def export_bundle(source_project: str, options: ExportOptions, feedback: QgsFeedback | None = None,
                  live_project: QgsProject | None = None) -> Report:
    return MapBundleExporter(source_project, options, feedback, live_project).run()
