"""Disaster event data sources: CEMS Rapid Mapping and EFFIS.

Queries the Copernicus Emergency Management Service (CEMS) and
the European Forest Fire Information System (EFFIS) for confirmed
disaster events in Italy, returning structured event objects.
"""
import re
import logging

from .config import (
    CEMS_API_URL, EFFIS_WFS_URL,
    DISASTER_CONFIG, DisasterType, ITALIAN_REGIONS,
    DTYPE_TO_CEMS_SLUGS, EFFIS_TIMEOUT_MS,
)
from .network import get_json, get_text, NetworkError

logger = logging.getLogger("CDE.events")


class DisasterEvent:
    """A confirmed disaster event from any source."""
    def __init__(self, source, event_type, name, date, lat, lon,
                 bbox=None, magnitude=None, area_ha=None,
                 province=None, code=None, geometry_wkt=None):
        self.source = source
        self.event_type = event_type
        self.name = name
        self.date = date
        self.lat = lat
        self.lon = lon
        self.bbox = bbox
        self.magnitude = magnitude
        self.area_ha = area_ha
        self.province = province
        self.code = code
        self.geometry_wkt = geometry_wkt

    @property
    def display_name(self):
        parts = [self.name]
        if self.province:
            parts.append(f"({self.province})")
        if self.magnitude:
            parts.append(f"M{self.magnitude:.1f}")
        if self.area_ha:
            parts.append(f"{self.area_ha:.0f} ha")
        return " ".join(parts)

    @property
    def search_bbox(self):
        if self.bbox:
            return self.bbox
        buf = 0.5
        return (self.lon - buf, self.lat - buf, self.lon + buf, self.lat + buf)


def _dtype_matches_cems_slug(dtype, slug):
    """Check if a CEMS category slug matches the given DisasterType."""
    return slug in DTYPE_TO_CEMS_SLUGS.get(dtype, [])


# Keywords that indicate a landslide even when CEMS categorises the event
# under a different slug (typically "flood" for storm-triggered landslides).
_LANDSLIDE_KEYWORDS = (
    "landslide", "frana", "mass movement", "mudslide", "mudflow",
    "debris flow", "slope failure", "collapse", "ground deformation",
    "niscemi",
)


def search_cems_events(category=None, feedback=None):
    """Query CEMS Rapid Mapping API for Italy events.

    Paginates through up to 500 results to find events for
    rare categories (volcanoes, landslides) that may not appear
    in the most recent 100 activations.

    For landslide searches, also includes flood-category events
    whose name mentions landslide-related keywords — this is
    necessary because CEMS frequently classifies storm-triggered
    landslides under the 'flood' category.
    """
    events = []
    seen_codes = set()
    max_pages = 5  # Up to 500 events
    try:
        url = f"{CEMS_API_URL}?limit=100"
        for page in range(max_pages):
            if not url:
                break
            data = get_json(url, feedback=feedback)
            results = data.get("results", [])
            for item in results:
                countries = [c.get("short_name", "") for c in item.get("countries", [])]
                if "Italy" not in countries:
                    continue
                code = item.get("code", "")
                if code in seen_codes:
                    continue
                cat_slug = item.get("category", {}).get("slug", "")
                item_name = item.get("name", "?").strip()

                # Primary match: category slug matches directly
                direct_match = (not category) or _dtype_matches_cems_slug(category, cat_slug)

                # Cross-category match for LANDSLIDE:
                # 1) Include flood/storm events with landslide keywords in name
                # 2) Include ALL recent flood/storm events (first 2 pages)
                #    because in Italy major floods almost always include landslides.
                #    These are tagged with "[Alluvione]" prefix so the user knows.
                cross_match = False
                cross_is_generic = False
                if (category == DisasterType.LANDSLIDE and not direct_match
                        and cat_slug in ("flood", "storm")):
                    name_lower = item_name.lower()
                    if any(kw in name_lower for kw in _LANDSLIDE_KEYWORDS):
                        cross_match = True
                    elif page < 1:
                        # Include recent floods as potential landslide events
                        cross_match = True
                        cross_is_generic = True

                if not direct_match and not cross_match:
                    continue

                dtype = _cems_category_to_dtype(cat_slug) if direct_match else category
                if dtype is None:
                    continue

                centroid = item.get("centroid", "")
                lat, lon = _parse_point_wkt(centroid)
                if lat is None:
                    continue
                date_str = (item.get("activationTime") or "")[:10]
                seen_codes.add(code)
                display_name = item_name
                if cross_is_generic:
                    display_name = f"{item_name} (possibile frana)"
                events.append(DisasterEvent(
                    source="cems",
                    event_type=dtype,
                    name=display_name,
                    date=date_str,
                    lat=lat, lon=lon,
                    code=code,
                ))
            # Stop early if we found enough for common categories
            if len(events) >= 10 and category in (DisasterType.FLOOD, DisasterType.FIRE):
                break
            # Continue to next page for rare categories
            url = data.get("next")
            # Fix http -> https if needed
            if url and url.startswith("http://"):
                url = "https://" + url[7:]
    except NetworkError as exc:
        logger.warning("CEMS API error: %s", exc)
    return events


def search_effis_fires(bbox=None, feedback=None):
    """Query EFFIS WFS for burnt areas in Italy.
    Falls back to CEMS fire events if EFFIS is unreachable.
    """
    events = []
    try:
        west, south, east, north = bbox or ITALIAN_REGIONS["Tutta Italia"]
        url = (
            f"{EFFIS_WFS_URL}?service=WFS&version=1.1.0"
            f"&request=GetFeature&typeName=modis.ba.poly"
            f"&bbox={south},{west},{north},{east}"
            f"&maxFeatures=100&sortBy=FIREDATE+D"
        )
        gml_text = get_text(url, feedback=feedback, timeout_ms=EFFIS_TIMEOUT_MS)
        features = gml_text.split("<gml:featureMember>")[1:]
        for feat_xml in features:
            fields = dict(re.findall(r"<ms:(\w+)>([^<]*)</ms:", feat_xml))
            fire_date = fields.get("FIREDATE", "")[:10]
            province = fields.get("PROVINCE", "")
            commune = fields.get("COMMUNE", "")
            area_ha = float(fields.get("AREA_HA", 0) or 0)
            coords_match = re.search(r"<gml:coordinates[^>]*>([^<]+)</gml:coordinates>", feat_xml)
            lat, lon = None, None
            bbox_poly = None
            if coords_match:
                coords_text = coords_match.group(1).strip()
                pairs = coords_text.split(" ")
                if pairs:
                    lons, lats = [], []
                    for pair in pairs:
                        parts = pair.split(",")
                        if len(parts) >= 2:
                            try:
                                lons.append(float(parts[0]))
                                lats.append(float(parts[1]))
                            except ValueError:
                                continue
                    if lons and lats:
                        lon = sum(lons) / len(lons)
                        lat = sum(lats) / len(lats)
                        bbox_poly = (min(lons), min(lats), max(lons), max(lats))
            if lat is None or not fire_date:
                continue
            name = commune or province or "Incendio"
            events.append(DisasterEvent(
                source="effis",
                event_type=DisasterType.FIRE,
                name=f"Incendio {name}",
                date=fire_date,
                lat=lat, lon=lon,
                bbox=bbox_poly,
                area_ha=area_ha,
                province=province,
                code=fields.get("id", ""),
            ))
    except NetworkError as exc:
        logger.warning("EFFIS WFS error: %s", exc)
    except Exception as exc:
        logger.warning("EFFIS unexpected error: %s", exc)

    # Fallback: if EFFIS returned nothing, try CEMS fire events
    if not events:
        logger.info("EFFIS returned no results, falling back to CEMS fire events...")
        try:
            events = search_cems_events(category=DisasterType.FIRE, feedback=feedback)
        except Exception as exc:
            logger.warning("CEMS fire fallback also failed: %s", exc)

    return events



def _cems_category_to_dtype(slug):
    mapping = {
        "flood": DisasterType.FLOOD,
        "fire": DisasterType.FIRE,
        "volcan": DisasterType.VOLCANO,
        "volcanic-activity": DisasterType.VOLCANO,
        "volcano": DisasterType.VOLCANO,
        "mass-movement": DisasterType.LANDSLIDE,
        "mass": DisasterType.LANDSLIDE,
        "landslide": DisasterType.LANDSLIDE,
    }
    return mapping.get(slug)


def _parse_point_wkt(wkt):
    match = re.search(r"POINT\s*\(([\d.\-]+)\s+([\d.\-]+)\)", wkt or "")
    if match:
        return float(match.group(2)), float(match.group(1))
    return None, None
