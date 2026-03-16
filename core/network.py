"""Network utilities for the Copernicus Disaster Explorer plugin.

Provides HTTP helpers built on QgsBlockingNetworkRequest (for API calls
and geocoding) and Python stdlib urllib (for large binary downloads in
background threads where QgsNetworkAccessManager is not thread-safe).

All API calls honour QGIS proxy settings.
"""
import json
import logging
import os
import ssl
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import URLError, HTTPError

from qgis.PyQt.QtCore import QUrl, QByteArray, QSettings
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import QgsNetworkAccessManager, QgsBlockingNetworkRequest

from .config import API_TIMEOUT_MS, DOWNLOAD_TIMEOUT_MS, EFFIS_TIMEOUT_MS

logger = logging.getLogger("CDE.network")


class NetworkError(Exception):
    """Raised when a network request fails (timeout, HTTP error, etc.)."""
    def __init__(self, message, status_code=None):
        self.status_code = status_code
        super().__init__(message)

class AuthError(NetworkError):
    """Raised when authentication fails (401/403 or invalid credentials)."""
    pass


# ----------------------------------------------------------------
# QGIS proxy helper
# ----------------------------------------------------------------

def _get_qgis_proxy_handler():
    """Read proxy settings from QGIS and return a urllib ProxyHandler.

    Returns None if no proxy is configured, allowing urllib to use
    system defaults or direct connection.
    """
    s = QSettings()
    enabled = s.value("proxy/proxyEnabled", False)
    if not enabled or enabled == "false":
        return None
    ptype = s.value("proxy/proxyType", "")
    host = s.value("proxy/proxyHost", "")
    port = s.value("proxy/proxyPort", "")
    user = s.value("proxy/proxyUser", "")
    pw = s.value("proxy/proxyPassword", "")
    if not host:
        return None
    auth = f"{user}:{pw}@" if user else ""
    scheme = "socks5" if "Socks" in str(ptype) else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"
    return ProxyHandler({"http": proxy_url, "https": proxy_url})


def _build_urllib_opener():
    """Build a urllib opener that respects QGIS proxy settings."""
    handler = _get_qgis_proxy_handler()
    if handler:
        return build_opener(handler)
    return None  # use default


# ----------------------------------------------------------------
# QGIS Network helpers (for JSON API calls)
# ----------------------------------------------------------------

def _make_request(url, headers=None, timeout_ms=API_TIMEOUT_MS):
    """Build a QNetworkRequest with headers and timeout."""
    req = QNetworkRequest(QUrl(url))
    req.setTransferTimeout(timeout_ms)
    try:
        req.setAttribute(
            QNetworkRequest.Attribute.AuthenticationReuseAttribute,
            QNetworkRequest.Attribute(0),
        )
    except Exception:
        pass
    if headers:
        for key, val in headers.items():
            req.setRawHeader(QByteArray(key.encode()), QByteArray(val.encode()))
    return req


def get_json(url, headers=None, feedback=None, timeout_ms=None):
    """GET request returning parsed JSON. Safe from background threads."""
    req = _make_request(url, headers, timeout_ms=timeout_ms or API_TIMEOUT_MS)
    blocker = QgsBlockingNetworkRequest()
    err = blocker.get(req, forceRefresh=True, feedback=feedback)
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise NetworkError(f"Errore di rete: {blocker.errorMessage()}")
    reply = blocker.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if status in (401, 403):
        raise AuthError("Errore di autenticazione.")
    if status and status >= 400:
        raise NetworkError(f"HTTP {status}", status_code=status)
    try:
        return json.loads(bytes(reply.content()).decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NetworkError(f"Errore parsing JSON: {exc}")


def get_text(url, headers=None, feedback=None, timeout_ms=None):
    """GET request returning raw text. Safe from background threads."""
    req = _make_request(url, headers, timeout_ms=timeout_ms or API_TIMEOUT_MS)
    blocker = QgsBlockingNetworkRequest()
    err = blocker.get(req, forceRefresh=True, feedback=feedback)
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise NetworkError(f"Errore di rete: {blocker.errorMessage()}")
    reply = blocker.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if status and status >= 400:
        raise NetworkError(f"HTTP {status}", status_code=status)
    return bytes(reply.content()).decode()


def post_form(url, form_data, feedback=None):
    """POST form-encoded data, return parsed JSON.

    Note on urllib usage: Copernicus Data Space uses Keycloak OAuth2
    for authentication. QgsNetworkAccessManager intercepts auth
    challenges on these endpoints, causing failures. Python stdlib
    urllib is used as primary method for this specific case only.
    Falls back to QgsBlockingNetworkRequest for proxy environments.

    All other API calls (get_json, get_text, geocode_nominatim)
    use QgsBlockingNetworkRequest as recommended by QGIS guidelines.
    """
    body_str = urlencode(form_data)
    body_bytes = body_str.encode("utf-8")

    # --- Primary: Python stdlib ---
    try:
        py_req = Request(url, data=body_bytes, method="POST")
        py_req.add_header("Content-Type", "application/x-www-form-urlencoded")
        py_req.add_header("Accept", "application/json")
        ctx = ssl.create_default_context()
        timeout_s = max(API_TIMEOUT_MS / 1000, 30)
        opener = _build_urllib_opener()
        if opener:
            resp = opener.open(py_req, timeout=timeout_s)
        else:
            resp = urlopen(py_req, timeout=timeout_s, context=ctx)
        with resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except HTTPError as he:
        if he.code in (401, 403):
            try:
                err_body = json.loads(he.read().decode())
                err_msg = err_body.get("error_description",
                          err_body.get("error", "Credenziali non valide."))
            except Exception:
                err_msg = "Credenziali non valide."
            raise AuthError(err_msg)
        raise NetworkError(f"HTTP {he.code}: {he.reason}", status_code=he.code)
    except URLError as ue:
        logger.warning("urllib failed for %s: %s - trying QGIS network...", url, ue)
    except Exception as exc:
        logger.warning("urllib failed for %s: %s - trying QGIS network...", url, exc)

    # --- Fallback: QGIS network (for proxy environments) ---
    body = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in form_data.items())
    req = _make_request(url, {"Content-Type": "application/x-www-form-urlencoded"})
    blocker = QgsBlockingNetworkRequest()
    err = blocker.post(req, QByteArray(body.encode()), feedback=feedback)
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise NetworkError(f"Errore di rete: {blocker.errorMessage()}")
    reply = blocker.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if status in (401, 403):
        raise AuthError("Credenziali non valide.")
    if status and status >= 400:
        raise NetworkError(f"HTTP {status}", status_code=status)
    try:
        return json.loads(bytes(reply.content()).decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NetworkError(f"Errore parsing: {exc}")




# ----------------------------------------------------------------
# Geocoding (uses QgsBlockingNetworkRequest, proxy-safe)
# ----------------------------------------------------------------

def geocode_nominatim(query):
    """Geocode a place name using Nominatim via QgsBlockingNetworkRequest.

    Appends ', Italia' and restricts to Italian results.
    Returns dict with 'lat', 'lon', 'name' keys, or None if not found.

    Uses QgsBlockingNetworkRequest to respect QGIS proxy settings
    as recommended by the QGIS plugin guidelines.
    """
    from urllib.parse import quote as _quote
    encoded = _quote(query + ", Italia")
    url = (f"https://nominatim.openstreetmap.org/search?"
           f"q={encoded}&format=json&limit=1&countrycodes=it")
    headers = {"User-Agent": "QGIS-CDE-Plugin/2.0",
               "Accept": "application/json"}
    try:
        data = get_json(url, headers=headers, timeout_ms=10000)
        if data and len(data) > 0:
            return {
                "lat": float(data[0]["lat"]),
                "lon": float(data[0]["lon"]),
                "name": data[0].get("display_name", query).split(",")[0],
            }
    except NetworkError as exc:
        logger.warning("Nominatim geocode failed for '%s': %s", query, exc)
    return None

# ----------------------------------------------------------------
# Download functions
# ----------------------------------------------------------------

def download_to_file(url, dest_path, headers=None, progress_callback=None,
                     cancel_check=None, chunk_size=1024*1024):
    """Download a file with chunked transfer and progress reporting.

    Uses Python stdlib urllib because this function runs inside a
    QgsTask background thread where QgsNetworkAccessManager is not
    safe to use (Qt networking requires the main event loop).
    Respects QGIS proxy settings via _build_urllib_opener().
    """
    py_req = Request(url, method="GET")
    py_req.add_header("User-Agent", "QGIS-CDE/2.0")
    if headers:
        for k, v in headers.items():
            py_req.add_header(k, v)

    # Resume support: if file already exists, try Range request
    existing_size = 0
    if os.path.exists(dest_path):
        existing_size = os.path.getsize(dest_path)
        if existing_size > 0:
            py_req.add_header("Range", f"bytes={existing_size}-")
            logger.info("Attempting resume from byte %d", existing_size)

    ctx = ssl.create_default_context()
    timeout_s = max(DOWNLOAD_TIMEOUT_MS / 1000, 300)

    try:
        opener = _build_urllib_opener()
        if opener:
            resp = opener.open(py_req, timeout=timeout_s)
        else:
            resp = urlopen(py_req, timeout=timeout_s, context=ctx)
    except HTTPError as he:
        if he.code in (401, 403):
            raise AuthError("Errore di autenticazione.")
        raise NetworkError(f"HTTP {he.code}: {he.reason}", status_code=he.code)
    except URLError as ue:
        raise NetworkError(f"Errore di rete: {ue.reason}")

    status = resp.getcode()
    content_length = int(resp.headers.get("Content-Length", 0))

    if status == 206 and existing_size > 0:
        # Server accepted Range request - append to existing file
        total = existing_size + content_length
        downloaded = existing_size
        file_mode = "ab"
        logger.info("Resuming download: %d/%d bytes", existing_size, total)
    else:
        # Full download (no resume or server sent 200)
        total = content_length
        downloaded = 0
        file_mode = "wb"

    try:
        with open(dest_path, file_mode) as fh:
            while True:
                if cancel_check and cancel_check():
                    logger.info("Download cancelled by user")
                    resp.close()
                    raise NetworkError("Download annullato dall'utente.")
                try:
                    chunk = resp.read(chunk_size)
                except Exception as read_err:
                    logger.warning("Read error at %d/%d: %s", downloaded, total, read_err)
                    break
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total)
    except NetworkError:
        raise
    except Exception as exc:
        raise NetworkError(f"Errore scrittura file: {exc}")
    finally:
        resp.close()

    if total and downloaded < total * 0.95:
        raise NetworkError(
            f"Download incompleto: {downloaded}/{total} bytes"
        )
    return dest_path
