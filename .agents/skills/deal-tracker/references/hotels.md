## Hotel/resort workflow (CLI only)

Use `hotel_tools.py` for hotel pricing, independently of flights and the flight UI. For field-level or provider diagnostics, consult [hotel quote semantics](../../../../docs/booking-hotel-pricing.md); ordinary searches use the workflow below. Discover verified geographic names and IDs with `hotel_tools.py destinations`; Maui is REGION 3183, not a city or individual hotel. Known aliases are pinned automatically. Unknown names must resolve to a geographic destination; property-level resolutions fail explicitly. Preserve actual property city names, including nearby towns returned for a city search.

Translate calendar dates and guests explicitly. Five **calendar days** means four nights; five **nights** means checkout five days after check-in. For an ambiguous “five days,” state the four-night assumption. Use the upcoming stated month/year, ask for unspecified child ages when children are requested, and treat a group of four as four adults unless told otherwise. One requested room is the default; disclose offers with different room counts, or pass `--exact-rooms` when one/two rooms is a requirement. For four stars or above pass `--stars 4,5`; “five star” is a property rating, not review score.

Use `search` for fixed dates. Use `compare` for an entire week/month or multiple locations; repeat `--location` for each place. Both `--start-date` and `--end-date` bound the complete stay. For Jan 11–17 with four nights, all eligible starts are Jan 11, 12 and 13. January 1–31 with four nights has 27 eligible starts. Run `--dry-run` first for a date grid; it emits the exact plan without network calls. A date step above one is sampled coverage.

For “cheapest ten hotels,” use `--unique-properties --max-results 10`: deduplicate matching property IDs before limiting, and return fewer if fewer qualify. JSON preserves raw offers for later re-ranking. Compare only the same occupancy, currency and stated price basis; EUR requires a separate fresh query. Keep displayed price, known excluded charges, offered rooms, meal plan and cancellation deadlines together; a cancellation badge is not complete refund terms. User budgets apply to the displayed basis and do not prove final fee-inclusive affordability.

```bash
.venv/bin/python hotel_tools.py search --location 'Maui, Hawaii' \
  --check-in 2027-01-11 --check-out 2027-01-15 --adults 4 --stars 5 \
  --unique-properties --max-results 10 --snapshot tmp/hotels/maui.json --format summary

.venv/bin/python hotel_tools.py compare --location 'Maui, Hawaii' \
  --start-date 2027-01-01 --end-date 2027-01-31 --nights 4 --adults 4 \
  --stars 5 --unique-properties --max-results 10 --dry-run --format summary
```

Use `--snapshot PATH --format summary` for normal agent searches: the CLI saves full replayable JSON and returns compact facts, counts, coverage, fees and room/cancellation terms. Read the summary instead of loading raw offers or calculating minima in agent code. Summary output is not replayable; use its `snapshot_path` with `filter`. It shows five selected results by default (up to twenty when a larger limit is requested), with omissions counted; `--max-results 10` shows ten when available. Full snapshots retain every raw offer.

`filter` handles either a search or comparison snapshot without network; `--clear-filters` restores retained candidates, and CSV includes coverage records as well as selected rates. Export-only filtering preserves the saved result cap; `--all-results` removes just that cap. Offline filtering reports zero HTTP requests and preserves original quote timings. `quote_timings` describe the source lookup; `execution` times the current command. Page/result caps, excluded suggestions, errors and unfetched dates remain visible. Report “cheapest observed” unless provider/date coverage supports a stronger statement; an impossible budget or empty subset does not establish that no hotels are available. `compare` stops after a provider denial/rate limit/schema failure and retains completed searches. Use the [ten real-world evaluation cases](../../../../docs/hotel-use-cases.md) when validating hotel-tool changes.


For repeatable evaluations use `evaluate_hotel_skill.py --help`. Keep model instruction-following, provider coverage, command timing and output-size metrics separate. Inspect the compact dry-run before a date grid; ordinary exact-date searches need one search command, not browsing or source-code inspection.
