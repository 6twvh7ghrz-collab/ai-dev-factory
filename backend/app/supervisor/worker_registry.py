"""V2.0-B2: Worker Registry Service — registration, lookup, status management.

Manages the agent_workers and agent_capabilities tables.
Enforces:
  - Only ONE executor worker can be AVAILABLE/BUSY at a time
  - Supervisor/Reviewer workers don't count against executor concurrency
  - max_concurrency fixed to 1 for MVP
  - Idempotency-Key support for register_worker
  - Feature flag gating: V2_CONTROL_PLANE_ENABLED
"""
import sqlite3
import uuid
import json
import hashlib
import os
from typing import Dict, Any, Optional, List
from datetime import datetime


# ============================================================
# Worker status constants
# ============================================================

WORKER_STATUS_REGISTERED = "registered"
WORKER_STATUS_AVAILABLE = "available"
WORKER_STATUS_BUSY = "busy"
WORKER_STATUS_OFFLINE = "offline"
WORKER_STATUS_DISABLED = "disabled"

ALL_WORKER_STATUSES = frozenset([
    WORKER_STATUS_REGISTERED, WORKER_STATUS_AVAILABLE,
    WORKER_STATUS_BUSY, WORKER_STATUS_OFFLINE, WORKER_STATUS_DISABLED,
])

# ============================================================
# Worker type constants
# ============================================================

WORKER_TYPE_EXECUTOR = "executor"
WORKER_TYPE_SUPERVISOR = "supervisor"
WORKER_TYPE_REVIEWER = "reviewer"

ALL_WORKER_TYPES = frozenset([WORKER_TYPE_EXECUTOR, WORKER_TYPE_SUPERVISOR, WORKER_TYPE_REVIEWER])

# ============================================================
# Error codes
# ============================================================

ERROR_V2_CONTROL_PLANE_DISABLED = "V2_CONTROL_PLANE_DISABLED"
ERROR_WORKER_NOT_REGISTERED = "WORKER_NOT_REGISTERED"
ERROR_WORKER_NOT_AVAILABLE = "WORKER_NOT_AVAILABLE"
ERROR_WORKER_ALREADY_REGISTERED = "WORKER_ALREADY_REGISTERED"
ERROR_EXECUTOR_CONCURRENCY_LIMIT = "EXECUTOR_CONCURRENCY_LIMIT"
ERROR_INVALID_WORKER_TYPE = "INVALID_WORKER_TYPE"
ERROR_INVALID_WORKER_STATUS = "INVALID_WORKER_STATUS"
ERROR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERROR_WORKER_CAPABILITY_MISMATCH = "WORKER_CAPABILITY_MISMATCH"


class WorkerRegistryService:
    """Worker registration and lookup service.

    Feature flag: reads V2_CONTROL_PLANE_ENABLED from environment.
    When disabled, all mutation methods return V2_CONTROL_PLANE_DISABLED.
    """

    def __init__(self, db_path: str, v2_enabled: Optional[bool] = None):
        self.db_path = db_path
        if v2_enabled is not None:
            self._v2_enabled = v2_enabled
        else:
            val = os.getenv("V2_CONTROL_PLANE_ENABLED", "false").lower()
            self._v2_enabled = val in ("true", "1")

    @property
    def is_v2_enabled(self) -> bool:
        return self._v2_enabled

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _v2_gate(self) -> Optional[Dict[str, Any]]:
        """Return error dict if V2 is disabled, None if enabled."""
        if not self._v2_enabled:
            return {
                "success": False,
                "error": "V2_CONTROL_PLANE_ENABLED is off; operation rejected",
                "error_code": ERROR_V2_CONTROL_PLANE_DISABLED,
            }
        return None

    # ── Register Worker ──

    def register_worker(
        self,
        worker_id: str,
        worker_type: str,
        provider: str = "",
        display_name: str = "",
        capabilities: Optional[List[str]] = None,
        sandbox_profile_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register a worker with optional idempotency.

        Returns:
            {"success": bool, "worker": dict, "error": str, "error_code": str,
             "idempotent": bool}
        """
        gate = self._v2_gate()
        if gate:
            return gate

        # Validate worker_type
        if worker_type not in ALL_WORKER_TYPES:
            return {
                "success": False,
                "error": f"Invalid worker_type: {worker_type}",
                "error_code": ERROR_INVALID_WORKER_TYPE,
            }

        conn = self._get_conn()
        try:
            # ── Idempotency check ──
            if idempotency_key:
                idem_result = self._check_register_idempotency(
                    conn, worker_id, worker_type, provider, display_name,
                    capabilities or [], sandbox_profile_id, metadata or {},
                    idempotency_key
                )
                if idem_result is not None:
                    conn.close()
                    return idem_result

            # ── Enforce single executor AVAILABLE/BUSY ──
            if worker_type == WORKER_TYPE_EXECUTOR:
                cur = conn.cursor()
                cur.execute("""
                    SELECT worker_id FROM agent_workers
                    WHERE worker_type = 'executor' AND status IN ('available','busy')
                """)
                existing = cur.fetchone()
                if existing:
                    conn.close()
                    return {
                        "success": False,
                        "error": f"Another executor worker is already AVAILABLE/BUSY: {existing['worker_id']}",
                        "error_code": ERROR_EXECUTOR_CONCURRENCY_LIMIT,
                    }

            # ── Check if worker already registered ──
            cur = conn.cursor()
            cur.execute("SELECT worker_id, version FROM agent_workers WHERE worker_id = ?", (worker_id,))
            existing = cur.fetchone()
            if existing:
                # Idempotent: return existing worker
                worker = self._get_worker_dict(conn, worker_id)
                conn.close()
                return {
                    "success": True,
                    "worker": worker,
                    "error": None,
                    "error_code": None,
                    "idempotent": True,
                }

            # ── Register ──
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # Store idempotency fingerprint if a key was provided
                metadata_to_store = dict(metadata or {})
                if idempotency_key:
                    fp = self._build_register_fingerprint(
                        worker_id, worker_type, provider, display_name,
                        capabilities or [], sandbox_profile_id, metadata or {}
                    )
                    metadata_to_store["_idempotency_fingerprint"] = fp

                metadata_json = json.dumps(metadata_to_store, ensure_ascii=False)

                cur.execute("""
                    INSERT INTO agent_workers
                    (worker_id, worker_type, provider, display_name, status,
                     max_concurrency, current_load, sandbox_profile_id,
                     registered_at, last_seen_at, metadata_json, version)
                    VALUES (?, ?, ?, ?, 'registered', 1, 0, ?, ?, ?, ?, 1)
                """, (
                    worker_id, worker_type, provider, display_name,
                    sandbox_profile_id, now, now, metadata_json,
                ))

                # Write capabilities
                for cap in (capabilities or []):
                    cur.execute(
                        "INSERT INTO agent_capabilities (worker_id, capability) VALUES (?, ?)",
                        (worker_id, cap)
                    )

                conn.commit()

                worker = self._get_worker_dict(conn, worker_id)
                return {
                    "success": True,
                    "worker": worker,
                    "error": None,
                    "error_code": None,
                    "idempotent": False,
                }

            except Exception:
                conn.rollback()
                raise

        except Exception as e:
            conn.rollback()
            return {
                "success": False,
                "error": str(e),
                "error_code": "REGISTRATION_FAILED",
            }
        finally:
            conn.close()

    # ── Get Worker ──

    def get_worker(self, worker_id: str) -> Dict[str, Any]:
        """Get a worker by ID.

        Returns:
            {"success": bool, "worker": dict or None, "error": str}
        """
        conn = self._get_conn()
        try:
            worker = self._get_worker_dict(conn, worker_id)
            if worker is None:
                return {"success": False, "worker": None, "error": ERROR_WORKER_NOT_REGISTERED}
            return {"success": True, "worker": worker, "error": None}
        except Exception as e:
            return {"success": False, "worker": None, "error": str(e)}
        finally:
            conn.close()

    # ── List Workers ──

    def list_workers(
        self,
        worker_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List workers with optional filters.

        Returns:
            {"success": bool, "workers": list, "error": str}
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            query = "SELECT worker_id FROM agent_workers WHERE 1=1"
            params = []

            if worker_type:
                query += " AND worker_type = ?"
                params.append(worker_type)
            if status:
                query += " AND status = ?"
                params.append(status)

            query += " ORDER BY registered_at DESC"

            cur.execute(query, params)
            rows = cur.fetchall()
            workers = []
            for row in rows:
                w = self._get_worker_dict(conn, row["worker_id"])
                if w:
                    workers.append(w)

            return {"success": True, "workers": workers, "error": None}
        except Exception as e:
            return {"success": False, "workers": [], "error": str(e)}
        finally:
            conn.close()

    # ── Set Worker Status ──

    def set_worker_status(self, worker_id: str, status: str) -> Dict[str, Any]:
        """Change a worker's status.

        Returns:
            {"success": bool, "worker": dict, "error": str, "error_code": str}
        """
        gate = self._v2_gate()
        if gate:
            return gate

        if status not in ALL_WORKER_STATUSES:
            return {
                "success": False, "error": f"Invalid status: {status}",
                "error_code": ERROR_INVALID_WORKER_STATUS,
            }

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT worker_id, worker_type, status FROM agent_workers WHERE worker_id = ?", (worker_id,))
            row = cur.fetchone()
            if row is None:
                conn.close()
                return {
                    "success": False, "error": f"Worker not found: {worker_id}",
                    "error_code": ERROR_WORKER_NOT_REGISTERED,
                }

            # Enforce single executor AVAILABLE/BUSY rule
            if row["worker_type"] == WORKER_TYPE_EXECUTOR and status in ("available", "busy"):
                cur.execute("""
                    SELECT worker_id FROM agent_workers
                    WHERE worker_type = 'executor' AND status IN ('available','busy')
                      AND worker_id != ?
                """, (worker_id,))
                existing = cur.fetchone()
                if existing:
                    conn.close()
                    return {
                        "success": False,
                        "error": f"Another executor worker is already AVAILABLE/BUSY: {existing['worker_id']}",
                        "error_code": ERROR_EXECUTOR_CONCURRENCY_LIMIT,
                    }

            conn.execute("BEGIN IMMEDIATE")
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute("""
                    UPDATE agent_workers
                    SET status = ?, last_seen_at = ?, version = version + 1
                    WHERE worker_id = ?
                """, (status, now, worker_id))

                if status in ("available",):
                    cur.execute("""
                        UPDATE agent_workers SET current_load = 0
                        WHERE worker_id = ?
                    """, (worker_id,))

                conn.commit()

                worker = self._get_worker_dict(conn, worker_id)
                return {"success": True, "worker": worker, "error": None, "error_code": None}

            except Exception:
                conn.rollback()
                raise

        except Exception as e:
            conn.rollback()
            return {"success": False, "error": str(e), "error_code": "SET_STATUS_FAILED"}
        finally:
            conn.close()

    # ── Validate Worker ──

    def validate_worker(self, worker_id: str) -> Dict[str, Any]:
        """Validate a worker exists and is in a usable state.

        Returns:
            {"success": bool, "valid": bool, "worker": dict, "reason": str}
        """
        conn = self._get_conn()
        try:
            worker = self._get_worker_dict(conn, worker_id)
            if worker is None:
                return {
                    "success": True, "valid": False, "worker": None,
                    "reason": ERROR_WORKER_NOT_REGISTERED,
                }

            if worker["status"] not in ("available", "busy"):
                return {
                    "success": True, "valid": False, "worker": worker,
                    "reason": ERROR_WORKER_NOT_AVAILABLE,
                }

            return {
                "success": True, "valid": True, "worker": worker,
                "reason": "ok",
            }
        except Exception as e:
            return {
                "success": False, "valid": False, "worker": None,
                "reason": str(e),
            }
        finally:
            conn.close()

    # ── Get Capabilities ──

    def get_capabilities(self, worker_id: str) -> Dict[str, Any]:
        """Get capabilities for a worker.

        Returns:
            {"success": bool, "capabilities": list, "error": str}
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            # Verify worker exists
            cur.execute("SELECT worker_id FROM agent_workers WHERE worker_id = ?", (worker_id,))
            if not cur.fetchone():
                return {"success": False, "capabilities": [], "error": ERROR_WORKER_NOT_REGISTERED}

            cur.execute(
                "SELECT capability FROM agent_capabilities WHERE worker_id = ? ORDER BY capability",
                (worker_id,)
            )
            caps = [row["capability"] for row in cur.fetchall()]
            return {"success": True, "capabilities": caps, "error": None}
        except Exception as e:
            return {"success": False, "capabilities": [], "error": str(e)}
        finally:
            conn.close()

    # ── Helpers ──

    def _get_worker_dict(self, conn: sqlite3.Connection, worker_id: str) -> Optional[Dict[str, Any]]:
        """Get worker with capabilities as a dict."""
        cur = conn.cursor()
        cur.execute("""
            SELECT worker_id, worker_type, provider, display_name, status,
                   max_concurrency, current_load, sandbox_profile_id,
                   registered_at, last_seen_at, metadata_json, version
            FROM agent_workers
            WHERE worker_id = ?
        """, (worker_id,))
        row = cur.fetchone()
        if row is None:
            return None

        worker = dict(row)

        # Parse metadata_json
        try:
            worker["metadata"] = json.loads(worker.pop("metadata_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            worker["metadata"] = {}
        # Strip internal fingerprint from caller-visible metadata
        worker["metadata"].pop("_idempotency_fingerprint", None)

        # Get capabilities
        cur.execute(
            "SELECT capability FROM agent_capabilities WHERE worker_id = ? ORDER BY capability",
            (worker_id,)
        )
        worker["capabilities"] = [r["capability"] for r in cur.fetchall()]

        return worker

    # ── Idempotency for register ──

    _FINGERPRINT_TABLE = "agent_workers"  # virtual table name for fingerprint storage

    def _check_register_idempotency(
        self,
        conn: sqlite3.Connection,
        worker_id: str,
        worker_type: str,
        provider: str,
        display_name: str,
        capabilities: List[str],
        sandbox_profile_id: str,
        metadata: Dict[str, Any],
        idempotency_key: str,
    ) -> Optional[Dict[str, Any]]:
        """Check idempotency for register_worker.

        Since agent_workers doesn't have an idempotency_key column,
        we store a fingerprint in agent_capabilities metadata or just detect
        duplicate worker_id registration.
        """
        cur = conn.cursor()

        # Check if worker already exists with this idempotency_key
        # We use a convention: store idempotency info in metadata_json
        cur.execute("""
            SELECT worker_id, metadata_json FROM agent_workers
            WHERE worker_id = ?
        """, (worker_id,))
        existing = cur.fetchone()

        if existing is None:
            return None  # not registered yet, proceed

        # Worker exists - compare fingerprint
        try:
            meta = json.loads(existing["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}

        stored_fp = meta.get("_idempotency_fingerprint")
        new_fp = self._build_register_fingerprint(
            worker_id, worker_type, provider, display_name,
            capabilities, sandbox_profile_id, metadata
        )

        if stored_fp is None:
            # No fingerprint stored - treat as idempotent (already registered)
            worker = self._get_worker_dict(conn, worker_id)
            return {
                "success": True, "worker": worker,
                "error": None, "error_code": None,
                "idempotent": True,
            }

        if stored_fp == new_fp:
            # Same request - return cached result
            worker = self._get_worker_dict(conn, worker_id)
            return {
                "success": True, "worker": worker,
                "error": None, "error_code": None,
                "idempotent": True,
            }
        else:
            # Same key, different params - conflict
            return {
                "success": False,
                "error": f"Idempotency key '{idempotency_key}' already used with different params",
                "error_code": ERROR_IDEMPOTENCY_CONFLICT,
            }

    def _build_register_fingerprint(
        self,
        worker_id: str,
        worker_type: str,
        provider: str,
        display_name: str,
        capabilities: List[str],
        sandbox_profile_id: str,
        metadata: Dict[str, Any],
    ) -> str:
        """Build a fingerprint for register idempotency."""
        data = json.dumps({
            "worker_id": worker_id,
            "worker_type": worker_type,
            "provider": provider,
            "display_name": display_name,
            "capabilities": sorted(capabilities),
            "sandbox_profile_id": sandbox_profile_id,
            "metadata": metadata,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()
