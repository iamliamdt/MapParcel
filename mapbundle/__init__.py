"""MapBundle QGIS plugin."""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import MapBundlePlugin
    return MapBundlePlugin(iface)
