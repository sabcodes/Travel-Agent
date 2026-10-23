# Booking.com hotel/resort CLI

Implemented October 22, 2026 for the CLI portion of [hotel backlog #5](https://github.com/sabcodes/Travel-Agent-Skills/issues/5). Hotel UI and flight packaging are outside this increment. `hotel_tools.py` is independent of `main.py`, the flight UI, and the Airbnb provider.

## Retrieval evidence

The supplied `www.booking.com.har` contains 1,748 requests, including 37 read-only `FullSearch` GraphQL operations. The relevant endpoint is `https://www.booking.com/dml/graphql`. Initial search results also appear in the page's Apollo store, but the implemented client requests JSON directly.

The captured schema includes city destinations, check-in/check-out, adults, requested rooms and child ages. Booking.com's public destination ID for Cabo San Lucas is `-1698603`. Source filters include:

| Selection | Website filter |
|---|---|
| Hotels | `ht_id=204` |
| Resorts | `ht_id=206` |
| Both | `ht_id=204;ht_id=206` |
| Breakfast included | `mealplan=1` |
| All-inclusive | `mealplan=4` |
| All meals included | `mealplan=3` |
| Five stars | `class=5` |

Other observed property types are exposed by `search --help` and `inspect-har`. Multiple choices within a field are alternatives. Combining property type with meal-plan or star filters narrows the search. Local filters also validate returned types, stars and advertised meals because the provider sometimes returns other property types despite requested filters. Breakfast filtering accepts advertised all-meals/all-inclusive packages as well.

Ordinary HTTP to the HTML search page returned a JavaScript verification page, despite HTTP 200. Fresh ordinary headless Chrome navigations redirected to an undated city landing page, without usable dated property cards. These trials did not establish a working browser search.

The read-only JSON search operation succeeded with a minimal request containing dates, location, adults/rooms/child ages, filters, pagination and price sorting. It resolves city names without a captured destination ID, but realistic testing found ambiguous names can resolve to an individual hotel. Verified aliases now pin Maui REGION 3183 and the checked Cancún, Cabo and San Juan city IDs; property-level resolutions are rejected. Discover these bindings with `destinations`. Requests use the public persisted-operation hash and a descriptive user agent. They omit captured cookies, CSRF tokens, page/session identifiers, request IDs, affiliate labels and experiment state. No browser or additional Python package is used by the CLI.

The website operation is **undocumented**. Its persisted hash is a query identifier, not an API credential; it can change. `schema_changed`, `scope_mismatch`, `blocked`, `rate_limited` and `unavailable` states require investigation, rather than interpreting failures as empty inventory. There is no automatic retry or browser/security fallback. The official partner Demand API remains a separate option with [registration and authentication requirements](https://developers.booking.com/demand/docs/getting-started/prerequisites).

## Quote and comparison semantics

- `displayed_total` is `priceDisplayInfoIrene.displayPrice.amountPerStay.amountUnformatted`, in the requested currency, for the entire party/stay offer. `displayed_nightly_average` divides that amount by checkout minus check-in. Budgets and ranking use this displayed basis, excluding some charges.
- `rate_blocks` preserves room ID, policy/meal/bundle/package identity, occupancy, per-block price and cancellation deadline. Repeated identical blocks can mean multiple rooms; the importer preserves them. The party total is not replaced by a single block's price. Offered room counts come from matching unit configurations; `--exact-rooms` excludes different/unknown counts.
- `reported_charges` preserves inclusion flags, type IDs, modes and per-stay amounts. Only explicitly excluded, same-currency amounts are added to `known_excluded_charges_total`. Per-stay amounts are not multiplied again by nights. The original aggregate is retained separately; discrepancies with the sum of explicitly excluded items are flagged. In the HAR, some aggregates include amounts whose inclusion flag is `INCLUDED`.
- `displayed_plus_known_excluded` is an arithmetic subtotal, not a final payable amount. Unknown charge amounts/currencies, inconsistent aggregates and tax exceptions remain explicit. `taxes_and_fees_complete` is false and `final_payable_total` remains null. Lack of a cancellation badge does not establish a nonrefundable rate; full refund terms remain unknown.
- Every page must match requested dates, nights, adults, children/ages and requested rooms. Explicit destination IDs/types must match; name searches retain the resolved ID/type and reject changes between pages. Suggested alternate dates, autoextended/nonmatching suggestions, sold-out properties and missing/wrong-currency quotes are excluded with reasons.
- Ranking is deterministic by displayed total, property ID, then complete room/rate identity. Repeated offers are deduplicated, preserving the lowest observed displayed quote and its timestamp. Quotes with different rate identities remain separate.
- `raw_rates` retains all fetched priced candidates, while `rates` applies local filters and an optional export limit. Clearing a saved filter can restore retained offers but cannot retrieve unfetched inventory or broaden the original live search.

## Commands and output

Run from the project folder with Python 3.10+. The existing `.venv/bin/python` works; no install step is needed for this module.

```bash
mkdir -p tmp/hotels
.venv/bin/python hotel_tools.py search --location 'Cabo San Lucas, Mexico' \
  --check-in 2027-01-15 --check-out 2027-01-19 --adults 4 --rooms 1 \
  --property-types hotel,resort --max-pages 5 --progress-json \
  --output tmp/hotels/cabo.json

.venv/bin/python hotel_tools.py search --location 'Cancun, Mexico' \
  --check-in 2027-01-15 --check-out 2027-01-19 --adults 4 --rooms 2 \
  --children-ages 8 --currency EUR --max-pages 1 --output tmp/hotels/cancun-family.json

.venv/bin/python hotel_tools.py filter --input tmp/hotels/cabo.json \
  --property-types resort --meal-plan all-inclusive --exact-rooms \
  --max-nightly-price 2000 --format table

.venv/bin/python hotel_tools.py filter --input tmp/hotels/cabo.json \
  --clear-filters --property-types hotel,resort --format csv --output tmp/hotels/cabo.csv

# Inspect scopes first; prefetches with different filters are distinct searches.
.venv/bin/python hotel_tools.py inspect-har --input www.booking.com.har
.venv/bin/python hotel_tools.py import-har --input www.booking.com.har \
  --entry 1302 --output tmp/hotels/cabo-har.json
```

`--output` writes the selected format instead of stdout. JSON snapshots contain query, live/applied filters, raw/filtered offers, excluded-property reasons, pages, coverage, provenance and source failures. CSV contains a coverage record followed by rate records, with nested room/charge fields encoded as JSON; the coverage record preserves query/error context even for an empty or failed search. Table output is a brief price overview; JSON/CSV retain the full rate fields.

`--snapshot PATH --format summary` saves the full JSON separately and emits compact JSON for an agent to read. Summary output includes omitted-result counts, actual cities, room/fee/cancellation facts, per-date coverage and errors, but is explicitly not replayable. It shows five selected results by default, or the requested limit up to twenty. `quote_timings` are source lookup timings; `execution` times the current CLI command and reports zero HTTP requests for offline operations. External full-workflow timings and evaluation limits are documented in the [skill scorecard](hotel-skill-evaluation.md).

The default live bound is three pages of 20 rows, with at most 20 pages/50 rows per page and a configurable socket timeout (20 seconds by default, maximum 60). A socket timeout is not a hard total search deadline. Page progress goes to stderr. Ctrl-C returns completed pages, emits `cancelled` and exits 130. Exit 1 indicates a source error with structured output retained; exit 2 indicates invalid CLI/file input. A bounded successful search can exit 0 with `status: partial`; inspect coverage and excluded-property reasons.

`coverage.all_reported_rows_fetched` describes traversing the provider's reported rows, not unique properties or exhaustive hotel inventory. Pages can repeat sponsored offers and change between requests. Name resolution outside verified aliases is provider-defined; check the resolved destination and property cities, or pin an explicit Booking.com ID/type for ambiguous names. Airport presets are not a hotel coverage manifest. `--destination-type` requires `--destination-id` unless it is the default `CITY`.

For week/month or multi-location queries use `compare`; see the [ten use-case evaluations](hotel-use-cases.md) for exact commands and outcomes. `--start-date` and `--end-date` contain the complete stay, `--nights` is explicit, and `--date-step-days` defaults to daily check-ins. `--dry-run` emits the exact plan without fetching quotes. Comparison JSON retains each search, per-cell coverage and minima, plus global and per-location minima. `--unique-properties` selects each property's cheapest eligible offer before `--max-results`. Comparison CSV adds a global coverage record, per-query coverage records and the selected rate rows. `filter` preserves a saved result cap; `--all-results` clears only that cap, while `--clear-filters` also clears local filters. Nested output directories are created before live retrieval.

HAR import chooses one explicit `FullSearch` response, retains captured filters and quote timestamps, and marks availability as `quoted_in_capture`. It does not replay captured headers or perform a fresh lookup. Only full response bodies in the supplied HAR format are supported; the initial HTML Apollo store is not imported. Keep raw HARs and normalized personal searches outside commits; `*.har` and `tmp/hotels/` are ignored.

## Verification, October 22, 2026

| Search | Fetched / reported rows | Distinct priced offers | Matching hotel/resort offers |
|---|---:|---:|---:|
| Cabo, four adults, one room, USD | 80 / 80 across four pages | 79 | 73 |
| Cancún, two adults, one room, USD | 20 / 206 on one page | 20 | 19 |
| Cancún, four adults, two rooms, child aged eight, EUR | 20 / 195 on one page | 20 | 18 |
| Historical Cabo HAR entry 1302 | 20 / 24 on one captured page | 20 | 20 |

All live pages matched dates and occupancy. The EUR request returned EUR amounts. Different offered room counts and fee-aggregate discrepancies appeared and were preserved. A saved Cabo replay with resorts, all-inclusive meals and exact requested room count returned ten matching offers. Prices and counts can change; these observations do not establish broader coverage or final payment amounts.

Offline tests cover guest/date/currency scope, nightly conversion, repeated room blocks, fee inclusion/discrepancies, missing prices, suggestions, stable filtering/ranking, pagination/deduplication, source errors, cancellation, CSV safety and historical import/replay without session leakage. Run:

```bash
.venv/bin/python -B -W error::DeprecationWarning -m unittest discover -v
```

Issue #5 is completed for the approved CLI-only Booking.com scope. Hotel UI and flight packaging are deferred to separately requested work. Confirmed final payment/refund terms remain a provider limitation, explicitly reported as unknown rather than inferred from search quotes.
