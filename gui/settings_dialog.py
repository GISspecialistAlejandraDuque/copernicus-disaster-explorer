"""Settings dialog for Copernicus Data Space credentials.

Allows users to enter and store their CDSE username and password
using QGIS QgsAuthManager for secure credential persistence.
"""
import logging

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QPushButton, QLabel, QHBoxLayout, QMessageBox,
)
from qgis.PyQt.QtCore import Qt

from ..core.auth_manager import AuthManager
from ..core.config import CDSE_REGISTER_URL

logger = logging.getLogger("CDE.settings")


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Copernicus Disaster Explorer - Impostazioni")
        self.setMinimumWidth(400)
        self._build_ui()
        self._load()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "<b>Credenziali Copernicus Data Space</b><br>"
            "Servono per scaricare immagini Sentinel.<br>"
            f"Registrati gratis su <a href='{CDSE_REGISTER_URL}'>"
            f"{CDSE_REGISTER_URL}</a>"
        ))

        form = QFormLayout()
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("Email di registrazione")
        form.addRow("Username:", self.user_edit)
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setPlaceholderText("Password")
        form.addRow("Password:", self.pass_edit)
        layout.addLayout(form)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Salva")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        test_btn = QPushButton("Testa connessione")
        test_btn.clicked.connect(self._test)
        btn_row.addWidget(test_btn)

        clear_btn = QPushButton("Cancella")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(clear_btn)

        layout.addLayout(btn_row)

    def _load(self):
        u, p = AuthManager.get_credentials()
        if u:
            self.user_edit.setText(u)
        if p:
            self.pass_edit.setText(p)

    def _save(self):
        u = self.user_edit.text().strip()
        p = self.pass_edit.text().strip()
        if not u or not p:
            self.status_label.setText("Inserisci username e password.")
            return
        AuthManager.save_credentials(u, p)
        AuthManager.instance().invalidate()
        self.status_label.setText("Credenziali salvate.")
        self.status_label.setStyleSheet("color: green;")

    def _test(self):
        self._save()
        try:
            auth = AuthManager.instance()
            auth.invalidate()
            auth.get_auth_headers()
            self.status_label.setText("Connessione OK!")
            self.status_label.setStyleSheet("color: green;")
        except Exception as exc:
            self.status_label.setText(f"Errore: {exc}")
            self.status_label.setStyleSheet("color: red;")

    def _clear(self):
        AuthManager.clear_credentials()
        AuthManager.instance().invalidate()
        self.user_edit.clear()
        self.pass_edit.clear()
        self.status_label.setText("Credenziali cancellate.")
        self.status_label.setStyleSheet("color: orange;")
