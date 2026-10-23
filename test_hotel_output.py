"""Compact output preserves decision facts while full snapshots stay replayable."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import hotel_tools as hotels
from test_hotel_tools import query
from test_hotel_use_cases import BASE, provider


class CompactHotelOutputTests(unittest.TestCase):
    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_summary_keeps_city_room_fees_terms_and_original_quote_identity(self, fetch):
        snapshot = hotels.search_hotels(query(), {"unique_properties": True})
        original = snapshot["rates"][0]
        original["city"] = "Nearby actual town"
        summary = hotels.compact_summary(snapshot)
        view = summary["rates"][0]
        for field in ("property_id", "city", "requested_rooms", "offered_rooms", "known_excluded_charges_total",
                      "fees_status", "final_payable_total", "quote_checked_at", "booking_url", "refund_terms"):
            self.assertEqual(view[field], original[field])
        self.assertEqual(view["city"], "Nearby actual town")
        self.assertEqual(view["cancellation_deadlines"], ["2027-01-10T07:00:00Z"])
        self.assertEqual(summary["counts"]["matching_unit"], "properties")
        self.assertFalse(summary["replayable"])
        self.assertGreaterEqual(snapshot["timings"]["total_seconds"], snapshot["timings"]["fetch_decode_seconds"])
        self.assertEqual(snapshot["timings"]["http_requests"], 1)

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_comparison_summary_preserves_all_destination_minima_and_cell_coverage(self, fetch):
        plan = hotels.comparison_plan(["Maui", "San Juan"], BASE, "2027-01-11", "2027-01-15", 4)
        snapshot = hotels.compare_hotels(plan, {}, max_results=1)
        summary = hotels.compact_summary(snapshot)
        self.assertEqual(len(summary["rates"]), 1)
        self.assertEqual(len(summary["cheapest_by_location"]), 2)
        self.assertEqual(len(summary["searches"]), 2)
        self.assertEqual(summary["searches"][0]["coverage"], snapshot["searches"][0]["coverage"])
        self.assertEqual(snapshot["timings"]["http_requests"], 2)
        self.assertNotIn("queries", summary["plan"])

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_cli_summary_and_snapshot_separate_execution_from_quote_timing(self, fetch):
        snapshot = hotels.search_hotels(query(), {})
        with tempfile.TemporaryDirectory() as tmp:
            source, full = Path(tmp) / "source.json", Path(tmp) / "full.json"
            source.write_text(json.dumps(snapshot))
            with redirect_stdout(io.StringIO()) as output:
                code = hotels.main(["filter", "--input", str(source), "--snapshot", str(full), "--format", "summary"])
            view = json.loads(output.getvalue())
            canonical = json.loads(full.read_text())
            self.assertEqual(code, 0)
            self.assertEqual(view["execution"]["http_requests"], 0)
            self.assertEqual(view["quote_timings"], snapshot["timings"])
            self.assertEqual(canonical["rates"][0]["quote_checked_at"], snapshot["rates"][0]["quote_checked_at"])
            self.assertIn("rate_blocks", canonical["raw_rates"][0])
            self.assertEqual(view["snapshot_path"], str(full.resolve()))
            self.assertEqual(fetch.call_count, 1)

    @patch("hotel_tools.fetch_page")
    def test_compact_dry_run_saves_complete_daily_plan_without_fetching(self, fetch):
        with tempfile.TemporaryDirectory() as tmp:
            full = Path(tmp) / "plan.json"
            with redirect_stdout(io.StringIO()) as output:
                code = hotels.main(["compare", "--location", "Maui", "--start-date", "2027-01-01", "--end-date", "2027-01-31",
                                    "--nights", "4", "--adults", "4", "--dry-run", "--format", "summary", "--snapshot", str(full)])
            view = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(view["planned_searches"], 27)
            self.assertEqual(view["first_stay"], ["2027-01-01", "2027-01-05"])
            self.assertEqual(view["last_stay"], ["2027-01-27", "2027-01-31"])
            self.assertEqual(len(json.loads(full.read_text())["queries"]), 27)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
