"""Background tasks for event search and image download.

Runs network-intensive operations (API queries, file downloads)
in QgsTask threads to keep the QGIS UI responsive.
"""
import os
import logging

from qgis.core import QgsTask, QgsApplication, QgsFeedback

from .event_sources import search_cems_events, search_effis_fires
from .sentinel_search import search_sentinel_for_event
from .auth_manager import AuthManager
from .network import download_to_file, NetworkError
from .band_loader import extract_safe, load_bands_into_qgis
from .config import DISASTER_CONFIG, DisasterType, DOWNLOAD_CHUNK_SIZE

logger = logging.getLogger("CDE.tasks")


class EventSearchTask(QgsTask):
    """Search for disaster events from CEMS/EFFIS (background)."""

    def __init__(self, disaster_type, bbox, start_date, end_date, on_ok, on_err):
        cfg = DISASTER_CONFIG.get(disaster_type, {})
        super().__init__(
            f"Ricerca eventi - {cfg.get('label_it', '')}",
            QgsTask.Flag.CanCancel,
        )
        self.disaster_type = disaster_type
        self.bbox = bbox
        self.start_date = start_date
        self.end_date = end_date
        self._on_ok = on_ok
        self._on_err = on_err
        self._events = []
        self._error = None

    def run(self):
        try:
            self.setProgress(10)
            cfg = DISASTER_CONFIG.get(self.disaster_type, {})
            source = cfg.get("event_source", "cems")
            fb = QgsFeedback()

            if self.isCanceled():
                return False

            if source == "effis":
                self.setProgress(30)
                self._events = search_effis_fires(
                    bbox=self.bbox, feedback=fb,
                )
            else:
                self.setProgress(30)
                self._events = search_cems_events(
                    category=self.disaster_type, feedback=fb,
                )

            if self.isCanceled():
                return False

            # Filter by date range
            if self.start_date and self.end_date:
                self._events = [
                    e for e in self._events
                    if self.start_date <= (e.date or "") <= self.end_date
                ]
            # Filter by bbox
            if self.bbox:
                w, s, e, n = self.bbox
                self._events = [
                    ev for ev in self._events
                    if s <= ev.lat <= n and w <= ev.lon <= e
                ]

            self.setProgress(90)
            self._events.sort(key=lambda ev: ev.date or "", reverse=True)
            self.setProgress(100)
            return True
        except Exception as exc:
            self._error = str(exc)
            return False

    def finished(self, ok):
        if self.isCanceled():
            return
        if ok:
            self._on_ok(self._events)
        else:
            self._on_err(self._error or "Errore nella ricerca eventi.")


class ImageSearchTask(QgsTask):
    """Search Sentinel imagery for a specific event (background)."""

    def __init__(self, event, disaster_type, max_cloud, on_ok, on_err):
        super().__init__("Ricerca immagini Sentinel", QgsTask.Flag.CanCancel)
        self.event = event
        self.disaster_type = disaster_type
        self.max_cloud = max_cloud
        self._on_ok = on_ok
        self._on_err = on_err
        self._pre = []
        self._post = []
        self._error = None

    def run(self):
        try:
            self.setProgress(10)
            if self.isCanceled():
                return False
            fb = QgsFeedback()
            self._pre, self._post = search_sentinel_for_event(
                self.event, self.disaster_type, self.max_cloud,
                feedback=fb,
            )
            self.setProgress(100)
            return True
        except Exception as exc:
            self._error = str(exc)
            return False

    def finished(self, ok):
        if self.isCanceled():
            return
        if ok:
            self._on_ok(self._pre, self._post)
        else:
            self._on_err(self._error or "Errore nella ricerca immagini.")


class DownloadAndLoadTask(QgsTask):
    """Download product with chunked progress, extract, load bands."""

    def __init__(self, product, download_dir, disaster_type, display_name,
                 on_ok, on_err):
        super().__init__(
            f"Download {product.name[:40]}...",
            QgsTask.Flag.CanCancel,
        )
        self.product = product
        self.download_dir = download_dir
        self.disaster_type = disaster_type
        self.display_name = display_name
        self._on_ok = on_ok
        self._on_err = on_err
        self._zip_path = None
        self._safe_dir = None
        self._error = None

    def run(self):
        try:
            self.setProgress(1)
            if self.isCanceled():
                return False

            auth = AuthManager.instance()
            fb = QgsFeedback()
            headers = auth.get_auth_headers(fb)

            self._zip_path = os.path.join(
                self.download_dir, f"{self.product.name}.zip"
            )

            def on_progress(downloaded, total):
                if total > 0:
                    # Map download progress to 2-80%
                    pct = 2 + (downloaded / total) * 78
                    self.setProgress(pct)

            def is_canceled():
                return self.isCanceled()

            # Chunked download with real progress
            download_to_file(
                self.product.download_url,
                self._zip_path,
                headers=headers,
                progress_callback=on_progress,
                cancel_check=is_canceled,
                chunk_size=DOWNLOAD_CHUNK_SIZE,
            )

            if self.isCanceled():
                return False

            # Extract
            self.setProgress(85)
            self._safe_dir = extract_safe(self._zip_path, self.download_dir)
            self.setProgress(95)
            return self._safe_dir is not None
        except Exception as exc:
            self._error = str(exc)
            return False

    def finished(self, ok):
        """Runs on main thread: load layers into QGIS."""
        if self.isCanceled():
            return
        if not ok or not self._safe_dir:
            self._on_err(self._error or "Errore durante il download.")
            return
        try:
            layers = load_bands_into_qgis(
                self._safe_dir, self.disaster_type, self.display_name,
            )
            self._on_ok(self._zip_path, layers)
        except Exception as exc:
            self._on_err(f"Download OK, errore caricamento: {exc}")
