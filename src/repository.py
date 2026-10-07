import json
import sqlite3
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
                CREATE TABLE IF NOT EXISTS outbox (
                    op_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    steps TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_status
                    ON outbox(status);
                CREATE TABLE IF NOT EXISTS offline_record (
                    shift TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    kind TEXT,
                    payload TEXT NOT NULL,
                    entity_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(shift, seq)
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

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

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
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
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
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

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

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    # ---- durable outbox (write-step recovery) ----

    def create_operation(self, op_id, kind, payload):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO outbox(op_id, kind, status, payload, steps, attempts, created_at, updated_at) "
                "VALUES (?, ?, 'in_progress', ?, '[]', 0, ?, ?)",
                (op_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), now, now),
            )
        return self.get_operation(op_id)

    def get_operation(self, op_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM outbox WHERE op_id = ?", (op_id,)
            ).fetchone()
        return self._operation_from_row(row) if row else None

    def _operation_from_row(self, row):
        return {
            "op_id": row["op_id"],
            "kind": row["kind"],
            "status": row["status"],
            "payload": json.loads(row["payload"]),
            "steps": json.loads(row["steps"]),
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_incomplete_operations(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox WHERE status != 'completed' ORDER BY op_id"
            ).fetchall()
        return [self._operation_from_row(row) for row in rows]

    def update_step(self, op_id, step_name, status, result, error):
        now = utcnow()
        with self._connect() as connection:
            row = connection.execute("SELECT steps FROM outbox WHERE op_id = ?", (op_id,)).fetchone()
            steps = json.loads(row["steps"]) if row else []
            found = False
            for step in steps:
                if step.get("name") == step_name:
                    step["status"] = status
                    step["result"] = result
                    step["error"] = error
                    found = True
                    break
            if not found:
                steps.append({"name": step_name, "status": status, "result": result, "error": error})
            connection.execute(
                "UPDATE outbox SET steps = ?, updated_at = ? WHERE op_id = ?",
                (json.dumps(steps, ensure_ascii=False, sort_keys=True), now, op_id),
            )

    def mark_operation_completed(self, op_id):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE outbox SET status = 'completed', updated_at = ? WHERE op_id = ?",
                (now, op_id),
            )

    def record_operation_error(self, op_id, error):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE op_id = ?",
                (error, now, op_id),
            )

    # ---- offline records (shift + sequence merge) ----

    def get_offline_record(self, shift, seq):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM offline_record WHERE shift = ? AND seq = ?", (shift, seq)
            ).fetchone()
        return self._offline_from_row(row) if row else None

    def _offline_from_row(self, row):
        return {
            "shift": row["shift"],
            "seq": int(row["seq"]),
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "entity_id": row["entity_id"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def save_offline_record(self, shift, seq, kind, payload, entity_id, status):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO offline_record(shift, seq, kind, payload, entity_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (shift, seq, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), entity_id, status, now),
            )

    def list_offline_records(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM offline_record ORDER BY shift, seq"
            ).fetchall()
        return [self._offline_from_row(row) for row in rows]
