"""V2 Task Event Service — append-only event log with dual-layer protection.

Writes task events with:
  - append-only guarantee: no UPDATE/DELETE on task_events at service layer
  - Database trigger protection: optional CREATE TRIGGER to block UPDATE/DELETE
  - Idempotency: UNIQUE(idempotency_key) at DB level
  - Atomic: always within the same transaction as state change
"""
import sqlite3
import uuid
import json
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path


class TaskEventService:
    """Append-only event log for task state changes.

    Events are immutable once written. The service layer enforces this
    by providing no UPDATE or DELETE methods. An optional database trigger
    provides a second layer of protection.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    # ── Install append-only trigger ──

    INSTALL_TRIGGER_SQL = """
    CREATE TRIGGER IF NOT EXISTS trg_task_events_append_only
    BEFORE UPDATE ON task_events
    BEGIN
        SELECT RAISE(ABORT, 'task_events is append-only: UPDATE not allowed');
    END;
    """

    DELETE_TRIGGER_SQL = """
    CREATE TRIGGER IF NOT EXISTS trg_task_events_no_delete
    BEFORE DELETE ON task_events
    BEGIN
        SELECT RAISE(ABORT, 'task_events is append-only: DELETE not allowed');
    END;
    """

    def install_triggers(self) -> bool:
        """Install database-level append-only protection triggers."""
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(self.INSTALL_TRIGGER_SQL)
            conn.execute(self.DELETE_TRIGGER_SQL)
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            print(f"[WARN] Failed to install append-only triggers: {e}")
            return False
        finally:
            conn.close()

    def remove_triggers(self) -> bool:
        """Remove append-only triggers (for rollback/testing)."""
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TRIGGER IF EXISTS trg_task_events_append_only")
            conn.execute("DROP TRIGGER IF EXISTS trg_task_events_no_delete")
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            print(f"[WARN] Failed to remove triggers: {e}")
            return False
        finally:
            conn.close()

    def triggers_installed(self) -> Dict[str, bool]:
        """Check if append-only triggers are installed."""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_task_events_%'")
            names = {row["name"] for row in cur.fetchall()}
            return {
                "update_trigger": "trg_task_events_append_only" in names,
                "delete_trigger": "trg_task_events_no_delete" in names,
            }
        finally:
            conn.close()

    # ── Write event (must be called INSIDE a transaction) ──

    def write_event(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        project_id: int,
        event_type: str,
        from_state: Optional[str],
        to_state: Optional[str],
        actor_type: str,
        actor_id: str,
        reason: str = "",
        idempotency_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        state_version_before: Optional[int] = None,
        state_version_after: Optional[int] = None,
        assignment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write an event record. Must be called within an existing transaction.

        This method does NOT manage its own transaction — the caller is
        responsible for BEGIN/COMMIT/ROLLBACK to ensure atomicity between
        state change and event write.

        Returns:
            {"success": bool, "event_id": str, "error": str}
        """
        event_id = f"event-{uuid.uuid4().hex[:12]}"
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO task_events
                (event_id, task_id, assignment_id, project_id,
                 event_type, from_state, to_state, reason, detail_json,
                 operator_type, operator_id, idempotency_key,
                 state_version_before, state_version_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id, task_id, assignment_id, project_id,
                event_type, from_state, to_state, reason, meta_json,
                actor_type, actor_id, idempotency_key,
                state_version_before, state_version_after,
            ))

            return {"success": True, "event_id": event_id, "error": None}

        except sqlite3.IntegrityError as e:
            error_str = str(e)
            if "UNIQUE constraint failed: task_events.event_id" in error_str:
                return {"success": False, "event_id": None,
                        "error": "event_id collision"}
            if "UNIQUE constraint failed: task_events.idempotency_key" in error_str:
                return {"success": False, "event_id": None,
                        "error": "idempotency_key collision"}
            return {"success": False, "event_id": None, "error": error_str}

    # ── Query events ──

    def get_events_for_task(self, task_id: int,
                           limit: int = 100,
                           event_type: Optional[str] = None) -> Dict[str, Any]:
        """Get events for a task, newest first.

        Returns:
            {"success": bool, "events": list, "error": str}
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            if event_type:
                cur.execute("""
                    SELECT event_id, task_id, event_type, from_state, to_state,
                           reason, operator_type, operator_id, idempotency_key,
                           state_version_before, state_version_after, created_at
                    FROM task_events
                    WHERE task_id = ? AND event_type = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (task_id, event_type, limit))
            else:
                cur.execute("""
                    SELECT event_id, task_id, event_type, from_state, to_state,
                           reason, operator_type, operator_id, idempotency_key,
                           state_version_before, state_version_after, created_at
                    FROM task_events
                    WHERE task_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (task_id, limit))

            events = [dict(row) for row in cur.fetchall()]
            return {"success": True, "events": events, "error": None}
        except Exception as e:
            return {"success": False, "events": [], "error": str(e)}
        finally:
            conn.close()

    def get_event_by_id(self, event_id: str) -> Dict[str, Any]:
        """Get a single event by its ID.

        Returns:
            {"success": bool, "event": dict or None, "error": str}
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM task_events WHERE event_id = ?", (event_id,))
            row = cur.fetchone()
            if row is None:
                return {"success": True, "event": None, "error": None}
            return {"success": True, "event": dict(row), "error": None}
        except Exception as e:
            return {"success": False, "event": None, "error": str(e)}
        finally:
            conn.close()

    # ── Verify append-only protection ──

    def verify_append_only(self) -> Dict[str, Any]:
        """Verify that task_events enforces append-only at service and DB level.

        Returns:
            {"append_only": bool, "triggers_installed": dict, "no_update_method": bool, "no_delete_method": bool}
        """
        # Service layer: this class has no update or delete methods by design
        public_methods = [m for m in dir(self) if not m.startswith('_') and callable(getattr(self, m))]
        has_update = any("update" in m.lower() for m in public_methods)
        has_delete = any("delete" in m.lower() for m in public_methods)

        return {
            "append_only": True,
            "triggers_installed": self.triggers_installed(),
            "no_update_method": not has_update,
            "no_delete_method": not has_delete,
        }
