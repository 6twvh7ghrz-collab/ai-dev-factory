/** 规划预览 API V1.4 */

import api, { ApiResponse } from './client';

// ── 类型定义 ──

export interface PlannerPreviewRequest {
  project_id: number;
  task_ids?: number[];
  force_regenerate?: boolean;
}

export interface DataSourceStrategy {
  primary: string;
  fallbacks: string[];
}

export interface TaskPlanItem {
  task_id: number;
  title: string;
  recommended_status: 'needs_planning' | 'ready';
  implementation_strategy: string;
  files_to_modify_suggestion: string[];
  test_strategy: string[];
  dependencies: string[];
  risks: string[];
  requires_approval: boolean;
  data_source_strategy: DataSourceStrategy;
  risk_level?: 'LOW' | 'MEDIUM' | 'HIGH' | 'BLOCKED';
}

export interface PlanPreview {
  project_summary: string;
  recommended_architecture: string;
  execution_order: number[];
  tasks: TaskPlanItem[];
  global_risks: string[];
  approval_items: string[];
  next_step: 'review_plan' | 'approve_and_execute' | 'request_manual_review';
}

export interface PlanCallRecord {
  provider: string;
  model: string;
  request_id: string;
  started_at: string;
  finished_at: string;
  input_tokens: number;
  output_tokens: number;
  success: boolean;
  error: string;
  from_cache?: boolean;
}

export interface PlannerPreviewResponse {
  ok: boolean;
  code: string;
  executed: boolean;
  project_id: number;
  project_name: string;
  preview_id?: string;
  expires_at?: string;
  preview: PlanPreview | null;
  call_record: PlanCallRecord | null;
  message?: string;
}

// ── V1.4 审批相关类型 ──

export interface ApprovalPreviewRequest {
  project_id: number;
  preview_id: string;
  selected_task_ids: number[];
}

export interface ApprovalPreviewResponse {
  ok: boolean;
  code: string;
  confirmation_token: string;
  expires_in: number;
  safe_tasks: TaskApprovalItem[];
  medium_risk_tasks: TaskApprovalItem[];
  high_risk_tasks: TaskApprovalItem[];
  blocked_tasks: TaskApprovalItem[];
  writeback_preview: Record<string, unknown>;
}

export interface TaskApprovalItem {
  task_id: number;
  title: string;
  risk_level: string;
  reason: string;
  fields_to_write: Record<string, unknown>;
}

export interface ApproveRequest {
  project_id: number;
  preview_id: string;
  selected_task_ids: number[];
  confirmation_token: string;
}

export interface ApproveResponse {
  ok: boolean;
  code: string;
  approved_task_ids: number[];
  kept_needs_planning_task_ids: number[];
  runnable_count: number;
  executed: boolean;
}

export interface RejectRequest {
  project_id: number;
  preview_id: string;
}

/** 生成工程规划预览 */
export async function generatePlanPreview(
  projectId: number,
  taskIds?: number[],
  forceRegenerate?: boolean,
): Promise<ApiResponse<PlannerPreviewResponse>> {
  const res = await api.post<ApiResponse<PlannerPreviewResponse>>('/planner/preview', {
    project_id: projectId,
    task_ids: taskIds || [],
    force_regenerate: forceRegenerate || false,
  });
  return res.data;
}

/** 获取规划预览详情 */
export async function getPreview(previewId: string): Promise<ApiResponse<PlannerPreviewResponse>> {
  const res = await api.get<ApiResponse<PlannerPreviewResponse>>(`/planner/previews/${previewId}`);
  return res.data;
}

/** 审批预检 */
export async function approvalPreview(
  req: ApprovalPreviewRequest,
): Promise<ApiResponse<ApprovalPreviewResponse>> {
  const res = await api.post<ApiResponse<ApprovalPreviewResponse>>('/planner/approval-preview', req);
  return res.data;
}

/** 正式批准 */
export async function approve(req: ApproveRequest): Promise<ApiResponse<ApproveResponse>> {
  const res = await api.post<ApiResponse<ApproveResponse>>('/planner/approve', req);
  return res.data;
}

/** 拒绝规划 */
export async function rejectPlan(req: RejectRequest): Promise<ApiResponse<{ ok: boolean }>> {
  const res = await api.post<ApiResponse<{ ok: boolean }>>('/planner/reject', req);
  return res.data;
}
