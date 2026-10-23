# Hotel skill timing and efficiency evaluation

Measured October 22, 2026 against the [ten hotel use cases](hotel-use-cases.md). The evaluation separates live provider workflows, deterministic tool correctness, compact output, and independent agent interpretation. It does not treat a passing unit test as evidence of model behavior or complete hotel inventory.

## Timed live workflows

One sequential live run used one page of up to 50 rows per query, without automatic retries or browser workarounds. Times use `perf_counter` around the complete CLI workflow, including process startup, HTTP work and file output. They exclude model reasoning and human interaction.

| Case | Workflow | Wall seconds | HTTP requests |
|---|---|---:|---:|
| 1 | Cancún week, three eligible four-night stays | 3.056 | 3 |
| 2 | Maui five-star, ten distinct properties | 0.965 | 1 |
| 3 | Maui and San Juan, separate destination minima | 2.111 | 2 |
| 4 | Maui January, all 27 eligible four-night stays | 23.870 | 27 |
| 5 | Cancún family, breakfast/rating/room/budget filters | 1.205 | 1 |
| 6 | Cabo all-inclusive, two rooms, five nights, cancellation badge | 0.933 | 1 |
| 7 | San Juan USD and separate EUR quotes | 2.061 | 2 |
| 8 | Saved-result budget/filter clearing/CSV workflow | 0.273 | 0 |
| 9 | Cancún exact one-room versus two-room queries | 2.060 | 2 |
| 10 | Maui five-star, impossible displayed-total budget | 0.990 | 1 |
| **Total** | **Ten workflows** | **37.524** | **40** |

All ten workflows passed **407/407 observable checks**, including request scope, geography, dates/guests/currency, selected filters, deterministic minima, source-status handling and actual CSV output. Some searches intentionally have partial property coverage; the check requires truthful coverage rather than pretending that one page exhausts a destination. Prices and network latency can change.

`search` and `compare` now record source `timings`, including fetch/decode seconds and HTTP request count. Existing `elapsed_seconds` remains available. Compact CLI output also records `execution`: an offline filter reports zero HTTP requests and its own command duration, while `quote_timings` continues to describe the original lookup. The internal command timer begins inside Python; the external evaluator additionally includes startup and file output.

## Compact output and deterministic work

The new `--format summary --snapshot PATH` combination returns answer-sized JSON while saving full replayable data to disk. It retains query scope, actual property cities, requested/offered rooms, known excluded charges, incomplete final/refund terms, cancellation deadlines, quote timestamps, links, source errors and per-date coverage. Counts identify raw offers, matching offers/properties, selection, shown results and omissions. A summary is explicitly not replayable; its full snapshot is.

Using the **exact same fresh live snapshots** for both formats, five offline render repetitions per case:

| Metric | Full JSON | Compact summary |
|---|---:|---:|
| Total returned UTF-8 bytes across ten cases | 7,375,085 | 98,834 |
| Maui monthly output bytes | 2,488,713 | 23,368 |
| Maui monthly median render time | 8.436 ms | 0.192 ms |

That is **98.66% less returned text overall**, and **99.06% less** for the monthly example. These are exact byte measurements, not measured model-token savings. The evaluator also reports a clearly labeled characters/4 token proxy for environments without a tokenizer. It is not a model's billed usage.

An independent audit measured monthly ranking at roughly 2 ms before these changes. Aggregation was therefore left intact: avoiding megabytes of repeated raw data offers a larger benefit than a speculative ranking refactor. No provider-speed improvement is claimed from faster JSON rendering.

## Independent agent skill evaluation

Two fresh subagents received the same ten natural-language requests and produced executable plans without live network calls. One used a frozen baseline skill/tool snapshot; the other used the updated skill. A semantic grader expands actual date grids and checks flags, scopes, currencies, room counts, distinct-property selection and offline artifact flow. It accepts valid aliases, preflights and alternative command sequences. Answer prose and real-world recommendation quality are not graded.

Both agents passed **52/52 instruction checks**. Thus the observed interpretation score was preserved, not improved from a lower baseline.

The skill now routes hotel, flight and other-lane instructions separately. The entrypoint shrank from **17,018 to 2,142 bytes**. The hotel evaluator read **6,636 guidance bytes**, versus **27,945** in the baseline trial; help output is excluded from those read counters.

The agents' own local trace counters provided these observations:

| Metric | Baseline | Updated |
|---|---:|---:|
| Instruction checks | 52/52 | 52/52 |
| Uncached input tokens | 20,869 | 7,080 |
| Total input tokens, including repeated cached input | 243,205 | 265,896 |
| Cached input tokens | 222,336 | 258,816 |
| Output tokens | 3,284 | 4,105 |
| Total model-reported tokens | 246,489 | 270,001 |
| Tool round trips, excluding coordination | 4 | 6 |
| Tool-output bytes | 40,507 | 20,664 |
| First-to-last trace wall time | 93.380 s | 114.080 s |

Uncached input fell **66.07%**, but total model token volume rose **9.54%** and planning time rose **22.17%**. More tool round trips and a larger returned plan offset the smaller guidance load in this trial. This is **one agent per variant**, not a statistically established speed or billing result. Trace counters include cached context repeatedly processed across turns; they are model usage metadata, not an invoice. There is no blanket claim that the updated skill is faster or uses fewer total tokens.

## Repeat the evaluation

All tools use the standard library. The case manifest and evaluator are checked-in source; captures, reports and agent traces stay outside commits. A new checkout can collect its own live snapshots, then replay that collection without needing the earlier local fixture layout:

```bash
# One bounded live collection. Case 8 reuses the run's collected Cancún result.
.venv/bin/python evaluate_hotel_skill.py --live \
  --output tmp/hotels/evals/live-report.json

# No new network: compare full and compact render output on those exact snapshots.
.venv/bin/python evaluate_hotel_skill.py replay \
  --replay-live-report tmp/hotels/evals/live-report.json --repeats 5 \
  --output tmp/hotels/evals/replay-report.json

# Deterministically score an agent's structured plans without executing them.
.venv/bin/python evaluate_hotel_skill.py grade-plans \
  --input tmp/hotels/evals/agent-plan.json \
  --output tmp/hotels/evals/agent-grade.json

# Normal efficient search: compact stdout, complete retained data on disk.
.venv/bin/python hotel_tools.py search --location 'Maui, Hawaii' \
  --check-in 2027-01-11 --check-out 2027-01-15 --adults 4 --stars 5 \
  --unique-properties --max-results 10 \
  --snapshot tmp/hotels/maui.json --format summary
```

`hotel_skill_eval_cases.json` defines the requests, canonical scopes and workflow steps. `grade-plans` consumes `{ "cases": [{ "id": 1, "commands": [["compare", "--location", "...", "..."]] }] }`; each command is an argv array beginning with `search`, `compare` or `filter`. The evaluator parses and scores these arrays without executing agent-supplied command text. Optional `--agent-trace /exact/path.jsonl` extracts counters only from the explicitly supplied evaluation trace; it does not discover unrelated chats or export transcript text.

The evaluator exits nonzero for failed behavior checks or missing artifacts. Its own regressions include wrong dates/guests/geography, omitted currencies, sampled calendars passed off as monthly coverage, missing distinct selection, bad export lineage, hidden fees and source failures. Compact-output tests separately verify preserved decision facts and source timestamps. The two evaluation layers should remain separate in future scorecards.
