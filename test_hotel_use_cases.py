"""Ten user-facing hotel workflows, deterministic offline companions to live agent trials."""
import copy
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import hotel_tools as hotels
from test_hotel_tools import page, property_result


BASE = {"adults": 4, "rooms": 1, "children_ages": [], "currency": "USD"}


def query(location="Cancun, Mexico", **changes):
    return {**BASE, "location": location, "check_in": "2027-01-11", "check_out": "2027-01-15", **changes}


def provider(payload, currency, timeout):
    """Return public-shaped offers for exactly the requested search scope."""
    inp = payload["variables"]["input"]
    loc = inp["location"]
    total = 800 if currency == "USD" else 710
    result = property_result(total=total, room_count=inp["nbRooms"], currency=currency)
    result["basicPropertyData"]["location"]["city"] = loc["searchString"]
    result["blocks"][0]["mealPlanIncluded"]["text"] = "Breakfast included"
    for block in result["blocks"]:
        block["mealPlanIncluded"]["text"] = "All-inclusive"
    dates = inp["dates"]
    return page([result], destId=loc.get("destId", 123), destType=loc.get("destType", "CITY"),
                nbAdults=inp["nbAdults"], nbRooms=inp["nbRooms"], nbChildren=inp["nbChildren"], childrenAges=inp["childrenAges"],
                dates={**dates, "lengthOfStayInDays": (hotels.date.fromisoformat(dates["checkout"]) - hotels.date.fromisoformat(dates["checkin"])).days})


class HotelUseCases(unittest.TestCase):
    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_01_cancun_four_stars_five_days_during_a_week(self, fetch):
        plan = hotels.comparison_plan(["Cancun, Mexico"], BASE, "2027-01-11", "2027-01-17", 4)
        self.assertEqual(plan["date_pairs"], [("2027-01-11", "2027-01-15"), ("2027-01-12", "2027-01-16"), ("2027-01-13", "2027-01-17")])
        result = hotels.compare_hotels(plan, {"property_types": ["hotel", "resort"], "stars": [4, 5]})
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(len(result["matrix"]), 3)
        self.assertTrue(all(r["nights"] == 4 and r["adults"] == 4 and r["stars"] >= 4 for r in result["rates"]))

    @patch("hotel_tools.fetch_page")
    def test_02_ten_distinct_five_star_maui_properties(self, fetch):
        def response(payload, currency, timeout):
            data = provider(payload, currency, timeout)
            offers = [property_result(n, total=800 + n) for n in range(1, 13)]
            alternative = copy.deepcopy(offers[0])
            alternative["blocks"][0]["blockId"]["policyGroupId"] = "other-plan"
            offers.insert(1, alternative)
            data["results"] = offers
            data["pagination"]["nbResultsTotal"] = len(offers)
            return data
        fetch.side_effect = response
        result = hotels.search_hotels(query("Maui, Hawaii"), {"stars": [5], "unique_properties": True}, max_results=10)
        self.assertEqual(result["resolved_destination"], {"destination_id": 3183, "destination_type": "REGION"})
        self.assertEqual(len(result["raw_rates"]), 13)
        self.assertEqual(len(result["rates"]), 10)
        self.assertEqual(len({r["property_id"] for r in result["rates"]}), 10)

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_03_compare_maui_and_san_juan_same_occupancy(self, fetch):
        plan = hotels.comparison_plan(["Maui, Hawaii", "San Juan, Puerto Rico"], BASE, "2027-01-11", "2027-01-15", 4)
        result = hotels.compare_hotels(plan, {"property_types": ["hotel"]})
        self.assertEqual(len(result["cheapest_by_location"]), 2)
        self.assertEqual(result["coverage"]["completed_searches"], 2)
        self.assertEqual({s["query"]["adults"] for s in result["searches"]}, {4})
        self.assertEqual({s["resolved_destination"]["destination_type"] for s in result["searches"]}, {"CITY", "REGION"})
        hotels.summarize_comparison(result, max_results=1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selected.json"
            path.write_text(json.dumps(result))
            with redirect_stdout(io.StringIO()) as out:
                hotels.main(["filter", "--input", str(path), "--format", "csv"])
            rows = list(csv.DictReader(io.StringIO(out.getvalue())))
            self.assertEqual(sum(r["record_type"] == "rate" for r in rows), 1)
            self.assertEqual(sum(r["record_type"] == "coverage" for r in rows), 2)
            with redirect_stdout(io.StringIO()) as out:
                hotels.main(["filter", "--input", str(path), "--all-results"])
            self.assertEqual(len(json.loads(out.getvalue())["rates"]), 2)

    @patch("hotel_tools.fetch_page")
    def test_04_cheapest_five_star_maui_stay_anywhere_in_january(self, fetch):
        def response(payload, currency, timeout):
            data = provider(payload, currency, timeout)
            day = int(payload["variables"]["input"]["dates"]["checkin"][-2:])
            data["results"] = [property_result(total=2000 - 10 * day)]
            return data
        fetch.side_effect = response
        plan = hotels.comparison_plan(["Maui, Hawaii"], BASE, "2027-01-01", "2027-01-31", 4)
        result = hotels.compare_hotels(plan, {"stars": [5], "unique_properties": True})
        self.assertEqual(fetch.call_count, 27)
        self.assertEqual(result["coverage"]["date_coverage"], "every_eligible_check_in")
        self.assertEqual(result["cheapest_displayed"]["check_in"], "2027-01-27")
        self.assertEqual(result["cheapest_displayed"]["check_out"], "2027-01-31")
        with self.assertRaises(ValueError):
            hotels.comparison_plan(["Maui"], BASE, "2027-01-01", "2027-01-31", 4, max_searches=2)

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_05_family_ages_breakfast_rating_and_nightly_budget(self, fetch):
        result = hotels.search_hotels(query(adults=2, children_ages=[7, 12]),
            {"stars": [4, 5], "meal_plan": "breakfast", "max_nightly_price": 300, "exact_rooms": True})
        rate = result["rates"][0]
        self.assertEqual(rate["children_ages"], [7, 12])
        self.assertEqual(rate["adults"], 2)
        self.assertLessEqual(rate["displayed_nightly_average"], 300)
        self.assertEqual(fetch.call_args.args[0]["variables"]["input"]["nbChildren"], 2)

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_06_cabo_two_rooms_all_inclusive_five_nights_cancellation(self, fetch):
        result = hotels.search_hotels(query("Cabo San Lucas", rooms=2, check_in="2027-01-18", check_out="2027-01-23"),
            {"meal_plan": "all-inclusive", "exact_rooms": True, "free_cancellation": True})
        rate = result["rates"][0]
        self.assertEqual(rate["nights"], 5)
        self.assertEqual(rate["offered_rooms"], 2)
        self.assertEqual(len(rate["rate_blocks"]), 2)
        self.assertTrue(rate["free_cancellation_advertised"])
        self.assertIsNone(rate["refund_terms"])

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_07_san_juan_usd_and_eur_are_separate_quotes(self, fetch):
        filters = {"min_review_score": 8, "max_total_price": 1000}
        usd = hotels.search_hotels(query("San Juan, Puerto Rico"), filters)
        eur = hotels.search_hotels(query("San Juan, Puerto Rico", currency="EUR"), filters)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(usd["rates"][0]["currency"], "USD")
        self.assertEqual(eur["rates"][0]["currency"], "EUR")
        self.assertNotEqual(usd["rates"][0]["displayed_total"], eur["rates"][0]["displayed_total"])

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_08_saved_budget_clear_filters_and_csv_without_network(self, fetch):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "new" / "nested" / "quote.json"
            hotels.main(["search", "--location", "Cancun", "--check-in", "2027-01-11", "--check-out", "2027-01-15", "--adults", "4", "--output", str(target)])
            self.assertTrue(target.is_file())
            with redirect_stdout(io.StringIO()) as out:
                hotels.main(["filter", "--input", str(target), "--max-total-price", "10"])
            limited = json.loads(out.getvalue())
            self.assertEqual(limited["status"], "filtered_out")
            target.write_text(json.dumps(limited))
            with redirect_stdout(io.StringIO()) as out:
                hotels.main(["filter", "--input", str(target), "--clear-filters", "--format", "csv"])
            rows = list(csv.DictReader(io.StringIO(out.getvalue())))
            self.assertEqual(len(rows), 2)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(rows[1]["quote_checked_at"], limited["raw_rates"][0]["quote_checked_at"])

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_09_one_vs_two_rooms_are_separate_party_offers(self, fetch):
        one = hotels.search_hotels(query(rooms=1), {"exact_rooms": True})
        two = hotels.search_hotels(query(rooms=2), {"exact_rooms": True})
        self.assertEqual(one["rates"][0]["offered_rooms"], 1)
        self.assertEqual(two["rates"][0]["offered_rooms"], 2)
        self.assertEqual(two["rates"][0]["rate_blocks"][0]["price"], 400)
        self.assertEqual(two["rates"][0]["displayed_total"], 800)

    @patch("hotel_tools.fetch_page", side_effect=provider)
    def test_10_impossible_budget_is_not_provider_failure_or_wrong_geography(self, fetch):
        result = hotels.search_hotels(query("Maui, Hawaii"), {"stars": [5], "max_total_price": 50})
        self.assertEqual(result["status"], "filtered_out")
        self.assertEqual(len(result["raw_rates"]), 1)
        fetch.side_effect = hotels.SourceError("blocked", "Verification required")
        plan = hotels.comparison_plan(["Maui"], BASE, "2027-01-01", "2027-01-31", 4)
        fetch.reset_mock()
        blocked = hotels.compare_hotels(plan, {})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(blocked["coverage"]["completed_searches"], 1)
        self.assertEqual(blocked["source_errors"][0]["status"], "blocked")
        wrong = page(destId=276309, destType="HOTEL")
        with self.assertRaises(hotels.SourceError):
            hotels.verify_scope(wrong, hotels.validate_query(query("Unmapped place")))


if __name__ == "__main__":
    unittest.main()
