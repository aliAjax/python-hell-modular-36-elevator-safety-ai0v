import hashlib
import json
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    DomainError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .repository import utcnow
from .rules import (
    ACTIVE_ALARM_STATUSES,
    PERMIT_OCCUPYING_STATUSES,
    RuleEngine,
)

# 操作中可重试的瞬时错误
TRANSIENT_ERRORS = (ConflictError, InvalidTransition)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    # ---------- lookups ----------

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _tx_lookup(self, connection, kind, field, value):
        kind = self.rules.normalize_kind(kind)
        rows = self.repository._rows(connection, kind=kind)
        if field == "*":
            return rows
        return [
            row
            for row in rows
            if (row["id"] == value if field == "id" else row["data"].get(field) == value)
        ]

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ---------- direct (synchronous) use cases ----------

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        with self.repository.transaction() as connection:
            lookup = lambda k, f, v: self._tx_lookup(connection, k, f, v)
            self.rules.validate_create(actor, kind, payload, lookup)
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            status = self.rules.initial_status(kind, payload)
            entity = self.repository.tx_create_entity(
                connection, entity_id, kind, status, payload, actor.user_id
            )
            self.repository.tx_audit(
                connection, entity_id, actor.user_id, actor.role, "create", None, status, {"kind": kind}
            )
            self._record_occupancy_for_event(connection, actor, kind, entity, None, status, payload)
            self._handle_shaft_side_effects(
                connection, actor, {"kind": kind, "status": None}, entity, "create",
                lookup,
            )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return self.repository.get_entity(entity_id)

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        with self.repository.transaction() as connection:
            result = self._apply_transition(
                connection, actor, entity_id, action, dict(data or {}), expected_version
            )
        return self.repository.get_entity(entity_id if isinstance(result, dict) else result["id"])

    def _apply_transition(self, connection, actor, entity_id, action, data, expected_version=None):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        entity = self.repository._entity_from_row(row)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if entity["version"] != expected:
            raise ConflictError(
                "version conflict: expected %s, found %s" % (expected, entity["version"])
            )
        lookup = lambda k, f, v: self._tx_lookup(connection, k, f, v)
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, data, lookup
        )
        updated = self.repository.tx_update_entity(connection, entity, next_status, patch)
        self.repository.tx_audit(
            connection, entity_id, actor.user_id, actor.role, action,
            entity["status"], next_status, {"patch": patch},
        )
        self._record_occupancy_for_event(
            connection, actor, entity["kind"], updated, entity["status"], next_status, patch
        )
        self._handle_shaft_side_effects(connection, actor, entity, updated, action, lookup)
        return updated

    # ---------- occupancy account ----------

    def _shaft_id_of(self, entity, lookup):
        shaft_id = entity.get("data", {}).get("shaft_id")
        if shaft_id:
            return shaft_id
        equipment_id = entity.get("data", {}).get("equipment_id")
        if equipment_id:
            equipment = self._find(lookup, "equipment", "id", equipment_id)
            if equipment:
                return equipment["data"].get("shaft_id")
        return None

    @staticmethod
    def _find(lookup, kind, field, value):
        rows = lookup(kind, field, value) or [] if lookup else []
        return rows[0] if rows else None

    def _record_occupancy_for_event(self, connection, actor, kind, entity, from_status, to_status, patch):
        lookup = lambda k, f, v: self._tx_lookup(connection, k, f, v)
        team = entity.get("data", {}).get("team")
        if kind == "shaft":
            self.repository.tx_occupancy(
                connection, entity["id"], "shaft:" + to_status, None, kind, entity["id"],
                {"from": from_status, "by": actor.user_id},
            )
        elif kind == "work_permit":
            shaft_id = entity["data"].get("shaft_id")
            if to_status == "requested":
                event = "permit_requested"
            elif to_status == "issued":
                event = "permit_issued"
            elif to_status == "active":
                event = "permit_activated"
            elif to_status == "evacuated":
                event = "permit_evacuation_confirmed"
            elif to_status == "rescue_stop":
                event = "permit_rescue_stop"
            elif to_status == "void":
                event = "permit_void"
            elif to_status == "closed":
                event = "permit_closed"
            else:
                event = "permit:" + to_status
            self.repository.tx_occupancy(
                connection, shaft_id, event, team, kind, entity["id"],
                {"from": from_status, "permit_id": entity["id"], "by": actor.user_id},
            )
        elif kind == "maintenance":
            shaft_id = self._shaft_id_of(entity, lookup)
            if to_status == "planned":
                event = "task_planned"
            elif to_status == "in_progress":
                event = "task_entered"
            elif to_status == "evacuated":
                event = "task_evacuation_confirmed"
            elif to_status == "review_pending":
                event = "task_needs_review"
            elif to_status == "completed":
                event = "task_completed"
            else:
                event = "task:" + to_status
            self.repository.tx_occupancy(
                connection, shaft_id, event, team, kind, entity["id"],
                {"from": from_status, "task_id": entity["id"], "by": actor.user_id},
            )
        elif kind == "alarm":
            shaft_id = self._shaft_id_of(entity, lookup)
            event = "alarm:" + to_status
            self.repository.tx_occupancy(
                connection, shaft_id, event, None, kind, entity["id"],
                {"from": from_status, "alarm_id": entity["id"], "by": actor.user_id},
            )
        elif kind == "rescue_job":
            alarm = self._find(lookup, "alarm", "id", entity["data"].get("alarm_id"))
            shaft_id = self._shaft_id_of(alarm, lookup) if alarm else None
            event = "rescue:" + to_status
            self.repository.tx_occupancy(
                connection, shaft_id, event, team, kind, entity["id"],
                {"from": from_status, "rescue_id": entity["id"], "by": actor.user_id},
            )

    def _handle_shaft_side_effects(self, connection, actor, before, after, action, lookup):
        """状态一变，旧许可失效；没确认撤离的任务转待复核。"""
        kind = after["kind"]
        shafts = set()
        alarm_active_now = False
        if kind == "alarm" and after["status"] in ACTIVE_ALARM_STATUSES and before["status"] not in ACTIVE_ALARM_STATUSES:
            shaft_id = self._shaft_id_of(after, lookup)
            if shaft_id:
                shafts.add(shaft_id)
                alarm_active_now = True
        if kind == "rescue_job" and after["status"] in ("dispatched", "on_site"):
            alarm = self._find(lookup, "alarm", "id", after["data"].get("alarm_id"))
            shaft_id = self._shaft_id_of(alarm, lookup) if alarm else None
            if shaft_id:
                shafts.add(shaft_id)
                alarm_active_now = True
        if kind == "work_permit" and after["status"] in PERMIT_OCCUPYING_STATUSES:
            shafts.add(after["data"].get("shaft_id"))
        if kind == "maintenance":
            shaft_id = self._shaft_id_of(after, lookup)
            if shaft_id:
                shafts.add(shaft_id)
        if not shafts:
            return
        for shaft_id in shafts:
            if alarm_active_now or _active_alarm(lookup, shaft_id):
                self._freeze_shaft_for_rescue(connection, actor, shaft_id, lookup, after)

    def _freeze_shaft_for_rescue(self, connection, actor, shaft_id, lookup, trigger):
        """同一井道有报警/救援进行：占用中的许可失效，在井道内的任务转待复核。"""
        now = utcnow()
        for permit in lookup("work_permit", "*", None):
            if permit["data"].get("shaft_id") != shaft_id:
                continue
            if permit["status"] in ("issued", "active"):
                updated = self.repository.tx_update_entity(
                    connection, permit, "rescue_stop",
                    {"rescue_stop_reason": "alarm or rescue became active: " + trigger["id"],
                     "rescue_stopped_at": now},
                    now=now,
                )
                self.repository.tx_audit(
                    connection, permit["id"], actor.user_id, actor.role,
                    "rescue_stop", permit["status"], "rescue_stop", {"auto": True}, now=now,
                )
                self.repository.tx_occupancy(
                    connection, shaft_id, "permit_rescue_stop",
                    permit["data"].get("team"), "work_permit", permit["id"],
                    {"auto": True, "trigger": trigger["id"]}, now=now,
                )
            elif permit["status"] == "requested":
                # 未发放的请求不能占用井道，记录一条拒绝占用账
                self.repository.tx_occupancy(
                    connection, shaft_id, "permit_blocked_by_rescue",
                    permit["data"].get("team"), "work_permit", permit["id"],
                    {"auto": True, "trigger": trigger["id"]}, now=now,
                )
        for task in lookup("maintenance", "*", None):
            if task["status"] != "in_progress":
                continue
            if self._shaft_id_of(task, lookup) != shaft_id:
                continue
            updated = self.repository.tx_update_entity(
                connection, task, "review_pending",
                {"review_reason": "shaft rescue active; evacuation not confirmed",
                 "review_trigger": trigger["id"], "marked_at": now},
                now=now,
            )
            self.repository.tx_audit(
                connection, task["id"], actor.user_id, actor.role,
                "mark_review", task["status"], "review_pending", {"auto": True}, now=now,
            )
            self.repository.tx_occupancy(
                connection, shaft_id, "task_needs_review",
                task["data"].get("team"), "maintenance", task["id"],
                {"auto": True, "trigger": trigger["id"]}, now=now,
            )

    def shaft_account(self, shaft_id):
        shaft = self.repository.get_entity(shaft_id)
        if not shaft or shaft["kind"] != "shaft":
            raise NotFoundError("shaft not found: " + shaft_id)
        events = self.repository.list_occupancy(shaft_id)
        permits = []
        tasks = []
        alarms = []
        for permit in self.repository.list_entities(kind="work_permit"):
            if permit["data"].get("shaft_id") == shaft_id:
                permits.append(permit)
        for task in self.repository.list_entities(kind="maintenance"):
            if task["data"].get("shaft_id") == shaft_id or self._shaft_id_of(task, self._lookup) == shaft_id:
                tasks.append(task)
        for alarm in self.repository.list_entities(kind="alarm"):
            if self._shaft_id_of(alarm, self._lookup) == shaft_id:
                alarms.append(alarm)
        return {
            "shaft": shaft,
            "state": self._derive_state(permits, tasks, alarms),
            "permits": permits,
            "maintenance_tasks": tasks,
            "alarms": alarms,
            "events": events,
        }

    @staticmethod
    def _derive_state(permits, tasks, alarms):
        active_alarms = [a for a in alarms if a["status"] in ACTIVE_ALARM_STATUSES]
        if active_alarms:
            return {"state": "rescue", "ref": active_alarms[0]["id"]}
        for permit in permits:
            if permit["status"] == "active":
                return {"state": "occupied", "permit_id": permit["id"], "team": permit["data"].get("team")}
        for task in tasks:
            if task["status"] in ("in_progress", "review_pending"):
                return {"state": "occupied", "task_id": task["id"], "team": task["data"].get("team")}
        for permit in permits:
            if permit["status"] == "issued":
                return {"state": "permit_issued", "permit_id": permit["id"], "team": permit["data"].get("team")}
        return {"state": "available"}

    # ---------- durable operation queue ----------

    def submit_operation(self, actor, op_type, payload, op_id=None, idempotency_key=None):
        op_id = op_id or str(uuid4())
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, "op:" + idempotency_key)
            if existing and self.repository.get_operation(existing):
                op_id = existing
            else:
                self.repository.save_idempotency(actor.user_id, "op:" + idempotency_key, op_id)
        plan = self._plan_steps(op_type, payload)
        existing = self.repository.get_operation(op_id)
        if existing:
            # 同键重试：保留已确认步骤，只补未完成项，然后立即重放
            for seq, step in enumerate(plan):
                self.repository.upsert_step(op_id, step["key"], seq, step["request"])
            if existing["status"] in ("failed", "blocked"):
                for step in self.repository.list_steps(op_id):
                    if step["status"] == "failed":
                        self.repository.set_step(op_id, step["step_key"], "pending", error=None)
                self.repository.update_operation(op_id, "pending", last_error=None)
            return self.run_operation(op_id)
        try:
            self.repository.create_operation(op_id, op_type, actor.user_id, actor.role, payload)
        except Exception:
            raise ConflictError("operation already exists: " + op_id)
        for seq, step in enumerate(plan):
            self.repository.upsert_step(op_id, step["key"], seq, step["request"])
        return self.run_operation(op_id)

    def _plan_steps(self, op_type, payload):
        if op_type == "batch":
            items = payload.get("steps") or payload.get("items")
            if not isinstance(items, list) or not items:
                raise ValidationError("batch operation requires a non-empty steps list")
            plan = []
            for index, item in enumerate(items):
                self._validate_step_request(item)
                plan.append({"key": "step-%04d" % index, "request": item})
            return plan
        request = self._request_for(op_type, payload)
        self._validate_step_request(request)
        return [{"key": "only", "request": request}]

    @staticmethod
    def _validate_step_request(request):
        if not isinstance(request, dict):
            raise ValidationError("step must be an object")
        if request.get("type") not in ("create", "transition", "log"):
            raise ValidationError("step type must be create, transition or log")
        if request["type"] == "create" and not request.get("kind"):
            raise ValidationError("create step requires kind")
        if request["type"] == "transition" and (not request.get("entity_id") or not request.get("action")):
            raise ValidationError("transition step requires entity_id and action")

    @staticmethod
    def _request_for(op_type, payload):
        if op_type == "create":
            data = dict(payload.get("data", payload))
            data.pop("id", None)
            request = {"type": "create", "kind": payload.get("kind"), "data": data}
            if payload.get("entity_id"):
                request["entity_id"] = payload["entity_id"]
            return request
        if op_type == "transition":
            return {
                "type": "transition",
                "entity_id": payload.get("entity_id"),
                "action": payload.get("action"),
                "data": payload.get("data", {}),
                "expected_version": payload.get("expected_version"),
            }
        raise ValidationError("unknown op_type: " + str(op_type))

    def run_operation(self, op_id):
        op = self.repository.get_operation(op_id)
        if not op:
            raise NotFoundError("operation not found: " + op_id)
        if op["status"] in ("completed",):
            return self.repository.operation_snapshot(op_id)
        actor = ActorView(op["actor_id"], op["actor_role"])
        steps = self.repository.list_steps(op_id)
        attempts = op["attempts"] + 1
        for step in steps:
            if step["status"] == "confirmed":
                continue
            if step["status"] == "blocked":
                # 缺序号导致的等待项：先不动，等漏传补齐
                self.repository.update_operation(op_id, "blocked", attempts=attempts,
                                                 last_error="waiting for missing offline sequence(s)")
                return self.repository.operation_snapshot(op_id)
            try:
                result = self._execute_step(actor, step["request"])
                self.repository.set_step(op_id, step["step_key"], "confirmed", result=result)
            except DomainError as exc:
                self.repository.set_step(op_id, step["step_key"], "failed", error=str(exc))
                self.repository.update_operation(op_id, "failed", attempts=attempts,
                                                 last_error=str(exc))
                return self.repository.operation_snapshot(op_id)
        self.repository.update_operation(op_id, "completed", attempts=attempts)
        return self.repository.operation_snapshot(op_id)

    def _execute_step(self, actor, request):
        step_type = request["type"]
        if step_type == "log":
            return {"logged": True, "at": utcnow()}
        if step_type == "create":
            data = dict(request.get("data", {}))
            entity_id = data.pop("id", None) or request.get("entity_id") or str(uuid4())
            kind = self.rules.normalize_kind(request["kind"])
            with self.repository.transaction() as connection:
                lookup = lambda k, f, v: self._tx_lookup(connection, k, f, v)
                self.rules.validate_create(actor, kind, data, lookup)
                if connection.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone():
                    raise ConflictError("entity already exists: " + entity_id)
                status = self.rules.initial_status(kind, data)
                entity = self.repository.tx_create_entity(
                    connection, entity_id, kind, status, data, actor.user_id
                )
                self.repository.tx_audit(
                    connection, entity_id, actor.user_id, actor.role, "create", None, status,
                    {"kind": kind, "op_step": True},
                )
                self._record_occupancy_for_event(connection, actor, kind, entity, None, status, data)
                self._handle_shaft_side_effects(connection, actor,
                                                {"kind": kind, "status": None}, entity, "create", lookup)
            return {"entity_id": entity_id, "status": status}
        if step_type == "transition":
            with self.repository.transaction() as connection:
                updated = self._apply_transition(
                    connection, actor, request["entity_id"], request["action"],
                    dict(request.get("data", {})), request.get("expected_version"),
                )
            return {"entity_id": updated["id"], "status": updated["status"], "version": updated["version"]}
        raise ValidationError("unknown step type: " + step_type)

    def retry_operation(self, op_id):
        op = self.repository.get_operation(op_id)
        if not op:
            raise NotFoundError("operation not found: " + op_id)
        # 失败项：重置未确认步骤，保留已确认步骤，只重试未完成项
        for step in self.repository.list_steps(op_id):
            if step["status"] == "failed":
                self.repository.set_step(op_id, step["step_key"], "pending", error=None)
        if op["status"] in ("failed", "blocked"):
            self.repository.update_operation(op_id, "pending", last_error=None)
        return self.run_operation(op_id)

    def retry_pending(self, max_runs=100):
        """重启接着处理：把未完成的操作逐个重放，直到没有进展。"""
        processed = []
        for _ in range(max_runs):
            pending = self.repository.list_operations(status="pending")
            blocked = self.repository.list_operations(status="blocked")
            candidates = pending + [op for op in blocked if self._blocked_gap_filled(op)]
            if not candidates:
                break
            progressed = False
            for op in candidates:
                before = self.repository.operation_snapshot(op["id"])
                after = self.run_operation(op["id"])
                processed.append(op["id"])
                if after["status"] != before["status"] or any(
                    s["status"] != b["status"] for s, b in zip(after["steps"], before["steps"])
                ):
                    progressed = True
            if not progressed:
                break
        return processed

    def _blocked_gap_filled(self, op):
        """被缺序号阻塞的操作，检查阻塞点前的缺口是否已补齐（无等待项则可继续）。"""
        return not any(step["status"] == "blocked" for step in self.repository.list_steps(op["id"]))

    def get_operation(self, op_id):
        snapshot = self.repository.operation_snapshot(op_id)
        if not snapshot:
            raise NotFoundError("operation not found: " + op_id)
        return snapshot

    def list_operations(self, status=None):
        ops = self.repository.list_operations(status=status)
        for op in ops:
            op["steps"] = self.repository.list_steps(op["id"])
        return ops

    # ---------- offline merge by (source, shift, seq) ----------

    def merge_offline(self, actor, records):
        """断网记录回网：按班组(source)和班次(shift)、序号(seq)合并。

        - 每条记录必须有 source_id / shift_id / seq（序号从1开始）。
        - 重复序号：内容一致视为重传，直接去重；内容不一致计入 duplicates 待核对。
        - 与 shift_end_seq 对照找出漏传序号，回补后再次调用即可重放被阻塞的步骤。
        - 记录里可带 request（create/transition/log），按序号顺序重放为操作队列步骤，
          已确认步骤不会重复执行。
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        normalized = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            shift_id = str(raw.get("shift_id", "")).strip()
            if not source_id or not shift_id:
                raise ValidationError("source_id and shift_id are required")
            try:
                seq = int(raw.get("seq"))
            except (TypeError, ValueError):
                raise ValidationError("seq must be an integer")
            if seq < 1:
                raise ValidationError("seq must start from 1")
            normalized.append((source_id, shift_id, seq, raw))

        touched_shifts = set()
        inserted_count = 0
        duplicate_count = 0
        conflicting = []
        for source_id, shift_id, seq, raw in normalized:
            record_id = "offline-" + hashlib.sha256(
                (source_id + "\0" + shift_id + "\0" + str(seq)).encode("utf-8")
            ).hexdigest()[:32]
            payload = dict(raw)
            inserted, stored = self.repository.insert_offline_record(
                record_id, source_id, shift_id, seq, payload
            )
            touched_shifts.add((source_id, shift_id))
            if inserted:
                inserted_count += 1
            else:
                duplicate_count += 1
                if _canonical(stored) != _canonical(payload):
                    conflicting.append({"source_id": source_id, "shift_id": shift_id, "seq": seq,
                                         "stored": stored, "resent": payload})

        # 回补的 shift_end_seq 声明（可选），用于定位漏传
        shift_ends = {}
        for source_id, shift_id, seq, raw in normalized:
            if "shift_end_seq" in raw and raw["shift_end_seq"] is not None:
                shift_ends[(source_id, shift_id)] = max(
                    shift_ends.get((source_id, shift_id), 0), int(raw["shift_end_seq"])
                )

        reports = []
        for source_id, shift_id in sorted(touched_shifts):
            rows = self.repository.list_offline_records(source_id, shift_id)
            seqs = sorted(row["seq"] for row in rows)
            max_seen = max(seqs) if seqs else 0
            end_seq = shift_ends.get((source_id, shift_id))
            upper = end_seq if end_seq is not None else max_seen
            have = set(seqs)
            missing = [n for n in range(1, upper + 1) if n not in have]
            beyond = sorted(n for n in seqs if end_seq is not None and n > end_seq)
            # 本次批次中同 shift 的重复序号数（内容冲突另计）
            batch_seqs = [s for src, sid, s, _ in normalized if src == source_id and sid == shift_id]
            batch_dup = len(batch_seqs) - len(set(batch_seqs))
            conflicts_here = [c for c in conflicting if c["source_id"] == source_id and c["shift_id"] == shift_id]
            if conflicts_here:
                status = "conflict"
            elif missing:
                status = "gap"
            elif beyond:
                status = "seq_beyond_end"
            else:
                status = "complete"
            self.repository.upsert_offline_shift(
                source_id, shift_id, max_seen, missing, batch_dup, status
            )
            report = {
                "source_id": source_id,
                "shift_id": shift_id,
                "received_max_seq": max_seen,
                "shift_end_seq": end_seq,
                "missing": missing,
                "batch_duplicates": batch_dup,
                "content_conflicts": conflicts_here,
                "status": status,
            }
            op_id = self._replay_shift(actor, source_id, shift_id, rows, missing)
            report["operation_id"] = op_id
            reports.append(report)

        return {
            "inserted": inserted_count,
            "duplicates": duplicate_count,
            "content_conflicts": conflicting,
            "shifts": reports,
        }

    def _replay_shift(self, actor, source_id, shift_id, rows, missing):
        """把班次内带 request 的记录按序号排成操作步骤；缺序号先阻塞，补齐后续跑。"""
        actionable = [(row["seq"], row["payload"].get("request"), row["payload"])
                      for row in rows if row["payload"].get("request")]
        if not actionable:
            # 纯观察记录：记一条确认账
            return None
        missing_set = set(missing)
        op_id = "shiftop-" + hashlib.sha256(
            (source_id + "\0" + shift_id).encode("utf-8")
        ).hexdigest()[:24]
        steps = []
        for seq, request, payload in actionable:
            steps.append((seq, dict(request)))
        existing = self.repository.get_operation(op_id)
        if not existing:
            self.repository.create_operation(op_id, "batch", actor.user_id, actor.role,
                                             {"source_id": source_id, "shift_id": shift_id})
        for seq, request in steps:
            # 该步骤之前还有漏传序号：先阻塞等待补齐
            blocked_before = any(n < seq for n in missing_set)
            self.repository.upsert_step(
                op_id, "seq-%06d" % seq, seq, request,
                status="blocked" if blocked_before else "pending",
            )
        # 全部缺口已补：放开残留的 blocked 步骤
        if not missing_set:
            for step in self.repository.list_steps(op_id):
                if step["status"] == "blocked":
                    self.repository.set_step(op_id, step["step_key"], "pending", error=None)
            self.repository.update_operation(op_id, "pending", last_error=None)
        snapshot = self.run_operation(op_id)
        still_blocked = any(n < max((s for s, _ in steps), default=0) for n in missing_set)
        if still_blocked and snapshot["status"] not in ("failed",):
            self.repository.update_operation(
                op_id, "blocked", last_error="waiting for missing seqs: "
                + ",".join(map(str, sorted(n for n in missing_set)))
            )
        return op_id

    # ---------- reads ----------

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def offline_shifts(self):
        return self.repository.list_offline_shifts()


class ActorView:
    """操作队列里持久化的操作者快照。"""

    def __init__(self, user_id, role):
        self.user_id = user_id
        self.role = role


def _active_alarm(lookup, shaft_id):
    for alarm in lookup("alarm", "*", None):
        if alarm["status"] not in ACTIVE_ALARM_STATUSES:
            continue
        sid = alarm["data"].get("shaft_id")
        if not sid:
            equipment = DomainService._find(lookup, "equipment", "id", alarm["data"].get("equipment_id"))
            sid = equipment["data"].get("shaft_id") if equipment else None
        if sid == shaft_id:
            return True
    return False


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
