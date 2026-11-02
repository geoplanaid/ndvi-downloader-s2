"""NDVI Downloader Sentinel-2 - QGIS plugin by Geoplana."""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import NdviDownloaderPlugin

    return NdviDownloaderPlugin(iface)
