import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import db
import scheduler
import tracker
from models import ScrapeOutcome, ScrapedListing
from notifier import _format_discord, _format_google_chat
from scraper import KijijiScraper


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self._responses = iter(responses)

    def get(self, url, timeout):
        return next(self._responses)


class ScraperOutcomeTests(unittest.TestCase):
    def test_blocked_kijiji_request_preserves_failed_url(self):
        session = FakeSession([FakeResponse(403)])
        scraper = KijijiScraper(session=session, delay_min=0, delay_max=0)

        outcome = scraper.scrape_search("https://www.kijiji.ca/b-hamilton/prusa/k0l80014")

        self.assertEqual("blocked", outcome.status)
        self.assertEqual(403, outcome.http_status)
        self.assertEqual("https://www.kijiji.ca/b-hamilton/prusa/k0l80014", outcome.failed_url)
        self.assertEqual(1, outcome.pages_attempted)
        self.assertEqual(0, outcome.pages_completed)

    def test_later_page_failure_is_partial_and_keeps_results(self):
        session = FakeSession([FakeResponse(200, "page one"), FakeResponse(500)])
        scraper = KijijiScraper(session=session, delay_min=0, delay_max=0, max_pages=2)
        listing = ScrapedListing(kijiji_id="123", url="https://example/123", title="Printer")
        scraper._parse_search_page = Mock(return_value=([listing], True))

        outcome = scraper.scrape_search("https://www.kijiji.ca/b-hamilton/prusa/k0l80014")

        self.assertEqual("partial", outcome.status)
        self.assertEqual([listing], outcome.listings)
        self.assertEqual(2, outcome.pages_attempted)
        self.assertEqual(1, outcome.pages_completed)
        self.assertIn("page-2", outcome.failed_url)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test.db")
        db.init_db(self.db_path)
        self.conn = db.get_conn(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def add_listing(self, listing_id: str):
        db.upsert_listing(
            {
                "kijiji_id": listing_id,
                "url": f"https://example/{listing_id}",
                "title": f"Printer {listing_id}",
                "price": 100,
                "currency": "CAD",
            },
            conn=self.conn,
        )


class QueryVisibilityTests(DatabaseTestCase):
    def test_single_query_ages_only_its_own_listings(self):
        query_one, query_two = db.get_search_queries(conn=self.conn)[:2]
        self.add_listing("only-one")
        self.add_listing("only-two")
        db.record_listing_query("only-one", query_one["id"], "2026-09-09T00:00:00+00:00", self.conn)
        db.record_listing_query("only-two", query_two["id"], "2026-09-09T00:00:00+00:00", self.conn)
        self.conn.commit()

        for _ in range(3):
            db.finalize_query_visibility(query_one["id"], set(), self.conn)

        self.assertEqual(0, db.get_listing("only-one", self.conn)["is_active"])
        self.assertEqual(1, db.get_listing("only-two", self.conn)["is_active"])


class ListingManagementTests(DatabaseTestCase):
    def test_status_filter_selects_active_inactive_or_all(self):
        self.add_listing("active")
        self.add_listing("inactive")
        self.conn.execute("UPDATE listings SET is_active = 0 WHERE kijiji_id = 'inactive'")
        self.conn.commit()

        active = db.get_listings({"listing_status": "active"}, self.conn)
        inactive = db.get_listings({"listing_status": "inactive"}, self.conn)
        all_listings = db.get_listings({"listing_status": "all"}, self.conn)

        self.assertEqual(["active"], [row["kijiji_id"] for row in active])
        self.assertEqual(["inactive"], [row["kijiji_id"] for row in inactive])
        self.assertEqual({"active", "inactive"}, {row["kijiji_id"] for row in all_listings})

    def test_prune_inactive_preserves_active_listings(self):
        self.add_listing("keep")
        self.add_listing("prune")
        self.conn.execute("UPDATE listings SET is_active = 0 WHERE kijiji_id = 'prune'")
        self.conn.execute(
            "INSERT INTO price_snapshots (kijiji_id, price, scraped_at) VALUES (?, ?, ?)",
            ("prune", 100, "2026-09-09T00:00:00+00:00"),
        )
        self.conn.commit()

        deleted = db.delete_inactive_listings(self.conn)
        self.conn.commit()

        self.assertEqual(1, deleted)
        self.assertIsNotNone(db.get_listing("keep", self.conn))
        self.assertIsNone(db.get_listing("prune", self.conn))
        snapshots = self.conn.execute(
            "SELECT COUNT(*) AS c FROM price_snapshots WHERE kijiji_id = 'prune'"
        ).fetchone()["c"]
        self.assertEqual(0, snapshots)

    def test_stale_filter_and_prune_handle_legacy_active_rows(self):
        self.add_listing("fresh")
        self.add_listing("legacy-stale")
        self.conn.execute(
            "UPDATE listings SET last_seen = '2020-01-01T00:00:00+00:00' "
            "WHERE kijiji_id = 'legacy-stale'"
        )
        self.conn.execute(
            "INSERT INTO price_snapshots (kijiji_id, price, scraped_at) VALUES (?, ?, ?)",
            ("legacy-stale", 100, "2020-01-01T00:00:00+00:00"),
        )
        self.conn.commit()

        stale = db.get_listings(
            {"listing_status": "active", "stale_days": 30}, self.conn
        )
        self.assertEqual(["legacy-stale"], [row["kijiji_id"] for row in stale])
        self.assertEqual(1, db.count_stale_listings(30, "active", self.conn))

        deleted = db.delete_stale_listings(30, "active", self.conn)
        self.conn.commit()
        self.assertEqual(1, deleted)
        self.assertIsNotNone(db.get_listing("fresh", self.conn))


class DealNotificationTests(DatabaseTestCase):
    def test_same_deal_state_is_not_notified_twice(self):
        self.add_listing("deal-one")
        self.conn.commit()
        deal = {
            "kijiji_id": "deal-one",
            "currency": "CAD",
            "current_price": 100,
            "price_drop_pct": 20,
            "price_to_retail_ratio": 0.8,
        }

        first = db.get_unnotified_deals([deal], self.conn)
        db.mark_deals_notified(first, self.conn)
        second = db.get_unnotified_deals([deal], self.conn)

        self.assertEqual(1, len(first))
        self.assertEqual([], second)


class CrossProcessLockTests(DatabaseTestCase):
    def test_scrape_lock_rejects_second_owner(self):
        original_get_conn = db.get_conn

        def temp_get_conn(db_path=self.db_path):
            return original_get_conn(self.db_path)

        with patch.object(db, "get_conn", side_effect=temp_get_conn):
            self.assertTrue(db.try_acquire_app_lock("scrape"))
            self.assertFalse(db.try_acquire_app_lock("scrape"))
            db.release_app_lock("scrape")
            self.assertTrue(db.try_acquire_app_lock("scrape"))
            db.release_app_lock("scrape")


class CurrencySafetyTests(unittest.TestCase):
    def test_msrp_ratio_is_omitted_when_currencies_do_not_match(self):
        listing = {
            "kijiji_id": "usd-product",
            "title": "USD product",
            "url": "https://example/product",
            "current_price": 500,
            "original_price": 700,
            "currency": "USD",
            "msrp": 600,
            "msrp_currency": "CAD",
            "first_seen": "2026-09-09T00:00:00+00:00",
        }

        deal = tracker.compute_deals([listing])[0]

        self.assertIsNone(deal.price_to_msrp_ratio)


class CatalogQualityTests(DatabaseTestCase):
    def test_catalog_upgrade_removes_legacy_unverified_rows(self):
        db.upsert_msrp_entry("legacy", "Mystery 9000", 123, conn=self.conn)
        db.add_brand_keyword("legacy", "mystery", conn=self.conn)
        self.conn.execute("DELETE FROM settings WHERE key = 'reference_catalog_version'")
        self.conn.commit()

        db._upgrade_reference_catalog(self.conn)

        models = {(row["brand"], row["model"]) for row in db.get_msrp_entries(self.conn)}
        self.assertNotIn(("legacy", "Mystery 9000"), models)
        self.assertEqual(len(db._catalog_entries()), len(models))
        self.assertNotIn("legacy", db.get_brand_keywords_map(self.conn))

    def test_bundled_catalog_requires_evidence_and_never_invents_second_currency(self):
        for brand, model, details in db._catalog_entries():
            with self.subTest(brand=brand, model=model):
                self.assertTrue(details.get("source_url", "").startswith("https://"))
                self.assertTrue(details.get("source_name"))
                self.assertRegex(details.get("verified_at", ""), r"^\d{4}-\d{2}-\d{2}$")
                self.assertIn(details.get("product_status"), {"current", "discontinued"})
                self.assertFalse(
                    details.get("msrp_cad") is not None and details.get("msrp_usd") is not None,
                    "Bundled prices must come from a source, not an implicit conversion",
                )
                if details.get("price_basis") == "unavailable":
                    self.assertIsNone(details.get("msrp_cad"))
                    self.assertIsNone(details.get("msrp_usd"))
                else:
                    self.assertTrue(
                        details.get("msrp_cad") is not None or details.get("msrp_usd") is not None
                    )

    def test_catalog_keeps_source_provenance_and_currency_independent(self):
        entries = {
            (entry["brand"], entry["model"]): entry
            for entry in db.get_msrp_entries(self.conn)
        }
        prusa = entries[("prusa", "MK4S")]
        self.assertIsNone(prusa["msrp_cad"])
        self.assertEqual(729, prusa["msrp_usd"])
        self.assertEqual("manufacturer_list_price", prusa["price_basis"])
        self.assertTrue(prusa["source_url"].startswith("https://www.prusa3d.com/"))
        self.assertEqual("2026-09-09", prusa["verified_at"])

    def test_export_import_round_trip_preserves_catalog_evidence(self):
        exported = db.export_app_data("msrp", self.conn)
        self.conn.execute("DELETE FROM msrp_entries")
        result = db.import_app_data(exported, "msrp", conn=self.conn)
        self.assertGreater(result["msrp"], 0)
        restored = next(
            row for row in db.get_msrp_entries(self.conn)
            if row["brand"] == "bambu" and row["model"] == "X1 Carbon"
        )
        self.assertIn("X1C", json.loads(restored["aliases"]))
        self.assertEqual("discontinued", restored["product_status"])

    def test_model_matching_prefers_specific_models_and_aliases(self):
        with patch.object(tracker, "_get_msrp_data", return_value=db.get_msrp_map(self.conn)):
            self.assertEqual("A1 Mini", tracker.detect_model("Bambu Lab A1 Mini", brand="bambu"))
            self.assertEqual("K1C", tracker.detect_model("Creality K1C", brand="creality"))
            self.assertEqual("X1 Carbon", tracker.detect_model("Bambu X1C", brand="bambu"))

    def test_brand_matching_uses_boundaries_and_canonical_creality(self):
        keywords = db.get_brand_keywords_map(self.conn)
        with patch.object(tracker, "_get_brand_keywords", return_value=keywords):
            self.assertEqual("creality", tracker.detect_brand("Ender 3 V3 printer"))
            self.assertIsNone(tracker.detect_brand("Excellent printer with mega bundle"))


class SchedulerDiagnosticsTests(DatabaseTestCase):
    def test_failed_query_is_persisted_and_returned(self):
        for query in db.get_search_queries(conn=self.conn):
            db.update_search_query(query["id"], enabled=False, conn=self.conn)
        query_id = db.add_search_query(
            "https://www.kijiji.ca/b-hamilton/test/k0l80014", "diagnostic query", conn=self.conn
        )
        self.conn.commit()

        original_get_conn = db.get_conn

        def temp_get_conn(db_path=self.db_path):
            return original_get_conn(self.db_path)

        failed = ScrapeOutcome(
            status="blocked",
            requested_url="https://www.kijiji.ca/b-hamilton/test/k0l80014",
            failed_url="https://www.kijiji.ca/b-hamilton/test/page-2/k0l80014",
            pages_attempted=2,
            pages_completed=1,
            http_status=403,
            error_type="HttpError",
            error_message="HTTP 403 (blocked)",
        )
        scraper_instance = Mock()
        scraper_instance.scrape_search.return_value = failed

        with patch.object(db, "get_conn", side_effect=temp_get_conn), \
             patch.object(scheduler, "KijijiScraper", return_value=scraper_instance), \
             patch.object(scheduler, "_emit_event", return_value=False):
            result = scheduler.run_scrape()

        self.assertEqual("failed", result["status"])
        self.assertEqual(query_id, result["failures"][0]["query_id"])
        self.assertEqual(403, result["failures"][0]["http_status"])

        run = db.get_recent_scrape_runs(limit=1, conn=self.conn)[0]
        self.assertEqual("failed", run["status"])
        self.assertEqual("diagnostic query", run["queries"][0]["label"])
        self.assertIn("page-2", run["queries"][0]["failed_url"])

    def test_success_records_query_ownership_and_empty_result_does_not_age_it(self):
        for query in db.get_search_queries(conn=self.conn):
            db.update_search_query(query["id"], enabled=False, conn=self.conn)
        query_id = db.add_search_query(
            "https://www.kijiji.ca/b-hamilton/test/k0l80014", "owned query", conn=self.conn
        )
        self.conn.commit()
        original_get_conn = db.get_conn

        def temp_get_conn(db_path=self.db_path):
            return original_get_conn(self.db_path)

        listing = ScrapedListing(
            kijiji_id="owned-listing",
            url="https://www.kijiji.ca/v/owned-listing/123",
            title="Bambu Lab A1",
            price=300,
            currency="CAD",
        )
        scraper_instance = Mock()
        scraper_instance.scrape_search.return_value = ScrapeOutcome(
            status="success",
            requested_url="https://www.kijiji.ca/b-hamilton/test/k0l80014",
            listings=[listing],
            pages_attempted=1,
            pages_completed=1,
        )

        with patch.object(db, "get_conn", side_effect=temp_get_conn), \
             patch.object(scheduler, "KijijiScraper", return_value=scraper_instance), \
             patch.object(scheduler, "_emit_event", return_value=False):
            first = scheduler.run_scrape()
            scraper_instance.scrape_search.return_value = ScrapeOutcome(
                status="empty",
                requested_url="https://www.kijiji.ca/b-hamilton/test/k0l80014",
                pages_attempted=1,
                pages_completed=1,
            )
            second = scheduler.run_scrape()

        self.assertEqual("success", first["status"])
        self.assertEqual("success", second["status"])
        association = self.conn.execute(
            "SELECT * FROM listing_queries WHERE kijiji_id = ? AND query_id = ?",
            ("owned-listing", query_id),
        ).fetchone()
        self.assertIsNotNone(association)
        self.assertEqual(0, association["missed_runs"])
        self.assertEqual(1, db.get_listing("owned-listing", self.conn)["is_active"])


class NotificationTests(unittest.TestCase):
    def test_failure_formats_include_query_and_url(self):
        event = {
            "event": "scrape_failed",
            "data": {
                "run_id": 42,
                "queries_total": 3,
                "queries_succeeded": 2,
                "queries_failed": 1,
                "found": 20,
                "new": 2,
                "price_changes": 1,
                "failures": [{
                    "label": "qidi plus4",
                    "url": "https://example/product",
                    "failed_url": "https://example/product",
                    "status": "blocked",
                    "http_status": 403,
                    "error_type": "HttpError",
                    "error_message": "HTTP 403 (blocked)",
                }],
            },
        }

        for payload in (_format_discord(event), _format_google_chat(event)):
            text = payload.get("content") or payload.get("text")
            self.assertIn("qidi plus4", text)
            self.assertIn("https://example/product", text)
            self.assertIn("HTTP 403", text)
            self.assertIn("Run: #42", text)


if __name__ == "__main__":
    unittest.main()
