#!/usr/bin/env python3
"""CLI for the 3D Printer Kijiji Deal Tracker."""

import logging
import os
import sys

import click

import db
from scheduler import run_scrape
from tracker import compute_deals


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_HOST = os.environ.get("HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("PORT", "5000"))
DEFAULT_RELOAD = _as_bool(os.environ.get("RELOAD"), default=False)
DEFAULT_WORKERS = int(os.environ.get("WORKERS", "1"))


def run_server(host: str, port: int, reload: bool, workers: int):
    import uvicorn

    if reload and workers > 1:
        workers = 1

    click.echo(f"Starting dashboard on http://{host}:{port}")
    click.echo(f"API docs at http://{host}:{port}/docs")
    uvicorn.run("app:app", host=host, port=port, reload=reload, workers=workers)


@click.group(invoke_without_command=True)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.pass_context
def cli(ctx, verbose):
    """3D Printer Kijiji Deal Tracker"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    db.init_db()

    # Server-first UX: running without a subcommand starts the dashboard.
    if ctx.invoked_subcommand is None:
        run_server(host=DEFAULT_HOST, port=DEFAULT_PORT, reload=DEFAULT_RELOAD, workers=DEFAULT_WORKERS)


@cli.command()
@click.option("--query", "-q", help="Run only a specific search query label")
@click.option("--max-pages", default=None, type=int, help="Max pages per query (default: from settings)")
def scrape(query, max_pages):
    """Scrape Kijiji for 3D printer listings."""
    result = run_scrape(max_pages=max_pages, query_filter=query)

    if "error" in result:
        click.echo(f"Error: {result['error']}")
        sys.exit(1)

    click.echo(f"\nDone! Found: {result['found']}, New: {result['new']}, "
               f"Price changes: {result['price_changes']}, Errors: {result['errors']}")


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
@click.option("--port", default=DEFAULT_PORT, help="Port to serve on")
@click.option("--host", default=DEFAULT_HOST, help="Host to bind to")
@click.option("--reload/--no-reload", default=DEFAULT_RELOAD, help="Enable auto-reload")
@click.option("--workers", default=DEFAULT_WORKERS, type=int, help="Number of worker processes")
def serve(port, host, reload, workers):
    """Start the web dashboard."""
    run_server(host=host, port=port, reload=reload, workers=workers)


@cli.command()
@click.option("--port", default=5000, help="Port to serve on")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
def dev(port, host):
    """Start the web dashboard in development mode (auto-reload on)."""
    run_server(host=host, port=port, reload=True, workers=1)


@cli.command()
def update_retail_prices():
    """Update retail prices from Aurora Tech Channel."""
    from aurora_scraper import update_retail_prices_from_aurora
    
    click.echo("Fetching current retail prices from Aurora Tech Channel...")
    try:
        update_retail_prices_from_aurora(delay=1.0)
        click.echo("✓ Retail prices updated successfully!")
    except Exception as e:
        click.echo(f"✗ Error updating retail prices: {e}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
