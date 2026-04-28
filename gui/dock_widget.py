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

try:
    from qgis.PyQt.QtCore import QMetaType
    _FIELD_TYPE_STRING = QMetaType.Type.QString
except (ImportError, AttributeError):
    from qgis.PyQt.QtCore import QVariant
    _FIELD_TYPE_STRING = QVariant.String
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
        self._visible_pre_products = []
        self._visible_post_products = []
        self._download_complete = False
        self._downloaded_images = []  # [{safe_dir, phase, date}]
        self._download_queue = []
        self._download_queue_index = 0
        self._current_task = None
        self._current_safe_dir = None
        self._build_ui()

    def _build_ui(self):
        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(2, 2, 2, 2)
        main_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Download banner (visible on all steps when download active/complete)
        self.dl_banner = QPushButton("")
        self.dl_banner.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-weight:bold;"
            "padding:10px;border-radius:4px;font-size:12px;}"
            "QPushButton:hover{background:#2ecc71;}")
        self.dl_banner.setVisible(False)
        self.dl_banner.clicked.connect(self._go_to_download_step)
        main_layout.addWidget(self.dl_banner)

        main_layout.addWidget(self.stack, 1)

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
        layout.setSpacing(4)
        layout.setContentsMargins(4, 4, 4, 4)


        # --- Welcome hint ---
        self.welcome_label = QLabel(
            "<span style='font-size:14px; font-weight:600; "
            "color:#2c3e50;'>"
            "Esplora i disastri italiani dallo spazio"
            "</span><br>"
            "<span style='font-size:11px; color:#5d6d7e;'>"
            "Scegli un disastro e cerca eventi \u2014 "
            "oppure attiva la <b>Ricerca libera</b></span>"
        )
        self.welcome_label.setWordWrap(True)
        self.welcome_label.setStyleSheet(
            "QLabel{padding:8px 12px; "
            "border-left:4px solid #2980b9; "
            "background:qlineargradient("
            "x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #eaf2f8, stop:1 rgba(234,242,248,0)); "
            "border-radius:0px;}"
        )
        layout.addWidget(self.welcome_label)
        layout.addSpacing(6)

        # Disaster type buttons
        dtype_group = QGroupBox("Tipo di disastro")
        dtype_layout = QHBoxLayout(dtype_group)
        dtype_layout.setContentsMargins(4, 2, 4, 2)
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
        layout.addSpacing(8)

        # --- Volcano hint + suggestion (compact, hidden by default) ---
        self.volcano_hint = QLabel(
            "<span style='font-size:10px; color:#e67e22;'>"
            "CEMS si attiva raramente per eruzioni (ultima: EMSR336, dic 2018). "
            "Usa la Ricerca libera per eruzioni recenti.</span>"
        )
        self.volcano_hint.setWordWrap(True)
        self.volcano_hint.setVisible(False)
        layout.addWidget(self.volcano_hint)

        self.volcano_suggest_btn = QPushButton("Carica: Eruzione Etna Feb 2025")
        self.volcano_suggest_btn.setStyleSheet(
            "QPushButton{font-size:10px; color:#c0392b; "
            "border:1px solid #c0392b; border-radius:3px; padding:3px 6px;"
            "background:#fdf2e9;}"
            "QPushButton:hover{background:#c0392b; color:white;}"
        )
        self.volcano_suggest_btn.setVisible(False)
        self.volcano_suggest_btn.clicked.connect(self._load_volcano_suggestion)
        layout.addWidget(self.volcano_suggest_btn)

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
        layout.addSpacing(6)

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
            "- Incendio: B12/B8A/B04 (cicatrice = rosso)<br>"
            "- Vulcano: B12/B11/B04 (lava = giallo)<br>"
            "- Frana: B12/B11/B04 (suolo esposto = rosa)"
        ))
        ft_row.addWidget(info_bands)
        free_layout.addLayout(ft_row)

        # Search button for free mode
        self.free_search_go = QPushButton("Cerca immagini →")
        self.free_search_go.setMinimumHeight(30)
        self.free_search_go.setStyleSheet("font-weight:bold; background:#8e44ad; color:white;")
        self.free_search_go.setEnabled(True)
        self.free_search_go.clicked.connect(self._on_free_search_go)
        free_layout.addWidget(self.free_search_go)

        layout.addWidget(self.free_group)

        # --- Event search widgets (hidden in free mode) ---
        self.event_search_widgets = QWidget()
        es_layout = QVBoxLayout(self.event_search_widgets)
        es_layout.setContentsMargins(0, 0, 0, 0)
        es_layout.setSpacing(3)

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
        es_layout.addSpacing(6)
        self.search_btn = QPushButton("CERCA EVENTI")
        self.search_btn.setMinimumHeight(32)
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
        self.events_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.events_table.setColumnCount(4)
        self.events_table.setHorizontalHeaderLabels(["Data", "Evento", "Dettagli", "Fonte"])
        self.events_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.events_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.events_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.events_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.events_table.selectionModel().selectionChanged.connect(self._on_event_selected)
        es_layout.addWidget(self.events_table)
        es_layout.addSpacing(6)

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
        es_layout.addSpacing(8)

        # Action button row 2: search images
        self.next_btn = QPushButton("Cerca immagini →")
        self.next_btn.setEnabled(False)
        self.next_btn.setMinimumHeight(30)
        self.next_btn.setStyleSheet(
            "QPushButton{font-weight:bold; background-color:#2980b9; color:white; "
            "border-radius:4px;}"
            "QPushButton:hover{background-color:#3498db;}"
            "QPushButton:disabled{background-color:#bdc3c7; color:#7f8c8d;}"
        )
        self.next_btn.clicked.connect(self._on_go_to_step2)
        es_layout.addWidget(self.next_btn)

        layout.addWidget(self.event_search_widgets)
        layout.addSpacing(8)

        # Bottom buttons row - always visible
        bottom_row = QHBoxLayout()

        self.guide_btn = QPushButton("Legenda colori")
        self.guide_btn.setStyleSheet(
            "QPushButton{font-size:11px; color:#2980b9; "
            "border:1px solid #2980b9; border-radius:4px; padding:5px 10px;}"
            "QPushButton:hover{background-color:#eaf2f8; color:#1a5276;}"
        )
        self.guide_btn.clicked.connect(self._show_interpretation_guide)
        bottom_row.addWidget(self.guide_btn)

        self.exercises_btn = QPushButton("Casi di prova")
        self.exercises_btn.setStyleSheet(
            "QPushButton{font-size:11px; color:#16a085; "
            "border:1px solid #16a085; border-radius:4px; padding:5px 10px;}"
            "QPushButton:hover{background-color:#e8f8f5; color:#0e6655;}"
        )
        self.exercises_btn.clicked.connect(self._show_exercises)
        bottom_row.addWidget(self.exercises_btn)

        self.usage_btn = QPushButton("Come usare")
        self.usage_btn.setStyleSheet(
            "QPushButton{font-size:11px; color:#8e44ad; "
            "border:1px solid #8e44ad; border-radius:4px; padding:5px 10px;}"
            "QPushButton:hover{background-color:#f4ecf7; color:#6c3483;}"
        )
        self.usage_btn.clicked.connect(self._show_usage_guide)
        bottom_row.addWidget(self.usage_btn)

        bottom_row.addStretch()

        self.reset_btn = QPushButton("🗑 Pulisci tutto")
        self.reset_btn.setStyleSheet(
            "QPushButton{font-size:11px; color:#7f8c8d; "
            "border:1px solid #bdc3c7; border-radius:4px; padding:5px 10px;}"
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
        layout.setSpacing(4)
        layout.setContentsMargins(4, 4, 4, 4)

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
        layout.addSpacing(6)

        # Pre-event images
        self.pre_label = QLabel("Pre-evento:")
        layout.addWidget(self.pre_label)
        self.pre_table = QTableWidget()
        self.pre_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.pre_table.setColumnCount(4)
        self.pre_table.setHorizontalHeaderLabels(["Data", "Sensore", "Nuvole", "Dimensione"])
        self.pre_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pre_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pre_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.pre_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pre_table.cellClicked.connect(
            lambda r, c: self._toggle_table_selection(self.pre_table, r))
        layout.addWidget(self.pre_table)
        layout.addSpacing(6)

        # Post-event images
        self.post_label = QLabel("Post-evento:")
        layout.addWidget(self.post_label)
        self.post_table = QTableWidget()
        self.post_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.post_table.setColumnCount(4)
        self.post_table.setHorizontalHeaderLabels(["Data", "Sensore", "Nuvole", "Dimensione"])
        self.post_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.post_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.post_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.post_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.post_table.cellClicked.connect(
            lambda r, c: self._toggle_table_selection(self.post_table, r))
        layout.addWidget(self.post_table)

        # Band info
        self.bands_info = QLabel("")
        self.bands_info.setStyleSheet("color:#2980b9; font-size:11px;")
        layout.addWidget(self.bands_info)
        layout.addSpacing(8)

        # Download dir
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("Cartella:"))
        self.download_dir = QgsFileWidget()
        self.download_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        dir_row.addWidget(self.download_dir)
        layout.addLayout(dir_row)
        layout.addSpacing(8)

        # Navigation
        nav_row = QHBoxLayout()
        back_btn = QPushButton("← Indietro")
        back_btn.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        nav_row.addWidget(back_btn)
        self.download_btn = QPushButton("Scarica e Visualizza")
        self.download_btn.setToolTip(
            "Seleziona una PRE e una POST, poi clicca per scaricare entrambe")
        self.download_btn.setMinimumHeight(32)
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
        scroll2 = QScrollArea()
        scroll2.setWidget(page)
        scroll2.setWidgetResizable(True)
        scroll2.setFrameShape(QScrollArea.Shape.NoFrame)
        self.stack.addWidget(scroll2)

    # ==============================================================
    # STEP 3: Download progress
    # ==============================================================
    def _build_step3(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(4)
        layout.setContentsMargins(4, 4, 4, 4)
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

        self._current_safe_dir = None

        # Navigation: back to image selection, or to search
        nav3_row = QHBoxLayout()

        back_to_images = QPushButton("← Seleziona altre immagini")
        back_to_images.setToolTip("Torna alla selezione immagini PRE/POST")
        back_to_images.setStyleSheet(
            "QPushButton{padding:6px 10px; font-weight:bold;"
            "border:1px solid #2980b9; border-radius:4px; color:#2980b9;}"
            "QPushButton:hover{background:#2980b9; color:white;}")
        back_to_images.clicked.connect(lambda: self._navigate_to_step(1))
        nav3_row.addWidget(back_to_images)

        back_to_search = QPushButton("← Nuova ricerca")
        back_to_search.setToolTip("Torna alla ricerca eventi")
        back_to_search.setStyleSheet(
            "QPushButton{padding:6px 10px;"
            "border:1px solid #7f8c8d; border-radius:4px; color:#7f8c8d;}"
            "QPushButton:hover{background:#7f8c8d; color:white;}")
        back_to_search.clicked.connect(lambda: self._navigate_to_step(0))
        nav3_row.addWidget(back_to_search)

        layout.addLayout(nav3_row)

        layout.addStretch()
        scroll3 = QScrollArea()
        scroll3.setWidget(page)
        scroll3.setWidgetResizable(True)
        scroll3.setFrameShape(QScrollArea.Shape.NoFrame)
        self.stack.addWidget(scroll3)

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
        """Apply red star with label showing event name."""
        from qgis.core import (
            QgsMarkerSymbol, QgsTextFormat, QgsTextBufferSettings,
            QgsPalLayerSettings, QgsVectorLayerSimpleLabeling,
        )
        from qgis.PyQt.QtGui import QFont, QColor

        # Symbol: red star
        symbol = QgsMarkerSymbol.createSimple({
            'name': 'star',
            'color': '#e74c3c',
            'size': '8',
            'outline_color': '#ffffff',
            'outline_width': '1',
        })
        layer.renderer().setSymbol(symbol)

        # Label: show "nome" field
        text_format = QgsTextFormat()
        text_format.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        text_format.setColor(QColor("#2c3e50"))
        text_format.setSize(10)

        buffer = QgsTextBufferSettings()
        buffer.setEnabled(True)
        buffer.setSize(1.5)
        buffer.setColor(QColor(255, 255, 255, 220))
        text_format.setBuffer(buffer)

        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = "nome"
        label_settings.setFormat(text_format)
        label_settings.placement = QgsPalLayerSettings.Placement.OrderedPositionsAroundPoint

        labeling = QgsVectorLayerSimpleLabeling(label_settings)
        layer.setLabelsEnabled(True)
        layer.setLabeling(labeling)
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
        """Show test cases catalog with real disaster events."""
        from .test_cases import show_test_cases
        show_test_cases(self)


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
        # banner managed by _go_to_download_step and _navigate_away

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
        self._visible_pre_products = []
        self._visible_post_products = []
        self._download_complete = False
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
        # Show/hide volcano educational hint
        is_volcano = (self._dtype == "volcano")
        self.volcano_hint.setVisible(is_volcano)
        self.volcano_suggest_btn.setVisible(is_volcano)
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
            if len(events) == 0 and self._dtype == "volcano":
                self.events_label.setText(
                    "Nessun evento vulcanico trovato nel catalogo CEMS. "
                    "Questo e' normale: il sistema CEMS viene attivato "
                    "solo per emergenze gravi (l'ultima per l'Etna fu "
                    "EMSR336, dicembre 2018). Per cercare eruzioni recenti, "
                    "attiva la Ricerca libera qui sotto: inserisci un "
                    "luogo (es. Etna) e le date dell'eruzione che ti interessa."
                )
                self.events_label.setStyleSheet("font-size:11px; color:#e67e22; font-style:italic;")
                self._set_status("Nessun evento CEMS — usa Ricerca libera per i vulcani", True)
            elif len(events) == 0:
                self.events_label.setText(f"Nessun evento di tipo '{label}' trovato nel periodo selezionato.")
                self.events_label.setStyleSheet("font-size:11px; color:#e74c3c;")
                self._set_status("Nessun evento trovato", False)
            else:
                self.events_label.setText(f"{len(events)} eventi di tipo '{label}' trovati")
                self.events_label.setStyleSheet("font-size:11px; color:#27ae60;")
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
        from qgis.core import QgsField
        layer = QgsVectorLayer("Point?crs=EPSG:4326", f"Evento: {ev.name[:25]}", "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("nome", _FIELD_TYPE_STRING),
            QgsField("data", _FIELD_TYPE_STRING),
            QgsField("tipo", _FIELD_TYPE_STRING),
        ])
        layer.updateFields()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        dtype_label = DISASTER_CONFIG.get(self._dtype, {}).get("label_it", "")
        feat.setAttribute("nome", ev.name[:40])
        feat.setAttribute("data", ev.date or "")
        feat.setAttribute("tipo", dtype_label)
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
            from qgis.core import QgsField
            layer = QgsVectorLayer(
                "Point?crs=EPSG:4326",
                f"Evento: {ev.name[:25]}", "memory"
            )
            pr = layer.dataProvider()
            pr.addAttributes([
                QgsField("nome", _FIELD_TYPE_STRING),
                QgsField("data", _FIELD_TYPE_STRING),
                QgsField("tipo", _FIELD_TYPE_STRING),
            ])
            layer.updateFields()
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
            dtype_label = DISASTER_CONFIG.get(self._dtype, {}).get("label_it", "")
            feat.setAttribute("nome", ev.name[:40])
            feat.setAttribute("data", ev.date or "")
            feat.setAttribute("tipo", dtype_label)
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
        """Populate pre/post tables filtering by cloud threshold."""
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
            self.post_table.setMaximumHeight(16777215)
            self.post_table.setVisible(True)
            vis = self._fill_table(self.post_table, all_products, threshold)
            self._visible_pre_products = []
            self._visible_post_products = vis
            self._set_status(
                f"{len(vis)}/{len(all_products)} immagini (filtro nuvole: max {threshold}%)",
                True)
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
            self.pre_table.setMaximumHeight(16777215)
            self.post_label.setText(f"Post-evento (dal {event_date}):")
            self.post_label.setVisible(True)
            self.post_table.setVisible(True)
            self.post_table.setMaximumHeight(16777215)
            vis_pre = self._fill_table(self.pre_table, pre, threshold)
            vis_post = self._fill_table(self.post_table, post, threshold)
            self._visible_pre_products = vis_pre
            self._visible_post_products = vis_post
            self._set_status(
                f"{len(vis_pre) + len(vis_post)} immagini "
                f"({len(vis_pre)} pre, {len(vis_post)} post) "
                f"| filtro nuvole: max {threshold}%",
                True)
        else:
            # Event search: use pre/post as returned
            self.pre_label.setText("Pre-evento:")
            self.pre_label.setVisible(True)
            self.pre_table.setVisible(True)
            self.pre_table.setMaximumHeight(16777215)
            self.post_label.setText("Post-evento:")
            self.post_label.setVisible(True)
            self.post_table.setVisible(True)
            self.post_table.setMaximumHeight(16777215)
            vis_pre = self._fill_table(self.pre_table, self._pre_products, threshold)
            vis_post = self._fill_table(self.post_table, self._post_products, threshold)
            self._visible_pre_products = vis_pre
            self._visible_post_products = vis_post
            self._set_status(
                f"{len(vis_pre) + len(vis_post)} immagini "
                f"({len(vis_pre)} pre, {len(vis_post)} post) "
                f"| filtro nuvole: max {threshold}%",
                True)


    def _passes_cloud(self, product, threshold):
        """Check if product passes the cloud filter."""
        if "S1" in product.name:
            return True  # SAR is always visible
        if product.cloud_cover is None or product.cloud_cover < 0:
            return True  # Unknown cloud, show it
        return product.cloud_cover <= threshold

    def _fill_table(self, table, products, threshold):
        """Fill a table widget with only products that pass the cloud filter."""
        from qgis.PyQt.QtGui import QColor, QBrush

        # Filter: only include products that pass the cloud threshold
        visible = [p for p in products if self._passes_cloud(p, threshold)]
        table.setRowCount(len(visible))

        for i, p in enumerate(visible):
            table.setItem(i, 0, QTableWidgetItem(p.sensing_date))
            table.setItem(i, 1, QTableWidgetItem(p.sensor_label))
            table.setItem(i, 2, QTableWidgetItem(p.cloud_display))
            table.setItem(i, 3, QTableWidgetItem(p.size_display))

            # Color by cloud cover
            is_sar = "S1" in p.name
            cloud = p.cloud_cover

            if is_sar:
                bg = QColor(200, 220, 255, 40)
            elif cloud is None or cloud < 0:
                bg = QColor(240, 240, 240, 40)
            elif cloud <= 20:
                bg = QColor(39, 174, 96, 50)
            elif cloud <= 50:
                bg = QColor(241, 196, 15, 50)
            else:
                bg = QColor(192, 57, 43, 50)

            brush = QBrush(bg)
            for col in range(4):
                item = table.item(i, col)
                if item:
                    item.setBackground(brush)

        return visible

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
        """Download selected images. Supports PRE+POST simultaneous selection."""
        vis_post = getattr(self, '_visible_post_products', self._post_products)
        vis_pre = getattr(self, '_visible_pre_products', self._pre_products)

        # Collect selected products from both tables
        download_queue = []

        pre_rows = self.pre_table.selectionModel().selectedRows()
        if pre_rows and pre_rows[0].row() < len(vis_pre):
            download_queue.append(("PRE", vis_pre[pre_rows[0].row()]))

        post_rows = self.post_table.selectionModel().selectedRows()
        if post_rows and post_rows[0].row() < len(vis_post):
            download_queue.append(("POST", vis_post[post_rows[0].row()]))

        # If nothing selected, auto-select first of each
        if not download_queue:
            if vis_pre:
                download_queue.append(("PRE", vis_pre[0]))
            if vis_post:
                download_queue.append(("POST", vis_post[0]))

        if not download_queue:
            self.iface.messageBar().pushWarning("CDE", "Nessuna immagine disponibile")
            return

        dl_dir = self.download_dir.filePath()
        if not dl_dir or not os.path.isdir(dl_dir):
            self.iface.messageBar().pushWarning("CDE", "Seleziona una cartella di download")
            return

        # Store the queue for sequential download
        self._download_queue = download_queue
        self._download_queue_index = 0

        # Start first download
        self._start_next_download()

    def _start_next_download(self):
        """Start the next download in the queue."""
        idx = self._download_queue_index
        queue = self._download_queue

        if idx >= len(queue):
            return  # All done

        phase, product = queue[idx]
        dl_dir = self.download_dir.filePath()
        ev = self._selected_event
        self._current_dl_phase = phase

        display_name = (
            f"{DISASTER_CONFIG.get(self._dtype,{}).get('label_it','')} "
            f"{ev.name[:20]} {product.sensing_date}"
        )

        total = len(queue)
        self.stack.setCurrentIndex(2)
        self.dl_status_label.setText(
            f"Download {idx+1}/{total}: <b>{phase}</b> - "
            f"{product.name[:50]}...<br>"
            f"Dimensione: {product.size_display}<br>"
            f"{'Poi scarichera\u0027 la seconda immagine automaticamente.' if total > 1 and idx == 0 else ''}"
        )
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._set_status(
            f"Download {phase} ({idx+1}/{total}): {product.size_display}...",
            True)
        self.cancel_dl_btn.setEnabled(True)

        task = DownloadAndLoadTask(
            product, dl_dir, self._dtype, display_name,
            self._on_dl_ok, self._on_dl_err,
        )
        self._current_task = task
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

    def _navigate_to_step(self, step_index):
        """Navigate between steps with proper banner management."""
        self.stack.setCurrentIndex(step_index)

        # Show/hide banner based on where we are
        if step_index == 2:
            # Going TO download step — hide banner
            self.dl_banner.setVisible(False)
        else:
            # Going AWAY from download — show banner if download exists
            if getattr(self, '_download_complete', False):
                self.dl_banner.setVisible(True)
                self.dl_banner.setText(
                    "\u2705 Immagine scaricata \u2014 "
                    "Clicca per tornare al download")
                self.dl_banner.setStyleSheet(
                    "QPushButton{background:#27ae60;color:white;font-weight:bold;"
                    "padding:10px;border-radius:4px;font-size:12px;}"
                    "QPushButton:hover{background:#2ecc71;}")
            elif self._current_task is not None:
                self.dl_banner.setVisible(True)
                pct = self.progress.value()
                self.dl_banner.setText(
                    f"\u2b07 Download in corso: {pct}% \u2014 clicca per vedere")
                self.dl_banner.setStyleSheet(
                    "QPushButton{background:#e67e22;color:white;font-weight:bold;"
                    "padding:10px;border-radius:4px;font-size:12px;}"
                    "QPushButton:hover{background:#f39c12;}")

    def _go_to_download_step(self):
        """Banner click handler: go to step 3."""
        self._navigate_to_step(2)

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

    def _on_dl_ok(self, zip_path, layers, safe_dir=None):
        try:
            self.progress.setVisible(False)
            self.cancel_dl_btn.setEnabled(False)
            self._current_task = None

            # Store safe_dir for reference
            if safe_dir:
                self._current_safe_dir = safe_dir
                phase = getattr(self, '_current_dl_phase', 'POST')
                sensing_date = ""
                try:
                    parts = os.path.basename(safe_dir).split('_')
                    if len(parts) > 2:
                        sensing_date = parts[2][:8]
                except Exception:
                    pass
                self._downloaded_images.append({
                    "safe_dir": safe_dir,
                    "phase": phase,
                    "date": sensing_date,
                })

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
                self._download_complete = True
                if self.stack.currentIndex() != 2:
                    self.dl_banner.setVisible(True)
                self.dl_banner.setText(
                    f"\u2705 {len(layers)} bande caricate \u2014 "
                    f"Clicca per tornare al download")
                self.dl_banner.setStyleSheet(
                    "QPushButton{background:#27ae60;color:white;font-weight:bold;"
                    "padding:10px;border-radius:4px;font-size:12px;}"
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

                # Continue with next download in queue
                queue = getattr(self, '_download_queue', [])
                queue_idx = getattr(self, '_download_queue_index', 0) + 1
                self._download_queue_index = queue_idx
                if queue_idx < len(queue):
                    total = len(queue)
                    QTimer.singleShot(1000, self._start_next_download)
                    self._set_status(
                        f"Download {queue_idx}/{total} completato! "
                        f"Avvio download {queue_idx+1}/{total}...",
                        True)
            else:
                self._set_status("Download OK, bande non trovate", False)
                self.dl_status_label.setText(
                    "Download completato ma nessuna banda trovata.<br>"
                    "Il prodotto potrebbe avere un formato non supportato."
                )
        except RuntimeError:
            pass


    def _show_usage_guide(self):
        """Show usage guide dialog."""
        try:
            from qgis.PyQt.QtWidgets import (
                QDialog, QVBoxLayout, QLabel, QPushButton,
                QTabWidget, QWidget, QScrollArea,
            )
            from qgis.PyQt.QtCore import Qt

            dlg = QDialog(self)
            dlg.setWindowTitle("Come usare il plugin")
            dlg.setMinimumWidth(700)
            dlg.setMinimumHeight(520)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            ly = QVBoxLayout(dlg)

            tabs = QTabWidget()

            def make_tab(html, has_links=False):
                page = QWidget()
                page_ly = QVBoxLayout(page)
                page_ly.setContentsMargins(14, 14, 14, 14)
                lbl = QLabel(html)
                lbl.setWordWrap(True)
                lbl.setStyleSheet(
                    "font-size:13px; line-height:1.5; "
                    "a{color:#2980b9;}")
                if has_links:
                    lbl.setOpenExternalLinks(True)
                page_ly.addWidget(lbl)
                page_ly.addStretch()
                scroll = QScrollArea()
                scroll.setWidget(page)
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QScrollArea.Shape.NoFrame)
                return scroll

            # --- TAB 1: Prima di iniziare + Cercare ---
            tabs.addTab(make_tab(
                "<h3 style='color:#2c3e50;'>Prima di iniziare</h3>"

                "<p style='background:#fff3cd; padding:8px; border-radius:4px; "
                "border-left:4px solid #f39c12;'>"
                "Per scaricare le immagini Sentinel serve un account "
                "<b>Copernicus Data Space</b> (gratuito).<br>"
                "Se non lo hai, registrati su "
                "<a href='https://dataspace.copernicus.eu'>dataspace.copernicus.eu</a><br>"
                "Poi clicca <b>Impostazioni</b> (nel menu Raster) e inserisci "
                "le tue credenziali.</p>"

                "<h3 style='color:#27ae60;'>Cercare un evento</h3>"
                "<p>Hai due opzioni:</p>"

                "<p><b>Opzione A \u2014 Dal catalogo (non conosci l'evento):</b></p>"
                "<ol>"
                "<li>Scegli il tipo di disastro: "
                "<b>Alluvione</b>, <b>Incendio</b>, <b>Vulcano</b> o <b>Frana</b></li>"
                "<li>Seleziona una <b>regione</b> (o Tutta Italia) "
                "e il <b>periodo</b> di ricerca</li>"
                "<li>Clicca il pulsante verde <b>CERCA EVENTI</b></li>"
                "<li>Nella tabella appariranno gli eventi confermati dal "
                "sistema europeo CEMS e EFFIS</li>"
                "<li>Seleziona un evento dalla tabella</li>"
                "<li>Clicca <b>Mostra sulla mappa</b> per vederlo "
                "(puoi correggere la posizione se necessario)</li>"
                "<li>Clicca <b>Cerca immagini</b> per passare alla "
                "selezione delle immagini satellitari</li>"
                "</ol>"

                "<p><b>Opzione B \u2014 Ricerca libera (conosci gia' l'evento):</b></p>"
                "<ol>"
                "<li>Clicca il pulsante viola <b>Ricerca libera</b></li>"
                "<li>Scrivi il nome del luogo (es. 'Faenza', 'Etna') "
                "e selezionalo dal menu a tendina</li>"
                "<li>Se conosci la data del disastro, attiva "
                "<b>Separa PRE/POST</b> e inserisci la data. "
                "Cosi' le immagini verranno divise in "
                "PRIMA e DOPO il disastro</li>"
                "<li>Imposta le date di ricerca "
                "(consiglio: 30-60 giorni centrati sull'evento)</li>"
                "<li>Clicca <b>Cerca immagini</b></li>"
                "</ol>"

                "<p style='background:#eaf6ff; padding:8px; border-radius:4px; "
                "border-left:4px solid #2980b9;'>"
                "<b>Suggerimento:</b> Usa il pulsante <b>Casi di prova</b> "
                "(in basso) per provare con 8 eventi reali gia' configurati. "
                "Basta un clic e la ricerca parte automaticamente.</p>"

                "<p style='background:#fdf2e9; padding:8px; border-radius:4px; "
                "border-left:4px solid #e67e22;'>"
                "<b>Vulcani:</b> Il catalogo CEMS ha pochissimi eventi vulcanici "
                "(l'ultimo fu l'Etna nel 2018). Quando selezioni Vulcano, "
                "appare un suggerimento per l'eruzione dell'Etna Feb 2025 "
                "che puoi caricare direttamente nella Ricerca libera.</p>"
            , has_links=True), "Cercare")

            # --- TAB 2: Selezionare e scaricare ---
            tabs.addTab(make_tab(
                "<h3 style='color:#2980b9;'>Selezionare e scaricare immagini</h3>"

                "<p>Dopo aver cliccato <b>Cerca immagini</b>, vedrai due tabelle:</p>"
                "<ul>"
                "<li><b>Pre-evento:</b> immagini scattate PRIMA del disastro</li>"
                "<li><b>Post-evento:</b> immagini scattate DOPO il disastro</li>"
                "</ul>"
                "<p>Per confrontare i danni, dovrai scaricare almeno "
                "un'immagine PRE e una POST.</p>"

                "<h4 style='color:#f39c12;'>Filtro nuvole</h4>"
                "<p>Le nuvole nascondono il suolo e rovinano l'analisi. "
                "In alto trovi un filtro per mostrare solo le immagini "
                "con poche nuvole:</p>"
                "<ul>"
                "<li><b>Max 20%</b> \u2014 quasi limpide (la scelta migliore)</li>"
                "<li><b>Max 50%</b> \u2014 parzialmente nuvolose</li>"
                "<li><b>Mostra tutte</b> \u2014 nessun filtro</li>"
                "</ul>"
                "<p>Le righe nella tabella sono colorate:<br>"
                "<span style='color:#27ae60;'>\u25a0 Verde</span> = poche nuvole \u2014 "
                "<span style='color:#f1c40f;'>\u25a0 Giallo</span> = nuvolose \u2014 "
                "<span style='color:#3498db;'>\u25a0 Blu</span> = SAR (radar, zero nuvole!)</p>"

                "<h4 style='color:#27ae60;'>Scaricare</h4>"
                "<ol>"
                "<li>Seleziona la <b>cartella di download</b> dove salvare i file</li>"
                "<li>Seleziona un'immagine dalla tabella "
                "(consiglio: inizia con una POST con poche nuvole)</li>"
                "<li>Clicca <b>Scarica e Visualizza</b> \u2014 "
"se hai selezionato entrambe, il plugin scarica prima la PRE "
"e poi la POST automaticamente</li>"
                "<li>Attendi il download (puoi continuare a lavorare, "
                "il download prosegue in background)</li>"
                "<li>L'immagine viene caricata automaticamente in QGIS "
                "con la simbologia falso colore appropriata</li>"
                "</ol>"

                "<p style='background:#e8f8f0; padding:8px; border-radius:4px; "
                "border-left:4px solid #27ae60;'>"
                "<b>Combinazione automatica:</b> Il plugin carica "
                "automaticamente un singolo layer con la combinazione "
                "di bande ottimale per il tipo di disastro: "
                "B12/B08/B03 per alluvioni, B12/B8A/B04 per incendi, "
                "B12/B11/B04 per vulcani e frane.</p>"
), "Scaricare")

            # --- TAB 3: Confrontare ---
            tabs.addTab(make_tab(
                "<h3 style='color:#e67e22;'>Confrontare prima e dopo</h3>"

                "<p style='background:#fdedec; padding:8px; border-radius:4px; "
                "border-left:4px solid #e74c3c;'>"
                "<b>Importante:</b> Un'immagine singola non basta per identificare "
                "un disastro. Serve SEMPRE confrontare un'immagine di <b>prima</b> "
                "e una di <b>dopo</b> l'evento.</p>"

                "<ol>"
                "<li>Torna alla selezione immagini "
                "(pulsante \u2190 Continua a esplorare)</li>"
                "<li>Scarica un'altra immagine (se hai scaricato una POST, "
                "ora scarica una PRE)</li>"
                "<li>Nel <b>pannello dei layer</b> di QGIS (a sinistra), "
                "alterna la visibilita' dei layer PRE e POST usando "
                "la checkbox accanto a ciascuno per confrontare "
                "il prima e il dopo</li>"
                "</ol>"

                "<h4>Cosa cercare per tipo di disastro</h4>"
                "<table cellpadding='6' cellspacing='0' "
                "style='border-collapse:collapse; width:100%;'>"
                "<tr style='background:#eaf6ff;'>"
                "<td style='border:1px solid #ddd;'><b>Alluvione</b></td>"
                "<td style='border:1px solid #ddd;'>Zone verdi che diventano "
                "<b>NERE</b> (acqua). Usa SWIR.</td></tr>"
                "<tr>"
                "<td style='border:1px solid #ddd;'><b>Incendio</b></td>"
                "<td style='border:1px solid #ddd;'>Zone verdi che diventano "
                "<b>MARRONI/NERE</b> (cicatrice). Usa SWIR o CIR.</td></tr>"
                "<tr style='background:#eaf6ff;'>"
                "<td style='border:1px solid #ddd;'><b>Frana</b></td>"
                "<td style='border:1px solid #ddd;'>Zone verdi che diventano "
                "<b>ROSA/ARANCIONE</b> (suolo esposto). Usa Geologia.</td></tr>"
                "<tr>"
                "<td style='border:1px solid #ddd;'><b>Vulcano</b></td>"
                "<td style='border:1px solid #ddd;'>Nuove zone "
                "<b>BIANCHE brillanti</b> in SWIR (emissione termica della lava)."
                "</td></tr>"
                "</table>"

                "<p style='margin-top:12px; background:#e8f8f5; "
                "padding:8px; border-radius:4px; "
                "border-left:4px solid #1abc9c;'>"
                "<b>Se le nuvole coprono tutto:</b> Usa le immagini "
                "<b>SAR</b> (Sentinel-1, righe blu nella tabella). "
                "Il radar penetra le nuvole! Particolarmente utile "
                "in inverno (nov-feb) quando l'Italia e' spesso coperta.</p>"
            ), "Confrontare")

            ly.addWidget(tabs)

            btn = QPushButton("Chiudi")
            btn.setStyleSheet(
                "QPushButton{padding:8px; font-weight:bold;"
                "background:#8e44ad; color:white; border-radius:4px;}"
                "QPushButton:hover{background:#9b59b6;}")
            btn.clicked.connect(dlg.accept)
            ly.addWidget(btn)
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
                    ("<b>Cosa cercare:</b>", ""),
                    ("<span style='color:#111133'>■ NERO / BLU SCURO</span>", "= Acqua. L'acqua assorbe fortemente SWIR e NIR, apparendo molto scura."),
                    ("<span style='color:#33cc33'>■ VERDE BRILLANTE</span>", "= Vegetazione sana. Alta riflessione NIR."),
                    ("<span style='color:#cc8866'>■ MARRONE / ROSA</span>", "= Suolo nudo, aree urbane."),
                    ("<span style='color:#ffffff; background:#666'>■ BIANCO</span>", "= Nuvole."),
                    ("<b>Come identificare l'alluvione:</b>", "Confronta PRE vs POST. Le zone allagate appaiono NERE nel POST dove prima c'era vegetazione (VERDE) o terreno. Cerca aree scure lungo fiumi e pianure."),
                    ("<b>Perche' questa combinazione?</b>", "La banda SWIR2 (B12) e' fortemente assorbita dall'acqua, creando il massimo contrasto tra terra e acqua. La NIR (B08) distingue la vegetazione. La banda Green (B03) e' la base dell'indice NDWI per la rilevazione dell'acqua."),
                ]),
                ("🔥 Incendio", "#e67e22", [
                    ("<b>Combinazione principale: SWIR2/NIR narrow/Red</b>", "R=B12 (2190nm) | G=B8A (865nm) | B=B04 (665nm)"),
                    ("<b>Cosa cercare:</b>", ""),
                    ("<span style='color:#cc4444'>■ ROSSO / MARRONE SCURO</span>", "= Cicatrice di incendio. Alta SWIR (suolo caldo/cenere), bassa NIR (vegetazione bruciata)."),
                    ("<span style='color:#33cc33'>■ VERDE BRILLANTE</span>", "= Vegetazione sana non bruciata."),
                    ("<span style='color:#cc66aa'>■ ROSA / MAGENTA</span>", "= Suolo nudo."),
                    ("<span style='color:#ff8800'>■ ARANCIONE / GIALLO</span>", "= Incendio ATTIVO (la fiamma emette in SWIR)."),
                    ("<b>Come identificare l'incendio:</b>", "La cicatrice appare come area ROSSO/MARRONE SCURO con bordo irregolare. Confronta PRE (verde) vs POST (rosso) per delimitare l'area bruciata."),
                    ("<b>Indice utile: NBR</b>", "NBR = (B8A-B12)/(B8A+B12). Valori bassi o negativi = area bruciata. B8A (NIR narrow) e' lo standard per il calcolo del NBR."),
                ]),
                ("🌋 Vulcano", "#c0392b", [
                    ("<b>Combinazione principale: SWIR2/SWIR1/Red</b>", "R=B12 (2190nm) | G=B11 (1610nm) | B=B04 (665nm)"),
                    ("<b>Cosa cercare:</b>", ""),
                    ("<span style='color:#ffcc00'>■ GIALLO / BIANCO</span>", "= Lava calda o attivita' termale. La lava emette RADIAZIONE INFRAROSSA a onde corte."),
                    ("<span style='color:#555555'>■ GRIGIO SCURO</span>", "= Cenere vulcanica o colate di lava raffreddata."),
                    ("<span style='color:#33cc33'>■ VERDE</span>", "= Vegetazione."),
                    ("<span style='color:#8855cc'>■ VIOLA / BLU</span>", "= Gas vulcanici (SO2)."),
                    ("<b>Perche' il doppio SWIR?</b>", "B12 + B11 sono entrambe sensibili al calore. Questa combinazione e' l'unica che puo' 'vedere' la temperatura: lava attiva appare GIALLA/BIANCA, colate fredde GRIGIO SCURO."),
                ]),
                ("⛰ Frana", "#795548", [
                    ("<b>Combinazione principale: SWIR2/SWIR1/Red</b>", "R=B12 (2190nm) | G=B11 (1610nm) | B=B04 (665nm)"),
                    ("<b>Cosa cercare:</b>", ""),
                    ("<span style='color:#cc66aa'>■ ROSA / MAGENTA</span>", "= Suolo esposto, corpo di frana (alta SWIR = terreno senza vegetazione)."),
                    ("<span style='color:#33cc33'>■ VERDE BRILLANTE</span>", "= Vegetazione sana."),
                    ("<span style='color:#886644'>■ MARRONE</span>", "= Suolo secco, argilla."),
                    ("<span style='color:#cc9933'>■ ARANCIONE</span>", "= Argille plioceniche, suolo saturo d'acqua."),
                    ("<b>Come identificare la frana:</b>", "1) Confronta PRE vs POST: verde che diventa rosa/marrone.\n2) Cerca forma irregolare, lobata (non rettangolare come un campo).\n3) Su versante, non in pianura.\n4) Cerca la SCARPATA: bordo netto in alto (corona) con materiale accumulato in basso."),
                    ("<b>Perche' il doppio SWIR?</b>", "B12 + B11 distinguono i tipi di suolo: argille, roccia, suolo saturo hanno firme spettrali diverse nel SWIR. Questo aiuta a capire PERCHE' e' avvenuta la frana."),
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
                "<b>2. Attenzione alle nuvole!</b><br>"
                "Le nuvole appaiono BIANCHE in tutte le combinazioni e nascondono il suolo. "
                "Usa il filtro nuvole per selezionare immagini pulite. "
                "Se le nuvole coprono tutto, usa le immagini SAR (S1) che penetrano le nuvole.<br><br>"
                "<b>3. Lo stretch conta</b><br>"
                "Per confrontare due immagini, devono avere lo STESSO stretch (contrasto). "
                "Il plugin applica automaticamente stretch uniformi per facilitare il confronto.<br><br>"
                "<b>4. Risoluzione Sentinel-2</b><br>"
                "B02, B03, B04, B08 = 10 metri<br>"
                "B11, B12 = 20 metri (ricampionati a 10m nel composito)<br>"
                "Un pixel = 10m x 10m. Frane o incendi piccoli (&lt;100m) possono essere difficili da vedere.<br><br>"
                "<b>5. SAR (Sentinel-1)</b><br>"
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

    def _load_volcano_suggestion(self):
        """Load pre-configured Etna eruption search into free search."""
        # Activate free search mode
        if not self.free_search_btn.isChecked():
            self.free_search_btn.setChecked(True)
            self._on_free_search_toggle(True)

        # Pre-fill with Etna eruption data
        self.free_place.setText("Etna, Sicilia")
        self.free_lat.setText("37.7510")
        self.free_lon.setText("14.9930")
        self.free_loc_label.setText("\u2705 Etna: 37.7510, 14.9930")
        self.free_loc_label.setStyleSheet("font-size:11px; color:#27ae60; font-weight:bold;")
        self.free_date_from.setDate(QDate(2025, 1, 20))
        self.free_date_to.setDate(QDate(2025, 2, 28))

        # Set type to Vulcano
        for i in range(self.free_type_combo.count()):
            if self.free_type_combo.itemText(i) == "Vulcano":
                self.free_type_combo.setCurrentIndex(i)
                break

        # Enable event date split
        self.free_event_check.setChecked(True)
        self._on_event_date_toggle(True)
        self.free_event_date.setDate(QDate(2025, 2, 8))

        self.free_search_go.setEnabled(True)
        self._set_status(
            "Suggerimento caricato: Eruzione Etna 8 Feb 2025. "
            "Clicca 'Cerca immagini' per procedere.", True
        )

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
