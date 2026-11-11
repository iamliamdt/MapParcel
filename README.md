# MapParcel (QGIS plugin)

Share styled layers as **one GeoPackage** that looks the same on any computer. It keeps:
- layers, numbered in drawing order
- styles
- SVG and image symbols
- fonts
- the project itself

No server needed.

Requires QGIS 3.44 or newer, and is written to also run on QGIS 4 (Qt 6).

## Using it

All actions are under *Plugins → MapParcel*.

**Export MapParcel…** (students)

Exports the whole project, or only some layers. To export some layers, either:
- right-click layers in the Layers panel → **Export Selected Layers as MapParcel…**, or
- untick layers in the export dialog's layer list. The **All**, **Visible only** and **Selected in Layers panel** buttons set the ticks quickly.

Layers that a chosen layer depends on (table joins, relations) are added automatically, and the report says which ones. Empty groups are dropped.

1. If the project has unsaved changes, the plugin asks to save it first, because the parcel is built from the saved project.
2. Choose the layers, the output file and a name. Optionally:
   - only include data within the current map view
   - pick a tile format for raster layers
   - choose whether fonts are bundled
   - also save a separate `<name>_assets.zip` with copies of the SVGs, images and rasters used
3. A report lists what was packaged and anything to check.

**Add MapParcel to Project…** (recipients)

- Installs any missing bundled fonts as QGIS user fonts (no admin rights needed).
- Adds the layers to the **open project** as a collapsed group `map_parcel_1`, `map_parcel_2`, … at the bottom of the Layers panel. The number is one higher than the highest existing `map_parcel_N` group.
- Styles, visibility, subgroups and drawing order are kept.
- If the project was empty, it also takes the parcel's coordinate system.

**Open MapParcel as Project…** replaces the current project with the parcel's full original project, including print layouts.

**Without the plugin:**
- *Project → Open From → GeoPackage* opens the full project.
- Dragging the .gpkg into QGIS adds all its layers, stacked in the right order thanks to the `01_`, `02_`, … table names.
- Each layer brings its own style.
- Fonts that aren't installed are replaced with a substitute.

## What's inside a MapParcel

| Content | How |
|---|---|
| Vector layers (files, databases, scratch layers) | Copied as tables named `NN_layer_name`, where `01` is drawn on top. Expression and joined fields stay defined in the project. |
| Local raster layers | Copied as GeoPackage raster tiles, with overviews. Byte rasters use the chosen tile format; other data types (e.g. DEMs) stay lossless. |
| Layer styles | Saved in the project, and also saved as each table's default style. |
| SVG and image symbols | Embedded as base64. This covers markers, fills, label backgrounds and layout pictures. |
| Fonts | The font files are attached to the stored project. Only fonts whose licence clearly allows sharing (OFL, Apache, GPL, …) are bundled, unless you choose otherwise. |
| Project | Stored in `qgis_projects` with relative paths. |

Fonts and parcel information travel inside the project, so the GeoPackage contains no extra tables and QGIS only ever lists real layers.

The project you have open is never modified: the export works on a private copy.

## Known limits

- **Not embedded automatically:** SVG paths or fonts chosen by an expression (data-defined). The report lists them.
- **Kept as links:** online layers (WMS, XYZ, vector tiles) and other layer types (mesh, point cloud). Recipients need internet access for online layers.
- **Renamed files:** a renamed file only keeps working on computers with the plugin installed. Without the plugin, keep the exported file name.
- **Scratch layers:** these must still be open in QGIS when you export.
- **Subset exports:** print layouts are kept but may refer to layers that weren't included.
- **Add to Project:** brings layers only. Layouts and map themes need *Open MapParcel as Project…*.

## Publishing on plugins.qgis.org

1. Replace the placeholders in `map_parcel/metadata.txt`: `author`, `email`, `homepage`, `repository`, `tracker`.
2. Push this repository to a public GitHub repo.
3. Run `python make_zip.py`. This creates `dist/map_parcel-<version>.zip`.
4. Sign in at <https://plugins.qgis.org> with an OSGeo ID and upload the zip.
5. For later releases, bump `version` and `changelog` in `metadata.txt` and upload a new zip.

Until the plugin is approved, share the zip directly: *Plugins → Manage and Install Plugins → Install from ZIP*.

## Development

The code is split into a GUI layer and a core that doesn't need the GUI:

- `map_parcel/plugin.py` and `dialogs.py`: QGIS GUI.
- `map_parcel/core/`: the logic, which also runs without the GUI.
  - `packager.py`: the export pipeline.
  - `xmlscan.py`: embeds images and finds fonts in the project XML.
  - `fonts.py`: reads font names and licences, stores and installs fonts.
  - `parcel.py`: the file format (project URIs, attachments).
  - `pathfix.py`: redirects renamed GeoPackages.
  - `importer.py`: font install, and adding a parcel to a project as a group.
- `tools/make_icon2.py`: draws the plugin icon (option F). `tools/make_icon.py` holds earlier icon drafts.
- `tools/make_toolbar_icons.py`: draws the toolbar icons (Export: blue arrow badge; Add: green plus) into `map_parcel/icons/`.

### Tests

The tests are end to end. They build a styled project with:
- custom SVG/PNG symbols
- a font marker
- a label with an SVG background
- a font the recipient doesn't have
- a scratch layer
- byte and float rasters

Then they export it, rename the file, delete the sources, open it in a fresh profile and compare renders pixel by pixel.

```bash
QGIS_PYTHON=/path/to/qgis/python tests/run_tests.sh
```

## Licence

GPL-2.0-or-later (see `LICENSE`).
