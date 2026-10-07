import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OccupancyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "occ.db"), RuleEngine()
        )
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("disp1", "dispatcher")
        self.maint = Actor("maint1", "maintenance")
        self.shaft = self.service.create(self.admin, "shaft", {
            "shaft_code": "SHAFT-A", "location": "Tower A/1#",
        })
        self.equipment = self.service.create(self.admin, "equipment", {
            "asset_no": "E-1", "equipment_type": "elevator", "location": "Tower A",
            "inspection_interval_days": 365, "shaft_id": self.shaft["id"],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def permit(self, team="Alpha", actor=None):
        return self.service.create(actor or self.admin, "work_permit", {
            "shaft_id": self.shaft["id"], "team": team,
            "work_type": "routine", "planned_at": "2026-10-07T09:00:00Z",
        })

    def issue_and_enter(self, team="Alpha", actor=None):
        permit = self.permit(team, actor)
        permit = self.service.transition(self.dispatcher, permit["id"], "issue", {})
        permit = self.service.transition(self.maint, permit["id"], "activate", {})
        task = self.service.create(self.maint, "maintenance", {
            "shaft_id": self.shaft["id"], "equipment_id": self.equipment["id"],
            "work_type": "routine", "team": team, "planned_at": "2026-10-07T09:00:00Z",
        })
        task = self.service.transition(self.maint, task["id"], "start", {})
        return permit, task

    def test_only_one_active_permit_per_shaft(self):
        permit_a = self.permit("Alpha")
        permit_a = self.service.transition(self.dispatcher, permit_a["id"], "issue", {})
        permit_b = self.permit("Bravo")
        # 第二张许可仍可申请，但发放被互斥规则拒绝
        with self.assertRaises(ConflictError):
            self.service.transition(self.dispatcher, permit_b["id"], "issue", {})

    def test_alarm_voids_permit_and_moves_task_to_review(self):
        permit, task = self.issue_and_enter("Alpha")
        alarm = self.service.create(self.dispatcher, "alarm", {
            "equipment_id": self.equipment["id"], "code": "TRAP",
            "occurred_at": "2026-10-07T10:00:00Z",
        })
        permit = self.service.get(permit["id"])
        task = self.service.get(task["id"])
        self.assertEqual(permit["status"], "rescue_stop")
        self.assertEqual(task["status"], "review_pending")

        account = self.service.shaft_account(self.shaft["id"])
        self.assertEqual(account["state"]["state"], "rescue")
        self.assertIn("permit_rescue_stop", [e["event"] for e in account["events"]])
        self.assertIn("task_needs_review", [e["event"] for e in account["events"]])

        # 旧许可状态变了即失效：救援结束后同井道可以发新许可
        self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "Bravo"})
        rescue = self.service.create(self.dispatcher, "rescue_job", {
            "alarm_id": alarm["id"], "dedupe_key": "r-1", "team": "Bravo",
        })
        self.service.transition(self.dispatcher, rescue["id"], "arrive", {})
        self.service.transition(self.dispatcher, rescue["id"], "complete", {"outcome": "freed"})
        self.service.transition(self.dispatcher, alarm["id"], "resolve", {"resolution": "safe"})
        self.service.transition(self.dispatcher, alarm["id"], "close", {})

        new_permit = self.permit("Bravo")
        new_permit = self.service.transition(self.dispatcher, new_permit["id"], "issue", {})
        self.assertEqual(new_permit["status"], "issued")

    def test_reviewed_evacuation_completes_task_and_frees_shaft(self):
        permit, task = self.issue_and_enter("Alpha")
        self.service.create(self.dispatcher, "alarm", {
            "equipment_id": self.equipment["id"], "code": "TRAP",
            "occurred_at": "2026-10-07T10:00:00Z",
        })
        # 没确认撤离 -> 待复核；复核确认撤离后才能完成
        task = self.service.get(task["id"])
        self.assertEqual(task["status"], "review_pending")
        task = self.service.transition(self.dispatcher, task["id"], "review_evacuated",
                                       {"reviewed_by": "disp1"})
        self.assertEqual(task["status"], "evacuated")
        task = self.service.transition(self.maint, task["id"], "complete",
                                       {"completed_at": "2026-10-07T11:00:00Z"})
        self.assertEqual(task["status"], "completed")

    def test_maintenance_requires_valid_permit(self):
        task = self.service.create(self.maint, "maintenance", {
            "shaft_id": self.shaft["id"], "equipment_id": self.equipment["id"],
            "work_type": "routine", "team": "Alpha", "planned_at": "2026-10-07T09:00:00Z",
        })
        with self.assertRaises(ConflictError):
            self.service.transition(self.maint, task["id"], "start", {})


if __name__ == "__main__":
    unittest.main()
