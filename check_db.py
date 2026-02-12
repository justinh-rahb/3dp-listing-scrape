#!/usr/bin/env python3
"""Quick script to check database MSRP entries."""

import sqlite3

conn = sqlite3.connect('listings.db')
conn.row_factory = sqlite3.Row

print("\nCreality entries:")
print("=" * 80)
rows = conn.execute("""
    SELECT brand, model, msrp_cad, msrp_usd, retail_price 
    FROM msrp_entries 
    WHERE brand = 'creality' 
    ORDER BY model
""").fetchall()

for row in rows:
    print(f"{row['model']:30s} | CAD: ${row['msrp_cad']:6.0f} | USD: ${row['msrp_usd'] or 0:6.0f} | Retail: ${row['retail_price'] or 0:6.0f}")

print(f"\nTotal entries: {len(rows)}")

print("\n\nAll brands summary:")
print("=" * 80)
summary = conn.execute("""
    SELECT brand, COUNT(*) as count
    FROM msrp_entries
    GROUP BY brand
    ORDER BY brand
""").fetchall()

for row in summary:
    print(f"{row['brand']:15s}: {row['count']:3d} models")

conn.close()
