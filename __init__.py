"""Copernicus Disaster Explorer Italia — QGIS plugin package."""
def classFactory(iface):
    from .copernicus_disaster_explorer import CopernicusDisasterExplorer
    return CopernicusDisasterExplorer(iface)
