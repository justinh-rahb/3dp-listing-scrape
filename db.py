"""Database layer for the 3D Printer Kijiji Deal Tracker."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from config import DB_PATH, DEFAULT_BRAND_KEYWORDS, DEFAULT_SEARCH_QUERIES, DEFAULT_SETTINGS


def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            kijiji_id       TEXT PRIMARY KEY,
            source          TEXT NOT NULL DEFAULT 'kijiji',
            url             TEXT NOT NULL,
            title           TEXT NOT NULL,
            description     TEXT,
            seller_name     TEXT,
            location        TEXT,
            image_urls      TEXT,
            listing_date    TEXT,
            first_seen      TEXT NOT NULL,
            last_seen       TEXT NOT NULL,
            is_active       INTEGER DEFAULT 1,
            is_hidden       INTEGER DEFAULT 0,
            is_starred      INTEGER DEFAULT 0,
            missed_runs     INTEGER DEFAULT 0,
            brand           TEXT,
            model           TEXT,
            msrp            REAL,
            msrp_currency   TEXT,
            current_price   REAL,
            original_price  REAL,
            nominal_price   REAL,
            on_sale         INTEGER DEFAULT 0,
            currency        TEXT NOT NULL DEFAULT 'CAD'
        );

        CREATE TABLE IF NOT EXISTS price_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kijiji_id       TEXT NOT NULL REFERENCES listings(kijiji_id),
            price           REAL,
            scraped_at      TEXT NOT NULL,
            UNIQUE(kijiji_id, scraped_at)
        );

        CREATE TABLE IF NOT EXISTS scrape_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            listings_found  INTEGER DEFAULT 0,
            new_listings    INTEGER DEFAULT 0,
            price_changes   INTEGER DEFAULT 0,
            errors          INTEGER DEFAULT 0,
            search_query    TEXT,
            status          TEXT NOT NULL DEFAULT 'running',
            queries_total   INTEGER DEFAULT 0,
            queries_succeeded INTEGER DEFAULT 0,
            queries_failed  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS scrape_query_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES scrape_runs(id) ON DELETE CASCADE,
            query_id        INTEGER REFERENCES search_queries(id) ON DELETE SET NULL,
            label           TEXT NOT NULL,
            url             TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            status          TEXT NOT NULL DEFAULT 'running',
            listings_found  INTEGER DEFAULT 0,
            new_listings    INTEGER DEFAULT 0,
            price_changes   INTEGER DEFAULT 0,
            pages_attempted INTEGER DEFAULT 0,
            pages_completed INTEGER DEFAULT 0,
            failed_url      TEXT,
            http_status     INTEGER,
            error_type      TEXT,
            error_message   TEXT
        );

        CREATE TABLE IF NOT EXISTS listing_queries (
            kijiji_id       TEXT NOT NULL REFERENCES listings(kijiji_id) ON DELETE CASCADE,
            query_id        INTEGER NOT NULL REFERENCES search_queries(id) ON DELETE CASCADE,
            last_seen       TEXT NOT NULL,
            missed_runs     INTEGER NOT NULL DEFAULT 0,
            is_active       INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (kijiji_id, query_id)
        );

        CREATE TABLE IF NOT EXISTS deal_notifications (
            kijiji_id       TEXT NOT NULL REFERENCES listings(kijiji_id) ON DELETE CASCADE,
            fingerprint     TEXT NOT NULL,
            notified_at     TEXT NOT NULL,
            PRIMARY KEY (kijiji_id, fingerprint)
        );

        CREATE TABLE IF NOT EXISTS app_locks (
            name            TEXT PRIMARY KEY,
            acquired_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS search_queries (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            url     TEXT NOT NULL,
            label   TEXT NOT NULL,
            enabled INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS brand_keywords (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            brand   TEXT NOT NULL,
            keyword TEXT NOT NULL,
            UNIQUE(brand, keyword)
        );

        CREATE TABLE IF NOT EXISTS msrp_entries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            brand         TEXT NOT NULL,
            model         TEXT NOT NULL,
            msrp_cad      REAL,
            msrp_usd      REAL,
            retail_price  REAL,
            last_updated  TEXT,
            aliases       TEXT NOT NULL DEFAULT '[]',
            price_basis   TEXT NOT NULL DEFAULT 'unverified',
            product_status TEXT NOT NULL DEFAULT 'unknown',
            source_name   TEXT,
            source_url    TEXT,
            verified_at   TEXT,
            notes         TEXT,
            UNIQUE(brand, model)
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_kijiji_id ON price_snapshots(kijiji_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_scraped_at ON price_snapshots(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_listings_brand ON listings(brand);
        CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active);
        CREATE INDEX IF NOT EXISTS idx_listings_current_price ON listings(current_price);
        CREATE INDEX IF NOT EXISTS idx_scrape_query_runs_run_id ON scrape_query_runs(run_id);
        CREATE INDEX IF NOT EXISTS idx_listing_queries_query_id ON listing_queries(query_id);
    """)
    _ensure_schema_updates(conn)
    conn.commit()

    # Seed defaults if tables are empty
    _seed_defaults(conn)
    _upgrade_reference_catalog(conn)
    conn.close()


def _ensure_schema_updates(conn: sqlite3.Connection):
    """Apply additive schema updates for existing databases."""
    listing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()
    }
    if "is_hidden" not in listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN is_hidden INTEGER DEFAULT 0")
    if "source" not in listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN source TEXT NOT NULL DEFAULT 'kijiji'")
    if "currency" not in listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN currency TEXT NOT NULL DEFAULT 'CAD'")
    if "nominal_price" not in listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN nominal_price REAL")
    if "on_sale" not in listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN on_sale INTEGER DEFAULT 0")
    if "is_starred" not in listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN is_starred INTEGER DEFAULT 0")
    if "msrp_currency" not in listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN msrp_currency TEXT")
        conn.execute("UPDATE listings SET msrp_currency = 'CAD' WHERE msrp IS NOT NULL")

    msrp_table = conn.execute("PRAGMA table_info(msrp_entries)").fetchall()
    if any(row["name"] == "msrp_cad" and row["notnull"] for row in msrp_table):
        # Older databases required a CAD value, which encouraged unsourced currency
        # conversions. Rebuild once so a verified USD-only reference can stay USD-only.
        conn.execute("ALTER TABLE msrp_entries RENAME TO msrp_entries_legacy")
        conn.execute("""
            CREATE TABLE msrp_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                msrp_cad REAL,
                msrp_usd REAL,
                retail_price REAL,
                last_updated TEXT,
                UNIQUE(brand, model)
            )
        """)
        conn.execute("""
            INSERT INTO msrp_entries
                (id, brand, model, msrp_cad, msrp_usd, retail_price, last_updated)
            SELECT id, brand, model, msrp_cad, msrp_usd, retail_price, last_updated
            FROM msrp_entries_legacy
        """)
        conn.execute("DROP TABLE msrp_entries_legacy")

    msrp_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(msrp_entries)").fetchall()
    }
    for name, definition in {
        "aliases": "TEXT NOT NULL DEFAULT '[]'",
        "price_basis": "TEXT NOT NULL DEFAULT 'unverified'",
        "product_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "source_name": "TEXT",
        "source_url": "TEXT",
        "verified_at": "TEXT",
        "notes": "TEXT",
    }.items():
        if name not in msrp_columns:
            conn.execute(f"ALTER TABLE msrp_entries ADD COLUMN {name} {definition}")

    scrape_run_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(scrape_runs)").fetchall()
    }
    for name, definition in {
        "status": "TEXT NOT NULL DEFAULT 'running'",
        "queries_total": "INTEGER DEFAULT 0",
        "queries_succeeded": "INTEGER DEFAULT 0",
        "queries_failed": "INTEGER DEFAULT 0",
    }.items():
        if name not in scrape_run_columns:
            conn.execute(f"ALTER TABLE scrape_runs ADD COLUMN {name} {definition}")
    conn.execute(
        """
        UPDATE scrape_runs
        SET status = CASE WHEN errors > 0 THEN 'partial' ELSE 'success' END
        WHERE status = 'running' AND finished_at IS NOT NULL
        """
    )

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scrape_query_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES scrape_runs(id) ON DELETE CASCADE,
            query_id INTEGER REFERENCES search_queries(id) ON DELETE SET NULL,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            listings_found INTEGER DEFAULT 0,
            new_listings INTEGER DEFAULT 0,
            price_changes INTEGER DEFAULT 0,
            pages_attempted INTEGER DEFAULT 0,
            pages_completed INTEGER DEFAULT 0,
            failed_url TEXT,
            http_status INTEGER,
            error_type TEXT,
            error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS listing_queries (
            kijiji_id TEXT NOT NULL REFERENCES listings(kijiji_id) ON DELETE CASCADE,
            query_id INTEGER NOT NULL REFERENCES search_queries(id) ON DELETE CASCADE,
            last_seen TEXT NOT NULL,
            missed_runs INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (kijiji_id, query_id)
        );
        CREATE TABLE IF NOT EXISTS deal_notifications (
            kijiji_id TEXT NOT NULL REFERENCES listings(kijiji_id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL,
            notified_at TEXT NOT NULL,
            PRIMARY KEY (kijiji_id, fingerprint)
        );
        CREATE TABLE IF NOT EXISTS app_locks (
            name TEXT PRIMARY KEY,
            acquired_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scrape_query_runs_run_id ON scrape_query_runs(run_id);
        CREATE INDEX IF NOT EXISTS idx_listing_queries_query_id ON listing_queries(query_id);
    """)
    # Normalize historical source tags for Qidi URLs (e.g. "ca" -> "qidi3d").
    conn.execute(
        """
        UPDATE listings
        SET source = 'qidi3d'
        WHERE LOWER(url) LIKE '%qidi3d.com%'
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_hidden ON listings(is_hidden)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_starred ON listings(is_starred)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source)")


def _seed_defaults(conn: sqlite3.Connection):
    """Populate settings, search queries, brands, and MSRP on first run."""
    # Settings
    existing = conn.execute("SELECT COUNT(*) as c FROM settings").fetchone()["c"]
    if existing == 0:
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value))
            )
    else:
        # Backfill newly introduced defaults without overwriting user values.
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value))
            )

    # Search queries
    existing = conn.execute("SELECT COUNT(*) as c FROM search_queries").fetchone()["c"]
    if existing == 0:
        for q in DEFAULT_SEARCH_QUERIES:
            conn.execute(
                "INSERT INTO search_queries (url, label, enabled) VALUES (?, ?, 1)",
                (q["url"], q["label"])
            )
    else:
        # Add any newly introduced default queries without duplicating existing URLs.
        existing_urls = {
            row["url"]
            for row in conn.execute("SELECT url FROM search_queries").fetchall()
        }
        for q in DEFAULT_SEARCH_QUERIES:
            if q["url"] in existing_urls:
                continue
            conn.execute(
                "INSERT INTO search_queries (url, label, enabled) VALUES (?, ?, 1)",
                (q["url"], q["label"])
            )

    # Brand keywords
    existing = conn.execute("SELECT COUNT(*) as c FROM brand_keywords").fetchone()["c"]
    if existing == 0:
        for brand, keywords in DEFAULT_BRAND_KEYWORDS.items():
            for kw in keywords:
                conn.execute(
                    "INSERT OR IGNORE INTO brand_keywords (brand, keyword) VALUES (?, ?)",
                    (brand, kw)
                )

    # MSRP entries from msrp_data.json
    existing = conn.execute("SELECT COUNT(*) as c FROM msrp_entries").fetchone()["c"]
    if existing == 0:
        import os
        msrp_path = os.path.join(os.path.dirname(__file__), "msrp_data.json")
        if os.path.exists(msrp_path):
            with open(msrp_path) as f:
                msrp_data = json.load(f)
            for brand, models in msrp_data.items():
                for model, prices in models.items():
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO msrp_entries
                            (brand, model, msrp_cad, msrp_usd, aliases, price_basis,
                             product_status, source_name, source_url, verified_at, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (brand, model, prices.get("msrp_cad"), prices.get("msrp_usd"),
                         json.dumps(prices.get("aliases", [])), prices.get("price_basis", "unverified"),
                         prices.get("product_status", "unknown"), prices.get("source_name"),
                         prices.get("source_url"), prices.get("verified_at"), prices.get("notes"))
                    )

    conn.commit()


def _catalog_entries() -> list[tuple[str, str, dict]]:
    """Load the checked-in, source-backed reference catalog."""
    import os
    path = os.path.join(os.path.dirname(__file__), "msrp_data.json")
    with open(path, encoding="utf-8") as source:
        catalog = json.load(source)
    return [
        (brand, model, details)
        for brand, models in catalog.items()
        for model, details in models.items()
    ]


def _upgrade_reference_catalog(conn: sqlite3.Connection):
    """Apply each bundled catalog revision once to existing installations."""
    version = "2026-09-09.2"
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'reference_catalog_version'"
    ).fetchone()
    if row and json.loads(row["value"]) == version:
        return

    # The bundled set is authoritative: it consolidates Creality's Ender line
    # and excludes broad aliases such as `xl`, `u1`, `mega`, and `v0` that match
    # ordinary listing text.
    conn.execute("DELETE FROM brand_keywords")
    for brand, keywords in DEFAULT_BRAND_KEYWORDS.items():
        for keyword in keywords:
            conn.execute(
                "INSERT OR IGNORE INTO brand_keywords (brand, keyword) VALUES (?, ?)",
                (brand, keyword),
            )

    # This catalog is authoritative. Keeping older unsourced rows made them look
    # like valid comparison data even when the UI labelled them as unverified.
    conn.execute("DELETE FROM msrp_entries")
    for brand, model, details in _catalog_entries():
        upsert_msrp_entry(
            brand, model, details.get("msrp_cad"), details.get("msrp_usd"),
            aliases=details.get("aliases", []),
            price_basis=details.get("price_basis", "unverified"),
            product_status=details.get("product_status", "unknown"),
            source_name=details.get("source_name"), source_url=details.get("source_url"),
            verified_at=details.get("verified_at"), notes=details.get("notes"), conn=conn,
        )

    conn.execute("UPDATE listings SET brand = 'creality' WHERE brand = 'ender'")
    conn.execute("UPDATE listings SET model = 'X1 Carbon' WHERE brand = 'bambu' AND model = 'X1C'")
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("reference_catalog_version", json.dumps(version)),
    )
    conn.commit()


# ── Settings CRUD ──────────────────────────────────────────────

def get_setting(key: str, default: Any = None, conn: Optional[sqlite3.Connection] = None) -> Any:
    close = conn is None
    if close:
        conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if close:
        conn.close()
    if row:
        return json.loads(row["value"])
    return default


def set_setting(key: str, value: Any, conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, json.dumps(value))
    )
    if close:
        conn.commit()
        conn.close()


def get_all_settings(conn: Optional[sqlite3.Connection] = None) -> dict:
    close = conn is None
    if close:
        conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    if close:
        conn.close()
    return {row["key"]: json.loads(row["value"]) for row in rows}


def try_acquire_app_lock(name: str, ttl_seconds: int = 7200) -> bool:
    """Acquire a cross-process SQLite lock, replacing only a stale owner."""
    conn = get_conn()
    now = datetime.now(timezone.utc)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT acquired_at FROM app_locks WHERE name = ?", (name,)).fetchone()
        if row:
            try:
                acquired_at = datetime.fromisoformat(row["acquired_at"])
                if acquired_at.tzinfo is None:
                    acquired_at = acquired_at.replace(tzinfo=timezone.utc)
                stale = (now - acquired_at).total_seconds() >= ttl_seconds
            except (TypeError, ValueError):
                stale = True
            if not stale:
                conn.rollback()
                return False
            conn.execute("DELETE FROM app_locks WHERE name = ?", (name,))
        conn.execute(
            "INSERT INTO app_locks (name, acquired_at) VALUES (?, ?)",
            (name, now.isoformat()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def release_app_lock(name: str):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM app_locks WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


def is_app_lock_active(name: str, ttl_seconds: int = 7200) -> bool:
    conn = get_conn()
    try:
        row = conn.execute("SELECT acquired_at FROM app_locks WHERE name = ?", (name,)).fetchone()
        if not row:
            return False
        try:
            acquired_at = datetime.fromisoformat(row["acquired_at"])
            if acquired_at.tzinfo is None:
                acquired_at = acquired_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return (datetime.now(timezone.utc) - acquired_at).total_seconds() < ttl_seconds
    finally:
        conn.close()


# ── Search Queries CRUD ───────────────────────────────────────

def get_search_queries(enabled_only: bool = False, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    close = conn is None
    if close:
        conn = get_conn()
    if enabled_only:
        rows = conn.execute("SELECT * FROM search_queries WHERE enabled = 1 ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM search_queries ORDER BY id").fetchall()
    if close:
        conn.close()
    return [dict(r) for r in rows]


def add_search_query(url: str, label: str, conn: Optional[sqlite3.Connection] = None) -> int:
    close = conn is None
    if close:
        conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO search_queries (url, label, enabled) VALUES (?, ?, 1)",
        (url, label)
    )
    conn.commit()
    qid = cursor.lastrowid
    if close:
        conn.close()
    return qid


def update_search_query(query_id: int, url: Optional[str] = None,
                        label: Optional[str] = None, enabled: Optional[bool] = None,
                        conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    updates = []
    params = []
    if url is not None:
        updates.append("url = ?")
        params.append(url)
    if label is not None:
        updates.append("label = ?")
        params.append(label)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if updates:
        params.append(query_id)
        conn.execute(f"UPDATE search_queries SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
    if close:
        conn.close()


def delete_search_query(query_id: int, conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    conn.execute("DELETE FROM search_queries WHERE id = ?", (query_id,))
    if close:
        conn.commit()
        conn.close()


# ── Brand Keywords CRUD ───────────────────────────────────────

def get_brand_keywords(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    close = conn is None
    if close:
        conn = get_conn()
    rows = conn.execute("SELECT * FROM brand_keywords ORDER BY brand, keyword").fetchall()
    if close:
        conn.close()
    return [dict(r) for r in rows]


def get_brand_keywords_map(conn: Optional[sqlite3.Connection] = None) -> dict[str, list[str]]:
    """Return brand keywords as a {brand: [keywords]} dict for use in detection."""
    entries = get_brand_keywords(conn)
    result = {}
    for entry in entries:
        result.setdefault(entry["brand"], []).append(entry["keyword"])
    return result


def add_brand_keyword(brand: str, keyword: str, conn: Optional[sqlite3.Connection] = None) -> int:
    close = conn is None
    if close:
        conn = get_conn()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO brand_keywords (brand, keyword) VALUES (?, ?)",
        (brand.lower(), keyword.lower())
    )
    conn.commit()
    kid = cursor.lastrowid
    if close:
        conn.close()
    return kid


def delete_brand_keyword(keyword_id: int, conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    conn.execute("DELETE FROM brand_keywords WHERE id = ?", (keyword_id,))
    conn.commit()
    if close:
        conn.close()


# ── MSRP CRUD ─────────────────────────────────────────────────

def get_msrp_entries(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    close = conn is None
    if close:
        conn = get_conn()
    rows = conn.execute("SELECT * FROM msrp_entries ORDER BY brand, model").fetchall()
    if close:
        conn.close()
    return [dict(r) for r in rows]


def upsert_msrp_entry(brand: str, model: str, msrp_cad: Optional[float] = None,
                      msrp_usd: Optional[float] = None,
                      retail_price: Optional[float] = None,
                      aliases: Optional[list[str]] = None,
                      price_basis: str = "unverified",
                      product_status: str = "unknown",
                      source_name: Optional[str] = None,
                      source_url: Optional[str] = None,
                      verified_at: Optional[str] = None,
                      notes: Optional[str] = None,
                      conn: Optional[sqlite3.Connection] = None) -> int:
    close = conn is None
    if close:
        conn = get_conn()
    
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    
    cursor = conn.execute("""
        INSERT INTO msrp_entries
            (brand, model, msrp_cad, msrp_usd, retail_price, last_updated, aliases,
             price_basis, product_status, source_name, source_url, verified_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(brand, model) DO UPDATE SET 
            msrp_cad = ?,
            msrp_usd = ?,
            retail_price = ?,
            last_updated = ?,
            aliases = ?,
            price_basis = ?,
            product_status = ?,
            source_name = ?,
            source_url = ?,
            verified_at = ?,
            notes = ?
    """, (brand.lower(), model, msrp_cad, msrp_usd, retail_price, now,
           json.dumps(aliases or []), price_basis, product_status, source_name, source_url,
           verified_at, notes, msrp_cad, msrp_usd, retail_price, now,
           json.dumps(aliases or []), price_basis, product_status, source_name, source_url,
           verified_at, notes))
    conn.commit()
    eid = cursor.lastrowid
    if close:
        conn.close()
    return eid


def delete_msrp_entry(entry_id: int, conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    conn.execute("DELETE FROM msrp_entries WHERE id = ?", (entry_id,))
    conn.commit()
    if close:
        conn.close()


def get_msrp_map(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Return MSRP data as {brand: {model: {msrp_cad, msrp_usd, retail_price}}} for tracker use."""
    entries = get_msrp_entries(conn)
    result = {}
    for e in entries:
        brand = result.setdefault(e["brand"], {})
        brand[e["model"]] = {
            "msrp_cad": e["msrp_cad"],
            "msrp_usd": e["msrp_usd"],
            "retail_price": e.get("retail_price"),
            "aliases": json.loads(e.get("aliases") or "[]"),
        }
    return result


def export_app_data(data_type: str = "all", conn: Optional[sqlite3.Connection] = None) -> dict:
    """Export search queries, brand keywords, and/or MSRP entries as a dictionary."""
    close_after = False
    if conn is None:
        conn = get_conn()
        close_after = True

    try:
        result = {}

        if data_type in ("all", "queries"):
            queries = []
            for row in conn.execute("SELECT url, label, enabled FROM search_queries").fetchall():
                queries.append({"url": row["url"], "label": row["label"], "enabled": bool(row["enabled"])})
            result["search_queries"] = queries

        if data_type in ("all", "brands"):
            brands = []
            for row in conn.execute("SELECT brand, keyword FROM brand_keywords").fetchall():
                brands.append({"brand": row["brand"], "keyword": row["keyword"]})
            result["brand_keywords"] = brands

        if data_type in ("all", "msrp"):
            msrp = []
            for row in conn.execute("SELECT * FROM msrp_entries ORDER BY brand, model").fetchall():
                item = dict(row)
                item.pop("id", None)
                item["aliases"] = json.loads(item.get("aliases") or "[]")
                msrp.append(item)
            result["msrp_entries"] = msrp

        return result
    finally:
        if close_after:
            conn.close()


def import_app_data(data: dict, data_type: str = "all", clear_existing: bool = False, overwrite: bool = False, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Import search queries, brand keywords, and/or MSRP entries from a dictionary."""
    close_after = False
    if conn is None:
        conn = get_conn()
        close_after = True

    result = {"queries": 0, "brands": 0, "msrp": 0}
    try:
        if clear_existing:
            if data_type in ("all", "queries"):
                conn.execute("DELETE FROM search_queries")
            if data_type in ("all", "brands"):
                conn.execute("DELETE FROM brand_keywords")
            if data_type in ("all", "msrp"):
                conn.execute("DELETE FROM msrp_entries")

        if data_type in ("all", "queries"):
            queries = data.get("search_queries", [])
            for q in queries:
                if "url" in q and "label" in q:
                    enabled = 1 if q.get("enabled", True) else 0
                    # search_queries doesn't have an obvious UNIQUE constraint on url+label, so it might just insert duplicates 
                    # unless we do a select check. I'll preserve exact behaviour for now.
                    conn.execute(
                        "INSERT INTO search_queries (url, label, enabled) VALUES (?, ?, ?)",
                        (q["url"], q["label"], enabled)
                    )
                    result["queries"] += 1

        if data_type in ("all", "brands"):
            brands = data.get("brand_keywords", [])
            for b in brands:
                if "brand" in b and "keyword" in b:
                    conn.execute(
                        "INSERT OR IGNORE INTO brand_keywords (brand, keyword) VALUES (?, ?)",
                        (b["brand"].lower(), b["keyword"].lower())
                    )
                    result["brands"] += 1

        if data_type in ("all", "msrp"):
            msrp = data.get("msrp_entries", [])
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            
            for m in msrp:
                if "brand" in m and "model" in m and (
                    m.get("msrp_cad") is not None or m.get("msrp_usd") is not None
                    or m.get("source_url") or m.get("aliases")
                ):
                    values = (
                        m["brand"].lower(), m["model"], m.get("msrp_cad"), m.get("msrp_usd"),
                        m.get("retail_price"), m.get("last_updated") or now,
                        json.dumps(m.get("aliases", [])), m.get("price_basis", "unverified"),
                        m.get("product_status", "unknown"), m.get("source_name"),
                        m.get("source_url"), m.get("verified_at"), m.get("notes"),
                    )
                    if overwrite:
                        query = """
                            INSERT INTO msrp_entries
                                (brand, model, msrp_cad, msrp_usd, retail_price, last_updated,
                                 aliases, price_basis, product_status, source_name, source_url,
                                 verified_at, notes)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(brand, model) DO UPDATE SET
                                msrp_cad=excluded.msrp_cad,
                                msrp_usd=excluded.msrp_usd,
                                retail_price=excluded.retail_price,
                                last_updated=excluded.last_updated,
                                aliases=excluded.aliases,
                                price_basis=excluded.price_basis,
                                product_status=excluded.product_status,
                                source_name=excluded.source_name,
                                source_url=excluded.source_url,
                                verified_at=excluded.verified_at,
                                notes=excluded.notes
                        """
                    else:
                        query = """
                            INSERT OR IGNORE INTO msrp_entries
                                (brand, model, msrp_cad, msrp_usd, retail_price, last_updated,
                                 aliases, price_basis, product_status, source_name, source_url,
                                 verified_at, notes)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    conn.execute(query, values)
                    result["msrp"] += 1
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        if close_after:
            conn.close()
    
    return result


# ── Listings CRUD (unchanged from V1) ─────────────────────────

def upsert_listing(listing_data: dict, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Insert or update a listing. Returns True if this is a new listing."""
    close = conn is None
    if close:
        conn = get_conn()

    now = datetime.now(timezone.utc).isoformat()
    image_urls_json = json.dumps(listing_data.get("image_urls", []))

    existing = conn.execute(
        "SELECT kijiji_id, current_price FROM listings WHERE kijiji_id = ?",
        (listing_data["kijiji_id"],)
    ).fetchone()

    is_new = existing is None

    if is_new:
        conn.execute("""
            INSERT INTO listings (kijiji_id, source, url, title, description, seller_name,
                                  location, image_urls, listing_date, first_seen, last_seen,
                                  is_active, is_hidden, is_starred, missed_runs, brand, model, msrp, msrp_currency,
                                  current_price, original_price, nominal_price, on_sale, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            listing_data["kijiji_id"],
            listing_data.get("source", "kijiji"),
            listing_data["url"],
            listing_data["title"],
            listing_data.get("description"),
            listing_data.get("seller_name"),
            listing_data.get("location"),
            image_urls_json,
            listing_data.get("listing_date"),
            now, now,
            listing_data.get("brand"),
            listing_data.get("model"),
            listing_data.get("msrp"),
            listing_data.get("msrp_currency"),
            listing_data.get("price"),
            listing_data.get("price"),
            listing_data.get("nominal_price"),
            1 if listing_data.get("on_sale", False) else 0,
            listing_data.get("currency", "CAD").upper(),
        ))
    else:
        conn.execute("""
            UPDATE listings SET
                source = ?, url = ?, title = ?, description = COALESCE(?, description),
                seller_name = COALESCE(?, seller_name),
                location = COALESCE(?, location),
                image_urls = CASE WHEN ? != '[]' THEN ? ELSE image_urls END,
                listing_date = COALESCE(?, listing_date),
                last_seen = ?, is_active = 1, missed_runs = 0,
                brand = COALESCE(?, brand), model = COALESCE(?, model),
                msrp = COALESCE(?, msrp), msrp_currency = COALESCE(?, msrp_currency),
                current_price = COALESCE(?, current_price),
                nominal_price = COALESCE(?, nominal_price),
                on_sale = ?,
                currency = COALESCE(?, currency)
            WHERE kijiji_id = ?
        """, (
            listing_data.get("source", "kijiji"),
            listing_data["url"],
            listing_data["title"],
            listing_data.get("description"),
            listing_data.get("seller_name"),
            listing_data.get("location"),
            image_urls_json, image_urls_json,
            listing_data.get("listing_date"),
            now,
            listing_data.get("brand"),
            listing_data.get("model"),
            listing_data.get("msrp"),
            listing_data.get("msrp_currency"),
            listing_data.get("price"),
            listing_data.get("nominal_price"),
            1 if listing_data.get("on_sale", False) else 0,
            listing_data.get("currency", "CAD").upper(),
            listing_data["kijiji_id"],
        ))

    if close:
        conn.commit()
        conn.close()
    return is_new


def add_price_snapshot(kijiji_id: str, price: Optional[float], scraped_at: str,
                       conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO price_snapshots (kijiji_id, price, scraped_at) VALUES (?, ?, ?)",
        (kijiji_id, price, scraped_at)
    )
    if close:
        conn.commit()
        conn.close()


def start_scrape_run(search_query: str = "", queries_total: int = 0,
                     conn: Optional[sqlite3.Connection] = None) -> int:
    close = conn is None
    if close:
        conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "INSERT INTO scrape_runs (started_at, search_query, status, queries_total) VALUES (?, ?, 'running', ?)",
        (now, search_query, queries_total)
    )
    conn.commit()
    run_id = cursor.lastrowid
    if close:
        conn.close()
    return run_id


def finish_scrape_run(run_id: int, listings_found: int, new_listings: int,
                      price_changes: int, errors: int, queries_succeeded: int = 0,
                      queries_failed: int = 0, status: str = "success",
                      conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE scrape_runs SET finished_at = ?, listings_found = ?,
               new_listings = ?, price_changes = ?, errors = ?, status = ?,
               queries_succeeded = ?, queries_failed = ?
        WHERE id = ?
    """, (now, listings_found, new_listings, price_changes, errors, status,
          queries_succeeded, queries_failed, run_id))
    conn.commit()
    if close:
        conn.close()


def start_scrape_query_run(run_id: int, query: dict,
                           conn: Optional[sqlite3.Connection] = None) -> int:
    close = conn is None
    if close:
        conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO scrape_query_runs (run_id, query_id, label, url, started_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, query.get("id"), query["label"], query["url"], now),
    )
    conn.commit()
    query_run_id = cursor.lastrowid
    if close:
        conn.close()
    return query_run_id


def finish_scrape_query_run(query_run_id: int, *, status: str,
                            listings_found: int = 0, new_listings: int = 0,
                            price_changes: int = 0, pages_attempted: int = 0,
                            pages_completed: int = 0, failed_url: Optional[str] = None,
                            http_status: Optional[int] = None,
                            error_type: Optional[str] = None,
                            error_message: Optional[str] = None,
                            conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE scrape_query_runs
        SET finished_at = ?, status = ?, listings_found = ?, new_listings = ?,
            price_changes = ?, pages_attempted = ?, pages_completed = ?,
            failed_url = ?, http_status = ?, error_type = ?, error_message = ?
        WHERE id = ?
        """,
        (now, status, listings_found, new_listings, price_changes,
         pages_attempted, pages_completed, failed_url, http_status,
         error_type, error_message, query_run_id),
    )
    conn.commit()
    if close:
        conn.close()


def fail_running_query_runs(run_id: int, error_type: str, error_message: str,
                            conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE scrape_query_runs
        SET finished_at = ?, status = 'failed', error_type = ?, error_message = ?
        WHERE run_id = ? AND status = 'running'
        """,
        (now, error_type, error_message, run_id),
    )
    if close:
        conn.commit()
        conn.close()


def record_listing_query(kijiji_id: str, query_id: int, seen_at: str,
                         conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    conn.execute(
        """
        INSERT INTO listing_queries (kijiji_id, query_id, last_seen, missed_runs, is_active)
        VALUES (?, ?, ?, 0, 1)
        ON CONFLICT(kijiji_id, query_id) DO UPDATE SET
            last_seen = excluded.last_seen, missed_runs = 0, is_active = 1
        """,
        (kijiji_id, query_id, seen_at),
    )
    if close:
        conn.commit()
        conn.close()


def finalize_query_visibility(query_id: int, seen_ids: set,
                              conn: Optional[sqlite3.Connection] = None):
    """Age only listings associated with one successfully completed query."""
    close = conn is None
    if close:
        conn = get_conn()

    inactive_threshold = get_setting("inactive_threshold", 3, conn)

    mapped = conn.execute(
        "SELECT kijiji_id FROM listing_queries WHERE query_id = ? AND is_active = 1",
        (query_id,),
    ).fetchall()

    for row in mapped:
        kid = row["kijiji_id"]
        if kid not in seen_ids:
            conn.execute(
                "UPDATE listing_queries SET missed_runs = missed_runs + 1 WHERE kijiji_id = ? AND query_id = ?",
                (kid, query_id),
            )
            conn.execute("""
                UPDATE listing_queries SET is_active = 0
                WHERE kijiji_id = ? AND query_id = ? AND missed_runs >= ?
            """, (kid, query_id, inactive_threshold))

    conn.execute(
        """
        UPDATE listings
        SET is_active = CASE WHEN EXISTS (
                SELECT 1 FROM listing_queries lq
                WHERE lq.kijiji_id = listings.kijiji_id AND lq.is_active = 1
            ) THEN 1 ELSE 0 END,
            missed_runs = COALESCE((
                SELECT MIN(lq.missed_runs) FROM listing_queries lq
                WHERE lq.kijiji_id = listings.kijiji_id
            ), missed_runs)
        WHERE kijiji_id IN (
            SELECT kijiji_id FROM listing_queries WHERE query_id = ?
        )
        """,
        (query_id,),
    )

    conn.commit()
    if close:
        conn.close()


def get_recent_scrape_runs(limit: int = 10,
                           conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    close = conn is None
    if close:
        conn = get_conn()
    runs = [dict(row) for row in conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()]
    for run in runs:
        run["queries"] = [dict(row) for row in conn.execute(
            "SELECT * FROM scrape_query_runs WHERE run_id = ? ORDER BY id", (run["id"],)
        ).fetchall()]
    if close:
        conn.close()
    return runs


def get_unnotified_deals(deals: list[dict],
                         conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Return deal states that have not already produced a notification."""
    close = conn is None
    if close:
        conn = get_conn()
    unseen = []
    for deal in deals:
        state = {
            "currency": deal.get("currency"),
            "current_price": deal.get("current_price"),
            "price_drop_pct": deal.get("price_drop_pct"),
            "price_to_retail_ratio": deal.get("price_to_retail_ratio"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        exists = conn.execute(
            "SELECT 1 FROM deal_notifications WHERE kijiji_id = ? AND fingerprint = ?",
            (deal["kijiji_id"], fingerprint),
        ).fetchone()
        if not exists:
            item = dict(deal)
            item["_fingerprint"] = fingerprint
            unseen.append(item)
    if close:
        conn.close()
    return unseen


def mark_deals_notified(deals: list[dict],
                        conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    for deal in deals:
        conn.execute(
            """
            INSERT OR IGNORE INTO deal_notifications (kijiji_id, fingerprint, notified_at)
            VALUES (?, ?, ?)
            """,
            (deal["kijiji_id"], deal["_fingerprint"], now),
        )
    conn.commit()
    if close:
        conn.close()


def get_listings(filters: Optional[dict] = None,
                 conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    close = conn is None
    if close:
        conn = get_conn()

    filters = filters or {}
    where_clauses = []
    params = []

    if not filters.get("show_hidden", False):
        where_clauses.append("is_hidden = 0")

    listing_status = filters.get("listing_status")
    if listing_status == "inactive":
        where_clauses.append("is_active = 0")
    elif listing_status != "all" and filters.get("active_only", True):
        # active_only remains supported for callers outside the dashboard.
        where_clauses.append("is_active = 1")

    if filters.get("stale_days"):
        where_clauses.append("datetime(last_seen) < datetime('now', ?)")
        params.append(f"-{int(filters['stale_days'])} days")

    if filters.get("starred_only", False):
        where_clauses.append("is_starred = 1")

    if filters.get("brand"):
        where_clauses.append("brand = ?")
        params.append(filters["brand"])

    if filters.get("model"):
        where_clauses.append("LOWER(model) = LOWER(?)")
        params.append(filters["model"])

    if filters.get("min_price") is not None:
        where_clauses.append("current_price >= ?")
        params.append(filters["min_price"])

    if filters.get("max_price") is not None:
        where_clauses.append("current_price <= ?")
        params.append(filters["max_price"])

    if filters.get("location"):
        where_clauses.append("location LIKE ?")
        params.append(f"%{filters['location']}%")

    if filters.get("search"):
        where_clauses.append("(title LIKE ? OR description LIKE ?)")
        params.extend([f"%{filters['search']}%", f"%{filters['search']}%"])

    where = " AND ".join(where_clauses) if where_clauses else "1=1"

    sort_map = {
        "title_asc": "LOWER(title) ASC",
        "title_desc": "LOWER(title) DESC",
        "price_asc": "current_price ASC",
        "price_desc": "current_price DESC",
        "brand_asc": "brand IS NULL, LOWER(brand) ASC",
        "brand_desc": "brand IS NULL, LOWER(brand) DESC",
        "model_asc": "model IS NULL, LOWER(model) ASC",
        "model_desc": "model IS NULL, LOWER(model) DESC",
        "location_asc": "location IS NULL, LOWER(location) ASC",
        "location_desc": "location IS NULL, LOWER(location) DESC",
        "first_seen_asc": "first_seen ASC",
        "first_seen_desc": "first_seen DESC",
        "last_seen_asc": "last_seen ASC",
        "last_seen_desc": "last_seen DESC",
        "price_drop_asc": "(COALESCE(original_price, 0) - COALESCE(current_price, 0)) ASC",
        "price_drop_desc": "(COALESCE(original_price, 0) - COALESCE(current_price, 0)) DESC",
        # Backward-compatible aliases
        "newest": "first_seen DESC",
        "oldest": "first_seen ASC",
        "last_seen": "last_seen DESC",
        "price_drop": "(COALESCE(original_price, 0) - COALESCE(current_price, 0)) DESC",
    }
    sort = sort_map.get(filters.get("sort_by", "last_seen_desc"), "last_seen DESC")

    rows = conn.execute(
        f"SELECT * FROM listings WHERE {where} ORDER BY {sort}", params
    ).fetchall()

    result = [dict(row) for row in rows]
    if close:
        conn.close()
    return result


def get_listing(kijiji_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    close = conn is None
    if close:
        conn = get_conn()
    row = conn.execute(
        "SELECT * FROM listings WHERE kijiji_id = ?", (kijiji_id,)
    ).fetchone()
    result = dict(row) if row else None
    if close:
        conn.close()
    return result


def set_listing_hidden(kijiji_id: str, hidden: bool,
                       conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    conn.execute(
        "UPDATE listings SET is_hidden = ? WHERE kijiji_id = ?",
        (1 if hidden else 0, kijiji_id),
    )
    conn.commit()
    if close:
        conn.close()


def set_listing_starred(kijiji_id: str, starred: bool,
                        conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    conn.execute(
        "UPDATE listings SET is_starred = ? WHERE kijiji_id = ?",
        (1 if starred else 0, kijiji_id),
    )
    conn.commit()
    if close:
        conn.close()


def update_listing_brand_model(kijiji_id: str, brand: Optional[str], model: Optional[str],
                               conn: Optional[sqlite3.Connection] = None) -> bool:
    """Manually update listing brand/model and refresh MSRP from msrp_entries."""
    close = conn is None
    if close:
        conn = get_conn()

    normalized_brand = (brand or "").strip().lower() or None
    normalized_model = (model or "").strip() or None
    listing_row = conn.execute(
        "SELECT currency FROM listings WHERE kijiji_id = ?", (kijiji_id,)
    ).fetchone()
    currency = ((listing_row["currency"] if listing_row else None) or "CAD").upper()

    msrp = None
    if normalized_brand and normalized_model:
        row = conn.execute(
            """
            SELECT msrp_cad, msrp_usd
            FROM msrp_entries
            WHERE brand = ? AND LOWER(model) = LOWER(?)
            LIMIT 1
            """,
            (normalized_brand, normalized_model),
        ).fetchone()
        if row:
            msrp = row["msrp_usd"] if currency == "USD" else row["msrp_cad"]

    cursor = conn.execute(
        "UPDATE listings SET brand = ?, model = ?, msrp = ?, msrp_currency = ? WHERE kijiji_id = ?",
        (normalized_brand, normalized_model, msrp, currency if msrp is not None else None, kijiji_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()

    if close:
        conn.close()
    return updated


def delete_listing(kijiji_id: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Delete one listing and its price snapshots. Returns True if deleted."""
    close = conn is None
    if close:
        conn = get_conn()

    conn.execute("DELETE FROM price_snapshots WHERE kijiji_id = ?", (kijiji_id,))
    cursor = conn.execute("DELETE FROM listings WHERE kijiji_id = ?", (kijiji_id,))
    deleted = cursor.rowcount > 0

    if close:
        conn.commit()
        conn.close()
    return deleted


def delete_listings(kijiji_ids: list[str], conn: Optional[sqlite3.Connection] = None) -> int:
    """Delete multiple listings and their snapshots. Returns number deleted."""
    close = conn is None
    if close:
        conn = get_conn()

    deleted = 0
    for kid in kijiji_ids:
        if delete_listing(kid, conn=conn):
            deleted += 1

    if close:
        conn.commit()
        conn.close()
    return deleted


def delete_inactive_listings(conn: Optional[sqlite3.Connection] = None) -> int:
    """Delete listings already marked inactive and their related rows."""
    close = conn is None
    if close:
        conn = get_conn()

    listing_ids = [
        row["kijiji_id"]
        for row in conn.execute(
            "SELECT kijiji_id FROM listings WHERE is_active = 0"
        ).fetchall()
    ]
    # price_snapshots predates the newer cascading foreign keys, so route
    # cleanup through the dependency-aware deletion function.
    deleted = delete_listings(listing_ids, conn=conn)

    if close:
        conn.commit()
        conn.close()
    return deleted


def _stale_where(days: int, listing_status: str = "all") -> tuple[str, list[Any]]:
    clauses = ["datetime(last_seen) < datetime('now', ?)"]
    params: list[Any] = [f"-{days} days"]
    if listing_status == "active":
        clauses.append("is_active = 1")
    elif listing_status == "inactive":
        clauses.append("is_active = 0")
    return " AND ".join(clauses), params


def count_stale_listings(days: int, listing_status: str = "all",
                         conn: Optional[sqlite3.Connection] = None) -> int:
    close = conn is None
    if close:
        conn = get_conn()
    where, params = _stale_where(days, listing_status)
    count = conn.execute(
        f"SELECT COUNT(*) AS c FROM listings WHERE {where}", params
    ).fetchone()["c"]
    if close:
        conn.close()
    return count


def delete_stale_listings(days: int, listing_status: str = "all",
                          conn: Optional[sqlite3.Connection] = None) -> int:
    """Delete listings not observed within the requested age threshold."""
    close = conn is None
    if close:
        conn = get_conn()
    where, params = _stale_where(days, listing_status)
    listing_ids = [
        row["kijiji_id"]
        for row in conn.execute(
            f"SELECT kijiji_id FROM listings WHERE {where}", params
        ).fetchall()
    ]
    deleted = delete_listings(listing_ids, conn=conn)
    if close:
        conn.commit()
        conn.close()
    return deleted


def clear_database(preserve_settings: bool = True,
                   conn: Optional[sqlite3.Connection] = None) -> dict:
    """Clear listing data. Optionally clear configuration tables too."""
    close = conn is None
    if close:
        conn = get_conn()

    conn.execute("DELETE FROM deal_notifications")
    conn.execute("DELETE FROM listing_queries")
    conn.execute("DELETE FROM scrape_query_runs")
    conn.execute("DELETE FROM price_snapshots")
    conn.execute("DELETE FROM listings")
    conn.execute("DELETE FROM scrape_runs")

    result = {
        "cleared": ["deal_notifications", "listing_queries", "scrape_query_runs",
                    "price_snapshots", "listings", "scrape_runs"],
        "preserved_settings": preserve_settings,
    }

    if not preserve_settings:
        conn.execute("DELETE FROM settings")
        conn.execute("DELETE FROM search_queries")
        conn.execute("DELETE FROM brand_keywords")
        conn.execute("DELETE FROM msrp_entries")
        result["cleared"].extend(["settings", "search_queries", "brand_keywords", "msrp_entries"])
        _seed_defaults(conn)

    # Reset autoincrement counters for cleaner IDs after clear.
    conn.execute(
        "DELETE FROM sqlite_sequence WHERE name IN ('price_snapshots', 'scrape_runs', 'search_queries', 'brand_keywords', 'msrp_entries')"
    )

    if close:
        conn.commit()
        conn.close()
    return result


def get_price_history(kijiji_id: str, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    close = conn is None
    if close:
        conn = get_conn()
    rows = conn.execute(
        "SELECT price, scraped_at FROM price_snapshots WHERE kijiji_id = ? ORDER BY scraped_at",
        (kijiji_id,)
    ).fetchall()
    result = [dict(row) for row in rows]
    if close:
        conn.close()
    return result


def get_distinct_brands(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    close = conn is None
    if close:
        conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT brand FROM listings WHERE brand IS NOT NULL AND is_active = 1 ORDER BY brand"
    ).fetchall()
    result = [row["brand"] for row in rows]
    if close:
        conn.close()
    return result


def get_distinct_models(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    close = conn is None
    if close:
        conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT model FROM listings WHERE model IS NOT NULL AND is_active = 1 ORDER BY model"
    ).fetchall()
    result = [row["model"] for row in rows]
    if close:
        conn.close()
    return result


def get_distinct_locations(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    close = conn is None
    if close:
        conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT location FROM listings WHERE location IS NOT NULL AND is_active = 1 ORDER BY location"
    ).fetchall()
    result = [row["location"] for row in rows]
    if close:
        conn.close()
    return result


def get_stats(conn: Optional[sqlite3.Connection] = None) -> dict:
    close = conn is None
    if close:
        conn = get_conn()

    stats = {}
    stats["total_listings"] = conn.execute("SELECT COUNT(*) as c FROM listings").fetchone()["c"]
    stats["active_listings"] = conn.execute("SELECT COUNT(*) as c FROM listings WHERE is_active = 1").fetchone()["c"]
    stats["inactive_listings"] = stats["total_listings"] - stats["active_listings"]
    stats["stale_listings_30d"] = count_stale_listings(30, "all", conn)
    stats["total_snapshots"] = conn.execute("SELECT COUNT(*) as c FROM price_snapshots").fetchone()["c"]
    stats["total_scrape_runs"] = conn.execute("SELECT COUNT(*) as c FROM scrape_runs").fetchone()["c"]

    last_run = conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    stats["last_run"] = dict(last_run) if last_run else None

    stats["listings_with_drops"] = conn.execute(
        "SELECT COUNT(*) as c FROM listings WHERE current_price < original_price AND is_active = 1"
    ).fetchone()["c"]

    if close:
        conn.close()
    return stats
