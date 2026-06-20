/** 任务 API */
import api, { ApiResponse } from './client';

export interface Task {
  id: number;
  project_id: number;
  title: string;
  description: string | null;
  task_type: string;
  priority: string;
  dependencies: string[];
  files_to_check: string[];
  files_to_modify: string[];
  codex_prompt: string | null;
  test_steps: string[];
  acceptance_criteria: string[];
  status: string;
  readiness_status: string;
  execution_result: string | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export async function listTasks(projectId: number) {
  const res = await api.get<ApiResponse<Task[]>>(`/projects/${projectId}/tasks`);
  return res.data;
}

export async function getTask(taskId: number) {
  const res = await api.get<ApiResponse<Task>>(`/tasks/${taskId}`);
  return res.data;
}

export async function updateTask(taskId: number, data: Partial<Task>) {
  const res = await api.put<ApiResponse<Task>>(`/tasks/${taskId}`, data);
  return res.data;
}

export async function generateCodexPrompt(taskId: number) {
  const res = await api.post<ApiResponse>(`/tasks/${taskId}/generate-codex-prompt`);
  return res.data;
}

export async function completeTask(taskId: number) {
  const res = await api.post<ApiResponse>(`/tasks/${taskId}/complete`);
  return res.data;
}

export async function retryTask(taskId: number) {
  const res = await api.post<ApiResponse>(`/tasks/${taskId}/retry`);
  return res.data;
}
