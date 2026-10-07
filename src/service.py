import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def merge_offline(self, actor, records):
        """Merge offline records by (shift, seq).

        Records are applied in sequence order per shift. Duplicates (same shift+seq
        already applied) are reconciled and skipped; conflicting content for an
        already-applied key is flagged; gaps (missing seq numbers within a shift)
        are reported so they can be retransmitted.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        normalized = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            shift = str(raw.get("shift", "")).strip()
            if not shift:
                raise ValidationError("shift is required")
            try:
                seq = int(raw.get("seq"))
            except (TypeError, ValueError):
                raise ValidationError("seq must be an integer")
            if seq <= 0:
                raise ValidationError("seq must be a positive integer")
            normalized.append({"shift": shift, "seq": seq, "raw": dict(raw)})
        normalized.sort(key=lambda r: (r["shift"], r["seq"]))

        applied, duplicates, conflicts, errors = [], [], [], []
        for rec in normalized:
            shift, seq, raw = rec["shift"], rec["seq"], rec["raw"]
            kind = raw.get("kind")
            payload = raw.get("payload", raw)
            existing = self.repository.get_offline_record(shift, seq)
            if existing:
                if existing["kind"] == kind and existing["payload"] == payload:
                    duplicates.append({"shift": shift, "seq": seq, "entity_id": existing["entity_id"]})
                else:
                    conflicts.append({"shift": shift, "seq": seq, "incoming": raw, "existing": {
                        "kind": existing["kind"], "payload": existing["payload"], "entity_id": existing["entity_id"],
                    }})
                continue
            try:
                entity_id = self._apply_offline_record(actor, kind, payload, shift, seq)
                self.repository.save_offline_record(shift, seq, kind, payload, entity_id, "applied")
                applied.append({"shift": shift, "seq": seq, "entity_id": entity_id})
            except Exception as exc:
                errors.append({"shift": shift, "seq": seq, "error": str(exc)})
        return {
            "applied": applied,
            "duplicates": duplicates,
            "conflicts": conflicts,
            "errors": errors,
            "gaps": self._offline_gaps(),
        }

    def _apply_offline_record(self, actor, kind, payload, shift, seq):
        if kind and kind in self.rules.INITIAL_STATUS:
            entity = self.create(actor, kind, payload, "offline:%s:%s" % (shift, seq))
            return entity["id"]
        entity_id = "offline-" + hashlib.sha256((shift + "\0" + str(seq)).encode("utf-8")).hexdigest()[:32]
        existing = self.repository.get_entity(entity_id)
        if existing:
            return entity_id
        self.rules.validate_create(actor, "offline_record", payload, self._lookup)
        entity = self.repository.create_entity(
            entity_id,
            "offline_record",
            self.rules.initial_status("offline_record", payload),
            payload,
            actor.user_id,
        )
        self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"shift": shift, "seq": seq})
        return entity_id

    def _offline_gaps(self):
        by_shift = {}
        for row in self.repository.list_offline_records():
            by_shift.setdefault(row["shift"], set()).add(row["seq"])
        gaps = []
        for shift in sorted(by_shift):
            seqs = by_shift[shift]
            if not seqs:
                continue
            max_seq = max(seqs)
            missing = [seq for seq in range(1, max_seq + 1) if seq not in seqs]
            if missing:
                gaps.append({"shift": shift, "missing": missing})
        return gaps

    def offline_gaps(self):
        return {"gaps": self._offline_gaps()}

    # ---- occupancy coordinator (durable multi-step writes) ----

    def _idempotent_create(self, actor, kind, data, entity_id):
        existing = self.repository.get_entity(entity_id)
        if existing:
            return existing
        return self.create(actor, kind, {**data, "id": entity_id})

    def _idempotent_transition(self, actor, entity_id, action, data=None, target_status=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if target_status and entity["status"] == target_status:
            return entity
        return self.transition(actor, entity_id, action, data or {}, entity["version"])

    def _run(self, actor, op_id, kind, payload):
        self.repository.create_operation(op_id, kind, payload)
        steps = self._build_steps(kind, payload)
        return self._execute_steps(actor, op_id, kind, payload, steps)

    def _execute_steps(self, actor, op_id, kind, payload, steps):
        ctx = {"payload": payload, "results": {}}
        for name, fn in steps:
            op = self.repository.get_operation(op_id)
            step_rec = next((s for s in op["steps"] if s.get("name") == name), None) if op else None
            if step_rec and step_rec["status"] == "completed":
                ctx["results"][name] = step_rec.get("result")
                continue
            try:
                result = fn(actor, payload, ctx)
                self.repository.update_step(op_id, name, "completed", result, None)
                ctx["results"][name] = result
            except Exception as exc:
                self.repository.update_step(op_id, name, "failed", None, str(exc))
                self.repository.record_operation_error(op_id, str(exc))
                raise
        self.repository.mark_operation_completed(op_id)
        return ctx

    def _build_steps(self, kind, payload):
        builders = {
            "issue_entry_permit": self._steps_issue_entry_permit,
            "report_alarm": self._steps_report_alarm,
            "release_hoistway": self._steps_release_hoistway,
        }
        builder = builders.get(kind)
        if not builder:
            raise ValidationError("unknown operation kind: " + str(kind))
        return builder(payload)

    def _steps_issue_entry_permit(self, payload):
        hoistway_id = payload["hoistway_id"]
        permit_id = "permit-" + str(payload["op_id"])

        def invalidate_old(actor, payload, ctx):
            for permit in self.repository.find_entities("entry_permit", "hoistway_id", hoistway_id):
                if permit["status"] == "active":
                    self._idempotent_transition(actor, permit["id"], "invalidate", target_status="invalidated")
            return True

        def release_hoistway(actor, payload, ctx):
            return self._idempotent_transition(actor, hoistway_id, "release", target_status="free")

        def create_permit(actor, payload, ctx):
            return self._idempotent_create(actor, "entry_permit", {
                "hoistway_id": hoistway_id,
                "team": payload["team"],
                "shift": payload.get("shift"),
                "seq": payload.get("seq"),
                "purpose": payload["purpose"],
            }, permit_id)

        def grant_permit(actor, payload, ctx):
            return self._idempotent_transition(actor, permit_id, "grant", target_status="active")

        def occupy(actor, payload, ctx):
            return self._idempotent_transition(actor, hoistway_id, "occupy", target_status="occupied")

        return [
            ("invalidate_old", invalidate_old),
            ("release_hoistway", release_hoistway),
            ("create_permit", create_permit),
            ("grant_permit", grant_permit),
            ("occupy", occupy),
        ]

    def _steps_report_alarm(self, payload):
        hoistway_id = payload["hoistway_id"]
        alarm_id = "alarm-" + str(payload["op_id"])

        def create_alarm(actor, payload, ctx):
            data = {
                "equipment_id": payload["equipment_id"],
                "code": payload["code"],
                "occurred_at": payload["occurred_at"],
                "hoistway_id": hoistway_id,
            }
            if payload.get("team"):
                data["team"] = payload["team"]
            return self._idempotent_create(actor, "alarm", data, alarm_id)

        def invalidate_permit(actor, payload, ctx):
            for permit in self.repository.find_entities("entry_permit", "hoistway_id", hoistway_id):
                if permit["status"] in ("active", "requested"):
                    self._idempotent_transition(actor, permit["id"], "invalidate", target_status="invalidated")
            return True

        def alarm_hoistway(actor, payload, ctx):
            return self._idempotent_transition(actor, hoistway_id, "alarm", target_status="alarm")

        def review_tasks(actor, payload, ctx):
            affected = []
            equipment_id = payload.get("equipment_id")
            if equipment_id:
                for task in self.repository.find_entities("maintenance", "equipment_id", equipment_id):
                    if task["status"] == "in_progress" and not task["data"].get("evacuated"):
                        updated = self._idempotent_transition(
                            actor, task["id"], "mark_pending_review", target_status="pending_review"
                        )
                        affected.append(updated["id"])
            return affected

        return [
            ("create_alarm", create_alarm),
            ("invalidate_permit", invalidate_permit),
            ("alarm_hoistway", alarm_hoistway),
            ("review_tasks", review_tasks),
        ]

    def _steps_release_hoistway(self, payload):
        hoistway_id = payload["hoistway_id"]

        def check_clear(actor, payload, ctx):
            hoistway = self.repository.get_entity(hoistway_id)
            equipment_id = hoistway["data"].get("equipment_id") if hoistway else None
            for alarm in self.repository.find_entities("alarm", "hoistway_id", hoistway_id):
                if alarm["status"] not in ("closed", "false_alarm"):
                    raise ConflictError("cannot release: active alarm " + alarm["id"])
            for alarm in self.repository.find_entities("alarm", "hoistway_id", hoistway_id):
                for job in self.repository.find_entities("rescue_job", "alarm_id", alarm["id"]):
                    if job["status"] not in ("completed", "aborted"):
                        raise ConflictError("cannot release: active rescue job " + job["id"])
            if equipment_id:
                for task in self.repository.find_entities("maintenance", "equipment_id", equipment_id):
                    if task["status"] == "in_progress" and not task["data"].get("evacuated"):
                        raise ConflictError("cannot release: maintenance task not evacuated " + task["id"])
            return True

        def evacuate_permit(actor, payload, ctx):
            for permit in self.repository.find_entities("entry_permit", "hoistway_id", hoistway_id):
                if permit["status"] == "active":
                    self._idempotent_transition(actor, permit["id"], "evacuate", target_status="evacuated")
            return True

        def release(actor, payload, ctx):
            return self._idempotent_transition(actor, hoistway_id, "release", target_status="free")

        return [
            ("check_clear", check_clear),
            ("evacuate_permit", evacuate_permit),
            ("release", release),
        ]

    def issue_entry_permit(self, actor, hoistway_id, team, purpose, shift=None, seq=None):
        hoistway = self.repository.get_entity(hoistway_id)
        if not hoistway or hoistway["kind"] != "hoistway":
            raise ValidationError("hoistway not found: " + str(hoistway_id))
        for alarm in self.repository.find_entities("alarm", "hoistway_id", hoistway_id):
            if alarm["status"] not in ("closed", "false_alarm"):
                raise ConflictError("cannot issue permit: active alarm " + alarm["id"])
        op_id = "issue:%s:%s:%s" % (hoistway_id, shift, seq)
        payload = {"op_id": op_id, "hoistway_id": hoistway_id, "team": team,
                   "purpose": purpose, "shift": shift, "seq": seq}
        ctx = self._run(actor, op_id, "issue_entry_permit", payload)
        permit = ctx["results"].get("grant_permit") or self.repository.get_entity("permit-" + op_id)
        return {
            "permit": permit,
            "hoistway": self.repository.get_entity(hoistway_id),
        }

    def report_alarm(self, actor, hoistway_id, code, occurred_at, team=None):
        hoistway = self.repository.get_entity(hoistway_id)
        if not hoistway or hoistway["kind"] != "hoistway":
            raise ValidationError("hoistway not found: " + str(hoistway_id))
        equipment_id = hoistway["data"].get("equipment_id")
        if not equipment_id:
            raise ValidationError("hoistway has no equipment_id; cannot raise alarm")
        op_id = "alarm:%s:%s:%s" % (hoistway_id, code, occurred_at)
        payload = {"op_id": op_id, "hoistway_id": hoistway_id, "equipment_id": equipment_id,
                   "code": code, "occurred_at": occurred_at, "team": team}
        ctx = self._run(actor, op_id, "report_alarm", payload)
        return {
            "alarm": ctx["results"]["create_alarm"],
            "hoistway": self.repository.get_entity(hoistway_id),
        }

    def confirm_evacuation(self, actor, maintenance_id):
        return self.transition(actor, maintenance_id, "confirm_evacuation", {}, None)

    def release_hoistway(self, actor, hoistway_id):
        hoistway = self.repository.get_entity(hoistway_id)
        if not hoistway or hoistway["kind"] != "hoistway":
            raise ValidationError("hoistway not found: " + str(hoistway_id))
        op_id = "release:" + hoistway_id
        payload = {"op_id": op_id, "hoistway_id": hoistway_id}
        self._run(actor, op_id, "release_hoistway", payload)
        return {"hoistway": self.repository.get_entity(hoistway_id)}

    def occupancy_ledger(self, hoistway_id):
        hoistway = self.repository.get_entity(hoistway_id)
        if not hoistway or hoistway["kind"] != "hoistway":
            raise ValidationError("hoistway not found: " + str(hoistway_id))
        equipment_id = hoistway["data"].get("equipment_id")
        permits = self.repository.find_entities("entry_permit", "hoistway_id", hoistway_id)
        alarms = self.repository.find_entities("alarm", "hoistway_id", hoistway_id)
        maintenance = self.repository.find_entities("maintenance", "equipment_id", equipment_id) if equipment_id else []
        rescue_jobs = []
        for alarm in alarms:
            rescue_jobs.extend(self.repository.find_entities("rescue_job", "alarm_id", alarm["id"]))
        active_permit = next((p for p in permits if p["status"] == "active"), None)
        unevacuated = [t for t in maintenance if t["status"] == "in_progress" and not t["data"].get("evacuated")]
        active_alarms = [a for a in alarms if a["status"] not in ("closed", "false_alarm")]
        active_rescue = [j for j in rescue_jobs if j["status"] not in ("completed", "aborted")]
        return {
            "hoistway": hoistway,
            "active_permit": active_permit,
            "permits": permits,
            "maintenance": {
                "in_progress": [t for t in maintenance if t["status"] == "in_progress"],
                "pending_review": [t for t in maintenance if t["status"] == "pending_review"],
                "planned": [t for t in maintenance if t["status"] == "planned"],
                "completed": [t for t in maintenance if t["status"] == "completed"],
            },
            "alarms": {"active": active_alarms, "all": alarms},
            "rescue_jobs": {"active": active_rescue, "all": rescue_jobs},
            "unevacuated_tasks": unevacuated,
            "can_release": not active_alarms and not active_rescue and not unevacuated,
        }

    def recover(self, actor=None):
        actor = actor or Actor("system", "admin")
        ops = self.repository.list_incomplete_operations()
        recovered, failed = [], []
        for op in ops:
            try:
                self._run(actor, op["op_id"], op["kind"], op["payload"])
                recovered.append({"op_id": op["op_id"], "kind": op["kind"], "status": "recovered"})
            except Exception as exc:
                failed.append({"op_id": op["op_id"], "kind": op["kind"], "status": "failed", "error": str(exc)})
        return {"recovered": recovered, "failed": failed, "pending": len(ops)}

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
