"""MapParcel QGIS plugin."""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import MapParcelPlugin
    return MapParcelPlugin(iface)
