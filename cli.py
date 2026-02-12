#!/usr/bin/env python3
"""CLI for the 3D Printer Kijiji Deal Tracker."""

import logging
import sys
from datetime import datetime, timezone

import click

import db
from config import SEARCH_QUERIES
from scraper import KijijiScraper
from tracker import compute_deals, detect_brand, detect_model, lookup_msrp


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def cli(verbose):
    """3D Printer Kijiji Deal Tracker"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    db.init_db()


@cli.command()
@click.option("--query", "-q", help="Run only a specific search query label")
@click.option("--max-pages", default=5, help="Max pages per query")
@click.option("--detail/--no-detail", default=False, help="Scrape individual listing pages for full details")
def scrape(query, max_pages, detail):
    """Scrape Kijiji for 3D printer listings."""
    scraper = KijijiScraper()
    conn = db.get_conn()

    queries = SEARCH_QUERIES
    if query:
        queries = [q for q in queries if q["label"] == query]
        if not queries:
            click.echo(f"Unknown query label: {query}")
            click.echo(f"Available: {', '.join(q['label'] for q in SEARCH_QUERIES)}")
            sys.exit(1)

    total_found = 0
    total_new = 0
    total_price_changes = 0
    total_errors = 0
    all_seen_ids = set()

    run_id = db.start_scrape_run(
        search_query=", ".join(q["label"] for q in queries),
        conn=conn,
    )

    now = datetime.now(timezone.utc).isoformat()

    for q in queries:
        click.echo(f"\nSearching: {q['label']} ...")
        try:
            listings = scraper.scrape_search(q["url"], max_pages=max_pages)
        except Exception as e:
            click.echo(f"  Error: {e}")
            total_errors += 1
            continue

        click.echo(f"  Found {len(listings)} listings")
        total_found += len(listings)

        for listing in listings:
            all_seen_ids.add(listing.kijiji_id)

            # Detect brand and model
            brand = detect_brand(listing.title, listing.description or "")
            model = detect_model(listing.title, listing.description or "", brand)
            msrp = lookup_msrp(brand, model)

            # Check for price change
            existing = db.get_listing(listing.kijiji_id, conn=conn)
            price_changed = False
            if existing and existing["current_price"] is not None and listing.price is not None:
                if existing["current_price"] != listing.price:
                    price_changed = True
                    total_price_changes += 1
                    direction = "↓" if listing.price < existing["current_price"] else "↑"
                    click.echo(
                        f"  Price change {direction}: {listing.title[:50]} "
                        f"${existing['current_price']:.0f} -> ${listing.price:.0f}"
                    )

            listing_data = {
                "kijiji_id": listing.kijiji_id,
                "url": listing.url,
                "title": listing.title,
                "price": listing.price,
                "description": listing.description,
                "seller_name": listing.seller_name,
                "location": listing.location,
                "listing_date": listing.listing_date,
                "image_urls": listing.image_urls,
                "brand": brand,
                "model": model,
                "msrp": msrp,
            }

            is_new = db.upsert_listing(listing_data, conn=conn)
            if is_new:
                total_new += 1

            db.add_price_snapshot(listing.kijiji_id, listing.price, now, conn=conn)

            # Optionally fetch detail page for new listings
            if detail and is_new:
                detail_data = scraper.scrape_listing_detail(listing.url)
                if detail_data:
                    # Update with detail info
                    for key in ("description", "seller_name", "listing_date"):
                        if detail_data.get(key):
                            listing_data[key] = detail_data[key]
                    if detail_data.get("image_urls"):
                        listing_data["image_urls"] = detail_data["image_urls"]
                    db.upsert_listing(listing_data, conn=conn)

    # Mark listings not seen
    db.increment_missed_runs(all_seen_ids, conn=conn)

    db.finish_scrape_run(
        run_id, total_found, total_new, total_price_changes, total_errors, conn=conn
    )
    conn.close()

    click.echo(f"\nDone! Found: {total_found}, New: {total_new}, "
               f"Price changes: {total_price_changes}, Errors: {total_errors}")


@cli.command()
@click.option("--limit", "-n", default=20, help="Number of deals to show")
def deals(limit):
    """Show the best current deals."""
    listings = db.get_listings({"active_only": True})
    deal_list = compute_deals(listings)

    if not deal_list:
        click.echo("No deals found. Run 'scrape' first, then run it again later to detect price drops.")
        return

    click.echo(f"\nTop {min(limit, len(deal_list))} Deals:\n")
    click.echo(f"{'Title':<50} {'Price':>8} {'Drop':>8} {'Drop%':>6} {'Days':>5} {'Brand':<10}")
    click.echo("-" * 95)

    for deal in deal_list[:limit]:
        click.echo(
            f"{deal.title[:49]:<50} "
            f"${deal.current_price:>7.0f} "
            f"${deal.price_drop_abs:>7.0f} "
            f"{deal.price_drop_pct:>5.1f}% "
            f"{deal.days_on_market:>5} "
            f"{(deal.brand or ''):>10}"
        )


@cli.command()
def stats():
    """Show database statistics."""
    s = db.get_stats()
    click.echo(f"\nDatabase Statistics:")
    click.echo(f"  Total listings:      {s['total_listings']}")
    click.echo(f"  Active listings:     {s['active_listings']}")
    click.echo(f"  Price snapshots:     {s['total_snapshots']}")
    click.echo(f"  Scrape runs:         {s['total_scrape_runs']}")
    click.echo(f"  Listings with drops: {s['listings_with_drops']}")

    if s["last_run"]:
        run = s["last_run"]
        click.echo(f"\n  Last scrape: {run['started_at']}")
        click.echo(f"    Found: {run['listings_found']}, New: {run['new_listings']}, "
                    f"Price changes: {run['price_changes']}")


@cli.command()
@click.option("--port", default=5000, help="Port to serve on")
@click.option("--debug/--no-debug", default=True)
def serve(port, debug):
    """Start the web dashboard."""
    from app import app
    click.echo(f"Starting dashboard on http://localhost:{port}")
    app.run(debug=debug, port=port)


if __name__ == "__main__":
    cli()
