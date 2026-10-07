from datetime import datetime

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError

# 占用账中被视为"救援进行中"的报警状态
ACTIVE_ALARM_STATUSES = ("received", "dispatched", "resolved")
RESCUE_PAUSE_STATUSES = ("requested", "issued", "active")
PERMIT_OCCUPYING_STATUSES = ("issued", "active")


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _positive(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number <= 0:
        raise ValidationError(field + " must be positive")
    return number


def _shaft_of_alarm(alarm, lookup):
    shaft_id = alarm["data"].get("shaft_id")
    if shaft_id:
        return shaft_id
    equipment = _find_one(lookup, "equipment", "id", alarm["data"].get("equipment_id"))
    return (equipment or {}).get("data", {}).get("shaft_id") if equipment else None


def _active_alarm_for_shaft(lookup, shaft_id):
    for alarm in _all(lookup, "alarm"):
        if alarm["status"] in ACTIVE_ALARM_STATUSES and _shaft_of_alarm(alarm, lookup) == shaft_id:
            return alarm
    return None


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")


def _validate_inspection(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    shaft_id = data.get("shaft_id")
    equipment_id = data.get("equipment_id")
    if not shaft_id and not equipment_id:
        raise ValidationError("maintenance requires shaft_id or equipment_id")
    if equipment_id and not _find_one(lookup, "equipment", "id", equipment_id):
        raise ValidationError("maintenance references unknown equipment")
    if shaft_id and not _find_one(lookup, "shaft", "id", shaft_id):
        raise ValidationError("maintenance requires an existing shaft")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")
    if not str(data.get("team", "")).strip():
        raise ValidationError("team is required")


def _validate_alarm(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("alarm requires equipment")
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ("closed", "false_alarm")
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] == "closed":
        raise ValidationError("rescue_job requires an active alarm")
    key = data.get("dedupe_key")
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
            raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _validate_permit(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


def _validate_shaft(data, lookup):
    shaft_code = str(data.get("shaft_code", "")).strip()
    if not shaft_code:
        raise ValidationError("shaft_code is required")
    if _find_one(lookup, "shaft", "shaft_code", shaft_code):
        raise ConflictError("shaft_code already exists: " + shaft_code)
    if not str(data.get("location", "")).strip():
        raise ValidationError("location is required")


def _validate_work_permit(data, lookup):
    shaft = _find_one(lookup, "shaft", "id", data.get("shaft_id"))
    if not shaft:
        raise ValidationError("work_permit requires an existing shaft")
    if not str(data.get("team", "")).strip():
        raise ValidationError("team is required")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    # 申请阶段允许排队；同井道只放一张进场许可的互斥在 issue 时强制
    # （并由 work_permit(status='issued') 部分唯一索引兜底）。


def _grant_permit(actor, entity, data, lookup):
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    if not equipment or equipment["status"] not in ("in_service", "suspended"):
        raise ConflictError("permit can only be granted for a serviceable equipment")
    inspections = [i for i in _all(lookup, "inspection") if i["data"].get("equipment_id") == equipment["id"] and i["status"] == "passed"]
    if not inspections:
        raise ConflictError("permit requires a passed inspection")
    if [r for r in _all(lookup, "remediation") if r["data"].get("equipment_id") == equipment["id"] and r["status"] != "closed"]:
        raise ConflictError("permit blocked by open remediation")
    return {"granted_by": actor.user_id, "granted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _issue_work_permit(actor, entity, data, lookup):
    shaft_id = entity["data"].get("shaft_id")
    if _active_alarm_for_shaft(lookup, shaft_id):
        raise ConflictError("cannot issue work permit while shaft rescue is active")
    for permit in _all(lookup, "work_permit"):
        if permit["id"] == entity["id"]:
            continue
        if permit["data"].get("shaft_id") == shaft_id and permit["status"] in PERMIT_OCCUPYING_STATUSES:
            raise ConflictError("shaft already has an active work permit: " + permit["id"])
    return {"issued_by": actor.user_id, "issued_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _activate_work_permit(actor, entity, data, lookup):
    if _active_alarm_for_shaft(lookup, entity["data"].get("shaft_id")):
        raise ConflictError("cannot enter shaft while rescue is active")
    return {"activated_by": actor.user_id, "activated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}


def _complete_rescue(actor, entity, data, lookup):
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if not jobs or any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    return {"resolved_by": actor.user_id}


def _start_maintenance(actor, entity, data, lookup):
    shaft_id = entity["data"].get("shaft_id")
    permit = None
    for candidate in _all(lookup, "work_permit"):
        if (
            candidate["data"].get("shaft_id") == shaft_id
            and candidate["data"].get("team") == entity["data"].get("team")
            and candidate["status"] in PERMIT_OCCUPYING_STATUSES
        ):
            permit = candidate
            break
    if not permit:
        raise ConflictError("maintenance requires a valid issued/active work permit for this shaft and team")
    if _active_alarm_for_shaft(lookup, shaft_id):
        raise ConflictError("cannot start maintenance while shaft rescue is active")
    return {"permit_id": permit["id"], "started_by": actor.user_id, "started_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _resume_maintenance(actor, entity, data, lookup):
    shaft_id = entity["data"].get("shaft_id")
    valid_permit = any(
        permit["data"].get("shaft_id") == shaft_id
        and permit["data"].get("team") == entity["data"].get("team")
        and permit["status"] in PERMIT_OCCUPYING_STATUSES
        for permit in _all(lookup, "work_permit")
    )
    if not valid_permit:
        raise ConflictError("cannot resume maintenance without a valid work permit")
    if _active_alarm_for_shaft(lookup, shaft_id):
        raise ConflictError("cannot resume maintenance while shaft rescue is active")
    return {"resumed_by": actor.user_id, "resumed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit", "shafts": "shaft", "work_permits": "work_permit",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "dispatched", "remediation": "open",
        "permit": "blocked", "shaft": "available", "work_permit": "requested",
    }
    TRANSITIONS = {
        "equipment": {
            "suspend": (("in_service",), "suspended"),
            "out_of_service": (("in_service", "suspended"), "out_of_service"),
            "return_to_service": (("suspended",), "in_service"),
        },
        "inspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "reschedule": (("failed",), "scheduled"),
        },
        "maintenance": {
            "start": (("planned",), "in_progress"),
            "confirm_evacuation": (("in_progress",), "evacuated"),
            "complete": (("evacuated",), "completed"),
            "mark_review": (("in_progress",), "review_pending"),
            "review_evacuated": (("review_pending",), "evacuated"),
            "review_resume": (("review_pending",), "in_progress"),
        },
        "alarm": {
            "dispatch": (("received",), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
        },
        "rescue_job": {
            "arrive": (("dispatched",), "on_site"),
            "complete": (("on_site",), "completed"),
            "abort": (("dispatched", "on_site"), "aborted"),
        },
        "remediation": {
            "submit_evidence": (("open",), "evidence_submitted"),
            "verify": (("evidence_submitted",), "verified"),
            "reject": (("evidence_submitted",), "open"),
            "close": (("verified",), "closed"),
        },
        "permit": {
            "request_review": (("blocked",), "pending_review"),
            "grant": (("pending_review",), "granted"),
            "revoke": (("granted", "pending_review"), "revoked"),
            "expire": (("granted",), "expired"),
        },
        "work_permit": {
            "issue": (("requested",), "issued"),
            "activate": (("issued",), "active"),
            "confirm_evacuation": (("active", "issued"), "evacuated"),
            "close": (("evacuated", "rescue_stop", "issued", "requested"), "closed"),
            "void": (("requested", "issued", "active"), "void"),
        },
    }
    CREATE_REQUIRED = {
        "equipment": ("asset_no", "equipment_type", "location", "inspection_interval_days"),
        "inspection": ("equipment_id", "scheduled_at", "cycle_days"),
        "maintenance": ("work_type", "planned_at", "team"),
        "alarm": ("equipment_id", "code", "occurred_at"),
        "rescue_job": ("alarm_id", "dedupe_key", "team"),
        "remediation": ("issue", "owner", "due_at"),
        "permit": ("equipment_id", "purpose", "requested_by"),
        "shaft": ("shaft_code", "location"),
        "work_permit": ("shaft_id", "team", "work_type"),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("maintenance", "confirm_evacuation"): ("evacuated_by",),
        ("maintenance", "review_evacuated"): ("reviewed_by",),
        ("maintenance", "review_resume"): ("reviewed_by",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
        ("permit", "revoke"): ("reason",),
        ("work_permit", "confirm_evacuation"): ("evacuated_by",),
        ("work_permit", "close"): ("reason",),
    }
    CREATE_ROLES = {
        "equipment": ("admin", "inspector"),
        "inspection": ("admin", "inspector"),
        "maintenance": ("admin", "maintenance"),
        "alarm": ("admin", "dispatcher", "inspector", "maintenance"),
        "rescue_job": ("admin", "dispatcher"),
        "remediation": ("admin", "inspector", "maintenance"),
        "permit": ("admin", "inspector"),
        "shaft": ("admin", "inspector", "dispatcher"),
        "work_permit": ("admin", "maintenance", "inspector"),
    }
    ROLE_ACTIONS = {
        "suspend": ("admin", "inspector"),
        "out_of_service": ("admin", "inspector"),
        "return_to_service": ("admin", "inspector"),
        "pass": ("admin", "inspector"),
        "fail": ("admin", "inspector"),
        "reschedule": ("admin", "inspector"),
        "start": ("admin", "maintenance"),
        "complete": ("admin", "maintenance", "dispatcher"),
        "confirm_evacuation": ("admin", "maintenance"),
        "mark_review": ("admin", "maintenance", "dispatcher", "inspector"),
        "review_evacuated": ("admin", "dispatcher", "inspector"),
        "review_resume": ("admin", "dispatcher", "inspector", "maintenance"),
        "dispatch": ("admin", "dispatcher"),
        "mark_false": ("admin", "dispatcher", "inspector"),
        "resolve": ("admin", "dispatcher"),
        "close": ("admin", "dispatcher", "inspector", "maintenance"),
        "arrive": ("admin", "dispatcher"),
        "abort": ("admin", "dispatcher"),
        "submit_evidence": ("admin", "maintenance", "inspector"),
        "verify": ("admin", "inspector"),
        "reject": ("admin", "inspector"),
        "request_review": ("admin", "inspector"),
        "grant": ("admin", "inspector"),
        "revoke": ("admin", "inspector"),
        "expire": ("admin", "inspector"),
        "issue": ("admin", "dispatcher", "inspector"),
        "activate": ("admin", "maintenance"),
        "void": ("admin", "dispatcher", "inspector"),
    }
    CUSTOM_CREATE = {
        "equipment": lambda a, d, l: _validate_equipment(d, l),
        "inspection": lambda a, d, l: _validate_inspection(d, l),
        "maintenance": lambda a, d, l: _validate_maintenance(d, l),
        "alarm": lambda a, d, l: _validate_alarm(d, l),
        "rescue_job": lambda a, d, l: _validate_rescue(d, l),
        "remediation": lambda a, d, l: _validate_remediation(d, l),
        "permit": lambda a, d, l: _validate_permit(d, l),
        "shaft": lambda a, d, l: _validate_shaft(d, l),
        "work_permit": lambda a, d, l: _validate_work_permit(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("permit", "grant"): _grant_permit,
        ("remediation", "verify"): _verify_remediation,
        ("alarm", "close"): _complete_rescue,
        ("work_permit", "issue"): _issue_work_permit,
        ("work_permit", "activate"): _activate_work_permit,
        ("maintenance", "start"): _start_maintenance,
        ("maintenance", "review_resume"): _resume_maintenance,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
