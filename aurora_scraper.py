"""Scraper for Aurora Tech Channel price data.

NOTE: Aurora Tech Channel integration is currently disabled due to HTML parsing complexity.
This module is kept for future enhancement. For now, use the static msrp_data.json file.

Fetches current MSRP and retail sale prices from auroratechchannel.com.
"""

import logging

logger = logging.getLogger(__name__)


def update_retail_prices_from_aurora(delay: float = 1.0, usd_to_cad_rate: float = 1.35):
    """
    Aurora Tech Channel scraping is currently disabled.
    
    The website's HTML structure is complex and changes frequently.
    For now, manually update msrp_data.json with current prices.
    
    Future enhancement: Implement proper API or more robust scraping.
    """
    logger.warning("Aurora Tech Channel integration is currently disabled")
    logger.info("To update prices, manually edit msrp_data.json")
    logger.info("Run 'python cli.py' after updating to reload database")
    return


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("Aurora Tech Channel scraper is currently disabled")
    logger.info("Please update msrp_data.json manually for now")


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
    
    def _normalize_model_name(self, model: str) -> str:
        """Normalize model names to handle variations like 'Ender-3' vs 'Ender 3'."""
        if not model:
            return model
        # Standardize hyphens in common patterns
        model = re.sub(r'Ender-3', 'Ender 3', model)
        model = re.sub(r'Ender-5', 'Ender 5', model)
        return model
    
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
        
        # Normalize model name
        if model:
            model = self._normalize_model_name(model)
        
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
                
                # Find the immediate parent that contains just this item's prices
                # Look for the closest wrapper div/container
                container = link.find_parent()
                if not container:
                    continue
                
                # Get text from just the direct siblings, not nested elements
                # The format on Aurora is: BrandModel $MSRP$CurrentPrice
                siblings_text = ''.join([str(s) for s in container.next_siblings if isinstance(s, str)])
                price_text = link.get_text() + siblings_text
                
                # More specific pattern: consecutive prices like $1,999.00$1,499.00
                # Use a tighter pattern to avoid picking up unrelated prices
                price_pattern = r'\$([\d,]+(?:\.\d{2})?)\s*\$([\d,]+(?:\.\d{2})?)'  
                match = re.search(price_pattern, price_text)
                
                if match:
                    msrp = self._parse_price(match.group(1))
                    current_price = self._parse_price(match.group(2))
                    
                    if msrp and current_price and msrp >= current_price:  # Sanity check
                        price_drop = msrp - current_price
                        drop_pct = (price_drop / msrp * 100) if msrp > 0 else 0
                        
                        results.append({
                            'brand': brand,
                            'model': model,
                            'msrp_usd': msrp,  # Aurora shows USD prices
                            'current_price_usd': current_price,  # Current retail in USD
                            'price_drop': price_drop,
                            'drop_percentage': drop_pct
                        })
                        
                        logger.debug(f"Found: {brand} {model} - MSRP: ${msrp:.0f}, Current: ${current_price:.0f}")
                
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
            Dict with msrp_usd and current_price_usd, or None if not found
        """
        all_prices = self.scrape_fdm_prices()
        
        brand_lower = brand.lower()
        model_normalized = self._normalize_model_name(model).lower()
        
        for item in all_prices:
            if (item['brand'].lower() == brand_lower and 
                self._normalize_model_name(item['model']).lower() == model_normalized):
                return {
                    'brand': item['brand'],
                    'model': item['model'],
                    'msrp_usd': item['msrp_usd'],
                    'current_price_usd': item['current_price_usd'],
                    'price_drop': item['price_drop'],
                    'drop_percentage': item['drop_percentage']
                }
        
        return None


def update_retail_prices_from_aurora(delay: float = 1.0, usd_to_cad_rate: float = 1.35):
    """
    Scrape Aurora Tech Channel and update the database with retail prices.
    
    Args:
        delay: Delay in seconds between operations (be respectful)
        usd_to_cad_rate: USD to CAD conversion rate (default: 1.35)
    """
    import db
    
    scraper = AuroraScraper()
    logger.info("Starting Aurora Tech Channel price update...")
    logger.info(f"Using USD to CAD conversion rate: {usd_to_cad_rate}")
    
    time.sleep(delay)  # Be respectful
    prices = scraper.scrape_fdm_prices()
    
    if not prices:
        logger.warning("No prices retrieved from Aurora Tech Channel")
        return
    
    conn = db.get_conn()
    updated = 0
    skipped = 0
    
    try:
        for item in prices:
            # Convert USD prices to CAD
            msrp_usd = item['msrp_usd']
            retail_price_usd = item['current_price_usd']
            retail_price_cad = retail_price_usd * usd_to_cad_rate
            
            # Check if entry exists - only update if we have it already
            existing = conn.execute(
                "SELECT msrp_cad, msrp_usd FROM msrp_entries WHERE brand = ? AND model = ?",
                (item['brand'].lower(), item['model'])
            ).fetchone()
            
            if existing:
                # Update only retail price and msrp_usd, keep existing msrp_cad
                db.upsert_msrp_entry(
                    brand=item['brand'],
                    model=item['model'],
                    msrp_cad=existing['msrp_cad'],  # Keep existing CAD MSRP
                    msrp_usd=msrp_usd,  # Update USD MSRP from Aurora
                    retail_price=retail_price_cad,  # Current retail converted to CAD
                    conn=conn
                )
                updated += 1
            else:
                # New entry - use Aurora USD price converted to CAD
                db.upsert_msrp_entry(
                    brand=item['brand'],
                    model=item['model'],
                    msrp_cad=msrp_usd * usd_to_cad_rate,  # Convert USD to CAD
                    msrp_usd=msrp_usd,
                    retail_price=retail_price_cad,
                    conn=conn
                )
                updated += 1
                logger.debug(f"Added new model: {item['brand']} {item['model']}")
        
        logger.info(f"Updated {updated} models from Aurora Tech Channel")
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
        print(f"  MSRP: ${item['msrp_usd']:.0f} USD")
        print(f"  Current: ${item['current_price_usd']:.0f} USD")
        print(f"  Drop: ${item['price_drop']:.0f} ({item['drop_percentage']:.1f}%)")
        print()
