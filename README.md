# MapBundle

Share styled layers as one GeoPackage that looks the same on any computer. It keeps:
- layers, numbered in drawing order
- styles
- SVG and image symbols
- fonts

No server needed.

Requires QGIS 3.44 or newer, and is written to also run on QGIS 4 (Qt 6).

## Using it

All actions are under *Plugins → MapBundle*.

**Export MapBundle**

Exports the whole project, or only some layers. To export some layers, either:
- right-click layers in the Layers panel → **Export Selected Layers as MapBundle…**, or
- untick layers in the export dialog's layer list.

Layers that a chosen layer depends on (table joins, relations) are added automatically, and the report says which ones. Empty groups are dropped.

1. If the project has unsaved changes, the plugin asks to save it first, because the bundle is built from the saved project.
2. Choose the layers, the output file and a name. Optionally:
   - only include data within the current map view
   - pick a tile format for raster layers
   - choose whether fonts are bundled
   - also save a separate `<name>_assets.zip` with copies of the SVGs, images and rasters used
3. A report lists what was packaged and anything to check.

**Add MapBundle to Project**

- Installs any missing bundled fonts as QGIS user fonts (no admin rights needed).
- Adds the layers to the **open project** as a collapsed group `mapbundle_1`, `mapbundle_2`, … at the bottom of the Layers panel. The number is one higher than the highest existing `mapbundle_N` group.
- Styles, visibility, subgroups and drawing order are kept.
- If the project was empty, it also takes the bundle's coordinate system.

**Open MapBundle as Project** replaces the current project with the bundle's full original project, including print layouts.

**Without the plugin:**
- *Project → Open From → GeoPackage* opens the full project.
- Dragging the .gpkg into QGIS adds all its layers, stacked in the right order
- Each layer brings its own style.
- Fonts that aren't installed are replaced with a substitute.

The project you have open is never modified: the export works on a private copy.

## Known limits

- **Not embedded automatically:** SVG paths or fonts chosen by an expression (data-defined). The report lists them.
- **Kept as links:** online layers (WMS, XYZ, vector tiles) and other layer types (mesh, point cloud). Those importing a map bundle need internet access for online layers.
- **Renaming the file:** renaming the exported geopackage can cause issues with imports.
- **Scratch layers:** these must still be open in QGIS when you export, but don't need to be made permanent to be exported. When imported back, they are made permanent layers.
- **Subset exports:** print layouts are kept but may refer to layers that weren't included.
- **Add to Project:** brings layers only. Print Layouts and map themes need *Open MapBundle as Project…*.

## Licence

GPL-3.0.

## Disclaimer

AI generated code was used in the making of this plugin. Sorry about that... just wanted to finally have a plugin that streamlined importing and exporting of layers/styles/everything.
