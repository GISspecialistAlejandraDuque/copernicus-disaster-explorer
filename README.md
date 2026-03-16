# Copernicus Disaster Explorer Italia

A QGIS plugin for exploring confirmed natural disaster events in Italy and loading
the Sentinel-2 satellite imagery that captured them, with automatic band selection
and false-color symbology for immediate visual analysis.

## Features

- Search confirmed disaster events from CEMS Rapid Mapping and EFFIS
- Four disaster types: flood, wildfire, volcano, landslide
- Automatic Sentinel-2 image search filtered by cloud cover
- Download with real-time progress bar and cancel support
- Automatic false-color symbology tailored to each disaster type
- PRE/POST event date comparison mode
- Educational interpretation guide with spectral band explanations
- Built-in exercises with real Italian disaster cases
- Interface in Italian, designed for GIS professionals

## Supported Disaster Types

| Type | Source | False Color | Bands |
|------|--------|-------------|-------|
| Flood | CEMS | SWIR2/NIR/Green | B12/B08/B03 |
| Wildfire | EFFIS | SWIR2/NIR/Red | B12/B08/B04 |
| Volcano | CEMS | SWIR2/SWIR1/Red | B12/B11/B04 |
| Landslide | CEMS | SWIR2/NIR/Red | B12/B08/B04 |

## Requirements

- QGIS 3.34 or later (including QGIS 4.x with Qt6)
- A free Copernicus Data Space account: https://dataspace.copernicus.eu

## Network

All API calls use `QgsBlockingNetworkRequest` to respect QGIS proxy settings.
Binary file downloads run in background threads using Python stdlib for thread safety.

## License

GNU General Public License v2.0 or later.

## Author

David Fernando Duque Ropero
