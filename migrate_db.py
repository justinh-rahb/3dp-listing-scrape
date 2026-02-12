#!/usr/bin/env python3
"""Database migration script to add retail_price and last_updated columns."""

import logging
import sqlite3
import sys
from datetime import datetime, timezone

from config import DB_PATH

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def migrate_db(db_path: str = DB_PATH):
    """Add retail_price and last_updated columns to msrp_entries table if they don't exist."""
    
    logger.info(f"Migrating database: {db_path}")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Check if columns already exist
        cursor.execute("PRAGMA table_info(msrp_entries)")
        columns = {row[1] for row in cursor.fetchall()}
        
        needs_migration = False
        
        if 'retail_price' not in columns:
            logger.info("Adding retail_price column...")
            cursor.execute("ALTER TABLE msrp_entries ADD COLUMN retail_price REAL")
            needs_migration = True
        else:
            logger.info("✓ retail_price column already exists")
        
        if 'last_updated' not in columns:
            logger.info("Adding last_updated column...")
            cursor.execute("ALTER TABLE msrp_entries ADD COLUMN last_updated TEXT")
            
            # Set initial timestamp for existing rows
            now = datetime.now(timezone.utc).isoformat()
            cursor.execute("UPDATE msrp_entries SET last_updated = ? WHERE last_updated IS NULL", (now,))
            needs_migration = True
        else:
            logger.info("✓ last_updated column already exists")
        
        if needs_migration:
            conn.commit()
            logger.info("✓ Database migration completed successfully!")
        else:
            logger.info("✓ Database is already up to date!")
        
    except sqlite3.Error as e:
        logger.error(f"✗ Migration failed: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_db()
