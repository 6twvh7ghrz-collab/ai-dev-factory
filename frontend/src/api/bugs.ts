/** Bug API - 完整生命周期 */
import api, { type ApiResponse } from './client';

export interface BugStatusLog {
  id: number;
  bug_id: number;
  from_status: string | null;
  to_status: string;
  from_status_label: string | null;
  to_status_label: string;
  reason: string | null;
  created_at: string;
}

export interface Bug {
  id: number;
  project_id: number;
  title: string;
  description: string | null;
  error_message: string | null;
  reproduction_steps: string | null;
  expected_result: string | null;
  actual_result: string | null;
  bug_type: string | null;
  severity: string | null;
  probable_cause: string[];
  affected_module: string | null;
  affected_files: string[];
  fix_plan: string[];
  regression_risks: string[];
  fix_prompt: string | null;
  test_steps: string[];
  is_blocking: string | null;
  execution_result: string | null;
  files_changed: string | null;
  test_result: string | null;
  remaining_issues: string | null;
  executed_at: string | null;
  status: string;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
  status_logs?: BugStatusLog[];
}

export async function createBug(projectId: number, data: {
  title: string;
  description?: string;
  error_message?: string;
  reproduction_steps?: string;
  expected_result?: string;
  actual_result?: string;
}) {
  const res = await api.post<ApiResponse<Bug>>(`/projects/${projectId}/bugs`, data);
  return res.data;
}

export async function listBugs(projectId: number) {
  const res = await api.get<ApiResponse<Bug[]>>(`/projects/${projectId}/bugs`);
  return res.data;
}

export async function getBug(bugId: number) {
  const res = await api.get<ApiResponse<Bug & { status_logs: BugStatusLog[] }>>(`/bugs/${bugId}`);
  return res.data;
}

export async function analyzeBug(bugId: number) {
  const res = await api.post<ApiResponse<Bug>>(`/bugs/${bugId}/analyze`);
  return res.data;
}

export async function generateFixPrompt(bugId: number) {
  const res = await api.post<ApiResponse<Bug>>(`/bugs/${bugId}/generate-fix-prompt`);
  return res.data;
}

export async function updateBugStatus(bugId: number, status: string, reason?: string) {
  const res = await api.put<ApiResponse<Bug>>(`/bugs/${bugId}/status`, { status, reason });
  return res.data;
}

export async function saveExecutionResult(bugId: number, data: {
  execution_result: string;
  files_changed?: string;
  test_result?: string;
  remaining_issues?: string;
}) {
  const res = await api.post<ApiResponse<Bug>>(`/bugs/${bugId}/execution-result`, data);
  return res.data;
}

export async function saveTestResult(bugId: number, data: {
  passed: boolean;
  test_notes?: string;
  checklist?: string[];
}) {
  const res = await api.post<ApiResponse<Bug>>(`/bugs/${bugId}/test-result`, data);
  return res.data;
}

export async function getBugStatusLogs(bugId: number) {
  const res = await api.get<ApiResponse<BugStatusLog[]>>(`/bugs/${bugId}/status-logs`);
  return res.data;
}
