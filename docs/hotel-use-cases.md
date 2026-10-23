# Ten real-world hotel-search evaluations

Three independent subagents exercised these requests using the deal-tracker skill and live Booking.com searches on October 22, 2026. They inspected scope, filters, numeric ordering, room/rate identity, fees, coverage and exports. They then retested repaired workflows. Normalized artifacts and reports are ignored local files under `tmp/hotels/use-cases/`.

The subsequent [timing and skill-efficiency evaluation](hotel-skill-evaluation.md) records per-case live durations, compact-output measurements, independent instruction scores and model-reported token counters, including metrics that did not improve.

The harness fixes “week of X” to **January 11–17, 2027**. Five calendar days means **four nights**; five nights is tested separately. A group of four means four adults unless children are specified. One requested room is the default; offered room counts remain visible and exact-room filters apply only when required. Prices below are observed displayed party/stay quotes, with known excluded charges separate, not final payable prices.

| # | Realistic user request | Independent live outcome after fixes |
|---|---|---|
| 1 | Four-star-or-better Cancún hotels/resorts for four people for five days during the week of Jan 11 | All three eligible starts, Jan 11/12/13, checked; 53 distinct qualifying properties from 150 fetched rows. Property coverage remains partial on each date. |
| 2 | Cheapest ten distinct five-star Maui hotels/resorts for four people, Jan 11–15 | Maui REGION 3183 verified; ten distinct properties returned, with all ten reported rows fetched. Two offer two rooms; exact-one-room replay correctly returns eight. |
| 3 | Cheapest hotel in Maui and San Juan for four people, Jan 11–15 | Both queries completed with separate minima and coverage. Maui fetched 24/24 reported rows; San Juan fetched 50/93. Actual property cities remain visible. |
| 4 | Cheapest five-star Maui hotel/resort for five days anywhere fully within January | All 27 eligible four-night stays checked, Jan 1–27 check-ins and Jan 5–31 checkouts. Every provider row fetched for every date, zero exclusions/errors. 253 offers reduced to ten distinct property minima. |
| 5 | Cancún four-star-or-better family stay, two adults plus children aged 7/12, breakfast, one room, Jan 11–15, at most $300/night | 22 matching exact-one-room offers from 50/87 rows. Cheapest observed Hotel Kavia: $353.07 stay/$88.27 nightly, plus $74.14 known excluded charges. |
| 6 | Cabo all-inclusive resorts, four adults, exactly two rooms, five nights Jan 18–23, free cancellation | 23/23 rows fetched; four suggestions excluded; 14 qualifying offers. Riu Santa Fe: $2,302.60 stay plus $31.20 known exclusions. Both repeated room blocks and cancellation deadlines retained. |
| 7 | San Juan four-adult stay Jan 18–22, review score at least 8, displayed total at most $1,000, then EUR pricing | Separate USD/EUR queries passed. Hotel Villa del Sol: USD832 plus74.88 known exclusions; separately EUR743.02 plus66.87. Property is in nearby Carolina/Isla Verde, not silently relabeled San Juan. Inventory coverage partial. |
| 8 | Filter saved Cancún results under a budget, clear the budget, export cheapest distinct properties | Offline replay preserves raw offers and quote timestamps. Clearing restores retained candidates. Export retains the saved top-N limit; `--all-results` clears only that cap. Comparison CSV retains global/per-query coverage. |
| 9 | Compare one room versus two rooms for four adults in Cancún, Jan 11–15 | Separate scopes and exact-room filtering passed. A one-room request can return a cheaper two-room offer, correctly labeled and excluded by `--exact-rooms`. Party totals are not replaced by one room block's amount. |
| 10 | Five-star Maui hotel/resort for four people Jan 11–15 for under $50 total | Correct REGION 3183; ten priced rows fetched, none under the displayed-total cap. `filtered_out` remains distinct from blocked/unavailable/unknown inventory; no claim that hotels do not exist. |

Case 4's cheapest observed offer was **Aston at The Whaler on Kaanapali Beach, January 23–27**, **USD2,243.40** for the stay (**560.85/night**), one offered/requested room, plus **419.79 known excluded charges**. This result describes the tested dates/provider rows, not every possible rate or the final payable total. Quotes can change between requests.

## Gaps found and repaired

- **Wrong geography:** all three agents reproduced `Maui, Hawaii` resolving to `HOTEL 276309`. The [official Maui region page](https://www.booking.com/region/us/maui-hawaii.html) and a successful geographic search identify **REGION 3183**. Verified aliases now pin it; property-level resolutions fail explicitly. Cancún, Cabo and San Juan aliases are also pinned to checked city IDs. `destinations` exposes these bindings.
- **Week/month and multi-location searches:** added `compare`, with an explicit bounded query plan, daily or sampled windows, per-cell coverage and destination/global minima. January four-night stays produce exactly 27 starts; Jan 11–17 produces three. Both bounds contain the complete stay.
- **Top ten properties versus offers:** added `--unique-properties`, selecting each property's cheapest matching offer before the result limit while retaining every raw offer.
- **Saved-view exports:** fixed an export-only `filter` call silently clearing a saved result cap. It now preserves the selected view; `--all-results` removes only the limit and `--clear-filters` removes limits/local filters. JSON and CSV retain coverage and timestamps.
- **Instructions and output paths:** the skill now explains hotel routing, geographic verification, days/nights, adult/child counts, exact offered rooms, star versus review ratings, displayed-budget limits and source coverage. Nested output directories are created before live retrieval.

## Reproduce the workflows

```bash
.venv/bin/python hotel_tools.py destinations

# Case 1: every eligible five-calendar-day stay in the named week.
.venv/bin/python hotel_tools.py compare --location 'Cancun, Mexico' \
  --start-date 2027-01-11 --end-date 2027-01-17 --nights 4 --adults 4 \
  --stars 4,5 --unique-properties --page-size 50 --max-pages 1 \
  --output tmp/hotels/use-cases/cancun-week.json

# Case 2: ten distinct properties, with offered rooms disclosed.
.venv/bin/python hotel_tools.py search --location 'Maui, Hawaii' \
  --check-in 2027-01-11 --check-out 2027-01-15 --adults 4 --stars 5 \
  --unique-properties --max-results 10 --format table

# Case 3: one exact stay, two independently scoped destinations.
.venv/bin/python hotel_tools.py compare --location 'Maui, Hawaii' \
  --location 'San Juan, Puerto Rico' --start-date 2027-01-11 \
  --end-date 2027-01-15 --nights 4 --adults 4 --property-types hotel \
  --page-size 50 --max-pages 1 --output tmp/hotels/use-cases/two-places.json

# Case 4: inspect the plan; omit --dry-run to retrieve all 27 dated searches.
.venv/bin/python hotel_tools.py compare --location 'Maui, Hawaii' \
  --start-date 2027-01-01 --end-date 2027-01-31 --nights 4 --adults 4 \
  --stars 5 --unique-properties --max-results 10 --page-size 50 --max-pages 1 \
  --dry-run

# Case 8: export the saved selection without another provider request.
.venv/bin/python hotel_tools.py filter --input tmp/hotels/use-cases/two-places.json \
  --unique-properties --max-results 10 --format csv \
  --output tmp/hotels/use-cases/two-places.csv
```

`--page-size 50 --max-pages 1` is a bounded evaluation setting, not a property-coverage guarantee. Inspect coverage before claiming cheapest. `compare --max-searches` defaults to100 and can be raised explicitly up to1000; date steps above one sample the calendar. Provider denial, rate limit or schema failure stops the comparison with completed searches retained. Dates and destination bindings appear in `--dry-run` JSON before network work.

## Automated checks

`test_hotel_use_cases.py` contains ten named offline regressions corresponding to the table. Public-shaped synthetic responses exercise planning, scope validation, distinct ranking, exports and failure states without asserting mutable fare amounts. These tests complement the independent live trials; they are not live-availability claims.

```bash
.venv/bin/python -B -m unittest test_hotel_use_cases -v
.venv/bin/python -B -W error::DeprecationWarning -m unittest discover -v
```
