/** AI 分析 API */
import api, { ApiResponse } from './client';

export async function analyzeRequirements(projectId: number) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/analyze`);
  return res.data;
}

export async function generateMvp(projectId: number) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/generate-mvp`);
  return res.data;
}

export async function generateModules(projectId: number) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/generate-modules`);
  return res.data;
}

export async function generateDatabase(projectId: number) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/generate-database`);
  return res.data;
}

export async function generateApis(projectId: number) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/generate-apis`);
  return res.data;
}

export async function generateTasks(projectId: number) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/generate-tasks`);
  return res.data;
}

export async function getAnalysis(projectId: number) {
  const res = await api.get<ApiResponse>(`/projects/${projectId}/analysis`);
  return res.data;
}

export async function listModules(projectId: number) {
  const res = await api.get<ApiResponse>(`/projects/${projectId}/modules`);
  return res.data;
}
