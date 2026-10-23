"""Deterministic Booking.com search quotes, HAR inspection, and saved-result exports.

Uses the website's read-only FullSearch operation, not the partner Demand API.
No browser, credentials, captured cookies, or third-party packages are required.
"""
import argparse
import copy
import csv
import io
import json
import math
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


ENDPOINT = "https://www.booking.com/dml/graphql"
# Public persisted operation identifier observed in the October 22, 2026 HAR.
SEARCH_HASH = "695b137a206f0390666b6a0956a13b30bdd571b2945e7666ef214101ba3b17ee"
PROPERTY_TYPES = {"hotel": 204, "resort": 206, "apartment": 201, "villa": 213,
                  "vacation-home": 220, "bed-and-breakfast": 208, "guesthouse": 216,
                  "hostel": 203, "condo-hotel": 219, "homestay": 222}
MEAL_PLANS = {"breakfast": 1, "all-inclusive": 4, "all-meals": 3}
MEAL_TEXT = {"breakfast": "breakfast included", "all-inclusive": "all-inclusive",
             "all-meals": "all meals included"}
DESTINATION_TYPES = ("CITY", "REGION", "DISTRICT", "LANDMARK", "COUNTRY")
DESTINATIONS = [
    {"name": "Maui, Hawaii", "destination_id": 3183, "destination_type": "REGION",
     "aliases": ["Maui", "Maui, HI", "Maui, Hawaii, United States"],
     "source_url": "https://www.booking.com/region/us/maui-hawaii.html"},
    {"name": "Cancun, Mexico", "destination_id": -1655011, "destination_type": "CITY", "aliases": ["Cancun", "Cancún", "Cancún, Mexico"]},
    {"name": "Cabo San Lucas, Mexico", "destination_id": -1698603, "destination_type": "CITY", "aliases": ["Cabo San Lucas"]},
    {"name": "San Juan, Puerto Rico", "destination_id": 20154335, "destination_type": "CITY", "aliases": ["San Juan", "San Juan, PR"]},
]
LIMITATIONS = [
    "Ranked by the displayed party/stay amount; this is not a confirmed final payable total.",
    "Reported excluded charges and offered room counts are separate; rate terms may be incomplete.",
    "Results cover only the fetched Booking.com pages and are not exhaustive market inventory.",
    "The website operation is undocumented and can change or deny access.",
]


class SourceError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def money(value):
    """Accept finite, nonnegative provider numbers without parsing localized copy."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def rounded(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if value is not None else None


def amount(container, currency):
    container = container or {}
    return money(container.get("amountUnformatted")) if container.get("currency") == currency else None


def split_values(value):
    return [v.strip() for v in value.split(",") if v.strip()]


def location_key(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)).split())


def bind_destination(query):
    """Known aliases pin geography; an explicit caller-supplied ID takes precedence."""
    if query.get("destination_id") is None:
        for destination in DESTINATIONS:
            if location_key(query["location"]) in {location_key(n) for n in [destination["name"], *destination["aliases"]]}:
                query.update({k: destination[k] for k in ("destination_id", "destination_type")})
                query["destination_binding"] = "verified_alias"
                break
    return query


def validate_query(query):
    if not isinstance(query.get("location"), str) or not query["location"].strip() or len(query["location"]) > 200:
        raise ValueError("location must be a name of at most 200 characters")
    for field in ("check_in", "check_out"):
        if not isinstance(query.get(field), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", query[field]):
            raise ValueError(f"{field} must use YYYY-MM-DD")
    nights = (date.fromisoformat(query["check_out"]) - date.fromisoformat(query["check_in"])).days
    if not 1 <= nights <= 90:
        raise ValueError("stay must be between 1 and 90 nights")
    for field in ("adults", "rooms"):
        if type(query.get(field)) is not int or not 1 <= query[field] <= 30:
            raise ValueError(f"{field} must be a whole number between 1 and 30")
    ages = query.get("children_ages", [])
    if not isinstance(ages, list) or len(ages) > 10 or any(type(a) is not int or not 0 <= a <= 17 for a in ages):
        raise ValueError("children ages must be whole numbers from 0 to 17 (at most 10 children)")
    if not isinstance(query.get("currency"), str) or not re.fullmatch(r"[A-Z]{3}", query["currency"]):
        raise ValueError("currency must be a three-letter uppercase code")
    if query.get("destination_id") is not None and (type(query["destination_id"]) is not int or query["destination_id"] == 0):
        raise ValueError("destination ID must be a nonzero integer")
    if query.get("destination_type", "CITY") not in DESTINATION_TYPES:
        raise ValueError("unsupported destination type")
    query["nights"] = nights
    return query


def validate_filters(filters):
    types = filters.get("property_types")
    if types is not None and (not types or any(t not in PROPERTY_TYPES for t in types)):
        raise ValueError("choose property types from: " + ", ".join(PROPERTY_TYPES) + ", or all")
    stars = filters.get("stars")
    if stars is not None and (not stars or any(type(n) is not int or not 1 <= n <= 5 for n in stars)):
        raise ValueError("stars must be comma-separated whole numbers from 1 to 5")
    if filters.get("meal_plan") not in (None, *MEAL_PLANS):
        raise ValueError("unsupported meal plan")
    for key in ("max_total_price", "max_nightly_price", "min_review_score"):
        value = filters.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise ValueError(f"{key} must be a finite nonnegative number")
    if (filters.get("min_review_score") or 0) > 10:
        raise ValueError("minimum review score must be between 0 and 10")
    return filters


def selected_filters(filters):
    parts = [f"ht_id={PROPERTY_TYPES[t]}" for t in filters.get("property_types") or []]
    parts += [f"class={n}" for n in filters.get("stars") or []]
    if filters.get("meal_plan"):
        parts.append(f"mealplan={MEAL_PLANS[filters['meal_plan']]}")
    return ";".join(parts)


def search_payload(query, filters, offset, page_size):
    location = {"searchString": query["location"]}
    if query.get("destination_id") is not None:
        location.update(destId=query["destination_id"], destType=query.get("destination_type", "CITY"))
    return {"operationName": "FullSearch", "extensions": {"persistedQuery": {"version": 1, "sha256Hash": SEARCH_HASH}},
            "variables": {"includeBundle": False, "carouselLowCodeExp": False, "input": {
                "dates": {"checkin": query["check_in"], "checkout": query["check_out"]},
                "location": location, "nbAdults": query["adults"], "nbRooms": query["rooms"],
                "nbChildren": len(query["children_ages"]), "childrenAges": query["children_ages"],
                "filters": {"selectedFilters": selected_filters(filters)},
                "pagination": {"offset": offset, "rowsPerPage": page_size},
                "sorters": {"selectedSorter": "price"}, "useSearchParamsFromSession": False}}}


def fetch_page(payload, currency, timeout):
    """One ordinary HTTP request. Denials are returned without retry or escalation."""
    request = Request(ENDPOINT + "?" + urlencode({"lang": "en-us", "selected_currency": currency}),
                      data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "User-Agent": "TravelAgentDealTracker/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            # Bound memory use if the provider changes from JSON to an unexpected document.
            body = response.read(10_000_001)
            if status in (202, 401, 403):
                raise SourceError("blocked", "Booking.com requires verification or denied access.")
            if status != 200 or len(body) > 10_000_000:
                raise SourceError("unavailable", "Unexpected HTTP response or oversized search page.")
            try:
                data = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                lower = body.lower()
                challenge = any(t in lower for t in (b"challenge", b"robot", b"javascript is disabled"))
                raise SourceError("blocked" if challenge else "schema_changed", "Booking.com did not return search JSON.") from None
    except HTTPError as error:
        state = "blocked" if error.code in (401, 403) else "rate_limited" if error.code == 429 else "unavailable"
        retry_after = error.headers.get("Retry-After") if error.code == 429 else None
        message = f"Booking.com returned HTTP {error.code}."
        error.close()
        raise SourceError(state, message, retry_after) from None
    except (URLError, TimeoutError, OSError):
        raise SourceError("unavailable", "Booking.com request failed or timed out.") from None
    return decode_search(data)


def decode_search(data):
    if not isinstance(data, dict):
        raise SourceError("schema_changed", "Search response is not an object.")
    if data.get("errors"):
        changed = "PersistedQuery" in json.dumps(data["errors"])
        raise SourceError("schema_changed" if changed else "unavailable", "Booking.com returned a GraphQL search error.")
    try:
        search = data["data"]["searchQueries"]["search"]
        if not isinstance(search["results"], list) or not isinstance(search["searchMeta"], dict):
            raise TypeError
        return search
    except (KeyError, TypeError):
        raise SourceError("schema_changed", "Search response is missing results or search metadata.") from None


def verify_scope(search, query):
    meta = search["searchMeta"]
    dates = meta.get("dates") or {}
    ages = meta.get("childrenAges")
    if not isinstance(dates, dict) or not isinstance(ages, list) or any(type(a) is not int for a in ages):
        raise SourceError("scope_mismatch", "Provider date/child metadata is missing or malformed.")
    expected = {"nbAdults": query["adults"], "nbRooms": query["rooms"],
                "nbChildren": len(query["children_ages"])}
    if (any(meta.get(k) != v for k, v in expected.items()) or
            dates.get("checkin") != query["check_in"] or dates.get("checkout") != query["check_out"] or
            dates.get("lengthOfStayInDays") != query["nights"] or
            sorted(ages) != sorted(query["children_ages"])):
        raise SourceError("scope_mismatch", "Provider dates or guest/room settings differ from the requested search.")
    if query.get("destination_id") is not None and (meta.get("destId") != query["destination_id"] or meta.get("destType") != query.get("destination_type", "CITY")):
        raise SourceError("scope_mismatch", "Provider destination differs from the requested destination ID/type.")
    if type(meta.get("destId")) is not int or not isinstance(meta.get("destType"), str):
        raise SourceError("scope_mismatch", "Provider did not resolve a destination.")
    if meta["destType"] not in DESTINATION_TYPES:
        raise SourceError("scope_mismatch", "Location resolved to a property or unsupported area; choose a verified destination ID/type.")
    return {"destination_id": meta["destId"], "destination_type": meta["destType"]}


def property_url(basic, query):
    location = basic.get("location") or {}
    country, page = location.get("countryCode", ""), basic.get("pageName", "")
    if not re.fullmatch(r"[a-z]{2}", country) or not re.fullmatch(r"[a-zA-Z0-9_-]+", page):
        return "https://www.booking.com/searchresults.html?" + urlencode({"ss": query["location"],
            "checkin": query["check_in"], "checkout": query["check_out"], "group_adults": query["adults"],
            "no_rooms": query["rooms"], "group_children": len(query["children_ages"]), "age": query["children_ages"],
            "selected_currency": query["currency"]}, doseq=True)
    return f"https://www.booking.com/hotel/{country}/{page}.html?" + urlencode({
        "checkin": query["check_in"], "checkout": query["check_out"], "group_adults": query["adults"],
        "no_rooms": query["rooms"], "group_children": len(query["children_ages"]), "age": query["children_ages"],
        "selected_currency": query["currency"]}, doseq=True)


def parse_rate(result, query, checked_at, source):
    """Keep the complete party offer together, including repeated room blocks."""
    basic = result.get("basicPropertyData") or {}
    identity = {"property_id": str(basic.get("id", "")), "name": (result.get("displayName") or {}).get("text", "")}
    if not identity["property_id"] or not identity["name"]:
        return None, {**identity, "reason": "missing_property_identity"}
    if (result.get("soldOutInfo") or {}).get("isSoldOut"):
        return None, {**identity, "reason": "sold_out"}
    recommendation = result.get("recommendedDate") or {}
    if ((result.get("persuasion") or {}).get("autoextended") or result.get("nonMatchingFlexibleFilterOptions") or
            any(recommendation.get(k) and recommendation[k] != query[q] for k, q in (("checkin", "check_in"), ("checkout", "check_out")))):
        return None, {**identity, "reason": "suggested_alternative_outside_search"}
    price_info = result.get("priceDisplayInfoIrene") or {}
    price = amount((price_info.get("displayPrice") or {}).get("amountPerStay"), query["currency"])
    if price is None or price <= 0:
        return None, {**identity, "reason": "missing_price_or_currency_mismatch"}
    configs = result.get("matchingUnitConfigurations") or {}
    units = configs.get("unitConfigurations") or []
    offered_rooms = (configs.get("commonConfiguration") or {}).get("nbUnits")
    if type(offered_rooms) is not int or offered_rooms <= 0:
        offered_rooms = None
    blocks = []
    for block in result.get("blocks") or []:
        block_id = block.get("blockId") or {}
        blocks.append({"room_id": str(block_id.get("roomId", "")), "policy_group_id": str(block_id.get("policyGroupId", "")),
                       "meal_plan_id": block_id.get("mealPlanId"), "bundle_id": block_id.get("bundleId"),
                       "package_id": block_id.get("packageId"), "occupancy": block_id.get("occupancy"),
                       "price": rounded(money((block.get("finalPrice") or {}).get("amount"))),
                       "currency": (block.get("finalPrice") or {}).get("currency"),
                       "meal_plan": (block.get("mealPlanIncluded") or {}).get("text"),
                       "free_cancellation_until": block.get("freeCancellationUntil")})
    charges = []
    excluded = price_info.get("excludedCharges") or {}
    known_excluded = Decimal(0)
    charges_known = isinstance(excluded.get("excludeChargesList"), list)
    for charge in excluded.get("excludeChargesList") or []:
        charge_amount = amount(charge.get("amountPerStay"), query["currency"])
        inclusion = charge.get("chargeInclusion")
        charges.append({"type_id": charge.get("chargeType"), "mode": charge.get("chargeMode"),
                        "inclusion": inclusion, "amount_per_stay": rounded(charge_amount),
                        "currency": (charge.get("amountPerStay") or {}).get("currency")})
        if inclusion == "EXCLUDED" and charge_amount is not None:
            known_excluded += charge_amount
        elif inclusion != "INCLUDED":
            charges_known = False
    aggregate = amount((excluded.get("excludeChargesAggregated") or {}).get("amountPerStay"), query["currency"])
    discrepancy = aggregate is not None and charges_known and abs(aggregate - known_excluded) > Decimal("0.02")
    fees_status = "unknown" if not charges_known else "inconsistent_aggregate" if discrepancy else "reported_exclusions" if known_excluded else "no_reported_exclusions"
    if price_info.get("taxExceptions"):
        fees_status = "tax_exceptions"
    review = basic.get("reviewScore") or {}
    room_names = [u.get("name") for u in units if u.get("name")]
    return {**identity, "provider": "Booking.com", "property_type_id": basic.get("accommodationTypeId"),
            "property_type": next((k for k, v in PROPERTY_TYPES.items() if v == basic.get("accommodationTypeId")), "other"),
            "city": (basic.get("location") or {}).get("city"), "country_code": (basic.get("location") or {}).get("countryCode"),
            "search_location": query["location"], "stars": (basic.get("starRating") or {}).get("value"),
            "review_score": review.get("score") if review.get("showScore") else None, "review_count": review.get("reviewCount"),
            "check_in": query["check_in"], "check_out": query["check_out"], "nights": query["nights"],
            "adults": query["adults"], "children_ages": query["children_ages"], "requested_rooms": query["rooms"],
            "offered_rooms": offered_rooms, "room_match": "unknown" if offered_rooms is None else "exact" if offered_rooms == query["rooms"] else "different",
            "room_names": room_names, "rate_blocks": blocks, "currency": query["currency"],
            "displayed_total": rounded(price), "displayed_nightly_average": rounded(price / query["nights"]),
            "reported_charges": charges, "reported_charges_aggregate": rounded(aggregate),
            "known_excluded_charges_total": rounded(known_excluded) if charges_known else None,
            "displayed_plus_known_excluded": rounded(price + known_excluded) if charges_known else None,
            "fees_status": fees_status, "tax_exception_count": len(price_info.get("taxExceptions") or []),
            "taxes_and_fees_complete": False, "final_payable_total": None,
            "free_cancellation_advertised": (result.get("policies") or {}).get("showFreeCancellation"),
            "no_prepayment_advertised": (result.get("policies") or {}).get("showNoPrepayment"),
            "refund_terms": None, "meal_plan": (result.get("mealPlanIncluded") or {}).get("text"),
            "booking_url": property_url(basic, query), "availability_status": "quoted_in_capture" if source == "booking_com_har" else "search_quote",
            "price_source": source, "quote_checked_at": checked_at}, None


def rate_key(rate):
    # Repeated identical blocks represent multiple offered rooms, not duplicates.
    ids = sorted((b["room_id"], b["policy_group_id"], str(b["meal_plan_id"]), str(b["bundle_id"]), str(b["package_id"]), str(b["occupancy"])) for b in rate["rate_blocks"])
    return json.dumps([rate["property_id"], rate["check_in"], rate["check_out"], rate["currency"], rate["adults"], rate["children_ages"], rate["offered_rooms"], ids], sort_keys=True)


def sort_key(rate):
    return (rate["displayed_total"], rate["property_id"], rate_key(rate))


def filter_rates(rates, filters):
    result = []
    for rate in rates:
        if filters.get("property_types") is not None and rate["property_type"] not in filters["property_types"]:
            continue
        if filters.get("stars") is not None and rate.get("stars") not in filters["stars"]:
            continue
        if filters.get("exact_rooms") and rate.get("room_match") != "exact":
            continue
        if filters.get("free_cancellation") and rate.get("free_cancellation_advertised") is not True:
            continue
        if filters.get("meal_plan"):
            meals = [(b.get("meal_plan") or "").casefold() for b in rate["rate_blocks"]]
            allowed = {MEAL_TEXT[filters["meal_plan"]]}
            if filters["meal_plan"] == "breakfast":
                allowed.update((MEAL_TEXT["all-inclusive"], MEAL_TEXT["all-meals"]))
            if not meals or not all(m in allowed for m in meals):
                continue
        if filters.get("max_total_price") is not None and rate["displayed_total"] > filters["max_total_price"]:
            continue
        if filters.get("max_nightly_price") is not None and Decimal(str(rate["displayed_total"])) > Decimal(str(filters["max_nightly_price"])) * rate["nights"]:
            continue
        if filters.get("min_review_score") is not None and (rate.get("review_score") is None or rate["review_score"] < filters["min_review_score"]):
            continue
        result.append(rate)
    result.sort(key=sort_key)
    if filters.get("unique_properties"):
        unique = {}
        for rate in result:
            unique.setdefault(rate["property_id"], rate)
        result = list(unique.values())
    return result


def new_snapshot(query, filters, source):
    return {"schema_version": 1, "kind": "booking_hotel_search", "source": source, "checked_at": utc_now(),
            "query": copy.deepcopy(query), "live_filters": copy.deepcopy(filters), "applied_filters": copy.deepcopy(filters),
            "price_basis": "displayed_party_stay_total", "raw_rates": [], "rates": [], "excluded_properties": [],
            "pages": [], "source_errors": [], "coverage": {"status": "unavailable", "pages_fetched": 0, "rows_fetched": 0,
            "reported_total": None, "all_reported_rows_fetched": False}, "limitations": list(LIMITATIONS)}


def summarize(snapshot, max_results=None):
    rates = filter_rates(snapshot["raw_rates"], snapshot["applied_filters"])
    snapshot["matching_rate_count"] = len(rates)
    snapshot["rates"] = rates if max_results is None else rates[:max_results]
    snapshot["max_results"] = max_results
    snapshot["cheapest_displayed"] = rates[0] if rates else None
    coverage = snapshot["coverage"]
    if snapshot["source_errors"]:
        snapshot["status"] = "partial" if snapshot["raw_rates"] else snapshot["source_errors"][-1]["status"]
    elif not coverage["all_reported_rows_fetched"] or snapshot["excluded_properties"]:
        snapshot["status"] = "partial"
    elif rates:
        snapshot["status"] = "ok"
    else:
        snapshot["status"] = "filtered_out" if snapshot["raw_rates"] else "no_matching_rates"
    return snapshot


def add_page(snapshot, search, query, offset, checked_at, source):
    resolved = verify_scope(search, query)
    previous = snapshot.get("resolved_destination")
    if previous is not None and previous != resolved:
        raise SourceError("scope_mismatch", "Provider destination changed between pages.")
    pagination = search.get("pagination") or {}
    total = pagination.get("nbResultsTotal")
    if type(total) is not int or total < 0:
        raise SourceError("schema_changed", "Provider result count is missing or invalid.")
    snapshot["resolved_destination"] = resolved
    merged = {rate_key(r): r for r in snapshot["raw_rates"]}
    for result in search["results"]:
        try:
            rate, excluded = parse_rate(result, query, checked_at, source)
        except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation):
            rate, excluded = None, {"reason": "malformed_property"}
        if rate:
            key = rate_key(rate)
            if key not in merged or sort_key(rate) < sort_key(merged[key]):
                merged[key] = rate
        if excluded:
            snapshot["excluded_properties"].append({**excluded, "offset": offset})
    snapshot["raw_rates"] = sorted(merged.values(), key=sort_key)
    count = len(search["results"])
    snapshot["pages"].append({"offset": offset, "row_count": count, "reported_total": total, "checked_at": checked_at})
    coverage = snapshot["coverage"]
    coverage.update(pages_fetched=len(snapshot["pages"]), rows_fetched=coverage["rows_fetched"] + count,
                    reported_total=total, all_reported_rows_fetched=offset == 0 and count >= total if len(snapshot["pages"]) == 1 else offset + count >= total)
    coverage["status"] = "all_reported_rows_fetched" if coverage["all_reported_rows_fetched"] else "partial"
    return count, total


def search_hotels(query, filters, *, max_pages=3, page_size=20, timeout=20, max_results=None, progress=None):
    started = time.monotonic()
    validate_query(query)
    bind_destination(query)
    validate_filters(filters)
    snapshot = new_snapshot(query, filters, "booking_com_http")
    offset = 0
    fetch_seconds, normalization_seconds, requests = 0.0, 0.0, 0
    try:
        for page in range(max_pages):
            if progress:
                progress({"event": "page", "page": page + 1, "max_pages": max_pages, "offset": offset,
                          "elapsed_seconds": round(time.monotonic() - started, 2)})
            request_started = time.monotonic()
            requests += 1
            try:
                response = fetch_page(search_payload(query, filters, offset, page_size), query["currency"], timeout)
            finally:
                fetch_seconds += time.monotonic() - request_started
            normalize_started = time.monotonic()
            count, total = add_page(snapshot, response, query, offset, utc_now(), snapshot["source"])
            normalization_seconds += time.monotonic() - normalize_started
            if offset + count >= total:
                break
            if count == 0:
                raise SourceError("unavailable", "Provider returned an empty page before its reported end.")
            offset += count
    except SourceError as error:
        snapshot["source_errors"].append({"status": error.status, "message": str(error), "offset": offset, "retry_after": error.retry_after})
    except KeyboardInterrupt:
        snapshot["source_errors"].append({"status": "cancelled", "message": "Search cancelled; completed pages retained.", "offset": offset})
    ranking_started = time.monotonic()
    summarize(snapshot, max_results)
    snapshot["timings"] = {"total_seconds": round(time.monotonic() - started, 6),
                           "fetch_decode_seconds": round(fetch_seconds, 6), "normalization_seconds": round(normalization_seconds, 6),
                           "ranking_seconds": round(time.monotonic() - ranking_started, 6), "http_requests": requests}
    snapshot["elapsed_seconds"] = snapshot["timings"]["total_seconds"]
    return snapshot


def comparison_plan(locations, base_query, start_date, end_date, nights, step=1, max_searches=100):
    """Both check-in and checkout must fall within the inclusive calendar bounds."""
    if type(nights) is not int or not 1 <= nights <= 90 or type(step) is not int or step < 1:
        raise ValueError("nights must be 1–90 and date-step-days must be positive")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if (end - start).days < nights:
        raise ValueError("date window must contain the entire stay")
    if (end - start).days > 366:
        raise ValueError("comparison window must be at most 366 days")
    locations = list(dict.fromkeys(location.strip() for location in locations))
    pairs = []
    while start + timedelta(days=nights) <= end:
        pairs.append((start.isoformat(), (start + timedelta(days=nights)).isoformat()))
        start += timedelta(days=step)
    if not 1 <= max_searches <= 1000 or len(pairs) * len(locations) > max_searches:
        raise ValueError("comparison exceeds max-searches; narrow the window/locations or explicitly raise the bound (at most 1000)")
    queries = [bind_destination(validate_query({**base_query, "location": location, "check_in": check_in, "check_out": check_out}))
               for location in locations for check_in, check_out in pairs]
    return {"locations": locations, "start_date": start_date, "end_date": end_date, "nights": nights,
            "date_step_days": step, "date_coverage": "every_eligible_check_in" if step == 1 else "sampled_check_ins",
            "date_pairs": pairs, "planned_searches": len(queries), "queries": queries}


def summarize_comparison(snapshot, max_results=None):
    snapshot["raw_rates"] = [rate for search in snapshot["searches"] for rate in search["raw_rates"]]
    rates = filter_rates(snapshot["raw_rates"], snapshot["applied_filters"])
    snapshot["matching_rate_count"] = len(rates)
    snapshot["rates"] = rates if max_results is None else rates[:max_results]
    snapshot["max_results"] = max_results
    snapshot["cheapest_displayed"] = rates[0] if rates else None
    snapshot["matrix"] = []
    snapshot["source_errors"] = []
    by_location = {}
    for search in snapshot["searches"]:
        search["applied_filters"] = copy.deepcopy(snapshot["applied_filters"])
        summarize(search)
        q = search["query"]
        snapshot["matrix"].append({"location": q["location"], "check_in": q["check_in"], "check_out": q["check_out"],
                                   "status": search["status"], "coverage": search["coverage"],
                                   "cheapest_displayed": search["cheapest_displayed"]})
        snapshot["source_errors"].extend({"location": q["location"], "check_in": q["check_in"], **error} for error in search["source_errors"])
        if search["rates"]:
            current = by_location.get(q["location"])
            if current is None or sort_key(search["rates"][0]) < sort_key(current):
                by_location[q["location"]] = search["rates"][0]
    snapshot["cheapest_by_location"] = by_location
    complete = sum(s["coverage"]["all_reported_rows_fetched"] and not s["source_errors"] and not s["excluded_properties"] for s in snapshot["searches"])
    planned, completed = snapshot["plan"]["planned_searches"], len(snapshot["searches"])
    full = complete == planned and not snapshot.get("cancelled")
    snapshot["coverage"] = {"status": "complete_provider_searches" if full else "partial", "planned_searches": planned,
                            "completed_searches": completed, "complete_provider_searches": complete,
                            "date_coverage": snapshot["plan"]["date_coverage"]}
    snapshot["status"] = "cancelled" if snapshot.get("cancelled") else "partial" if not full else "ok" if rates else "filtered_out" if snapshot["raw_rates"] else "no_matching_rates"
    return snapshot


def compare_hotels(plan, filters, *, max_pages=3, page_size=20, timeout=20, max_results=None, progress=None):
    started = time.monotonic()
    snapshot = {"schema_version": 1, "kind": "booking_hotel_comparison", "source": "booking_com_http", "checked_at": utc_now(),
                "plan": plan, "live_filters": copy.deepcopy(filters), "applied_filters": copy.deepcopy(filters),
                "searches": [], "price_basis": "displayed_party_stay_total", "limitations": list(LIMITATIONS)}
    pinned = {}
    try:
        for index, original in enumerate(plan["queries"]):
            query = copy.deepcopy(original)
            if query["location"] in pinned and query.get("destination_id") is None:
                query.update(pinned[query["location"]])
            def report(event):
                if progress:
                    progress({**event, "search": index + 1, "total_searches": plan["planned_searches"],
                              "location": query["location"], "check_in": query["check_in"]})
            search = search_hotels(query, filters, max_pages=max_pages, page_size=page_size, timeout=timeout, progress=report)
            snapshot["searches"].append(search)
            if search.get("resolved_destination"):
                pinned[query["location"]] = search["resolved_destination"]
            if any(e["status"] == "cancelled" for e in search["source_errors"]):
                snapshot["cancelled"] = True
                break
            if any(e["status"] in ("blocked", "rate_limited", "schema_changed") for e in search["source_errors"]):
                snapshot["stopped_after_source_failure"] = True
                break
    except KeyboardInterrupt:
        snapshot["cancelled"] = True
    summarize_comparison(snapshot, max_results)
    snapshot["timings"] = {"total_seconds": round(time.monotonic() - started, 6),
                           "fetch_decode_seconds": round(sum(s.get("timings", {}).get("fetch_decode_seconds", 0) for s in snapshot["searches"]), 6),
                           "http_requests": sum(s.get("timings", {}).get("http_requests", 0) for s in snapshot["searches"])}
    snapshot["elapsed_seconds"] = snapshot["timings"]["total_seconds"]
    return snapshot


def har_searches(path):
    """Yield only read-only FullSearch records; never export headers or opaque state."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))["log"]["entries"]
    for index, entry in enumerate(entries):
        request = entry.get("request", {})
        url = urlsplit(request.get("url", ""))
        if url.hostname != "www.booking.com" or url.path != "/dml/graphql":
            continue
        try:
            payload = json.loads(request.get("postData", {}).get("text", "{}"))
            if payload.get("operationName") != "FullSearch":
                continue
            inp = payload["variables"]["input"]
            yield index, entry, inp
        except (KeyError, TypeError, ValueError):
            continue


def inspect_har(path):
    searches = []
    for index, entry, inp in har_searches(path):
        try:
            search = decode_search(json.loads(entry["response"]["content"]["text"]))
            count = len(search["results"])
        except (KeyError, ValueError, SourceError):
            count = None
        searches.append({"entry": index, "captured_at": entry.get("startedDateTime"),
                         "location": (inp.get("location") or {}).get("searchString"), "dates": inp.get("dates"),
                         "adults": inp.get("nbAdults"), "rooms": inp.get("nbRooms"), "children": inp.get("nbChildren"),
                         "filters": (inp.get("filters") or {}).get("selectedFilters", ""),
                         "pagination": inp.get("pagination"), "http_status": entry["response"]["status"], "result_count": count})
    return {"searches": searches, "property_types": PROPERTY_TYPES, "meal_plans": MEAL_PLANS}


def import_har(path, entry_index):
    for index, entry, inp in har_searches(path):
        if index != entry_index:
            continue
        if entry["response"].get("status") != 200:
            raise ValueError("HAR entry is not a successful HTTP 200 search response")
        search = decode_search(json.loads(entry["response"]["content"]["text"]))
        currencies = {((r.get("priceDisplayInfoIrene") or {}).get("displayPrice") or {}).get("amountPerStay", {}).get("currency") for r in search["results"]}
        currencies.discard(None)
        if len(currencies) != 1:
            raise ValueError("HAR entry must contain quotes in one identifiable currency")
        location = inp["location"]
        query = validate_query({"location": location["searchString"], "destination_id": location.get("destId"),
                                "destination_type": location.get("destType", "CITY"), "check_in": inp["dates"]["checkin"],
                                "check_out": inp["dates"]["checkout"], "adults": inp["nbAdults"], "rooms": inp["nbRooms"],
                                "children_ages": inp.get("childrenAges", []), "currency": next(iter(currencies))})
        # Retain source filters as evidence; selecting another entry is an explicit action.
        filters = {"property_types": None}
        snapshot = new_snapshot(query, filters, "booking_com_har")
        snapshot["captured_filters"] = (inp.get("filters") or {}).get("selectedFilters", "")
        snapshot["har_entry"] = index
        add_page(snapshot, search, query, inp["pagination"]["offset"], entry["startedDateTime"], snapshot["source"])
        snapshot["limitations"].insert(0, "Historical HAR quotes; importing or filtering does not recheck availability.")
        return summarize(snapshot)
    raise ValueError("entry is not a Booking.com FullSearch record; use inspect-har to choose an entry")


def csv_export(snapshot):
    if snapshot["kind"] == "booking_hotel_comparison":
        stream = io.StringIO(newline="")
        rows, fields = [], ["record_type", "status", "source_errors"]
        selected = {(rate_key(r), r.get("search_location")) for r in snapshot["rates"]}
        for original in snapshot["searches"]:
            search = copy.deepcopy(original)
            search["rates"] = [r for r in search["rates"] if (rate_key(r), r.get("search_location")) in selected]
            reader = csv.DictReader(io.StringIO(csv_export(search)))
            fields = reader.fieldnames
            rows.extend(reader)
        writer = csv.DictWriter(stream, fieldnames=[*fields, "planned_searches", "completed_searches", "date_coverage"])
        writer.writeheader()
        writer.writerow({"record_type": "comparison_coverage", "status": snapshot["status"],
                         "source_errors": json.dumps(snapshot["source_errors"]),
                         **{k: snapshot["coverage"][k] for k in ("planned_searches", "completed_searches", "date_coverage")}})
        writer.writerows(rows)
        return stream.getvalue()
    stream = io.StringIO(newline="")
    fields = ["record_type", "status", "coverage_status", "reported_total", "rows_fetched", "location", "destination_id", "destination_type",
              "price_basis", "source", "checked_at", "property_id", "name", "property_type",
              "city", "check_in", "check_out", "nights", "adults", "children_ages", "requested_rooms", "offered_rooms", "room_match",
              "room_names", "rate_blocks", "stars", "review_score", "currency", "displayed_total", "displayed_nightly_average",
              "known_excluded_charges_total", "displayed_plus_known_excluded", "reported_charges_aggregate", "reported_charges",
              "fees_status", "tax_exception_count", "taxes_and_fees_complete", "final_payable_total", "free_cancellation_advertised", "no_prepayment_advertised", "refund_terms",
              "meal_plan", "availability_status", "price_source", "quote_checked_at", "booking_url", "source_errors"]
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    def safe_row(row):
        result = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
                value = "'" + value
            result[key] = value
        return result
    common = {"status": snapshot["status"], "coverage_status": snapshot["coverage"]["status"],
              "reported_total": snapshot["coverage"]["reported_total"], "rows_fetched": snapshot["coverage"]["rows_fetched"],
              **{k: snapshot["query"][k] for k in ("location", "check_in", "check_out", "nights", "adults", "children_ages", "currency")},
              **snapshot.get("resolved_destination", {}), "requested_rooms": snapshot["query"]["rooms"],
              "price_basis": snapshot["price_basis"], "source": snapshot["source"], "checked_at": snapshot["checked_at"]}
    writer.writerow(safe_row({**common, "record_type": "coverage", "source_errors": snapshot["source_errors"]}))
    for rate in snapshot["rates"]:
        writer.writerow(safe_row({**common, **rate, "record_type": "rate"}))
    return stream.getvalue()


def compact_rate(rate):
    if rate is None:
        return None
    fields = ("property_id", "name", "search_location", "city", "stars", "review_score", "check_in", "check_out", "nights",
              "currency", "displayed_total", "displayed_nightly_average", "requested_rooms", "offered_rooms", "room_match",
              "room_names", "known_excluded_charges_total", "displayed_plus_known_excluded", "fees_status", "taxes_and_fees_complete",
              "final_payable_total", "meal_plan", "free_cancellation_advertised", "refund_terms", "availability_status", "price_source", "quote_checked_at", "booking_url")
    result = {key: rate.get(key) for key in fields}
    result["cancellation_deadlines"] = sorted({b["free_cancellation_until"] for b in rate.get("rate_blocks", []) if b.get("free_cancellation_until")})
    result["rate_block_count"] = len(rate.get("rate_blocks", []))
    result["block_meal_plans"] = sorted({b["meal_plan"] for b in rate.get("rate_blocks", []) if b.get("meal_plan")})
    return result


def compact_summary(snapshot):
    """Answer-sized view; full quotes remain in a separately saved replayable snapshot."""
    searches = snapshot.get("searches", []) if snapshot["kind"] == "booking_hotel_comparison" else [snapshot]
    show = min(snapshot.get("max_results") or 5, 20)
    rates = snapshot["rates"][:show]
    summary = {"kind": "booking_hotel_summary", "snapshot_kind": snapshot["kind"], "status": snapshot["status"],
               "checked_at": snapshot["checked_at"], "price_basis": snapshot["price_basis"],
               "applied_filters": snapshot["applied_filters"], "coverage": snapshot["coverage"],
               "counts": {"raw_offers": len(snapshot["raw_rates"]), "matching": snapshot["matching_rate_count"],
                          "matching_unit": "properties" if snapshot["applied_filters"].get("unique_properties") else "rate_offers",
                          "selected": len(snapshot["rates"]), "shown": len(rates), "omitted": len(snapshot["rates"]) - len(rates)},
               "rates": [compact_rate(rate) for rate in rates], "source_errors": snapshot["source_errors"],
               "quote_timings": snapshot.get("timings"), "limitations": snapshot["limitations"], "replayable": False}
    if snapshot["kind"] == "booking_hotel_search":
        summary["query"] = snapshot["query"]
        summary["resolved_destination"] = snapshot.get("resolved_destination")
    else:
        summary["plan"] = {key: value for key, value in snapshot["plan"].items() if key not in ("queries", "date_pairs")}
        summary["party"] = {key: snapshot["plan"]["queries"][0][key] for key in ("adults", "rooms", "children_ages", "currency")}
        summary["cheapest_by_location"] = {location: compact_rate(rate) for location, rate in snapshot["cheapest_by_location"].items()}
        summary["searches"] = [{"location": s["query"]["location"], "check_in": s["query"]["check_in"], "check_out": s["query"]["check_out"],
                                "status": s["status"], "resolved_destination": s.get("resolved_destination"), "coverage": s["coverage"],
                                "excluded_count": len(s["excluded_properties"]), "source_error_count": len(s["source_errors"]),
                                "cheapest_displayed_total": s["cheapest_displayed"]["displayed_total"] if s["cheapest_displayed"] else None}
                               for s in searches]
    summary["excluded_reasons"] = dict(Counter(p["reason"] for s in searches for p in s["excluded_properties"]))
    return summary


def compact_plan(plan):
    return {**{key: value for key, value in plan.items() if key not in ("queries", "date_pairs")},
            "first_stay": plan["date_pairs"][0], "last_stay": plan["date_pairs"][-1],
            "destinations": [{key: q.get(key) for key in ("location", "destination_id", "destination_type")}
                             for q in plan["queries"][::len(plan["date_pairs"])]],
            "party": {key: plan["queries"][0][key] for key in ("adults", "rooms", "children_ages", "currency")}}


def render(snapshot, fmt):
    if fmt == "json":
        return json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if fmt == "csv":
        return csv_export(snapshot)
    if fmt == "summary":
        return json.dumps(compact_summary(snapshot), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    coverage = snapshot["coverage"]
    description = (f"{coverage['completed_searches']}/{coverage['planned_searches']} searches | {coverage['date_coverage']}"
                   if snapshot["kind"] == "booking_hotel_comparison" else f"{coverage['rows_fetched']} rows fetched / {coverage['reported_total']} reported")
    lines = [f"{snapshot['status']} | {description} | {snapshot['matching_rate_count']} matching quotes",
             "Prices are displayed party/stay amounts; final fees and refund terms require verification.",
             "Location\tCheck-in\tCheckout\tTotal\tAvg/night\tCurrency\tRooms offered/requested\tProperty\tCharges"]
    lines.extend(f"{r.get('search_location', r.get('city', ''))}\t{r['check_in']}\t{r['check_out']}\t{r['displayed_total']:.2f}\t{r['displayed_nightly_average']:.2f}\t{r['currency']}\t{r['offered_rooms']}/{r['requested_rooms']}\t{r['name']}\t{r['fees_status']}" for r in snapshot["rates"])
    lines.extend(f"{e['status']}: {e['message']}" for e in snapshot["source_errors"])
    return "\n".join(lines) + "\n"


def add_filters(parser):
    parser.add_argument("--property-types", help="Comma-separated: " + ",".join(PROPERTY_TYPES) + "; defaults to hotel,resort for search; all clears this filter")
    parser.add_argument("--meal-plan", choices=["any", *MEAL_PLANS])
    parser.add_argument("--stars", help="Comma-separated ratings, e.g. 4,5; all clears this filter")
    parser.add_argument("--min-review-score", type=float)
    parser.add_argument("--max-total-price", type=float, help="Ceiling on the displayed party/stay total, excluding some charges")
    parser.add_argument("--max-nightly-price", type=float, help="Ceiling on displayed total divided by stay nights")
    parser.add_argument("--exact-rooms", action=argparse.BooleanOptionalAction, default=None, help="Require offered room count to equal requested count")
    parser.add_argument("--free-cancellation", action=argparse.BooleanOptionalAction, default=None, help="Require the provider's free-cancellation badge")
    parser.add_argument("--unique-properties", action=argparse.BooleanOptionalAction, default=None, help="Keep each property's cheapest matching offer before the result limit")


def filters_from_args(args, previous=None):
    filters = copy.deepcopy(previous) if previous is not None else {"property_types": ["hotel", "resort"]}
    for key in ("meal_plan", "min_review_score", "max_total_price", "max_nightly_price", "exact_rooms", "free_cancellation", "unique_properties"):
        value = getattr(args, key)
        if value is not None:
            filters[key] = None if value == "any" else value
    if args.property_types is not None:
        filters["property_types"] = None if args.property_types == "all" else split_values(args.property_types)
    if args.stars is not None:
        filters["stars"] = None if args.stars == "all" else [int(n) for n in split_values(args.stars)]
    return validate_filters(filters)


def main(argv=None):
    command_started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Retrieve fresh Booking.com search quotes without a browser")
    search.add_argument("--location", required=True, help="City or location name; not an airport code")
    search.add_argument("--destination-id", type=int, help="Optional explicit Booking.com destination ID")
    search.add_argument("--destination-type", choices=DESTINATION_TYPES, default="CITY")
    search.add_argument("--check-in", required=True)
    search.add_argument("--check-out", required=True)
    compare = commands.add_parser("compare", help="Compare locations and bounded date windows deterministically")
    compare.add_argument("--location", action="append", required=True, help="Repeat for multiple locations; use destinations to inspect verified aliases")
    compare.add_argument("--start-date", required=True, help="Earliest check-in, YYYY-MM-DD")
    compare.add_argument("--end-date", required=True, help="Latest checkout, YYYY-MM-DD, inclusive")
    compare.add_argument("--nights", type=int, required=True)
    compare.add_argument("--date-step-days", type=int, default=1)
    compare.add_argument("--max-searches", type=int, default=100)
    compare.add_argument("--dry-run", action="store_true", help="Export exact query plan as JSON without network access")
    commands.add_parser("destinations", help="List verified Booking.com names, aliases and ID/type bindings")
    for command in (search, compare):
        command.add_argument("--adults", type=int, default=2)
        command.add_argument("--rooms", type=int, default=1)
        command.add_argument("--children-ages", default="", help="Comma-separated ages; children are separate from adults")
        command.add_argument("--currency", default="USD")
        command.add_argument("--max-pages", type=int, default=3, help="At most 20 pages per query; unfetched inventory remains partial")
        command.add_argument("--page-size", type=int, default=20)
        command.add_argument("--timeout", type=float, default=20, help="HTTP socket timeout in seconds, at most 60")
        command.add_argument("--progress-json", action="store_true", help="Write page/search progress to stderr")
    saved = commands.add_parser("filter", help="Filter/export retained quotes without network access")
    saved.add_argument("--input", required=True, help="A JSON snapshot produced by this CLI")
    saved.add_argument("--clear-filters", action="store_true", help="Clear local filters before applying the supplied ones; retained inventory stays fixed")
    saved.add_argument("--all-results", action="store_true", help="Clear the saved result limit while retaining other filters")
    inspect = commands.add_parser("inspect-har", help="List captured search scopes, filters and entry indexes")
    inspect.add_argument("--input", required=True)
    imported = commands.add_parser("import-har", help="Import one explicit historical FullSearch response")
    imported.add_argument("--input", required=True)
    imported.add_argument("--entry", required=True, type=int)
    for command in (search, compare, saved):
        add_filters(command)
        command.add_argument("--max-results", type=int, help="Limit exported rates; JSON retains every fetched raw candidate")
    for command in (search, compare, saved, imported):
        command.add_argument("--format", choices=["json", "summary", "csv", "table"], default="json")
        command.add_argument("--output", help="Save the selected format here instead of stdout")
        command.add_argument("--snapshot", help="Save full replayable JSON separately; combine with --format summary for compact stdout")
    args = parser.parse_args(argv)
    try:
        if args.command == "destinations":
            print(json.dumps(DESTINATIONS, ensure_ascii=False, indent=2))
            return 0
        if args.command == "inspect-har":
            print(json.dumps(inspect_har(args.input), indent=2))
            return 0
        if getattr(args, "max_results", None) is not None and args.max_results < 1:
            raise ValueError("max-results must be positive; omit it to keep all matching quotes")
        if getattr(args, "all_results", False) and args.max_results is not None:
            raise ValueError("choose all-results or max-results, not both")
        if getattr(args, "output", None):
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        if args.snapshot:
            Path(args.snapshot).parent.mkdir(parents=True, exist_ok=True)
            if args.output and Path(args.snapshot).resolve() == Path(args.output).resolve():
                raise ValueError("snapshot and output must be different paths")
        if args.command in ("search", "compare"):
            if not 1 <= args.max_pages <= 20 or not 1 <= args.page_size <= 50:
                raise ValueError("max-pages must be 1–20 and page-size 1–50")
            if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
                raise ValueError("timeout must be finite and between 0 and 60 seconds")
            if args.command == "search" and args.destination_type != "CITY" and args.destination_id is None:
                raise ValueError("destination-type requires an explicit destination-id")
            base = {"adults": args.adults, "rooms": args.rooms, "children_ages": [int(n) for n in split_values(args.children_ages)], "currency": args.currency}
            filters = filters_from_args(args)
            progress = (lambda event: print(json.dumps(event), file=sys.stderr, flush=True)) if args.progress_json else None
            options = {"max_pages": args.max_pages, "page_size": args.page_size, "timeout": args.timeout, "max_results": args.max_results, "progress": progress}
            if args.command == "search":
                query = {**base, "location": args.location.strip(), "destination_id": args.destination_id, "destination_type": args.destination_type,
                         "check_in": args.check_in, "check_out": args.check_out}
                snapshot = search_hotels(query, filters, **options)
            else:
                plan = comparison_plan(args.location, base, args.start_date, args.end_date, args.nights, args.date_step_days, args.max_searches)
                if args.dry_run:
                    full_plan = {**plan, "live_filters": filters, "max_pages_per_search": args.max_pages}
                    if args.snapshot:
                        Path(args.snapshot).write_text(json.dumps(full_plan, indent=2) + "\n", encoding="utf-8")
                    output = json.dumps(compact_plan(full_plan) if args.format == "summary" else full_plan, indent=2) + "\n"
                    if args.output:
                        Path(args.output).write_text(output, encoding="utf-8")
                    else:
                        print(output, end="")
                    return 0
                snapshot = compare_hotels(plan, filters, **options)
        elif args.command == "import-har":
            snapshot = import_har(args.input, args.entry)
        else:
            snapshot = json.loads(Path(args.input).read_text(encoding="utf-8"))
            if snapshot.get("schema_version") != 1 or snapshot.get("kind") not in ("booking_hotel_search", "booking_hotel_comparison"):
                raise ValueError("input must be a hotel_tools JSON search snapshot")
            snapshot["applied_filters"] = filters_from_args(args, {"property_types": None} if args.clear_filters else snapshot["applied_filters"])
            limit = args.max_results if args.max_results is not None else None if args.clear_filters or args.all_results else snapshot.get("max_results")
            (summarize_comparison if snapshot["kind"] == "booking_hotel_comparison" else summarize)(snapshot, limit)
        if args.snapshot:
            Path(args.snapshot).write_text(render(snapshot, "json"), encoding="utf-8")
        if args.format == "summary":
            view = compact_summary(snapshot)
            view["snapshot_path"] = str(Path(args.snapshot).resolve()) if args.snapshot else None
            view["execution"] = {"command": args.command, "elapsed_seconds": round(time.monotonic() - command_started, 6),
                                 "http_requests": snapshot.get("timings", {}).get("http_requests", 0) if args.command in ("search", "compare") else 0}
            output = json.dumps(view, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        else:
            output = render(snapshot, args.format)
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        if snapshot.get("cancelled") or any(e["status"] == "cancelled" for e in snapshot["source_errors"]):
            return 130
        return 1 if snapshot["source_errors"] else 0
    except (ValueError, OSError, KeyError, TypeError, SourceError) as error:
        parser.error(str(error) if isinstance(error, (ValueError, SourceError)) else "Input/output file is unreadable or has an invalid schema")


if __name__ == "__main__":
    raise SystemExit(main())
