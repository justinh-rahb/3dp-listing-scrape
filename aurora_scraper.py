"""Scraper for Aurora Tech Channel price data.

Fetches current MSRP and retail sale prices from auroratechchannel.com.
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin, parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class AuroraScraper:
    """Scrapes 3D printer pricing data from Aurora Tech Channel."""
    
    BASE_URL = "https://auroratechchannel.com"
    PRICE_TRACKER_URL = f"{BASE_URL}/3d-printer-price.php"
    
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
    
    def _parse_price(self, price_text: str) -> Optional[float]:
        """Extract numeric price from text like '$1,999.00'."""
        if not price_text:
            return None
        # Remove currency symbols, commas, and whitespace
        cleaned = re.sub(r'[$,\s]', '', price_text)
        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return None
    
    def _extract_brand_model(self, link_element) -> tuple[Optional[str], Optional[str]]:
        """Extract brand and model from a price history link."""
        href = link_element.get('href', '')
        if 'price-details.php' not in href:
            return None, None
        
        # Parse query parameters
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        
        brand = params.get('brand', [None])[0]
        model = params.get('model', [None])[0]
        
        return brand, model
    
    def scrape_fdm_prices(self) -> list[dict]:
        """
        Scrape FDM 3D printer prices from Aurora Tech Channel.
        
        Returns:
            List of dicts with keys: brand, model, msrp, current_price, price_drop, drop_percentage
        """
        logger.info("Fetching Aurora Tech Channel FDM printer prices...")
        
        try:
            response = self.session.get(self.PRICE_TRACKER_URL, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch Aurora price page: {e}")
            return []
        
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Find all price drop entries - they're in a specific section
        # The structure shows recent price drops with brand/model info
        price_items = soup.find_all('a', href=re.compile(r'price-details\.php'))
        
        seen = set()  # Track unique brand/model combinations
        
        for link in price_items:
            try:
                brand, model = self._extract_brand_model(link)
                if not brand or not model:
                    continue
                
                # Create unique key
                key = (brand.lower() if brand else '', model.lower() if model else '')
                if key in seen:
                    continue
                seen.add(key)
                
                # Find the parent container with price info
                container = link.find_parent(['div', 'section'])
                if not container:
                    continue
                
                # Look for price information in the container
                # Prices are typically shown as: $MSRP$CurrentPrice
                price_text = container.get_text()
                
                # Extract prices using regex
                # Pattern: finds prices like $1,999.00$1,499.00
                price_matches = re.findall(r'\$[\d,]+\.?\d*', price_text)
                
                if len(price_matches) >= 2:
                    msrp = self._parse_price(price_matches[0])
                    current_price = self._parse_price(price_matches[1])
                    
                    if msrp and current_price:
                        price_drop = msrp - current_price
                        drop_pct = (price_drop / msrp * 100) if msrp > 0 else 0
                        
                        results.append({
                            'brand': brand,
                            'model': model,
                            'msrp': msrp,
                            'current_price': current_price,
                            'price_drop': price_drop,
                            'drop_percentage': drop_pct
                        })
                        
                        logger.debug(f"Found: {brand} {model} - MSRP: ${msrp}, Current: ${current_price}")
                
            except Exception as e:
                logger.debug(f"Error parsing price item: {e}")
                continue
        
        logger.info(f"Scraped {len(results)} printer prices from Aurora Tech Channel")
        return results
    
    def get_price_for_model(self, brand: str, model: str) -> Optional[dict]:
        """
        Get price information for a specific brand/model.
        
        Args:
            brand: Brand name (e.g., 'BambuLab', 'Prusa')
            model: Model name (e.g., 'P1S', 'MK4S')
        
        Returns:
            Dict with msrp and current_price, or None if not found
        """
        all_prices = self.scrape_fdm_prices()
        
        brand_lower = brand.lower()
        model_lower = model.lower()
        
        for item in all_prices:
            if (item['brand'].lower() == brand_lower and 
                item['model'].lower() == model_lower):
                return {
                    'brand': item['brand'],
                    'model': item['model'],
                    'msrp': item['msrp'],
                    'current_price': item['current_price'],
                    'price_drop': item['price_drop'],
                    'drop_percentage': item['drop_percentage']
                }
        
        return None


def update_retail_prices_from_aurora(delay: float = 1.0):
    """
    Scrape Aurora Tech Channel and update the database with retail prices.
    
    Args:
        delay: Delay in seconds between operations (be respectful)
    """
    import db
    
    scraper = AuroraScraper()
    logger.info("Starting Aurora Tech Channel price update...")
    
    time.sleep(delay)  # Be respectful
    prices = scraper.scrape_fdm_prices()
    
    if not prices:
        logger.warning("No prices retrieved from Aurora Tech Channel")
        return
    
    conn = db.get_conn()
    updated = 0
    
    try:
        for item in prices:
            # Update or insert retail price information
            db.upsert_msrp_entry(
                brand=item['brand'],
                model=item['model'],
                msrp_cad=item['msrp'],
                msrp_usd=None,  # Aurora shows USD prices, we'll use them as CAD for now
                retail_price=item['current_price'],
                conn=conn
            )
            updated += 1
        
        logger.info(f"Updated {updated} retail prices from Aurora Tech Channel")
    finally:
        conn.close()


if __name__ == "__main__":
    # Test the scraper
    logging.basicConfig(level=logging.DEBUG)
    
    scraper = AuroraScraper()
    prices = scraper.scrape_fdm_prices()
    
    print(f"\nFound {len(prices)} printers with pricing:\n")
    for item in prices[:10]:  # Show first 10
        print(f"{item['brand']} {item['model']}")
        print(f"  MSRP: ${item['msrp']:.0f}")
        print(f"  Current: ${item['current_price']:.0f}")
        print(f"  Drop: ${item['price_drop']:.0f} ({item['drop_percentage']:.1f}%)")
        print()
