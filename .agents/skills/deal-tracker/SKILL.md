---
name: deal-tracker
description: Search and compare cash flights and Booking.com hotel/resort quotes, including flexible dates and multiple locations; analyze saved results and research public award deals and Chase, Amex, or Capital One offers.
---

# Travel Deal Tracker

Translate requests into checked-in CLI arguments; tools calculate dates, filters and ranking. Run from this repository with `.venv/bin/python`. For first-time setup see [README](../../../README.md#setup-and-requirements).

Read only the reference needed for the requested lane:

- **Hotel/resort quotes, flexible stay dates, multiple places, or saved hotel results:** [hotel workflow](references/hotels.md). Use `hotel_tools.py`; keep the full snapshot on disk and return compact summaries.
- **Cash flights or the local flight app:** [flight workflow](references/flights.md). Use `flight_tools.py` or `flight_ui.py`.
- **Public award reports, card/transfer offers or Airbnb:** [other lanes](references/other-lanes.md). These retain the legacy `main.py` modes.

Preserve the requested lane and distinguish observed quotes, partial coverage and provider failures. A saved-result filter is not a fresh availability check.

## Verification

```bash
.venv/bin/python -B -W error::DeprecationWarning -m unittest discover -v
.venv/bin/python /path/to/skill-creator/scripts/quick_validate.py .agents/skills/deal-tracker
```

Verify saved date bounds, inclusive lengths, airports, matrix coverage, errors, and ranking. Live-test lite/deep against two selected destinations before a broad sweep. Replay a saved snapshot to confirm identical filtering and minima. Tests and format validation do not prove live availability.

For authorized commits and pushes, keep README and skill guidance synchronized at each major stage and the final update. Use the installed `repo-security-gate` skill to check staged content before committing and the resulting commit before pushing. Supplement its credential patterns with personal-data and commit-metadata checks; keep generated reports and local fare snapshots outside commits. A clean staged diff does not certify the full history.
