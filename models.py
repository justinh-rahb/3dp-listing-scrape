"""Data classes for the 3D Printer Kijiji Deal Tracker."""

from dataclasses import dataclass, field
from typing import Literal, Optional


ScrapeStatus = Literal["success", "empty", "partial", "blocked", "failed", "unsupported"]


@dataclass
class ScrapedListing:
    """Raw data extracted from a listing source."""
    kijiji_id: str
    url: str
    title: str
    price: Optional[float] = None
    currency: str = "CAD"
    nominal_price: Optional[float] = None
    on_sale: bool = False
    source: str = "kijiji"
    description: Optional[str] = None
    seller_name: Optional[str] = None
    location: Optional[str] = None
    listing_date: Optional[str] = None
    image_urls: list[str] = field(default_factory=list)


@dataclass
class ScrapeOutcome:
    """Structured result returned by every source adapter."""

    status: ScrapeStatus
    requested_url: str
    listings: list[ScrapedListing] = field(default_factory=list)
    pages_attempted: int = 0
    pages_completed: int = 0
    failed_url: Optional[str] = None
    http_status: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status in {"success", "empty"}

    @property
    def can_mark_missing(self) -> bool:
        """Only authoritative non-empty results may age unseen listings."""
        return self.status == "success"


@dataclass
class Deal:
    """Computed deal information for the dashboard."""
    kijiji_id: str
    title: str
    url: str
    current_price: float
    original_price: float
    price_drop_abs: float
    price_drop_pct: float
    days_on_market: int
    nominal_price: Optional[float] = None
    currency: str = "USD"
    source: str = "kijiji"
    brand: Optional[str] = None
    msrp: Optional[float] = None
    msrp_currency: Optional[str] = None
    retail_price: Optional[float] = None
    price_to_msrp_ratio: Optional[float] = None
    price_to_retail_ratio: Optional[float] = None
    vs_retail_savings: Optional[float] = None
    last_drop_date: Optional[str] = None
    location: Optional[str] = None
    image_url: Optional[str] = None
