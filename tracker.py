"""Price tracking, brand detection, and deal scoring."""

import json
from datetime import datetime, timezone
from typing import Optional

from models import Deal


def _get_brand_keywords() -> dict[str, list[str]]:
    """Get brand keywords from DB."""
    import db
    return db.get_brand_keywords_map()


def _get_msrp_data() -> dict:
    """Get MSRP data from DB."""
    import db
    return db.get_msrp_map()


def detect_brand(title: str, description: str = "") -> Optional[str]:
    """Detect brand from title and description."""
    combined = f"{title} {description}".lower()
    for brand, keywords in _get_brand_keywords().items():
        for kw in keywords:
            if kw in combined:
                return brand
    return None


def detect_model(title: str, description: str = "", brand: Optional[str] = None) -> Optional[str]:
    """Detect specific model from title and description."""
    combined = f"{title} {description}".lower()
    msrp_data = _get_msrp_data()

    if brand and brand in msrp_data:
        for model_name in msrp_data[brand]:
            if model_name.lower() in combined:
                return model_name
    else:
        for b, models in msrp_data.items():
            for model_name in models:
                if model_name.lower() in combined:
                    return model_name

    return None


def lookup_msrp(brand: Optional[str], model: Optional[str]) -> Optional[float]:
    """Look up MSRP (CAD) for a brand/model combo."""
    if not brand or not model:
        return None
    msrp_data = _get_msrp_data()
    brand_data = msrp_data.get(brand, {})
    model_data = brand_data.get(model, {})
    return model_data.get("msrp_cad")


def lookup_retail_price(brand: Optional[str], model: Optional[str]) -> Optional[float]:
    """Look up current retail price for a brand/model combo."""
    if not brand or not model:
        return None
    msrp_data = _get_msrp_data()
    brand_data = msrp_data.get(brand, {})
    model_data = brand_data.get(model, {})
    return model_data.get("retail_price")


def compute_deals(listings: list[dict]) -> list[Deal]:
    """Compute deal scores for listings with price drops."""
    deals = []

    for listing in listings:
        current = listing.get("current_price")
        original = listing.get("original_price")

        if current is None or original is None or current <= 0:
            continue

        price_drop = original - current
        msrp = listing.get("msrp")
        brand = listing.get("brand")
        model = listing.get("model")
        
        # Get retail price from database
        retail_price = lookup_retail_price(brand, model) if brand and model else None
        
        # Calculate comparison metrics
        msrp_ratio = (current / msrp) if msrp and msrp > 0 else None
        retail_ratio = (current / retail_price) if retail_price and retail_price > 0 else None
        vs_retail_savings = (retail_price - current) if retail_price and retail_price > current else None
        
        # Include if there's a price drop OR a good MSRP ratio OR beats retail price
        is_good_deal = (
            price_drop > 0 or 
            (msrp_ratio is not None and msrp_ratio < 0.7) or
            (retail_ratio is not None and retail_ratio < 0.9)  # 10% or more below retail
        )
        
        if not is_good_deal:
            continue

        # Calculate metrics
        drop_pct = (price_drop / original * 100) if original > 0 and price_drop > 0 else 0

        first_seen = listing.get("first_seen", "")
        try:
            first_dt = datetime.fromisoformat(first_seen)
            days_on_market = (datetime.now(timezone.utc) - first_dt).days
        except (ValueError, TypeError):
            days_on_market = 0

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
            brand=brand,
            msrp=msrp,
            retail_price=retail_price,
            price_to_msrp_ratio=msrp_ratio,
            price_to_retail_ratio=retail_ratio,
            vs_retail_savings=vs_retail_savings,
            location=listing.get("location"),
            image_url=image_urls[0] if image_urls else None,
        ))

    # Enhanced sorting: prioritize retail price savings, then price drop %, then days on market
    def deal_score(d: Deal) -> float:
        score = 0.0
        
        # Savings vs retail is most important (0-100 points)
        if d.vs_retail_savings:
            score += min(d.vs_retail_savings / 10, 100)  # $10 saved = 1 point, cap at 100
        
        # Price drop percentage (0-50 points)
        score += d.price_drop_pct * 0.5
        
        # Days on market bonus for newer listings (0-20 points)
        if d.days_on_market <= 7:
            score += 20 - (d.days_on_market * 2)
        
        # Bonus for beating retail significantly (0-30 points)
        if d.price_to_retail_ratio and d.price_to_retail_ratio < 0.8:
            score += (0.8 - d.price_to_retail_ratio) * 150  # 20% below retail = 30 points
        
        return score
    
    deals.sort(key=deal_score, reverse=True)
    return deals
