/** 执行器 API */
import api, { ApiResponse } from './client';

// ── 类型定义 ──

export interface ExecutorLoopStatus {
  running: boolean;
  run: ExecutorRun | null;
  is_paused: boolean;
  is_stopping: boolean;
}

export interface ExecutorRun {
  id: number;
  run_id: string;
  project_id: number;
  current_task_id: number | null;
  worker_id: string | null;
  status: string;
  mode: string;
  started_at: string | null;
  heartbeat_at: string | null;
  finished_at: string | null;
  tasks_completed: number;
  tasks_blocked: number;
  tasks_failed: number;
  tasks_repaired: number;
  tasks_skipped: number;
  tasks_total: number;
  current_step: string | null;
  pause_reason: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkerStatus {
  worker_id: string;
  status: string;
  current_task_id: number | null;
  current_execution_id: number | null;
  started_at: string | null;
}

export interface ResourceLock {
  id: number;
  lock_type: string;
  resource_key: string;
  worker_id: string;
  execution_id: number | null;
  status: string;
  created_at: string;
  expires_at: string | null;
}

export interface MergeQueueItem {
  execution_id: number;
  task_id: number;
  worker_id: string;
  branch: string;
  status: string;
  created_at: string;
}

export interface ExecutionRecord {
  id: number;
  task_id: number;
  project_id: number;
  worker_id: string | null;
  status: string;
  worktree_path: string | null;
  start_commit: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  repair_count: number;
  test_result: string | null;
  exit_code: number | null;
  error_message: string | null;
  safety_passed: boolean;
  files_modified: string[] | null;
}

export interface ExecutorStatus {
  loop: ExecutorLoopStatus | null;
  recent_runs: ExecutorRun[];
  available_adapters: { name: string; version: string }[];
  workers: {
    list: WorkerStatus[];
    active_count: number;
    max_workers: number;
  };
  resource_locks: {
    locks: ResourceLock[];
    count: number;
  };
  merge_queue: {
    queue: MergeQueueItem[];
    is_merging: boolean;
  };
  running_executions: { id: number; task_id: number; status: string; started_at: string }[];
  recent_executions: { id: number; task_id: number; status: string; test_result: string; duration_ms: number }[];
}

export interface QueueStatus {
  pending: number;
  claiming: number;
  executing: number;
  testing: number;
  repairing: number;
  waiting_merge: number;
  completed: number;
  blocked: number;
  paused: number;
  total: number;
  tasks: Array<{
    id: number;
    title: string;
    status: string;
    priority: string;
    task_type: string;
  }>;
}

export interface ExecutionLog {
  id: number;
  execution_id: number;
  step_name: string;
  step_status: string;
  command: string | null;
  exit_code: number | null;
  duration_ms: number | null;
  detail: string | null;
}

// ── API 方法 ──

/** 被阻塞任务的诊断详情 */
export interface BlockedTaskInfo {
  task_id: number;
  title: string;
  blocked_reasons: string[];
  readiness_status?: string;
}

/** Preflight 检查响应 */
export interface PreflightResponse {
  can_start: boolean;
  runnable_task_ids: number[];
  blocked_task_ids: number[];
  retryable_tasks: Array<{ id: number; status: string }>;
  active_run: boolean;
  active_run_status: string | null;
  active_leases: number;
  database_path: string;
  pid: number;
}

/** 启动响应 */
export interface StartResponse {
  started: boolean;
  already_running: boolean;
  run_id: string | null;
  status: string | null;
  message: string;
  code?: string;
  reason?: string;
  total_tasks?: number;
  pending_tasks?: number;
  provider?: string;
  runnable_tasks?: number;
  /** 阻塞原因分类统计 */
  blocked_by_category?: Record<string, number>;
  /** 每个阻塞任务的详细原因 */
  blocked_tasks?: BlockedTaskInfo[];
  /** 被阻塞的任务总数 */
  blocked_count?: number;
  /** 尚未完成工程规划的任务数量 */
  not_ready_count?: number;
}

/** 启动自动循环 */
export async function startExecutor(projectId: number, mode = 'auto_until_blocked'): Promise<ApiResponse<StartResponse>> {
  const res = await api.post<ApiResponse<StartResponse>>('/executor/start', null, {
    params: { project_id: projectId, mode },
  });
  return res.data;
}

/** 暂停循环 */
export async function pauseExecutor() {
  const res = await api.post<ApiResponse>('/executor/pause');
  return res.data;
}

/** 恢复循环 */
export async function resumeExecutor() {
  const res = await api.post<ApiResponse>('/executor/resume');
  return res.data;
}

/** 停止循环 */
export async function stopExecutor() {
  const res = await api.post<ApiResponse>('/executor/stop');
  return res.data;
}

/** 获取执行器完整状态 */
export async function getExecutorStatus(): Promise<ApiResponse<ExecutorStatus>> {
  const res = await api.get<ApiResponse<ExecutorStatus>>('/executor/status');
  return res.data;
}

/** 获取队列状态 */
export async function getExecutorQueue(projectId: number): Promise<ApiResponse<QueueStatus>> {
  const res = await api.get<ApiResponse<QueueStatus>>('/executor/queue', {
    params: { project_id: projectId },
  });
  return res.data;
}

/** 获取 Worker 列表 */
export async function getWorkers() {
  const res = await api.get<ApiResponse>('/executor/workers');
  return res.data;
}

/** 获取资源锁 */
export async function getResourceLocks(params?: { project_id?: number; worker_id?: string }) {
  const res = await api.get<ApiResponse>('/executor/resource-locks', { params });
  return res.data;
}

/** 获取合并队列 */
export async function getMergeQueue() {
  const res = await api.get<ApiResponse>('/executor/merge-queue');
  return res.data;
}

/** 获取执行记录 */
export async function getExecutions(params?: { task_id?: number; status?: string; limit?: number }) {
  const res = await api.get<ApiResponse<ExecutionRecord[]>>('/executor/executions', { params });
  return res.data;
}

/** 获取单条执行详情 */
export async function getExecutionDetail(executionId: number) {
  const res = await api.get<ApiResponse>(`/executor/executions/${executionId}`);
  return res.data;
}

/** 获取执行日志 */
export async function getExecutionLogs(params?: { execution_id?: number; limit?: number }): Promise<ApiResponse<ExecutionLog[]>> {
  const res = await api.get<ApiResponse<ExecutionLog[]>>('/executor/logs', { params });
  return res.data;
}

/** 执行前完整检查 */
export async function getPreflight(projectId: number): Promise<ApiResponse<PreflightResponse>> {
  const res = await api.get<ApiResponse<PreflightResponse>>('/executor/preflight', {
    params: { project_id: projectId },
  });
  return res.data;
}

// ── AI 自然语言指令预览 ──

export interface AICommandPreviewRequest {
  project_id: number;
  text: string;
}

export interface AICommandPreviewResponse {
  project_id: number;
  intent: string;
  confidence: number;
  requires_confirmation: boolean;
  action: string;
  message: string;
  executed: boolean;
  confirmation_token?: string;  // V1.2: 为需要确认的写意图生成
}

/** 预览自然语言指令 */
export async function previewAICommand(
  projectId: number,
  text: string,
): Promise<ApiResponse<AICommandPreviewResponse>> {
  const res = await api.post<ApiResponse<AICommandPreviewResponse>>('/ai-command/preview', {
    project_id: projectId,
    text,
  });
  return res.data;
}

// ── AI 只读指令执行（V1.1）──

export interface AICommandExecuteReadonlyResponse {
  project_id: number;
  intent: string;
  executed: boolean;
  action: string;
  data: ShowStatusData | DiagnoseBlockerData | null;
  message: string;
  code?: string;
}

/** show_status 返回数据 */
export interface ShowStatusData {
  project_id: number;
  project_name: string;
  run_status: string;
  current_task: { task_id: number; title: string; status: string } | null;
  worker_count: number;
  pending_count: number;
  ready_count: number;
  needs_planning_count: number;
  completed_count: number;
  blocked_count: number;
  total_count: number;
  active_leases: number;
  active_resource_locks: number;
  last_error: string | null;
}

/** diagnose_blocker 返回数据 */
export interface DiagnoseBlockerData {
  status: string;
  summary: string;
  categories: {
    needs_planning: number;
    dependency_incomplete: number;
    active_lease: number;
    missing_files: number;
    manual_approval: number;
    missing_prompt: number;
    missing_test_steps: number;
    missing_acceptance_criteria: number;
    missing_implementation_steps: number;
  };
  total_pending: number;
  runnable_count: number;
  blocked_count: number;
  tasks: Array<{
    task_id: number;
    title: string;
    reason: string;
    all_reasons: string[];
    readiness_status: string;
  }>;
}

/** 执行只读自然语言指令 */
export async function executeReadonlyAICommand(
  projectId: number,
  text: string,
  confirmedIntent: string,
): Promise<ApiResponse<AICommandExecuteReadonlyResponse>> {
  const res = await api.post<ApiResponse<AICommandExecuteReadonlyResponse>>('/ai-command/execute-readonly', {
    project_id: projectId,
    text,
    confirmed_intent: confirmedIntent,
  });
  return res.data;
}

// ── AI 写指令执行（V1.2）──

/** 写指令执行响应 */
export interface AICommandExecuteResponse {
  project_id: number;
  intent: string;
  executed: boolean;
  action?: string;
  data?: unknown;
  run_id?: string;
  task_id?: number;
  message: string;
  code?: string;
}

/** 安全执行自然语言写指令（需确认令牌） */
export async function executeAICommand(
  projectId: number,
  text: string,
  confirmedIntent: string,
  confirmationToken: string,
): Promise<ApiResponse<AICommandExecuteResponse>> {
  const res = await api.post<ApiResponse<AICommandExecuteResponse>>('/ai-command/execute', {
    project_id: projectId,
    text,
    confirmed_intent: confirmedIntent,
    confirmation_token: confirmationToken,
  });
  return res.data;
}

// ── 项目执行配置 ──

/** 项目执行配置 */
export interface ProjectExecutionConfig {
  configured: boolean;
  project_id: number;
  project_name: string;
  workspace_path: string;
  workspace_name: string;
  execution_enabled: boolean;
  execution_mode: string;
  allowed_models: string[];
  max_workers: number;
  max_tasks: number;
  requires_confirmation: boolean;
  message?: string;
}

/** 获取项目执行配置 */
export async function getProjectExecutionConfig(projectId: number): Promise<ApiResponse<ProjectExecutionConfig>> {
  const res = await api.get<ApiResponse<ProjectExecutionConfig>>(`/executor/project-config/${projectId}`);
  return res.data;
}

// ── V1.2.2 统一启动决策 ──

/** 启动决策类型 */
export type StartDecision =
  | 'EXECUTE_READY_TASKS'
  | 'PLAN_EXISTING_TASKS'
  | 'GENERATE_TASKS'
  | 'BIND_WORKSPACE'
  | 'WAIT_DEPENDENCIES'
  | 'REQUEST_APPROVAL'
  | 'ALREADY_RUNNING'
  | 'PROJECT_COMPLETED'
  | 'BLOCK_UNSAFE';

/** 启动决策详情 */
export interface StartDecisionDetails {
  pending: number;
  ready: number;
  needs_planning: number;
  runnable: number;
  completed: number;
  blocked: number;
  execution_enabled: boolean;
  workspace_path: string;
  workspace_exists: boolean;
  git_valid: boolean;
  git_clean: boolean;
  active_run: boolean;
  active_run_status: string;
  is_high_risk: boolean;
}

/** 启动决策响应 */
export interface StartDecisionResponse {
  ok: boolean;
  project_id: number;
  project_name: string;
  decision: StartDecision;
  can_execute: boolean;
  can_plan: boolean;
  can_generate_tasks: boolean;
  requires_workspace: boolean;
  requires_approval: boolean;
  summary: string;
  details: StartDecisionDetails;
  error?: string;
}

/** 获取统一启动决策 */
export async function getStartDecision(projectId: number): Promise<ApiResponse<StartDecisionResponse>> {
  const res = await api.get<ApiResponse<StartDecisionResponse>>('/executor/start-decision', {
    params: { project_id: projectId },
  });
  return res.data;
}
