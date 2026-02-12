"""Database layer for the 3D Printer Kijiji Deal Tracker."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config import DB_PATH


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

        CREATE INDEX IF NOT EXISTS idx_snapshots_kijiji_id ON price_snapshots(kijiji_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_scraped_at ON price_snapshots(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_listings_brand ON listings(brand);
        CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active);
        CREATE INDEX IF NOT EXISTS idx_listings_current_price ON listings(current_price);
    """)
    conn.commit()
    conn.close()


def upsert_listing(listing_data: dict, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Insert or update a listing. Returns True if this is a new listing."""
    close = False
    if conn is None:
        conn = get_conn()
        close = True

    now = datetime.now(timezone.utc).isoformat()
    image_urls_json = json.dumps(listing_data.get("image_urls", []))

    # Check if listing exists
    existing = conn.execute(
        "SELECT kijiji_id, current_price FROM listings WHERE kijiji_id = ?",
        (listing_data["kijiji_id"],)
    ).fetchone()

    is_new = existing is None

    if is_new:
        conn.execute("""
            INSERT INTO listings (kijiji_id, url, title, description, seller_name,
                                  location, image_urls, listing_date, first_seen, last_seen,
                                  is_active, missed_runs, brand, model, msrp,
                                  current_price, original_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?)
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
    close = False
    if conn is None:
        conn = get_conn()
        close = True

    conn.execute(
        "INSERT OR IGNORE INTO price_snapshots (kijiji_id, price, scraped_at) VALUES (?, ?, ?)",
        (kijiji_id, price, scraped_at)
    )
    conn.commit()
    if close:
        conn.close()


def start_scrape_run(search_query: str = "", conn: Optional[sqlite3.Connection] = None) -> int:
    close = False
    if conn is None:
        conn = get_conn()
        close = True

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
    close = False
    if conn is None:
        conn = get_conn()
        close = True

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
    from config import INACTIVE_THRESHOLD

    close = False
    if conn is None:
        conn = get_conn()
        close = True

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
            """, (kid, INACTIVE_THRESHOLD))

    conn.commit()
    if close:
        conn.close()


def get_listings(filters: Optional[dict] = None,
                 conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    close = False
    if conn is None:
        conn = get_conn()
        close = True

    filters = filters or {}
    where_clauses = []
    params = []

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
    close = False
    if conn is None:
        conn = get_conn()
        close = True

    row = conn.execute(
        "SELECT * FROM listings WHERE kijiji_id = ?", (kijiji_id,)
    ).fetchone()

    result = dict(row) if row else None
    if close:
        conn.close()
    return result


def get_price_history(kijiji_id: str, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    close = False
    if conn is None:
        conn = get_conn()
        close = True

    rows = conn.execute(
        "SELECT price, scraped_at FROM price_snapshots WHERE kijiji_id = ? ORDER BY scraped_at",
        (kijiji_id,)
    ).fetchall()

    result = [dict(row) for row in rows]
    if close:
        conn.close()
    return result


def get_distinct_brands(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    close = False
    if conn is None:
        conn = get_conn()
        close = True

    rows = conn.execute(
        "SELECT DISTINCT brand FROM listings WHERE brand IS NOT NULL AND is_active = 1 ORDER BY brand"
    ).fetchall()

    result = [row["brand"] for row in rows]
    if close:
        conn.close()
    return result


def get_distinct_locations(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    close = False
    if conn is None:
        conn = get_conn()
        close = True

    rows = conn.execute(
        "SELECT DISTINCT location FROM listings WHERE location IS NOT NULL AND is_active = 1 ORDER BY location"
    ).fetchall()

    result = [row["location"] for row in rows]
    if close:
        conn.close()
    return result


def get_stats(conn: Optional[sqlite3.Connection] = None) -> dict:
    close = False
    if conn is None:
        conn = get_conn()
        close = True

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
