# 3D Printer Listing Tracker

Track 3D printer listings across Kijiji and retailer sites, then automatically detect price drops and deals.

## Features

- **Automated Scraping**: Periodically scrape Kijiji plus selected retailer pages
- **Price Tracking**: Monitor price changes over time with historical snapshots
- **Multi-Currency Storage**: Store one canonical `price` plus `currency` per listing
- **Sale Detection**: Detect sale pricing and keep nominal (non-sale) price when available
- **Deal Detection**: Automatically identify listings with:
  - Price drops from original listing price
  - Prices significantly below MSRP
  - Prices below current retail (via Aurora Tech Channel integration)
- **Brand/Model Detection**: Automatically identify printer brands and models
- **Retail Price Integration**: Compare against live retail prices from Aurora Tech Channel
- **Web Dashboard**: View listings, deals, and price history in a clean interface
- **CLI Tools**: Command-line interface for scraping, viewing deals, and stats

## Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/justinh-rahb/3dp-listing-scrape.git
   cd 3dp-listing-scrape
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Initialize the database**:
   ```bash
   python migrate_db.py
   ```

## Usage

### Command Line

**Run the server (default behavior)**:
```bash
python cli.py
```
Then visit http://127.0.0.1:5000

**Run the server (explicit command)**:
```bash
python cli.py serve --host 0.0.0.0 --port 5000 --workers 1
```

**Run a scrape**:
```bash
python cli.py scrape
```

**View top deals**:
```bash
python cli.py deals --limit 20
```

**Update retail prices from Aurora Tech Channel**:
```bash
python cli.py update-retail-prices
```

**View database statistics**:
```bash
python cli.py stats
```

**Start the server using production entrypoint**:
```bash
python server.py
```

### Docker / Compose

**Build and run with Docker Compose**:
```bash
docker compose up --build -d
```

**View logs**:
```bash
docker compose logs -f tracker
```

**Stop**:
```bash
docker compose down
```

### Web Dashboard

The web dashboard provides:
- **Listings**: Browse all active listings with filtering
- **Deals**: View listings sorted by deal quality
- **Price History**: See price changes over time
- **Settings**: Configure scraping behavior and search queries

## Configuration

### Search Queries

By default, the scraper searches Hamilton (ON) Kijiji for:
- 3d printer
- bambu lab
- prusa
- creality
- ender 3
- anycubic
- voron

It also includes:
- Sovol Zero product page (`sovol3d.com`)
- Formbot Voron collection page (`formbot3d.com`)

You can modify search queries in the Settings page of the web dashboard or by editing the database directly.

### Scraping Settings

Configure scraping behavior in Settings:
- **Scrape Interval**: Hours between automated scrapes (if using scheduler)
- **Max Pages**: Maximum pages to scrape per search query
- **Request Delay**: Random delay between requests (2-5 seconds)
- **Inactive Threshold**: Missed runs before marking a listing inactive
- **FX Rates to USD**: Used for USD-equivalent price change detection (`fx_rates_to_usd`)

## Aurora Tech Channel Integration (Currently Disabled)

The Aurora Tech Channel integration has been temporarily disabled due to HTML parsing complexity. The website structure changes frequently, making reliable scraping difficult.

**Current workaround**: Manually update [msrp_data.json](msrp_data.json) with current prices.

**Future enhancement**: Consider using an API or more robust scraping approach when available.

Running `python cli.py update-retail-prices` will show a message that this feature is disabled.

## Deal Scoring

Deals are scored based on multiple factors:
1. **Savings vs Retail** (highest priority): Amount saved compared to current retail price
2. **Price Drop Percentage**: Discount from original listing price
3. **Newness**: Bonus points for recently listed items
4. **Below Retail Ratio**: Extra points for listings significantly below retail

## Database Schema

The database tracks:
- **listings**: All scraped listings with current and original prices
- **price_snapshots**: Historical price data for each listing
- **scrape_runs**: Log of all scraping operations
- **msrp_entries**: Brand/model MSRP and retail price data
- **brand_keywords**: Keywords for brand detection
- **search_queries**: Configured search URLs
- **settings**: Application configuration

## Development

### Running Tests

```bash
pytest
```

### Database Migration

After updating the schema, run:
```bash
python migrate_db.py
```

### Adding New Brands

Add brand keywords in Settings or edit `config.py`:

```python
DEFAULT_BRAND_KEYWORDS = {
    "brand_name": ["keyword1", "keyword2", "model1", "model2"],
}
```

## Scheduled Scraping

The built-in scheduler now starts automatically with the server by default.
Configure scrape interval in the Settings page (`scrape_interval_hours`).

## Notes

- **Be Respectful**: The scraper includes delays to avoid overloading Kijiji's servers
- **Hamilton Only**: Currently configured for Hamilton, ON listings
- **Price Format**: Listings store both `price` and `currency`
- **Aurora Data**: Retail prices are in USD but tracked as CAD for comparison

## Contributing

Pull requests welcome! Please ensure code follows existing style and includes tests.

## License

MIT License - see LICENSE file for details

## Acknowledgments

- [Aurora Tech Channel](https://auroratechchannel.com/) for providing comprehensive 3D printer pricing data
- Kijiji for providing a platform for local buying/selling
