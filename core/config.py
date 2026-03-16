"""Plugin configuration: disaster types, band mappings, API endpoints, and UI constants."""
import os

PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
ICONS_DIR = os.path.join(PLUGIN_DIR, "resources", "icons")

# --- Copernicus Data Space ---
CDSE_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CDSE_CATALOG_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
CDSE_DOWNLOAD_URL = "https://zipper.dataspace.copernicus.eu/odata/v1/Products"
CDSE_CLIENT_ID = "cdse-public"
CDSE_REGISTER_URL = "https://dataspace.copernicus.eu"

# --- Event source APIs ---
CEMS_API_URL = "https://mapping.emergency.copernicus.eu/activations/api/activations/"
EFFIS_WFS_URL = "https://maps.wild-fire.eu/effis"

# --- Timeouts ---
API_TIMEOUT_MS = 30000
DOWNLOAD_TIMEOUT_MS = 3600000
EFFIS_TIMEOUT_MS = 10000

# --- Token management ---
TOKEN_REFRESH_MARGIN_S = 120
REFRESH_TOKEN_LIFETIME_S = 3600
SETTINGS_PREFIX = "CopernicusDisasterExplorer"

# --- Download ---
DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB chunks


class DisasterType:
    FLOOD = "flood"
    FIRE = "fire"
    VOLCANO = "volcano"
    LANDSLIDE = "landslide"


# Each disaster type: event source, sensor, product type, bands to load,
# and false-color band assignment for symbology.
# sensor_secondary / product_filter_secondary enable dual-sensor search.
DISASTER_CONFIG = {
    DisasterType.FLOOD: {
        "label_it": "Alluvione",
        "icon": "flood.png",
        "event_source": "cems",
        "sensor": "SENTINEL-1",
        "product_filter": "GRD",
        "cloud_relevant": False,
        "bands_s1": ["vv", "vh"],
        "bands_s2": ["B03", "B08", "B12"],
        "false_color_s2": {"R": "B12", "G": "B08", "B": "B03"},
        "search_days_before": 5,
        "search_days_after": 12,
        "sensor_secondary": "SENTINEL-2",
        "product_filter_secondary": "MSIL2A",
        "max_cloud_secondary": 30,
        "extra_band_sets": [
            {"bands": ["B04", "B03", "B02"], "fc": {"R": "B04", "G": "B03", "B": "B02"}, "label": "Colore naturale"},
        ],
        "edu_legend": (
            "COMBINAZIONE SWIR2/NIR/Green (B12/B08/B03)\n\n"
            "NERO / BLU SCURO = Acqua (assorbe SWIR e NIR)\n"
            "VERDE BRILLANTE = Vegetazione sana (alta NIR)\n"
            "MARRONE/ROSA = Suolo nudo o urbano\n"
            "BIANCO = Nuvole\n\n"
            "COME LEGGERE: Le zone allagate appaiono NERE\n"
            "perche' l'acqua assorbe la radiazione infrarossa.\n"
            "Confronta PRE e POST per vedere dove e' apparsa l'acqua."
        ),
    },
    DisasterType.FIRE: {
        "label_it": "Incendio",
        "icon": "fire.png",
        "event_source": "effis",
        "sensor": "SENTINEL-2",
        "product_filter": "MSIL2A",
        "cloud_relevant": True,
        "bands_s1": ["vv", "vh"],
        "bands_s2": ["B04", "B08", "B12"],
        "false_color": {"R": "B12", "G": "B08", "B": "B04"},
        "search_days_before": 15,
        "search_days_after": 15,
        "sensor_secondary": "SENTINEL-1",
        "product_filter_secondary": "GRD",
        "max_cloud_secondary": 100,
        "extra_band_sets": [
            {"bands": ["B04", "B03", "B02"], "fc": {"R": "B04", "G": "B03", "B": "B02"}, "label": "Colore naturale"},
        ],
        "edu_legend": (
            "COMBINAZIONE SWIR2/NIR/Red (B12/B08/B04)\n\n"
            "ROSSO/MARRONE = Cicatrice di incendio (alta SWIR, bassa NIR)\n"
            "VERDE BRILLANTE = Vegetazione sana (alta NIR)\n"
            "ROSA/MAGENTA = Suolo nudo\n"
            "NERO = Acqua o cenere bagnata\n\n"
            "COME LEGGERE: La cicatrice di incendio appare\n"
            "ROSSA/MARRONE perche' il suolo bruciato\n"
            "riflette molto in SWIR ma ha perso la vegetazione (NIR bassa)."
        ),
    },
DisasterType.VOLCANO: {
        "label_it": "Vulcano",
        "icon": "volcano.png",
        "event_source": "cems",
        "sensor": "SENTINEL-2",
        "product_filter": "MSIL2A",
        "cloud_relevant": True,
        "bands_s1": ["vv", "vh"],
        "bands_s2": ["B04", "B11", "B12"],
        "false_color": {"R": "B12", "G": "B11", "B": "B04"},
        "search_days_before": 10,
        "search_days_after": 15,
        "sensor_secondary": "SENTINEL-1",
        "product_filter_secondary": "GRD",
        "max_cloud_secondary": 100,
        "extra_band_sets": [
            {"bands": ["B04", "B03", "B02"], "fc": {"R": "B04", "G": "B03", "B": "B02"}, "label": "Colore naturale"},
        ],
        "edu_legend": (
            "COMBINAZIONE SWIR2/SWIR1/Red (B12/B11/B04)\n\n"
            "GIALLO/BIANCO = Lava calda o attivita' termale\n"
            "  (la lava emette radiazione infrarossa a onde corte)\n"
            "GRIGIO SCURO = Cenere vulcanica o lava raffreddata\n"
            "VERDE = Vegetazione\n"
            "VIOLA/BLU = Gas vulcanici (SO2)\n\n"
            "COME LEGGERE: Il doppio SWIR (B12+B11) e'\n"
            "sensibile al calore: la lava attiva appare\n"
            "GIALLA/BIANCA, le colate fredde GRIGIO SCURO."
        ),
    },
    DisasterType.LANDSLIDE: {
        "label_it": "Frana",
        "icon": "landslide.png",
        "event_source": "cems",
        "sensor": "SENTINEL-1",
        "product_filter": "GRD",
        "cloud_relevant": False,
        "bands_s1": ["vv", "vh"],
        "bands_s2": ["B04", "B08", "B12"],
        "false_color_s2": {"R": "B12", "G": "B08", "B": "B04"},
        "search_days_before": 5,
        "search_days_after": 15,
        "sensor_secondary": "SENTINEL-2",
        "product_filter_secondary": "MSIL2A",
        "max_cloud_secondary": 30,
        "extra_band_sets": [
            {"bands": ["B04", "B03", "B02"], "fc": {"R": "B04", "G": "B03", "B": "B02"}, "label": "Colore naturale"},
            {"bands": ["B04", "B11", "B12"], "fc": {"R": "B12", "G": "B11", "B": "B04"}, "label": "Geologia"},
        ],
        "edu_legend": (
            "COMBINAZIONE SWIR2/NIR/Red (B12/B08/B04)\n\n"
            "ROSA/MAGENTA = Suolo esposto, corpo di frana\n"
            "  (alta SWIR, bassa NIR = terreno senza vegetazione)\n"
            "VERDE BRILLANTE = Vegetazione sana\n"
            "MARRONE = Suolo secco, argilla\n"
            "NERO = Acqua o ombre\n\n"
            "COME LEGGERE: La frana appare come zona ROSA\n"
            "con forma irregolare su versante, dove prima\n"
            "c'era vegetazione (VERDE). Confronta PRE vs POST.\n"
            "Cerca: corona in alto, accumulo in basso."
        ),
    },
}

# Reverse mapping: DisasterType -> list of CEMS category slugs
DTYPE_TO_CEMS_SLUGS = {
    DisasterType.FLOOD: ["flood"],
    DisasterType.FIRE: ["fire"],
    DisasterType.VOLCANO: ["volcan", "volcanic-activity", "volcano"],
    DisasterType.LANDSLIDE: ["mass-movement", "mass", "landslide"],
}

ITALIAN_REGIONS = {
    "Tutta Italia": (6.62, 36.62, 18.52, 47.09),
    "Abruzzo": (13.02, 41.68, 14.79, 42.90),
    "Basilicata": (15.33, 39.89, 16.87, 41.14),
    "Calabria": (15.63, 37.91, 17.13, 39.95),
    "Campania": (13.76, 39.99, 15.81, 41.51),
    "Emilia-Romagna": (9.20, 43.73, 12.76, 45.14),
    "Friuli Venezia Giulia": (12.32, 45.58, 13.92, 46.65),
    "Lazio": (11.45, 41.19, 14.03, 42.84),
    "Liguria": (7.49, 43.78, 10.07, 44.68),
    "Lombardia": (8.50, 44.68, 11.43, 46.64),
    "Marche": (12.09, 42.69, 13.92, 43.97),
    "Molise": (13.94, 41.36, 15.16, 41.91),
    "Piemonte": (6.63, 44.06, 9.21, 46.46),
    "Puglia": (14.93, 39.78, 18.52, 42.23),
    "Sardegna": (8.13, 38.82, 9.83, 41.32),
    "Sicilia": (12.37, 36.64, 15.65, 38.82),
    "Toscana": (9.69, 42.24, 12.37, 44.47),
    "Trentino-Alto Adige": (10.38, 45.67, 12.48, 47.09),
    "Umbria": (12.09, 42.37, 13.26, 43.44),
    "Valle d'Aosta": (6.80, 45.47, 7.94, 45.99),
    "Veneto": (10.62, 44.79, 13.10, 46.68),
}

# --- Italian places for autocomplete (capoluoghi + disaster areas) ---
ITALIAN_PLACES = [
    ("Roma", "Lazio", 41.9028, 12.4964),
    ("Milano", "Lombardia", 45.4642, 9.1900),
    ("Napoli", "Campania", 40.8518, 14.2681),
    ("Torino", "Piemonte", 45.0703, 7.6869),
    ("Palermo", "Sicilia", 38.1157, 13.3615),
    ("Genova", "Liguria", 44.4056, 8.9463),
    ("Bologna", "Emilia-Romagna", 44.4949, 11.3426),
    ("Firenze", "Toscana", 43.7696, 11.2558),
    ("Catania", "Sicilia", 37.5079, 15.0830),
    ("Bari", "Puglia", 41.1171, 16.8719),
    ("Venezia", "Veneto", 45.4408, 12.3155),
    ("Verona", "Veneto", 45.4384, 10.9916),
    ("Messina", "Sicilia", 38.1938, 15.5540),
    ("Padova", "Veneto", 45.4064, 11.8768),
    ("Trieste", "Friuli VG", 45.6495, 13.7768),
    ("Brescia", "Lombardia", 45.5416, 10.2118),
    ("Taranto", "Puglia", 40.4644, 17.2470),
    ("Reggio Calabria", "Calabria", 38.1113, 15.6473),
    ("Cagliari", "Sardegna", 39.2238, 9.1217),
    ("Perugia", "Umbria", 43.1107, 12.3908),
    ("Trento", "Trentino-AA", 46.0748, 11.1217),
    ("Bolzano", "Trentino-AA", 46.4983, 11.3548),
    ("Ancona", "Marche", 43.6158, 13.5189),
    ("L'Aquila", "Abruzzo", 42.3498, 13.3995),
    ("Potenza", "Basilicata", 40.6404, 15.8056),
    ("Catanzaro", "Calabria", 38.9098, 16.5877),
    ("Campobasso", "Molise", 41.5604, 14.6628),
    ("Aosta", "Valle d'Aosta", 45.7375, 7.3154),
    ("Bergamo", "Lombardia", 45.6983, 9.6773),
    ("Como", "Lombardia", 45.8081, 9.0852),
    ("Cremona", "Lombardia", 45.1333, 10.0243),
    ("Lecco", "Lombardia", 45.8566, 9.3977),
    ("Lodi", "Lombardia", 45.3096, 9.5033),
    ("Mantova", "Lombardia", 45.1564, 10.7914),
    ("Monza", "Lombardia", 45.5845, 9.2745),
    ("Pavia", "Lombardia", 45.1847, 9.1582),
    ("Sondrio", "Lombardia", 46.1699, 9.8727),
    ("Varese", "Lombardia", 45.8206, 8.8257),
    ("Alessandria", "Piemonte", 44.9122, 8.6154),
    ("Asti", "Piemonte", 44.9001, 8.2065),
    ("Cuneo", "Piemonte", 44.3904, 7.5492),
    ("Novara", "Piemonte", 45.4464, 8.6220),
    ("Belluno", "Veneto", 46.1426, 12.2167),
    ("Rovigo", "Veneto", 45.0700, 11.7897),
    ("Treviso", "Veneto", 45.6669, 12.2430),
    ("Vicenza", "Veneto", 45.5475, 11.5464),
    ("Gorizia", "Friuli VG", 45.9415, 13.6225),
    ("Pordenone", "Friuli VG", 45.9564, 12.6602),
    ("Udine", "Friuli VG", 46.0711, 13.2346),
    ("Imperia", "Liguria", 43.8857, 8.0386),
    ("La Spezia", "Liguria", 44.1025, 9.8240),
    ("Savona", "Liguria", 44.3091, 8.4772),
    ("Ferrara", "Emilia-Romagna", 44.8381, 11.6199),
    ("Modena", "Emilia-Romagna", 44.6471, 10.9252),
    ("Parma", "Emilia-Romagna", 44.8015, 10.3279),
    ("Piacenza", "Emilia-Romagna", 45.0526, 9.6930),
    ("Ravenna", "Emilia-Romagna", 44.4184, 12.2035),
    ("Reggio Emilia", "Emilia-Romagna", 44.6989, 10.6310),
    ("Rimini", "Emilia-Romagna", 44.0678, 12.5695),
    ("Arezzo", "Toscana", 43.4633, 11.8798),
    ("Grosseto", "Toscana", 42.7635, 11.1124),
    ("Livorno", "Toscana", 43.5493, 10.3105),
    ("Lucca", "Toscana", 43.8376, 10.4951),
    ("Pisa", "Toscana", 43.7228, 10.4017),
    ("Pistoia", "Toscana", 43.9335, 10.9170),
    ("Prato", "Toscana", 43.8809, 11.0965),
    ("Siena", "Toscana", 43.3188, 11.3308),
    ("Pesaro", "Marche", 43.9104, 12.9133),
    ("Ascoli Piceno", "Marche", 42.8537, 13.5749),
    ("Macerata", "Marche", 43.3004, 13.4533),
    ("Terni", "Umbria", 42.5636, 12.6426),
    ("Chieti", "Abruzzo", 42.3510, 14.1676),
    ("Pescara", "Abruzzo", 42.4618, 14.2146),
    ("Teramo", "Abruzzo", 42.6597, 13.7042),
    ("Avellino", "Campania", 40.9146, 14.7906),
    ("Benevento", "Campania", 41.1298, 14.7822),
    ("Caserta", "Campania", 41.0743, 14.3325),
    ("Salerno", "Campania", 40.6824, 14.7681),
    ("Foggia", "Puglia", 41.4622, 15.5446),
    ("Lecce", "Puglia", 40.3516, 18.1750),
    ("Brindisi", "Puglia", 40.6328, 17.9418),
    ("Matera", "Basilicata", 40.6654, 16.6043),
    ("Cosenza", "Calabria", 39.3088, 16.2505),
    ("Crotone", "Calabria", 39.0847, 17.1236),
    ("Agrigento", "Sicilia", 37.3111, 13.5766),
    ("Caltanissetta", "Sicilia", 37.4901, 14.0630),
    ("Enna", "Sicilia", 37.5652, 14.2750),
    ("Ragusa", "Sicilia", 36.9269, 14.7253),
    ("Siracusa", "Sicilia", 37.0755, 15.2866),
    ("Trapani", "Sicilia", 38.0175, 12.5367),
    ("Nuoro", "Sardegna", 40.3211, 9.3295),
    ("Sassari", "Sardegna", 40.7268, 8.5592),
    ("Niscemi", "Sicilia", 37.1465, 14.3942),
    ("Ischia", "Campania", 40.7290, 13.9429),
    ("Amatrice", "Lazio", 42.6295, 13.2918),
    ("Norcia", "Umbria", 42.7932, 13.0897),
    ("Sarno", "Campania", 40.8119, 14.6140),
    ("Stromboli", "Sicilia", 38.7891, 15.2132),
    ("Etna", "Sicilia", 37.7510, 14.9934),
    ("Vesuvio", "Campania", 40.8219, 14.4286),
    ("Cinque Terre", "Liguria", 44.1461, 9.6547),
    ("Senigallia", "Marche", 43.7155, 13.2169),
    ("Faenza", "Emilia-Romagna", 44.2870, 11.8825),
    ("Cesena", "Emilia-Romagna", 44.1396, 12.2430),
    ("Casamicciola Terme", "Campania", 40.7467, 13.9098),
    ("Vajont", "Friuli VG", 46.2658, 12.3289),
    ("Longarone", "Veneto", 46.2647, 12.2969),
    ("Courmayeur", "Valle d'Aosta", 45.7933, 6.9686),
    ("Gela", "Sicilia", 37.0659, 14.2501),
    ("Sciacca", "Sicilia", 37.5077, 13.0843),
    ("Vibo Valentia", "Calabria", 38.6763, 16.0999),
    ("Corigliano-Rossano", "Calabria", 39.5977, 16.5177),
    ("Forlì", "Emilia-Romagna", 44.2227, 12.0408),
]

# --- Basemap sources (ordered by reliability) ---
BASEMAP_SOURCES = {
    "Google Satellite": {
        "url": "type=xyz&url=https://mt1.google.com/vt/lyrs%3Ds%26x%3D%7Bx%7D%26y%3D%7By%7D%26z%3D%7Bz%7D&zmax=19&zmin=0",
    },
    "Esri World Imagery": {
        "url": "type=xyz&url=https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/%7Bz%7D/%7By%7D/%7Bx%7D&zmax=19&zmin=0",
    },
    "OSM Standard": {
        "url": "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=19&zmin=0",
    },
}
