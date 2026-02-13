"""Configuration defaults for the 3D Printer Kijiji Deal Tracker.

Runtime-configurable settings are stored in the DB (settings table).
These defaults are used for first-run seeding only.
"""

import os

# Database path
DB_PATH = os.environ.get("DB_PATH", "listings.db")

# User agents to rotate (not user-configurable, just a static list)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

# ── Defaults for first-run DB seeding ──────────────────────────

DEFAULT_SETTINGS = {
    "scrape_interval_hours": 6,
    "max_pages_per_query": 5,
    "request_delay_min": 2.0,
    "request_delay_max": 5.0,
    "inactive_threshold": 3,
    # Used for USD-equivalent change detection for non-USD listings.
    "fx_rates_to_usd": {"USD": 1.0, "CAD": 0.74},
    # Production-friendly default: start scheduler automatically with the server.
    "scheduler_enabled": True,
}

DEFAULT_SEARCH_QUERIES = [
    {"url": "https://www.kijiji.ca/b-hamilton/3d-printer/k0l80014", "label": "3d printer"},
    {"url": "https://www.kijiji.ca/b-hamilton/3d-printing/k0l80014", "label": "3d printing"},
    {"url": "https://www.kijiji.ca/b-hamilton/bambu-lab/k0l80014", "label": "bambu lab"},
    {"url": "https://www.kijiji.ca/b-hamilton/prusa/k0l80014", "label": "prusa"},
    {"url": "https://www.kijiji.ca/b-hamilton/creality/k0l80014", "label": "creality"},
    {"url": "https://www.kijiji.ca/b-hamilton/ender-3/k0l80014", "label": "ender 3"},
    {"url": "https://www.kijiji.ca/b-hamilton/anycubic/k0l80014", "label": "anycubic"},
    {"url": "https://www.kijiji.ca/b-hamilton/voron/k0l80014", "label": "voron"},
    {"url": "https://www.sovol3d.com/products/sovol-zero-3d-printer?variant=50760656060725", "label": "sovol zero"},
    {
        "url": "https://www.formbot3d.com/products/voron-series-salad-fork-180mm-printers-for-ants-scaled-down-trident-high-quality-corexy-3d-printer-kit?VariantsId=11156",
        "label": "formbot salad fork",
    },
    {
        "url": "https://www.formbot3d.com/products/voron-micron-r1-180mm-high-quality-corexy-3d-printer-kit-latest-version?VariantsId=11069",
        "label": "formbot micron r1",
    },
    {
        "url": "https://www.formbot3d.com/products/voron-v02-corexy-3d-printer-kit-with-high-quality-parts?VariantsId=11017",
        "label": "formbot v0.2 r1",
    },
    {
        "url": "https://www.formbot3d.com/products/voron-trident-r1-pro-corexy-3d-printer-kit-with-best-quality-parts?VariantsId=10505",
        "label": "formbot trident r1",
    },
    {
        "url": "https://www.formbot3d.com/products/voron-24-r2-pro-corexy-3d-printer-kit-with-m8p-cb1-board-and-canbus-wiring-system?VariantsId=10457",
        "label": "formbot 2.4 r2",
    },
    {
        "url": "https://3dprintingcanada.com/products/bambu-lab-p1s",
        "label": "3dpc p1s",
    },
    {
        "url": "https://shop.snapmaker.com/products/snapmaker-u1-3d-printer",
        "label": "snapmaker u1",
    },
    {
        "url": "https://www.sovol3d.com/products/sovol-sv08-3d-printer?variant=48547571499317",
        "label": "sovol sv08",
    },
    {
        "url": "https://www.sovol3d.com/products/sovol-sv08-max-3d-printer?variant=51066484392245",
        "label": "sovol sv08 max",
    },
    {
        "url": "https://ca.qidi3d.com/products/qidi-q2",
        "label": "qidi q2",
    },
    {
        "url": "https://ca.qidi3d.com/products/max4",
        "label": "qidi max4",
    },
    {
        "url": "https://ca.qidi3d.com/products/plus4-3d-printer",
        "label": "qidi plus4",
    },
]

DEFAULT_BRAND_KEYWORDS = {
    "bambu": ["bambu", "bambulab", "bambu lab", "x1c", "x1 carbon", "p1s", "p1p", "a1 mini", "a1mini"],
    "prusa": ["prusa", "mk4", "mk3s", "mk3", "mini+", "xl"],
    "creality": ["creality", "cr-10", "cr10", "k1 max", "k1c"],
    "ender": ["ender", "ender 3", "ender3", "ender 5", "ender5"],
    "anycubic": ["anycubic", "kobra", "vyper", "mega"],
    "voron": ["voron", "v0", "v2.4", "trident"],
    "elegoo": ["elegoo", "neptune"],
    "flashforge": ["flashforge", "adventurer"],
    "sovol": ["sovol", "sv06", "sv07"],
    "qidi": ["qidi"],
}
