"""Tests for dates-to-remember-mcp-server: recurrence math, storage, security,
all tools, and a real stdio protocol round-trip. Fully offline: dates are
injected, so nothing depends on the wall clock except pure round-trips.

Run with: python3 -m unittest -v test_server
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import server as srv


def make_store(tmp, data_file=srv.DEFAULT_DATA_FILE):
    return srv.Store(Path(tmp), data_file)


class TestRecurrenceMath(unittest.TestCase):
    """Pure date math with injected 'today' — no mocks, no sleeps."""

    def test_days_remaining_basic(self):
        t = date(2026, 10, 26)
        self.assertEqual(srv.days_remaining(10, 27, t), 1)
        self.assertEqual(srv.days_remaining(10, 25, t), 364)  # next year
        self.assertEqual(srv.days_remaining(10, 26, t), 0)  # today counts as 0

    def test_year_boundary_rolls_forward(self):
        t = date(2026, 12, 31)
        self.assertEqual(srv.days_remaining(1, 1, t), 1)
        self.assertEqual(srv.next_occurrence(12, 30, t), date(2027, 12, 30))

    def test_feb29_uses_feb28_in_non_leap_years(self):
        t = date(2026, 1, 1)
        self.assertEqual(srv.next_occurrence(2, 29, t), date(2026, 2, 28))
        self.assertEqual(srv.next_occurrence(2, 29, date(2027, 3, 1)), date(2028, 2, 29))

    def test_feb29_exact_in_leap_years(self):
        t = date(2028, 1, 1)
        self.assertEqual(srv.next_occurrence(2, 29, t), date(2028, 2, 29))

    def test_invalid_month_and_day_rejected(self):
        t = date(2026, 10, 26)
        with self.assertRaises(srv.DatesError):
            srv.next_occurrence(13, 1, t)
        with self.assertRaises(srv.DatesError):
            srv.next_occurrence(1, 0, t)
        with self.assertRaises(srv.DatesError):
            srv.next_occurrence(1, 32, t)

    def test_year_roll_forward(self):
        self.assertEqual(srv.next_occurrence(1, 1, date(2026, 12, 5)), date(2027, 1, 1))

    def test_status_labels(self):
        self.assertEqual(srv.event_status(0, 21), "today")
        self.assertEqual(srv.event_status(21, 21), "nudge")
        self.assertEqual(srv.event_status(22, 21), "later")


class TestLeadTimes(unittest.TestCase):
    """Lead-time reminders: default is nudge-me-three-weeks (21 days)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.today = date(2026, 10, 26)
        self.store = srv.Store(Path(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_default_lead_is_21_days(self):
        srv.call_tool(
            self.store,
            "add_person",
            {"name": "Car renewal", "renewals": [{"type": "car", "month": 11, "day": 12}]},
            self.today,
        )
        doc = self.store.load()
        self.assertEqual(doc["people"][0]["renewals"][0]["lead_days"], 21)

    def test_nudge_window_uses_per_event_lead(self):
        srv.call_tool(
            self.store,
            "add_person",
            {
                "name": "Insurance renewal",
                "renewals": [
                    {"type": "insurance", "month": 11, "day": 10, "lead_days": 30}
                ],
            },
            self.today,
        )
        result = srv.call_tool(self.store, "upcoming", {"within_days": 30}, self.today)
        self.assertEqual(result["items"][0]["days_remaining"], 15)
        self.assertEqual(result["items"][0]["status"], "nudge")
        self.assertEqual(result["nudges"], ["Insurance renewal: insurance in 15 day(s)"])

    def test_nudge_silent_outside_window(self):
        srv.call_tool(
            self.store,
            "add_person",
            {"name": "Warranty renewal", "renewals": [{"type": "warranty", "month": 12, "day": 25, "lead_days": 21}]},
            self.today,
        )
        result = srv.call_tool(self.store, "upcoming", {"within_days": 21}, self.today)
        self.assertEqual(result["count"], 0)  # 60 days away: no nudge yet


class TestPathSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_traversal_rejected(self):
        store = make_store(self.tmp)
        with self.assertRaises(srv.DatesError):
            store.set_data_file("../outside.json")
        with self.assertRaises(srv.DatesError):
            store.set_data_file("sub/../../outside.json")

    def test_absolute_and_windows_paths_rejected(self):
        store = make_store(self.tmp)
        for bad in ("/etc/passwd", "~/secrets.json", "C:\\x.json", "a\\b.json"):
            with self.assertRaises(srv.DatesError):
                store.set_data_file(bad)

    def test_null_bytes_rejected(self):
        store = make_store(self.tmp)
        with self.assertRaises(srv.DatesError):
            store.set_data_file("good.json\x00.json")

    def test_exotic_traversal_rejected(self):
        store = make_store(self.tmp)
        for bad in (".//..//elsewhere.json", "a/../b/../../../c.json", "sub/../../outside.json"):
            with self.assertRaises(srv.DatesError):
                store.set_data_file(bad)

    def test_safe_stays_inside_accepted(self):
        store = make_store(self.tmp)
        for good in ("people.json", "a/b/c/people.json", "x/./y.json", "./list.json"):
            store.set_data_file(good)  # must not raise

    def test_subdirectory_files_stay_inside(self):
        store = make_store(self.tmp)
        store.set_data_file("a/b/c/people.json")
        store.save({"version": 1, "people": []})
        self.assertTrue((self.base / "a" / "b" / "c" / "people.json").exists())
        self.assertEqual(store.load()["people"], [])

    def test_tool_call_rejects_dangerous_path(self):
        with self.assertRaises(srv.DatesError):
            srv.call_tool(make_store(self.tmp), "list_people", {"path": "../e.json"})


class TestStorageBackups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_backup_written_on_overwrite(self):
        store = make_store(self.tmp)
        store.save({"version": 1, "people": [{"name": "A"}]})
        store.save({"version": 1, "people": [{"name": "B"}]})
        backups = list((Path(self.tmp) / srv.BACKUP_DIR_NAME).glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())["people"][0]["name"], "A")
        self.assertEqual(json.loads(store.path.read_text())["people"][0]["name"], "B")

    def test_backups_pruned_to_cap(self):
        store = make_store(self.tmp)
        for i in range(srv.MAX_BACKUPS + 5):
            store.save({"version": 1, "people": [{"n": i}]})
        backups = list((Path(self.tmp) / srv.BACKUP_DIR_NAME).glob("*.json"))
        self.assertLessEqual(len(backups), srv.MAX_BACKUPS)

    def test_invalid_document_refused_on_save(self):
        store = make_store(self.tmp)
        with self.assertRaises(srv.DatesError):
            store.save({"people": "not-a-list"})

    def test_load_of_corrupt_file_raises(self):
        store = make_store(self.tmp)
        store.path.write_text("{ not json")
        with self.assertRaises(srv.DatesError):
            store.load()


class TestPersonValidation(unittest.TestCase):
    def test_name_required_and_clean(self):
        with self.assertRaises(srv.DatesError):
            srv.clean_person({"name": "   "})
        with self.assertRaises(srv.DatesError):
            srv.clean_person({"name": "Bad\x01name"})
        self.assertEqual(srv.clean_person({"name": "  Mom  "})["name"], "Mom")

    def test_birthday_bounds(self):
        with self.assertRaises(srv.DatesError):
            srv.clean_person({"name": "X", "birthday": {"month": 0, "day": 5}})
        with self.assertRaises(srv.DatesError):
            srv.clean_person({"name": "X", "birthday": {"month": 2, "day": 30}})
        with self.assertRaises(srv.DatesError):
            srv.clean_person({"name": "X", "birthday": {"month": 4, "day": 31}})
        record = srv.clean_person({"name": "X", "birthday": {"month": 2, "day": 29}})
        self.assertEqual(record["birthday"], {"month": 2, "day": 29})

    def test_renewal_type_allowed(self):
        record = srv.clean_person(
            {"name": "X", "renewals": [{"type": "car", "month": 3, "day": 1}]}
        )
        self.assertEqual(record["renewals"][0]["type"], "car")
        with self.assertRaises(srv.DatesError):
            srv.clean_person(
                {"name": "X", "renewals": [{"type": "car/../../x", "month": 3, "day": 1}]}
            )

    def test_lead_days_bounds(self):
        record = srv.clean_person(
            {"name": "X", "renewals": [{"type": "car", "month": 3, "day": 1, "lead_days": 0}]}
        )
        self.assertEqual(record["renewals"][0]["lead_days"], 0)
        with self.assertRaises(srv.DatesError):
            srv.clean_person(
                {"name": "X", "renewals": [{"type": "car", "month": 3, "day": 1, "lead_days": 999}]}
            )

    def test_duplicate_names_rejected_case_insensitive(self):
        self.assertEqual(srv.find_index([{"name": "Mom"}], "  MOM  "), 0)
        self.assertEqual(srv.find_index([{"name": "Mom"}], "Mum"), -1)
        with self.assertRaises(srv.DatesError):
            srv.find_index([{"name": "Mom"}], {"bad": "type"})

    def test_add_requires_a_date(self):
        tmp = tempfile.mkdtemp()
        try:
            with self.assertRaises(srv.DatesError):
                srv.call_tool(srv.Store(Path(tmp)), "add_person", {"name": "Nobody"})
        finally:
            shutil.rmtree(tmp)


def seed(tmp):
    today = date(2026, 10, 26)
    store = srv.Store(Path(tmp))
    srv.call_tool(
        store,
        "add_person",
        {
            "name": "Dad",
            "birthday": {"month": 11, "day": 2},
            "address": "106 Birchwood Ln, Awenda, IN",
            "renewals": [
                {"type": "car", "label": "Car registration", "month": 11, "day": 5}
            ],
        },
        today,
    )
    srv.call_tool(
        store,
        "add_person",
        {"name": "Leap-year cousin Riley", "birthday": {"month": 2, "day": 29}},
        today,
    )
    return store, today


class TestTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, self.today = seed(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_list_people_sorted_and_counted(self):
        result = srv.call_tool(self.store, "list_people", {}, self.today)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["people"][0]["name"], "Dad")
        self.assertTrue(all(isinstance(e["days_remaining"], int) for e in result["people"][0]["events"]))
        self.assertTrue(any(e["status"] == "nudge" for e in result["people"][0]["events"]))

    def test_days_remaining_query(self):
        result = srv.call_tool(self.store, "days_remaining", {"name": "dad"}, self.today)
        self.assertEqual(result["name"], "Dad")
        self.assertEqual(result["events"][0]["kind"], "birthday")
        self.assertEqual(result["events"][0]["days_remaining"], 7)

    def test_days_remaining_unknown_person(self):
        with self.assertRaises(srv.DatesError):
            srv.call_tool(self.store, "days_remaining", {"name": "Ghost"}, self.today)

    def test_nudge_shows_up_inside_window(self):
        result = srv.call_tool(self.store, "upcoming", {}, self.today)
        self.assertGreaterEqual(result["count"], 2)
        self.assertEqual(result["items"][0]["days_remaining"], 7)  # birthday Nov 2
        self.assertEqual(result["items"][1]["days_remaining"], 10)  # car Nov 5
        self.assertEqual(result["items"][0]["kind"], "birthday")
        self.assertTrue(any("7 day" in n for n in result["nudges"]))

    def test_birthday_card_batch_includes_address_and_text(self):
        result = srv.call_tool(
            self.store, "birthday_card_batch", {"within_days": 10}, self.today
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["cards"][0]["name"], "Dad")
        self.assertIn("Birchwood", result["cards"][0]["address"])
        self.assertIn("- Dad", result["batch_text"])

    def test_birthday_card_batch_leap_day_via_feb28(self):
        result = srv.call_tool(
            self.store,
            "birthday_card_batch",
            {"within_days": 130},
            date(2026, 1, 1),
        )
        self.assertEqual(result["cards"][0]["birthday"], "2026-02-28")

    def test_remove_person(self):
        result = srv.call_tool(self.store, "remove_person", {"name": "DAD"}, self.today)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(
            srv.call_tool(self.store, "list_people", {}, self.today)["count"], 1
        )

    def test_remove_unknown(self):
        with self.assertRaises(srv.DatesError):
            srv.call_tool(self.store, "remove_person", {"name": "Nobody"}, self.today)

    def test_alternate_data_file_via_tool(self):
        doc = {
            "version": 1,
            "people": [
                {"name": "Seeded", "birthday": {"month": 12, "day": 1}, "renewals": []}
            ],
        }
        (Path(self.tmp) / "season.json").write_text(json.dumps(doc))
        result = srv.call_tool(
            self.store, "list_people", {"path": "season.json"}, self.today
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["people"][0]["name"], "Seeded")


class TestToolArgBounds(unittest.TestCase):
    def test_month_day_range_rejected(self):
        tmp = tempfile.mkdtemp()
        try:
            store = srv.Store(Path(tmp))
            for bad in ({"month": 0, "day": 10}, {"month": 13, "day": 10}, {"month": 5, "day": 40}):
                with self.assertRaises(srv.DatesError):
                    srv.call_tool(
                        store,
                        "add_person",
                        {"name": "Zoomer", "birthday": bad},
                    )
        finally:
            shutil.rmtree(tmp)

    def test_within_days_bounds(self):
        tmp = tempfile.mkdtemp()
        try:
            store = srv.Store(Path(tmp))
            with self.assertRaises(srv.DatesError):
                srv.call_tool(store, "upcoming", {"within_days": -1})
            with self.assertRaises(srv.DatesError):
                srv.call_tool(store, "birthday_card_batch", {"within_days": 400})
        finally:
            shutil.rmtree(tmp)


class TestRpc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.server = srv.McpServer(srv.Store(Path(self.tmp)))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_initialize(self):
        reply = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(reply["result"]["protocolVersion"], srv.PROTOCOL_VERSION)
        self.assertIn("tools", reply["result"]["capabilities"])
        self.assertEqual(reply["result"]["serverInfo"]["name"], srv.SERVER_NAME)

    def test_tools_list(self):
        reply = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in reply["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "add_person",
                "list_people",
                "days_remaining",
                "upcoming",
                "birthday_card_batch",
                "remove_person",
            },
        )

    def test_full_workflow(self):
        today = date(2026, 10, 26)
        ok = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "add_person",
                    "arguments": {
                        "name": "Mom",
                        "birthday": {"month": 12, "day": 2},
                        "address": "106 Birchwood Ln",
                    },
                },
            }
        )
        self.assertIn("added", ok["result"]["content"][0]["text"])
        query = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "days_remaining", "arguments": {"name": "Mom"}},
            }
        )
        payload = json.loads(query["result"]["content"][0]["text"])
        self.assertEqual(payload["events"][0]["days_remaining"], 37)  # Oct 26 → Dec 2

    def test_unknown_method(self):
        reply = self.server.handle({"jsonrpc": "2.0", "id": 9, "method": "no/such"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_tool_error_is_error_result_not_crash(self):
        reply = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "days_remaining", "arguments": {"name": "Ghost"}},
            }
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("no person", reply["result"]["content"][0]["text"])

    def test_decode_rejects_json_and_lsp_framing(self):
        with self.assertRaises(srv.LspFramingError):
            srv.decode_message("Content-Length: 22\r\n\r\n{\"jsonrpc\":\"2.0\"}")
        with self.assertRaises(json.JSONDecodeError):
            srv.decode_message("nope")


class TestStdioSmoke(unittest.TestCase):
    """Spawn server.py over real stdio and run a protocol round-trip."""

    def test_full_roundtrip(self):
        data_dir = Path(tempfile.mkdtemp())
        try:
            shutil.copy(
                Path(__file__).parent / "samples" / "sample_data.json",
                data_dir / "people.json",
            )
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).parent / "server.py"), str(data_dir)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
            initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            batch = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "birthday_card_batch", "arguments": {"within_days": 130}},
            }
            out, err = proc.communicate(
                "\n".join([json.dumps(init), json.dumps(initialized), json.dumps(batch)]) + "\n",
                timeout=15,
            )
            responses = [json.loads(line) for line in out.splitlines() if line.strip()]
            self.assertEqual(len(responses), 2)
            self.assertEqual(
                responses[0]["result"]["serverInfo"]["name"],
                "dates-to-remember-mcp-server",
            )
            payload = json.loads(responses[1]["result"]["content"][0]["text"])
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                {c["name"] for c in payload["cards"]},
                {"Mom", "Leap-year cousin Riley"},
            )
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
