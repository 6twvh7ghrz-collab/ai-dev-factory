"""V2 Worker Control Plane API Router.

V2.0-B2e: Exposes Worker registration, Task claim, and Heartbeat endpoints
under the /api/v2 prefix.

All endpoints require the Idempotency-Key header and respect the
V2_CONTROL_PLANE_ENABLED feature flag.
"""

from __future__ import annotations

import uuid
import os
import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app.schemas.v2_worker import (
    RegisterWorkerRequest, ClaimTaskRequest, HeartbeatRequest,
    SubmitTaskResultRequest, ReviewTaskRequest, HandoffTaskRequest,
    RegisterWorkerResponse, ClaimTaskResponse, HeartbeatResponse,
    SubmitTaskResultResponse, ReviewTaskResponse, HandoffTaskResponse,
    V2ErrorResponse,
    _WorkerInfo,
)

# ── Lazy-imported services (created per-request to pick up env changes) ──
from app.supervisor.worker_registry import (
    WorkerRegistryService,
    ERROR_V2_CONTROL_PLANE_DISABLED as _E_DISABLED,
    ERROR_INVALID_WORKER_TYPE,
    ERROR_IDEMPOTENCY_CONFLICT as _WR_IDEM_CONFLICT,
    ERROR_WORKER_ALREADY_REGISTERED,
    ERROR_EXECUTOR_CONCURRENCY_LIMIT,
    ERROR_INVALID_WORKER_STATUS,
)
from app.supervisor.task_claim_service import (
    TaskClaimService,
    ERROR_TASK_NOT_FOUND,
    ERROR_TASK_NOT_CLAIMABLE,
    ERROR_TASK_SCOPE_VIOLATION,
    ERROR_STATE_VERSION_CONFLICT,
    ERROR_LEASE_CONFLICT,
    ERROR_IDEMPOTENCY_CONFLICT,
    ERROR_VALIDATION_ERROR,
    ERROR_INTERNAL_ERROR,
    ERROR_WORKER_NOT_REGISTERED,
    ERROR_WORKER_NOT_AVAILABLE,
    ERROR_WORKER_CAPABILITY_MISMATCH,
    ERROR_WORKER_TYPE_NOT_ALLOWED,
)
from app.supervisor.worker_heartbeat_service import (
    WorkerHeartbeatService,
    ERROR_ASSIGNMENT_NOT_FOUND,
    ERROR_STALE_LEASE,
)
from app.supervisor.task_result_submission_service import (
    TaskResultSubmissionService,
    ERROR_TASK_NOT_SUBMITTABLE,
    ERROR_RESULT_PACKET_INVALID,
    ERROR_ARTIFACT_INVALID,
)
from app.supervisor.task_review_service import TaskReviewService
from app.supervisor.task_handoff_service import TaskHandoffService

logger = logging.getLogger(__name__)

router = APIRouter()

ERROR_DATABASE_CONFIG_INVALID = "DATABASE_CONFIG_INVALID"


class DatabaseConfigError(ValueError):
    """Raised when the configured database URL cannot be used safely."""


# ============================================================
# Error code → HTTP status mapping
# ============================================================

_ERROR_STATUS: Dict[str, int] = {
    "V2_CONTROL_PLANE_DISABLED":   503,
    "VALIDATION_ERROR":            422,
    "WORKER_NOT_REGISTERED":       404,
    "ASSIGNMENT_NOT_FOUND":        404,
    "TASK_NOT_FOUND":              404,
    "WORKER_NOT_AVAILABLE":        409,
    "WORKER_TYPE_NOT_ALLOWED":     403,
    "WORKER_CAPABILITY_MISMATCH":  409,
    "TASK_NOT_CLAIMABLE":          409,
    "TASK_SCOPE_VIOLATION":        403,
    "STATE_VERSION_CONFLICT":      409,
    "LEASE_CONFLICT":              409,
    "STALE_LEASE":                 409,
    "IDEMPOTENCY_CONFLICT":        409,
    "INTERNAL_ERROR":              500,
    "INVALID_WORKER_TYPE":         409,
    "WORKER_ALREADY_REGISTERED":   409,
    "EXECUTOR_CONCURRENCY_LIMIT":  409,
    "REGISTRATION_FAILED":         500,
    ERROR_DATABASE_CONFIG_INVALID: 500,
    "TASK_NOT_SUBMITTABLE":        409,
    "RESULT_PACKET_INVALID":       422,
    "ARTIFACT_INVALID":            422,
    "REVIEWER_NOT_REGISTERED":     404,
    "REVIEWER_NOT_AVAILABLE":      409,
    "REVIEWER_TYPE_NOT_ALLOWED":   403,
    "RESULT_NOT_FOUND":            404,
    "RESULT_NOT_REVIEWABLE":       409,
    "TASK_NOT_REVIEWABLE":         409,
    "REVIEW_CONFLICT":             409,
    "EVIDENCE_INVALID":            422,
    "DECISION_INVALID":            422,
    "HANDOFF_NOT_FOUND":           404,
    "HANDOFF_NOT_ALLOWED":         409,
    "HANDOFF_CONFLICT":            409,
    "HANDOFF_EXPIRED":             409,
}


def _status_for(error_code: Optional[str]) -> int:
    """Map a V2 error code to an HTTP status code."""
    if error_code is None:
        return 200
    return _ERROR_STATUS.get(error_code, 500)


# ============================================================
# Helpers
# ============================================================

def _get_db_path() -> str:
    """Resolve the SQLite filesystem path from env, falling back to settings."""
    raw = os.getenv("DATABASE_URL")
    if raw is None or raw.strip() == "":
        from app.core.config import settings
        raw = getattr(settings, "DATABASE_URL", "") or ""
    return _parse_sqlite_db_path(str(raw))


def _parse_sqlite_db_path(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise DatabaseConfigError("Database configuration is invalid")

    normalize_backslashes = False
    if value.startswith("sqlite:///"):
        path = value[len("sqlite:///"):]
        normalize_backslashes = True
    elif value.startswith("sqlite:"):
        raise DatabaseConfigError("Database configuration is invalid")
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        raise DatabaseConfigError("Database configuration is invalid")
    else:
        path = value

    if not path or path.strip() == "":
        raise DatabaseConfigError("Database configuration is invalid")
    if normalize_backslashes and "\\" in path:
        path = path.replace("\\\\", "\\")
    return path


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:16]}"


def _v2_enabled() -> bool:
    """Read V2_CONTROL_PLANE_ENABLED at call time (not import time)."""
    return os.getenv("V2_CONTROL_PLANE_ENABLED", "false").lower() in ("true", "1")


def _v2_api_gate(request_id: str) -> Optional[JSONResponse]:
    """Return 503 error if V2 control plane is disabled."""
    if not _v2_enabled():
        return _error_response(
            "V2_CONTROL_PLANE_DISABLED",
            "V2 control plane is disabled; set V2_CONTROL_PLANE_ENABLED=true",
            request_id,
        )
    return None


def _error_response(
    error_code: str,
    message: str,
    request_id: str,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    """Build a unified V2 error JSONResponse."""
    status = _status_for(error_code)
    body = {
        "ok": False,
        "error_code": error_code,
        "message": _sanitize_error_message(message),
        "request_id": request_id,
        "details": details or {},
    }
    return JSONResponse(status_code=status, content=body)


def _sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to remove internal paths, SQL, and tokens."""
    msg = str(msg or "")
    # Redact common absolute path shapes without resolving database config.
    msg = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "<PATH>", msg)
    msg = re.sub(r"(?<!:)/[^\s\"'<>]+", "<PATH>", msg)
    # Redact database URLs and filenames.
    msg = re.sub(r"\b\w+://[^\s\"'<>]+", "<URL>", msg)
    msg = re.sub(r"[\w .-]+\.db\b", "<DATABASE>", msg, flags=re.IGNORECASE)
    if any(term in msg.lower() for term in ("sqlite", "select ", "insert ", "update ", "delete ", "traceback")):
        msg = "Internal error"
    # Redact lease_token from error (64-char hex pattern)
    msg = re.sub(r'[0-9a-fA-F]{64}', '***REDACTED***', msg)
    return msg


def _database_config_error_response(request_id: str) -> JSONResponse:
    return _error_response(
        ERROR_DATABASE_CONFIG_INVALID,
        "Database configuration is invalid",
        request_id,
    )


def _extract_idempotency_key(headers: dict) -> Optional[str]:
    """Extract Idempotency-Key from request headers (case-insensitive)."""
    for k, v in headers.items():
        if k.lower() == "idempotency-key":
            return v
    return None


# ============================================================
# POST /api/v2/workers/register
# ============================================================

@router.post(
    "/api/v2/workers/register",
    response_model=RegisterWorkerResponse,
    summary="Register a V2 worker",
    responses={
        201: {"description": "Worker registered successfully"},
        200: {"description": "Idempotent repeat"},
        400: {"description": "Missing Idempotency-Key"},
        422: {"description": "Validation error"},
        409: {"description": "Conflict"},
        503: {"description": "V2 control plane disabled"},
    },
)
async def register_worker(
    request: Request,
    body: RegisterWorkerRequest,
):
    request_id = _new_request_id()

    # ── Feature flag ──
    gate = _v2_api_gate(request_id)
    if gate:
        return gate

    # ── Idempotency-Key ──
    idem_key = request.headers.get("Idempotency-Key", "")
    if not idem_key:
        return _error_response(
            "VALIDATION_ERROR",
            "Idempotency-Key header is required",
            request_id,
        )

    try:
        db_path = _get_db_path()
        svc = WorkerRegistryService(db_path, v2_enabled=True)
        result = svc.register_worker(
            worker_id=body.worker_id,
            worker_type=body.worker_type,
            provider=body.provider,
            display_name=body.display_name,
            capabilities=body.capabilities,
            sandbox_profile_id=body.sandbox_profile_id,
            metadata=body.metadata,
            idempotency_key=idem_key,
        )
    except DatabaseConfigError:
        return _database_config_error_response(request_id)
    except Exception as exc:
        logger.exception("register_worker failed")
        return _error_response("INTERNAL_ERROR", str(exc), request_id)

    if not result.get("success"):
        return _error_response(
            result.get("error_code") or "INTERNAL_ERROR",
            result.get("error") or result.get("error_message") or "Unknown error",
            request_id,
        )

    # ── Build safe worker info (strip internal fingerprints) ──
    worker_raw = result.get("worker") or {}
    safe_worker = _worker_info_safe(worker_raw)

    status_code = 201 if not result.get("idempotent") else 200

    return JSONResponse(
        status_code=status_code,
        content={
            "ok": True,
            "worker": safe_worker,
            "idempotent": bool(result.get("idempotent")),
            "request_id": request_id,
        },
    )


def _worker_info_safe(worker_raw: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal fields from worker info for external exposure."""
    safe = dict(worker_raw)
    # Remove internal fingerprint
    meta = safe.get("metadata", {})
    if isinstance(meta, dict):
        meta.pop("_idempotency_fingerprint", None)
        safe["metadata"] = meta
    # Remove raw _idempotency_key if leaked
    safe.pop("_idempotency_key", None)
    return safe


# ============================================================
# POST /api/v2/tasks/{task_id}/claim
# ============================================================

@router.post(
    "/api/v2/tasks/{task_id}/claim",
    response_model=ClaimTaskResponse,
    summary="Claim a task for execution",
    responses={
        200: {"description": "Task claimed successfully"},
        403: {"description": "Task scope violation"},
        404: {"description": "Worker or task not found"},
        409: {"description": "Conflict (not claimable, version, worker, idempotency)"},
        422: {"description": "Validation error"},
        503: {"description": "V2 control plane disabled"},
    },
)
async def claim_task(
    task_id: int,
    request: Request,
    body: ClaimTaskRequest,
):
    request_id = _new_request_id()

    # ── Feature flag ──
    gate = _v2_api_gate(request_id)
    if gate:
        return gate

    # ── Idempotency-Key ──
    idem_key = request.headers.get("Idempotency-Key", "")
    if not idem_key:
        return _error_response(
            "VALIDATION_ERROR",
            "Idempotency-Key header is required",
            request_id,
        )

    try:
        db_path = _get_db_path()
        svc = TaskClaimService(db_path, v2_enabled=True)
        result = svc.claim_task(
            task_id=task_id,
            worker_id=body.worker_id,
            expected_version=body.expected_version,
            idempotency_key=idem_key,
            lease_seconds=body.lease_seconds,
            allowed_task_ids=body.allowed_task_ids,
            project_id=body.project_id,
        )
    except DatabaseConfigError:
        return _database_config_error_response(request_id)
    except Exception as exc:
        logger.exception("claim_task failed")
        return _error_response("INTERNAL_ERROR", str(exc), request_id)

    if not result.get("success"):
        return _error_response(
            result.get("error_code") or "INTERNAL_ERROR",
            result.get("error_message") or result.get("error") or "Unknown error",
            request_id,
        )

    # ── Security: idempotent claim must not reissue lease_token ──
    idempotent = bool(result.get("idempotent"))
    task_packet = result.get("task_packet")

    if idempotent:
        # For idempotent responses, do NOT return the lease_token.
        # Set lease_token_reissued=false to indicate the token was not re-issued.
        response_content = {
            "ok": True,
            "assignment_id": result.get("assignment_id"),
            "task_id": task_id,
            "worker_id": body.worker_id,
            "lease_token": None,
            "lease_token_reissued": False,
            "lease_expires_at": result.get("lease_expires_at"),
            "task_packet": task_packet,
            "state_version": result.get("state_version"),
            "idempotent": True,
            "request_id": request_id,
        }
    else:
        # First claim — return the lease_token
        if task_packet and isinstance(task_packet, dict):
            task_packet.pop("lease_token", None)

        response_content = {
            "ok": True,
            "assignment_id": result.get("assignment_id"),
            "task_id": task_id,
            "worker_id": body.worker_id,
            "lease_token": result.get("lease_token"),
            "lease_token_reissued": True,
            "lease_expires_at": result.get("lease_expires_at"),
            "task_packet": _sanitize_task_packet(task_packet),
            "state_version": result.get("state_version"),
            "idempotent": False,
            "request_id": request_id,
        }

    return JSONResponse(status_code=200, content=response_content)


def _sanitize_task_packet(packet: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Remove any internal fields from task_packet for external exposure."""
    if packet is None:
        return None
    safe = dict(packet)
    # lease_token is in the packet for executor use but should not leak
    # via logs; it IS sent to the executor in the first claim, so keep it
    # but ensure no internal fingerprints leak
    safe.pop("_claim_fingerprint", None)
    return safe


# ============================================================
# POST /api/v2/tasks/{task_id}/heartbeat
# ============================================================

@router.post(
    "/api/v2/tasks/{task_id}/heartbeat",
    response_model=HeartbeatResponse,
    summary="Renew lease on a claimed task",
    responses={
        200: {"description": "Heartbeat processed"},
        404: {"description": "Assignment or worker not found"},
        409: {"description": "Conflict (lease token, worker, expired, idempotency)"},
        422: {"description": "Validation error"},
        503: {"description": "V2 control plane disabled"},
    },
)
async def heartbeat(
    task_id: int,
    request: Request,
    body: HeartbeatRequest,
):
    request_id = _new_request_id()

    # ── Feature flag ──
    gate = _v2_api_gate(request_id)
    if gate:
        return gate

    # ── Idempotency-Key ──
    idem_key = request.headers.get("Idempotency-Key", "")
    if not idem_key:
        return _error_response(
            "VALIDATION_ERROR",
            "Idempotency-Key header is required",
            request_id,
        )

    try:
        db_path = _get_db_path()
        svc = WorkerHeartbeatService(db_path, v2_enabled=True)
        result = svc.heartbeat(
            task_id=task_id,
            assignment_id=body.assignment_id,
            worker_id=body.worker_id,
            lease_token=body.lease_token,
            idempotency_key=idem_key,
            extend_seconds=body.extend_seconds,
        )
    except DatabaseConfigError:
        return _database_config_error_response(request_id)
    except Exception as exc:
        logger.exception("heartbeat failed")
        return _error_response("INTERNAL_ERROR", str(exc), request_id)

    if not result.get("success"):
        return _error_response(
            result.get("error_code") or "INTERNAL_ERROR",
            result.get("error_message") or result.get("error") or "Unknown error",
            request_id,
        )

    # ── Build response — NEVER include lease_token ──
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "heartbeat_id": result.get("heartbeat_id"),
            "assignment_id": result.get("assignment_id"),
            "task_id": task_id,
            "worker_id": body.worker_id,
            "previous_expires_at": result.get("previous_expires_at"),
            "lease_expires_at": result.get("lease_expires_at"),
            "worker_last_seen_at": result.get("worker_last_seen_at"),
            "idempotent": bool(result.get("idempotent")),
            "request_id": request_id,
        },
    )


# ============================================================
# POST /api/v2/tasks/{task_id}/submit
# ============================================================

@router.post(
    "/api/v2/tasks/{task_id}/submit",
    response_model=SubmitTaskResultResponse,
    summary="Submit a task result packet for review",
    responses={
        200: {"description": "Task result submitted"},
        403: {"description": "Worker type or task scope violation"},
        404: {"description": "Worker or assignment not found"},
        409: {"description": "Lease, state version, task state, or idempotency conflict"},
        422: {"description": "Validation or result packet error"},
        503: {"description": "V2 control plane disabled"},
    },
)
async def submit_task_result(
    task_id: int,
    request: Request,
    body: SubmitTaskResultRequest,
):
    request_id = _new_request_id()

    gate = _v2_api_gate(request_id)
    if gate:
        return gate

    idem_key = request.headers.get("Idempotency-Key", "")
    if not idem_key:
        return _error_response(
            "VALIDATION_ERROR",
            "Idempotency-Key header is required",
            request_id,
        )

    if hasattr(body, "model_dump"):
        result_packet = body.model_dump()
    else:
        result_packet = body.dict()
    lease_token = result_packet.pop("lease_token")
    expected_version = result_packet.pop("expected_version")
    assignment_id = result_packet.get("assignment_id")
    worker_id = result_packet.get("worker_id")
    result_packet["task_id"] = task_id

    try:
        db_path = _get_db_path()
        svc = TaskResultSubmissionService(db_path, v2_enabled=True)
        result = svc.submit_result(
            task_id=task_id,
            assignment_id=assignment_id,
            worker_id=worker_id,
            lease_token=lease_token,
            expected_version=expected_version,
            idempotency_key=idem_key,
            result_packet=result_packet,
        )
    except DatabaseConfigError:
        return _database_config_error_response(request_id)
    except Exception as exc:
        logger.exception("submit_task_result failed")
        return _error_response("INTERNAL_ERROR", str(exc), request_id)

    if not result.get("success"):
        return _error_response(
            result.get("error_code") or "INTERNAL_ERROR",
            result.get("error_message") or "Unknown error",
            request_id,
        )

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "result_id": result.get("result_id"),
            "task_id": result.get("task_id"),
            "assignment_id": result.get("assignment_id"),
            "task_state": result.get("task_state"),
            "state_version": result.get("state_version"),
            "assignment_status": result.get("assignment_status"),
            "worker_status": result.get("worker_status"),
            "artifact_count": result.get("artifact_count"),
            "idempotent": bool(result.get("idempotent")),
            "result_summary": result.get("result_summary") or {},
            "request_id": request_id,
        },
    )


# ============================================================
# POST /api/v2/tasks/{task_id}/review
# ============================================================

@router.post(
    "/api/v2/tasks/{task_id}/review",
    response_model=ReviewTaskResponse,
    summary="Begin review or submit a reviewer decision",
)
async def review_task(
    task_id: int,
    request: Request,
    body: ReviewTaskRequest,
):
    request_id = _new_request_id()

    gate = _v2_api_gate(request_id)
    if gate:
        return gate

    idem_key = request.headers.get("Idempotency-Key", "")
    if not idem_key:
        return _error_response(
            "VALIDATION_ERROR",
            "Idempotency-Key header is required",
            request_id,
        )

    try:
        db_path = _get_db_path()
        svc = TaskReviewService(db_path, v2_enabled=True)
        action = (body.action or "decide").lower()
        if action == "begin":
            result = svc.begin_review(
                task_id=task_id,
                result_id=body.result_id,
                reviewer_id=body.reviewer_id,
                expected_version=body.expected_version,
                idempotency_key=idem_key,
            )
        elif action == "decide":
            result = svc.submit_decision(
                task_id=task_id,
                result_id=body.result_id,
                reviewer_id=body.reviewer_id,
                expected_version=body.expected_version,
                decision=body.decision or "",
                summary=body.summary,
                issues=body.issues,
                evidence_refs=body.evidence_refs,
                idempotency_key=idem_key,
                risk_level=body.risk_level,
                user_action_required=body.user_action_required,
                metadata=body.metadata,
            )
        else:
            return _error_response("VALIDATION_ERROR", "action must be begin or decide", request_id)
    except DatabaseConfigError:
        return _database_config_error_response(request_id)
    except Exception as exc:
        logger.exception("review_task failed")
        return _error_response("INTERNAL_ERROR", str(exc), request_id)

    if not result.get("success"):
        return _error_response(
            result.get("error_code") or "INTERNAL_ERROR",
            result.get("error_message") or "Unknown error",
            request_id,
        )

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "decision_id": result.get("decision_id"),
            "task_id": result.get("task_id"),
            "result_id": result.get("result_id"),
            "reviewer_id": result.get("reviewer_id"),
            "previous_state": result.get("previous_state"),
            "task_state": result.get("task_state"),
            "state_version": result.get("state_version"),
            "decision": result.get("decision"),
            "summary": result.get("summary"),
            "idempotent": bool(result.get("idempotent")),
            "request_id": request_id,
        },
    )


# ============================================================
# POST /api/v2/tasks/{task_id}/handoff
# ============================================================

@router.post(
    "/api/v2/tasks/{task_id}/handoff",
    response_model=HandoffTaskResponse,
    summary="Request, accept, reject, cancel, or expire a V2 task handoff",
)
async def handoff_task(
    task_id: int,
    request: Request,
    body: HandoffTaskRequest,
):
    request_id = _new_request_id()

    gate = _v2_api_gate(request_id)
    if gate:
        return gate

    idem_key = request.headers.get("Idempotency-Key", "")
    if not idem_key:
        return _error_response(
            "VALIDATION_ERROR",
            "Idempotency-Key header is required",
            request_id,
        )

    try:
        db_path = _get_db_path()
        svc = TaskHandoffService(db_path, v2_enabled=True)
        action = (body.action or "").lower()
        if action == "request":
            result = svc.request_handoff(
                task_id=task_id,
                assignment_id=body.assignment_id,
                from_worker_id=body.from_worker_id,
                lease_token=body.lease_token,
                reason_code=body.reason_code,
                reason=body.reason,
                completed_steps=body.completed_steps,
                remaining_steps=body.remaining_steps,
                recent_errors=body.recent_errors,
                evidence_refs=body.evidence_refs,
                forbidden_actions=body.forbidden_actions,
                idempotency_key=idem_key,
                files_changed=body.files_changed,
                tests_run=body.tests_run,
                context_snapshot=body.context_snapshot,
                git_head=body.git_head,
                current_stage=body.current_stage,
                expires_seconds=body.expires_seconds,
            )
        elif action == "accept":
            if body.expected_version is None:
                return _error_response("VALIDATION_ERROR", "expected_version is required", request_id)
            result = svc.accept_handoff(
                handoff_id=body.handoff_id or "",
                to_worker_id=body.to_worker_id or body.worker_id,
                expected_version=body.expected_version,
                idempotency_key=idem_key,
                lease_seconds=body.lease_seconds,
            )
        elif action == "reject":
            result = svc.reject_handoff(
                handoff_id=body.handoff_id or "",
                worker_id=body.worker_id or body.to_worker_id,
                reason=body.reason,
                idempotency_key=idem_key,
            )
        elif action == "cancel":
            result = svc.cancel_handoff(
                handoff_id=body.handoff_id or "",
                actor_id=body.actor_id or body.from_worker_id or body.worker_id,
                reason=body.reason,
                idempotency_key=idem_key,
            )
        elif action == "expire":
            result = svc.expire_handoffs(idempotency_key=idem_key)
        else:
            return _error_response("VALIDATION_ERROR", "action must be request, accept, reject, cancel, or expire", request_id)
    except DatabaseConfigError:
        return _database_config_error_response(request_id)
    except Exception as exc:
        logger.exception("handoff_task failed")
        return _error_response("INTERNAL_ERROR", str(exc), request_id)

    if not result.get("success"):
        return _error_response(
            result.get("error_code") or "INTERNAL_ERROR",
            result.get("error_message") or "Unknown error",
            request_id,
        )

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "handoff_id": result.get("handoff_id"),
            "task_id": result.get("task_id") or task_id,
            "project_id": result.get("project_id"),
            "status": result.get("status"),
            "from_worker_id": result.get("from_worker_id"),
            "to_worker_id": result.get("to_worker_id"),
            "assignment_id": result.get("assignment_id"),
            "lease_expires_at": result.get("lease_expires_at"),
            "expired_count": result.get("expired_count"),
            "idempotent": bool(result.get("idempotent")),
            "request_id": request_id,
        },
    )
