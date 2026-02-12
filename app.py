"""FastAPI web dashboard for the 3D Printer Kijiji Deal Tracker."""

import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import db
import scheduler
from tracker import compute_deals

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    db.init_db()
    if db.get_setting("scheduler_enabled", False):
        scheduler.start_scheduler()
        stats = db.get_stats()
        if stats["total_scrape_runs"] == 0:
            scheduler.trigger_now()
    yield
    scheduler.stop_scheduler(disable=False)


app = FastAPI(title="3DP Deal Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def from_json_filter(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


templates.env.filters["from_json"] = from_json_filter


# ── Page Routes ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, brand: Optional[str] = None,
                min_price: Optional[float] = None, max_price: Optional[float] = None,
                location: Optional[str] = None, search: Optional[str] = None,
                active_only: str = "1", sort_by: str = "last_seen"):
    filters = {
        "brand": brand,
        "min_price": min_price,
        "max_price": max_price,
        "location": location,
        "search": search,
        "active_only": active_only == "1",
        "sort_by": sort_by,
    }
    listings = db.get_listings(filters)
    brands = db.get_distinct_brands()
    stats = db.get_stats()
    sched_status = scheduler.get_status()
    return templates.TemplateResponse("index.html", {
        "request": request, "listings": listings, "brands": brands,
        "filters": filters, "stats": stats, "scheduler": sched_status,
    })


@app.get("/listing/{kijiji_id}", response_class=HTMLResponse)
async def listing_detail(request: Request, kijiji_id: str):
    listing = db.get_listing(kijiji_id)
    if not listing:
        return HTMLResponse("Listing not found", status_code=404)
    price_history = db.get_price_history(kijiji_id)
    return templates.TemplateResponse("listing.html", {
        "request": request, "listing": listing, "price_history": price_history,
    })


@app.get("/deals", response_class=HTMLResponse)
async def deals_page(request: Request):
    listings = db.get_listings({"active_only": True})
    deal_list = compute_deals(listings)
    return templates.TemplateResponse("deals.html", {
        "request": request, "deals": deal_list,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    settings = db.get_all_settings()
    queries = db.get_search_queries()
    brands = db.get_brand_keywords()
    msrp = db.get_msrp_entries()
    sched_status = scheduler.get_status()
    return templates.TemplateResponse("settings.html", {
        "request": request, "settings": settings, "queries": queries,
        "brands": brands, "msrp": msrp, "scheduler": sched_status,
    })


# ── API: Price History ─────────────────────────────────────────

@app.get("/api/price-history/{kijiji_id}")
async def api_price_history(kijiji_id: str):
    history = db.get_price_history(kijiji_id)
    return {
        "dates": [h["scraped_at"][:10] for h in history],
        "prices": [h["price"] for h in history],
    }


# ── API: Settings ─────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return db.get_all_settings()


class SettingsUpdate(BaseModel):
    scrape_interval_hours: Optional[float] = None
    max_pages_per_query: Optional[int] = None
    request_delay_min: Optional[float] = None
    request_delay_max: Optional[float] = None
    inactive_threshold: Optional[int] = None


@app.put("/api/settings")
async def api_update_settings(data: SettingsUpdate):
    updated = {}
    for key, value in data.model_dump(exclude_none=True).items():
        db.set_setting(key, value)
        updated[key] = value

    if "scrape_interval_hours" in updated and scheduler.get_status()["running"]:
        scheduler.start_scheduler(updated["scrape_interval_hours"])

    return {"updated": updated}


# ── API: Search Queries ────────────────────────────────────────

@app.get("/api/search-queries")
async def api_list_queries():
    return db.get_search_queries()


class SearchQueryCreate(BaseModel):
    url: str
    label: str


@app.post("/api/search-queries")
async def api_add_query(data: SearchQueryCreate):
    qid = db.add_search_query(data.url, data.label)
    return {"id": qid, "url": data.url, "label": data.label, "enabled": 1}


class SearchQueryUpdate(BaseModel):
    url: Optional[str] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None


@app.put("/api/search-queries/{query_id}")
async def api_update_query(query_id: int, data: SearchQueryUpdate):
    db.update_search_query(query_id, url=data.url, label=data.label, enabled=data.enabled)
    return {"ok": True}


@app.delete("/api/search-queries/{query_id}")
async def api_delete_query(query_id: int):
    db.delete_search_query(query_id)
    return {"ok": True}


# ── API: Brand Keywords ───────────────────────────────────────

@app.get("/api/brands")
async def api_list_brands():
    return db.get_brand_keywords()


class BrandKeywordCreate(BaseModel):
    brand: str
    keyword: str


@app.post("/api/brands")
async def api_add_brand(data: BrandKeywordCreate):
    kid = db.add_brand_keyword(data.brand, data.keyword)
    return {"id": kid, "brand": data.brand, "keyword": data.keyword}


@app.delete("/api/brands/{keyword_id}")
async def api_delete_brand(keyword_id: int):
    db.delete_brand_keyword(keyword_id)
    return {"ok": True}


# ── API: MSRP ─────────────────────────────────────────────────

@app.get("/api/msrp")
async def api_list_msrp():
    return db.get_msrp_entries()


class MsrpCreate(BaseModel):
    brand: str
    model: str
    msrp_cad: float
    msrp_usd: Optional[float] = None


@app.post("/api/msrp")
async def api_upsert_msrp(data: MsrpCreate):
    eid = db.upsert_msrp_entry(data.brand, data.model, data.msrp_cad, data.msrp_usd)
    return {"id": eid, "brand": data.brand, "model": data.model, "msrp_cad": data.msrp_cad}


@app.delete("/api/msrp/{entry_id}")
async def api_delete_msrp(entry_id: int):
    db.delete_msrp_entry(entry_id)
    return {"ok": True}


# ── API: Scheduler ─────────────────────────────────────────────

@app.get("/api/scheduler/status")
async def api_scheduler_status():
    return scheduler.get_status()


@app.post("/api/scheduler/start")
async def api_scheduler_start():
    interval = db.get_setting("scrape_interval_hours", 6)
    scheduler.start_scheduler(interval)
    return scheduler.get_status()


@app.post("/api/scheduler/stop")
async def api_scheduler_stop():
    scheduler.stop_scheduler()
    return scheduler.get_status()


@app.post("/api/scheduler/trigger")
async def api_scheduler_trigger():
    return scheduler.trigger_now()
