"""Eval validity checks: broken plans and snapshots must lose points."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import evaluate_hotel_skill as ev
import hotel_tools as hotels
from test_hotel_use_cases import provider


class HotelSkillEvalTests(unittest.TestCase):
    def setUp(self):
        self.plans = {"cases": [{"id": c["id"], "commands": copy.deepcopy(c["commands"])} for c in ev.cases()]}

    def command(self, case, index=0):
        return self.plans["cases"][case - 1]["commands"][index]

    def change(self, case, flag, value, index=0):
        command = self.command(case, index)
        command[command.index(flag) + 1] = value

    def grade(self, case):
        return ev.grade_plans(self.plans)["cases"][case - 1]

    def test_canonical_plans_pass_all_ten(self):
        report = ev.grade_plans(self.plans)
        self.assertTrue(report["all_passed"], report)
        self.assertEqual(len(report["cases"]), 10)

    def test_alias_and_reordered_flags_preserve_semantics(self):
        self.plans["cases"][1]["id"] = "02"
        self.change(2, "--location", "Maui")
        command = self.command(2)
        index = command.index("--adults")
        command.extend(command[index:index + 2])
        del command[index:index + 2]
        self.assertTrue(self.grade(2)["all_passed"])

    def test_wrong_dates_and_guests_fail(self):
        self.change(2, "--check-out", "2027-01-16")
        self.change(5, "--children-ages", "7,13")
        self.assertFalse(self.grade(2)["all_passed"])
        self.assertFalse(self.grade(5)["all_passed"])

    def test_missing_unique_before_top10_fails(self):
        self.command(2).remove("--unique-properties")
        self.assertFalse(self.grade(2)["all_passed"])

    def test_fake_all_month_sample_fails(self):
        self.command(4).extend(["--date-step-days", "7"])
        self.assertFalse(self.grade(4)["all_passed"])
        self.assertEqual(len(ev.command_plan(ev.cases()[3]["commands"][0])["queries"]), 27)

    def test_preflight_and_minimum_only_month_are_valid(self):
        preflight = copy.deepcopy(self.command(4)) + ["--dry-run"]
        self.plans["cases"][3]["commands"].insert(0, preflight)
        self.change(4, "--max-results", "1", 1)
        self.assertTrue(self.grade(4)["all_passed"])

    def test_wrong_rooms_and_missing_currency_request_fail(self):
        self.change(9, "--rooms", "1", 1)
        self.plans["cases"][6]["commands"].pop()
        self.assertFalse(self.grade(9)["all_passed"])
        self.assertFalse(self.grade(7)["all_passed"])

    def test_offline_network_request_and_broken_export_chain_fail(self):
        self.change(8, "--input", "unrelated.json", 2)
        self.assertFalse(self.grade(8)["all_passed"])
        self.plans["cases"][7]["commands"] = self.plans["cases"][1]["commands"]
        self.assertFalse(self.grade(8)["all_passed"])

    def test_offline_snapshot_lineage_and_clear_first_are_valid(self):
        self.plans["cases"][7]["commands"] = [
            ["filter", "--input", "saved.json", "--clear-filters", "--max-total-price", "500", "--unique-properties", "--max-results", "10", "--snapshot", "budget.json", "--format", "summary"],
            ["filter", "--input", "budget.json", "--format", "csv"],
        ]
        self.assertTrue(self.grade(8)["all_passed"])

    def test_duplicate_case_is_not_a_pass(self):
        self.plans["cases"].append(copy.deepcopy(self.plans["cases"][0]))
        self.assertFalse(self.grade(1)["all_passed"])

    def snapshot(self):
        with patch("hotel_tools.fetch_page", side_effect=provider):
            return hotels.search_hotels({"location": "Maui", "check_in": "2027-01-11", "check_out": "2027-01-15", "adults": 4,
                "rooms": 1, "children_ages": [], "currency": "USD"}, {"property_types": ["hotel", "resort"], "stars": [5], "unique_properties": True}, max_results=10)

    def test_snapshot_scope_and_provider_failure_are_graded(self):
        snapshot = self.snapshot()
        self.assertTrue(ev.grade_snapshots(ev.cases()[1], [snapshot])["all_passed"])
        snapshot["source_errors"] = [{"status": "blocked", "message": "verification"}]
        snapshot["query"]["adults"] = 2
        report = ev.grade_snapshots(ev.cases()[1], [snapshot])
        self.assertFalse(report["all_passed"])
        self.assertTrue(any(not c["passed"] and "source_failure" in c["name"] for c in report["assertions"]))

    def test_snapshot_incorrect_geography_and_hidden_fees_fail(self):
        snapshot = self.snapshot()
        snapshot["resolved_destination"] = {"destination_id": 276309, "destination_type": "HOTEL"}
        snapshot["raw_rates"][0]["final_payable_total"] = 800
        self.assertFalse(ev.grade_snapshots(ev.cases()[1], [snapshot])["all_passed"])

    def test_render_benchmark_sizes_and_no_input_mutation(self):
        snapshot = self.snapshot()
        before = copy.deepcopy(snapshot)
        result = ev.benchmark([snapshot], 3)
        self.assertEqual(snapshot, before)
        self.assertEqual(result["json"]["utf8_bytes"], len(hotels.render(snapshot, "json").encode("utf-8")))
        self.assertEqual(result["json"]["render_seconds"]["samples"], 3)
        self.assertGreater(result["summary_byte_reduction_percent"], 0)
        self.assertEqual(ev.output_size("é"), {"utf8_bytes": 2, "characters": 1, "estimated_tokens_chars_div_4": .25})

    def test_missing_replay_artifacts_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = ev.replay(Path(tmp), 1, [1])
        self.assertFalse(report["all_passed"])
        self.assertTrue(report["cases"][0]["errors"])

    def test_replay_live_collection_is_independent_of_ignored_original_fixture_layout(self):
        snapshot = self.snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "collected.json"
            snapshot_path.write_text(json.dumps(snapshot))
            collection = Path(tmp) / "live-report.json"
            collection.write_text(json.dumps({"cases": [{"id": 2, "commands": [{"artifact": str(snapshot_path)}]}]}))
            report = ev.replay_live_report(collection, repeats=1)
        self.assertTrue(report["all_passed"], report)
        timing = ev.historical_timing([snapshot], offline=True)
        self.assertIsNone(timing["seconds"])
        self.assertGreaterEqual(timing["original_collection_seconds"], 0)

    def test_live_collection_bootstraps_offline_case_from_its_own_cancun_snapshot(self):
        inputs = []
        def execute(command, **kwargs):
            argv = command[3:]
            if argv[0] == "filter":
                inputs.append(Path(argv[argv.index("--input") + 1]))
            with redirect_stdout(io.StringIO()) as output:
                returncode = hotels.main(argv)
            return SimpleNamespace(returncode=returncode, stdout=output.getvalue(), stderr="")
        with tempfile.TemporaryDirectory() as tmp, patch.object(ev, "ROOT", Path(tmp)), patch.object(ev, "SOURCES", Path(tmp) / "missing-originals"), \
                patch("hotel_tools.fetch_page", side_effect=provider), patch("evaluate_hotel_skill.subprocess.run", side_effect=execute):
            report = ev.live(selected=[1, 8])
            self.assertTrue(report["all_passed"], report)
            fresh = Path(report["cases"][0]["commands"][0]["artifact"])
            self.assertEqual(inputs[0], fresh)
            self.assertTrue(fresh.exists())

    def test_explicit_trace_counters_do_not_leak_transcript(self):
        events = [
            {"timestamp": "2027-01-01T00:00:00Z", "payload": {"type": "function_call", "namespace": "collaboration", "name": "send_message", "arguments": "private text"}},
            {"timestamp": "2027-01-01T00:00:01Z", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 50, "output_tokens": 5}}}},
            {"timestamp": "2027-01-01T00:00:02Z", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 10}}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "own-eval.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in events))
            report = ev.trace_metrics(path)
        self.assertEqual(report["model_token_usage"]["input_tokens"], 100)
        self.assertEqual(report["tool_calls_including_coordination"], 1)
        self.assertEqual(report["coordination_calls"], 1)
        self.assertEqual(report["elapsed_first_to_last_timestamp_seconds"], 2)
        self.assertNotIn("private text", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
