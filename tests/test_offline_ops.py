import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OperationQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "ops.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatch = Actor("disp1", "dispatcher")
        self.maint = Actor("maint1", "maintenance")
        self.shaft = self.service.create(self.admin, "shaft", {
            "shaft_code": "S1", "location": "B1",
        })
        self.equipment = self.service.create(self.admin, "equipment", {
            "asset_no": "E1", "equipment_type": "elevator", "location": "B1",
            "inspection_interval_days": 365, "shaft_id": self.shaft["id"],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_keeps_confirmed_steps_and_retries_rest(self):
        # 第1步成功（创建许可请求），第2步会失败（无效动作）
        permit_id = "wp-fixed-1"
        batch = {
            "steps": [
                {"type": "create", "kind": "work_permit", "entity_id": permit_id,
                 "data": {"shaft_id": self.shaft["id"], "team": "Alpha",
                          "work_type": "routine", "planned_at": "2026-10-07T09:00:00Z"}},
                {"type": "transition", "entity_id": permit_id, "action": "no_such_action",
                 "data": {}},
            ]
        }
        op = self.service.submit_operation(self.admin, "batch", batch, op_id="op-1")
        self.assertEqual(op["status"], "failed")
        statuses = [s["status"] for s in op["steps"]]
        self.assertEqual(statuses, ["confirmed", "failed"])
        # 失败后第1步的结果仍在
        self.assertTrue(self.service.get(permit_id))

        # 修正第2步后重试：已确认步骤不重放，未完成项继续
        fixed_steps = [
            batch["steps"][0],
            {"type": "transition", "entity_id": permit_id, "action": "issue", "data": {}},
        ]
        self.service.submit_operation(self.dispatch, "batch", {"steps": fixed_steps}, op_id="op-1")
        op = self.service.retry_operation("op-1")
        self.assertEqual(op["status"], "completed")
        self.assertTrue(all(s["status"] == "confirmed" for s in op["steps"]))
        self.assertEqual(self.service.get(permit_id)["status"], "issued")

    def test_restart_resumes_unfinished_operations(self):
        op = self.service.submit_operation(self.admin, "create", {
            "kind": "shaft", "data": {"shaft_code": "S2", "location": "B2"},
        }, op_id="op-pending")
        self.assertEqual(op["status"], "completed")
        created_id = op["steps"][0]["result"]["entity_id"]
        self.assertTrue(self.service.get(created_id))

        # 模拟重启：新建 service 指向同一个 DB，retry_pending 不应重复执行已完成项
        service2 = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        processed = service2.retry_pending()
        self.assertEqual(processed, [])

    def test_failed_operation_is_resumable_after_external_fix(self):
        # 维保任务无许可启动 -> 失败；补上许可后重试同一条操作
        task = self.service.create(self.maint, "maintenance", {
            "shaft_id": self.shaft["id"], "equipment_id": self.equipment["id"],
            "work_type": "routine", "team": "Alpha", "planned_at": "2026-10-07T09:00:00Z",
        })
        op = self.service.submit_operation(self.maint, "transition", {
            "entity_id": task["id"], "action": "start", "data": {},
        }, op_id="op-start")
        self.assertEqual(op["status"], "failed")

        permit = self.service.create(self.admin, "work_permit", {
            "shaft_id": self.shaft["id"], "team": "Alpha", "work_type": "routine",
            "planned_at": "2026-10-07T09:00:00Z",
        })
        permit = self.service.transition(self.dispatch, permit["id"], "issue", {})
        permit = self.service.transition(self.maint, permit["id"], "activate", {})

        op = self.service.retry_operation("op-start")
        self.assertEqual(op["status"], "completed")
        self.assertEqual(self.service.get(task["id"])["status"], "in_progress")


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "off.db"), RuleEngine()
        )
        self.admin = Actor("admin", "admin")
        self.dispatch = Actor("disp1", "dispatcher")
        self.maint = Actor("maint1", "maintenance")
        self.shaft = self.service.create(self.admin, "shaft", {
            "shaft_code": "S1", "location": "B1",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self, seq, request=None, end_seq=None, extra=None):
        record = {
            "source_id": "team-alpha-tablet-7",
            "shift_id": "2026-10-07-day",
            "seq": seq,
            "captured_at": "2026-10-07T%02d:00:00Z" % (8 + seq),
        }
        if request is not None:
            record["request"] = request
        if end_seq is not None:
            record["shift_end_seq"] = end_seq
        if extra:
            record.update(extra)
        return record

    def test_gap_detection_then_backfill_replays_in_order(self):
        # 序号2漏传，1、3先回网
        result = self.service.merge_offline(self.maint, [
            self._record(1, {"type": "create", "kind": "work_permit",
                             "entity_id": "wp-off-1",
                             "data": {"shaft_id": self.shaft["id"], "team": "Alpha",
                                      "work_type": "routine",
                                      "planned_at": "2026-10-07T09:00:00Z"}}),
            self._record(3, {"type": "log"}, end_seq=3),
        ])
        shift = result["shifts"][0]
        self.assertEqual(shift["missing"], [2])
        self.assertEqual(shift["status"], "gap")
        self.assertEqual(self.service.get_operation(shift["operation_id"])["status"], "blocked")

        # 漏传补回：操作继续，许可创建成功
        result = self.service.merge_offline(self.maint, [
            self._record(2, {"type": "log"}, end_seq=3),
        ])
        shift = result["shifts"][0]
        self.assertEqual(shift["missing"], [])
        self.assertEqual(shift["status"], "complete")
        op = self.service.get_operation(shift["operation_id"])
        self.assertEqual(op["status"], "completed")
        self.assertEqual(self.service.get("wp-off-1")["status"], "requested")

    def test_duplicate_resends_are_deduped_content_conflicts_flagged(self):
        records = [
            self._record(1, {"type": "log"}),
            self._record(1, {"type": "log"}),  # 完全重复，去重
            self._record(2, {"type": "log"}, extra={"note": "original"}),
            self._record(2, {"type": "log"}, extra={"note": "tampered"}),  # 内容冲突
        ]
        result = self.service.merge_offline(self.maint, records)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["duplicates"], 2)
        self.assertEqual(len(result["content_conflicts"]), 1)
        self.assertEqual(result["shifts"][0]["status"], "conflict")

        # 重放幂等：再来一遍相同批次不产生新实体
        again = self.service.merge_offline(self.maint, [
            self._record(1, {"type": "log"}),
        ])
        self.assertEqual(again["inserted"], 0)

    def test_separate_shifts_merge_independently(self):
        result = self.service.merge_offline(self.maint, [
            self._record(1, {"type": "log"}, extra={"shift_id": "2026-10-07-day"}),
            {"source_id": "team-alpha-tablet-7", "shift_id": "2026-10-07-night",
             "seq": 1, "request": {"type": "log"}},
        ])
        sources = {(s["source_id"], s["shift_id"]) for s in result["shifts"]}
        self.assertEqual(len(sources), 2)
        shifts = self.service.offline_shifts()
        self.assertEqual(len(shifts), 2)


if __name__ == "__main__":
    unittest.main()
