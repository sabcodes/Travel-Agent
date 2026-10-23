"""Offline checks for Booking.com quote scope, money semantics, and CLI replay."""
import copy
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import hotel_tools as hotels


def query(**changes):
    return hotels.validate_query({"location": "Cabo San Lucas", "destination_id": -1698603,
        "destination_type": "CITY", "check_in": "2027-01-15", "check_out": "2027-01-19",
        "adults": 4, "rooms": 1, "children_ages": [], "currency": "USD", **changes})


def price(value, currency="USD"):
    return {"amountUnformatted": value, "currency": currency}


def property_result(property_id=1, total=800, room_count=1, currency="USD", **changes):
    block = {"blockId": {"roomId": "101", "policyGroupId": "201", "occupancy": 2,
                        "mealPlanId": 85, "bundleId": 0, "packageId": "0"},
             "finalPrice": {"amount": total / room_count, "currency": currency},
             "mealPlanIncluded": {"text": "All-inclusive"}, "freeCancellationUntil": "2027-01-10T07:00:00Z"}
    result = {"basicPropertyData": {"id": property_id, "pageName": "example-hotel", "accommodationTypeId": 204,
                "location": {"countryCode": "mx", "city": "Cabo San Lucas"}, "starRating": {"value": 5},
                "reviewScore": {"score": 8.5, "reviewCount": 100, "showScore": True}},
              "displayName": {"text": "Example hotel"}, "priceDisplayInfoIrene": {
                  "displayPrice": {"amountPerStay": price(total, currency)}, "taxExceptions": [],
                  "excludedCharges": {"excludeChargesList": [], "excludeChargesAggregated": {"amountPerStay": price(0, currency)}}},
              "matchingUnitConfigurations": {"commonConfiguration": {"nbUnits": room_count},
                  "unitConfigurations": [{"name": "Suite", "nbUnits": room_count}]},
              "blocks": [copy.deepcopy(block) for _ in range(room_count)], "policies": {"showFreeCancellation": True},
              "mealPlanIncluded": {"text": "All-inclusive"}, "persuasion": {"autoextended": False},
              "soldOutInfo": {"isSoldOut": False}, "recommendedDate": {}, "nonMatchingFlexibleFilterOptions": []}
    result.update(changes)
    return result


def page(results=None, total=None, **meta_changes):
    results = [property_result()] if results is None else results
    return {"results": results, "pagination": {"nbResultsTotal": len(results) if total is None else total},
            "searchMeta": {"destId": -1698603, "destType": "CITY", "nbAdults": 4, "nbRooms": 1,
                "nbChildren": 0, "childrenAges": [], "dates": {"checkin": "2027-01-15", "checkout": "2027-01-19", "lengthOfStayInDays": 4}, **meta_changes}}


def rate(result=None, **query_changes):
    return hotels.parse_rate(result or property_result(), query(**query_changes), "2026-10-22T12:00:00Z", "booking_com_http")[0]


class HotelQuoteTests(unittest.TestCase):
    def test_party_total_is_not_a_single_room_price_and_blocks_remain_repeated(self):
        result = rate(property_result(total=1200, room_count=2))
        self.assertEqual(result["displayed_total"], 1200)
        self.assertEqual(result["displayed_nightly_average"], 300)
        self.assertEqual([b["price"] for b in result["rate_blocks"]], [600, 600])
        self.assertEqual(result["room_match"], "different")
        self.assertEqual(hotels.filter_rates([result], {"exact_rooms": True}), [])
        self.assertEqual(len(hotels.filter_rates([result], {})), 1)

    def test_fee_aggregate_never_adds_included_taxes_again(self):
        result = property_result()
        result["priceDisplayInfoIrene"]["excludedCharges"] = {
            "excludeChargesList": [
                {"chargeInclusion": "EXCLUDED", "chargeMode": "PER_NIGHT", "amountPerStay": price(18)},
                {"chargeInclusion": "INCLUDED", "chargeMode": "PERCENTAGE", "amountPerStay": price(100)}],
            "excludeChargesAggregated": {"amountPerStay": price(118)}}
        parsed = rate(result)
        self.assertEqual(parsed["known_excluded_charges_total"], 18)
        self.assertEqual(parsed["displayed_plus_known_excluded"], 818)
        self.assertEqual(parsed["fees_status"], "inconsistent_aggregate")
        self.assertIsNone(parsed["final_payable_total"])
        self.assertFalse(parsed["taxes_and_fees_complete"])
        self.assertEqual(parsed["reported_charges"][0]["amount_per_stay"], 18)  # Already a stay amount; don't multiply again.

    def test_unknown_fees_currency_and_prices_do_not_turn_into_zero(self):
        result = property_result()
        result["priceDisplayInfoIrene"].pop("excludedCharges")
        parsed = rate(result)
        self.assertIsNone(parsed["known_excluded_charges_total"])
        self.assertEqual(parsed["fees_status"], "unknown")
        for value in (None, 0, -1, float("nan"), float("inf"), True):
            result = property_result()
            result["priceDisplayInfoIrene"]["displayPrice"]["amountPerStay"]["amountUnformatted"] = value
            parsed, excluded = hotels.parse_rate(result, query(), "now", "booking_com_http")
            self.assertIsNone(parsed)
            self.assertIn("price", excluded["reason"])
        self.assertIsNone(rate(property_result(currency="MXN")))

    def test_suggested_dates_sold_out_and_autoextended_properties_are_excluded(self):
        alternatives = [property_result(recommendedDate={"checkin": "2027-01-16"}),
                        property_result(persuasion={"autoextended": True}),
                        property_result(nonMatchingFlexibleFilterOptions=[{"id": "outside_filters"}]),
                        property_result(soldOutInfo={"isSoldOut": True})]
        for result in alternatives:
            self.assertIsNone(rate(result))

    def test_scope_rejects_mismatched_dates_guests_rooms_children_and_destination(self):
        for changes in ({"nbAdults": 2}, {"nbRooms": 2}, {"nbChildren": 1}, {"destId": 123}, {"destType": "REGION"},
                        {"dates": {"checkin": "2027-01-15", "checkout": "2027-01-20", "lengthOfStayInDays": 5}}):
            with self.assertRaises(hotels.SourceError):
                hotels.verify_scope(page(**changes), query())
        q = query(children_ages=[4, 8])
        with self.assertRaises(hotels.SourceError):
            hotels.verify_scope(page(nbChildren=2, childrenAges=[5, 8]), q)

    def test_date_currency_guest_and_filter_validation(self):
        for changes in ({"check_out": "2027-01-15"}, {"check_in": "2027-02-30"}, {"adults": 0}, {"rooms": True},
                        {"children_ages": [18]}, {"currency": "usd"}):
            with self.assertRaises(ValueError):
                query(**changes)
        self.assertEqual(query(check_in="2028-02-28", check_out="2028-03-01")["nights"], 2)
        for filters in ({"max_total_price": float("nan")}, {"stars": [6]}, {"property_types": ["castle"]}):
            with self.assertRaises(ValueError):
                hotels.validate_filters(filters)

    def test_filters_ties_and_rounding_are_deterministic(self):
        a, b = rate(property_result(1, total=800)), rate(property_result(2, total=799.99))
        self.assertEqual(hotels.filter_rates([a, b], {"max_nightly_price": 199.999})[0]["property_id"], "2")
        self.assertEqual(hotels.filter_rates([a], {"property_types": ["resort"]}), [])
        self.assertEqual(hotels.filter_rates([a], {"meal_plan": "all-inclusive", "stars": [5], "free_cancellation": True}), [a])
        self.assertEqual(hotels.filter_rates([a], {"min_review_score": 9}), [])
        a["free_cancellation_advertised"] = None
        self.assertEqual(hotels.filter_rates([a], {"free_cancellation": True}), [])

    @patch("hotel_tools.fetch_page")
    def test_pagination_deduplication_and_partial_coverage(self, fetch):
        fetch.side_effect = [page(total=3), page([property_result(1), property_result(2)], total=3)]
        snapshot = hotels.search_hotels(query(), {}, max_pages=2)
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(len(snapshot["raw_rates"]), 2)
        self.assertEqual(snapshot["coverage"]["rows_fetched"], 3)
        self.assertEqual(fetch.call_args.args[0]["variables"]["input"]["pagination"]["offset"], 1)
        fetch.side_effect = None
        fetch.return_value = page(total=40)
        snapshot = hotels.search_hotels(query(), {}, max_pages=1)
        self.assertEqual(snapshot["status"], "partial")
        self.assertFalse(snapshot["coverage"]["all_reported_rows_fetched"])

    @patch("hotel_tools.fetch_page")
    def test_failure_cancellation_and_missing_prices_preserve_honest_states(self, fetch):
        fetch.side_effect = [page(total=2), hotels.SourceError("rate_limited", "HTTP 429", "60")]
        snapshot = hotels.search_hotels(query(), {}, max_pages=2)
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(len(snapshot["raw_rates"]), 1)
        self.assertEqual(snapshot["source_errors"][0]["retry_after"], "60")
        fetch.side_effect = [page(total=2), KeyboardInterrupt()]
        snapshot = hotels.search_hotels(query(), {}, max_pages=2)
        self.assertEqual(snapshot["source_errors"][0]["status"], "cancelled")
        self.assertEqual(len(snapshot["rates"]), 1)
        fetch.side_effect = None
        fetch.return_value = page([property_result(currency="MXN")])
        snapshot = hotels.search_hotels(query(), {})
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(snapshot["excluded_properties"][0]["reason"], "missing_price_or_currency_mismatch")
        fetch.return_value = page([], total=0)
        self.assertEqual(hotels.search_hotels(query(), {})["status"], "no_matching_rates")

    def test_graphql_errors_are_not_empty_inventory(self):
        for response in ({"data": None, "errors": [{"message": "failed"}]}, {"data": {}}, []):
            with self.assertRaises(hotels.SourceError):
                hotels.decode_search(response)

    @patch("hotel_tools.fetch_page")
    def test_distinct_rate_plans_and_export_limit_replay_preserve_candidates(self, fetch):
        first = property_result()
        second = copy.deepcopy(first)
        second["blocks"][0]["blockId"]["policyGroupId"] = "different-policy"
        fetch.return_value = page([first, second])
        snapshot = hotels.search_hotels(query(), {}, max_results=1)
        self.assertEqual(len(snapshot["rates"]), 1)
        self.assertEqual(len(snapshot["raw_rates"]), 2)
        self.assertEqual(snapshot["matching_rate_count"], 2)
        hotels.summarize(snapshot)
        self.assertEqual(len(snapshot["rates"]), 2)

    @patch("hotel_tools.urlopen")
    def test_http_denials_have_no_retry_and_no_captured_authentication(self, urlopen):
        for status in (403, 429):
            urlopen.reset_mock()
            urlopen.side_effect = HTTPError(hotels.ENDPOINT, status, "denied", {"Retry-After": "60"}, None)
            snapshot = hotels.search_hotels(query(), {})
            self.assertEqual(snapshot["status"], "blocked" if status == 403 else "rate_limited")
            self.assertEqual(urlopen.call_count, 1)
            request = urlopen.call_args.args[0]
            self.assertEqual({k.lower() for k in request.headers}, {"content-type", "user-agent"})
            body = json.loads(request.data)
            self.assertNotIn("rawQueryForSession", body["variables"]["input"])

    def test_har_import_preserves_timestamp_and_csv_json_replay_keeps_rate_terms(self):
        inp = hotels.search_payload(query(), {}, 0, 20)["variables"]["input"]
        captured_cookie = "never-export-this-session-value"
        inp["rawQueryForSession"] = captured_cookie
        entry = {"startedDateTime": "2026-10-22T12:00:00Z", "request": {
            "url": hotels.ENDPOINT, "headers": [{"name": "Cookie", "value": captured_cookie}],
            "postData": {"text": json.dumps({"operationName": "FullSearch", "variables": {"input": inp}})}},
            "response": {"status": 200, "content": {"text": json.dumps({"data": {"searchQueries": {"search": page()}}})}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            path.write_text(json.dumps({"log": {"entries": [entry]}}))
            snapshot = hotels.import_har(path, 0)
            self.assertNotIn(captured_cookie, json.dumps(snapshot))
            self.assertNotIn(captured_cookie, json.dumps(hotels.inspect_har(path)))
            self.assertEqual(snapshot["rates"][0]["availability_status"], "quoted_in_capture")
            self.assertEqual(snapshot["rates"][0]["quote_checked_at"], entry["startedDateTime"])
            snapshot["rates"][0]["name"] = "=UNTRUSTED()"
            rows = list(csv.DictReader(io.StringIO(hotels.csv_export(snapshot))))
            self.assertEqual(rows[0]["record_type"], "coverage")
            self.assertTrue(rows[1]["name"].startswith("'="))
            self.assertEqual(json.loads(rows[1]["rate_blocks"])[0]["policy_group_id"], "201")
            saved = Path(tmp) / "saved.json"
            saved.write_text(json.dumps(snapshot))
            with redirect_stdout(io.StringIO()) as out:
                code = hotels.main(["filter", "--input", str(saved), "--max-total-price", "1"])
            filtered = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(filtered["status"], "filtered_out")
            self.assertEqual(len(filtered["raw_rates"]), 1)
            self.assertEqual(filtered["rates"], [])
            saved.write_text(json.dumps(filtered))
            with redirect_stdout(io.StringIO()) as out:
                hotels.main(["filter", "--input", str(saved), "--clear-filters", "--format", "csv"])
            replayed = list(csv.DictReader(io.StringIO(out.getvalue())))
            self.assertEqual(replayed[0]["check_in"], "2027-01-15")
            self.assertEqual(replayed[0]["requested_rooms"], "1")
            self.assertEqual(len(replayed), 2)


if __name__ == "__main__":
    unittest.main()
