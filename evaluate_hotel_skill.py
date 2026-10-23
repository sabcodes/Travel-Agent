"""Deterministic hotel workflow evals. Replay is offline; --live explicitly uses Booking.com.

Output sizes and chars/4 estimates describe tool output, never actual model usage.
Plan grades check executable request semantics, not the quality of answer prose.
"""
import argparse
import copy
import csv
from datetime import datetime
import io
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

import hotel_tools as hotels

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "hotel_skill_eval_cases.json"
SOURCES = ROOT / "tmp/hotels/use-cases"
SCOPE = ("destination_id", "destination_type", "check_in", "check_out", "adults", "rooms", "children_ages", "currency")


def cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def distribution(values):
    ordered = sorted(values)
    return {"samples": len(values), "min": min(values), "median": statistics.median(values),
            "p95": ordered[max(0, (95 * len(values) + 99) // 100 - 1)], "max": max(values)}


def output_size(text):
    return {"utf8_bytes": len(text.encode("utf-8")), "characters": len(text),
            "estimated_tokens_chars_div_4": len(text) / 4}


def checks_report(checks):
    return {"passed": sum(c["passed"] for c in checks), "total": len(checks),
            "all_passed": bool(checks) and all(c["passed"] for c in checks), "assertions": checks}


def check(checks, name, condition):
    checks.append({"name": name, "passed": bool(condition)})


def scope(query):
    bound = hotels.bind_destination(copy.deepcopy(query))
    return tuple(tuple(bound.get(k, [])) if k == "children_ages" else bound.get(k) for k in SCOPE)


def command_plan(argv):
    """Parse flags and expand actual dates; never execute an agent-supplied command."""
    if not isinstance(argv, list) or not argv or argv[0] not in ("search", "compare", "filter") or not all(isinstance(x, str) for x in argv):
        raise ValueError("commands must be argv lists beginning search, compare or filter")
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False, allow_abbrev=False)
    parser.add_argument("command", choices=("search", "compare", "filter"))
    parser.add_argument("--location", action="append", default=[])
    for name in ("check-in", "check-out", "start-date", "end-date", "input", "output", "snapshot"):
        parser.add_argument("--" + name)
    for name, default in (("adults", 2), ("rooms", 1), ("nights", None), ("date-step-days", 1), ("max-searches", 100), ("max-results", None), ("destination-id", None), ("max-pages", 3), ("page-size", 20)):
        parser.add_argument("--" + name, type=int, default=default)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--destination-type", default="CITY")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--children-ages", default="")
    parser.add_argument("--format", choices=("json", "summary", "table", "csv"), default="json")
    for name in ("clear-filters", "all-results", "dry-run", "progress-json"):
        parser.add_argument("--" + name, action="store_true")
    hotels.add_filters(parser)
    try:
        args = parser.parse_args(argv)
    except (SystemExit, argparse.ArgumentError) as exc:
        raise ValueError("unsupported or malformed command flags") from exc
    filters = hotels.filters_from_args(args)
    queries = []
    if args.command != "filter":
        base = {"adults": args.adults, "rooms": args.rooms, "children_ages": [int(n) for n in hotels.split_values(args.children_ages)], "currency": args.currency}
        if args.command == "compare":
            queries = hotels.comparison_plan(args.location, base, args.start_date, args.end_date, args.nights, args.date_step_days, args.max_searches)["queries"]
        else:
            if len(args.location) != 1:
                raise ValueError("search needs exactly one location")
            queries = [hotels.bind_destination(hotels.validate_query({**base, "location": args.location[0], "check_in": args.check_in, "check_out": args.check_out,
                       "destination_id": args.destination_id, "destination_type": args.destination_type}))]
    return {"queries": queries, "filters": filters, "args": vars(args)}


def expected_cells(case):
    result = [(q, p["filters"], p["args"]["max_results"]) for argv in case["commands"] for p in [command_plan(argv)] for q in p["queries"]]
    # The original case 7 sets a USD budget and requests fresh EUR quotes;
    # it does not define an exchange conversion or a separate EUR budget.
    if case["id"] == 7:
        for query, filters, _ in result:
            if query["currency"] == "EUR":
                filters.pop("max_total_price", None)
    return result


def grade_filter_plan(plans, checks):
    """Follow saved-view lineage rather than require a particular argv recipe."""
    check(checks, "offline_filter_steps_only", bool(plans) and all(p["args"]["command"] == "filter" for p in plans))
    artifacts = {}
    initial_path = plans[0]["args"]["input"] if plans else None
    initial = {"filters": {"property_types": ["hotel", "resort"], "stars": [4, 5]}, "limit": None, "cleared": False}
    budget, distinct, clear, csv_export, lineage = False, False, False, False, bool(initial_path)
    for plan in plans:
        args = plan["args"]
        previous = artifacts.get(args["input"], initial if args["input"] == initial_path else None)
        if previous is None:
            lineage = False
            continue
        filters = hotels.filters_from_args(argparse.Namespace(**args), {"property_types": None} if args["clear_filters"] else previous["filters"])
        limit = args["max_results"] if args["max_results"] is not None else None if args["clear_filters"] or args["all_results"] else previous["limit"]
        state = {"filters": filters, "limit": limit, "cleared": previous["cleared"] or args["clear_filters"]}
        budget |= filters.get("max_total_price") == 500
        distinct |= bool(filters.get("unique_properties")) and limit == 10
        clear |= args["clear_filters"]
        csv_export |= args["format"] == "csv"
        if args["output"] and args["format"] == "json":
            artifacts[args["output"]] = state
        if args["snapshot"]:
            artifacts[args["snapshot"]] = state
    check(checks, "displayed_budget_500_phase", budget)
    check(checks, "distinct_top10_phase", distinct)
    check(checks, "clear_filters_phase", clear)
    check(checks, "csv_export_from_valid_saved_view", csv_export)
    check(checks, "valid_saved_artifact_lineage", lineage)


def grade_plans(document):
    """Compare query/filter meanings; alternate argv ordering and aliases are accepted."""
    supplied = document.get("cases", [])
    def case_id(value):
        value = str(value)
        return str(int(value)) if value.isdecimal() else value
    by_id = {case_id(c.get("id")): c for c in supplied}
    results = []
    for case in cases():
        checks = []
        entry = by_id.get(str(case["id"]))
        check(checks, "case_present_exactly_once", entry is not None and sum(case_id(c.get("id")) == str(case["id"]) for c in supplied) == 1)
        if entry:
            try:
                plans = [command_plan(a) for a in entry.get("commands", [])]
                if case["id"] == 8:
                    grade_filter_plan(plans, checks)
                else:
                    retrievals = [p for p in plans if not p["args"]["dry_run"]]
                    actual = [(q, p["filters"], p["args"]["max_results"]) for p in retrievals for q in p["queries"]]
                    expected = expected_cells(case)
                    check(checks, "retrieval_commands_present", bool(retrievals) and all(p["args"]["command"] != "filter" for p in plans))
                    check(checks, "exact_query_cells_dates_geography_guests_currency", sorted((scope(q) for q, _, _ in actual), key=str) == sorted((scope(q) for q, _, _ in expected), key=str))
                    def required_filters(query, filters):
                        result = copy.deepcopy(filters)
                        if case["id"] != 2:
                            result.pop("unique_properties", None)
                        if case["id"] == 7 and query["currency"] == "EUR":
                            result.pop("max_total_price", None)
                        return {k: sorted(v) if isinstance(v, list) else v for k, v in result.items()}
                    actual_constraints = [(scope(q), required_filters(q, f), n if case["id"] == 2 else None) for q, f, n in actual]
                    expected_constraints = [(scope(q), required_filters(q, f), n if case["id"] == 2 else None) for q, f, n in expected]
                    check(checks, "filters_and_unique_before_top_n", sorted(actual_constraints, key=str) == sorted(expected_constraints, key=str))
                    check(checks, "bounded_provider_work", all(1 <= p["args"]["max_pages"] <= 3 and 1 <= p["args"]["page_size"] <= 50 and 0 < p["args"]["timeout"] <= 60 for p in plans))
            except (ValueError, TypeError, KeyError) as exc:
                check(checks, "valid_executable_plan", False)
                checks[-1]["error"] = str(exc)
        results.append({"id": case["id"], **checks_report(checks)})
    return {"metric_scope": "deterministic command semantics; answer prose and actual agent execution are not graded", "cases": results,
            "passed": sum(r["passed"] for r in results), "total": sum(r["total"] for r in results), "all_passed": all(r["all_passed"] for r in results)}


def search_cells(snapshot):
    return snapshot.get("searches", []) if snapshot.get("kind") == "booking_hotel_comparison" else [snapshot]


def grade_snapshots(case, snapshots):
    checks = []
    if case["id"] == 8:
        return grade_offline(snapshots[0])
    cells = [s for snapshot in snapshots for s in search_cells(snapshot)]
    expected = expected_cells(case)
    check(checks, "all_requested_query_cells_present", sorted((scope(c["query"]) for c in cells), key=str) == sorted((scope(q) for q, _, _ in expected), key=str))
    for i, cell in enumerate(cells):
        prefix = f"cell_{i + 1}_"
        matches = [(f, n) for q, f, n in expected if scope(q) == scope(cell["query"])]
        check(checks, prefix + "scope_matches_request", len(matches) == 1)
        bound = hotels.bind_destination(copy.deepcopy(cell["query"]))
        check(checks, prefix + "resolved_geography", cell.get("resolved_destination") == {k: bound.get(k) for k in ("destination_id", "destination_type")})
        check(checks, prefix + "no_source_failure", not cell.get("source_errors"))
        cov = cell.get("coverage", {})
        check(checks, prefix + "coverage_is_explicit", all(k in cov for k in ("rows_fetched", "reported_total", "all_reported_rows_fetched")))
        check(checks, prefix + "source_status_not_hidden", (not cell.get("source_errors") and (cell.get("status") != "ok" or (cov.get("all_reported_rows_fetched") and not cell.get("excluded_properties")))) or (bool(cell.get("source_errors")) and cell.get("status") != "ok"))
        query = cell["query"]
        check(checks, prefix + "raw_rates_keep_dates_guests_rooms_currency", all(r.get("check_in") == query["check_in"] and r.get("check_out") == query["check_out"] and r.get("adults") == query["adults"] and r.get("children_ages") == query["children_ages"] and r.get("requested_rooms") == query["rooms"] and r.get("currency") == query["currency"] for r in cell.get("raw_rates", [])))
        check(checks, prefix + "fee_and_timestamp_fields_retained", bool(cell.get("limitations")) and cell.get("price_basis") == "displayed_party_stay_total" and all(all(k in r for k in ("fees_status", "known_excluded_charges_total", "final_payable_total", "quote_checked_at")) and r["final_payable_total"] is None for r in cell.get("raw_rates", [])))
        if matches:
            filters, _ = matches[0]
            check(checks, prefix + "required_filters_applied", all(cell.get("applied_filters", {}).get(k) == v for k, v in filters.items()))
            eligible = hotels.filter_rates(cell.get("raw_rates", []), filters)
            check(checks, prefix + "selected_rates_obey_filters", all(r in eligible for r in cell.get("rates", [])))
    for i, snapshot in enumerate(snapshots):
        filters = snapshot.get("applied_filters", {})
        eligible = hotels.filter_rates(snapshot.get("raw_rates", []), filters)
        limit = snapshot.get("max_results")
        check(checks, f"snapshot_{i + 1}_sorted_selection_unique_then_limit", snapshot.get("rates") == (eligible if limit is None else eligible[:limit]))
        check(checks, f"snapshot_{i + 1}_cheapest_matches_retained_inventory", snapshot.get("cheapest_displayed") == (eligible[0] if eligible else None))
        if case["id"] == 2:
            check(checks, "ten_result_limit", limit == 10 and len(snapshot["rates"]) <= 10)
        if snapshot.get("kind") == "booking_hotel_comparison":
            check(checks, "comparison_coverage_counts_match_cells", snapshot["coverage"]["completed_searches"] == len(snapshot["searches"]) and snapshot["coverage"]["planned_searches"] == len(expected))
            if case["id"] in (1, 4):
                check(checks, "every_eligible_date_not_sampling", snapshot["plan"]["date_step_days"] == 1 and snapshot["coverage"]["date_coverage"] == "every_eligible_check_in")
            if case["id"] == 3:
                check(checks, "per_destination_minima_present", len(snapshot.get("cheapest_by_location", {})) == len({q["destination_id"] for q, _, _ in expected}))
        if case["id"] == 10:
            check(checks, "empty_budget_view_is_distinct_from_source_failure", not snapshot["rates"] and bool(snapshot["raw_rates"]) and snapshot["status"] in ("filtered_out", "partial") and not snapshot["source_errors"])
    return checks_report(checks)


def grade_offline(snapshot):
    checks = []
    check(checks, "saved_source_has_no_provider_failure", not snapshot.get("source_errors"))
    saved = copy.deepcopy(snapshot)
    summarize = hotels.summarize_comparison if saved["kind"] == "booking_hotel_comparison" else hotels.summarize
    raw_before = copy.deepcopy(saved["raw_rates"])
    original_times = [r["quote_checked_at"] for r in raw_before]
    saved["applied_filters"]["max_total_price"] = 500
    summarize(saved)
    check(checks, "budget_filters_displayed_total", all(r["displayed_total"] <= 500 for r in saved["rates"]))
    budget_count = len(saved["rates"])
    saved["applied_filters"] = {"property_types": ["hotel", "resort"], "stars": [4, 5], "unique_properties": True}
    summarize(saved, 10)
    check(checks, "cleared_budget_restores_retained_candidates", saved["matching_rate_count"] >= budget_count and "max_total_price" not in saved["applied_filters"])
    check(checks, "raw_inventory_and_timestamps_unchanged", saved["raw_rates"] == raw_before and [r["quote_checked_at"] for r in saved["raw_rates"]] == original_times)
    check(checks, "distinct_before_top10", saved["rates"] == hotels.filter_rates(raw_before, saved["applied_filters"])[:10] and len({r["property_id"] for r in saved["rates"]}) == len(saved["rates"]))
    rows = list(csv.DictReader(io.StringIO(hotels.csv_export(saved))))
    exported = [r for r in rows if r.get("record_type") == "rate"]
    check(checks, "csv_preserves_selected_count_and_quotes", len(exported) == len(saved["rates"]) and all(r["quote_checked_at"] in original_times for r in exported))
    check(checks, "offline_operations_use_only_saved_inventory", saved["raw_rates"] == snapshot["raw_rates"])
    return checks_report(checks)


def benchmark(snapshots, repeats):
    output = {}
    for fmt in ("json", "summary"):
        elapsed = []
        texts = []
        for _ in range(repeats):
            copies = copy.deepcopy(snapshots)  # identical inputs, outside render timing
            started = time.perf_counter()
            texts = [hotels.render(snapshot, fmt) for snapshot in copies]
            elapsed.append(time.perf_counter() - started)
        joined = "".join(texts)
        output[fmt] = {"render_seconds": distribution(elapsed), **output_size(joined)}
    output["summary_byte_reduction_percent"] = 100 * (1 - output["summary"]["utf8_bytes"] / output["json"]["utf8_bytes"]) if output["json"]["utf8_bytes"] else None
    return output


def historical_timing(snapshots, offline=False):
    cells = [s for snapshot in snapshots for s in search_cells(snapshot)]
    times = [s.get("elapsed_seconds") for s in cells]
    seconds = sum(t for t in times if isinstance(t, (int, float)))
    return {"metric_scope": "original quote collection time, not offline replay duration" if offline else "historical saved search elapsed_seconds sum; excludes CLI startup, rendering, model time",
            "searches_with_timing": sum(isinstance(t, (int, float)) for t in times), "searches": len(cells),
            "seconds": None if offline else seconds, "original_collection_seconds": seconds,
            "pages_fetched": sum(len(s.get("pages", [])) for s in cells)}


def replay(source_root=SOURCES, repeats=3, selected=None):
    results = []
    for case in cases():
        if selected and case["id"] not in selected:
            continue
        result = {"id": case["id"], "request": case["request"], "sources": []}
        snapshots = []
        for name in case["sources"]:
            path = Path(source_root) / name
            try:
                raw = path.read_bytes()
                snapshots.append(json.loads(raw))
                result["sources"].append({"path": str(path), "artifact_bytes": len(raw)})
            except (OSError, ValueError) as exc:
                result.setdefault("errors", []).append({"path": str(path), "error": str(exc)})
        if len(snapshots) == len(case["sources"]):
            try:
                result["behavior"] = grade_snapshots(case, snapshots)
                result["benchmark"] = benchmark(snapshots, repeats)
                result["historical_search_timing"] = historical_timing(snapshots, offline=case["id"] == 8)
                result["source_errors"] = [e for s in snapshots for e in s.get("source_errors", [])]
            except (KeyError, ValueError, TypeError) as exc:
                result.setdefault("errors", []).append({"error": str(exc)})
        results.append(result)
    return {"mode": "offline_replay", "repeats": repeats, "metric_scope": "render-only perf_counter timing, identical deep-copied captured inputs; chars/4 is an output proxy, not measured model tokens", "cases": results,
            "all_passed": all(not r.get("errors") and r.get("behavior", {}).get("all_passed", False) for r in results)}


def replay_live_report(path, repeats=3, selected=None):
    """Replay an explicitly selected live collection, without the original local fixtures."""
    collection = json.loads(Path(path).read_text(encoding="utf-8"))
    results = []
    definitions = {c["id"]: c for c in cases()}
    for recorded in collection["cases"]:
        case_id = recorded["id"]
        if selected and case_id not in selected:
            continue
        result = {"id": case_id, "request": definitions[case_id]["request"]}
        try:
            paths = [Path(c["artifact"]) for c in recorded["commands"] if Path(c["artifact"]).suffix == ".json"]
            snapshots = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
            if case_id == 8:
                snapshots = snapshots[-1:]
            if not snapshots:
                raise ValueError("collection contains no replayable snapshot")
            result["behavior"] = grade_snapshots(definitions[case_id], snapshots)
            result["benchmark"] = benchmark(snapshots, repeats)
            result["historical_search_timing"] = historical_timing(snapshots, offline=case_id == 8)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["errors"] = [{"error": str(exc)}]
        results.append(result)
    return {"mode": "offline_replay_of_live_collection", "collection": str(path), "repeats": repeats, "cases": results,
            "metric_scope": "render-only timing on the exact collected inputs; no fresh network requests",
            "all_passed": bool(results) and all(not r.get("errors") and r.get("behavior", {}).get("all_passed", False) for r in results)}


def trace_metrics(path):
    """Read ONLY the explicitly supplied JSONL and return counters, never transcript text."""
    timestamps, usage, calls, coordination, events, malformed = [], None, 0, 0, 0, 0
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            events += 1
            if event.get("timestamp"):
                try:
                    timestamps.append(datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")))
                except (TypeError, ValueError):
                    pass
            payload = event.get("payload", {})
            if payload.get("type") in ("function_call", "custom_tool_call"):
                calls += 1
                if payload.get("namespace") == "collaboration" or str(payload.get("name", "")).startswith("collaboration."):
                    coordination += 1
            if payload.get("type") == "token_count":
                total = (payload.get("info") or {}).get("total_token_usage")
                if isinstance(total, dict):
                    usage = {k: total[k] for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens") if isinstance(total.get(k), (int, float))}
    if usage and "input_tokens" in usage and "cached_input_tokens" in usage:
        usage["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    return {"scope": "caller-supplied trace; last cumulative token_count; may cover more than one eval; usage is model metadata, not billed dollars",
            "path": str(path), "events": events, "malformed_lines": malformed, "tool_calls_including_coordination": calls,
            "coordination_calls": coordination, "tool_calls_excluding_coordination": calls - coordination,
            "elapsed_first_to_last_timestamp_seconds": (max(timestamps) - min(timestamps)).total_seconds() if timestamps else None,
            "model_token_usage": usage}


def live(selected=None):
    """Run canonical workflows once, sequentially, without retries or concurrency."""
    outdir = ROOT / "tmp/hotels/evals/live" / ("run-" + str(time.time_ns()))
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases():
        if selected and case["id"] not in selected:
            continue
        started = time.perf_counter()
        records, snapshots = [], []
        collected_week = outdir / "case01-1.json"
        saved = collected_week if collected_week.exists() else SOURCES / case["sources"][0]
        substitutions = {"{saved}": str(saved), "{budget}": str(outdir / "case08-budget.json"), "{selected}": str(outdir / "case08-selected.json")}
        for index, canonical in enumerate(case["commands"]):
            argv = [substitutions.get(arg, arg) for arg in canonical]
            target = outdir / f"case{case['id']:02d}-{index + 1}.json"
            if case["id"] == 8:
                if "--output" in argv:
                    target = Path(argv[argv.index("--output") + 1])
                else:
                    target = target.with_suffix(".csv")
                    argv += ["--output", str(target)]
            else:
                argv += ["--page-size", "50", "--max-pages", "1", "--timeout", "20", "--format", "summary", "--snapshot", str(target)]
            command_started = time.perf_counter()
            record = {"argv": argv, "artifact": str(target)}
            try:
                completed = subprocess.run([sys.executable, "-B", str(ROOT / "hotel_tools.py"), *argv], capture_output=True, text=True, timeout=900, cwd=ROOT)
                record.update(returncode=completed.returncode, stderr=completed.stderr, stdout_bytes=len(completed.stdout.encode("utf-8")))
                if target.exists():
                    record["artifact_bytes"] = target.stat().st_size
                    if target.suffix == ".json":
                        snapshot = json.loads(target.read_text(encoding="utf-8"))
                        snapshots.append(snapshot)
                        record["http_requests"] = 0 if argv[0] == "filter" else snapshot.get("timings", {}).get("http_requests")
                        record["quote_timings"] = snapshot.get("timings")
                else:
                    record["error"] = "Expected output artifact missing"
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                record["error"] = str(exc)
            record["cli_seconds"] = time.perf_counter() - command_started
            records.append(record)
            if record.get("returncode") != 0 or record.get("error"):
                break
        result = {"id": case["id"], "workflow_cli_seconds": time.perf_counter() - started, "commands": records}
        success = len(records) == len(case["commands"]) and all(r.get("returncode") == 0 and not r.get("error") for r in records)
        result["completed_without_cli_failure"] = success
        if snapshots:
            try:
                result["behavior"] = grade_snapshots(case, snapshots[-1:] if case["id"] == 8 else snapshots)
                result["source_errors"] = [e for s in snapshots for e in s.get("source_errors", [])]
                if case["id"] == 8 and success:
                    actual_rows = list(csv.DictReader(io.StringIO(Path(records[-1]["artifact"]).read_text(encoding="utf-8"))))
                    expected_rows = list(csv.DictReader(io.StringIO(hotels.csv_export(snapshots[-1]))))
                    assertions = result["behavior"]["assertions"]
                    check(assertions, "actual_cli_csv_matches_saved_selected_json_and_coverage", actual_rows == expected_rows)
                    result["behavior"] = checks_report(assertions)
            except (ValueError, KeyError, TypeError) as exc:
                result["grading_error"] = str(exc)
        results.append(result)
    return {"mode": "live_once", "metric_scope": "whole sequential CLI workflow perf_counter time including startup, provider I/O and file output; no model time", "page_size": 50, "max_pages_per_search": 1,
            "cases": results, "all_passed": all(r["completed_without_cli_failure"] and r.get("behavior", {}).get("all_passed", False) for r in results)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("replay", "grade-plans"), default="replay")
    parser.add_argument("--input", type=Path, help="Agent plans JSON: {cases: [{id, commands: [[argv...]], notes: ...}]}")
    parser.add_argument("--source-root", type=Path, default=SOURCES)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--case", type=int, action="append", choices=range(1, 11), help="Select cases; repeat as needed")
    parser.add_argument("--live", action="store_true", help="Explicit opt-in to run bounded canonical provider workflows once")
    parser.add_argument("--replay-live-report", type=Path, help="Replay snapshots referenced by an earlier --live report; no original local fixtures needed")
    parser.add_argument("--agent-trace", type=Path, help="Only this caller-supplied JSONL is inspected for usage metadata")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 100:
        parser.error("repeats must be 1–100")
    if args.mode == "grade-plans" and (args.input is None or args.live):
        parser.error("grade-plans needs --input and cannot use --live")
    if args.replay_live_report and (args.live or args.mode != "replay"):
        parser.error("replay-live-report requires replay mode without --live")
    report = (grade_plans(json.loads(args.input.read_text(encoding="utf-8"))) if args.mode == "grade-plans" else
              live(args.case) if args.live else replay_live_report(args.replay_live_report, args.repeats, args.case) if args.replay_live_report else
              replay(args.source_root, args.repeats, args.case))
    if args.agent_trace:
        report["agent_trace"] = trace_metrics(args.agent_trace)
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(json.dumps({"report": str(args.output), "all_passed": report["all_passed"], "cases": len(report["cases"])}))
    else:
        print(output, end="")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
