"""Main plugin entry point. Registers the dock widget and toolbar button."""
import os
import logging

from qgis.PyQt.QtGui import QAction, QIcon
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMessageBox
from .core.config import ICONS_DIR
from .core.auth_manager import AuthManager

logger = logging.getLogger("CDE")


class CopernicusDisasterExplorer:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.menu_name = "Copernicus Disaster Explorer"
        self.toolbar = None
        self.dock = None

    def initGui(self):
        self.toolbar = self.iface.addToolBar(self.menu_name)
        self.toolbar.setObjectName("CDEToolbar")
        icon_path = os.path.join(ICONS_DIR, "plugin_icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.main_action = QAction(icon, "Copernicus Disaster Explorer", self.iface.mainWindow())
        self.main_action.setCheckable(True)
        self.main_action.triggered.connect(self._toggle)
        self.toolbar.addAction(self.main_action)
        self.iface.addPluginToRasterMenu(self.menu_name, self.main_action)
        self.actions.append(self.main_action)

        settings_icon_path = os.path.join(ICONS_DIR, "settings.png")
        si = QIcon(settings_icon_path) if os.path.exists(settings_icon_path) else QIcon()
        self.settings_action = QAction(si, "Impostazioni", self.iface.mainWindow())
        self.settings_action.triggered.connect(self._settings)
        self.iface.addPluginToRasterMenu(self.menu_name, self.settings_action)
        self.actions.append(self.settings_action)

    def unload(self):
        for a in self.actions:
            self.iface.removePluginRasterMenu(self.menu_name, a)
            self.iface.removeToolBarIcon(a)
        if self.toolbar:
            del self.toolbar
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        self.actions.clear()

    def _toggle(self, checked):
        if not self.dock:
            from .gui.dock_widget import MainDockWidget
            self.dock = MainDockWidget(self.iface, self.iface.mainWindow())
            self.dock.visibilityChanged.connect(self.main_action.setChecked)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        if checked:
            self.dock.show()
            if not AuthManager.has_credentials():
                msg = QMessageBox(self.iface.mainWindow())
                msg.setWindowTitle("Benvenuto")
                msg.setIcon(QMessageBox.Icon.Information)
                msg.setText(
                    "Benvenuto in Copernicus Disaster Explorer!\n\n"
                    "Per scaricare immagini Sentinel, configura le\n"
                    "credenziali Copernicus Data Space (gratuite).\n\n"
                    "Vuoi configurarle ora?"
                )
                msg.setStandardButtons(
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if msg.exec() == QMessageBox.StandardButton.Yes:
                    self._settings()
        else:
            self.dock.hide()

    def _settings(self):
        from .gui.settings_dialog import SettingsDialog
        SettingsDialog(self.iface.mainWindow()).exec()
