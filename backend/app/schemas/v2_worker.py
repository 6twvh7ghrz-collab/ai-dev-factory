"""V2 Worker API — Pydantic request / response models.

V2.0-B2e: Request/response schemas for the Worker Control Plane API Router.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ============================================================
# Request Models
# ============================================================


class RegisterWorkerRequest(BaseModel):
    """POST /api/v2/workers/register"""
    worker_id: str = Field(..., min_length=1, description="Unique worker identifier")
    worker_type: str = Field(..., description="One of: executor, supervisor, reviewer")
    provider: str = Field(default="", description="Provider name (e.g. openai, custom)")
    display_name: str = Field(default="", description="Human-readable name")
    capabilities: List[str] = Field(default_factory=list, description="Worker capability tags")
    sandbox_profile_id: str = Field(default="", description="Sandbox profile association")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata")


class ClaimTaskRequest(BaseModel):
    """POST /api/v2/tasks/{task_id}/claim"""
    worker_id: str = Field(..., min_length=1, description="Worker claiming the task")
    expected_version: int = Field(..., ge=1, description="Expected task state_version for optimistic lock")
    lease_seconds: int = Field(default=300, ge=30, le=3600, description="Lease duration in seconds (30-3600)")
    allowed_task_ids: List[int] = Field(default_factory=list, description="Task IDs the worker is allowed to claim")
    project_id: Optional[int] = Field(default=None, description="Project scope enforcement")


class HeartbeatRequest(BaseModel):
    """POST /api/v2/tasks/{task_id}/heartbeat"""
    assignment_id: str = Field(..., min_length=1, description="Assignment to renew")
    worker_id: str = Field(..., min_length=1, description="Worker owning the assignment")
    lease_token: str = Field(..., min_length=1, description="Lease token from claim response")
    extend_seconds: int = Field(default=300, ge=30, le=3600, description="Extension in seconds (30-3600)")


class SubmitTaskResultRequest(BaseModel):
    """POST /api/v2/tasks/{task_id}/submit"""
    model_config = {"protected_namespaces": ()}

    assignment_id: str = Field(..., min_length=1)
    worker_id: str = Field(..., min_length=1)
    lease_token: str = Field(..., min_length=1)
    expected_version: int = Field(..., ge=1)
    execution_id: str = Field(..., min_length=1)
    result_status: str = Field(..., description="Worker submissions must use submitted")
    files_modified: List[str] = Field(default_factory=list)
    files_checked: List[str] = Field(default_factory=list)
    diff_summary: str = ""
    tests: Dict[str, Any] = Field(default_factory=dict)
    git_commit: str = ""
    git_branch: str = ""
    base_commit: str = ""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    manual_actions: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    handoff_requested: bool = False
    remaining_steps: List[str] = Field(default_factory=list)
    submitted_at: str = Field(..., min_length=1)
    duration_ms: int = 0
    model_calls: int = 0
    repair_attempts: int = 0


class ReviewTaskRequest(BaseModel):
    """POST /api/v2/tasks/{task_id}/review"""
    action: str = Field(default="decide", description="begin or decide")
    result_id: str = Field(..., min_length=1)
    reviewer_id: str = Field(..., min_length=1)
    expected_version: int = Field(..., ge=1)
    decision: Optional[str] = None
    summary: str = ""
    issues: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    risk_level: str = "low"
    user_action_required: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class HandoffTaskRequest(BaseModel):
    """POST /api/v2/tasks/{task_id}/handoff"""
    action: str = Field(..., description="request, accept, reject, cancel, or expire")
    handoff_id: Optional[str] = None
    assignment_id: str = ""
    from_worker_id: str = ""
    to_worker_id: str = ""
    worker_id: str = ""
    actor_id: str = ""
    lease_token: str = ""
    expected_version: Optional[int] = None
    lease_seconds: int = Field(default=300, ge=30, le=3600)
    reason_code: str = ""
    reason: str = ""
    completed_steps: List[Any] = Field(default_factory=list)
    remaining_steps: List[Any] = Field(default_factory=list)
    files_changed: List[str] = Field(default_factory=list)
    tests_run: List[Any] = Field(default_factory=list)
    recent_errors: List[Any] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    forbidden_actions: List[str] = Field(default_factory=list)
    context_snapshot: Dict[str, Any] = Field(default_factory=dict)
    git_head: str = ""
    current_stage: str = ""
    expires_seconds: int = Field(default=3600, ge=60, le=86400)


# ============================================================
# Response sub-models
# ============================================================


class _WorkerInfo(BaseModel):
    """Worker info returned in register response (safe for external exposure)."""
    worker_id: str
    worker_type: str
    provider: str = ""
    display_name: str = ""
    status: str
    max_concurrency: int = 1
    current_load: int = 0
    sandbox_profile_id: str = ""
    capabilities: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    registered_at: Optional[str] = None
    last_seen_at: Optional[str] = None


class _TaskPacket(BaseModel):
    """Task execution packet returned in claim response."""
    task_id: int
    project_id: Optional[int] = None
    title: str = ""
    description: str = ""
    task_type: str = ""
    current_stage: str = "implementation"
    allowed_task_ids: List[int] = Field(default_factory=list)
    allowed_files: List[Any] = Field(default_factory=list)
    forbidden_actions: List[str] = Field(default_factory=list)
    test_commands: List[Any] = Field(default_factory=list)
    success_criteria: List[Any] = Field(default_factory=list)
    evidence_required: List[Any] = Field(default_factory=list)
    assignment_id: Optional[str] = None
    lease_token: Optional[str] = None
    lease_expires_at: Optional[str] = None
    state_version: Optional[int] = None
    git_head: Any = None
    dependencies: Any = None


# ============================================================
# Success response models
# ============================================================


class RegisterWorkerResponse(BaseModel):
    """POST /api/v2/workers/register — success response."""
    ok: bool = True
    worker: Optional[_WorkerInfo] = None
    idempotent: bool = False
    request_id: str = ""


class ClaimTaskResponse(BaseModel):
    """POST /api/v2/tasks/{task_id}/claim — success response."""
    ok: bool = True
    assignment_id: Optional[str] = None
    task_id: Optional[int] = None
    worker_id: Optional[str] = None
    lease_token: Optional[str] = None
    lease_token_reissued: bool = False
    lease_expires_at: Optional[str] = None
    task_packet: Optional[Dict[str, Any]] = None
    state_version: Optional[int] = None
    idempotent: bool = False
    request_id: str = ""


class HeartbeatResponse(BaseModel):
    """POST /api/v2/tasks/{task_id}/heartbeat — success response."""
    ok: bool = True
    heartbeat_id: Optional[str] = None
    assignment_id: Optional[str] = None
    task_id: Optional[int] = None
    worker_id: Optional[str] = None
    previous_expires_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    worker_last_seen_at: Optional[str] = None
    idempotent: bool = False
    request_id: str = ""


class SubmitTaskResultResponse(BaseModel):
    """POST /api/v2/tasks/{task_id}/submit success response."""
    ok: bool = True
    result_id: Optional[str] = None
    task_id: Optional[int] = None
    assignment_id: Optional[str] = None
    task_state: str = "RESULT_SUBMITTED"
    state_version: Optional[int] = None
    assignment_status: str = ""
    worker_status: str = ""
    artifact_count: int = 0
    idempotent: bool = False
    result_summary: Dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""


class ReviewTaskResponse(BaseModel):
    """POST /api/v2/tasks/{task_id}/review success response."""
    ok: bool = True
    decision_id: Optional[str] = None
    task_id: Optional[int] = None
    result_id: Optional[str] = None
    reviewer_id: Optional[str] = None
    previous_state: str = ""
    task_state: str = ""
    state_version: Optional[int] = None
    decision: str = ""
    summary: str = ""
    idempotent: bool = False
    request_id: str = ""


class HandoffTaskResponse(BaseModel):
    """POST /api/v2/tasks/{task_id}/handoff success response."""
    ok: bool = True
    handoff_id: Optional[str] = None
    task_id: Optional[int] = None
    project_id: Optional[int] = None
    status: str = ""
    from_worker_id: Optional[str] = None
    to_worker_id: Optional[str] = None
    assignment_id: Optional[str] = None
    lease_expires_at: Optional[str] = None
    expired_count: Optional[int] = None
    idempotent: bool = False
    request_id: str = ""


# ============================================================
# Generic error response
# ============================================================


class V2ErrorResponse(BaseModel):
    """Unified V2 error response envelope."""
    ok: bool = False
    error_code: str
    message: str
    request_id: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)
