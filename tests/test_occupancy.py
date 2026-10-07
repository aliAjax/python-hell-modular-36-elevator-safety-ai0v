import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OccupancyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("disp", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1"):
        return self.service.create(self.admin, "equipment", {
            "asset_no": asset_no, "equipment_type": "elevator", "location": "A",
            "inspection_interval_days": 365,
        })

    def hoistway(self, name="HW-1", equipment=None):
        equipment = equipment or self.equipment()
        return self.service.create(self.admin, "hoistway", {"name": name, "equipment_id": equipment["id"]})

    def issue(self, hoistway, team="Team-A", purpose="maintenance", shift="d1", seq=1):
        return self.service.issue_entry_permit(self.admin, hoistway["id"], team, purpose, shift, seq)

    def start_task(self, equipment):
        task = self.service.create(self.admin, "maintenance", {
            "equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-10-07",
        })
        return self.service.transition(self.admin, task["id"], "start", {})

    def close_alarm(self, alarm):
        alarm = self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "Team-B"})
        job = self.service.create(self.dispatcher, "rescue_job", {
            "alarm_id": alarm["id"], "dedupe_key": "job-%s" % alarm["id"], "team": "Team-B",
        })
        self.service.transition(self.dispatcher, job["id"], "arrive", {})
        self.service.transition(self.dispatcher, job["id"], "complete", {"outcome": "freed"})
        alarm = self.service.transition(self.dispatcher, alarm["id"], "resolve", {"resolution": "ok"})
        return self.service.transition(self.dispatcher, alarm["id"], "close", {})

    def test_hoistway_requires_unique_name(self):
        self.hoistway("Shaft-1")
        with self.assertRaises(ConflictError):
            self.hoistway("Shaft-1")

    def test_issue_permit_occupies_hoistway(self):
        hoistway = self.hoistway()
        result = self.issue(hoistway)
        self.assertEqual(result["permit"]["status"], "active")
        self.assertEqual(result["hoistway"]["status"], "occupied")
        ledger = self.service.occupancy_ledger(hoistway["id"])
        self.assertIsNotNone(ledger["active_permit"])
        self.assertEqual(ledger["active_permit"]["id"], result["permit"]["id"])

    def test_only_one_active_permit_per_hoistway(self):
        hoistway = self.hoistway()
        self.issue(hoistway, shift="d1", seq=1)
        self.issue(hoistway, team="Team-B", shift="d1", seq=2)
        ledger = self.service.occupancy_ledger(hoistway["id"])
        self.assertEqual(ledger["active_permit"]["data"]["team"], "Team-B")
        statuses = [p["status"] for p in ledger["permits"]]
        self.assertIn("invalidated", statuses)
        self.assertEqual(sum(1 for s in statuses if s == "active"), 1)

    def test_cannot_issue_permit_while_alarm_active(self):
        hoistway = self.hoistway()
        self.issue(hoistway)
        self.service.report_alarm(self.admin, hoistway["id"], "DOOR-JAM", "2026-10-07T10:00:00Z")
        with self.assertRaises(ConflictError):
            self.issue(hoistway, team="Team-C", shift="d1", seq=2)

    def test_report_alarm_invalidates_permit_and_hoistway(self):
        hoistway = self.hoistway()
        self.issue(hoistway)
        result = self.service.report_alarm(self.admin, hoistway["id"], "DOOR-JAM", "2026-10-07T10:00:00Z")
        self.assertEqual(result["alarm"]["status"], "received")
        self.assertEqual(result["hoistway"]["status"], "alarm")
        ledger = self.service.occupancy_ledger(hoistway["id"])
        self.assertIsNone(ledger["active_permit"])
        self.assertEqual(ledger["permits"][0]["status"], "invalidated")

    def test_unevacuated_tasks_go_pending_review_on_alarm(self):
        equipment = self.equipment()
        hoistway = self.hoistway(equipment=equipment)
        self.issue(hoistway)
        task = self.start_task(equipment)
        self.assertEqual(task["status"], "in_progress")
        self.service.report_alarm(self.admin, hoistway["id"], "DOOR-JAM", "2026-10-07T10:00:00Z")
        ledger = self.service.occupancy_ledger(hoistway["id"])
        self.assertEqual(len(ledger["maintenance"]["pending_review"]), 1)
        self.assertEqual(ledger["maintenance"]["pending_review"][0]["id"], task["id"])
        self.assertFalse(ledger["can_release"])

    def test_evacuated_tasks_do_not_block_release(self):
        equipment = self.equipment()
        hoistway = self.hoistway(equipment=equipment)
        self.issue(hoistway)
        task = self.start_task(equipment)
        self.service.report_alarm(self.admin, hoistway["id"], "DOOR-JAM", "2026-10-07T10:00:00Z")
        task = self.service.confirm_evacuation(self.admin, task["id"])
        self.assertTrue(task["data"]["evacuated"])
        self.assertEqual(task["status"], "in_progress")

    def test_release_blocked_until_alarm_closed_and_evacuated(self):
        equipment = self.equipment()
        hoistway = self.hoistway(equipment=equipment)
        self.issue(hoistway)
        task = self.start_task(equipment)
        alarm_result = self.service.report_alarm(self.admin, hoistway["id"], "DOOR-JAM", "2026-10-07T10:00:00Z")
        with self.assertRaises(ConflictError):
            self.service.release_hoistway(self.admin, hoistway["id"])
        self.service.confirm_evacuation(self.admin, task["id"])
        with self.assertRaises(ConflictError):
            self.service.release_hoistway(self.admin, hoistway["id"])
        self.close_alarm(alarm_result["alarm"])
        result = self.service.release_hoistway(self.admin, hoistway["id"])
        self.assertEqual(result["hoistway"]["status"], "free")
        ledger = self.service.occupancy_ledger(hoistway["id"])
        self.assertTrue(ledger["can_release"])

    def test_release_evacuates_active_permit(self):
        hoistway = self.hoistway()
        self.issue(hoistway)
        result = self.service.release_hoistway(self.admin, hoistway["id"])
        self.assertEqual(result["hoistway"]["status"], "free")
        ledger = self.service.occupancy_ledger(hoistway["id"])
        self.assertIsNone(ledger["active_permit"])
        self.assertEqual(ledger["permits"][0]["status"], "evacuated")

    def test_offline_merge_applies_dedupes_and_reports_gaps(self):
        records = [
            {"shift": "s1", "seq": 1, "payload": {"note": "a"}},
            {"shift": "s1", "seq": 2, "payload": {"note": "b"}},
            {"shift": "s1", "seq": 1, "payload": {"note": "a"}},
            {"shift": "s2", "seq": 2, "payload": {"note": "x"}},
        ]
        report = self.service.merge_offline(self.admin, records)
        self.assertEqual(len(report["applied"]), 3)
        self.assertEqual(len(report["duplicates"]), 1)
        self.assertEqual(report["gaps"], [{"shift": "s2", "missing": [1]}])

    def test_offline_merge_flags_conflicting_content(self):
        records = [
            {"shift": "s1", "seq": 1, "payload": {"note": "a"}},
            {"shift": "s1", "seq": 1, "payload": {"note": "different"}},
        ]
        report = self.service.merge_offline(self.admin, records)
        self.assertEqual(len(report["applied"]), 1)
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["conflicts"][0]["seq"], 1)

    def test_offline_merge_reapply_is_idempotent(self):
        records = [
            {"shift": "s1", "seq": 1, "payload": {"note": "a"}},
            {"shift": "s1", "seq": 2, "payload": {"note": "b"}},
        ]
        first = self.service.merge_offline(self.admin, records)
        second = self.service.merge_offline(self.admin, records)
        self.assertEqual(len(first["applied"]), 2)
        self.assertEqual(len(second["applied"]), 0)
        self.assertEqual(len(second["duplicates"]), 2)

    def test_outbox_preserves_completed_steps_and_recovers(self):
        counts = {"s1": 0, "s2": 0}

        def make_builder(fail_s2):
            def builder(kind, payload):
                def step1(actor, payload, ctx):
                    counts["s1"] += 1
                    return "s1"

                def step2(actor, payload, ctx):
                    counts["s2"] += 1
                    if fail_s2:
                        raise RuntimeError("simulated write failure")
                    return "s2"

                return [("step1", step1), ("step2", step2)]
            return builder

        with mock.patch.object(self.service, "_build_steps", make_builder(True)):
            with self.assertRaises(RuntimeError):
                self.service._run(self.admin, "op-x", "k", {})
        op = self.service.repository.get_operation("op-x")
        self.assertEqual(op["status"], "in_progress")
        self.assertEqual([s["status"] for s in op["steps"]], ["completed", "failed"])

        with mock.patch.object(self.service, "_build_steps", make_builder(False)):
            result = self.service.recover(self.admin)
        self.assertEqual(len(result["recovered"]), 1)
        op = self.service.repository.get_operation("op-x")
        self.assertEqual(op["status"], "completed")
        self.assertEqual(counts, {"s1": 1, "s2": 2})

    def test_recover_resumes_interrupted_permit_issue(self):
        hoistway = self.hoistway()
        real_build = self.service._build_steps

        def failing_build(kind, payload):
            steps = real_build(kind, payload)

            def grant_then_fail(actor, payload, ctx):
                raise RuntimeError("simulated write failure after permit created")

            # All steps present; grant_permit fails on the first run.
            return steps[:3] + [("grant_permit", grant_then_fail)] + steps[4:]

        with mock.patch.object(self.service, "_build_steps", failing_build):
            with self.assertRaises(RuntimeError):
                self.service.issue_entry_permit(self.admin, hoistway["id"], "Team-A", "maintenance", "d1", 1)
        op = self.service.repository.get_operation("issue:%s:d1:1" % hoistway["id"])
        self.assertEqual(op["status"], "in_progress")
        self.assertEqual([s["status"] for s in op["steps"]], ["completed", "completed", "completed", "failed"])
        self.assertEqual(op["attempts"], 1)

        self.service.recover(self.admin)
        op = self.service.repository.get_operation("issue:%s:d1:1" % hoistway["id"])
        self.assertEqual(op["status"], "completed")
        ledger = self.service.occupancy_ledger(hoistway["id"])
        self.assertIsNotNone(ledger["active_permit"])
        self.assertEqual(ledger["hoistway"]["status"], "occupied")


if __name__ == "__main__":
    unittest.main()
