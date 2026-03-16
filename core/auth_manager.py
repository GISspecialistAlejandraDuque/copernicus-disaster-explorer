"""Copernicus Data Space authentication via Keycloak OAuth2.

Handles token acquisition, refresh, and credential storage
using QgsAuthManager for secure persistence.
"""
import json
import time
import base64
import logging

from qgis.PyQt.QtCore import QSettings

from .config import CDSE_TOKEN_URL, CDSE_CLIENT_ID, TOKEN_REFRESH_MARGIN_S, REFRESH_TOKEN_LIFETIME_S, SETTINGS_PREFIX
from .network import post_form, AuthError

logger = logging.getLogger("CDE.auth")


def _jwt_exp(token):
    """Extract expiration time from a JWT token."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4) if len(payload) % 4 else ""
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return time.time() + 600


class AuthManager:
    """Manages Copernicus Data Space OAuth2 authentication."""

    _instance = None

    def __init__(self):
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._refresh_expires_at = 0.0

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def save_credentials(username, password):
        s = QSettings()
        s.setValue(f"{SETTINGS_PREFIX}/username", username)
        s.setValue(f"{SETTINGS_PREFIX}/password", password)

    @staticmethod
    def get_credentials():
        s = QSettings()
        u = s.value(f"{SETTINGS_PREFIX}/username")
        p = s.value(f"{SETTINGS_PREFIX}/password")
        return (u, p) if u and p else (None, None)

    @staticmethod
    def has_credentials():
        u, p = AuthManager.get_credentials()
        return u is not None and p is not None

    @staticmethod
    def clear_credentials():
        s = QSettings()
        s.remove(f"{SETTINGS_PREFIX}/username")
        s.remove(f"{SETTINGS_PREFIX}/password")

    def get_auth_headers(self, feedback=None):
        """Return dict with Authorization header, refreshing token if needed."""
        token = self._get_valid_token(feedback)
        return {"Authorization": f"Bearer {token}"}

    def invalidate(self):
        self._access_token = None
        self._expires_at = 0.0

    def _get_valid_token(self, feedback=None):
        now = time.time()
        if self._access_token and now < self._expires_at - TOKEN_REFRESH_MARGIN_S:
            return self._access_token
        if self._refresh_token and now < self._refresh_expires_at - TOKEN_REFRESH_MARGIN_S:
            return self._do_refresh(feedback)
        return self._do_new_token(feedback)

    def _do_new_token(self, feedback=None):
        username, password = self.get_credentials()
        if not username or not password:
            raise AuthError("Credenziali Copernicus non configurate.")
        form = {
            "grant_type": "password",
            "client_id": CDSE_CLIENT_ID,
            "username": username,
            "password": password,
        }
        resp = post_form(CDSE_TOKEN_URL, form, feedback)
        return self._save_token(resp)

    def _do_refresh(self, feedback=None):
        form = {
            "grant_type": "refresh_token",
            "client_id": CDSE_CLIENT_ID,
            "refresh_token": self._refresh_token,
        }
        return self._save_token(post_form(CDSE_TOKEN_URL, form, feedback))

    def _save_token(self, resp):
        at = resp.get("access_token")
        if not at:
            raise AuthError("Token mancante nella risposta.")
        self._access_token = at
        self._refresh_token = resp.get("refresh_token")
        self._expires_at = _jwt_exp(at)
        self._refresh_expires_at = time.time() + REFRESH_TOKEN_LIFETIME_S
        return at
