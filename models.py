"""Data classes for the 3D Printer Kijiji Deal Tracker."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScrapedListing:
    """Raw data extracted from a Kijiji listing."""
    kijiji_id: str
    url: str
    title: str
    price: Optional[float] = None
    description: Optional[str] = None
    seller_name: Optional[str] = None
    location: Optional[str] = None
    listing_date: Optional[str] = None
    image_urls: list[str] = field(default_factory=list)


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
    brand: Optional[str] = None
    msrp: Optional[float] = None
    retail_price: Optional[float] = None
    price_to_msrp_ratio: Optional[float] = None
    price_to_retail_ratio: Optional[float] = None
    vs_retail_savings: Optional[float] = None
    last_drop_date: Optional[str] = None
    location: Optional[str] = None
    image_url: Optional[str] = None
