"""Main dock widget for the Copernicus Disaster Explorer.

Provides the user interface for searching disaster events,
browsing Sentinel imagery, downloading products, and loading
them into QGIS with automatic false-color symbology.
"""
import os
import logging

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QPushButton, QGroupBox, QButtonGroup, QSlider,
    QDateEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QStackedWidget, QProgressBar, QSizePolicy, QScrollArea,
    QLineEdit, QCompleter,
)
from qgis.PyQt.QtCore import Qt, QDate, QTimer, QStringListModel
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer,
    QgsFeature, QgsGeometry, QgsPointXY, QgsLayerTreeLayer,
    QgsRectangle,
)
from qgis.gui import QgsFileWidget

from ..core.config import (
    ICONS_DIR, DISASTER_CONFIG, DisasterType, ITALIAN_REGIONS,
    ITALIAN_PLACES, BASEMAP_SOURCES,
)
from ..core.tasks import EventSearchTask, ImageSearchTask, DownloadAndLoadTask

logger = logging.getLogger("CDE.gui")

_BASEMAP_NAME = "CDE Basemap"


class PlacesCompleter:
    """Local autocomplete for Italian places. Instant, no network needed.
    Shows suggestions as user types, like a search engine.
    Falls back to Nominatim when user presses Enter for unknown places.
    """

    def __init__(self, line_edit, on_selected=None):
        self.line_edit = line_edit
        self.on_selected = on_selected

        # Build lookup: "Name (Region)" -> (lat, lon)
        self._places = {}
        display_names = []
        for name, region, lat, lon in ITALIAN_PLACES:
            display = f"{name} ({region})"
            self._places[display] = (lat, lon, name)
            display_names.append(display)

        self._model = QStringListModel(sorted(display_names))
        self._completer = QCompleter()
        self._completer.setModel(self._model)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setMaxVisibleItems(10)
        self.line_edit.setCompleter(self._completer)

        # Style popup
        popup = self._completer.popup()
        popup.setStyleSheet(
            "QListView{font-size:12px; padding:2px; border:1px solid #bdc3c7;}"
            "QListView::item{padding:5px 10px;}"
            "QListView::item:hover{background:#eaf2f8;}"
            "QListView::item:selected{background:#2980b9; color:white;}"
        )

        self._completer.activated.connect(self._on_item_selected)

    def _on_item_selected(self, text):
        coords = self._places.get(text)
        if coords and self.on_selected:
            lat, lon, name = coords
            self.on_selected(lat, lon, text)


class MainDockWidget(QDockWidget):
    """Main dock widget for exploring disaster events and Sentinel imagery.

    Provides a tabbed interface with event search (by type or free text),
    Sentinel-2 image browsing with cloud filtering, download with progress,
    and automatic false-color symbology loading into QGIS.
    """
    def __init__(self, iface, parent=None):
        super().__init__("Copernicus Disaster Explorer", parent)
        self.iface = iface
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self._dtype = DisasterType.FLOOD
        self._events = []
        self._selected_event = None
        self._pre_products = []
        self._post_products = []
        self._current_task = None
        self._build_ui()

    def _build_ui(self):
        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(6, 6, 6, 6)

        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack)

        self._build_step1()
        self._build_step2()
        self._build_step3()

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        main_layout.addWidget(self.progress)

        self.status_label = QLabel("Pronto")
        self.status_label.setStyleSheet("font-size:11px; color:#7f8c8d;")
        main_layout.addWidget(self.status_label)

        self.setWidget(container)

    # ==============================================================
    # STEP 1: Search events
    # ==============================================================
    def _build_step1(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        # --- Download-in-progress banner (hidden by default) ---
        self.dl_banner = QPushButton("")
        self.dl_banner.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-weight:bold;"
            "padding:8px;border-radius:4px;text-align:left;}"
            "QPushButton:hover{background:#2ecc71;}")
        self.dl_banner.setVisible(False)
        self.dl_banner.clicked.connect(lambda: self.stack.setCurrentIndex(2))
        layout.addWidget(self.dl_banner)

        # Disaster type buttons
        dtype_group = QGroupBox("Tipo di disastro")
        dtype_layout = QHBoxLayout(dtype_group)
        self._dtype_buttons = QButtonGroup(self)
        self._dtype_buttons.setExclusive(True)
        for dtype, cfg in DISASTER_CONFIG.items():
            btn = QPushButton(cfg["label_it"])
            btn.setCheckable(True)
            btn.setProperty("dtype", dtype)
            icon_path = os.path.join(ICONS_DIR, cfg.get("icon", ""))
            if os.path.exists(icon_path):
                btn.setIcon(QIcon(icon_path))
            self._dtype_buttons.addButton(btn)
            dtype_layout.addWidget(btn)
            if dtype == DisasterType.FLOOD:
                btn.setChecked(True)
        self._dtype_buttons.buttonClicked.connect(self._on_dtype_changed)
        layout.addWidget(dtype_group)

        # --- "Ricerca libera" toggle ---
        self.free_search_btn = QPushButton("🌐 Ricerca libera (senza eventi)")
        self.free_search_btn.setCheckable(True)
        self.free_search_btn.setStyleSheet(
            "QPushButton{color:#8e44ad;border:1px solid #8e44ad;"
            "border-radius:4px;padding:5px;}"
            "QPushButton:checked{background:#8e44ad;color:white;}"
            "QPushButton:hover{background:#f4ecf7;}")
        self.free_search_btn.clicked.connect(self._on_free_search_toggle)
        layout.addWidget(self.free_search_btn)

        # --- Free search panel (hidden by default) ---
        self.free_group = QGroupBox("Ricerca libera per posizione e data")
        self.free_group.setVisible(False)
        free_layout = QVBoxLayout(self.free_group)
        free_layout.setSpacing(6)

        free_layout.addWidget(QLabel(
            "Indica la posizione e la data dell'evento per cercare "
            "le immagini Sentinel direttamente."
        ))

        # Place search
        fp_row = QHBoxLayout()
        self.free_place = QLineEdit()
        self.free_place.setPlaceholderText("Cerca località (es. Niscemi, Ischia...)")
        self.free_place.returnPressed.connect(self._on_free_geocode)
        fp_row.addWidget(self.free_place)
        free_geocode_btn = QPushButton("🔍")
        free_geocode_btn.setMaximumWidth(36)
        free_geocode_btn.clicked.connect(self._on_free_geocode)
        fp_row.addWidget(free_geocode_btn)
        free_layout.addLayout(fp_row)

        # Autocomplete for free search
        self._free_completer = PlacesCompleter(
            self.free_place,
            on_selected=self._on_free_autocomplete_selected,
        )

        # Map click
        self.free_pick_btn = QPushButton("📍 Clicca sulla mappa per scegliere il punto")
        self.free_pick_btn.setStyleSheet(
            "QPushButton{color:#2980b9;border:1px dashed #2980b9;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#eaf2f8;}")
        self.free_pick_btn.clicked.connect(self._on_free_pick_map)
        free_layout.addWidget(self.free_pick_btn)

        # Location display
        self.free_loc_label = QLabel("Nessuna posizione selezionata")
        self.free_loc_label.setStyleSheet("font-size:11px; color:#7f8c8d;")
        free_layout.addWidget(self.free_loc_label)

        # Hidden lat/lon
        self.free_lat = QLineEdit()
        self.free_lat.setVisible(False)
        self.free_lon = QLineEdit()
        self.free_lon.setVisible(False)
        free_layout.addWidget(self.free_lat)
        free_layout.addWidget(self.free_lon)

        # Event date (OPTIONAL - for PRE/POST split)
        ev_row = QHBoxLayout()
        self.free_event_check = QPushButton("☐ Separa PRE / POST")
        self.free_event_check.setCheckable(True)
        self.free_event_check.setChecked(False)
        self.free_event_check.setStyleSheet(
            "QPushButton{font-size:11px; color:#e67e22; border:1px solid #e67e22;"
            "border-radius:3px; padding:3px 6px;}"
            "QPushButton:checked{background:#e67e22; color:white;}"
            "QPushButton:hover{background:#fef0e0;}"
        )
        self.free_event_check.clicked.connect(self._on_event_date_toggle)
        ev_row.addWidget(self.free_event_check)
        info_ev = QPushButton("ℹ")
        info_ev.setMaximumWidth(24)
        info_ev.setStyleSheet("QPushButton{border:none; color:#2980b9; font-size:14px;}"
                              "QPushButton:hover{color:#e67e22;}")
        info_ev.clicked.connect(lambda: self._show_info(
            "Separare PRE / POST",
            "Attiva questa opzione se conosci la data esatta dell'evento "
            "(frana, alluvione, ecc.).<br>"
            "Quando attiva, le immagini verranno divise in due tabelle:<br>"
            "- PRE-evento: immagini PRIMA della data<br>"
            "- POST-evento: immagini DOPO la data<br>"
            "Questo ti permette di CONFRONTARE il prima e il dopo per "
            "identificare i danni.<br>"
            "Se non conosci la data esatta, lascia disattivato: "
            "tutte le immagini appariranno in un'unica lista."
        ))
        ev_row.addWidget(info_ev)
        ev_row.addStretch()
        free_layout.addLayout(ev_row)

        # Event date field (hidden by default)
        self.free_event_date_row = QWidget()
        edr_ly = QHBoxLayout(self.free_event_date_row)
        edr_ly.setContentsMargins(0, 0, 0, 0)
        edr_ly.addWidget(QLabel("Data evento:"))
        self.free_event_date = QDateEdit()
        self.free_event_date.setCalendarPopup(True)
        self.free_event_date.setDisplayFormat("dd/MM/yyyy")
        self.free_event_date.setDate(QDate.currentDate().addDays(-7))
        self.free_event_date.setStyleSheet(
            "QDateEdit{font-weight:bold; border:2px solid #e67e22; border-radius:3px; padding:2px;}")
        edr_ly.addWidget(self.free_event_date)
        self.free_event_date_row.setVisible(False)
        free_layout.addWidget(self.free_event_date_row)

        # Search date range
        fd_row = QHBoxLayout()
        fd_row.addWidget(QLabel("Cerca da:"))
        self.free_date_from = QDateEdit()
        self.free_date_from.setCalendarPopup(True)
        self.free_date_from.setDisplayFormat("dd/MM/yyyy")
        self.free_date_from.setDate(QDate.currentDate().addDays(-30))
        fd_row.addWidget(self.free_date_from)
        fd_row.addWidget(QLabel("a:"))
        self.free_date_to = QDateEdit()
        self.free_date_to.setCalendarPopup(True)
        self.free_date_to.setDisplayFormat("dd/MM/yyyy")
        self.free_date_to.setDate(QDate.currentDate())
        fd_row.addWidget(self.free_date_to)
        info_dates = QPushButton("ℹ")
        info_dates.setMaximumWidth(24)
        info_dates.setStyleSheet("QPushButton{border:none; color:#2980b9; font-size:14px;}"
                                 "QPushButton:hover{color:#e67e22;}")
        info_dates.clicked.connect(lambda: self._show_info(
            "Intervallo di ricerca",
            "Definisci il periodo in cui cercare le immagini satellite.<br>"
            "Consiglio: usa un intervallo di 30-60 giorni centrato "
            "sull'evento per avere abbastanza immagini PRE e POST.<br>"
            "Esempio per la frana di Niscemi (25 gen 2026):<br>"
            "- Cerca da: 10/01/2026<br>"
            "- A: 28/02/2026<br>"
            "- Data evento: 25/01/2026"
        ))
        fd_row.addWidget(info_dates)
        free_layout.addLayout(fd_row)

        # Band type selector
        ft_row = QHBoxLayout()
        ft_row.addWidget(QLabel("Bande e simbologia:"))
        self.free_type_combo = QComboBox()
        for dtype, cfg in DISASTER_CONFIG.items():
            self.free_type_combo.addItem(cfg["label_it"], dtype)
        ft_row.addWidget(self.free_type_combo)
        info_bands = QPushButton("ℹ")
        info_bands.setMaximumWidth(24)
        info_bands.setStyleSheet("QPushButton{border:none; color:#2980b9; font-size:14px;}"
                                 "QPushButton:hover{color:#e67e22;}")
        info_bands.clicked.connect(lambda: self._show_info(
            "Bande e simbologia",
            "Seleziona il tipo di evento per applicare automaticamente "
            "la combinazione di bande ottimale.<br>"
            "Ogni tipo usa bande Sentinel-2 diverse:<br>"
            "- Alluvione: B12/B08/B03 (acqua = nero)<br>"
            "- Incendio: B12/B08/B04 (cicatrice = rosso)<br>"
            "- Vulcano: B12/B11/B04 (lava = giallo)<br>"
            "- Frana: B12/B08/B04 (suolo esposto = rosa)<br>"
            "Viene caricato anche un layer in colore naturale "
            "(come una foto) per confronto."
        ))
        ft_row.addWidget(info_bands)
        free_layout.addLayout(ft_row)

        # Search button for free mode
        self.free_search_go = QPushButton("Cerca immagini →")
        self.free_search_go.setMinimumHeight(36)
        self.free_search_go.setStyleSheet("font-weight:bold; background:#8e44ad; color:white;")
        self.free_search_go.setEnabled(True)
        self.free_search_go.clicked.connect(self._on_free_search_go)
        free_layout.addWidget(self.free_search_go)

        layout.addWidget(self.free_group)

        # --- Event search widgets (hidden in free mode) ---
        self.event_search_widgets = QWidget()
        es_layout = QVBoxLayout(self.event_search_widgets)
        es_layout.setContentsMargins(0, 0, 0, 0)
        es_layout.setSpacing(8)

        # Region
        region_row = QHBoxLayout()
        region_row.addWidget(QLabel("Regione:"))
        self.region_combo = QComboBox()
        self.region_combo.addItems(list(ITALIAN_REGIONS.keys()))
        region_row.addWidget(self.region_combo)
        es_layout.addLayout(region_row)

        # Date range
        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("Da:"))
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("dd/MM/yyyy")
        self.date_from.setDate(QDate.currentDate().addYears(-1))
        date_row.addWidget(self.date_from)
        date_row.addWidget(QLabel("A:"))
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("dd/MM/yyyy")
        self.date_to.setDate(QDate.currentDate())
        date_row.addWidget(self.date_to)
        es_layout.addLayout(date_row)

        # Quick date buttons
        qd_row = QHBoxLayout()
        for label, months in [("1 mese", 1), ("3 mesi", 3), ("1 anno", 12), ("2 anni", 24)]:
            btn = QPushButton(label)
            btn.setProperty("months", months)
            btn.clicked.connect(self._on_quick_date)
            qd_row.addWidget(btn)
        es_layout.addLayout(qd_row)

        # Search button
        self.search_btn = QPushButton("CERCA EVENTI")
        self.search_btn.setMinimumHeight(40)
        self.search_btn.setStyleSheet(
            "QPushButton{font-weight:bold; font-size:14px; "
            "background-color:#27ae60; color:white; border-radius:4px;}"
            "QPushButton:hover{background-color:#2ecc71;}"
            "QPushButton:disabled{background-color:#bdc3c7; color:#7f8c8d;}"
        )
        self.search_btn.clicked.connect(self._on_search_events)
        es_layout.addWidget(self.search_btn)

        # Events table
        self.events_label = QLabel("")
        es_layout.addWidget(self.events_label)
        self.events_table = QTableWidget()
        self.events_table.setColumnCount(4)
        self.events_table.setHorizontalHeaderLabels(["Data", "Evento", "Dettagli", "Fonte"])
        self.events_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.events_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.events_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.events_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.events_table.selectionModel().selectionChanged.connect(self._on_event_selected)
        es_layout.addWidget(self.events_table)

        # Action buttons row 1: show on map
        self.show_map_btn = QPushButton("Mostra sulla mappa")
        self.show_map_btn.setEnabled(False)
        self.show_map_btn.clicked.connect(self._on_show_event_on_map)
        es_layout.addWidget(self.show_map_btn)

        # -- Location correction panel (shown after event selection) --
        self.loc_group = QGroupBox("Correggi posizione")
        self.loc_group.setVisible(False)
        loc_layout = QVBoxLayout(self.loc_group)
        loc_layout.setSpacing(4)

        self.loc_label = QLabel("")
        self.loc_label.setStyleSheet("font-size:11px;")
        loc_layout.addWidget(self.loc_label)

        self.coord_hint = QLabel("")
        self.coord_hint.setStyleSheet("color:#e67e22; font-size:10px;")
        self.coord_hint.setWordWrap(True)
        loc_layout.addWidget(self.coord_hint)

        place_row = QHBoxLayout()
        self.place_edit = QLineEdit()
        self.place_edit.setPlaceholderText("Cerca località (es. Niscemi, Ischia...)")
        self.place_edit.returnPressed.connect(self._on_geocode)
        place_row.addWidget(self.place_edit)
        self.geocode_btn = QPushButton("🔍")
        self.geocode_btn.setMaximumWidth(36)
        self.geocode_btn.setToolTip("Cerca località e aggiorna mappa")
        self.geocode_btn.clicked.connect(self._on_geocode)
        place_row.addWidget(self.geocode_btn)
        loc_layout.addLayout(place_row)

        # Autocomplete for location correction
        self._loc_completer = PlacesCompleter(
            self.place_edit,
            on_selected=self._on_loc_autocomplete_selected,
        )

        self.pick_map_btn = QPushButton("📍 Clicca sulla mappa per scegliere il punto")
        self.pick_map_btn.setStyleSheet(
            "QPushButton{color:#2980b9;border:1px dashed #2980b9;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#eaf2f8;}")
        self.pick_map_btn.clicked.connect(self._on_pick_from_map)
        loc_layout.addWidget(self.pick_map_btn)

        self.lat_edit = QLineEdit()
        self.lat_edit.setVisible(False)
        self.lon_edit = QLineEdit()
        self.lon_edit.setVisible(False)
        loc_layout.addWidget(self.lat_edit)
        loc_layout.addWidget(self.lon_edit)

        es_layout.addWidget(self.loc_group)

        # Action button row 2: search images
        self.next_btn = QPushButton("Cerca immagini →")
        self.next_btn.setEnabled(False)
        self.next_btn.setMinimumHeight(36)
        self.next_btn.setStyleSheet(
            "QPushButton{font-weight:bold; background-color:#2980b9; color:white; "
            "border-radius:4px;}"
            "QPushButton:hover{background-color:#3498db;}"
            "QPushButton:disabled{background-color:#bdc3c7; color:#7f8c8d;}"
        )
        self.next_btn.clicked.connect(self._on_go_to_step2)
        es_layout.addWidget(self.next_btn)

        layout.addWidget(self.event_search_widgets)

        # Bottom buttons row - always visible
        bottom_row = QHBoxLayout()

        self.guide_btn = QPushButton("📖 Guida")
        self.guide_btn.setStyleSheet(
            "QPushButton{font-size:11px; color:#2980b9; "
            "border:1px solid #2980b9; border-radius:4px; padding:4px 8px;}"
            "QPushButton:hover{background-color:#eaf2f8; color:#1a5276;}"
        )
        self.guide_btn.clicked.connect(self._show_interpretation_guide)
        bottom_row.addWidget(self.guide_btn)

        self.exercises_btn = QPushButton("🎓 Esercizi")
        self.exercises_btn.setStyleSheet(
            "QPushButton{font-size:11px; color:#16a085; "
            "border:1px solid #16a085; border-radius:4px; padding:4px 8px;}"
            "QPushButton:hover{background-color:#e8f8f5; color:#0e6655;}"
        )
        self.exercises_btn.clicked.connect(self._show_exercises)
        bottom_row.addWidget(self.exercises_btn)

        self.usage_btn = QPushButton("❓ Come usare")
        self.usage_btn.setStyleSheet(
            "QPushButton{font-size:11px; color:#8e44ad; "
            "border:1px solid #8e44ad; border-radius:4px; padding:4px 8px;}"
            "QPushButton:hover{background-color:#f4ecf7; color:#6c3483;}"
        )
        self.usage_btn.clicked.connect(self._show_usage_guide)
        bottom_row.addWidget(self.usage_btn)

        bottom_row.addStretch()

        self.reset_btn = QPushButton("🗑 Pulisci tutto")
        self.reset_btn.setStyleSheet(
            "QPushButton{font-size:11px; color:#7f8c8d; "
            "border:1px solid #bdc3c7; border-radius:4px; padding:4px 8px;}"
            "QPushButton:hover{background-color:#ecf0f1; color:#c0392b;}"
        )
        self.reset_btn.clicked.connect(self._on_reset)
        bottom_row.addWidget(self.reset_btn)

        layout.addLayout(bottom_row)

        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.stack.addWidget(scroll)

    # ==============================================================
    # STEP 2: Image selection
    # ==============================================================
    def _build_step2(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        self.event_info_label = QLabel("")
        self.event_info_label.setWordWrap(True)
        self.event_info_label.setStyleSheet("font-weight:bold;")
        layout.addWidget(self.event_info_label)

        # Cloud filter control
        cloud_row = QHBoxLayout()
        cloud_row.addWidget(QLabel("Filtro nuvole S2:"))
        self.cloud_slider = QComboBox()
        self.cloud_slider.addItems([
            "Mostra tutte", "Max 20% (cielo sereno)",
            "Max 50% (parziale)", "Max 80% (nuvoloso)"
        ])
        self.cloud_slider.setCurrentIndex(2)  # Default: max 50%
        self.cloud_slider.currentIndexChanged.connect(self._on_cloud_filter_changed)
        cloud_row.addWidget(self.cloud_slider)
        layout.addLayout(cloud_row)

        # Pre-event images
        self.pre_label = QLabel("Pre-evento:")
        layout.addWidget(self.pre_label)
        self.pre_table = QTableWidget()
        self.pre_table.setColumnCount(4)
        self.pre_table.setHorizontalHeaderLabels(["Data", "Sensore", "Nuvole", "Dimensione"])
        self.pre_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pre_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pre_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.pre_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pre_table.setMaximumHeight(120)
        self.pre_table.cellClicked.connect(
            lambda r, c: self._toggle_table_selection(self.pre_table, r))
        layout.addWidget(self.pre_table)

        # Post-event images
        self.post_label = QLabel("Post-evento:")
        layout.addWidget(self.post_label)
        self.post_table = QTableWidget()
        self.post_table.setColumnCount(4)
        self.post_table.setHorizontalHeaderLabels(["Data", "Sensore", "Nuvole", "Dimensione"])
        self.post_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.post_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.post_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.post_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.post_table.setMaximumHeight(120)
        self.post_table.cellClicked.connect(
            lambda r, c: self._toggle_table_selection(self.post_table, r))
        layout.addWidget(self.post_table)

        # Band info
        self.bands_info = QLabel("")
        self.bands_info.setStyleSheet("color:#2980b9; font-size:11px;")
        layout.addWidget(self.bands_info)

        # Download dir
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("Cartella:"))
        self.download_dir = QgsFileWidget()
        self.download_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        dir_row.addWidget(self.download_dir)
        layout.addLayout(dir_row)

        # Navigation
        nav_row = QHBoxLayout()
        back_btn = QPushButton("← Indietro")
        back_btn.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        nav_row.addWidget(back_btn)
        self.download_btn = QPushButton("Scarica e Visualizza")
        self.download_btn.setMinimumHeight(40)
        self.download_btn.setStyleSheet(
            "QPushButton{font-weight:bold; background-color:#27ae60; color:white; "
            "border-radius:4px;}"
            "QPushButton:hover{background-color:#2ecc71;}"
            "QPushButton:disabled{background-color:#bdc3c7; color:#7f8c8d;}"
        )
        self.download_btn.clicked.connect(self._on_download)
        nav_row.addWidget(self.download_btn)
        layout.addLayout(nav_row)

        layout.addStretch()
        self.stack.addWidget(page)

    # ==============================================================
    # STEP 3: Download progress
    # ==============================================================
    def _build_step3(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Download e caricamento in corso..."))
        self.dl_status_label = QLabel("")
        self.dl_status_label.setWordWrap(True)
        layout.addWidget(self.dl_status_label)

        # Cancel button (the ONLY way to stop the download)
        self.cancel_dl_btn = QPushButton("❌ Annulla download")
        self.cancel_dl_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-weight: bold; padding: 6px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #e74c3c; }"
            "QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }"
        )
        self.cancel_dl_btn.clicked.connect(self._on_cancel_download)
        layout.addWidget(self.cancel_dl_btn)

        # Back button - navigates back WITHOUT cancelling
        back_btn3 = QPushButton("← Continua a esplorare (download in background)")
        back_btn3.setToolTip(
            "Torna alla ricerca. Il download continuerà in background."
        )
        back_btn3.clicked.connect(lambda: self._navigate_away_from_download())
        layout.addWidget(back_btn3)

        layout.addStretch()
        self.stack.addWidget(page)

    # ==============================================================
    # Status helper
    # ==============================================================
    def _set_status(self, text, ok=True):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"font-size:11px; color:{'#27ae60' if ok else '#c0392b'};"
        )

    # ==============================================================
    # Basemap management
    # ==============================================================

    def _ensure_basemap(self):
        """Add a basemap layer at the bottom of the layer tree."""
        from qgis.core import QgsRasterLayer, QgsLayerTreeLayer
        project = QgsProject.instance()
        root = project.layerTreeRoot()

        # Already exists?
        for layer in project.mapLayers().values():
            if layer.name() == _BASEMAP_NAME:
                return layer

        for name, cfg in BASEMAP_SOURCES.items():
            layer = QgsRasterLayer(cfg["url"], _BASEMAP_NAME, "wms")
            if layer.isValid():
                project.addMapLayer(layer, False)
                root.addChildNode(QgsLayerTreeLayer(layer))
                logger.info("Basemap loaded: %s", name)
                return layer

        logger.warning("No basemap could be loaded")
        return None

    def _style_event_point(self, layer):
        """Apply red star with white border symbol to event point layer."""
        from qgis.core import QgsMarkerSymbol
        symbol = QgsMarkerSymbol.createSimple({
            'name': 'star',
            'color': '#e74c3c',
            'size': '7',
            'outline_color': '#ffffff',
            'outline_width': '0.8',
        })
        layer.renderer().setSymbol(symbol)
        layer.triggerRepaint()

    # ==============================================================
    # Event handlers
    # ==============================================================

    def _on_free_search_toggle(self, checked):
        """Toggle between event search and free search modes."""
        self.free_group.setVisible(checked)
        self.event_search_widgets.setVisible(not checked)
        if checked:
            # Uncheck all disaster type buttons
            checked_btn = self._dtype_buttons.checkedButton()
            if checked_btn:
                self._dtype_buttons.setExclusive(False)
                checked_btn.setChecked(False)
                self._dtype_buttons.setExclusive(True)
        else:
            # Re-select first disaster type
            for btn in self._dtype_buttons.buttons():
                btn.setChecked(True)
                self._on_dtype_changed(btn)
                break

    def _show_info(self, title, text):
        """Show an info dialog with explanation."""
        try:
            from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton
            from qgis.PyQt.QtCore import Qt
            dlg = QDialog(self)
            dlg.setWindowTitle(f"ℹ {title}")
            dlg.setMinimumWidth(380)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            ly = QVBoxLayout(dlg)
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-size:12px; padding:8px; line-height:1.5;")
            ly.addWidget(lbl)
            btn = QPushButton("OK")
            btn.setStyleSheet("QPushButton{padding:6px; font-weight:bold; "
                              "background:#2980b9; color:white; border-radius:4px;}"
                              "QPushButton:hover{background:#3498db;}")
            btn.clicked.connect(dlg.accept)
            ly.addWidget(btn)
            dlg.show()
        except Exception:
            pass

    def _on_event_date_toggle(self, checked):
        """Toggle the event date field visibility."""
        self.free_event_date_row.setVisible(checked)
        self.free_event_check.setText(
            "☑ Separa PRE / POST" if checked else "☐ Separa PRE / POST")
        if self._pre_products or self._post_products:
            self._populate_tables()

    def _show_exercises(self):
        """Show training exercises catalog for disaster interpretation."""
        try:
            from qgis.PyQt.QtWidgets import (
                QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                QFrame, QScrollArea, QWidget,
            )
            from qgis.PyQt.QtCore import Qt

            dlg = QDialog(self)
            dlg.setWindowTitle("🎓 Esercizi di interpretazione")
            dlg.setMinimumSize(500, 550)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            main_ly = QVBoxLayout(dlg)

            header = QLabel(
                "<b style='font-size:15px;'>🎓 Esercizi di interpretazione</b><br>"
                "<span style='color:#7f8c8d;'>Impara a leggere le immagini satellitari "
                "con eventi reali. Ogni esercizio include le migliori immagini PRE e POST "
                "da scaricare e confrontare.</span>"
            )
            header.setWordWrap(True)
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setStyleSheet("padding:8px;")
            main_ly.addWidget(header)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            content = QWidget()
            content_ly = QVBoxLayout(content)

            exercises = [
                {
                    "title": "⛰ Frana di Niscemi, Sicilia",
                    "color": "#795548",
                    "date": "25 gennaio 2026",
                    "coords": "37.1465, 14.3942",
                    "description": (
                        "Il 25 gennaio 2026 una frana di enormi dimensioni ha colpito "
                        "Niscemi (CL), in Sicilia. Causata dal Ciclone Harry e dalle "
                        "piogge prolungate su argille plioceniche, ha mobilizzato circa "
                        "80 milioni di m3 di materiale con un fronte di 4.7 km e "
                        "profondita' fino a 80 m.<br>"
                        "1.500 persone evacuate. Danni stimati: 2 miliardi EUR.<br>"
                        "Secondo l'Universita' di Firenze, la stabilizzazione "
                        "definitiva e' impossibile."
                    ),
                    "challenge": (
                        "SFIDA: In gennaio le nuvole coprono l'80-100% della Sicilia.<br>"
                        "Le immagini S2 sono quasi tutte inutilizzabili.<br>"
                        "Soluzione: usa le immagini SAR (Sentinel-1) che penetrano le nuvole,<br>"
                        "oppure cerca immagini S2 di marzo con meno nuvole."
                    ),
                    "pre_image": "S2C 11/01/2026 (27% nuvole) — Tile T33SVB",
                    "post_image": "S2C 12/03/2026 — Tile T33SVB (la piu' vicina con meno nuvole)",
                    "bands": "Frana: R=B12(SWIR2) G=B08(NIR) B=B04(Red)",
                    "setup": {
                        "place": "Niscemi",
                        "lat": 37.1465, "lon": 14.3942,
                        "event_date": "2026-01-25",
                        "date_from": "2026-01-10",
                        "date_to": "2026-03-15",
                        "dtype_idx": 4,  # Frana
                    },
                },
                {
                    "title": "🌊 Alluvione in Friuli VG",
                    "color": "#2980b9",
                    "date": "16-17 novembre 2025",
                    "coords": "45.9799, 13.4468",
                    "description": (
                        "Alluvione e frane nel Friuli Venezia Giulia (EMSN228).<br>"
                        "Piogge intense hanno causato esondazioni lungo l'Isonzo "
                        "e altri corsi d'acqua nella zona di Gorizia."
                    ),
                    "challenge": (
                        "SFIDA: Il confronto PRE vs POST mostra chiaramente le zone allagate.<br>"
                        "Le immagini migliori: PRE 10/11 (0% nubi) vs POST 23/11 (2% nubi).<br>"
                        "L'acqua appare NERA nella combinazione SWIR2/NIR/Green."
                    ),
                    "pre_image": "S2A 10/11/2025 (0% nuvole) — Tile T33TUL",
                    "post_image": "S2B 23/11/2025 (2% nuvole) — Tile T33TUL",
                    "bands": "Alluvione: R=B12(SWIR2) G=B08(NIR) B=B03(Green)",
                    "setup": {
                        "place": "Gorizia",
                        "lat": 45.9799, "lon": 13.4468,
                        "event_date": "2025-11-17",
                        "date_from": "2025-11-05",
                        "date_to": "2025-12-15",
                        "dtype_idx": 0,  # Alluvione
                    },
                },
            ]

            for ex in exercises:
                frame = QFrame()
                frame.setStyleSheet(
                    f"QFrame{{border:2px solid {ex['color']}; border-radius:8px; padding:4px;}}")
                frame_ly = QVBoxLayout(frame)

                title = QLabel(f"<b style='color:{ex['color']}; font-size:14px;'>{ex['title']}</b>")
                frame_ly.addWidget(title)

                info = QLabel(
                    f"<b>Data:</b> {ex['date']} | <b>Coord:</b> {ex['coords']}<br>"
                    f"<b>Bande:</b> {ex['bands']}"
                )
                info.setWordWrap(True)
                info.setStyleSheet("font-size:11px; color:#555;")
                frame_ly.addWidget(info)

                desc = QLabel(ex['description'])
                desc.setWordWrap(True)
                desc.setStyleSheet("font-size:11px; padding:4px; background:#f9f9f4; border-radius:4px;")
                frame_ly.addWidget(desc)

                challenge = QLabel(ex['challenge'])
                challenge.setWordWrap(True)
                challenge.setStyleSheet("font-size:11px; padding:4px; background:#fef9e7; border-radius:4px; color:#7d6608;")
                frame_ly.addWidget(challenge)

                imgs = QLabel(
                    f"<b>Immagine PRE:</b> {ex['pre_image']}<br>"
                    f"<b>Immagine POST:</b> {ex['post_image']}"
                )
                imgs.setWordWrap(True)
                imgs.setStyleSheet("font-size:11px; padding:4px; background:#eaf2f8; border-radius:4px;")
                frame_ly.addWidget(imgs)

                # Load exercise button
                setup = ex['setup']
                load_btn = QPushButton(f"🚀 Carica esercizio: {ex['title']}")
                load_btn.setStyleSheet(
                    f"QPushButton{{font-weight:bold; padding:6px; "
                    f"background:{ex['color']}; color:white; border-radius:4px;}}"
                    f"QPushButton:hover{{opacity:0.8;}}"
                )
                load_btn.clicked.connect(
                    lambda checked, s=setup, d=dlg: self._load_exercise(s, d))
                frame_ly.addWidget(load_btn)

                content_ly.addWidget(frame)

            content_ly.addStretch()
            scroll.setWidget(content)
            main_ly.addWidget(scroll)

            close_btn = QPushButton("Chiudi")
            close_btn.setStyleSheet(
                "QPushButton{padding:6px; background:#7f8c8d; color:white; border-radius:4px;}"
                "QPushButton:hover{background:#95a5a6;}")
            close_btn.clicked.connect(dlg.accept)
            main_ly.addWidget(close_btn)

            dlg.show()
        except Exception as exc:
            logger.warning("Exercises dialog error: %s", exc)

    def _load_exercise(self, setup, dialog):
        """Load an exercise configuration into the free search panel."""
        try:
            dialog.accept()
            # Switch to free search mode
            if not self.free_search_btn.isChecked():
                self.free_search_btn.setChecked(True)
                self._on_free_search_toggle(True)

            # Fill in fields
            self.free_place.setText(setup['place'])
            self.free_lat.setText(f"{setup['lat']:.4f}")
            self.free_lon.setText(f"{setup['lon']:.4f}")
            self.free_loc_label.setText(f"✅ {setup['place']}")
            self.free_loc_label.setStyleSheet("font-size:11px; color:#27ae60; font-weight:bold;")

            # Set dates
            from qgis.PyQt.QtCore import QDate
            self.free_date_from.setDate(QDate.fromString(setup['date_from'], "yyyy-MM-dd"))
            self.free_date_to.setDate(QDate.fromString(setup['date_to'], "yyyy-MM-dd"))

            # Enable PRE/POST and set event date
            self.free_event_check.setChecked(True)
            self._on_event_date_toggle(True)
            self.free_event_date.setDate(QDate.fromString(setup['event_date'], "yyyy-MM-dd"))

            # Set disaster type
            if 'dtype_idx' in setup:
                self.free_type_combo.setCurrentIndex(setup['dtype_idx'])

            self._set_status(f"Esercizio caricato: {setup['place']}. Clicca 'Cerca immagini' per procedere.", True)
        except Exception as exc:
            logger.warning("Load exercise error: %s", exc)

    def _on_free_autocomplete_selected(self, lat, lon, name):
        """Handle autocomplete selection in free search."""
        self.free_lat.setText(f"{lat:.4f}")
        self.free_lon.setText(f"{lon:.4f}")
        self.free_loc_label.setText(f"✅ {name}")
        self.free_loc_label.setStyleSheet("font-size:11px; color:#27ae60; font-weight:bold;")
        self.free_search_go.setEnabled(True)
        self._set_status(f"Posizione: {name} ({lat:.4f}, {lon:.4f})", True)

    def _on_loc_autocomplete_selected(self, lat, lon, name):
        """Handle autocomplete selection in location correction."""
        self._set_search_location(lat, lon, name)

    def _on_free_geocode(self):
        """Geocode place name in free search mode."""
        query = self.free_place.text().strip()
        if not query:
            self._set_status("✏ Scrivi il nome di una località (es. Niscemi, Ischia...)", False)
            return
        self._set_status(f"Cercando '{query}'...", True)
        try:
            from ..core.network import geocode_nominatim
            result = geocode_nominatim(query)
            if result:
                lat, lon, name = result["lat"], result["lon"], result["name"]
                self.free_lat.setText(f"{lat:.4f}")
                self.free_lon.setText(f"{lon:.4f}")
                self.free_loc_label.setText(f"✅ {name}: {lat:.4f}, {lon:.4f}")
                self.free_loc_label.setStyleSheet("font-size:11px; color:#27ae60;")
                self.free_search_go.setEnabled(True)
                self._set_status(f"Posizione trovata: {name}", True)
                # Show on map
                self._ensure_basemap()
                project = QgsProject.instance()
                for lid, lyr in list(project.mapLayers().items()):
                    if "Evento:" in lyr.name():
                        project.removeMapLayer(lid)
                layer = QgsVectorLayer("Point?crs=EPSG:4326", f"Evento: {name[:25]}", "memory")
                pr = layer.dataProvider()
                feat = QgsFeature()
                feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
                pr.addFeature(feat)
                layer.updateExtents()
                self._style_event_point(layer)
                project.addMapLayer(layer)
                iface_ref = self.iface
                def _zoom():
                    c = iface_ref.mapCanvas()
                    c.setExtent(QgsRectangle(lon - 0.3, lat - 0.3, lon + 0.3, lat + 0.3))
                    c.refresh()
                QTimer.singleShot(200, _zoom)
            else:
                self._set_status(f"Località '{query}' non trovata", False)
        except Exception as exc:
            self._set_status(f"Errore: {exc}", False)

    def _on_free_pick_map(self):
        """Activate map click tool for free search."""
        from qgis.gui import QgsMapToolEmitPoint
        canvas = self.iface.mapCanvas()
        self._free_map_tool = QgsMapToolEmitPoint(canvas)
        self._free_map_tool.canvasClicked.connect(self._on_free_map_clicked)
        canvas.setMapTool(self._free_map_tool)
        self.free_pick_btn.setText("⭐ Clicca sul punto desiderato...")
        self.free_pick_btn.setStyleSheet(
            "QPushButton{color:#e67e22;border:2px solid #e67e22;"
            "border-radius:4px;padding:4px;font-weight:bold;}")
        self._set_status("Clicca sulla mappa per scegliere la posizione", True)

    def _on_free_map_clicked(self, point, button):
        """Handle map click in free search mode."""
        from qgis.core import QgsCoordinateTransform, QgsCoordinateReferenceSystem
        from qgis.gui import QgsMapToolPan
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        if canvas_crs.authid() != "EPSG:4326":
            xform = QgsCoordinateTransform(
                canvas_crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance())
            point = xform.transform(point)
        lat, lon = point.y(), point.x()
        canvas.setMapTool(QgsMapToolPan(canvas))
        self.free_pick_btn.setText("📍 Clicca sulla mappa per scegliere il punto")
        self.free_pick_btn.setStyleSheet(
            "QPushButton{color:#2980b9;border:1px dashed #2980b9;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#eaf2f8;}")
        self.free_lat.setText(f"{lat:.4f}")
        self.free_lon.setText(f"{lon:.4f}")
        self.free_loc_label.setText(f"✅ Posizione: {lat:.4f}, {lon:.4f}")
        self.free_loc_label.setStyleSheet("font-size:11px; color:#27ae60;")
        self.free_search_go.setEnabled(True)
        self._set_status(f"Posizione selezionata: {lat:.4f}, {lon:.4f}", True)

    def _on_free_search_go(self):
        """Execute free search: go directly to Step 2 with user-specified params."""
        try:
            lat = float(self.free_lat.text())
            lon = float(self.free_lon.text())
        except ValueError:
            self._set_status("☝ Indica prima la posizione: cerca una località o clicca sulla mappa", False)
            return
        dtype_value = self.free_type_combo.currentData()
        self._dtype = dtype_value
        d_from = self.free_date_from.date()
        d_to = self.free_date_to.date()
        has_event_date = self.free_event_check.isChecked()

        if has_event_date:
            d_event = self.free_event_date.date()
            event_date_str = d_event.toString("yyyy-MM-dd")
            days_before = d_from.daysTo(d_event)
            days_after = d_event.daysTo(d_to)
            if days_before < 0:
                self._set_status("La data evento deve essere dopo 'Cerca da'", False)
                return
            if days_after < 0:
                self._set_status("La data evento deve essere prima di 'a'", False)
                return
        else:
            event_date_str = d_from.toString("yyyy-MM-dd")
            days_before = 0
            days_after = max(d_from.daysTo(d_to), 1)

        from ..core.event_sources import DisasterEvent
        place_name = self.free_place.text().strip() or f"{lat:.2f}, {lon:.2f}"
        cfg = DISASTER_CONFIG.get(dtype_value, {})
        ev = DisasterEvent(
            source="manual",
            event_type=dtype_value,
            name=f"{cfg.get('label_it', 'Evento')} - {place_name}",
            date=event_date_str,
            lat=lat, lon=lon,
            code="FREE",
        )
        ev.search_days_before = max(days_before, 0) if has_event_date else 0
        ev.search_days_after = max(days_after, 1)
        ev._has_event_date = has_event_date
        self._selected_event = ev
        self._on_go_to_step2()

    def _on_reset(self):
        """Reset the entire plugin to initial state."""
        # Cancel any running task
        try:
            if self._current_task and not self._current_task.isCanceled():
                self._current_task.cancel()
        except RuntimeError:
            pass
        self._current_task = None

        # Go back to Step 1
        self.stack.setCurrentIndex(0)
        self.progress.setVisible(False)
        self.dl_banner.setVisible(False)

        # Clear events
        self._events = []
        self._selected_event = None
        self.events_table.setRowCount(0)
        self.events_label.setText("")
        self.show_map_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.loc_group.setVisible(False)

        # Clear images
        self._pre_products = []
        self._post_products = []
        self.pre_table.setRowCount(0)
        self.post_table.setRowCount(0)

        # Clear free search fields
        self.free_place.clear()
        self.free_lat.clear()
        self.free_lon.clear()
        self.free_loc_label.setText("Nessuna posizione selezionata")
        self.free_loc_label.setStyleSheet("font-size:11px; color:#7f8c8d;")
        self.free_event_check.setChecked(False)
        self.free_event_date_row.setVisible(False)
        self.free_event_check.setText("☐ Separa PRE / POST")

        # Exit free search mode if active
        if self.free_search_btn.isChecked():
            self.free_search_btn.setChecked(False)
            self.free_group.setVisible(False)
            self.event_search_widgets.setVisible(True)

        # Remove event point layers from map
        project = QgsProject.instance()
        to_remove = [lid for lid, lyr in project.mapLayers().items()
                     if "Evento:" in lyr.name()]
        for lid in to_remove:
            project.removeMapLayer(lid)

        self._set_status("Pronto", True)

    def _on_dtype_changed(self, btn):
        self._dtype = btn.property("dtype")
        # Exit free search mode if active
        if self.free_search_btn.isChecked():
            self.free_search_btn.setChecked(False)
            self.free_group.setVisible(False)
            self.event_search_widgets.setVisible(True)

    def _on_quick_date(self):
        months = self.sender().property("months")
        self.date_from.setDate(QDate.currentDate().addMonths(-months))
        self.date_to.setDate(QDate.currentDate())

    def _on_search_events(self):
        region_name = self.region_combo.currentText()
        bbox = ITALIAN_REGIONS.get(region_name)
        if not bbox:
            self._set_status("Regione non valida. Seleziona una regione.", False)
            return
        d1 = self.date_from.date().toString("yyyy-MM-dd")
        d2 = self.date_to.date().toString("yyyy-MM-dd")

        self.search_btn.setEnabled(False)
        self.search_btn.setText("Ricerca...")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # Indeterminate
        self._set_status("Ricerca eventi in corso...", True)

        task = EventSearchTask(
            self._dtype, bbox, d1, d2,
            self._on_events_ok, self._on_events_err,
        )
        self._current_task = task  # prevent GC
        QgsApplication.taskManager().addTask(task)

    def _on_events_ok(self, events):
        try:
            self.search_btn.setEnabled(True)
            self.search_btn.setText("CERCA EVENTI")
            self.progress.setVisible(False)
            self._events = events

            self.events_table.setRowCount(len(events))
            for i, ev in enumerate(events):
                self.events_table.setItem(i, 0, QTableWidgetItem(ev.date or "?"))
                self.events_table.setItem(i, 1, QTableWidgetItem(
                    ev.name.replace(" (possibile frana)", "")[:30]))
                detail = ""
                if ev.magnitude:
                    detail = f"M{ev.magnitude:.1f}"
                elif ev.area_ha:
                    detail = f"{ev.area_ha:.0f} ha"
                elif ev.code:
                    detail = ev.code
                self.events_table.setItem(i, 2, QTableWidgetItem(detail))
                fonte = ""
                if "(possibile frana)" in ev.name:
                    fonte = "Alluvione/Frana"
                elif ev.source == "cems":
                    fonte = "CEMS"
                elif ev.source == "effis":
                    fonte = "EFFIS"
                self.events_table.setItem(i, 3, QTableWidgetItem(fonte))

            label = DISASTER_CONFIG.get(self._dtype, {}).get("label_it", "")
            self.events_label.setText(f"{len(events)} eventi di tipo '{label}' trovati")
            self._set_status(f"{len(events)} eventi trovati", True)
        except RuntimeError:
            pass

    def _on_events_err(self, msg):
        try:
            self.search_btn.setEnabled(True)
            self.search_btn.setText("CERCA EVENTI")
            self.progress.setVisible(False)
            self.events_label.setText(f"Errore: {msg}")
            self._set_status("Errore ricerca", False)
            self.iface.messageBar().pushCritical("CDE", msg)
        except RuntimeError:
            pass

    def _on_event_selected(self):
        rows = self.events_table.selectionModel().selectedRows()
        has_sel = len(rows) > 0
        self.show_map_btn.setEnabled(has_sel)
        self.next_btn.setEnabled(has_sel)
        if has_sel:
            idx = rows[0].row()
            if idx < len(self._events):
                ev = self._events[idx]
                self._selected_event = ev
                # Populate location panel
                self.lat_edit.setText(f"{ev.lat:.4f}")
                self.lon_edit.setText(f"{ev.lon:.4f}")
                self.loc_label.setText(f"Posizione: {ev.lat:.4f}, {ev.lon:.4f}")
                self.place_edit.clear()
                is_cross = "(possibile frana)" in ev.name
                if is_cross:
                    self.coord_hint.setText(
                        "⚠ Il centroide CEMS potrebbe non corrispondere alla "
                        "zona della frana. Verifica sulla mappa e correggi "
                        "cercando la località esatta."
                    )
                else:
                    self.coord_hint.setText("")
                self.loc_group.setVisible(True)
        else:
            self._selected_event = None
            self.loc_group.setVisible(False)

    def _on_show_event_on_map(self):
        if not self._selected_event:
            self._set_status("☝ Seleziona prima un evento dalla tabella", False)
            return
        ev = self._selected_event
        # Use corrected coords if available
        try:
            lat = float(self.lat_edit.text())
            lon = float(self.lon_edit.text())
        except ValueError:
            lat, lon = ev.lat, ev.lon
        layer = QgsVectorLayer("Point?crs=EPSG:4326", f"Evento: {ev.name[:25]}", "memory")
        pr = layer.dataProvider()
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        pr.addFeature(feat)
        layer.updateExtents()
        self._style_event_point(layer)
        QgsProject.instance().addMapLayer(layer)
        self._ensure_basemap()
        self._set_status(f"Evento mostrato: {ev.name[:30]}", True)
        iface_ref = self.iface
        def _do_zoom():
            canvas = iface_ref.mapCanvas()
            canvas.setExtent(QgsRectangle(lon - 0.5, lat - 0.5, lon + 0.5, lat + 0.5))
            canvas.refresh()
        QTimer.singleShot(200, _do_zoom)

    def _on_go_to_step2(self):
        if not self._selected_event:
            self._set_status("☝ Seleziona prima un evento dalla tabella", False)
            return
        ev = self._selected_event
        cfg = DISASTER_CONFIG.get(self._dtype, {})
        # Use coords from the appropriate source
        lat, lon = ev.lat, ev.lon
        try:
            if self.lat_edit.text():
                lat = float(self.lat_edit.text())
                lon = float(self.lon_edit.text())
        except ValueError:
            pass
        self.event_info_label.setText(
            f"{cfg.get('label_it', '')} - {ev.display_name}<br>"
            f"Data: {ev.date} | Posizione: {lat:.4f}, {lon:.4f}"
            + (f"\nPeriodo: {ev.date} → +{getattr(ev, 'search_days_after', '?')} giorni"
               if getattr(ev, 'search_days_before', None) == 0 else "")
        )
        # Show which bands will be loaded
        info_parts = []
        if cfg.get("bands_s1"):
            info_parts.append("SAR: " + ", ".join(p.upper() for p in cfg["bands_s1"]))
        if cfg.get("bands_s2"):
            info_parts.append("Ottico: " + ", ".join(cfg["bands_s2"]))
            fc = cfg.get("false_color") or cfg.get("false_color_s2")
            if fc:
                info_parts.append(f"Falso colore: R={fc['R']}, G={fc['G']}, B={fc['B']}")
        self.bands_info.setText("  |  ".join(info_parts))

        # Search for imagery
        self._run_image_search(ev.lat, ev.lon)

    def _run_image_search(self, lat, lon):
        """Launch the Sentinel image search at given coordinates."""
        if not self._selected_event:
            self._set_status("Nessun evento selezionato", False)
            return
        ev = self._selected_event
        # Create a copy of the event with overridden coordinates
        from ..core.event_sources import DisasterEvent
        search_ev = DisasterEvent(
            source=ev.source, event_type=ev.event_type,
            name=ev.name, date=ev.date,
            lat=lat, lon=lon,
            bbox=None,  # Force recalculation from new lat/lon
            magnitude=ev.magnitude, area_ha=ev.area_ha,
            province=ev.province, code=ev.code,
        )
        # Preserve custom search days from free search
        if hasattr(ev, 'search_days_before'):
            search_ev.search_days_before = ev.search_days_before
        if hasattr(ev, 'search_days_after'):
            search_ev.search_days_after = ev.search_days_after
        self.stack.setCurrentIndex(1)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self._set_status("Ricerca immagini Sentinel...", True)
        self.download_btn.setEnabled(False)

        task = ImageSearchTask(
            search_ev, self._dtype, 30,
            self._on_images_ok, self._on_images_err,
        )
        self._current_task = task  # prevent GC
        QgsApplication.taskManager().addTask(task)

    def _on_update_coords_search(self):
        """Re-run image search with user-edited coordinates (internal)."""
        try:
            lat = float(self.lat_edit.text())
            lon = float(self.lon_edit.text())
        except ValueError:
            return
        self._set_search_location(lat, lon, "coordinate manuali")

    def _set_search_location(self, lat, lon, source=""):
        """Update search coordinates and show point on map.

        Called from geocode or map-click. Updates the stored coordinates
        and shows the corrected point on the map so the user can verify
        before clicking 'Cerca immagini'.
        """
        self.lat_edit.setText(f"{lat:.4f}")
        self.lon_edit.setText(f"{lon:.4f}")
        self.loc_label.setText(f"Posizione: {lat:.4f}, {lon:.4f}" +
                               (f" ({source})" if source else ""))
        self.coord_hint.setText("✅ Posizione aggiornata. Clicca 'Cerca immagini' per continuare.")
        self.coord_hint.setStyleSheet("color:#27ae60; font-size:10px;")
        self._set_status(f"Posizione aggiornata: {lat:.4f}, {lon:.4f}", True)

        # Show corrected point on map
        ev = self._selected_event
        if ev:
            # Remove old event layers
            project = QgsProject.instance()
            for lid, lyr in list(project.mapLayers().items()):
                if "Evento:" in lyr.name():
                    project.removeMapLayer(lid)
            # Add new point at corrected location
            layer = QgsVectorLayer(
                "Point?crs=EPSG:4326",
                f"Evento: {ev.name[:25]}", "memory"
            )
            pr = layer.dataProvider()
            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
            pr.addFeature(feat)
            layer.updateExtents()
            self._style_event_point(layer)
            project.addMapLayer(layer)
            self._ensure_basemap()
            iface_ref = self.iface
            def _do_zoom():
                canvas = iface_ref.mapCanvas()
                canvas.setExtent(QgsRectangle(lon - 0.3, lat - 0.3, lon + 0.3, lat + 0.3))
                canvas.refresh()
            QTimer.singleShot(200, _do_zoom)
        # Auto-scroll Step 1 to show "Cerca immagini" button
        def _scroll():
            sa = self.stack.widget(0)
            if hasattr(sa, 'ensureWidgetVisible'):
                sa.ensureWidgetVisible(self.next_btn)
        QTimer.singleShot(400, _scroll)

    def _on_geocode(self):
        """Geocode a place name using OpenStreetMap Nominatim."""
        query = self.place_edit.text().strip()
        if not query:
            self._set_status("✏ Scrivi il nome di una località per la ricerca", False)
            return
        self._set_status(f"Cercando '{query}'...", True)
        try:
            from ..core.network import geocode_nominatim
            result = geocode_nominatim(query)
            if result:
                self._set_search_location(result["lat"], result["lon"], result["name"])
            else:
                self._set_status(f"Località '{query}' non trovata", False)
        except Exception as exc:
            self._set_status(f"Errore geocodifica: {exc}", False)

    def _on_pick_from_map(self):
        """Activate map click tool to pick coordinates."""
        from qgis.gui import QgsMapToolEmitPoint
        canvas = self.iface.mapCanvas()

        self._map_pick_tool = QgsMapToolEmitPoint(canvas)
        self._map_pick_tool.canvasClicked.connect(self._on_map_clicked)
        canvas.setMapTool(self._map_pick_tool)
        self.pick_map_btn.setText("⭐ Clicca sul punto desiderato nella mappa...")
        self.pick_map_btn.setStyleSheet(
            "QPushButton{color:#e67e22;border:2px solid #e67e22;"
            "border-radius:4px;padding:4px;font-weight:bold;}"
        )
        self._set_status("Clicca sulla mappa per scegliere la posizione", True)

    def _on_map_clicked(self, point, button):
        """Handle map canvas click for location picking."""
        from qgis.core import QgsCoordinateTransform, QgsCoordinateReferenceSystem
        canvas = self.iface.mapCanvas()
        # Convert click point to EPSG:4326
        canvas_crs = canvas.mapSettings().destinationCrs()
        if canvas_crs.authid() != "EPSG:4326":
            xform = QgsCoordinateTransform(
                canvas_crs,
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance(),
            )
            point = xform.transform(point)
        lat, lon = point.y(), point.x()
        # Restore normal map tool
        from qgis.gui import QgsMapToolPan
        canvas.setMapTool(QgsMapToolPan(canvas))
        self.pick_map_btn.setText("📍 Clicca sulla mappa per scegliere il punto")
        self.pick_map_btn.setStyleSheet(
            "QPushButton{color:#2980b9;border:1px dashed #2980b9;"
            "border-radius:4px;padding:4px;}"
            "QPushButton:hover{background:#eaf2f8;}")
        self._set_search_location(lat, lon, "dalla mappa")

    def _get_cloud_threshold(self):
        """Get the current cloud cover threshold from the filter dropdown."""
        idx = self.cloud_slider.currentIndex()
        thresholds = [100, 20, 50, 80]
        return thresholds[idx] if idx < len(thresholds) else 100

    def _on_cloud_filter_changed(self, index):
        """Re-populate image tables when cloud filter changes."""
        if self._pre_products or self._post_products:
            self._populate_tables()

    def _populate_tables(self):
        """Populate pre/post tables with cloud-based filtering and coloring."""
        ev = self._selected_event
        is_free = ev and ev.source == "manual"
        threshold = self._get_cloud_threshold()
        has_event_date = getattr(ev, '_has_event_date', False) if ev else False

        if is_free and not has_event_date:
            # No event date: show all images in one table
            all_products = sorted(
                self._pre_products + self._post_products,
                key=lambda p: p.sensing_date or "", reverse=True)
            self.pre_label.setVisible(False)
            self.pre_table.setVisible(False)
            self.post_label.setText("Immagini disponibili:")
            self.post_label.setVisible(True)
            self.post_table.setMaximumHeight(200)
            self.post_table.setVisible(True)
            self._fill_table(self.post_table, all_products, threshold)
            visible = sum(1 for p in all_products if self._passes_cloud(p, threshold))
            self._set_status(f"{visible}/{len(all_products)} immagini (filtro: {threshold}%)", True)
        elif is_free and has_event_date:
            # Has event date: split into PRE/POST
            all_products = self._pre_products + self._post_products
            event_date = ev.date or ""
            pre = sorted([p for p in all_products if (p.sensing_date or "") < event_date],
                         key=lambda p: p.sensing_date or "", reverse=True)
            post = sorted([p for p in all_products if (p.sensing_date or "") >= event_date],
                          key=lambda p: p.sensing_date or "", reverse=True)
            self.pre_label.setText(f"Pre-evento (prima del {event_date}):")
            self.pre_label.setVisible(True)
            self.pre_table.setVisible(True)
            self.pre_table.setMaximumHeight(120)
            self.post_label.setText(f"Post-evento (dal {event_date}):")
            self.post_label.setVisible(True)
            self.post_table.setVisible(True)
            self.post_table.setMaximumHeight(120)
            self._fill_table(self.pre_table, pre, threshold)
            self._fill_table(self.post_table, post, threshold)
            pre_vis = sum(1 for p in pre if self._passes_cloud(p, threshold))
            post_vis = sum(1 for p in post if self._passes_cloud(p, threshold))
            self._set_status(
                f"{pre_vis + post_vis} immagini ({pre_vis} pre, {post_vis} post) | filtro: {threshold}%",
                True)
        else:
            # Event search: use pre/post as returned
            self.pre_label.setText("Pre-evento:")
            self.pre_label.setVisible(True)
            self.pre_table.setVisible(True)
            self.pre_table.setMaximumHeight(120)
            self.post_label.setText("Post-evento:")
            self.post_label.setVisible(True)
            self.post_table.setVisible(True)
            self.post_table.setMaximumHeight(120)
            self._fill_table(self.pre_table, self._pre_products, threshold)
            self._fill_table(self.post_table, self._post_products, threshold)
            pre_vis = sum(1 for p in self._pre_products if self._passes_cloud(p, threshold))
            post_vis = sum(1 for p in self._post_products if self._passes_cloud(p, threshold))
            self._set_status(
                f"{pre_vis + post_vis} immagini ({pre_vis} pre, {post_vis} post) | filtro: {threshold}%",
                True)

    def _passes_cloud(self, product, threshold):
        """Check if product passes the cloud filter."""
        if "S1" in product.name:
            return True  # SAR is always visible
        if product.cloud_cover is None or product.cloud_cover < 0:
            return True  # Unknown cloud, show it
        return product.cloud_cover <= threshold

    def _fill_table(self, table, products, threshold):
        """Fill a table widget with products, coloring by cloud cover."""
        from qgis.PyQt.QtGui import QColor, QBrush
        table.setRowCount(len(products))

        hidden_count = 0
        for i, p in enumerate(products):
            # Populate cells
            table.setItem(i, 0, QTableWidgetItem(p.sensing_date))
            table.setItem(i, 1, QTableWidgetItem(p.sensor_label))
            table.setItem(i, 2, QTableWidgetItem(p.cloud_display))
            table.setItem(i, 3, QTableWidgetItem(p.size_display))

            # Color by cloud cover
            is_sar = "S1" in p.name
            cloud = p.cloud_cover

            if is_sar:
                # SAR: blue tint (always visible, no clouds)
                bg = QColor(200, 220, 255, 40)
            elif cloud is None or cloud < 0:
                # Unknown: light gray
                bg = QColor(240, 240, 240, 40)
            elif cloud <= 20:
                # Clean: green
                bg = QColor(39, 174, 96, 50)
            elif cloud <= 50:
                # Partial: yellow
                bg = QColor(241, 196, 15, 50)
            else:
                # Cloudy: red
                bg = QColor(192, 57, 43, 50)

            brush = QBrush(bg)
            for col in range(4):
                item = table.item(i, col)
                if item:
                    item.setBackground(brush)

            # Hide row if above threshold (except SAR)
            if not self._passes_cloud(p, threshold):
                table.setRowHidden(i, True)
                hidden_count += 1
            else:
                table.setRowHidden(i, False)

    def _toggle_table_selection(self, table, row):
        """Toggle row selection: click selected row to deselect."""
        if hasattr(self, '_last_clicked') and self._last_clicked == (table, row):
            table.clearSelection()
            self._last_clicked = None
        else:
            self._last_clicked = (table, row)

    def _on_images_ok(self, pre, post):
        try:
            self.progress.setVisible(False)
            self.download_btn.setEnabled(True)

            ev = self._selected_event
            is_free = ev and ev.source == "manual"

            if is_free:
                all_products = sorted(pre + post,
                    key=lambda p: p.sensing_date or "", reverse=True)
                self._pre_products = []
                self._post_products = all_products
            else:
                self._pre_products = pre
                self._post_products = post

            self._populate_tables()
        except RuntimeError:
            pass

    def _on_images_err(self, msg):
        try:
            self.progress.setVisible(False)
            self._set_status("Errore ricerca immagini", False)
            self.iface.messageBar().pushCritical("CDE", msg)
        except RuntimeError:
            pass

    def _on_download(self):
        # Get selected post-event image (preferred) or pre-event
        product = None
        rows = self.post_table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self._post_products):
            product = self._post_products[rows[0].row()]
        else:
            rows = self.pre_table.selectionModel().selectedRows()
            if rows and rows[0].row() < len(self._pre_products):
                product = self._pre_products[rows[0].row()]
        if not product:
            # Auto-select first post if available, else first pre
            if self._post_products:
                product = self._post_products[0]
            elif self._pre_products:
                product = self._pre_products[0]
        if not product:
            self.iface.messageBar().pushWarning("CDE", "Nessuna immagine disponibile")
            return

        dl_dir = self.download_dir.filePath()
        if not dl_dir or not os.path.isdir(dl_dir):
            self.iface.messageBar().pushWarning("CDE", "Seleziona una cartella di download")
            return

        ev = self._selected_event
        display_name = f"{DISASTER_CONFIG.get(self._dtype,{}).get('label_it','')} {ev.name[:20]} {product.sensing_date}"

        self.stack.setCurrentIndex(2)
        self.dl_status_label.setText(
            f"Download: {product.name[:50]}...<br>"
            f"Dimensione: {product.size_display}<br>"
            f"Questo puo richiedere diversi minuti."
        )
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._set_status(f"Download {product.size_display}...", True)
        self.cancel_dl_btn.setEnabled(True)

        task = DownloadAndLoadTask(
            product, dl_dir, self._dtype, display_name,
            self._on_dl_ok, self._on_dl_err,
        )
        self._current_task = task  # prevent GC

        # Connect task progress to progress bar
        task.progressChanged.connect(self._on_dl_progress)

        QgsApplication.taskManager().addTask(task)

    def _on_dl_progress(self, progress):
        """Update progress bar from task progress signal."""
        try:
            self.progress.setValue(int(progress))
            status_text = f"Download: {progress:.0f}%"
            if progress > 0:
                self.dl_status_label.setText(
                    self.dl_status_label.text().split("<br>")[0] + "<br>"
                    f"Progresso: {progress:.0f}%"
                )
            # Update banner if user is browsing Step 1
            if self.dl_banner.isVisible():
                self.dl_banner.setText(f"⬇ Download in corso: {progress:.0f}% — clicca per vedere")
            self._set_status(status_text, True)
        except RuntimeError:
            pass

    def _navigate_away_from_download(self):
        """Go back to Step 1 while download continues in background."""
        self.stack.setCurrentIndex(0)
        # Show banner so user can return to download
        self.dl_banner.setVisible(True)
        pct = self.progress.value()
        self.dl_banner.setText(f"⬇ Download in corso: {pct}% — clicca per vedere")

    def _on_cancel_download(self):
        """Cancel the active download task."""
        try:
            if self._current_task is not None:
                try:
                    self._current_task.cancel()
                except RuntimeError:
                    pass
                self._current_task = None
            self.progress.setVisible(False)
            self.cancel_dl_btn.setEnabled(False)
            self.dl_banner.setVisible(False)
            self.dl_status_label.setText("Download annullato dall'utente.")
            self._set_status("Download annullato", False)
            QTimer.singleShot(500, lambda: self.stack.setCurrentIndex(0))
        except RuntimeError:
            pass

    def _on_dl_ok(self, zip_path, layers):
        try:
            self.progress.setVisible(False)
            self.cancel_dl_btn.setEnabled(False)
            self.dl_banner.setVisible(False)
            self._current_task = None
            if layers:
                self._set_status(f"{len(layers)} bande caricate!", True)
                self.iface.messageBar().pushSuccess(
                    "CDE", f"Completato: {len(layers)} bande caricate"
                )
                self.dl_status_label.setText(
                    f"Download completato!<br>"
                    f"{len(layers)} bande caricate in QGIS.<br>"
                    f"Controlla il pannello dei layer."
                )
                if self.stack.currentIndex() == 0:
                    self.dl_banner.setVisible(True)
                    self.dl_banner.setText(f"✅ Download completato! {len(layers)} bande caricate. Clicca per dettagli.")
                    self.dl_banner.setStyleSheet(
                        "QPushButton{background:#27ae60;color:white;font-weight:bold;"
                        "padding:8px;border-radius:4px;text-align:left;}"
                        "QPushButton:hover{background:#2ecc71;}")

                # Auto-zoom to the loaded image extent
                try:
                    from qgis.core import QgsCoordinateTransform
                    layer = layers[0]
                    canvas = self.iface.mapCanvas()
                    xform = QgsCoordinateTransform(
                        layer.crs(), canvas.mapSettings().destinationCrs(),
                        QgsProject.instance())
                    extent = xform.transformBoundingBox(layer.extent())
                    canvas.setExtent(extent)
                    canvas.refresh()
                except Exception:
                    try:
                        self.iface.mapCanvas().setExtent(layers[0].extent())
                        self.iface.mapCanvas().refresh()
                    except Exception:
                        pass

                # Show educational legend dialog
                self._show_edu_legend()
            else:
                self._set_status("Download OK, bande non trovate", False)
                self.dl_status_label.setText(
                    "Download completato ma nessuna banda trovata.<br>"
                    "Il prodotto potrebbe avere un formato non supportato."
                )
        except RuntimeError:
            pass

    def _show_usage_guide(self):
        """Show plugin usage tutorial."""
        try:
            from qgis.PyQt.QtWidgets import (
                QDialog, QVBoxLayout, QLabel, QPushButton,
                QTabWidget, QWidget, QScrollArea, QFrame,
            )
            from qgis.PyQt.QtCore import Qt

            dlg = QDialog(self)
            dlg.setWindowTitle("Come usare il Copernicus Disaster Explorer")
            dlg.setMinimumSize(520, 560)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            main_ly = QVBoxLayout(dlg)

            header = QLabel(
                "<b style='font-size:16px;'>"
                "❓ Come usare il plugin"
                "</b><br>"
                "<span style='color:#7f8c8d;'>"
                "Guida passo-passo per esplorare le immagini satellitari dei disastri"
                "</span>"
            )
            header.setWordWrap(True)
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setStyleSheet("padding:8px;")
            main_ly.addWidget(header)

            tabs = QTabWidget()
            tabs.setStyleSheet(
                "QTabBar::tab{padding:6px 10px; font-weight:bold; font-size:11px;}"
                "QTabBar::tab:selected{color:#8e44ad; border-bottom:2px solid #8e44ad;}"
            )

            tab_data = [
                ("🔍 Cerca eventi", [
                    ("<b style='color:#27ae60;'>PASSO 1: Cerca un evento</b>", ""),
                    ("<b>1.</b> Seleziona il <b>tipo di evento</b>",
                     "Alluvione, Incendio, Vulcano o Frana."),
                    ("<b>2.</b> Scegli la <b>regione</b>",
                     "Seleziona una regione italiana o 'Tutta Italia'."),
                    ("<b>3.</b> Imposta le <b>date</b>",
                     "Usa i pulsanti rapidi (1 mese, 3 mesi, 1 anno) o le date manuali."),
                    ("<b>4.</b> Clicca <b>CERCA EVENTI</b>",
                     "Il plugin cerca eventi nel catalogo Copernicus EMS, EFFIS."),
                    ("<b>5.</b> Seleziona un evento dalla tabella",
                     "Clicca su una riga per selezionare l'evento."),
                    ("<b>6.</b> Clicca <b>Mostra sulla mappa</b>",
                     "Vedi la posizione dell'evento sulla mappa. "
                     "Puoi correggere la posizione cercando una localita' o cliccando sulla mappa."),
                    ("<b>7.</b> Clicca <b>Cerca immagini</b>",
                     "Il plugin cerca immagini Sentinel-1 e Sentinel-2 intorno all'evento."),
                ]),
                ("🌍 Ricerca libera", [
                    ("<b style='color:#8e44ad;'>Cerca senza eventi predefiniti</b>", ""),
                    ("<b>1.</b> Attiva <b>Ricerca libera</b>",
                     "Clicca il pulsante viola in alto."),
                    ("<b>2.</b> Cerca la <b>localita'</b>",
                     "Scrivi il nome (es. 'Niscemi') e seleziona dal menu a tendina. "
                     "Oppure clicca sulla mappa."),
                    ("<b>3.</b> Attiva <b>Separa PRE/POST</b> (opzionale)",
                     "Se conosci la data dell'evento, attiva questa opzione. "
                     "Le immagini verranno divise in PRIMA e DOPO l'evento."),
                    ("<b>4.</b> Imposta le <b>date di ricerca</b>",
                     "Definisci l'intervallo in cui cercare le immagini. "
                     "Consiglio: 30-60 giorni centrati sull'evento."),
                    ("<b>5.</b> Scegli <b>Bande e simbologia</b>",
                     "Il tipo di evento determina quali bande Sentinel-2 usare "
                     "e come colorare l'immagine."),
                    ("<b>6.</b> Clicca <b>Cerca immagini</b>",
                     "Appariranno le immagini disponibili con il livello di nuvole."),
                ]),
                ("⬇ Scarica e analizza", [
                    ("<b style='color:#2980b9;'>Scaricare e visualizzare le immagini</b>", ""),
                    ("<b>1.</b> Scegli un'immagine dalla tabella",
                     "Le righe sono colorate per nuvole:<br>"
                     "<span style='color:#27ae60;'>■ Verde</span> = poche nuvole (ideale)<br>"
                     "<span style='color:#f1c40f;'>■ Giallo</span> = parzialmente nuvoloso<br>"
                     "<span style='color:#c0392b;'>■ Rosso</span> = molto nuvoloso (nascosto)<br>"
                     "<span style='color:#3498db;'>■ Blu</span> = SAR (senza nuvole)"),
                    ("<b>2.</b> Usa il <b>filtro nuvole</b>",
                     "Il dropdown in alto permette di filtrare: Max 20%, 50%, 80% o Mostra tutte."),
                    ("<b>3.</b> Clicca <b>Scarica e Visualizza</b>",
                     "L'immagine viene scaricata e caricata automaticamente in QGIS "
                     "con la simbologia corretta."),
                    ("<b>4.</b> Il plugin carica automaticamente:",
                     "<b>Falso colore</b> (visibile) - la combinazione ottimale per il tipo di evento<br>"
                     "<b>Colore naturale</b> (nascosto) - come una fotografia<br>"
                     "Per frane: anche un layer <b>Geologia</b> (nascosto)"),
                    ("<b>5.</b> Puoi continuare a esplorare",
                     "Clicca 'Continua a esplorare' per tornare alle immagini "
                     "mentre il download prosegue in background."),
                ]),
                ("📊 Confronto PRE/POST", [
                    ("<b style='color:#e67e22;'>Come confrontare prima e dopo</b>", ""),
                    ("<b>Il confronto e' la chiave!</b>",
                     "Nessuna combinazione di bande identifica un disastro da sola. "
                     "Serve SEMPRE confrontare un'immagine PRIMA e una DOPO l'evento."),
                    ("<b>1.</b> Scarica almeno <b>2 immagini</b>",
                     "Una PRE-evento e una POST-evento. "
                     "Scegli quelle con meno nuvole."),
                    ("<b>2.</b> Nel <b>pannello dei layer</b>",
                     "Alterna la visibilita' (checkbox) dei layer PRE e POST. "
                     "Il plugin applica lo stesso stretch per un confronto diretto."),
                    ("<b>3.</b> Cosa cercare:",
                     "<b>Alluvione:</b> zone che diventano NERE (acqua)<br>"
                     "<b>Incendio:</b> zone verdi che diventano ROSSE (cicatrice)<br>"
                     "<b>Frana:</b> zone verdi che diventano ROSA (suolo esposto)<br>"
                     "<b>Vulcano:</b> nuove zone GIALLE (lava calda)"),
                    ("<b>4.</b> Usa il <b>Colore naturale</b>",
                     "Attiva il layer 'Colore naturale' per orientarti. "
                     "E' come una foto e aiuta a capire cosa stai guardando."),
                    ("<b>Suggerimento:</b>",
                     "Se le nuvole coprono tutto, usa le immagini SAR (Sentinel-1). "
                     "Il radar penetra le nuvole!"),
                ]),
                ("⚙ Strumenti", [
                    ("<b style='color:#7f8c8d;'>Funzionalita' del plugin</b>", ""),
                    ("<b>📖 Guida interpretazione</b>",
                     "Spiega cosa significa ogni colore per ogni tipo di evento. "
                     "Contiene una scheda per Alluvione, Incendio, Vulcano e Frana."),
                    ("<b>🎓 Esercizi</b>",
                     "Casi reali con immagini consigliate da scaricare e confrontare. "
                     "Clicca 'Carica esercizio' per pre-compilare tutti i campi."),
                    ("<b>🗑 Pulisci tutto</b>",
                     "Resetta il plugin allo stato iniziale. "
                     "Cancella tabelle, punti evento e ricerca libera."),
                    ("<b>ℹ Pulsanti info</b>",
                     "I pulsanti blu (i) accanto ai campi nella Ricerca libera "
                     "spiegano a cosa serve ogni opzione."),
                    ("<b>Autocomplete localita'</b>",
                     "Scrivi almeno 3 lettere nel campo di ricerca e apparira' "
                     "un menu a tendina con i comuni italiani."),
                    ("<b>Download in background</b>",
                     "Puoi continuare a lavorare in QGIS mentre un'immagine "
                     "si scarica. Un banner verde mostra il progresso."),
                ]),
            ]

            for tab_title, items in tab_data:
                tab = QWidget()
                tab_ly = QVBoxLayout(tab)
                tab_ly.setSpacing(4)
                for title_html, desc in items:
                    if not desc:
                        lbl = QLabel(title_html)
                        lbl.setStyleSheet("font-size:13px; padding:6px 0 2px 0;")
                    else:
                        text = f"{title_html}<br><span style='color:#555;'>{desc}</span>"
                        lbl = QLabel(text)
                        lbl.setStyleSheet("font-size:11px; padding:2px 8px;")
                    lbl.setWordWrap(True)
                    tab_ly.addWidget(lbl)
                tab_ly.addStretch()
                scroll = QScrollArea()
                scroll.setWidget(tab)
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QScrollArea.Shape.NoFrame)
                tabs.addTab(scroll, tab_title)

            main_ly.addWidget(tabs)

            close_btn = QPushButton("Chiudi")
            close_btn.setStyleSheet(
                "QPushButton{padding:8px; font-weight:bold;"
                "background:#8e44ad; color:white; border-radius:4px;}"
                "QPushButton:hover{background:#9b59b6;}")
            close_btn.clicked.connect(dlg.accept)
            main_ly.addWidget(close_btn)
            dlg.show()
        except Exception as exc:
            logger.warning("Usage guide error: %s", exc)

    def _show_interpretation_guide(self):
        """Show comprehensive interpretation guide for all disaster types."""
        try:
            from qgis.PyQt.QtWidgets import (
                QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                QFrame, QTabWidget, QWidget, QScrollArea,
            )
            from qgis.PyQt.QtCore import Qt

            dlg = QDialog(self)
            dlg.setWindowTitle("Guida all'interpretazione delle immagini satellitari")
            dlg.setMinimumSize(520, 580)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            main_ly = QVBoxLayout(dlg)

            # Header
            header = QLabel(
                "<b style='font-size:16px;'>"
                "📖 Guida all'interpretazione"
                "</b><br>"
                "<span style='color:#7f8c8d;'>"
                "Come leggere le immagini Sentinel per ogni tipo di evento"
                "</span>"
            )
            header.setWordWrap(True)
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setStyleSheet("padding:8px;")
            main_ly.addWidget(header)

            tabs = QTabWidget()
            tabs.setStyleSheet(
                "QTabBar::tab{padding:6px 12px; font-weight:bold;}"
                "QTabBar::tab:selected{color:#2980b9; border-bottom:2px solid #2980b9;}"
            )

            guide_data = [
                ("🌊 Alluvione", "#2980b9", [
                    ("<b>Combinazione principale: SWIR2/NIR/Green</b>", "R=B12 (2190nm) | G=B08 (842nm) | B=B03 (560nm)"),
                    ("<b>Colore naturale (alternativo):</b>", "R=B04 | G=B03 | B=B02 — come una fotografia"),
                    ("<b>Cosa cercare:</b>", ""),
                    ("<span style='color:#111133'>■ NERO / BLU SCURO</span>", "= Acqua. L'acqua assorbe fortemente SWIR e NIR, apparendo molto scura."),
                    ("<span style='color:#33cc33'>■ VERDE BRILLANTE</span>", "= Vegetazione sana. Alta riflessione NIR."),
                    ("<span style='color:#cc8866'>■ MARRONE / ROSA</span>", "= Suolo nudo, aree urbane."),
                    ("<span style='color:#ffffff; background:#666'>■ BIANCO</span>", "= Nuvole."),
                    ("<b>Come identificare l'alluvione:</b>", "Confronta PRE vs POST. Le zone allagate appaiono NERE nel POST dove prima c'era vegetazione (VERDE) o terreno. Cerca aree scure lungo fiumi e pianure."),
                    ("<b>Perche' questa combinazione?</b>", "La banda SWIR2 (B12) e' fortemente assorbita dall'acqua, creando il massimo contrasto tra terra e acqua. La NIR (B08) distingue la vegetazione."),
                ]),
                ("🔥 Incendio", "#e67e22", [
                    ("<b>Combinazione principale: SWIR2/NIR/Red</b>", "R=B12 (2190nm) | G=B08 (842nm) | B=B04 (665nm)"),
                    ("<b>Colore naturale (alternativo):</b>", "R=B04 | G=B03 | B=B02"),
                    ("<b>Cosa cercare:</b>", ""),
                    ("<span style='color:#cc4444'>■ ROSSO / MARRONE SCURO</span>", "= Cicatrice di incendio. Alta SWIR (suolo caldo/cenere), bassa NIR (vegetazione bruciata)."),
                    ("<span style='color:#33cc33'>■ VERDE BRILLANTE</span>", "= Vegetazione sana non bruciata."),
                    ("<span style='color:#cc66aa'>■ ROSA / MAGENTA</span>", "= Suolo nudo."),
                    ("<span style='color:#ff8800'>■ ARANCIONE / GIALLO</span>", "= Incendio ATTIVO (la fiamma emette in SWIR)."),
                    ("<b>Come identificare l'incendio:</b>", "La cicatrice appare come area ROSSO/MARRONE SCURO con bordo irregolare. Confronta PRE (verde) vs POST (rosso) per delimitare l'area bruciata."),
                    ("<b>Indice utile: NBR</b>", "NBR = (B08-B12)/(B08+B12). Valori bassi o negativi = area bruciata."),
                ]),
                ("🌋 Vulcano", "#c0392b", [
                    ("<b>Combinazione principale: SWIR2/SWIR1/Red</b>", "R=B12 (2190nm) | G=B11 (1610nm) | B=B04 (665nm)"),
                    ("<b>Colore naturale (alternativo):</b>", "R=B04 | G=B03 | B=B02"),
                    ("<b>Cosa cercare:</b>", ""),
                    ("<span style='color:#ffcc00'>■ GIALLO / BIANCO</span>", "= Lava calda o attivita' termale. La lava emette RADIAZIONE INFRAROSSA a onde corte."),
                    ("<span style='color:#555555'>■ GRIGIO SCURO</span>", "= Cenere vulcanica o colate di lava raffreddata."),
                    ("<span style='color:#33cc33'>■ VERDE</span>", "= Vegetazione."),
                    ("<span style='color:#8855cc'>■ VIOLA / BLU</span>", "= Gas vulcanici (SO2)."),
                    ("<b>Perche' il doppio SWIR?</b>", "B12 + B11 sono entrambe sensibili al calore. Questa combinazione e' l'unica che puo' 'vedere' la temperatura: lava attiva appare GIALLA/BIANCA, colate fredde GRIGIO SCURO."),
                ]),
                ("⛰ Frana", "#795548", [
                    ("<b>Combinazione principale: SWIR2/NIR/Red</b>", "R=B12 | G=B08 | B=B04"),
                    ("<b>Colore naturale (alternativo):</b>", "R=B04 | G=B03 | B=B02 — il piu' intuitivo per frane"),
                    ("<b>Geologia (alternativo):</b>", "R=B12 | G=B11 | B=B04 — distingue tipi di suolo"),
                    ("<b>Cosa cercare:</b>", ""),
                    ("<span style='color:#cc66aa'>■ ROSA / MAGENTA</span>", "= Suolo esposto, corpo di frana (alta SWIR, bassa NIR = terreno senza vegetazione)."),
                    ("<span style='color:#33cc33'>■ VERDE BRILLANTE</span>", "= Vegetazione sana."),
                    ("<span style='color:#886644'>■ MARRONE</span>", "= Suolo secco, argilla."),
                    ("<b>Come identificare la frana:</b>", "1) Confronta PRE vs POST: verde che diventa rosa/marrone.\n2) Cerca forma irregolare, lobata (non rettangolare come un campo).\n3) Su versante, non in pianura.\n4) Cerca la SCARPATA: bordo netto in alto (corona) con materiale accumulato in basso."),
                    ("<b>Combinazione geologia (B12/B11/B04):</b>", "Arancione = argilla (come a Niscemi!), Rosso = suolo umido/saturo, Marrone = suolo secco. Utile per capire PERCHE' e' avvenuta la frana."),
                    ("<b>Nota per l'Italia:</b>", "In inverno (nov-feb) le nuvole coprono quasi tutto il sud Italia. Per frane invernali, SAR (S1) e' l'unica opzione affidabile."),
                ]),
            ]

            for tab_title, color, items in guide_data:
                tab = QWidget()
                tab_ly = QVBoxLayout(tab)
                tab_ly.setSpacing(4)

                for title_html, desc in items:
                    if not desc:
                        lbl = QLabel(title_html)
                        lbl.setStyleSheet("font-size:12px; padding:4px 0 0 0;")
                    else:
                        text = f"{title_html}<br><span style='color:#555;'>{desc}</span>"
                        lbl = QLabel(text)
                        lbl.setStyleSheet("font-size:11px; padding:2px 4px;")
                    lbl.setWordWrap(True)
                    tab_ly.addWidget(lbl)

                tab_ly.addStretch()

                scroll = QScrollArea()
                scroll.setWidget(tab)
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QScrollArea.Shape.NoFrame)
                tabs.addTab(scroll, tab_title)

            # General tips tab
            tips_tab = QWidget()
            tips_ly = QVBoxLayout(tips_tab)
            tips_text = QLabel(
                "<b style='font-size:13px;'>💡 Consigli generali</b><br><br>"
                "<b>1. Confronta SEMPRE PRE vs POST</b><br>"
                "Nessuna combinazione di bande identifica un disastro da sola. "
                "Serve alternare l'immagine PRIMA e DOPO l'evento per vedere il CAMBIO.<br><br>"
                "<b>2. Usa il colore naturale per orientarti</b><br>"
                "Il layer 'Colore naturale' (B04/B03/B02) e' come una foto. "
                "Usalo per capire dove sei, poi passa al falso colore per l'analisi.<br><br>"
                "<b>3. Attenzione alle nuvole!</b><br>"
                "Le nuvole appaiono BIANCHE in tutte le combinazioni e nascondono il suolo. "
                "Usa il filtro nuvole per selezionare immagini pulite. "
                "Se le nuvole coprono tutto, usa le immagini SAR (S1) che penetrano le nuvole.<br><br>"
                "<b>4. Lo stretch conta</b><br>"
                "Per confrontare due immagini, devono avere lo STESSO stretch (contrasto). "
                "Il plugin applica automaticamente stretch uniformi per facilitare il confronto.<br><br>"
                "<b>5. Risoluzione Sentinel-2</b><br>"
                "B02, B03, B04, B08 = 10 metri<br>"
                "B11, B12 = 20 metri (ricampionati a 10m nel composito)<br>"
                "Un pixel = 10m x 10m. Frane o incendi piccoli (&lt;100m) possono essere difficili da vedere.<br><br>"
                "<b>6. SAR (Sentinel-1)</b><br>"
                "Immagini radar. Non hanno colori come l'ottico. "
                "Mostrano la rugosita' del terreno in scala di grigio. "
                "Utili quando le nuvole impediscono la visione ottica."
            )
            tips_text.setWordWrap(True)
            tips_text.setStyleSheet("font-size:11px; padding:8px;")
            tips_ly.addWidget(tips_text)
            tips_ly.addStretch()

            tips_scroll = QScrollArea()
            tips_scroll.setWidget(tips_tab)
            tips_scroll.setWidgetResizable(True)
            tips_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            tabs.addTab(tips_scroll, "💡 Consigli")

            main_ly.addWidget(tabs)

            close_btn = QPushButton("Chiudi")
            close_btn.setStyleSheet(
                "QPushButton{padding:8px; font-weight:bold;"
                "background:#2980b9; color:white; border-radius:4px;}"
                "QPushButton:hover{background:#3498db;}"
            )
            close_btn.clicked.connect(dlg.accept)
            main_ly.addWidget(close_btn)

            dlg.show()
        except Exception as exc:
            logger.warning("Guide dialog error: %s", exc)

    def _show_edu_legend(self):
        """Show educational color legend for the current disaster type."""
        try:
            cfg = DISASTER_CONFIG.get(self._dtype, {})
            legend_text = cfg.get("edu_legend", "")
            label = cfg.get("label_it", "?")
            if not legend_text:
                return

            from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QFrame
            from qgis.PyQt.QtCore import Qt

            dlg = QDialog(self)
            dlg.setWindowTitle(f"Guida colori: {label}")
            dlg.setMinimumWidth(420)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            ly = QVBoxLayout(dlg)

            title = QLabel(f"<b>Come leggere l'immagine: {label}</b>")
            title.setStyleSheet("font-size:14px; padding:4px;")
            ly.addWidget(title)

            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            ly.addWidget(sep)

            body = QLabel(legend_text.replace("<br>", "<br>"))
            body.setWordWrap(True)
            body.setStyleSheet(
                "font-family: monospace; font-size:12px; "
                "padding:8px; background:#f8f8f0; border-radius:4px;"
            )
            ly.addWidget(body)

            tip = QLabel(
                "<i>Suggerimento: alterna la visibilita' dei layer "
                "PRE e POST nel pannello dei layer per vedere i cambiamenti.</i>"
            )
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#7f8c8d; font-size:11px; padding:4px;")
            ly.addWidget(tip)

            ok_btn = QPushButton("Ho capito!")
            ok_btn.setStyleSheet(
                "QPushButton{font-weight:bold; padding:8px; "
                "background:#27ae60; color:white; border-radius:4px;}"
                "QPushButton:hover{background:#2ecc71;}"
            )
            ok_btn.clicked.connect(dlg.accept)
            ly.addWidget(ok_btn)

            dlg.show()
        except Exception:
            pass

    def _on_dl_err(self, msg):
        try:
            self.progress.setVisible(False)
            self.cancel_dl_btn.setEnabled(False)
            self._set_status("Errore download", False)
            self.dl_status_label.setText(f"Errore: {msg}")
            self.iface.messageBar().pushCritical("CDE", msg)
        except RuntimeError:
            pass
