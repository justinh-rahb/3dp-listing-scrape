"""Background scheduler and shared scrape logic."""

import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler

import db
from notifier import send_webhook_event
from scraper import KijijiScraper, RetailScraper
from tracker import compute_deals, detect_brand, detect_model, lookup_msrp

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None
_lock = threading.Lock()
_last_result: Optional[dict] = None
_is_running = False


def _source_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "kijiji.ca" in host:
        return "kijiji"
    if "sovol3d.com" in host:
        return "sovol"
    if "formbot3d.com" in host:
        return "formbot"
    if "qidi3d.com" in host:
        return "qidi3d"
    return "unknown"


def _to_usd(price: Optional[float], currency: Optional[str], fx_rates: dict) -> Optional[float]:
    if price is None:
        return None
    curr = (currency or "USD").upper()
    if curr == "USD":
        return float(price)
    rate = fx_rates.get(curr)
    if rate in (None, 0):
        return None
    return float(price) * float(rate)


def _emit_event(event_type: str, payload: dict, settings: dict):
    try:
        send_webhook_event(event_type, payload, settings)
    except Exception as e:
        logger.warning(f"Webhook send failed for event={event_type}: {e}")


def run_scrape(max_pages: Optional[int] = None,
               query_filter: Optional[str] = None,
               query_id: Optional[int] = None) -> dict:
    """Run a full scrape cycle. Shared between CLI and scheduler.

    Returns a summary dict with counts.
    """
    global _last_result, _is_running

    if _is_running:
        return {"error": "Scrape already in progress"}

    _is_running = True
    conn = None
    settings = {}
    try:
        conn = db.get_conn()

        # Read settings from DB
        settings = db.get_all_settings(conn)
        if max_pages is None:
            max_pages = settings.get("max_pages_per_query", 5)
        delay_min = settings.get("request_delay_min", 2.0)
        delay_max = settings.get("request_delay_max", 5.0)
        fx_rates = settings.get("fx_rates_to_usd", {"USD": 1.0})

        kijiji_scraper = KijijiScraper(delay_min=delay_min, delay_max=delay_max, max_pages=max_pages)
        retail_scraper = RetailScraper(delay_min=delay_min, delay_max=delay_max)

        # Get enabled search queries from DB
        queries = db.get_search_queries(enabled_only=True, conn=conn)
        if query_filter:
            queries = [q for q in queries if q["label"] == query_filter]
        if query_id is not None:
            queries = [q for q in queries if q["id"] == query_id]

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
            logger.info(f"Searching: {q['label']} ...")
            source = _source_from_url(q["url"])
            try:
                if source == "kijiji":
                    listings = kijiji_scraper.scrape_search(q["url"], max_pages=max_pages)
                else:
                    listings = retail_scraper.scrape_url(q["url"])
            except Exception as e:
                logger.error(f"Error scraping {q['label']}: {e}")
                total_errors += 1
                continue

            logger.info(f"  Found {len(listings)} listings")
            total_found += len(listings)

            for listing in listings:
                all_seen_ids.add(listing.kijiji_id)

                brand = detect_brand(listing.title, listing.description or "")
                model = detect_model(listing.title, listing.description or "", brand)
                msrp = lookup_msrp(brand, model)

                existing = db.get_listing(listing.kijiji_id, conn=conn)
                if existing and existing["current_price"] is not None and listing.price is not None:
                    old_usd = _to_usd(existing["current_price"], existing["currency"], fx_rates)
                    new_usd = _to_usd(listing.price, listing.currency, fx_rates)
                    if old_usd is not None and new_usd is not None and round(old_usd, 2) != round(new_usd, 2):
                        total_price_changes += 1
                        direction = "down" if new_usd < old_usd else "up"
                        logger.info(
                            f"  USD price {direction}: {listing.title[:50]} "
                            f"${old_usd:.2f} -> ${new_usd:.2f}"
                        )

                listing_data = {
                    "kijiji_id": listing.kijiji_id,
                    "source": listing.source or source,
                    "url": listing.url,
                    "title": listing.title,
                    "price": listing.price,
                    "currency": listing.currency,
                    "nominal_price": listing.nominal_price,
                    "on_sale": listing.on_sale,
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

        db.increment_missed_runs(all_seen_ids, conn=conn)
        db.finish_scrape_run(run_id, total_found, total_new, total_price_changes, total_errors, conn=conn)

        deal_ratio_max = float(settings.get("webhook_deal_max_price_to_retail_ratio", 0.9))
        deal_drop_min = float(settings.get("webhook_deal_min_drop_pct", 15.0))
        deal_batch_size = int(settings.get("webhook_deal_batch_size", 5))
        qualifying_deals = []
        for deal in compute_deals(db.get_listings({"active_only": True}, conn=conn)):
            ratio_match = deal.price_to_retail_ratio is not None and deal.price_to_retail_ratio <= deal_ratio_max
            drop_match = deal.price_drop_pct >= deal_drop_min
            if ratio_match or drop_match:
                qualifying_deals.append({
                    "kijiji_id": deal.kijiji_id,
                    "title": deal.title,
                    "url": deal.url,
                    "source": deal.source,
                    "currency": deal.currency,
                    "current_price": round(deal.current_price, 2),
                    "price_drop_pct": round(deal.price_drop_pct, 2),
                    "price_to_retail_ratio": round(deal.price_to_retail_ratio, 4) if deal.price_to_retail_ratio is not None else None,
                })
            if len(qualifying_deals) >= deal_batch_size:
                break

        conn.close()
        conn = None

        result = {
            "found": total_found,
            "new": total_new,
            "price_changes": total_price_changes,
            "errors": total_errors,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _last_result = result
        _emit_event("scrape_completed", result, settings)
        if total_errors > 0:
            _emit_event("scrape_failed", {
                "error": f"{total_errors} query errors during scrape run",
                **result,
            }, settings)
        if qualifying_deals:
            _emit_event("new_deal_detected", {
                "count": len(qualifying_deals),
                "deals": qualifying_deals,
                "thresholds": {
                    "max_price_to_retail_ratio": deal_ratio_max,
                    "min_drop_pct": deal_drop_min,
                },
                "finished_at": result["finished_at"],
            }, settings)
        logger.info(f"Scrape done: {result}")
        return result

    except Exception as e:
        logger.error(f"Scrape failed: {e}")
        if conn:
            conn.close()
        _emit_event("scrape_failed", {
            "error": str(e),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }, settings)
        return {"error": str(e)}
    finally:
        _is_running = False


def _scrape_job():
    """APScheduler job wrapper."""
    logger.info("Scheduled scrape starting...")
    run_scrape()


def start_scheduler(interval_hours: Optional[float] = None):
    """Start the background scheduler."""
    global _scheduler

    with _lock:
        if _scheduler and _scheduler.running:
            _scheduler.shutdown(wait=False)

        if interval_hours is None:
            interval_hours = db.get_setting("scrape_interval_hours", 6)

        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            _scrape_job,
            "interval",
            hours=interval_hours,
            id="scrape_job",
            replace_existing=True,
        )
        _scheduler.start()
        db.set_setting("scheduler_enabled", True)
        logger.info(f"Scheduler started: scraping every {interval_hours}h")


def stop_scheduler(disable: bool = True):
    """Stop the background scheduler.

    Set disable=False to stop only in-memory scheduling without changing the
    persisted scheduler setting.
    """
    global _scheduler

    with _lock:
        if _scheduler and _scheduler.running:
            _scheduler.shutdown(wait=False)
            _scheduler = None
        if disable:
            db.set_setting("scheduler_enabled", False)
        logger.info("Scheduler stopped")


def trigger_now():
    """Trigger an immediate scrape (runs in a background thread)."""
    if _is_running:
        return {"error": "Scrape already in progress"}
    thread = threading.Thread(target=run_scrape, daemon=True)
    thread.start()
    return {"status": "triggered"}


def trigger_query(query_id: int):
    """Trigger an immediate scrape for a single query id."""
    if _is_running:
        return {"error": "Scrape already in progress"}
    thread = threading.Thread(target=run_scrape, kwargs={"query_id": query_id}, daemon=True)
    thread.start()
    return {"status": "triggered", "query_id": query_id}


def get_status() -> dict:
    """Get scheduler status."""
    running = _scheduler is not None and _scheduler.running if _scheduler else False

    status = {
        "running": running,
        "scraping": _is_running,
        "last_result": _last_result,
    }

    if running and _scheduler:
        job = _scheduler.get_job("scrape_job")
        if job and job.next_run_time:
            status["next_run"] = job.next_run_time.isoformat()
        interval = db.get_setting("scrape_interval_hours", 6)
        status["interval_hours"] = interval

    return status
