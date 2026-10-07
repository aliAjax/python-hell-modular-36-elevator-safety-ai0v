import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_permit_shaft
                    ON entities(json_extract(data, '$.shaft_id'))
                    WHERE kind = 'work_permit' AND status IN ('issued', 'active');
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS occupancy_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shaft_id TEXT,
                    event TEXT NOT NULL,
                    team TEXT,
                    ref_kind TEXT,
                    ref_id TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_occupancy_shaft
                    ON occupancy_log(shaft_id, id);
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    op_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_steps (
                    op_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(op_id, step_key)
                );
                CREATE INDEX IF NOT EXISTS idx_steps_op ON operation_steps(op_id, seq);
                CREATE TABLE IF NOT EXISTS offline_records (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    shift_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_id, shift_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_offline_shift
                    ON offline_records(source_id, shift_id, seq);
                CREATE TABLE IF NOT EXISTS offline_shifts (
                    source_id TEXT NOT NULL,
                    shift_id TEXT NOT NULL,
                    max_seq INTEGER NOT NULL,
                    missing TEXT NOT NULL,
                    duplicates INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, shift_id)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _dump(data):
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @contextmanager
    def transaction(self):
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _rows(self, connection, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = connection.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def tx_create_entity(self, connection, entity_id, kind, status, data, actor_id, now=None):
        now = now or utcnow()
        try:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, self._dump(data), actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("write conflict for %s %s: %s" % (kind, entity_id, exc))
        return {
            "id": entity_id, "kind": kind, "status": status, "version": 1,
            "data": dict(data), "created_by": actor_id, "created_at": now, "updated_at": now,
        }

    def tx_update_entity(self, connection, entity, next_status, patch, now=None):
        now = now or utcnow()
        merged = dict(entity["data"])
        merged.update(patch or {})
        try:
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (next_status, self._dump(merged), now, entity["id"], entity["version"]),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("write conflict for %s %s: %s" % (entity["kind"], entity["id"], exc))
        return {
            "id": entity["id"], "kind": entity["kind"], "status": next_status,
            "version": entity["version"] + 1, "data": merged,
            "created_by": entity["created_by"], "created_at": entity["created_at"],
            "updated_at": now,
        }

    def tx_audit(self, connection, entity_id, actor_id, actor_role, action, from_status, to_status, detail, now=None):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_id, actor_id, actor_role, action, from_status, to_status,
             self._dump(detail or {}), now or utcnow()),
        )

    def tx_occupancy(self, connection, shaft_id, event, team, ref_kind, ref_id, detail, now=None):
        connection.execute(
            "INSERT INTO occupancy_log(shaft_id, event, team, ref_kind, ref_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (shaft_id, event, team, ref_kind, ref_id, self._dump(detail or {}), now or utcnow()),
        )

    def create_entity(self, entity_id, kind, status, data, actor_id):
        with self.transaction() as connection:
            entity = self.tx_create_entity(connection, entity_id, kind, status, data, actor_id)
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        with self._connect() as connection:
            return self._rows(connection, kind=kind, status=status)

    def find_entities(self, kind, field, value):
        entities = self.list_entities(kind=kind)
        if field == "*":
            return entities
        return [
            entity
            for entity in entities
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = self._dump(data)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            try:
                connection.execute(
                    "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (status, payload, now, entity_id, current_version),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("write conflict: " + str(exc))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            self.tx_audit(connection, entity_id, actor_id, actor_role, action,
                          from_status, to_status, detail)

    def append_occupancy(self, shaft_id, event, team=None, ref_kind=None, ref_id=None, detail=None):
        with self._connect() as connection:
            self.tx_occupancy(connection, shaft_id, event, team, ref_kind, ref_id, detail)

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_occupancy(self, shaft_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM occupancy_log WHERE shaft_id = ? ORDER BY id", (shaft_id,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "shaft_id": row["shaft_id"],
                "event": row["event"],
                "team": row["team"],
                "ref_kind": row["ref_kind"],
                "ref_id": row["ref_id"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ----- durable operations / step journal -----

    def create_operation(self, op_id, op_type, actor_id, actor_role, payload):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO operations(id, op_type, actor_id, actor_role, payload, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
                (op_id, op_type, actor_id, actor_role, self._dump(payload), now, now),
            )
        return self.get_operation(op_id)

    def get_operation(self, op_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (op_id,)
            ).fetchone()
        return self._operation_from_row(row) if row else None

    def list_operations(self, status=None):
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM operations WHERE status = ? ORDER BY created_at, id", (status,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM operations ORDER BY created_at, id"
                ).fetchall()
        return [self._operation_from_row(row) for row in rows]

    @staticmethod
    def _operation_from_row(row):
        return {
            "id": row["id"],
            "op_type": row["op_type"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "steps": [],
        }

    def update_operation(self, op_id, status, attempts=None, last_error=None):
        with self._connect() as connection:
            connection.execute(
                "UPDATE operations SET status = ?, attempts = COALESCE(?, attempts), "
                "last_error = ?, updated_at = ? WHERE id = ?",
                (status, attempts, last_error, utcnow(), op_id),
            )

    def upsert_step(self, op_id, step_key, seq, request, status="pending"):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO operation_steps(op_id, step_key, seq, request, status, result, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?) "
                "ON CONFLICT(op_id, step_key) DO UPDATE SET "
                "request = CASE WHEN operation_steps.status = 'confirmed' THEN operation_steps.request "
                "ELSE excluded.request END, "
                "status = CASE WHEN operation_steps.status = 'confirmed' THEN operation_steps.status "
                "ELSE excluded.status END, "
                "error = CASE WHEN operation_steps.status = 'confirmed' THEN operation_steps.error END, "
                "updated_at = ?",
                (op_id, step_key, seq, self._dump(request), status, now, now),
            )

    def set_step(self, op_id, step_key, status, result=None, error=None):
        with self._connect() as connection:
            connection.execute(
                "UPDATE operation_steps SET status = ?, result = ?, error = ?, updated_at = ? "
                "WHERE op_id = ? AND step_key = ?",
                (status, self._dump(result) if result is not None else None, error,
                 utcnow(), op_id, step_key),
            )

    def list_steps(self, op_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operation_steps WHERE op_id = ? ORDER BY seq, step_key", (op_id,)
            ).fetchall()
        return [
            {
                "step_key": row["step_key"],
                "seq": int(row["seq"]),
                "request": json.loads(row["request"]),
                "status": row["status"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": row["error"],
            }
            for row in rows
        ]

    def operation_snapshot(self, op_id):
        op = self.get_operation(op_id)
        if op:
            op["steps"] = self.list_steps(op_id)
        return op

    # ----- offline records, keyed by (source, shift, seq) -----

    def insert_offline_record(self, record_id, source_id, shift_id, seq, payload):
        """Return (inserted, stored_payload): duplicates are ignored, first copy wins."""
        now = utcnow()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO offline_records(id, source_id, shift_id, seq, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (record_id, source_id, shift_id, seq, self._dump(payload), now),
            )
            inserted = cursor.rowcount > 0
            row = connection.execute(
                "SELECT payload FROM offline_records WHERE source_id = ? AND shift_id = ? AND seq = ?",
                (source_id, shift_id, seq),
            ).fetchone()
        return inserted, json.loads(row["payload"])

    def list_offline_records(self, source_id, shift_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, seq, payload FROM offline_records WHERE source_id = ? AND shift_id = ? "
                "ORDER BY seq",
                (source_id, shift_id),
            ).fetchall()
        return [{"id": row["id"], "seq": int(row["seq"]), "payload": json.loads(row["payload"])} for row in rows]

    def upsert_offline_shift(self, source_id, shift_id, max_seq, missing, duplicates, status):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO offline_shifts(source_id, shift_id, max_seq, missing, duplicates, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_id, shift_id) DO UPDATE SET max_seq = excluded.max_seq, "
                "missing = excluded.missing, duplicates = excluded.duplicates, status = excluded.status, "
                "updated_at = excluded.updated_at",
                (source_id, shift_id, max_seq, self._dump(missing), duplicates, status, utcnow()),
            )

    def list_offline_shifts(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM offline_shifts ORDER BY source_id, shift_id"
            ).fetchall()
        return [
            {
                "source_id": row["source_id"],
                "shift_id": row["shift_id"],
                "max_seq": int(row["max_seq"]),
                "missing": json.loads(row["missing"]),
                "duplicates": int(row["duplicates"]),
                "status": row["status"],
            }
            for row in rows
        ]

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
