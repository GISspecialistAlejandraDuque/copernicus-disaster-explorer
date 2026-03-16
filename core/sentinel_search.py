"""Sentinel-2 and Sentinel-1 product search on Copernicus Data Space.

Searches the CDSE OData catalog for satellite imagery matching
a geographic area, date range, and cloud cover threshold.
"""
import logging
from .config import CDSE_CATALOG_URL, CDSE_DOWNLOAD_URL, DISASTER_CONFIG
from .network import get_json, NetworkError
from .auth_manager import AuthManager

logger = logging.getLogger("CDE.search")


class SentinelProduct:
    """A single Sentinel product from Copernicus catalog."""
    def __init__(self, pid, name, collection, date, cloud, size_mb, online,
                 footprint=None):
        self.product_id = pid
        self.name = name
        self.collection = collection
        self.sensing_date = date
        self.cloud_cover = cloud
        self.size_mb = size_mb
        self.online = online
        self.footprint = footprint  # list of [lon, lat] pairs or None

    @property
    def download_url(self):
        return f"{CDSE_DOWNLOAD_URL}({self.product_id})/$value"

    @property
    def sensor_label(self):
        return "S1-SAR" if "S1" in self.name else "S2-OPT"

    @property
    def cloud_display(self):
        if self.cloud_cover is None or self.cloud_cover < 0:
            return "SAR" if "S1" in self.name else "N/A"
        return f"{self.cloud_cover:.0f}%"

    @property
    def size_display(self):
        if not self.size_mb:
            return "?"
        return f"{self.size_mb / 1024:.1f} GB" if self.size_mb >= 1024 else f"{self.size_mb:.0f} MB"

    @property
    def is_pre_event(self):
        return getattr(self, '_is_pre', False)

    @is_pre_event.setter
    def is_pre_event(self, val):
        self._is_pre = val

    def covers_point(self, lat, lon):
        """Check if this product's footprint covers a given point.

        Uses the ray-casting algorithm for a proper point-in-polygon
        test on the GeoJSON footprint. This is necessary because
        Sentinel-2 tiles have rotated polygons in the UTM grid —
        a simple bounding-box check produces false positives.
        """
        if not self.footprint:
            return True  # no footprint info, assume ok
        try:
            return _point_in_polygon(lon, lat, self.footprint)
        except (IndexError, TypeError, ValueError):
            return True


def _point_in_polygon(px, py, polygon):
    """Ray-casting point-in-polygon test (no external dependencies).

    Args:
        px, py: point coordinates (lon, lat).
        polygon: list of [lon, lat] coordinate pairs forming a closed ring.

    Returns:
        True if the point is inside the polygon.
    """
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    x1, y1 = polygon[0]
    for i in range(1, n + 1):
        x2, y2 = polygon[i % n]
        if py > min(y1, y2):
            if py <= max(y1, y2):
                if px <= max(x1, x2):
                    if y1 != y2:
                        xinters = (py - y1) * (x2 - x1) / (y2 - y1) + x1
                    if y1 == y2 or px <= xinters:
                        inside = not inside
        x1, y1 = x2, y2
    return inside


def search_sentinel_for_event(event, disaster_type, max_cloud=30,
                               max_results=20, feedback=None):
    """Search Copernicus for Sentinel products matching a disaster event.

    Returns (pre_event_products, post_event_products).
    Products are filtered to only include those whose footprint
    actually covers the event point.

    If the disaster config includes a secondary sensor (e.g. S2 for
    floods), both sensors are searched and results merged.
    """
    cfg = DISASTER_CONFIG.get(disaster_type, {})
    bbox = event.search_bbox

    from datetime import datetime, timedelta
    try:
        event_date = datetime.strptime(event.date, "%Y-%m-%d")
    except (ValueError, TypeError):
        logger.error("Invalid event date: %s", event.date)
        return [], []

    days_before = getattr(event, 'search_days_before', None)
    if days_before is None:
        days_before = cfg.get("search_days_before", 10)
    days_after = getattr(event, 'search_days_after', None)
    if days_after is None:
        days_after = cfg.get("search_days_after", 10)
    pre_start = (event_date - timedelta(days=days_before)).strftime("%Y-%m-%d")
    pre_end = event.date
    post_start = event.date
    post_end = (event_date + timedelta(days=days_after)).strftime("%Y-%m-%d")

    auth = AuthManager.instance()

    # --- Primary sensor search ---
    collection = cfg.get("sensor", "SENTINEL-2")
    product_filter = cfg.get("product_filter", "")
    cloud_relevant = cfg.get("cloud_relevant", False)

    pre_products = _do_search(
        collection, product_filter, bbox, pre_start, pre_end,
        cloud_relevant, max_cloud, max_results, auth, feedback,
    )
    post_products = _do_search(
        collection, product_filter, bbox, post_start, post_end,
        cloud_relevant, max_cloud, max_results, auth, feedback,
    )

    # --- Secondary sensor search (e.g. S2 for floods) ---
    secondary = cfg.get("sensor_secondary")
    if secondary:
        sec_filter = cfg.get("product_filter_secondary", "")
        # Don't filter by cloud cover server-side; let user see all
        # products and decide. Cloud cover values are often missing
        # from CDSE metadata anyway.
        sec_pre = _do_search(
            secondary, sec_filter, bbox, pre_start, pre_end,
            False, 100, max_results, auth, feedback,
        )
        sec_post = _do_search(
            secondary, sec_filter, bbox, post_start, post_end,
            False, 100, max_results, auth, feedback,
        )
        pre_products.extend(sec_pre)
        post_products.extend(sec_post)

    # --- Filter by footprint coverage ---
    pre_products = [p for p in pre_products if p.covers_point(event.lat, event.lon)]
    post_products = [p for p in post_products if p.covers_point(event.lat, event.lon)]

    # --- Mark pre/post and sort by date ---
    for p in pre_products:
        p.is_pre_event = True
    for p in post_products:
        p.is_pre_event = False

    pre_products.sort(key=lambda p: p.sensing_date or "", reverse=True)
    post_products.sort(key=lambda p: p.sensing_date or "", reverse=True)

    return pre_products, post_products


def _do_search(collection, product_filter, bbox, start, end,
                cloud_relevant, max_cloud, max_results, auth, feedback):
    """Execute a single OData search query."""
    west, south, east, north = bbox
    fp = f"POLYGON(({west} {south},{east} {south},{east} {north},{west} {north},{west} {south}))"
    parts = [
        f"Collection/Name eq '{collection}'",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{fp}')",
        f"ContentDate/Start gt {start}T00:00:00.000Z",
        f"ContentDate/Start lt {end}T23:59:59.999Z",
    ]
    if product_filter:
        parts.append(f"contains(Name,'{product_filter}')")
    if cloud_relevant:
        parts.append(
            f"Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq "
            f"'cloudCover' and att/OData.CSC.DoubleAttribute/Value le {max_cloud})"
        )
    odata_filter = " and ".join(parts)
    url = (
        f"{CDSE_CATALOG_URL}?$filter={odata_filter}"
        f"&$orderby=ContentDate/Start desc&$top={max_results}"
        f"&$expand=Attributes"
    )
    try:
        headers = auth.get_auth_headers(feedback)
        data = get_json(url, headers=headers, feedback=feedback)
    except Exception as exc:
        logger.error("Sentinel search failed: %s", exc)
        return []

    products = []
    for item in data.get("value", []):
        try:
            cd = item.get("ContentDate", {})
            cl = item.get("ContentLength", 0)
            cloud = None
            for attr in item.get("Attributes", []):
                if attr.get("Name") == "cloudCover":
                    cloud = float(attr.get("Value", -1))
            # Extract footprint
            footprint = None
            geo = item.get("GeoFootprint", {})
            if geo and geo.get("coordinates"):
                footprint = geo["coordinates"][0]
            products.append(SentinelProduct(
                item.get("Id", ""),
                item.get("Name", "?"),
                collection,
                cd.get("Start", "")[:10],
                cloud,
                cl / (1024 * 1024) if cl else None,
                item.get("Online", True),
                footprint=footprint,
            ))
        except Exception:
            continue
    return products
