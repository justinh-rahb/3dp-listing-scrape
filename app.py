"""FastAPI web dashboard for the 3D Printer Kijiji Deal Tracker."""

import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import db
import scheduler
from config import SETTINGS_PASSWORD
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
settings_auth = HTTPBasic(auto_error=False)


def parse_optional_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned == "":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def require_settings_auth(credentials: Optional[HTTPBasicCredentials] = Depends(settings_auth)) -> None:
    """Protect settings UI and APIs when SETTINGS_PASSWORD is configured."""
    if not SETTINGS_PASSWORD:
        return

    if not credentials or not secrets.compare_digest(credentials.password, SETTINGS_PASSWORD):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Settings authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )


# ── Page Routes ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, brand: Optional[str] = None,
                min_price: Optional[str] = None, max_price: Optional[str] = None,
                search: Optional[str] = None,
                active_only: str = "1", show_hidden: str = "0", sort_by: str = "last_seen"):
    sort_aliases = {
        "last_seen": "last_seen_desc",
        "newest": "first_seen_desc",
        "oldest": "first_seen_asc",
        "price_drop": "price_drop_desc",
    }
    current_sort = sort_aliases.get(sort_by, sort_by)

    min_price_value = parse_optional_float(min_price)
    max_price_value = parse_optional_float(max_price)

    filters = {
        "brand": brand,
        "min_price": min_price_value,
        "max_price": max_price_value,
        "search": search,
        "active_only": active_only == "1",
        "show_hidden": show_hidden == "1",
        "sort_by": current_sort,
    }

    sortable_columns = {
        "title": ("title_asc", "title_desc"),
        "price": ("price_asc", "price_desc"),
        "change": ("price_drop_asc", "price_drop_desc"),
        "brand": ("brand_asc", "brand_desc"),
        "first_seen": ("first_seen_asc", "first_seen_desc"),
    }
    sort_urls = {}
    sort_icons = {}
    current_params = dict(request.query_params)

    for column, (asc_key, desc_key) in sortable_columns.items():
        next_key = desc_key if current_sort == asc_key else asc_key
        params = dict(current_params)
        params["sort_by"] = next_key
        sort_urls[column] = f"/?{urlencode(params)}"
        if current_sort == asc_key:
            sort_icons[column] = "↑"
        elif current_sort == desc_key:
            sort_icons[column] = "↓"
        else:
            sort_icons[column] = ""

    listings = db.get_listings(filters)
    brands = db.get_distinct_brands()
    stats = db.get_stats()
    sched_status = scheduler.get_status()
    return templates.TemplateResponse("index.html", {
        "request": request, "listings": listings, "brands": brands,
        "filters": filters, "stats": stats, "scheduler": sched_status,
        "sort_urls": sort_urls, "sort_icons": sort_icons,
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


@app.post("/listing/{kijiji_id}/hide")
async def hide_listing(request: Request, kijiji_id: str):
    db.set_listing_hidden(kijiji_id, True)
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)


@app.post("/listing/{kijiji_id}/unhide")
async def unhide_listing(request: Request, kijiji_id: str):
    db.set_listing_hidden(kijiji_id, False)
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)


@app.post("/api/listing/{kijiji_id}/hide")
async def api_hide_listing(kijiji_id: str):
    """JSON endpoint for hiding listing without page refresh."""
    db.set_listing_hidden(kijiji_id, True)
    return {"ok": True, "kijiji_id": kijiji_id, "is_hidden": True}


@app.post("/api/listing/{kijiji_id}/unhide")
async def api_unhide_listing(kijiji_id: str):
    """JSON endpoint for unhiding listing without page refresh."""
    db.set_listing_hidden(kijiji_id, False)
    return {"ok": True, "kijiji_id": kijiji_id, "is_hidden": False}


class BulkHideRequest(BaseModel):
    kijiji_ids: list[str]
    hide: bool


@app.post("/api/listings/bulk-hide")
async def api_bulk_hide(data: BulkHideRequest):
    """Bulk hide/unhide multiple listings."""
    conn = db.get_conn()
    try:
        for kijiji_id in data.kijiji_ids:
            db.set_listing_hidden(kijiji_id, data.hide, conn)
        conn.commit()
        return {
            "ok": True,
            "count": len(data.kijiji_ids),
            "is_hidden": data.hide
        }
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.delete("/api/listing/{kijiji_id}")
async def api_delete_listing(kijiji_id: str):
    deleted = db.delete_listing(kijiji_id)
    return {"ok": deleted, "kijiji_id": kijiji_id}


class BulkDeleteRequest(BaseModel):
    kijiji_ids: list[str]


@app.post("/api/listings/bulk-delete")
async def api_bulk_delete(data: BulkDeleteRequest):
    conn = db.get_conn()
    try:
        deleted = db.delete_listings(data.kijiji_ids, conn=conn)
        conn.commit()
        return {"ok": True, "deleted": deleted}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/deals", response_class=HTMLResponse)
async def deals_page(request: Request):
    listings = db.get_listings({"active_only": True})
    deal_list = compute_deals(listings)
    return templates.TemplateResponse("deals.html", {
        "request": request, "deals": deal_list,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _: None = Depends(require_settings_auth)):
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
async def api_get_settings(_: None = Depends(require_settings_auth)):
    return db.get_all_settings()


class SettingsUpdate(BaseModel):
    scrape_interval_hours: Optional[float] = None
    max_pages_per_query: Optional[int] = None
    request_delay_min: Optional[float] = None
    request_delay_max: Optional[float] = None
    inactive_threshold: Optional[int] = None


class ClearDbRequest(BaseModel):
    preserve_settings: bool = True


@app.put("/api/settings")
async def api_update_settings(data: SettingsUpdate, _: None = Depends(require_settings_auth)):
    updated = {}
    for key, value in data.model_dump(exclude_none=True).items():
        db.set_setting(key, value)
        updated[key] = value

    if "scrape_interval_hours" in updated and scheduler.get_status()["running"]:
        scheduler.start_scheduler(updated["scrape_interval_hours"])

    return {"updated": updated}


@app.post("/api/settings/clear-db")
async def api_clear_db(data: ClearDbRequest, _: None = Depends(require_settings_auth)):
    result = db.clear_database(preserve_settings=data.preserve_settings)
    return {"ok": True, **result}


# ── API: Search Queries ────────────────────────────────────────

@app.get("/api/search-queries")
async def api_list_queries(_: None = Depends(require_settings_auth)):
    return db.get_search_queries()


class SearchQueryCreate(BaseModel):
    url: str
    label: str


@app.post("/api/search-queries")
async def api_add_query(data: SearchQueryCreate, _: None = Depends(require_settings_auth)):
    qid = db.add_search_query(data.url, data.label)
    return {"id": qid, "url": data.url, "label": data.label, "enabled": 1}


class SearchQueryUpdate(BaseModel):
    url: Optional[str] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None


@app.put("/api/search-queries/{query_id}")
async def api_update_query(query_id: int, data: SearchQueryUpdate, _: None = Depends(require_settings_auth)):
    db.update_search_query(query_id, url=data.url, label=data.label, enabled=data.enabled)
    return {"ok": True}


@app.delete("/api/search-queries/{query_id}")
async def api_delete_query(query_id: int, _: None = Depends(require_settings_auth)):
    db.delete_search_query(query_id)
    return {"ok": True}


@app.post("/api/search-queries/{query_id}/scrape")
async def api_scrape_query(query_id: int, _: None = Depends(require_settings_auth)):
    query = next((q for q in db.get_search_queries() if q["id"] == query_id), None)
    if not query:
        raise HTTPException(status_code=404, detail="Search query not found")
    result = scheduler.trigger_query(query_id)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return {"ok": True, "query_id": query_id, "label": query["label"]}


# ── API: Brand Keywords ───────────────────────────────────────

@app.get("/api/brands")
async def api_list_brands(_: None = Depends(require_settings_auth)):
    return db.get_brand_keywords()


class BrandKeywordCreate(BaseModel):
    brand: str
    keyword: str


@app.post("/api/brands")
async def api_add_brand(data: BrandKeywordCreate, _: None = Depends(require_settings_auth)):
    kid = db.add_brand_keyword(data.brand, data.keyword)
    return {"id": kid, "brand": data.brand, "keyword": data.keyword}


@app.delete("/api/brands/{keyword_id}")
async def api_delete_brand(keyword_id: int, _: None = Depends(require_settings_auth)):
    db.delete_brand_keyword(keyword_id)
    return {"ok": True}


# ── API: MSRP ─────────────────────────────────────────────────

@app.get("/api/msrp")
async def api_list_msrp(_: None = Depends(require_settings_auth)):
    return db.get_msrp_entries()


class MsrpCreate(BaseModel):
    brand: str
    model: str
    msrp_cad: float
    msrp_usd: Optional[float] = None


@app.post("/api/msrp")
async def api_upsert_msrp(data: MsrpCreate, _: None = Depends(require_settings_auth)):
    eid = db.upsert_msrp_entry(data.brand, data.model, data.msrp_cad, data.msrp_usd)
    return {"id": eid, "brand": data.brand, "model": data.model, "msrp_cad": data.msrp_cad}


@app.delete("/api/msrp/{entry_id}")
async def api_delete_msrp(entry_id: int, _: None = Depends(require_settings_auth)):
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
