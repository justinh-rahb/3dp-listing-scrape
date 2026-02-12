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
    "scheduler_enabled": False,
}

DEFAULT_SEARCH_QUERIES = [
    {"url": "https://www.kijiji.ca/b-canada/3d-printer/k0l0", "label": "3d printer"},
    {"url": "https://www.kijiji.ca/b-canada/3d-printing/k0l0", "label": "3d printing"},
    {"url": "https://www.kijiji.ca/b-canada/bambu-lab/k0l0", "label": "bambu lab"},
    {"url": "https://www.kijiji.ca/b-canada/prusa/k0l0", "label": "prusa"},
    {"url": "https://www.kijiji.ca/b-canada/creality/k0l0", "label": "creality"},
    {"url": "https://www.kijiji.ca/b-canada/ender-3/k0l0", "label": "ender 3"},
    {"url": "https://www.kijiji.ca/b-canada/anycubic/k0l0", "label": "anycubic"},
    {"url": "https://www.kijiji.ca/b-canada/voron/k0l0", "label": "voron"},
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
