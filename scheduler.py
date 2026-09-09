"""Background scheduler and shared scrape logic."""

import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler

import db
from notifier import send_webhook_event
from models import ScrapeOutcome
from scraper import KijijiScraper, RetailScraper
from tracker import compute_deals, detect_brand, detect_model, lookup_msrp

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None
_lock = threading.Lock()
_run_lock = threading.Lock()
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
        return send_webhook_event(event_type, payload, settings)
    except Exception as e:
        logger.warning(f"Webhook send failed for event={event_type}: {e}")
        return False


def run_scrape(max_pages: Optional[int] = None,
               query_filter: Optional[str] = None,
               query_id: Optional[int] = None) -> dict:
    """Run a full scrape cycle. Shared between CLI and scheduler.

    Returns a summary dict with counts.
    """
    global _last_result, _is_running

    if not _run_lock.acquire(blocking=False):
        return {"error": "Scrape already in progress"}

    distributed_lock_acquired = False
    _is_running = True
    conn = None
    settings = {}
    run_id = None
    queries = []
    total_found = 0
    total_new = 0
    total_price_changes = 0
    failures = []
    queries_succeeded = 0
    try:
        distributed_lock_acquired = db.try_acquire_app_lock("scrape")
        if not distributed_lock_acquired:
            return {"error": "Scrape already in progress in another process"}
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

        run_id = db.start_scrape_run(
            search_query=", ".join(q["label"] for q in queries),
            queries_total=len(queries),
            conn=conn,
        )

        for q in queries:
            logger.info(f"Searching: {q['label']} ...")
            source = _source_from_url(q["url"])
            query_run_id = db.start_scrape_query_run(run_id, q, conn=conn)
            try:
                if source == "kijiji":
                    outcome = kijiji_scraper.scrape_search(q["url"], max_pages=max_pages)
                else:
                    outcome = retail_scraper.scrape_url(q["url"])
            except Exception as e:
                logger.exception(f"Unexpected error scraping {q['label']}")
                outcome = ScrapeOutcome(
                    status="failed",
                    requested_url=q["url"],
                    failed_url=q["url"],
                    error_type=type(e).__name__,
                    error_message=str(e),
                )

            listings = outcome.listings
            logger.info(f"  Found {len(listings)} listings")
            query_seen_ids = set()
            query_new = 0
            query_price_changes = 0
            now = datetime.now(timezone.utc).isoformat()

            try:
                for listing in listings:
                    query_seen_ids.add(listing.kijiji_id)

                    brand = detect_brand(listing.title, listing.description or "")
                    model = detect_model(listing.title, listing.description or "", brand)
                    msrp = lookup_msrp(brand, model, listing.currency)

                    existing = db.get_listing(listing.kijiji_id, conn=conn)
                    if existing and existing["current_price"] is not None and listing.price is not None:
                        old_usd = _to_usd(existing["current_price"], existing["currency"], fx_rates)
                        new_usd = _to_usd(listing.price, listing.currency, fx_rates)
                        if old_usd is not None and new_usd is not None and round(old_usd, 2) != round(new_usd, 2):
                            query_price_changes += 1
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
                        "msrp_currency": listing.currency.upper() if msrp is not None else None,
                    }

                    is_new = db.upsert_listing(listing_data, conn=conn)
                    if is_new:
                        query_new += 1
                    db.record_listing_query(listing.kijiji_id, q["id"], now, conn=conn)
                    db.add_price_snapshot(listing.kijiji_id, listing.price, now, conn=conn)

                if outcome.can_mark_missing:
                    db.finalize_query_visibility(q["id"], query_seen_ids, conn=conn)
            except Exception as exc:
                conn.rollback()
                logger.exception(f"Error storing results for {q['label']}")
                outcome = ScrapeOutcome(
                    status="failed",
                    requested_url=q["url"],
                    failed_url=q["url"],
                    error_type=type(exc).__name__,
                    error_message=f"Persistence error: {exc}",
                )
                query_seen_ids.clear()
                query_new = 0
                query_price_changes = 0

            total_found += len(query_seen_ids)
            total_new += query_new
            total_price_changes += query_price_changes

            db.finish_scrape_query_run(
                query_run_id,
                status=outcome.status,
                listings_found=len(query_seen_ids),
                new_listings=query_new,
                price_changes=query_price_changes,
                pages_attempted=outcome.pages_attempted,
                pages_completed=outcome.pages_completed,
                failed_url=outcome.failed_url,
                http_status=outcome.http_status,
                error_type=outcome.error_type,
                error_message=outcome.error_message,
                conn=conn,
            )

            if outcome.succeeded:
                queries_succeeded += 1
            else:
                failure = {
                    "query_id": q["id"],
                    "label": q["label"],
                    "url": q["url"],
                    "status": outcome.status,
                    "failed_url": outcome.failed_url,
                    "http_status": outcome.http_status,
                    "error_type": outcome.error_type,
                    "error_message": outcome.error_message,
                }
                failures.append(failure)
                logger.error("Query failed: %s", failure)

        total_errors = len(failures)
        if not queries:
            run_status = "empty"
        elif total_errors == 0:
            run_status = "success"
        elif queries_succeeded == 0 and total_found == 0:
            run_status = "failed"
        else:
            run_status = "partial"
        db.finish_scrape_run(
            run_id, total_found, total_new, total_price_changes, total_errors,
            queries_succeeded=queries_succeeded, queries_failed=total_errors,
            status=run_status, conn=conn,
        )

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
        qualifying_deals = db.get_unnotified_deals(qualifying_deals)[:deal_batch_size]

        conn.close()
        conn = None

        result = {
            "run_id": run_id,
            "status": run_status,
            "found": total_found,
            "new": total_new,
            "price_changes": total_price_changes,
            "errors": total_errors,
            "queries_total": len(queries),
            "queries_succeeded": queries_succeeded,
            "queries_failed": total_errors,
            "failures": failures,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _last_result = result
        if total_errors == 0:
            _emit_event("scrape_completed", result, settings)
        else:
            _emit_event("scrape_failed", {
                "error": f"{total_errors} of {len(queries)} queries failed during scrape run",
                **result,
            }, settings)
        if qualifying_deals:
            payload_deals = [
                {key: value for key, value in deal.items() if key != "_fingerprint"}
                for deal in qualifying_deals
            ]
            sent = _emit_event("new_deal_detected", {
                "count": len(qualifying_deals),
                "deals": payload_deals,
                "thresholds": {
                    "max_price_to_retail_ratio": deal_ratio_max,
                    "min_drop_pct": deal_drop_min,
                },
                "finished_at": result["finished_at"],
            }, settings)
            if sent:
                db.mark_deals_notified(qualifying_deals)
        logger.info(f"Scrape done: {result}")
        return result

    except Exception as e:
        logger.exception("Scrape failed")
        if run_id is not None and conn:
            try:
                db.fail_running_query_runs(
                    run_id, type(e).__name__, f"Run aborted: {e}", conn=conn
                )
                db.finish_scrape_run(
                    run_id, total_found, total_new, total_price_changes,
                    max(1, len(failures)), queries_succeeded=queries_succeeded,
                    queries_failed=max(1, len(failures)),
                    status="failed", conn=conn,
                )
            except Exception:
                logger.exception("Failed to record terminal scrape status")
        if conn:
            conn.close()
        _emit_event("scrape_failed", {
            "error": str(e),
            "run_id": run_id,
            "queries_total": len(queries),
            "queries_succeeded": queries_succeeded,
            "queries_failed": max(1, len(failures)),
            "found": total_found,
            "new": total_new,
            "price_changes": total_price_changes,
            "failures": failures,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }, settings)
        return {"error": str(e)}
    finally:
        _is_running = False
        if distributed_lock_acquired:
            try:
                db.release_app_lock("scrape")
            except Exception:
                logger.exception("Failed to release scrape lock")
        _run_lock.release()


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
    if _is_running or db.is_app_lock_active("scrape"):
        return {"error": "Scrape already in progress"}
    thread = threading.Thread(target=run_scrape, daemon=True)
    thread.start()
    return {"status": "triggered"}


def trigger_query(query_id: int):
    """Trigger an immediate scrape for a single query id."""
    if _is_running or db.is_app_lock_active("scrape"):
        return {"error": "Scrape already in progress"}
    thread = threading.Thread(target=run_scrape, kwargs={"query_id": query_id}, daemon=True)
    thread.start()
    return {"status": "triggered", "query_id": query_id}


def get_status() -> dict:
    """Get scheduler status."""
    running = _scheduler is not None and _scheduler.running if _scheduler else False
    last_result = _last_result
    if last_result is None:
        recent = db.get_recent_scrape_runs(limit=1)
        if recent:
            run = recent[0]
            last_result = {
                "run_id": run["id"],
                "status": run["status"],
                "found": run["listings_found"],
                "new": run["new_listings"],
                "price_changes": run["price_changes"],
                "errors": run["errors"],
                "queries_total": run["queries_total"],
                "queries_succeeded": run["queries_succeeded"],
                "queries_failed": run["queries_failed"],
                "finished_at": run["finished_at"],
            }

    status = {
        "running": running,
        "scraping": _is_running or db.is_app_lock_active("scrape"),
        "last_result": last_result,
    }

    if running and _scheduler:
        job = _scheduler.get_job("scrape_job")
        if job and job.next_run_time:
            status["next_run"] = job.next_run_time.isoformat()
        interval = db.get_setting("scrape_interval_hours", 6)
        status["interval_hours"] = interval

    return status
