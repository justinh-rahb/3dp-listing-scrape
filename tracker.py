"""Price tracking, brand detection, and deal scoring."""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from config import BRAND_KEYWORDS
from models import Deal


def load_msrp_data() -> dict:
    path = os.path.join(os.path.dirname(__file__), "msrp_data.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


_MSRP_DATA = None


def get_msrp_data() -> dict:
    global _MSRP_DATA
    if _MSRP_DATA is None:
        _MSRP_DATA = load_msrp_data()
    return _MSRP_DATA


def detect_brand(title: str, description: str = "") -> Optional[str]:
    """Detect brand from title and description."""
    combined = f"{title} {description}".lower()
    for brand, keywords in BRAND_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                return brand
    return None


def detect_model(title: str, description: str = "", brand: Optional[str] = None) -> Optional[str]:
    """Detect specific model from title and description."""
    combined = f"{title} {description}".lower()
    msrp_data = get_msrp_data()

    # If we know the brand, search its models
    if brand and brand in msrp_data:
        for model_name in msrp_data[brand]:
            if model_name.lower() in combined:
                return model_name
    else:
        # Search all brands
        for b, models in msrp_data.items():
            for model_name in models:
                if model_name.lower() in combined:
                    return model_name

    return None


def lookup_msrp(brand: Optional[str], model: Optional[str]) -> Optional[float]:
    """Look up MSRP (CAD) for a brand/model combo."""
    if not brand or not model:
        return None
    msrp_data = get_msrp_data()
    brand_data = msrp_data.get(brand, {})
    model_data = brand_data.get(model, {})
    return model_data.get("msrp_cad")


def compute_deals(listings: list[dict]) -> list[Deal]:
    """Compute deal scores for listings with price drops."""
    deals = []

    for listing in listings:
        current = listing.get("current_price")
        original = listing.get("original_price")

        if current is None or original is None or current <= 0:
            continue

        price_drop = original - current
        if price_drop <= 0 and listing.get("msrp") is None:
            continue  # No drop and no MSRP to compare against

        # Calculate metrics
        drop_pct = (price_drop / original * 100) if original > 0 and price_drop > 0 else 0

        first_seen = listing.get("first_seen", "")
        try:
            first_dt = datetime.fromisoformat(first_seen)
            days_on_market = (datetime.now(timezone.utc) - first_dt).days
        except (ValueError, TypeError):
            days_on_market = 0

        msrp = listing.get("msrp")
        msrp_ratio = (current / msrp) if msrp and msrp > 0 else None

        # Include if there's a price drop OR a good MSRP ratio
        if price_drop > 0 or (msrp_ratio is not None and msrp_ratio < 0.7):
            image_urls = listing.get("image_urls", "[]")
            if isinstance(image_urls, str):
                try:
                    image_urls = json.loads(image_urls)
                except (json.JSONDecodeError, TypeError):
                    image_urls = []

            deals.append(Deal(
                kijiji_id=listing["kijiji_id"],
                title=listing["title"],
                url=listing["url"],
                current_price=current,
                original_price=original,
                price_drop_abs=max(price_drop, 0),
                price_drop_pct=drop_pct,
                days_on_market=days_on_market,
                brand=listing.get("brand"),
                msrp=msrp,
                price_to_msrp_ratio=msrp_ratio,
                location=listing.get("location"),
                image_url=image_urls[0] if image_urls else None,
            ))

    # Sort by composite score: weight price drop % highest, then days on market
    deals.sort(key=lambda d: (d.price_drop_pct * 2 + min(d.days_on_market, 90) * 0.5), reverse=True)
    return deals
