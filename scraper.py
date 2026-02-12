"""Core Kijiji scraping logic."""

import json
import logging
import random
import re
import time
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import USER_AGENTS
from models import ScrapedListing

logger = logging.getLogger(__name__)


class KijijiScraper:
    def __init__(self, session: Optional[requests.Session] = None,
                 delay_min: float = 2.0, delay_max: float = 5.0,
                 max_pages: int = 5):
        self.session = session or requests.Session()
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_pages = max_pages
        self._rotate_ua()

    def _rotate_ua(self):
        self.session.headers.update({"User-Agent": random.choice(USER_AGENTS)})

    def _delay(self):
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _build_page_url(self, base_url: str, page: int) -> str:
        if page == 1:
            return base_url
        # /b-canada/3d-printer/k0l0 -> /b-canada/3d-printer/page-2/k0l0
        parts = base_url.rsplit("/", 1)
        return f"{parts[0]}/page-{page}/{parts[1]}"

    def scrape_search(self, base_url: str, max_pages: Optional[int] = None) -> list[ScrapedListing]:
        max_pages = max_pages or self.max_pages
        """Scrape all pages of a search query. Returns deduplicated listings."""
        all_listings = []
        seen_ids = set()

        for page in range(1, max_pages + 1):
            url = self._build_page_url(base_url, page)
            self._rotate_ua()
            self._delay()

            try:
                resp = self.session.get(url, timeout=30)
            except requests.RequestException as e:
                logger.error(f"Request failed for {url}: {e}")
                break

            if resp.status_code == 403:
                logger.warning(f"Got 403 (blocked) for {url}, stopping pagination")
                break
            if resp.status_code == 429:
                logger.warning(f"Got 429 (rate limited) for {url}, backing off")
                time.sleep(30)
                break
            if resp.status_code != 200:
                logger.warning(f"Got {resp.status_code} for {url}")
                break

            listings, has_next = self._parse_search_page(resp.text, base_url)

            for listing in listings:
                if listing.kijiji_id not in seen_ids:
                    seen_ids.add(listing.kijiji_id)
                    all_listings.append(listing)

            logger.info(f"Page {page}: found {len(listings)} listings (total: {len(all_listings)})")

            if not has_next or len(listings) == 0:
                break

        return all_listings

    def _parse_search_page(self, html: str, base_url: str) -> tuple[list[ScrapedListing], bool]:
        """Parse a search results page. Returns (listings, has_next_page)."""
        soup = BeautifulSoup(html, "lxml")
        listings = []

        # Strategy 1: __NEXT_DATA__ JSON
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if next_data_tag and next_data_tag.string:
            try:
                data = json.loads(next_data_tag.string)
                listings = self._parse_next_data(data)
                if listings:
                    has_next = self._has_next_page_from_data(data)
                    return listings, has_next
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to parse __NEXT_DATA__: {e}")

        # Strategy 2: HTML parsing
        listings = self._parse_html_listings(soup)
        has_next = self._has_next_page_html(soup)
        return listings, has_next

    def _parse_next_data(self, data: dict) -> list[ScrapedListing]:
        """Extract listings from __NEXT_DATA__ JSON."""
        listings = []

        # Navigate common Next.js data structures
        props = data.get("props", {}).get("pageProps", {})

        for listing_data in self._find_listing_collections(props):
            for item in listing_data:
                try:
                    listing = self._extract_from_json_item(item)
                    if listing:
                        listings.append(listing)
                except Exception as e:
                    logger.debug(f"Failed to parse JSON listing item: {e}")
                    continue

        # Deduplicate in case multiple collections include the same listing.
        deduped = []
        seen_ids = set()
        for listing in listings:
            if listing.kijiji_id not in seen_ids:
                seen_ids.add(listing.kijiji_id)
                deduped.append(listing)

        return deduped

    def _find_listing_collections(self, node) -> list[list[dict]]:
        """Find all list-like collections that look like listing result sets."""
        found = []

        if isinstance(node, dict):
            # Common wrappers seen in Next.js payloads.
            for key in ("listings", "ads", "results", "searchResults", "items", "data"):
                value = node.get(key)
                if self._looks_like_listing_collection(value):
                    found.append(value)
            for value in node.values():
                found.extend(self._find_listing_collections(value))
        elif isinstance(node, list):
            # Sometimes listing collections are nested directly in arrays.
            if self._looks_like_listing_collection(node):
                found.append(node)
            for value in node:
                found.extend(self._find_listing_collections(value))

        return found

    def _looks_like_listing_collection(self, value) -> bool:
        if not isinstance(value, list) or not value:
            return False
        sample = [v for v in value[:8] if isinstance(v, dict)]
        if not sample:
            return False
        id_keys = ("id", "adId", "listingId")
        title_keys = ("title", "name")
        url_keys = ("url", "seoUrl", "href")
        hits = 0
        for item in sample:
            if any(item.get(k) for k in id_keys) and any(item.get(k) for k in title_keys):
                hits += 1
                continue
            if any(item.get(k) for k in url_keys) and any(item.get(k) for k in title_keys):
                hits += 1
        return hits >= max(1, len(sample) // 2)

    def _extract_from_json_item(self, item: dict) -> Optional[ScrapedListing]:
        """Extract a ScrapedListing from a JSON listing object."""
        # Try common field names
        kijiji_id = str(item.get("id", item.get("adId", item.get("listingId", ""))))
        if not kijiji_id:
            kijiji_id = self._extract_kijiji_id(item.get("url", item.get("seoUrl", item.get("href", ""))))
        if not kijiji_id:
            return None

        title = item.get("title", item.get("name", ""))
        if not title:
            return None

        # URL
        url = item.get("url", item.get("seoUrl", item.get("href", "")))
        if url and not url.startswith("http"):
            url = f"https://www.kijiji.ca{url}"

        # Price
        price = None
        price_data = item.get("price", item.get("amount", item.get("priceInfo", {})))
        if isinstance(price_data, dict):
            price = price_data.get("amount", price_data.get("value"))
        elif isinstance(price_data, (int, float)):
            price = float(price_data)
        elif isinstance(price_data, str):
            price = self._parse_price_str(price_data)

        # Location
        location = None
        loc_data = item.get("location", item.get("address", {}))
        if isinstance(loc_data, dict):
            parts = [loc_data.get("city", ""), loc_data.get("province", loc_data.get("region", ""))]
            location = ", ".join(p for p in parts if p)
        elif isinstance(loc_data, str):
            location = loc_data

        # Images
        image_urls = []
        images = item.get("images", item.get("imageUrls", item.get("photos", [])))
        if isinstance(images, list):
            for img in images[:5]:
                if isinstance(img, str):
                    image_urls.append(img)
                elif isinstance(img, dict):
                    image_urls.append(img.get("href", img.get("url", img.get("src", ""))))

        # Description
        description = item.get("description", item.get("body", None))

        # Seller
        seller_name = None
        seller = item.get("seller", item.get("poster", item.get("user", {})))
        if isinstance(seller, dict):
            seller_name = seller.get("name", seller.get("displayName"))

        return ScrapedListing(
            kijiji_id=kijiji_id,
            url=url,
            title=title,
            price=price,
            description=description,
            seller_name=seller_name,
            location=location,
            image_urls=[u for u in image_urls if u],
        )

    def _has_next_page_from_data(self, data: dict) -> bool:
        """Check if there's a next page from __NEXT_DATA__."""
        props = data.get("props", {}).get("pageProps", {})
        # Look for pagination info
        pagination = props.get("pagination", {})
        if pagination:
            current = pagination.get("currentPage", pagination.get("page", 1))
            total = pagination.get("totalPages", pagination.get("numPages", 1))
            return current < total
        return True  # Assume more pages if we can't tell

    def _parse_html_listings(self, soup: BeautifulSoup) -> list[ScrapedListing]:
        """Parse listings from HTML when __NEXT_DATA__ is not available."""
        listings = []

        listing_links = []
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if not href:
                continue
            if self._extract_kijiji_id(href):
                listing_links.append(link)
                continue
            data_testid = (link.get("data-testid") or "").lower()
            if "listing" in data_testid and "title" in data_testid:
                listing_links.append(link)

        # Deduplicate by href (same listing can appear in multiple link elements)
        seen_hrefs = set()
        unique_links = []
        for link in listing_links:
            href = link.get("href", "")
            if href not in seen_hrefs:
                seen_hrefs.add(href)
                unique_links.append(link)

        for link in unique_links:
            try:
                listing = self._parse_listing_card(link)
                if listing:
                    listings.append(listing)
            except Exception as e:
                logger.debug(f"Failed to parse listing card: {e}")
                continue

        return listings

    def _parse_listing_card(self, link_element) -> Optional[ScrapedListing]:
        """Parse a single listing card from an anchor element."""
        href = link_element.get("href", "")
        if not href:
            return None

        kijiji_id = self._extract_kijiji_id(href)
        if not kijiji_id:
            return None

        url = urljoin("https://www.kijiji.ca", href)

        # Walk up to find the card container
        card = link_element
        for _ in range(5):
            parent = card.parent
            if parent and parent.name not in ("html", "body", "[document]"):
                card = parent
            else:
                break

        # Title
        title_el = link_element.find(["h2", "h3"]) or card.find(["h2", "h3"])
        title = title_el.get_text(strip=True) if title_el else link_element.get_text(strip=True)
        if not title or len(title) < 3:
            return None

        # Price
        price = self._extract_price_from_element(card)

        # Location
        location = self._extract_location_from_element(card)

        # Image
        image_urls = []
        img = link_element.find("img") or card.find("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and not src.startswith("data:"):
                image_urls.append(src)

        return ScrapedListing(
            kijiji_id=kijiji_id,
            url=url,
            title=title,
            price=price,
            location=location,
            image_urls=image_urls,
        )

    def _extract_kijiji_id(self, href: str) -> Optional[str]:
        """Extract listing ID from known Kijiji URL patterns."""
        if not href:
            return None

        candidate = href.strip()
        # /v-.../.../1234567890 or /vip/1234567890
        direct = re.search(r"/(\d{6,})(?:$|[/?#])", candidate)
        if direct:
            return direct.group(1)

        # /v-view-details.html?adId=1234567890
        parsed = urlparse(candidate)
        query = parse_qs(parsed.query)
        for key in ("adId", "adid", "listingId", "id"):
            values = query.get(key)
            if values:
                value = values[0]
                if re.fullmatch(r"\d{6,}", value):
                    return value
        return None

    def _extract_price_from_element(self, element) -> Optional[float]:
        """Extract price from a DOM element."""
        if element is None:
            return None
        text = element.get_text()
        return self._parse_price_str(text)

    def _parse_price_str(self, text: str) -> Optional[float]:
        """Parse a price from text. Returns None for 'Please Contact', etc."""
        if not text:
            return None
        if "free" in text.lower():
            return 0.0
        # Match $1,234.56 or $1234 patterns
        match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
        if match:
            return float(match.group(1).replace(",", ""))
        return None

    def _extract_location_from_element(self, element) -> Optional[str]:
        """Try to extract location text from a listing card."""
        # Look for common location patterns
        for tag in element.find_all(["span", "div", "p"]):
            text = tag.get_text(strip=True)
            # Location strings typically contain city, province patterns
            if re.search(r"[A-Z][a-z]+,\s*[A-Z]{2}", text) and len(text) < 100:
                return text
        return None

    def _has_next_page_html(self, soup: BeautifulSoup) -> bool:
        """Check for next page link in HTML."""
        # Look for pagination links
        next_link = soup.find("a", attrs={"aria-label": re.compile(r"next|Next", re.I)})
        if next_link:
            return True
        # Look for "Next" text in pagination
        pagination = soup.find(["nav", "div"], attrs={"aria-label": re.compile(r"paginat", re.I)})
        if pagination:
            next_btn = pagination.find(string=re.compile(r"Next|»|›"))
            return next_btn is not None
        return False

    def scrape_listing_detail(self, url: str) -> dict:
        """Scrape an individual listing page for full details."""
        self._rotate_ua()
        self._delay()

        try:
            resp = self.session.get(url, timeout=30)
        except requests.RequestException as e:
            logger.error(f"Failed to fetch listing detail {url}: {e}")
            return {}

        if resp.status_code != 200:
            logger.warning(f"Got {resp.status_code} for detail page {url}")
            return {}

        soup = BeautifulSoup(resp.text, "lxml")
        detail = {}

        # Try __NEXT_DATA__ first
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if next_data_tag and next_data_tag.string:
            try:
                data = json.loads(next_data_tag.string)
                props = data.get("props", {}).get("pageProps", {})
                ad = props.get("ad", props.get("listing", props.get("adInfo", {})))
                if ad:
                    detail["description"] = ad.get("description", ad.get("body"))
                    seller = ad.get("seller", ad.get("poster", ad.get("user", {})))
                    if isinstance(seller, dict):
                        detail["seller_name"] = seller.get("name", seller.get("displayName"))
                    detail["listing_date"] = ad.get("activationDate", ad.get("postedDate", ad.get("sortingDate")))
                    images = ad.get("images", ad.get("imageUrls", []))
                    if isinstance(images, list):
                        detail["image_urls"] = []
                        for img in images[:10]:
                            if isinstance(img, str):
                                detail["image_urls"].append(img)
                            elif isinstance(img, dict):
                                detail["image_urls"].append(
                                    img.get("href", img.get("url", img.get("src", "")))
                                )
                    return detail
            except (json.JSONDecodeError, KeyError):
                pass

        # Fallback: parse HTML
        # Description
        desc_el = soup.find(attrs={"itemprop": "description"})
        if not desc_el:
            # Try finding the main content area
            for tag in soup.find_all(["div", "section"]):
                text = tag.get_text(strip=True)
                if len(text) > 100 and "$" not in text[:10]:
                    detail["description"] = text[:2000]
                    break
        else:
            detail["description"] = desc_el.get_text(strip=True)

        # Date
        date_el = soup.find("time") or soup.find(attrs={"itemprop": "datePosted"})
        if date_el:
            detail["listing_date"] = date_el.get("datetime", date_el.get_text(strip=True))

        return detail
