"""Core Kijiji scraping logic."""

import hashlib
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


class RetailScraper:
    """Scraper for retailer/manufacturer pages."""

    def __init__(self, session: Optional[requests.Session] = None,
                 delay_min: float = 1.0, delay_max: float = 2.0):
        self.session = session or requests.Session()
        self.delay_min = delay_min
        self.delay_max = delay_max
        self._rotate_ua()

    def _rotate_ua(self):
        self.session.headers.update({"User-Agent": random.choice(USER_AGENTS)})

    def _delay(self):
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _get(self, url: str) -> str:
        self._rotate_ua()
        self._delay()
        try:
            resp = self.session.get(url, timeout=30)
        except requests.RequestException as e:
            raise RuntimeError(f"Request failed for {url}: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(f"Got {resp.status_code} for {url}")
        return resp.text

    def scrape_url(self, url: str) -> list[ScrapedListing]:
        path = urlparse(url).path.lower()
        if "/products/" in path:
            return self._scrape_shopify_product(url)
        if "formbot3d.com" in url:
            return self._scrape_formbot_vorons(url)
        logger.warning(f"No retail scraper registered for url={url}")
        return []

    def _infer_source_from_url(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        host = host.split(":", 1)[0]
        host = host.replace("www.", "")

        # Handle localized subdomains like ca.qidi3d.com -> qidi3d.com.
        locale_prefixes = {"ca", "us", "eu", "uk", "au", "de", "fr", "es", "it", "jp"}
        host_parts = host.split(".")
        if len(host_parts) >= 3 and host_parts[0] in locale_prefixes:
            host = ".".join(host_parts[1:])

        if "sovol3d.com" in host:
            return "sovol"
        if "formbot3d.com" in host:
            return "formbot"
        if "qidi3d.com" in host:
            return "qidi3d"
        # Generic fallback for new Shopify manufacturers.
        parts = host.split(".")
        if len(parts) >= 3 and parts[0] in {"shop", "store"}:
            base = parts[1].strip()
        else:
            base = parts[0].strip()
        return base if base else "shopify"

    def _stable_id(self, source: str, url: str) -> str:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return f"{source}:{digest}"

    def _parse_amount(self, value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            cleaned = re.sub(r"[^\d.]", "", cleaned)
            if cleaned == "":
                return None
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    def _parse_shopify_money(self, value) -> Optional[float]:
        """Parse Shopify money values that may be decimal dollars or integer cents."""
        amount = self._parse_amount(value)
        if amount is None:
            return None
        # Shopify product JSON often stores money in cents.
        if amount >= 10000:
            return amount / 100.0
        return amount

    def _extract_currency(self, text: str, default: str = "USD") -> str:
        lowered = text.lower()
        if "cad" in lowered:
            return "CAD"
        if "usd" in lowered or "$" in lowered:
            return "USD"
        return default

    def _extract_all_prices(self, text: str) -> list[float]:
        amounts = re.findall(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
        prices = []
        for amount in amounts:
            try:
                prices.append(float(amount.replace(",", "")))
            except ValueError:
                continue
        return prices

    def _detect_currency_from_text(self, text: str, default: str = "USD") -> str:
        lowered = text.lower()
        if "cad" in lowered or "ca$" in lowered or "c$" in lowered:
            return "CAD"
        if "usd" in lowered or "us$" in lowered or "$" in lowered:
            return "USD"
        return default

    def _json_ld_blocks(self, soup: BeautifulSoup) -> list[dict]:
        blocks = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                blocks.extend(item for item in parsed if isinstance(item, dict))
            elif isinstance(parsed, dict):
                blocks.append(parsed)
        return blocks

    def _extract_shopify_variant_prices(self, html: str, soup: BeautifulSoup) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """Extract min current price and max compare-at price from Shopify product data."""
        current_candidates: list[float] = []
        compare_candidates: list[float] = []
        currency: Optional[str] = None

        def collect_from_variant_dict(variant: dict):
            nonlocal currency
            curr = self._parse_shopify_money(variant.get("price"))
            comp = self._parse_shopify_money(variant.get("compare_at_price"))
            if curr is not None:
                current_candidates.append(curr)
            if comp is not None:
                compare_candidates.append(comp)
            curr_code = variant.get("currency") or variant.get("price_currency")
            if isinstance(curr_code, str) and curr_code.strip():
                currency = curr_code.strip().upper()

        # Parse explicit JSON script blocks where product variants are typically embedded.
        for script in soup.find_all("script", attrs={"type": "application/json"}):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            stack = [parsed]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    variants = node.get("variants")
                    if isinstance(variants, list):
                        for variant in variants:
                            if isinstance(variant, dict):
                                collect_from_variant_dict(variant)
                    for value in node.values():
                        if isinstance(value, (dict, list)):
                            stack.append(value)
                elif isinstance(node, list):
                    for value in node:
                        if isinstance(value, (dict, list)):
                            stack.append(value)

        # Regex fallback for compare-at values when script JSON is not directly parseable.
        for match in re.finditer(r'"compare_at_price(?:_min|_max)?"\s*:\s*"?(?P<v>\d+(?:\.\d+)?)"?', html):
            parsed = self._parse_shopify_money(match.group("v"))
            if parsed is not None:
                compare_candidates.append(parsed)
        if currency is None:
            m = re.search(r'"currency"\s*:\s*"(?P<c>[A-Za-z]{3})"', html)
            if m:
                currency = m.group("c").upper()

        # Guard against obvious junk values (e.g. "8" from "8% OFF" style payloads).
        current_candidates = [p for p in current_candidates if p is not None and p >= 20]
        compare_candidates = [p for p in compare_candidates if p is not None and p >= 20]

        current = min(current_candidates) if current_candidates else None
        nominal = max(compare_candidates) if compare_candidates else None
        return current, nominal, currency

    def _extract_dom_prices(self, soup: BeautifulSoup) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """Extract current and nominal prices from rendered product DOM."""
        current_candidates: list[float] = []
        nominal_candidates: list[float] = []
        currency: Optional[str] = None

        # Current price from known product-price containers.
        for selector in (
            "#cur_price",
            ".themes_products_price",
            "[itemprop='price']",
            ".product-price",
            ".price",
        ):
            for el in soup.select(selector):
                text = el.get_text(" ", strip=True)
                if not text:
                    continue
                prices = self._extract_all_prices(text)
                if prices:
                    current_candidates.extend(prices)
                if currency is None:
                    maybe_currency = self._detect_currency_from_text(text, default="USD")
                    if maybe_currency:
                        currency = maybe_currency

        # Nominal/original price from strike-through and compare-price containers.
        for selector in (
            "del",
            ".themes_products_origin_price",
            ".compare-at-price",
            ".old-price",
            ".origin-price",
        ):
            for el in soup.select(selector):
                text = el.get_text(" ", strip=True)
                if not text:
                    continue
                prices = self._extract_all_prices(text)
                if prices:
                    nominal_candidates.extend(prices)
                if currency is None:
                    maybe_currency = self._detect_currency_from_text(text, default="USD")
                    if maybe_currency:
                        currency = maybe_currency

        current = min(current_candidates) if current_candidates else None
        nominal = max(nominal_candidates) if nominal_candidates else None
        return current, nominal, currency

    def _extract_images_from_shopify_html(self, url: str, html: str, soup: BeautifulSoup) -> list[str]:
        image_urls: list[str] = []
        for block in self._json_ld_blocks(soup):
            if block.get("@type") != "Product":
                continue
            image_data = block.get("image")
            if isinstance(image_data, str) and image_data:
                image_urls.append(urljoin(url, image_data))
            elif isinstance(image_data, list):
                for img in image_data:
                    if isinstance(img, str) and img:
                        image_urls.append(urljoin(url, img))
                    elif isinstance(img, dict):
                        img_url = img.get("url") or img.get("contentUrl") or img.get("src")
                        if isinstance(img_url, str) and img_url:
                            image_urls.append(urljoin(url, img_url))
            elif isinstance(image_data, dict):
                img_url = image_data.get("url") or image_data.get("contentUrl") or image_data.get("src")
                if isinstance(img_url, str) and img_url:
                    image_urls.append(urljoin(url, img_url))

        if not image_urls:
            og_img = soup.find("meta", attrs={"property": "og:image"})
            if og_img and og_img.get("content"):
                image_urls.append(urljoin(url, og_img["content"]))
        if not image_urls:
            og_img_secure = soup.find("meta", attrs={"property": "og:image:secure_url"})
            if og_img_secure and og_img_secure.get("content"):
                image_urls.append(urljoin(url, og_img_secure["content"]))
        if not image_urls:
            twitter_img = soup.find("meta", attrs={"name": "twitter:image"})
            if twitter_img and twitter_img.get("content"):
                image_urls.append(urljoin(url, twitter_img["content"]))
        if not image_urls:
            featured_match = re.search(r'"featured_image"\s*:\s*"(?P<img>[^"]+)"', html)
            if featured_match:
                image_urls.append(urljoin(url, featured_match.group("img")))
        if not image_urls:
            cdn_match = re.search(
                r'(https?:)?//cdn\.shopify\.com/[^"\'\s>]+\.(?:jpg|jpeg|png|webp)',
                html,
                flags=re.IGNORECASE,
            )
            if cdn_match:
                image_urls.append(urljoin(url, cdn_match.group(0)))

        seen = set()
        deduped = []
        for img in image_urls:
            if img in seen:
                continue
            seen.add(img)
            deduped.append(img)
        return deduped[:10]

    def _scrape_shopify_product(self, url: str) -> list[ScrapedListing]:
        html = self._get(url)
        soup = BeautifulSoup(html, "lxml")
        source = self._infer_source_from_url(url)

        title = None
        current_price = None
        nominal_price = None
        currency = "USD"
        variant_current, variant_nominal, variant_currency = self._extract_shopify_variant_prices(html, soup)
        dom_current, dom_nominal, dom_currency = self._extract_dom_prices(soup)

        for block in self._json_ld_blocks(soup):
            if block.get("@type") != "Product":
                continue
            title = block.get("name") or title
            offers = block.get("offers")
            offer_items = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
            for offer in offer_items:
                price = self._parse_amount(offer.get("price"))
                if price is not None:
                    current_price = price if current_price is None else min(current_price, price)
                price_currency = offer.get("priceCurrency")
                if isinstance(price_currency, str) and price_currency.strip():
                    currency = price_currency.strip().upper()

        # Use rendered DOM price as strongest signal for the active variant.
        if dom_current is not None:
            current_price = dom_current
        elif variant_current is not None:
            current_price = variant_current

        # Currency precedence: DOM > variant JSON-LD inferred value.
        if dom_currency:
            currency = dom_currency
        elif variant_currency:
            currency = variant_currency

        if not title:
            title_tag = soup.find(["h1", "title"])
            title = title_tag.get_text(strip=True) if title_tag else f"{source.title()} Product"

        if current_price is None:
            price_candidates = self._extract_all_prices(soup.get_text(" ", strip=True))
            if price_candidates:
                current_price = min(price_candidates)
                nominal_price = max(price_candidates) if max(price_candidates) > current_price else None

        if dom_nominal is not None:
            nominal_price = dom_nominal
        elif nominal_price is None and variant_nominal is not None:
            nominal_price = variant_nominal

        # Last fallback for compare-at in raw source.
        if nominal_price is None:
            compare_match = re.search(r'"compare_at_price"\s*:\s*"?(?P<v>\d+)"?', html)
            if compare_match and current_price is not None:
                candidate = self._parse_shopify_money(compare_match.group("v"))
                if candidate is None:
                    candidate = 0
                if candidate > current_price:
                    nominal_price = candidate

        # Fallback for pages that expose only product price metas.
        if current_price is None:
            meta_price = soup.find("meta", attrs={"property": "product:price:amount"})
            if meta_price and meta_price.get("content"):
                current_price = self._parse_amount(meta_price["content"])
        meta_currency = soup.find("meta", attrs={"property": "product:price:currency"})
        if meta_currency and meta_currency.get("content"):
            currency = meta_currency["content"].strip().upper()

        listing = ScrapedListing(
            kijiji_id=self._stable_id(source, url),
            url=url,
            title=title,
            price=current_price,
            currency=currency,
            nominal_price=nominal_price,
            on_sale=nominal_price is not None and current_price is not None and nominal_price > current_price,
            source=source,
            location="Online",
            image_urls=self._extract_images_from_shopify_html(url, html, soup),
        )
        return [listing]

    def _scrape_formbot_vorons(self, url: str) -> list[ScrapedListing]:
        html = self._get(url)

        soup = BeautifulSoup(html, "lxml")
        listings = []
        seen_urls = set()

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "/products/" not in href:
                continue
            product_url = urljoin(url, href.split("?")[0])
            if product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            card_text = link.get_text(" ", strip=True)
            if not card_text:
                continue
            if "voron" not in card_text.lower():
                continue
            try:
                listings.extend(self._scrape_shopify_product(product_url))
            except Exception as e:
                logger.debug(f"Failed to parse formbot product {product_url}: {e}")
                continue

        return listings
