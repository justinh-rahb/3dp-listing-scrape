"""Database layer for the 3D Printer Kijiji Deal Tracker."""

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
            missed_runs     INTEGER DEFAULT 0,
            brand           TEXT,
            model           TEXT,
            msrp            REAL,
            current_price   REAL,
            original_price  REAL
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
            search_query    TEXT
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
            msrp_cad      REAL NOT NULL,
            msrp_usd      REAL,
            retail_price  REAL,
            last_updated  TEXT,
            UNIQUE(brand, model)
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_kijiji_id ON price_snapshots(kijiji_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_scraped_at ON price_snapshots(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_listings_brand ON listings(brand);
        CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active);
        CREATE INDEX IF NOT EXISTS idx_listings_current_price ON listings(current_price);
    """)
    _ensure_schema_updates(conn)
    conn.commit()

    # Seed defaults if tables are empty
    _seed_defaults(conn)
    conn.close()


def _ensure_schema_updates(conn: sqlite3.Connection):
    """Apply additive schema updates for existing databases."""
    listing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()
    }
    if "is_hidden" not in listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN is_hidden INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_hidden ON listings(is_hidden)")


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

    # Search queries
    existing = conn.execute("SELECT COUNT(*) as c FROM search_queries").fetchone()["c"]
    if existing == 0:
        for q in DEFAULT_SEARCH_QUERIES:
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
                        "INSERT OR IGNORE INTO msrp_entries (brand, model, msrp_cad, msrp_usd) VALUES (?, ?, ?, ?)",
                        (brand, model, prices.get("msrp_cad", 0), prices.get("msrp_usd"))
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
    conn.commit()
    if close:
        conn.close()


def get_all_settings(conn: Optional[sqlite3.Connection] = None) -> dict:
    close = conn is None
    if close:
        conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    if close:
        conn.close()
    return {row["key"]: json.loads(row["value"]) for row in rows}


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
    conn.commit()
    if close:
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


def upsert_msrp_entry(brand: str, model: str, msrp_cad: float,
                      msrp_usd: Optional[float] = None,
                      retail_price: Optional[float] = None,
                      conn: Optional[sqlite3.Connection] = None) -> int:
    close = conn is None
    if close:
        conn = get_conn()
    
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    
    cursor = conn.execute("""
        INSERT INTO msrp_entries (brand, model, msrp_cad, msrp_usd, retail_price, last_updated)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(brand, model) DO UPDATE SET 
            msrp_cad = ?,
            msrp_usd = ?,
            retail_price = ?,
            last_updated = ?
    """, (brand.lower(), model, msrp_cad, msrp_usd, retail_price, now,
           msrp_cad, msrp_usd, retail_price, now))
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
            "retail_price": e.get("retail_price")
        }
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
            INSERT INTO listings (kijiji_id, url, title, description, seller_name,
                                  location, image_urls, listing_date, first_seen, last_seen,
                                  is_active, is_hidden, missed_runs, brand, model, msrp,
                                  current_price, original_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?, ?, ?, ?)
        """, (
            listing_data["kijiji_id"],
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
            listing_data.get("price"),
            listing_data.get("price"),
        ))
    else:
        conn.execute("""
            UPDATE listings SET
                url = ?, title = ?, description = COALESCE(?, description),
                seller_name = COALESCE(?, seller_name),
                location = COALESCE(?, location),
                image_urls = CASE WHEN ? != '[]' THEN ? ELSE image_urls END,
                listing_date = COALESCE(?, listing_date),
                last_seen = ?, is_active = 1, missed_runs = 0,
                brand = COALESCE(?, brand), model = COALESCE(?, model),
                msrp = COALESCE(?, msrp),
                current_price = COALESCE(?, current_price)
            WHERE kijiji_id = ?
        """, (
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
            listing_data.get("price"),
            listing_data["kijiji_id"],
        ))

    conn.commit()
    if close:
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
    conn.commit()
    if close:
        conn.close()


def start_scrape_run(search_query: str = "", conn: Optional[sqlite3.Connection] = None) -> int:
    close = conn is None
    if close:
        conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "INSERT INTO scrape_runs (started_at, search_query) VALUES (?, ?)",
        (now, search_query)
    )
    conn.commit()
    run_id = cursor.lastrowid
    if close:
        conn.close()
    return run_id


def finish_scrape_run(run_id: int, listings_found: int, new_listings: int,
                      price_changes: int, errors: int,
                      conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if close:
        conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE scrape_runs SET finished_at = ?, listings_found = ?,
               new_listings = ?, price_changes = ?, errors = ?
        WHERE id = ?
    """, (now, listings_found, new_listings, price_changes, errors, run_id))
    conn.commit()
    if close:
        conn.close()


def increment_missed_runs(seen_ids: set, conn: Optional[sqlite3.Connection] = None):
    """Increment missed_runs for active listings not seen, mark inactive if threshold hit."""
    close = conn is None
    if close:
        conn = get_conn()

    inactive_threshold = get_setting("inactive_threshold", 3, conn)

    active = conn.execute(
        "SELECT kijiji_id FROM listings WHERE is_active = 1"
    ).fetchall()

    for row in active:
        kid = row["kijiji_id"]
        if kid not in seen_ids:
            conn.execute(
                "UPDATE listings SET missed_runs = missed_runs + 1 WHERE kijiji_id = ?",
                (kid,)
            )
            conn.execute("""
                UPDATE listings SET is_active = 0
                WHERE kijiji_id = ? AND missed_runs >= ?
            """, (kid, inactive_threshold))

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

    if filters.get("active_only", True):
        where_clauses.append("is_active = 1")

    if filters.get("brand"):
        where_clauses.append("brand = ?")
        params.append(filters["brand"])

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
        "price_asc": "current_price ASC",
        "price_desc": "current_price DESC",
        "newest": "first_seen DESC",
        "oldest": "first_seen ASC",
        "last_seen": "last_seen DESC",
        "price_drop": "(original_price - current_price) DESC",
    }
    sort = sort_map.get(filters.get("sort_by", "last_seen"), "last_seen DESC")

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
