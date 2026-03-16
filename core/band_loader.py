"""Band extraction and VRT composition for Sentinel imagery.

Extracts individual spectral bands from downloaded .SAFE products,
creates multi-band VRT composites, and applies false-color
symbology appropriate for each disaster type.
"""
import os
import zipfile
import logging

from qgis.core import (
    QgsRasterLayer, QgsProject, QgsLayerTreeGroup,
    QgsContrastEnhancement, QgsMultiBandColorRenderer,
)

from .config import DISASTER_CONFIG

logger = logging.getLogger("CDE.bands")


def extract_safe(zip_path, target_dir=None):
    """Extract a Sentinel ZIP. Returns the .SAFE directory path."""
    if target_dir is None:
        target_dir = os.path.dirname(zip_path)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            top_dirs = set()
            for name in zf.namelist():
                parts = name.split("/")
                if parts[0]:
                    top_dirs.add(parts[0])
            zf.extractall(target_dir)
        for d in top_dirs:
            candidate = os.path.join(target_dir, d)
            if os.path.isdir(candidate):
                return candidate
        return target_dir
    except zipfile.BadZipFile:
        logger.error("ZIP non valido: %s", zip_path)
        return None
    except Exception as exc:
        logger.error("Errore estrazione: %s", exc)
        return None


def find_s2_bands(safe_dir, band_ids):
    """Find Sentinel-2 band files. Returns {band_id: filepath}."""
    found = {}
    all_images = []
    for root, dirs, files in os.walk(safe_dir):
        for fname in files:
            if fname.lower().endswith((".jp2", ".tif", ".tiff")):
                all_images.append(os.path.join(root, fname))
    for band_id in band_ids:
        best = None
        preferred = None
        for fpath in all_images:
            fname_upper = os.path.basename(fpath).upper()
            if f"_{band_id}_" in fname_upper or f"_{band_id}." in fname_upper:
                if "10M" in fname_upper:
                    preferred = fpath
                if best is None:
                    best = fpath
        found[band_id] = preferred or best
    return found


def find_s1_bands(safe_dir):
    """Find Sentinel-1 polarization bands. Returns {pol: filepath}."""
    found = {}
    extensions = (".tiff", ".tif", ".nc")
    for root, dirs, files in os.walk(safe_dir):
        for fname in files:
            fl = fname.lower()
            if not any(fl.endswith(ext) for ext in extensions):
                continue
            fpath = os.path.join(root, fname)
            for pol in ("vv", "vh", "hh", "hv"):
                markers = (f"-{pol}-", f"_{pol}_", f"_{pol}.", f"-{pol}.")
                if any(m in fl for m in markers):
                    if pol not in found:
                        found[pol] = fpath
                    elif fpath.lower().endswith(".tiff") and not found[pol].lower().endswith(".tiff"):
                        found[pol] = fpath
                    break
    return found


def load_bands_into_qgis(safe_dir, disaster_type, display_name):
    """Load relevant bands into QGIS with symbology. Returns list of layers.

    For Sentinel-2: creates a GDAL VRT combining bands into a single
    multi-band layer with false-color rendering.
    For Sentinel-1: loads VV/VH as individual layers with SAR stretch.
    """
    cfg = DISASTER_CONFIG.get(disaster_type, {})
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.insertGroup(0, display_name)
    loaded = []

    # Detect sensor from SAFE directory name
    safe_name = os.path.basename(safe_dir).upper()
    is_s2 = "S2" in safe_name
    is_s1 = "S1" in safe_name

    if is_s2:
        band_ids = cfg.get("bands_s2", ["B04", "B08", "B12"])
        if not band_ids:
            band_ids = ["B04", "B08", "B12"]
        band_files = find_s2_bands(safe_dir, band_ids)

        # Determine false color mapping
        fc = cfg.get("false_color") or cfg.get("false_color_s2")

        # Try to create a multi-band VRT composite
        composite = _create_s2_composite(
            band_files, band_ids, fc, safe_dir, display_name
        )
        if composite and composite.isValid():
            project.addMapLayer(composite, False)
            group.addLayer(composite)
            loaded.append(composite)
        else:
            # Fallback: load individual bands
            for bid in band_ids:
                fpath = band_files.get(bid)
                if not fpath:
                    logger.warning("Banda %s non trovata", bid)
                    continue
                layer = QgsRasterLayer(fpath, f"{display_name} - {bid}")
                if layer.isValid():
                    project.addMapLayer(layer, False)
                    group.addLayer(layer)
                    _apply_optical_stretch(layer)
                    loaded.append(layer)

        # Extra band sets (e.g. natural color, geology)
        for extra in cfg.get("extra_band_sets", []):
            extra_bands = extra.get("bands", [])
            extra_fc = extra.get("fc", {})
            extra_label = extra.get("label", "Extra")
            # Find all unique bands needed
            all_needed = list(set(band_ids + extra_bands))
            all_files = find_s2_bands(safe_dir, all_needed)
            extra_composite = _create_s2_composite(
                all_files, extra_bands, extra_fc, safe_dir,
                f"{display_name} ({extra_label})"
            )
            if extra_composite and extra_composite.isValid():
                project.addMapLayer(extra_composite, False)
                node = group.addLayer(extra_composite)
                node.setItemVisibilityChecked(False)  # hidden by default
                loaded.append(extra_composite)
                logger.info("Extra composite loaded: %s", extra_label)

    elif is_s1:
        pols = cfg.get("bands_s1", ["vv", "vh"])
        if not pols:
            pols = ["vv", "vh"]
        band_files = find_s1_bands(safe_dir)
        for pol in pols:
            fpath = band_files.get(pol)
            if not fpath:
                continue
            layer = QgsRasterLayer(fpath, f"{display_name} - {pol.upper()}")
            if layer.isValid():
                project.addMapLayer(layer, False)
                group.addLayer(layer)
                _apply_sar_stretch(layer)
                loaded.append(layer)
    else:
        # Unknown sensor: try S2 composite, then S1
        if cfg.get("bands_s2"):
            band_files = find_s2_bands(safe_dir, cfg["bands_s2"])
            fc = cfg.get("false_color") or cfg.get("false_color_s2")
            composite = _create_s2_composite(
                band_files, cfg["bands_s2"], fc, safe_dir, display_name
            )
            if composite and composite.isValid():
                project.addMapLayer(composite, False)
                group.addLayer(composite)
                loaded.append(composite)
        if not loaded and cfg.get("bands_s1"):
            band_files = find_s1_bands(safe_dir)
            for pol in cfg["bands_s1"]:
                fpath = band_files.get(pol)
                if fpath:
                    layer = QgsRasterLayer(fpath, f"{display_name} - {pol.upper()}")
                    if layer.isValid():
                        project.addMapLayer(layer, False)
                        group.addLayer(layer)
                        _apply_sar_stretch(layer)
                        loaded.append(layer)

    if not loaded:
        root.removeChildNode(group)
    return loaded


def _create_s2_composite(band_files, band_ids, false_color, safe_dir,
                          display_name):
    """Create a multi-band VRT from S2 bands and apply false-color rendering.

    Uses GDAL BuildVRT to combine separate band files into a single
    multi-band virtual raster, then applies QgsMultiBandColorRenderer.

    Args:
        band_files: dict {band_id: filepath}
        band_ids: list of band IDs in config order
        false_color: dict {R: band_id, G: band_id, B: band_id} or None
        safe_dir: SAFE directory path (for VRT output location)
        display_name: layer display name

    Returns:
        QgsRasterLayer with multi-band renderer, or None on failure.
    """
    try:
        from osgeo import gdal
        gdal.UseExceptions()
    except ImportError:
        logger.warning("GDAL Python bindings not available for VRT creation")
        return None

    # Collect source files in order
    sources = []
    available_ids = []
    for bid in band_ids:
        fpath = band_files.get(bid)
        if fpath and os.path.exists(fpath):
            sources.append(fpath)
            available_ids.append(bid)

    if len(sources) < 2:
        return None

    # Build VRT
    vrt_name = display_name.replace(" ", "_").replace("/", "-")[:50]
    vrt_path = os.path.join(safe_dir, f"{vrt_name}_composite.vrt")
    try:
        vrt_options = gdal.BuildVRTOptions(separate=True, resolution="highest")
        vrt_ds = gdal.BuildVRT(vrt_path, sources, options=vrt_options)
        if vrt_ds is None:
            return None
        vrt_ds.FlushCache()
        vrt_ds = None
    except Exception as exc:
        logger.warning("VRT creation failed: %s", exc)
        return None

    # Determine label from false color config
    if false_color:
        fc_label = f"R={false_color.get('R','?')} G={false_color.get('G','?')} B={false_color.get('B','?')}"
    else:
        fc_label = "/".join(available_ids)
    layer = QgsRasterLayer(vrt_path, f"{display_name} [{fc_label}]")
    if not layer.isValid():
        return None

    # Map band IDs to VRT band numbers (1-based)
    id_to_vrt_band = {bid: i + 1 for i, bid in enumerate(available_ids)}

    # Determine RGB band assignment
    if false_color:
        r_band = id_to_vrt_band.get(false_color.get("R"), 1)
        g_band = id_to_vrt_band.get(false_color.get("G"), 2)
        b_band = id_to_vrt_band.get(false_color.get("B"), 3)
    else:
        # Default: first 3 bands in order
        r_band, g_band, b_band = 1, 2, min(3, len(sources))

    # Apply multi-band renderer
    renderer = QgsMultiBandColorRenderer(
        layer.dataProvider(), r_band, g_band, b_band
    )

    # Apply contrast enhancement per channel (mean +/- 2 stddev)
    dp = layer.dataProvider()
    for band_num, setter in [
        (r_band, renderer.setRedContrastEnhancement),
        (g_band, renderer.setGreenContrastEnhancement),
        (b_band, renderer.setBlueContrastEnhancement),
    ]:
        try:
            stats = dp.bandStatistics(band_num)
            ce = QgsContrastEnhancement(dp.dataType(band_num))
            ce.setContrastEnhancementAlgorithm(
                QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum
            )
            new_min = max(0, stats.mean - 2 * stats.stdDev)
            new_max = stats.mean + 2 * stats.stdDev
            if new_max <= new_min:
                new_max = new_min + 1
            ce.setMinimumValue(new_min)
            ce.setMaximumValue(new_max)
            setter(ce)
        except Exception:
            pass

    layer.setRenderer(renderer)
    layer.triggerRepaint()
    logger.info("S2 composite created: %s bands, RGB=%d/%d/%d",
                 len(sources), r_band, g_band, b_band)
    return layer


def _apply_sar_stretch(layer):
    """Apply mean +/- 2 stddev contrast stretch for SAR imagery.

    SAR amplitude data has a very skewed distribution with occasional
    very high values. A simple min-max stretch makes everything appear
    black. Using mean +/- 2*stddev captures 95% of the data range.
    """
    try:
        renderer = layer.renderer()
        if renderer is None:
            return
        dp = layer.dataProvider()
        stats = dp.bandStatistics(1)
        new_min = max(stats.minimumValue, stats.mean - 2 * stats.stdDev)
        new_max = stats.mean + 2 * stats.stdDev
        if new_max <= new_min:
            new_max = new_min + 1

        ce = QgsContrastEnhancement(dp.dataType(1))
        ce.setContrastEnhancementAlgorithm(
            QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum
        )
        ce.setMinimumValue(new_min)
        ce.setMaximumValue(new_max)
        if hasattr(renderer, 'setContrastEnhancement'):
            renderer.setContrastEnhancement(ce)
        layer.triggerRepaint()
    except Exception as exc:
        logger.debug("SAR stretch failed: %s", exc)


def _apply_optical_stretch(layer):
    """Apply min-max contrast stretch for optical (S2) bands."""
    try:
        renderer = layer.renderer()
        if renderer is None:
            return
        dp = layer.dataProvider()
        stats = dp.bandStatistics(1)
        ce = QgsContrastEnhancement(dp.dataType(1))
        ce.setContrastEnhancementAlgorithm(
            QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum
        )
        ce.setMinimumValue(stats.minimumValue)
        ce.setMaximumValue(stats.maximumValue)
        if hasattr(renderer, 'setContrastEnhancement'):
            renderer.setContrastEnhancement(ce)
        layer.triggerRepaint()
    except Exception as exc:
        logger.debug("Optical stretch failed: %s", exc)
