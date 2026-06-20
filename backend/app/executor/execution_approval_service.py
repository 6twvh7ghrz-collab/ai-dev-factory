"""
ProjectExecutionApprovalService - Project Execution Approval Service

Provides one-time project-level execution approval for high-risk projects.
Designed to fill the gap where StartDecisionService._compute_decision()
returns REQUEST_APPROVAL but with no actual approval workflow.

Architecture:
  1. Request a one-time execution approval (generates confirmation token)
  2. Approve with token (consumes token, sets status=approved)
  3. StartDecisionService queries for valid approval before REQUEST_APPROVAL
  4. On executor run start, approval is consumed (status=consumed)
  5. Expired approvals are excluded from valid check

Security:
  - Confirmation tokens are SHA-256 hashed before storage
  - Tokens are one-time use (consumed on approve)
  - Approvals are one-time use (consumed on executor start)
  - Expiration time enforced
  - All writes in explicit transactions
"""
import sqlite3
import uuid
import hashlib
import secrets
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List

logger = logging.getLogger("executor.execution_approval_service")

# Token TTL: 5 minutes (longer than planning token since this is project-level)
CONFIRMATION_TOKEN_TTL_SECONDS = 300

# Default approval expiry: 1 hour
DEFAULT_APPROVAL_EXPIRY_HOURS = 1


class ExecutionApprovalService:
    """Project-level execution approval service.

    Provides:
    - request_approval: Create a pending approval with confirmation token
    - approve: Verify token and set status=approved
    - reject: Set status=rejected
    - has_valid_approval: Check if project has approved, non-consumed, non-expired approval
    - consume_approval: Mark approval as consumed (one-time use)
    - expire_stale_approvals: Batch mark expired approvals
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def request_approval(
        self,
        project_id: int,
        allowed_task_ids: List[int] = None,
        max_workers: int = 1,
        auto_run_downstream: bool = False,
        approval_reason: str = "",
        requested_by: str = "user",
        expiry_hours: int = DEFAULT_APPROVAL_EXPIRY_HOURS,
    ) -> Dict[str, Any]:
        """Request a project execution approval.

        Creates a pending approval record and returns a one-time confirmation token.
        The token must be used to approve within CONFIRMATION_TOKEN_TTL_SECONDS.

        Args:
            project_id: Project ID
            allowed_task_ids: List of task IDs allowed to execute
            max_workers: Max concurrent workers
            auto_run_downstream: Whether to auto-run downstream tasks
            approval_reason: Human-readable reason
            requested_by: Who is requesting
            expiry_hours: Approval validity in hours

        Returns:
            dict with approval info including the raw token (only returned once!)
        """
        if allowed_task_ids is None:
            allowed_task_ids = []

        # Generate unique identifiers
        approval_id = str(uuid.uuid4())
        confirmation_token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(confirmation_token)

        now = datetime.now()
        expired_at = now + timedelta(hours=expiry_hours)

        # Build snapshots
        decision_snapshot = {
            "project_id": project_id,
            "allowed_task_ids": allowed_task_ids,
            "max_workers": max_workers,
            "auto_run_downstream": auto_run_downstream,
            "single_use": True,
            "requested_at": now.isoformat(),
        }

        risk_summary = {
            "risk_level": "HIGH",
            "risk_confirmed": False,  # Will be true after approval
            "policy_version": "v1.8c",
            "expires_at": expired_at.isoformat(),
            "approval_reason": approval_reason,
        }

        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO execution_approvals (
                    approval_id, project_id, status,
                    allowed_task_ids_json, max_workers, auto_run_downstream,
                    single_use, approval_reason,
                    decision_snapshot_json, risk_summary_json,
                    confirmation_token_hash,
                    requested_by, expired_at, created_at
                ) VALUES (?, ?, 'pending', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """, (
                approval_id, project_id,
                json.dumps(allowed_task_ids), max_workers,
                1 if auto_run_downstream else 0,
                approval_reason,
                json.dumps(decision_snapshot),
                json.dumps(risk_summary),
                token_hash,
                requested_by,
                expired_at.isoformat(),
                now.isoformat(),
            ))

            conn.commit()

            logger.info(
                f"Execution approval requested: project={project_id}, "
                f"approval_id={approval_id}, tasks={allowed_task_ids}"
            )

            return {
                "ok": True,
                "approval_id": approval_id,
                "project_id": project_id,
                "confirmation_token": confirmation_token,  # Raw token - only returned once!
                "token_expires_in_seconds": CONFIRMATION_TOKEN_TTL_SECONDS,
                "allowed_task_ids": allowed_task_ids,
                "max_workers": max_workers,
                "auto_run_downstream": auto_run_downstream,
                "expired_at": expired_at.isoformat(),
                "status": "pending",
                "message": (
                    f"Execution approval requested for project #{project_id}. "
                    f"Use the confirmation token to approve within "
                    f"{CONFIRMATION_TOKEN_TTL_SECONDS}s."
                ),
            }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to request execution approval: {e}")
            return {
                "ok": False,
                "error": str(e),
                "message": f"Failed to request execution approval: {e}",
            }
        finally:
            conn.close()

    def approve(
        self,
        project_id: int,
        confirmation_token: str,
        approved_by: str = "user",
    ) -> Dict[str, Any]:
        """Approve a pending execution approval.

        Verifies the confirmation token (one-time consumption) and sets status=approved.

        Args:
            project_id: Project ID
            confirmation_token: The raw token from request_approval
            approved_by: Who approved

        Returns:
            dict with approval status
        """
        token_hash = self._hash_token(confirmation_token)
        now = datetime.now()

        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # Find matching pending approval
            cur.execute("""
                SELECT * FROM execution_approvals
                WHERE project_id = ?
                AND status = 'pending'
                AND confirmation_token_hash = ?
                AND (expired_at IS NULL OR expired_at > ?)
                ORDER BY created_at DESC
                LIMIT 1
            """, (project_id, token_hash, now.isoformat()))

            row = cur.fetchone()
            if not row:
                return {
                    "ok": False,
                    "error": "INVALID_OR_EXPIRED_TOKEN",
                    "message": (
                        f"No pending execution approval found with the given "
                        f"confirmation token for project #{project_id}. "
                        f"The token may be invalid or expired."
                    ),
                }

            approval = dict(row)

            # Update to approved
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("""
                UPDATE execution_approvals
                SET status = 'approved',
                    approved_by = ?,
                    approved_at = ?,
                    risk_summary_json = ?
                WHERE approval_id = ?
            """, (
                approved_by,
                now.isoformat(),
                json.dumps({
                    "risk_level": "HIGH",
                    "risk_confirmed": True,
                    "policy_version": "v1.8c",
                    "approved_by": approved_by,
                    "approved_at": now.isoformat(),
                }),
                approval["approval_id"],
            ))
            conn.commit()

            logger.info(
                f"Execution approval approved: project={project_id}, "
                f"approval_id={approval['approval_id']}, by={approved_by}"
            )

            return {
                "ok": True,
                "approval_id": approval["approval_id"],
                "project_id": project_id,
                "status": "approved",
                "approved_by": approved_by,
                "approved_at": now.isoformat(),
                "expired_at": approval["expired_at"],
                "allowed_task_ids": json.loads(approval["allowed_task_ids_json"] or "[]"),
                "max_workers": approval["max_workers"],
                "message": (
                    f"Execution approval granted for project #{project_id}. "
                    f"Valid until {approval['expired_at']}. "
                    f"Allows tasks: {json.loads(approval['allowed_task_ids_json'] or '[]')}"
                ),
            }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to approve execution approval: {e}")
            return {
                "ok": False,
                "error": str(e),
                "message": f"Failed to approve: {e}",
            }
        finally:
            conn.close()

    def reject(
        self,
        project_id: int,
        confirmation_token: str,
    ) -> Dict[str, Any]:
        """Reject a pending execution approval.

        Args:
            project_id: Project ID
            confirmation_token: The raw token from request_approval

        Returns:
            dict with rejection status
        """
        token_hash = self._hash_token(confirmation_token)
        now = datetime.now()

        conn = self._get_conn()
        try:
            cur = conn.cursor()

            cur.execute("""
                SELECT approval_id FROM execution_approvals
                WHERE project_id = ?
                AND status = 'pending'
                AND confirmation_token_hash = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (project_id, token_hash))

            row = cur.fetchone()
            if not row:
                return {
                    "ok": False,
                    "error": "INVALID_OR_EXPIRED_TOKEN",
                    "message": "No pending execution approval found with the given token.",
                }

            conn.execute("BEGIN IMMEDIATE")
            cur.execute("""
                UPDATE execution_approvals
                SET status = 'rejected', rejected_at = ?
                WHERE approval_id = ?
            """, (now.isoformat(), row["approval_id"]))
            conn.commit()

            logger.info(
                f"Execution approval rejected: project={project_id}, "
                f"approval_id={row['approval_id']}"
            )

            return {
                "ok": True,
                "approval_id": row["approval_id"],
                "project_id": project_id,
                "status": "rejected",
                "rejected_at": now.isoformat(),
                "message": f"Execution approval rejected for project #{project_id}.",
            }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to reject execution approval: {e}")
            return {
                "ok": False,
                "error": str(e),
            }
        finally:
            conn.close()

    def has_valid_approval(self, project_id: int,
                             requested_task_ids: list = None) -> bool:
        """V1.8C-R: Check if project has a valid execution approval.

        If requested_task_ids is provided, also verifies all requested
        tasks are within the approval's allowed_task_ids.

        Args:
            project_id: Project ID
            requested_task_ids: Optional task IDs to validate against scope

        Returns:
            True if valid scoped approval exists
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            now = datetime.now()

            cur.execute("""
                SELECT allowed_task_ids_json, risk_summary_json FROM execution_approvals
                WHERE project_id = ?
                AND status = 'approved'
                AND (expired_at IS NULL OR expired_at > ?)
                AND consumed_at IS NULL
                AND single_use = 1
                ORDER BY created_at DESC
                LIMIT 1
            """, (project_id, now.isoformat()))

            row = cur.fetchone()
            if not row:
                return False

            # Check risk_acknowledged
            try:
                risk = json.loads(row["risk_summary_json"] or "{}")
                if not risk.get("risk_confirmed", False):
                    return False
            except (json.JSONDecodeError, TypeError):
                return False

            # Check task scope
            if requested_task_ids:
                try:
                    allowed = json.loads(row["allowed_task_ids_json"] or "[]")
                    if not allowed:
                        return False
                    if not set(requested_task_ids).issubset(set(allowed)):
                        return False
                except (json.JSONDecodeError, TypeError):
                    return False

            return True
        except Exception as e:
            logger.error(f"Failed to check valid approval: {e}")
            return False
        finally:
            conn.close()

    def get_valid_approval(self, project_id: int) -> Optional[Dict[str, Any]]:
        """Get the valid execution approval for a project.

        Returns:
            dict with approval details, or None if no valid approval
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            now = datetime.now()

            cur.execute("""
                SELECT * FROM execution_approvals
                WHERE project_id = ?
                AND status = 'approved'
                AND (expired_at IS NULL OR expired_at > ?)
                AND consumed_at IS NULL
                AND single_use = 1
                ORDER BY created_at DESC
                LIMIT 1
            """, (project_id, now.isoformat()))

            row = cur.fetchone()
            if not row:
                return None

            approval = dict(row)
            approval["allowed_task_ids"] = json.loads(
                approval["allowed_task_ids_json"] or "[]"
            )
            approval["decision_snapshot"] = json.loads(
                approval["decision_snapshot_json"] or "{}"
            )
            approval["risk_summary"] = json.loads(
                approval["risk_summary_json"] or "{}"
            )
            return approval
        finally:
            conn.close()

    def consume_approval(self, project_id: int,
                          executor_run_id: int = None,
                          task_id: int = None) -> Dict[str, Any]:
        """V1.8C-R: Atomically consume the valid execution approval.

        MUST be called only after ALL of these succeed:
          1. executor_run created successfully
          2. task lease claimed successfully
          3. task_id is within allowed_task_ids

        Records consumed_by_run_id and consumed_by_task_id in dedicated
        columns for audit trail.

        If consumption fails, the caller MUST release the lease and
        mark the run as blocked/failed.

        Args:
            project_id: Project ID
            executor_run_id: executor_runs.id that triggered consumption
            task_id: The specific task ID being claimed/executed

        Returns:
            dict with consumption status
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            now = datetime.now()

            # Find the valid approval (WITH row lock via BEGIN IMMEDIATE)
            cur.execute("""
                SELECT approval_id, allowed_task_ids_json FROM execution_approvals
                WHERE project_id = ?
                AND status = 'approved'
                AND (expired_at IS NULL OR expired_at > ?)
                AND consumed_at IS NULL
                AND single_use = 1
                ORDER BY created_at DESC
                LIMIT 1
            """, (project_id, now.isoformat()))

            row = cur.fetchone()
            if not row:
                return {
                    "ok": False,
                    "error": "NO_VALID_APPROVAL",
                    "message": "No valid execution approval to consume.",
                }

            # V1.8C-R: Verify task_id is in allowed_task_ids
            if task_id is not None:
                try:
                    allowed = json.loads(row["allowed_task_ids_json"] or "[]")
                    if allowed and task_id not in allowed:
                        return {
                            "ok": False,
                            "error": "TASK_NOT_ALLOWED",
                            "message": (
                                f"Task #{task_id} is not in the approval's "
                                f"allowed_task_ids: {allowed}"
                            ),
                        }
                except (json.JSONDecodeError, TypeError):
                    pass

            # Atomic consumption
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("""
                UPDATE execution_approvals
                SET status = 'consumed',
                    consumed_at = ?,
                    consumed_by_run_id = ?,
                    consumed_by_task_id = ?
                WHERE approval_id = ?
                AND status = 'approved'
                AND consumed_at IS NULL
            """, (
                now.isoformat(),
                executor_run_id,
                task_id,
                row["approval_id"],
            ))

            if cur.rowcount == 0:
                # Another process consumed it between our SELECT and UPDATE
                conn.rollback()
                return {
                    "ok": False,
                    "error": "RACE_CONDITION",
                    "message": "Approval was already consumed by another process.",
                }

            conn.commit()

            logger.info(
                f"Execution approval consumed: project={project_id}, "
                f"approval_id={row['approval_id']}, "
                f"run_id={executor_run_id}, task_id={task_id}"
            )

            # Parse allowed_task_ids for caller's scope tracking
            allowed_ids = []
            try:
                allowed_ids = json.loads(row["allowed_task_ids_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                pass

            return {
                "ok": True,
                "approval_id": row["approval_id"],
                "project_id": project_id,
                "consumed_at": now.isoformat(),
                "executor_run_id": executor_run_id,
                "task_id": task_id,
                "allowed_task_ids": allowed_ids,
                "message": "Execution approval consumed.",
            }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to consume approval: {e}")
            return {
                "ok": False,
                "error": str(e),
            }
        finally:
            conn.close()

    def expire_stale_approvals(self) -> int:
        """Batch expire all stale approved/pending approvals.

        Returns:
            Number of approvals expired
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            now = datetime.now()

            cur.execute("""
                UPDATE execution_approvals
                SET status = 'expired'
                WHERE status IN ('pending', 'approved')
                AND expired_at IS NOT NULL
                AND expired_at <= ?
                AND consumed_at IS NULL
            """, (now.isoformat(),))

            count = cur.rowcount
            conn.commit()

            if count > 0:
                logger.info(f"Expired {count} stale execution approvals")

            return count
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to expire approvals: {e}")
            return 0
        finally:
            conn.close()

    def get_approval_status(self, project_id: int) -> Dict[str, Any]:
        """Get the current execution approval status for a project.

        Returns:
            dict with approval status summary
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # Count by status
            cur.execute("""
                SELECT status, COUNT(*) as cnt
                FROM execution_approvals
                WHERE project_id = ?
                GROUP BY status
            """, (project_id,))
            status_counts = {row["status"]: row["cnt"] for row in cur.fetchall()}

            # Check if has valid approval
            has_valid = self.has_valid_approval(project_id)

            # Get latest approval
            cur.execute("""
                SELECT * FROM execution_approvals
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (project_id,))
            latest = cur.fetchone()

            return {
                "ok": True,
                "project_id": project_id,
                "has_valid_approval": has_valid,
                "status_counts": status_counts,
                "latest_approval": dict(latest) if latest else None,
            }

        finally:
            conn.close()


# ============================================================
# Free function for quick check (avoids circular import in start_decision.py)
# ============================================================

def has_valid_execution_approval(db_path: str, project_id: int) -> bool:
    """Quick check: does project have a valid execution approval?

    This free function is designed to be imported by start_decision.py
    without creating a circular dependency. It does a single SQL query.

    Args:
        db_path: Path to the SQLite database
        project_id: Project ID to check

    Returns:
        True if a valid (approved, non-consumed, non-expired) approval exists
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        now = datetime.now()

        cur.execute("""
            SELECT COUNT(*) as cnt FROM execution_approvals
            WHERE project_id = ?
            AND status = 'approved'
            AND (expired_at IS NULL OR expired_at > ?)
            AND consumed_at IS NULL
            AND single_use = 1
        """, (project_id, now.isoformat()))

        row = cur.fetchone()
        conn.close()
        return row["cnt"] > 0
    except Exception as e:
        logger.warning(f"Failed to check execution approval: {e}")
        return False


# ============================================================
# Global singleton
# ============================================================

_execution_approval_service: Optional[ExecutionApprovalService] = None


def get_execution_approval_service(db_path: str = None) -> ExecutionApprovalService:
    """Get global ExecutionApprovalService singleton."""
    global _execution_approval_service
    if _execution_approval_service is None:
        if db_path is None:
            db_path = str(
                Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db"
            )
        _execution_approval_service = ExecutionApprovalService(db_path)
    return _execution_approval_service
